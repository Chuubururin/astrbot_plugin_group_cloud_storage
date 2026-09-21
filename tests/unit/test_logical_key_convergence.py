"""logical_key convergence — one live row per logical file.

Context (the bug these pin): ``resource_id`` is derived from ``source_ref``,
and a NapCat ``file_id`` is a session-scoped handle that changes on every bot
restart. The sync therefore used to insert a SECOND row for one logical file,
leaving the composition/volume view at 0/n on the live row. Identity is now
``(group_id, type, name)`` (``logical_key``) backed by a partial unique index.

Assertions talk about *live* rows (status != 'deleted'), not total count: a
tombstone for the same logical file is kept on purpose, so totals may grow
while the live invariant holds.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.persistence.sqlite import SqliteMetaStore  # noqa: E402
from adapters.persistence.sqlite.migrations import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
    migrate,
)
from core.domain.enums import ResourceStatus, ResourceType  # noqa: E402
from core.domain.resource import Resource  # noqa: E402
from core.domain.sync import ResourceQuery, VolumeInfo  # noqa: E402

NAME = "movie.mp4"


@pytest.fixture
async def store(tmp_path):
    s = SqliteMetaStore(tmp_path / "meta.db")
    await s.init()
    yield s
    await s.close()


def _res(ref: str, name: str = NAME, group: str = "g1", size: int = 100,
         status: ResourceStatus = ResourceStatus.ACTIVE,
         meta: dict | None = None) -> Resource:
    return Resource(
        group_id=group, type=ResourceType.FILE, name=name, source_ref=ref,
        size=size, created_at=1700000000, status=status, meta=meta or {},
    )


def _q(group: str = "g1", keyword: str | None = None) -> ResourceQuery:
    return ResourceQuery(group_id=group, keyword=keyword, page_size=100)


async def _upsert(store, *refs: str, name: str = NAME, group: str = "g1"):
    return await store.upsert_resources(
        [_res(r, name=name, group=group) for r in refs]
    )


async def _live(store, group: str = "g1", name: str = NAME):
    """Active rows for one logical file (fold_parts off so parts do not hide it)."""
    return await store.query_resources(_q(group=group, keyword=name), fold_parts=False)


async def _raw(store, resource_id: str) -> dict | None:
    return await store.get_resource_by_resource_id(resource_id)


# ---------------------------------------------------------------- schema


def test_v29_is_registered():
    assert SCHEMA_VERSION >= 29
    assert 29 in MIGRATIONS
    joined = " ".join(MIGRATIONS[29])
    assert "logical_key" in joined
    assert "WHERE status != 'deleted'" in joined


def test_v29_migration_creates_the_partial_index(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    migrate(conn)
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_res_logical'"
    ).fetchone()
    assert idx is not None, "v29 did not create idx_res_logical"
    assert "WHERE status != 'deleted'" in idx[0]
    conn.close()


def test_v29_migrates_a_legacy_db_with_duplicate_live_rows(tmp_path):
    """A legacy DB already holding duplicates must migrate, not fail.

    CREATE UNIQUE INDEX over duplicate data raises, which would make
    store.init() fail and the plugin never start -- so v29 tombstones the
    older duplicates (newest by updated_at, ties by id) first.

    Read MIGRATIONS off the module rather than the imported name: another test
    calls importlib.reload(migrations), which rebinds the module attribute to a
    fresh dict while this file's imported reference still points at the old one
    -- mutating the stale dict then silently has no effect and the "legacy" DB
    comes out already migrated.
    """
    import adapters.persistence.sqlite.migrations as mig

    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    saved = mig.MIGRATIONS.pop(29, None)
    try:
        mig.migrate(conn)
    finally:
        if saved is not None:
            mig.MIGRATIONS[29] = saved
    for rid, ref, ts in [("g1:file:A", "A", 100), ("g1:file:B", "B", 200)]:
        conn.execute(
            "INSERT INTO resources (resource_id, group_id, type, name, size, "
            "source_ref, status, created_at, indexed_at, updated_at) "
            "VALUES (?,'g1','file',?,100,?,'active',1,1,?)",
            (rid, NAME, ref, ts),
        )
    conn.commit()
    mig.migrate(conn)

    live = conn.execute(
        "SELECT resource_id FROM resources WHERE status != 'deleted'"
    ).fetchall()
    assert len(live) == 1, f"expected one live row after dedupe, got {live}"
    assert live[0][0] == "g1:file:B", "newest updated_at must win"
    total = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    assert total == 2, "the superseded row should be tombstoned, not dropped"
    conn.close()


def test_partial_index_rejects_a_second_live_row(tmp_path):
    """The constraint itself, independent of the upsert path."""
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    migrate(conn)
    conn.execute(
        "INSERT INTO resources (resource_id, group_id, type, name, size, "
        "source_ref, status, created_at, indexed_at, updated_at, logical_key) "
        "VALUES ('g1:file:A','g1','file',?,100,'A','active',1,1,1,?)",
        (NAME, f"g1:file:{NAME}"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO resources (resource_id, group_id, type, name, size, "
            "source_ref, status, created_at, indexed_at, updated_at, logical_key) "
            "VALUES ('g1:file:B','g1','file',?,100,'B','active',1,1,1,?)",
            (NAME, f"g1:file:{NAME}"),
        )
    conn.close()


# ------------------------------------------------- cross-session identity


@pytest.mark.asyncio
async def test_cross_session_restart_collapses_to_one_live_row(store):
    """The core bug: a new file_id for a known logical file updates, not inserts."""
    for ref in ("A", "B", "C"):
        await _upsert(store, ref)
    page = await _live(store)
    assert page.total == 1, f"expected one live row, got {page.total}"
    assert page.items[0].source_ref == "C", "must adopt the newest source_ref"


@pytest.mark.asyncio
async def test_same_file_id_reupsert_is_stable(store):
    for _ in range(3):
        await _upsert(store, "A")
    assert (await _live(store)).total == 1


@pytest.mark.asyncio
async def test_resource_id_tracks_source_ref_across_sessions(store):
    """resource_id == group:type:source_ref must survive a cross-session update.

    Updating only source_ref left the row self-inconsistent and broke
    get_by_uri plus mark_missing's reverse lookup.
    """
    await _upsert(store, "A")
    await _upsert(store, "B")
    page = await store.query_resources(_q())
    for item in page.items:
        assert item.resource_id == f"{item.group_id}:{item.type}:{item.source_ref}"


@pytest.mark.asyncio
async def test_new_source_ref_is_resolvable_by_resource_id(store):
    """get_by_uri's lookup must find the row under its new source_ref."""
    await _upsert(store, "A")
    await _upsert(store, "B")
    assert await _raw(store, "g1:file:B") is not None
    assert await _raw(store, "g1:file:A") is None, "stale id must not resolve"


