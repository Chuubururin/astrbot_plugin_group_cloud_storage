"""Resources write domain — upsert, album/essence ingest, field/tag updates.

Single-transaction batch semantics: ``_apply_upsert`` runs inside the
caller's transaction (no commits), so album/essence reconcile + insert can
be atomic (folders.py used to commit the DELETEs, then upsert in a second
transaction — a crash between the two dropped album/essence rows for good).
"""
from __future__ import annotations

import json
import sqlite3
import time

from .resources import (
    _RENAME_LOGICAL_KEY,
    _RESOURCE_CONFLICT_BODY,
    _RESOURCE_FIELD_WHITELIST,
    _plan_reparents,
    _reparent_volumes,
)
from .row_identity import _logical_key, _logical_path, _path_for
from .state import StorePart

from core.domain.enums import ResourceStatus, ResourceType
from core.domain.resource import Resource


def _inherit_composition_identity(
    conn: sqlite3.Connection, items: list[Resource]
) -> None:
    """Safety net for composition parents that predate ``logical_key``.

    Normally the ON CONFLICT(logical_key) clause collapses a restarted
    session's file_id onto the live row, so nothing stale forms here. It
    stays for rows already duplicated (v29 keeps their losers as
    tombstones) and as a guard if one ever escapes the index.

    A stale same-(group_id, name) parent hands its ``meta`` to the
    successor and is TOMBSTONED, never hard-removed -- the partial index
    tolerates history. Rows whose ``source_ref`` is still listed are left
    alone: two distinct cloud files may share a name in one folder, and
    only a vanished session handle proves identity.
    """
    if not any(
        r.type == ResourceType.FILE and r.source_ref for r in items
    ):
        return
    # Indexed by idx_res_name; composition parents are rare per group.
    comp_rows = conn.execute(
        """
        SELECT resource_id, group_id, name, source_ref, meta FROM resources
        WHERE type='file'
          -- Both parent shapes: canonical composition.kind and the
          -- legacy {'volumes': true} from ingest/video.py. json_valid
          -- first -- json_extract raises on a non-NULL invalid meta and
          -- would abort the whole batch.
          AND json_valid(meta)
          AND (json_extract(meta, '$.composition.kind') = 'volumes'
               OR json_extract(meta, '$.volumes') = 1)
        """
    ).fetchall()
    if not comp_rows:
        return
    incoming_refs = {r.source_ref for r in items if r.source_ref}
    # Successor lookup: newest incoming row wins when the same name is
    # listed several times in one batch.
    successors: dict[tuple[str, str], Resource] = {}
    for r in items:
        if r.type == ResourceType.FILE and r.source_ref:
            successors[(r.group_id, r.name)] = r
    for pred in comp_rows:
        key = (pred["group_id"], pred["name"])
        succ = successors.get(key)
        if (
            succ is None
            or succ.resource_id == pred["resource_id"]
            or pred["source_ref"] in incoming_refs
        ):
            continue
        pred_id, pred_meta = pred["resource_id"], pred["meta"]
        try:
            merged = json.loads(pred_meta) if pred_meta else {}
        except (TypeError, ValueError):
            merged = {}
        conn.execute(
            "UPDATE resources SET meta=? WHERE resource_id=?",
            (json.dumps(merged, ensure_ascii=False), succ.resource_id),
        )
        # BEFORE tombstoning -- parent_resource_id has no FK backstop.
        conn.execute(
            "UPDATE volumes SET parent_resource_id=? WHERE parent_resource_id=?",
            (succ.resource_id, pred_id),
        )
        # Tombstone, not DELETE: the partial index tolerates history.
        conn.execute(
            "UPDATE resources SET status=?, size=0, updated_at=? "
            "WHERE resource_id=?",
            (ResourceStatus.DELETED.value, int(time.time()), pred_id),
        )


# A row whose meta.kind == 'text_split' is self-built (long-text split
# parts), never listed by the cloud -- reconcile must not delete it.
_NOT_TEXT_SPLIT = (
    "(meta IS NULL OR NOT json_valid(meta) "
    "OR COALESCE(json_extract(meta, '$.kind'), '') != 'text_split')"
)


def _apply_row_chunk(conn: sqlite3.Connection, chunk: list[tuple]) -> int:
    """One <=500-row executemany of the upsert batch (variable-limit chunk).

    Two ON CONFLICT targets, both required: a violation reaches only a
    NAMED handler, and the partial predicate spares tombstones so a file
    can return. Both bodies are identical because SQLite applies the FIRST
    matching clause and skips the rest -- a thin resource_id clause would
    shadow the full one on a same-session re-index. See
    _RESOURCE_CONFLICT_BODY.
    """
    cur = conn.executemany(
        f"""
        INSERT INTO resources
          (resource_id, group_id, type, name, size, uploader_id,
           uploader_name, source_ref, busid, folder_id, folder_name,
           status, tags, meta, created_at, indexed_at, updated_at,
           path, ext, logical_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(resource_id) DO UPDATE SET
{_RESOURCE_CONFLICT_BODY}

        ON CONFLICT(group_id, type, logical_key)
          WHERE status != 'deleted'
        DO UPDATE SET
{_RESOURCE_CONFLICT_BODY}
        """,
        chunk,
    )
    return cur.rowcount


