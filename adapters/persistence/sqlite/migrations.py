"""SQLite schema definition and versioned migrations.

Schema is versioned; migrations run incrementally on startup (idempotent).
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 17

MIGRATIONS: dict[int, list[str]] = {
    # Initial five tables (resources/snapshots/sync_logs/groups/schema_version)
    1: [
        """
        CREATE TABLE IF NOT EXISTS resources (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id   TEXT UNIQUE NOT NULL,
            group_id      TEXT NOT NULL,
            type          TEXT NOT NULL,
            name          TEXT NOT NULL,
            size          INTEGER NOT NULL DEFAULT 0,
            sha256        TEXT,
            mime          TEXT,
            uploader_id   TEXT,
            uploader_name TEXT,
            source_ref    TEXT NOT NULL,
            busid         INTEGER,
            folder_id     TEXT,
            folder_name   TEXT,
            status        TEXT NOT NULL DEFAULT 'active',
            tags          TEXT,
            meta          TEXT,
            created_at    INTEGER NOT NULL,
            indexed_at    INTEGER NOT NULL,
            updated_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_res_group_type_status
            ON resources (group_id, type, status);
        CREATE INDEX IF NOT EXISTS idx_res_folder ON resources (group_id, folder_id);
        CREATE INDEX IF NOT EXISTS idx_res_uploader ON resources (group_id, uploader_id);
        CREATE INDEX IF NOT EXISTS idx_res_name ON resources (group_id, name);
        """,
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    TEXT NOT NULL,
            type        TEXT NOT NULL,
            file_count  INTEGER NOT NULL,
            total_size  INTEGER NOT NULL,
            used_space  INTEGER NOT NULL,
            total_space INTEGER NOT NULL,
            detail      TEXT,
            taken_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snap_group_type ON snapshots (group_id, type, taken_at);
        """,
        """
        CREATE TABLE IF NOT EXISTS sync_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     TEXT NOT NULL,
            kind         TEXT NOT NULL,
            status       TEXT NOT NULL,
            files_found  INTEGER NOT NULL DEFAULT 0,
            files_indexed INTEGER NOT NULL DEFAULT 0,
            complete     INTEGER NOT NULL DEFAULT 0,
            error        TEXT,
            start_at     INTEGER NOT NULL,
            end_at       INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sync_group ON sync_logs (group_id, id);
        """,
        """
        CREATE TABLE IF NOT EXISTS groups (
            group_id     TEXT PRIMARY KEY,
            group_name   TEXT,
            join_time    INTEGER,
            last_sync_at INTEGER,
            sync_cursor  TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
        """,
    ],
    # Group management extensions
    2: [
        "ALTER TABLE groups ADD COLUMN role TEXT NOT NULL DEFAULT 'unknown';",
        "ALTER TABLE groups ADD COLUMN display_name TEXT;",
        "ALTER TABLE groups ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE groups ADD COLUMN label TEXT;",
        "ALTER TABLE groups ADD COLUMN last_scan_at INTEGER;",
    ],
    # Storage capacity monitoring
    3: [
        "ALTER TABLE groups ADD COLUMN used_space INTEGER;",
        "ALTER TABLE groups ADD COLUMN total_space INTEGER;",
        "ALTER TABLE groups ADD COLUMN file_count INTEGER;",
    ],
    # Volume persistence (WinRAR volume mode)
    4: [
        """
        CREATE TABLE IF NOT EXISTS volumes (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_resource_id   TEXT NOT NULL,
            seq                  INTEGER NOT NULL,
            part_name            TEXT NOT NULL,
            source_ref           TEXT,
            busid                INTEGER,
            size                 INTEGER NOT NULL DEFAULT 0,
            sha256               TEXT,
            status               TEXT NOT NULL DEFAULT 'pending',
            upload_time          INTEGER,
            group_id             TEXT,
            UNIQUE(parent_resource_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_vol_parent ON volumes (parent_resource_id, seq);
        """,
    ],
    # Group managed flag
    5: [
        "ALTER TABLE groups ADD COLUMN managed INTEGER NOT NULL DEFAULT 1;",
    ],
    # Cross-group volumes: the volumes table already declares group_id;
    # this step only backfills legacy rows.
    6: [
        "UPDATE volumes SET group_id = (SELECT group_id FROM resources "
        "WHERE resources.resource_id = volumes.parent_resource_id);",
    ],
    # Folder persistence
    7: [
        """
        CREATE TABLE IF NOT EXISTS folders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    TEXT NOT NULL,
            folder_id   TEXT NOT NULL,
            folder_name TEXT NOT NULL DEFAULT '',
            parent_id   TEXT NOT NULL DEFAULT '',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(group_id, folder_id)
        );
        CREATE INDEX IF NOT EXISTS idx_folders_group ON folders (group_id, parent_id);
        """,
    ],
    # Album/essence counts
    8: [
        "ALTER TABLE groups ADD COLUMN album_count INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE groups ADD COLUMN essence_count INTEGER NOT NULL DEFAULT 0;",
    ],
    # Multi-account support
    9: [
        "ALTER TABLE groups ADD COLUMN account_id TEXT NOT NULL DEFAULT '';",
    ],
    # Data normalization (path/ext)
    10: [
        "ALTER TABLE resources ADD COLUMN path TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE resources ADD COLUMN ext TEXT NOT NULL DEFAULT '';",
        "CREATE INDEX IF NOT EXISTS idx_res_group_path ON resources (group_id, path);",
        "UPDATE resources SET path = CASE "
        "WHEN folder_name IS NOT NULL AND folder_name != '' "
        "THEN '/' || group_id || '/' || folder_name || '/' || name "
        "ELSE '/' || group_id || '/' || name END WHERE type = 'file';",
        "UPDATE resources SET path = '/' || group_id || '/__album__/' || name "
        "WHERE type = 'album';",
        "UPDATE resources SET path = '/' || group_id || '/__essence__/' || name "
        "WHERE type = 'essence';",
        "UPDATE resources SET ext = '' WHERE type = 'file';",
        "CREATE VIEW IF NOT EXISTS v_resources AS "
        "SELECT 'cloud://' || group_id || '/' || type || '/' || id AS uri, "
        "id, resource_id, group_id, type, name, ext, path, "
        "size, uploader_id, uploader_name, busid, source_ref, "
        "folder_id, folder_name, status, tags, meta, "
        "created_at, indexed_at, updated_at FROM resources;",
    ],
    # FTS5 trigram full-text search
    11: [
        "CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5("
        "name, summary, tags, groupname, tokenize='trigram');",
        "INSERT INTO resources_fts(rowid, name, summary, tags, groupname) "
        "SELECT r.id, r.name, json_extract(r.meta, '$.summary'), r.tags, "
        "COALESCE(NULLIF(g.display_name, ''), g.group_name, '') "
        "FROM resources r LEFT JOIN groups g ON g.group_id = r.group_id;",
        "CREATE TRIGGER IF NOT EXISTS resources_fts_ai AFTER INSERT ON resources BEGIN "
        "INSERT INTO resources_fts(rowid, name, summary, tags, groupname) VALUES ("
        "new.id, new.name, json_extract(new.meta, '$.summary'), new.tags, "
        "(SELECT COALESCE(NULLIF(g.display_name, ''), g.group_name, '') FROM groups g "
        "WHERE g.group_id = new.group_id)); END;",
        "CREATE TRIGGER IF NOT EXISTS resources_fts_ad AFTER DELETE ON resources BEGIN "
        "DELETE FROM resources_fts WHERE rowid = old.id; END;",
        "CREATE TRIGGER IF NOT EXISTS resources_fts_au AFTER UPDATE "
        "OF name, meta, tags, group_id ON resources BEGIN "
        "DELETE FROM resources_fts WHERE rowid = old.id; "
        "INSERT INTO resources_fts(rowid, name, summary, tags, groupname) VALUES ("
        "new.id, new.name, json_extract(new.meta, '$.summary'), new.tags, "
        "(SELECT COALESCE(NULLIF(g.display_name, ''), g.group_name, '') FROM groups g "
        "WHERE g.group_id = new.group_id)); END;",
    ],
    # Archive map
    12: [
        """CREATE TABLE IF NOT EXISTS archive_map (
            resource_id  INTEGER NOT NULL,
            group_id     TEXT    NOT NULL,
            task_id      TEXT,
            remote_path  TEXT    NOT NULL,
            direction    TEXT    NOT NULL,
            state        TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            PRIMARY KEY (resource_id, group_id, direction)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_archive_map_state ON archive_map(state)",
    ],
    # Archive map deduplication rebuild
    13: [
        """CREATE TABLE IF NOT EXISTS archive_map_new (
            resource_id  INTEGER NOT NULL,
            group_id     TEXT    NOT NULL,
            task_id      TEXT,
            remote_path  TEXT    NOT NULL,
            direction    TEXT    NOT NULL,
            state        TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            PRIMARY KEY (resource_id, group_id, direction)
        )""",
        """INSERT OR REPLACE INTO archive_map_new
           (resource_id, group_id, task_id, remote_path, direction, state, updated_at)
           SELECT resource_id, group_id, task_id, remote_path, direction, state, updated_at
           FROM archive_map
           WHERE rowid IN (
               SELECT MAX(rowid) FROM archive_map
               GROUP BY resource_id, group_id, direction
           )""",
        "DROP TABLE IF EXISTS archive_map",
        "ALTER TABLE archive_map_new RENAME TO archive_map",
        "CREATE INDEX IF NOT EXISTS idx_archive_map_state ON archive_map(state)",
    ],
    # netdisk_meta
    14: [
        """CREATE TABLE IF NOT EXISTS netdisk_meta (
            remote_path   TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            is_dir        INTEGER NOT NULL DEFAULT 0,
            size          INTEGER NOT NULL DEFAULT 0,
            type          TEXT NOT NULL DEFAULT 'other',
            tags          TEXT NOT NULL DEFAULT '',
            registered_at TEXT NOT NULL,
            indexed_at    TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_netdisk_meta_dir ON netdisk_meta(remote_path)",
    ],
    # op_ledger + op_ops + groups.hidden
    15: [
        """CREATE TABLE IF NOT EXISTS op_ledger (
            task_id    TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,
            target     TEXT NOT NULL DEFAULT '',
            payload    TEXT NOT NULL DEFAULT '{}',
            state      TEXT NOT NULL,
            retries    INTEGER NOT NULL DEFAULT 0,
            error      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );""",
        "CREATE INDEX IF NOT EXISTS idx_ledger_state ON op_ledger(state);",
        "CREATE INDEX IF NOT EXISTS idx_ledger_updated ON op_ledger(updated_at);",
        """CREATE TABLE IF NOT EXISTS op_ops (
            op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL DEFAULT '',
            seq        INTEGER NOT NULL DEFAULT 0,
            action     TEXT NOT NULL,
            before     TEXT NOT NULL DEFAULT '{}',
            after      TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );""",
        "CREATE INDEX IF NOT EXISTS idx_ops_task ON op_ops(task_id, seq);",
        "ALTER TABLE groups ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;",
    ],
    # limit_count
    16: [
        "ALTER TABLE groups ADD COLUMN limit_count INTEGER;",
    ],
    # Extended FTS projection
    17: [
        "DROP TRIGGER IF EXISTS resources_fts_ai;",
        "DROP TRIGGER IF EXISTS resources_fts_ad;",
        "DROP TRIGGER IF EXISTS resources_fts_au;",
        "DROP TABLE IF EXISTS resources_fts;",
        "CREATE VIRTUAL TABLE resources_fts USING fts5("
        "name, summary, tags, groupname, path, folder, mime, uploader, hash, groupid, "
        "tokenize='trigram');",
        "INSERT INTO resources_fts(rowid, name, summary, tags, groupname, path, folder, mime, uploader, hash, groupid) "
        "SELECT r.id, COALESCE(r.name, ''), COALESCE(json_extract(r.meta, '$.summary'), ''), "
        "COALESCE(r.tags, ''), COALESCE(NULLIF(g.display_name, ''), g.group_name, ''), "
        "COALESCE(r.path, ''), COALESCE(r.folder_name, ''), COALESCE(r.mime, ''), "
        "COALESCE(r.uploader_name, '') || ' ' || COALESCE(r.uploader_id, ''), "
        "COALESCE(r.sha256, '') || ' ' || COALESCE(r.source_ref, ''), COALESCE(r.group_id, '') "
        "FROM resources r LEFT JOIN groups g ON g.group_id = r.group_id;",
        "CREATE TRIGGER resources_fts_ai AFTER INSERT ON resources BEGIN "
        "INSERT INTO resources_fts(rowid, name, summary, tags, groupname, path, folder, mime, uploader, hash, groupid) VALUES ("
        "new.id, COALESCE(new.name, ''), COALESCE(json_extract(new.meta, '$.summary'), ''), COALESCE(new.tags, ''), "
        "COALESCE((SELECT COALESCE(NULLIF(g.display_name, ''), g.group_name, '') FROM groups g WHERE g.group_id = new.group_id), ''), "
        "COALESCE(new.path, ''), COALESCE(new.folder_name, ''), COALESCE(new.mime, ''), COALESCE(new.uploader_name, '') || ' ' || COALESCE(new.uploader_id, ''), "
        "COALESCE(new.sha256, '') || ' ' || COALESCE(new.source_ref, ''), COALESCE(new.group_id, '')); END;",
        "CREATE TRIGGER resources_fts_ad AFTER DELETE ON resources BEGIN DELETE FROM resources_fts WHERE rowid = old.id; END;",
        "CREATE TRIGGER resources_fts_au AFTER UPDATE OF name, path, folder_name, mime, uploader_name, uploader_id, sha256, source_ref, meta, tags, group_id ON resources BEGIN "
        "DELETE FROM resources_fts WHERE rowid = old.id; INSERT INTO resources_fts(rowid, name, summary, tags, groupname, path, folder, mime, uploader, hash, groupid) VALUES ("
        "new.id, COALESCE(new.name, ''), COALESCE(json_extract(new.meta, '$.summary'), ''), COALESCE(new.tags, ''), "
        "COALESCE((SELECT COALESCE(NULLIF(g.display_name, ''), g.group_name, '') FROM groups g WHERE g.group_id = new.group_id), ''), COALESCE(new.path, ''), "
        "COALESCE(new.folder_name, ''), COALESCE(new.mime, ''), COALESCE(new.uploader_name, '') || ' ' || COALESCE(new.uploader_id, ''), "
        "COALESCE(new.sha256, '') || ' ' || COALESCE(new.source_ref, ''), COALESCE(new.group_id, '')); END;",
        "CREATE TABLE IF NOT EXISTS scan_schedule (group_id TEXT PRIMARY KEY, next_scan_at INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0);",
        "CREATE TABLE IF NOT EXISTS scan_claims (claim_key TEXT PRIMARY KEY, kind TEXT NOT NULL, group_id TEXT NOT NULL, worker_id TEXT NOT NULL, claimed_at INTEGER NOT NULL, lease_until INTEGER NOT NULL);",
        "CREATE TABLE IF NOT EXISTS fts_dirty_queue (resource_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT, lease_until INTEGER);",
        "CREATE TABLE IF NOT EXISTS fts_state (id INTEGER PRIMARY KEY CHECK (id=1), mode TEXT NOT NULL DEFAULT 'sync', updated_at INTEGER NOT NULL DEFAULT 0);",
        "INSERT OR IGNORE INTO fts_state(id, mode, updated_at) VALUES (1, 'sync', 0);",
        "CREATE TABLE IF NOT EXISTS outbox_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', available_at INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until INTEGER, created_at INTEGER NOT NULL DEFAULT 0);",
    ],
    18: ["CREATE TABLE IF NOT EXISTS scan_schedule (group_id TEXT PRIMARY KEY, next_scan_at INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0);"],
    19: ["CREATE TABLE IF NOT EXISTS scan_claims (claim_key TEXT PRIMARY KEY, kind TEXT NOT NULL, group_id TEXT NOT NULL, worker_id TEXT NOT NULL, claimed_at INTEGER NOT NULL, lease_until INTEGER NOT NULL);"],
    20: ["CREATE TABLE IF NOT EXISTS fts_dirty_queue (resource_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT, lease_until INTEGER);"],
    21: ["CREATE TABLE IF NOT EXISTS fts_state (id INTEGER PRIMARY KEY CHECK (id=1), mode TEXT NOT NULL DEFAULT 'sync', updated_at INTEGER NOT NULL DEFAULT 0); INSERT OR IGNORE INTO fts_state(id, mode, updated_at) VALUES (1, 'sync', 0);"],
    22: ["CREATE INDEX IF NOT EXISTS idx_scan_due ON scan_schedule(next_scan_at, priority);"],
    23: ["CREATE INDEX IF NOT EXISTS idx_scan_claim_lease ON scan_claims(lease_until);"],
    24: ["CREATE INDEX IF NOT EXISTS idx_fts_dirty_state ON fts_dirty_queue(state, lease_until);"],
    25: ["CREATE TABLE IF NOT EXISTS outbox_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', available_at INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until INTEGER, created_at INTEGER NOT NULL DEFAULT 0);", "CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox_events(state, available_at);"],
    # Optimize volume part-name backfills.
    26: ["CREATE INDEX IF NOT EXISTS idx_vol_part_name ON volumes(part_name);"],
}