# ------------------------------------------------------ status convergence


@pytest.mark.asyncio
async def test_deleted_file_returns_under_a_new_file_id(store):
    await _upsert(store, "A")
    assert await store.mark_missing_as_deleted("g1", True, set()) == 1
    assert (await _live(store)).total == 0
    await _upsert(store, "B")
    page = await _live(store)
    assert page.total == 1 and page.items[0].source_ref == "B"


@pytest.mark.asyncio
async def test_archived_survives_a_passive_reindex(store):
    """``archived`` is a user decision, so a sync pass must not resurrect it.

    ``_live`` filters on status='active', which would hide an archived row for
    the wrong reason -- read the row's own status instead.
    """
    await _upsert(store, "A")
    row = await _raw(store, "g1:file:A")
    await store.update_resource_fields(row["id"], status="archived")
    assert (await _raw(store, "g1:file:A"))["status"] == "archived"

    await _upsert(store, "B")

    kept = await _raw(store, "g1:file:B")
    assert kept["status"] == "archived", "a user decision must survive re-index"
    assert kept["id"] == row["id"], "same row, so the decision had one home"


@pytest.mark.asyncio
async def test_distinct_names_in_one_group_stay_distinct(store):
    await _upsert(store, "A", name="a.mp4")
    await _upsert(store, "B", name="b.mp4")
    assert (await _live(store, name="a.mp4")).total == 1
    assert (await _live(store, name="b.mp4")).total == 1


@pytest.mark.asyncio
async def test_same_name_in_different_groups_stay_distinct(store):
    await _upsert(store, "A", group="g1")
    await _upsert(store, "B", group="g2")
    assert (await _live(store, group="g1")).total == 1
    assert (await _live(store, group="g2")).total == 1


# ---------------------------------------------------- in-place convergence
#
# After the refactor a restarted session no longer creates a second row to be
# reconciled -- ON CONFLICT(logical_key) rewrites the ONE live row in place
# (including resource_id, which derives from source_ref). So these tests assert
# on the single surviving row rather than on a predecessor/successor pair.


def _volume(parent: str, seq: int, part_name: str) -> VolumeInfo:
    return VolumeInfo(
        parent_resource_id=parent, seq=seq, part_name=part_name,
        source_ref=f"ref_{seq}", busid=102, size=10, sha256="x",
        status="uploaded", upload_time=1, group_id="g1",
    )