def _apply_upsert(
    conn: sqlite3.Connection, items: list[Resource], now: int
) -> int:
    """Apply the batch in the caller's transaction; commits nothing.

    Chunking bounds the per-statement parameter block (executemany rebinds
    20 placeholders per row); it is NOT a savepoint scheme -- the whole batch
    is one atomic upsert, with the composition-identity chase in the same
    transaction (a half-moved set orphans parts for good).
    """
    moves = _plan_reparents(conn, items)
    rows = []
    for r in items:
        path, ext = _logical_path(r)
        rows.append(
            (
                r.resource_id,
                r.group_id,
                r.type.value,
                r.name,
                r.size,
                r.uploader_id,
                r.uploader_name,
                r.source_ref,
                r.busid,
                r.folder_id,
                r.folder_name,
                r.status.value,
                json.dumps(r.tags, ensure_ascii=False),
                json.dumps(r.meta, ensure_ascii=False),
                r.created_at,
                now,
                now,
                path,
                ext,
                _logical_key(r),
            )
        )
    n = 0
    for i in range(0, len(rows), 500):
        n += _apply_row_chunk(conn, rows[i : i + 500])
    # AFTER every chunk on purpose: mid-loop it would touch successor
    # rows a later chunk has not inserted yet, and drop their meta.
    _inherit_composition_identity(conn, items)
    if moves:
        _reparent_volumes(conn, moves)
    return n


