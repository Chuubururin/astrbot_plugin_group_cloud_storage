"""Folders domain — folder CRUD, album/essence reconciliation."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .state import StorePart

from core.domain.enums import ResourceType
from core.domain.resource import Resource

if TYPE_CHECKING:
    from .connection import ConnectionManager


class FoldersMixin(StorePart):
    """Folder and album/essence operations."""

    if TYPE_CHECKING:
        _conn: ConnectionManager
        upsert_resources: object  # provided by ResourcesMixin at runtime

    async def upsert_folders(self, group_id: str, folders) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                for f in folders:
                    conn.execute(
                        """INSERT INTO folders (group_id, folder_id, folder_name, parent_id)
                           VALUES (?,?,?,?)
                           ON CONFLICT(group_id, folder_id) DO UPDATE SET
                             folder_name=excluded.folder_name,
                             parent_id=COALESCE(excluded.parent_id, folders.parent_id)
                        """,
                        (
                            group_id,
                            f["folder_id"],
                            f.get("folder_name", ""),
                            f.get("parent_id", ""),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._conn.exec(_do)

    async def upsert_album_essence(
        self, group_id: str, albums: list, essences: list, account_id: str = ""
    ) -> None:
        rows = []
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

        def _reconcile(conn: sqlite3.Connection):
            for t in ("album", "essence"):
                refs = [
                    r.source_ref for r in rows if r.type.value == t and r.source_ref
                ]
                if refs:
                    marks = ",".join("?" for _ in refs)
                    conn.execute(
                        f"DELETE FROM resources WHERE group_id=? AND type=? "
                        f"AND source_ref NOT IN ({marks}) "
                        f'AND (meta IS NULL OR meta NOT LIKE \'%"kind": "text_split"%\')',
                        [group_id, t, *refs],
                    )
                else:
                    conn.execute(
                        "DELETE FROM resources WHERE group_id=? AND type=? "
                        'AND (meta IS NULL OR meta NOT LIKE \'%"kind": "text_split"%\')',
                        (group_id, t),
                    )
            conn.commit()

        await self._conn.exec(_reconcile)
        await self.upsert_resources(rows)

    async def list_folders_detail(self, group_id: str) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT folder_id, folder_name, parent_id, sort_order FROM folders "
                "WHERE group_id=? ORDER BY sort_order, folder_name",
                (group_id,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._conn.exec(_do)

    async def clear_folders(self, group_id: str) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("DELETE FROM folders WHERE group_id=?", (group_id,))
            conn.commit()

        await self._conn.exec(_do)