async def _composition_parent(store, resource_id: str, part: str) -> int:
    """Mark a row as a volume parent and register one part under it."""
    await store.insert_volumes([_volume(resource_id, 0, part)])
    row = await _raw(store, resource_id)
    await store.update_resource_fields(
        row["id"], meta=json.dumps({"composition": {"kind": "volumes"}})
    )
    return row["id"]


@pytest.mark.asyncio
async def test_volumes_follow_the_row_across_a_restart(store):
    """The parts must survive a file_id change -- the whole point of the fix.

    In-place collapse rewrites ``parent_resource_id``'s target, and there is no
    FK backstop, so without an explicit reparent the parts silently vanish from
    every listing (the part-fold JOIN stops matching).
    """
    await store.upsert_resources([_res("A")])
    await _composition_parent(store, "g1:file:A", "movie.part0")

    await _upsert(store, "B")

    parts = await store.list_volumes("g1:file:B")
    assert parts, "volumes must follow the collapsed row onto its new id"
    assert parts[0].part_name == "movie.part0"
    assert await store.list_volumes("g1:file:A") == [], "no orphans left behind"


@pytest.mark.asyncio
async def test_collapse_keeps_one_live_row_with_the_new_source_ref(store):
    """No duplicate, no stray tombstone: one row, carrying the new handle."""
    await store.upsert_resources([_res("A")])
    await _composition_parent(store, "g1:file:A", "movie.part0")
    first = await _raw(store, "g1:file:A")

    await _upsert(store, "B")

    page = await _live(store, name=NAME)
    assert page.total == 1, "in-place collapse must not duplicate the row"
    assert page.items[0].resource_id == "g1:file:B"
    # Same physical row: the id did not change, only what it points at.
    again = await _raw(store, "g1:file:B")
    assert again["id"] == first["id"], "the live row is updated, not replaced"
    assert await _raw(store, "g1:file:A") is None, "the old id must not resolve"


@pytest.mark.asyncio
async def test_composition_meta_survives_the_collapse(store):
    """json_patch keeps the composition identity when meta is an object."""
    await store.upsert_resources([_res("A")])
    await _composition_parent(store, "g1:file:A", "movie.part0")

    await _upsert(store, "B")

    row = await _raw(store, "g1:file:B")
    assert row["meta"]["composition"]["kind"] == "volumes"


@pytest.mark.asyncio
async def test_legacy_volumes_parent_keeps_its_identity(store):
    """The legacy {'volumes': True} shape must survive too, not just canonical.

    core/application/ingest/video.py writes ``{"volumes": true, "kind": "video"}``
    with NO composition key. The conflict body's meta CASE originally tested
    only ``$.composition``, so such a parent fell to ELSE and the incoming
    (usually empty) meta wiped its identity on the next sync.
    """
    await store.upsert_resources([
        _res("A", name="big.mp4",
             meta={"volumes": True, "kind": "video", "total_sha256": "beef"})
    ])

    await _upsert(store, "B", name="big.mp4")

    row = await _raw(store, "g1:file:B")
    assert row["meta"]["volumes"] is True
    assert row["meta"]["total_sha256"] == "beef"


@pytest.mark.asyncio
async def test_same_name_smaller_file_is_the_same_logical_file(store):
    """Online case 10879, re-pinned to ``logical_key`` semantics.

    A 200MB file was split into parts (composition parent), the cloud original
    was removed, and a much smaller file later appeared under the SAME name in
    the SAME group. The retired size gate read that as "a different file" and
    kept two rows.

    Identity is now (group_id, type, name) -- the stable key -- so a same-named
    file IS the same logical file, and inheriting the composition (with its
    parts reparented) is the CORRECT outcome. Size is a sync artifact, not
    identity: the same file legitimately changes size between listings.

    Kept as a test because the old expectation was the opposite, so a future
    reader needs to see the reversal was deliberate.
    """
    await store.upsert_resources([
        _res("old_ref", name="vol_test.bin", size=200_000_000,
             meta={"volumes": True, "compression": "zip-part",
                   "composition": {"kind": "volumes", "parts": 3}})
    ])
    await store.insert_volumes([_volume("g1:file:old_ref", 1, "vol_test.part01of03.zip")])

    # Same name, same group, far smaller, no meta of its own.
    await store.upsert_resources([_res("new_ref", name="vol_test.bin", size=2048)])

    page = await _live(store, name="vol_test.bin")
    assert page.total == 1, "one logical file, so one live row"

    row = await _raw(store, "g1:file:new_ref")
    assert row["meta"]["composition"]["kind"] == "volumes", "identity carries over"

    parts = await store.list_volumes("g1:file:new_ref")
    assert len(parts) == 1, "the parts follow the surviving row"
    assert parts[0].part_name == "vol_test.part01of03.zip"
    assert await store.list_volumes("g1:file:old_ref") == [], "no orphans"