class ResourceWriteMixin(StorePart):
    """Resource write operations (upsert / album-essence ingest / update)."""

    async def upsert_resources(self, items: list[Resource]) -> int:
        if not items:
            return 0
        now = int(time.time())

        def _do(conn: sqlite3.Connection):
            # No explicit BEGIN: sqlite3 auto-begins before DML and a
            # second BEGIN conflicts with the one already in progress.
            try:
                n = _apply_upsert(conn, items, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return n

        result = await self._conn.exec(_do)
        self._tag_cloud_cache = {}
        return result

    async def upsert_album_essence(
        self, group_id: str, albums: list, essences: list, account_id: str = ""
    ) -> None:
        """Reconcile + ingest album/essence rows in ONE transaction.

        ``account_id`` is accepted for port symmetry (group-scan callers pass
        it through); the resources table is keyed by group_id only.
        """
        rows = _build_album_essence_rows(group_id, albums, essences)

        def _do(conn: sqlite3.Connection):
            try:
                _reconcile_album_essence(conn, group_id, rows)
                _apply_upsert(conn, rows, int(time.time()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._conn.exec(_do)
        self._tag_cloud_cache = {}

    async def update_resource_fields(self, id: int, **fields) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - _RESOURCE_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid resource fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            cur = conn.execute(
                f"UPDATE resources SET {sets}, updated_at=? WHERE id=?",
                [*fields.values(), int(time.time()), id],
            )
            if "name" in fields:
                row = conn.execute(
                    "SELECT group_id, type, name, folder_name FROM resources WHERE id=?",
                    (id,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE resources SET path=? WHERE id=?",
                        (_path_for(row["group_id"], row["type"], row["name"],
                                   row["folder_name"]), id),
                    )
                    conn.execute(_RENAME_LOGICAL_KEY, (id,))
            conn.commit()
            return cur.rowcount

        await self._conn.exec(_do)
        self._tag_cloud_cache = {}

    async def update_resource_tags(self, id: int, tags: list[str]) -> None:
        cleaned = sorted({str(t).strip() for t in tags if str(t).strip()})
        await self.update_resource_fields(
            id, tags=json.dumps(cleaned, ensure_ascii=False)
        )

    async def mark_missing_as_deleted(
        self, group_id: str, complete: bool, source_file_ids: set[str]
    ) -> int:
        """Mark active files absent from a complete source listing as deleted.

        The source IDs are staged in a temporary table instead of being split into
        ``NOT IN`` batches.  Batching changes the meaning of the predicate (a row
        present in a later batch can be deleted by an earlier batch), and also
        made an empty complete listing impossible to represent.
        """
        if not complete:
            return 0

        def _do(conn: sqlite3.Connection):
            # BUG-2 fix: removed explicit BEGIN — Python sqlite3 auto-begins
            # transactions before DML; an explicit BEGIN would conflict with
            # any implicit transaction already in progress (OperationalError).
            # The ConnectionManager._run() rolls back uncommitted transactions
            # in its finally block, providing the safety net.
            try:
                conn.execute("CREATE TEMP TABLE sync_source_ids (source_ref TEXT PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO sync_source_ids(source_ref) VALUES (?)",
                    ((str(source_ref),) for source_ref in source_file_ids),
                )
                cur = conn.execute(
                    """
                    UPDATE resources SET status=?, updated_at=?
                    WHERE group_id=? AND type='file' AND status='active'
                      AND NOT EXISTS (
                          SELECT 1 FROM sync_source_ids s
                          WHERE s.source_ref = resources.source_ref
                      )
                      -- NOT json_valid(meta): a corrupt row cannot prove it
                      -- is a volume parent, and json_extract would raise
                      -- "malformed JSON" and fail the whole sweep.
                      AND (meta IS NULL OR NOT json_valid(meta)
                           OR COALESCE(json_extract(meta, '$.volumes'), 0) != 1)
                    """,
                    [ResourceStatus.DELETED.value, int(time.time()), group_id],
                )
                n = cur.rowcount
                conn.execute("DROP TABLE sync_source_ids")
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                # Rollback removes the temporary table creation as well, but drop
                # defensively for connection implementations that retain temp DDL.
                conn.execute("DROP TABLE IF EXISTS sync_source_ids")
                raise

        result = await self._conn.exec(_do)
        if result:
            self._tag_cloud_cache = {}
        return result


# ---------------------------------------------------------------------------
# album/essence ingest helpers (moved out of folders.py — they write resource
# rows and share the upsert transaction machinery)
# ---------------------------------------------------------------------------


def _build_album_essence_rows(
    group_id: str, albums: list, essences: list
) -> list[Resource]:
    rows: list[Resource] = []
    for a in albums:
        rows.append(
            Resource(
                group_id=group_id,
                type=ResourceType.ALBUM,
                name=str(
                    a.get("name")
                    or a.get("album_name")
                    or a.get("album_id")
                    or "相册"
                ),
                source_ref=str(a.get("album_id") or ""),
                size=0,
                uploader_id=str(
                    a.get("owner")
                    or a.get("creator_id")
                    or a.get("create_uin")
                    or ""
                )
                or None,
                uploader_name=str(
                    a.get("creator_name") or a.get("create_nick") or ""
                )
                or None,
                created_at=int(a.get("create_time", 0) or 0),
                meta={
                    "album_id": str(a.get("album_id") or ""),
                    "desc": str(a.get("desc") or ""),
                    # summary feeds FTS search (the description channel of
                    # album title-or-description search)
                    "summary": str(a.get("desc") or ""),
                    "upload_number": int(a.get("upload_number", 0) or 0),
                    "cover_url": str(a.get("cover_url") or a.get("cover") or ""),
                },
            )
        )
    for e in essences:
        raw_content = e.get("content")
        segs = raw_content if isinstance(raw_content, list) else []
        seg_types = [str(s.get("type") or "") for s in segs if isinstance(s, dict)]
        if segs:
            text = (
                " ".join(
                    str((s.get("data") or {}).get("text") or "")
                    for s in segs
                    if isinstance(s, dict) and s.get("type") == "text"
                )
                .replace("\n", " ")
                .strip()
            )
            etype = ",".join(dict.fromkeys(seg_types)) or "text"
            is_img = bool(seg_types) and all(
                t in ("image", "video") for t in seg_types
            )
        else:
            text = (
                str(e.get("content") or e.get("text") or "")
                .replace("\n", " ")
                .strip()
            )
            etype = str(e.get("type") or e.get("message_type") or "text")
            is_img = etype in ("image", "video")
        name = "[图片/视频]" if is_img else (text[:80] or "精华消息")
        rows.append(
            Resource(
                group_id=group_id,
                type=ResourceType.ESSENCE,
                name=name,
                source_ref=str(e.get("message_id") or e.get("message_seq") or ""),
                size=0,
                uploader_id=str(e.get("sender_id") or e.get("user_id") or "")
                or None,
                uploader_name=str(
                    e.get("sender_nick") or e.get("sender_name") or ""
                )
                or None,
                created_at=int(e.get("time", 0) or 0),
                # summary holds up to 2000 chars: FTS coverage window for
                # essence content search
                meta={"essence": True, "msg_type": etype, "summary": text[:2000]},
            )
        )
    return rows


def _reconcile_album_essence(
    conn: sqlite3.Connection, group_id: str, rows: list[Resource]
) -> None:
    """Delete stale album/essence rows; runs in the caller's transaction.

    A non-NULL but malformed meta makes json_extract raise "malformed
    JSON". The old text guard (meta NOT LIKE ...) was tolerant of that;
    this DELETE runs on every album/essence ingest, so one bad row used to
    abort the whole _reconcile (commit + upsert_resources never ran).
    NOT json_valid(meta) keeps such rows in scope: they cannot prove they
    are a self-built text_split row, so they reconcile away like before.
    """
    for t in ("album", "essence"):
        refs = [r.source_ref for r in rows if r.type.value == t and r.source_ref]
        if refs:
            marks = ",".join("?" for _ in refs)
            conn.execute(
                f"DELETE FROM resources WHERE group_id=? AND type=? "
                f"AND source_ref NOT IN ({marks}) "
                f"AND {_NOT_TEXT_SPLIT}",
                [group_id, t, *refs],
            )
        else:
            conn.execute(
                f"DELETE FROM resources WHERE group_id=? AND type=? "
                f"AND {_NOT_TEXT_SPLIT}",
                (group_id, t),
            )