def migrate(conn: sqlite3.Connection) -> int:
    """Run incremental migrations, return current schema version."""
    has_sv = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    cur = 0
    if has_sv:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        cur = row[0] if row else 0

    for v in sorted(MIGRATIONS):
        if v > cur and v <= SCHEMA_VERSION:
            for sql in MIGRATIONS[v]:
                conn.executescript(sql)

    # Post-migration backfill of the ext column.
    if cur < 10:
        rows = conn.execute(
            "SELECT id, name FROM resources WHERE type='file'"
        ).fetchall()
        conn.executemany(
            "UPDATE resources SET ext=? WHERE id=?",
            [
                (
                    name[name.rfind(".") + 1 :].lower() if "." in name else "",
                    rid,
                )
                for rid, name in rows
            ],
        )

    # Repair databases whose migration marker advanced while the extended FTS
    # projection was only partially created (seen in upgraded installations).
    fts_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(resources_fts)")
    }
    required_fts_columns = {
        "name", "summary", "tags", "groupname", "path", "folder",
        "mime", "uploader", "hash", "groupid",
    }
    if fts_columns and not required_fts_columns.issubset(fts_columns):
        conn.execute("DROP TRIGGER IF EXISTS resources_fts_ai")
        conn.execute("DROP TRIGGER IF EXISTS resources_fts_ad")
        conn.execute("DROP TRIGGER IF EXISTS resources_fts_au")
        conn.execute("DROP TABLE IF EXISTS resources_fts")
        for sql in MIGRATIONS[17]:
            conn.executescript(sql)

    # Unique constraint for schema_version
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_schema_version "
        "ON schema_version(version)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    return SCHEMA_VERSION