# ------------------------------------------------------------- rename


@pytest.mark.asyncio
async def test_rename_refreshes_logical_key(store):
    """A rename must move the identity with the name.

    ``logical_key`` is what the partial unique index enforces, so leaving it
    on the OLD name makes the renamed row matchable by a name it no longer
    has. The next file to claim that name then overwrites it (see the test
    below), which lost the renamed file.
    """
    await _upsert(store, "ref_a", name="a.mp4")
    row = await _raw(store, "g1:file:ref_a")
    assert row["logical_key"] == "g1:file:a.mp4"

    await store.update_resource_fields(row["id"], name="b.mp4")

    after = await _raw(store, "g1:file:ref_a")
    assert after["name"] == "b.mp4"
    assert after["logical_key"] == "g1:file:b.mp4", "key follows the new name"
    assert after["path"] == "/g1/b.mp4", "path follows too"


@pytest.mark.asyncio
async def test_renamed_file_survives_its_old_name_returning(store):
    """The regression: a stale key let the old name overwrite the renamed row.

    Sequence: a.mp4 -> renamed to b.mp4 -> a new a.mp4 appears (fresh session
    handle). With the key left stale, the returning a.mp4 matched b.mp4's row
    and replaced it in place, so b.mp4 disappeared with no trace.
    """
    await _upsert(store, "ref_a", name="a.mp4")
    row = await _raw(store, "g1:file:ref_a")
    await store.update_resource_fields(row["id"], name="b.mp4")

    await _upsert(store, "ref_b", name="a.mp4")

    page = await store.query_resources(_q())
    assert sorted(i.name for i in page.items) == ["a.mp4", "b.mp4"], "both live"
    kept = await _raw(store, "g1:file:ref_a")
    assert kept is not None and kept["name"] == "b.mp4", "the renamed row survived"


@pytest.mark.asyncio
async def test_rename_onto_a_name_another_live_row_holds(store):
    """Renaming onto an occupied name must not raise.

    The index forbids two live rows on one key, so the key is deliberately
    left alone in that case -- the guard exists so the rename itself still
    succeeds instead of turning a user action into IntegrityError.
    """
    await _upsert(store, "ref_a", name="a.mp4")
    await _upsert(store, "ref_b", name="b.mp4")
    row = await _raw(store, "g1:file:ref_a")

    await store.update_resource_fields(row["id"], name="b.mp4")

    page = await store.query_resources(_q())
    assert sorted(i.name for i in page.items) == ["b.mp4", "b.mp4"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("folder", "name", "expect_path"),
    [
        (None, "renamed.mp4", "/g1/renamed.mp4"),
        ("dir", "renamed.mp4", "/g1/dir/renamed.mp4"),
    ],
    ids=["root", "foldered"],
)
async def test_rename_recomputes_path_for_every_shape(
    store, folder, name, expect_path
):
    """path is recomputed in Python on rename, so each shape must still match.

    The rule used to live in the SQL statement; it now shares one helper with
    the upsert, so a foldered row is the case most likely to drift.
    """
    r = _res("ref_f", name="f.mp4")
    r.folder_name = folder
    await store.upsert_resources([r])
    before = await _raw(store, "g1:file:ref_f")
    assert before["path"] == (f"/g1/{folder}/f.mp4" if folder else "/g1/f.mp4")

    await store.update_resource_fields(before["id"], name=name)

    after = await _raw(store, "g1:file:ref_f")
    assert after["path"] == expect_path
    assert after["logical_key"] == f"g1:file:{name}", "folder is not identity"


@pytest.mark.asyncio
async def test_rename_recomputes_path_for_album_rows(store):
    """Non-file types use the __album__/__essence__ placeholder folder."""
    album = Resource(
        group_id="g1", type=ResourceType.ALBUM, name="shot", source_ref="alb_1",
        size=1, created_at=1700000000, status=ResourceStatus.ACTIVE, meta={},
    )
    await store.upsert_resources([album])
    before = await _raw(store, "g1:album:alb_1")
    assert before["path"] == "/g1/__album__/shot"

    await store.update_resource_fields(before["id"], name="renamed")

    after = await _raw(store, "g1:album:alb_1")
    assert after["path"] == "/g1/__album__/renamed"
    assert after["logical_key"] == "g1:album:renamed"
