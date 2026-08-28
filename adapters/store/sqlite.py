"""SqliteMetaStore —— MetaStorePort 的 SQLite 实现（Slice 0，docs/03）。

- 独立库：data/plugin_data/{name}/meta.db，WAL 模式
- 迁移：schema_version + 版本化 SQL 数组（幂等）
- 线程模型：单连接 + threading.Lock，I/O 经 asyncio.to_thread 不阻塞事件循环
- 一致性：唯一键 UPSERT / 孤儿清理门控 complete / 快照只追加（docs/03 §8）
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from core.log import logger

from core.domain.enums import ResourceStatus, SyncStatus
from core.domain.resource import Resource
from core.domain.sync import (
    GroupInfo,
    Page,
    VolumeInfo,
    PageItem,
    ResourceQuery,
    ResourceStats,
    Snapshot,
    SyncLog,
    SyncResult,
)
from ports.meta_store import MetaStorePort

_SCHEMA_VERSION = 16

# 版本化迁移：{版本号: [SQL 列表]}。启动时仅执行 当前版本 < 目标 的迁移（增量、幂等）。
_MIGRATIONS: dict[int, list[str]] = {
    # v1：V1.0 五张表（resources/snapshots/sync_logs/groups/schema_version）
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
    # v2：群集中管理（docs/09 §12.2）—— groups 表扩展
    2: [
        "ALTER TABLE groups ADD COLUMN role TEXT NOT NULL DEFAULT 'unknown';",
        "ALTER TABLE groups ADD COLUMN display_name TEXT;",
        "ALTER TABLE groups ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE groups ADD COLUMN label TEXT;",
        "ALTER TABLE groups ADD COLUMN last_scan_at INTEGER;",
    ],
    # v3：跨群存储容量监控（docs/09 §13.3）—— 每群 10GB 上限约束下统一统计
    3: [
        "ALTER TABLE groups ADD COLUMN used_space INTEGER;",
        "ALTER TABLE groups ADD COLUMN total_space INTEGER;",
        "ALTER TABLE groups ADD COLUMN file_count INTEGER;",
    ],
    # v4：分卷永久化（docs/09 §14.1 WinRAR 分卷模式）—— volumes 卷映射表
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
            UNIQUE(parent_resource_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_vol_parent ON volumes (parent_resource_id, seq);
        """,
    ],
    # v5：群管理条目可移除（managed=0 从列表隐藏；扫描不复活）
    5: [
        "ALTER TABLE groups ADD COLUMN managed INTEGER NOT NULL DEFAULT 1;",
    ],
    # v6：分卷可跨群存储（每卷记录所在群；文件大小不限制）
    6: [
        "ALTER TABLE volumes ADD COLUMN group_id TEXT;",
        "UPDATE volumes SET group_id = (SELECT group_id FROM resources "
        "WHERE resources.resource_id = volumes.parent_resource_id);",
    ],
    # v7：目录持久化管理（文件夹实体独立入库，文件列表按目录树驱动）
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
    # v8：群相册/精华消息统计（原始目标三块资源之二，docs/00 G 资源统计）
    8: [
        "ALTER TABLE groups ADD COLUMN album_count INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE groups ADD COLUMN essence_count INTEGER NOT NULL DEFAULT 0;",
    ],
    # v9：多 OneBot 账号（每个反向 WS 实例一个账号；群归属账号）
    9: [
        "ALTER TABLE groups ADD COLUMN account_id TEXT NOT NULL DEFAULT '';",
    ],
    # v10：数据规范化（可编码化/文件系统化/可读化，docs/13）
    10: [
        "ALTER TABLE resources ADD COLUMN path TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE resources ADD COLUMN ext TEXT NOT NULL DEFAULT '';",
        "CREATE INDEX IF NOT EXISTS idx_res_group_path ON resources (group_id, path);",
        # 存量回填：逻辑路径 /<群号>/<目录>/<文件名>（文件系统化寻址）
        "UPDATE resources SET path = CASE "
        "WHEN folder_name IS NOT NULL AND folder_name != '' "
        "THEN '/' || group_id || '/' || folder_name || '/' || name "
        "ELSE '/' || group_id || '/' || name END WHERE type = 'file';",
        "UPDATE resources SET path = '/' || group_id || '/__album__/' || name "
        "WHERE type = 'album';",
        "UPDATE resources SET path = '/' || group_id || '/__essence__/' || name "
        "WHERE type = 'essence';",
        "UPDATE resources SET ext = '' WHERE type = 'file';",
        # 可读视图：程序化查询面（友好口径 + uri）
        "CREATE VIEW IF NOT EXISTS v_resources AS "
        "SELECT 'cloud://' || group_id || '/' || type || '/' || id AS uri, "
        "id, resource_id, group_id, type, name, ext, path, "
        "size, uploader_id, uploader_name, busid, source_ref, "
        "folder_id, folder_name, status, tags, meta, "
        "created_at, indexed_at, updated_at FROM resources;",
    ],
    11: [
        # v11：磁盘化全文检索（FTS5 trigram，百万文件/万群规模；docs/13 §7 组合存储后）
        "CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5("
        "name, summary, tags, groupname, tokenize='trigram');",
        # 存量回填（先于触发器创建，避免重复索引）
        "INSERT INTO resources_fts(rowid, name, summary, tags, groupname) "
        "SELECT r.id, r.name, json_extract(r.meta, '$.summary'), r.tags, "
        "COALESCE(NULLIF(g.display_name, ''), g.group_name, '') "
        "FROM resources r LEFT JOIN groups g ON g.group_id = r.group_id;",
        # 触发器：增删改自动同步（groupname 快照；群改名后下次该行更新时刷新）
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
    # v12: archive_map for bridge operations (REQ-03)
    # v13: PRIMARY KEY 改为 (resource_id, group_id, direction) —— remote_path 不再是主键
    #      因为完成后重命名会改变 remote_path，旧主键导致新行而非更新
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
    # v13: 从旧主键 (resource_id, group_id, remote_path, direction) 迁移到新主键
    #      对已存在的旧表：重建表结构，去重保留最新行
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
    # v14: netdisk_meta 网盘索引与标记（ADR-0004，N4；remote_path 稳定主键，HL-07 无 URL）
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
    # v15: 任务台账与控制（ADR-0005 经纠偏 D-6 直接指令实施）+ 群组凋零隐藏
    #      op_ledger（任务台账：pending/running/paused/retry/done/failed/cancelled + 断点续传对账）
    #      op_ops（操作流：可逆操作 before/after 快照，供撤销补偿）
    #      groups.hidden（账号离线群组隐藏而非删除，恢复在线后显示）
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
    # v16：群容量 limit_count 落库（2026-09-03 口径核对：FileSystemInfo.limit_count 持久化）
    16: [
        "ALTER TABLE groups ADD COLUMN limit_count INTEGER;",
    ],
}


class SqliteMetaStore(MetaStorePort):
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tag_cloud_cache: dict[str, tuple] = {}

    # ---------- 基础设施 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    async def _exec(self, fn, *args):
        """把阻塞调用丢到线程池执行，并对事件循环做必要绑定。"""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return await asyncio.to_thread(fn, *args)

    def _run(self, fn, *args):
        with self._lock:
            # 每次操作独立短连接：WAL 并发安全，杜绝跨线程残留事务污染
            conn = self._connect()
            try:
                return fn(conn, *args)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    # ---------- 迁移 ----------

    async def init(self) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                # 当前版本：schema_version 表不存在则视为 0（全新库）
                has_sv = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone()
                cur = 0
                if has_sv:
                    row = conn.execute(
                        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
                    ).fetchone()
                    cur = row[0] if row else 0
                # 仅执行增量迁移（ALTER 等非幂等语句安全：每个版本只跑一次）
                for v in sorted(_MIGRATIONS):
                    if v > cur:
                        for sql in _MIGRATIONS[v]:
                            conn.executescript(sql)
                # v10 后置：ext 精确回填（SQLite 无 REVERSE；Python rfind 语义与运行时一致）
                if cur < 10:
                    rows = conn.execute(
                        "SELECT id, name FROM resources WHERE type='file'"
                    ).fetchall()
                    conn.executemany(
                        "UPDATE resources SET ext=? WHERE id=?",
                        [
                            (
                                name[name.rfind(".") + 1 :].lower()
                                if "." in name
                                else "",
                                rid,
                            )
                            for rid, name in rows
                        ],
                    )
                # schema_version 唯一约束（对旧库补索引，保证 INSERT OR REPLACE 语义；
                # 置于迁移之后：全新库此时表已由 v1 迁移创建）
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_schema_version "
                    "ON schema_version(version)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._exec(self._run, _do)
        logger.info(
            f"[group_cloud_storage] meta.db ready (schema v{_SCHEMA_VERSION}) at {self._db_path}"
        )

    async def close(self) -> None:
        def _do(conn: sqlite3.Connection):
            conn.close()

        if self._conn is not None:
            await self._exec(self._run, _do)
            self._conn = None

    # ---------- 资源 ----------

    async def upsert_resources(self, items: list[Resource]) -> int:
        if not items:
            return 0
        now = int(time.time())

        def _do(conn: sqlite3.Connection):
            rows = []
            for r in items:
                path, ext = self._logical_path(r)
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
                    )
                )
            n = 0
            for i in range(0, len(rows), 500):  # 分块事务（v2.13: 100→500 提升写入吞吐）
                chunk = rows[i : i + 500]
                conn.execute("BEGIN")
                try:
                    cur = conn.executemany(
                        """
                        INSERT INTO resources
                          (resource_id, group_id, type, name, size, uploader_id,
                           uploader_name, source_ref, busid, folder_id, folder_name,
                           status, tags, meta, created_at, indexed_at, updated_at,
                           path, ext)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(resource_id) DO UPDATE SET
                          name=excluded.name, size=excluded.size,
                          uploader_id=COALESCE(excluded.uploader_id, uploader_id),
                          uploader_name=COALESCE(excluded.uploader_name, uploader_name),
                          busid=COALESCE(excluded.busid, busid),
                          folder_id=excluded.folder_id, folder_name=excluded.folder_name,
                          created_at=CASE WHEN excluded.created_at > 0
                              THEN excluded.created_at ELSE created_at END,
                          meta=excluded.meta, updated_at=excluded.updated_at,
                          path=excluded.path, ext=excluded.ext
                        """,
                        chunk,
                    )
                    n += cur.rowcount
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return n

        result = await self._exec(self._run, _do)
        self._tag_cloud_cache = {}  # 同步 upsert → 标签云失效
        return result

    @staticmethod
    def _logical_path(r) -> tuple[str, str]:
        """逻辑路径（文件系统化寻址）与扩展名（docs/13）。"""
        t = r.type.value if hasattr(r.type, "value") else str(r.type)
        if t == "file":
            if r.folder_name:
                path = f"/{r.group_id}/{r.folder_name}/{r.name}"
            else:
                path = f"/{r.group_id}/{r.name}"
            dot = r.name.rfind(".")
            ext = r.name[dot + 1 :].lower() if dot > 0 else ""
        elif t == "album":
            path = f"/{r.group_id}/__album__/{r.name}"
            ext = "album"
        elif t == "essence":
            path = f"/{r.group_id}/__essence__/{r.name}"
            ext = "essence"
        else:
            path = f"/{r.group_id}/{r.name}"
            ext = ""
        return path, ext

    def resource_uri(self, group_id: str, rtype: str, id: int) -> str:
        """可编码化资源 URI：cloud://<group>/<type>/<id>（docs/13）。"""
        return f"cloud://{group_id}/{rtype}/{id}"

    async def get_by_uri(self, uri: str) -> dict | None:
        """按 cloud:// URI 定位资源（程序化引用）。"""
        prefix = "cloud://"
        if not uri.startswith(prefix):
            raise ValueError("invalid resource uri")
        parts = uri[len(prefix) :].split("/")
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValueError("invalid resource uri")
        return await self.get_resource_any(int(parts[2]))

    async def query_resources(self, q: ResourceQuery) -> Page:
        def _do(conn: sqlite3.Connection):
            where = ["status = ?"]
            params: list = [q.status]
            if q.type:
                where.append("type = ?")
                params.append(q.type)
            if q.groups:
                marks = ",".join("?" for _ in q.groups)
                where.append(f"group_id IN ({marks})")
                params.extend(q.groups)
            elif q.group_id:
                where.append("group_id = ?")
                params.append(q.group_id)
            if q.keyword:
                where.append("name LIKE ?")
                params.append(f"%{q.keyword}%")
            if q.uploader_id:
                where.append("uploader_id = ?")
                params.append(q.uploader_id)
            if q.folder_id is not None:
                where.append("folder_id = ?")
                params.append(q.folder_id)
            if q.ids is not None:
                if q.ids:
                    marks = ",".join("?" for _ in q.ids)
                    where.append(f"id IN ({marks})")
                    params.extend(q.ids)
                else:
                    where.append("1=0")
            if q.folder == "__root__":
                where.append("(folder_name IS NULL OR folder_name = '')")
            elif q.folder:
                where.append("folder_name = ?")
                params.append(q.folder)
            # 2026-09-01 N-02：派生状态筛选（在网盘/在相册/在精华/未下载）
            if q.store_status:
                if q.store_status == "netdisk":
                    where.append(
                        "EXISTS (SELECT 1 FROM archive_map am "
                        "WHERE am.resource_id = resources.id "
                        "AND am.direction = 'out' AND am.state = 'done')"
                    )
                elif q.store_status == "album":
                    where.append("type = 'album'")
                elif q.store_status == "essence":
                    where.append("type = 'essence'")
                elif q.store_status == "none":
                    where.append(
                        "type NOT IN ('album', 'essence') AND NOT EXISTS ("
                        "SELECT 1 FROM archive_map am WHERE am.resource_id = resources.id "
                        "AND am.direction = 'out' AND am.state = 'done')"
                    )
            if q.exts:
                # 扩展名筛选：name 大小写不敏感后缀匹配（Everything 类型分组）
                ext_where = " OR ".join(["LOWER(name) LIKE ?" for _ in q.exts])
                where.append(f"({ext_where})")
                params.extend([f"%{e.lower()}" for e in q.exts])
            if q.tags:
                for tag in q.tags:
                    where.append("tags LIKE ?")
                    params.append(f'%"{tag}"%')
            cond = " AND ".join(where)
            total = conn.execute(
                f"SELECT COUNT(*) FROM resources WHERE {cond}", params
            ).fetchone()[0]
            offset = (q.page - 1) * q.page_size
            # 排序白名单（Everything 风格列头排序，防注入）
            sort_map = {
                "id": "id",
                "name": "name",
                "size": "size",
                "created_at": "created_at",
                "uploader_name": "uploader_name",
            }
            order_by = sort_map.get(q.sort_by, "id")
            order_dir = "DESC" if q.sort_dir == "desc" else "ASC"
            rows = conn.execute(
                f"""
                SELECT id, resource_id, group_id, name, size, uploader_id,
                       uploader_name, folder_name, created_at, indexed_at,
                       busid, source_ref, meta, type, tags, path, ext
                FROM resources WHERE {cond}
                ORDER BY {order_by} {order_dir}, id LIMIT ? OFFSET ?
                """,
                [*params, q.page_size, offset],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                meta = d.get("meta")
                d["meta"] = json.loads(meta) if meta else None
                tags = d.get("tags")
                d["tags"] = json.loads(tags) if tags else []
                items.append(PageItem(**d))
            return Page(items=items, total=total, page=q.page, page_size=q.page_size)

        return await self._exec(self._run, _do)

    async def get_resource_detail(self, group_id: str, id: int) -> dict | None:
        """获取单个资源详情（包含分卷元数据）。"""
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND group_id = ? AND type='file'",
                (id, group_id),
            ).fetchone()
            if not row:
                return None
            return self._row_with_meta(row)

        return await self._exec(self._run, _do)

    async def stats(self, group_id: str) -> ResourceStats:
        def _do(conn: sqlite3.Connection):
            cond = "group_id=? AND type='file' AND status='active'"
            row = conn.execute(
                f"SELECT COUNT(*) c, COALESCE(SUM(size),0) s, COUNT(DISTINCT uploader_id) u "
                f"FROM resources WHERE {cond}",
                (group_id,),
            ).fetchone()
            st = ResourceStats(
                group_id=group_id,
                file_count=row["c"],
                total_size=row["s"],
                uploaders=row["u"],
                by_folder=[
                    dict(r)
                    for r in conn.execute(
                        f"SELECT folder_name, COUNT(*) cnt, SUM(size) bytes FROM resources "
                        f"WHERE {cond} GROUP BY folder_id ORDER BY bytes DESC LIMIT 10",
                        (group_id,),
                    )
                ],
                by_uploader=[
                    dict(r)
                    for r in conn.execute(
                        f"SELECT uploader_id, uploader_name, COUNT(*) cnt, SUM(size) bytes "
                        f"FROM resources WHERE {cond} GROUP BY uploader_id "
                        f"ORDER BY bytes DESC LIMIT 10",
                        (group_id,),
                    )
                ],
                recent_7d=[
                    dict(r)
                    for r in conn.execute(
                        f"SELECT date(created_at,'unixepoch') d, COUNT(*) cnt FROM resources "
                        f"WHERE {cond} AND created_at >= ? GROUP BY d ORDER BY d",
                        (group_id, int(time.time()) - 7 * 86400),
                    )
                ],
            )
            return st

        return await self._exec(self._run, _do)

    async def list_groups(self, include_hidden: bool = False) -> list[GroupInfo]:
        """群清单（v15：默认排除 hidden=1 的群——账号离线群组隐藏凋零，恢复后重新显示）。"""

        def _do(conn: sqlite3.Connection):
            sql = (
                """SELECT group_id, group_name, join_time, last_sync_at,
                          role, display_name, sort_order, label, last_scan_at,
                          used_space, total_space, file_count, limit_count, managed,
                          album_count, essence_count, account_id, hidden
                   FROM groups"""
            )
            if not include_hidden:
                sql += " WHERE hidden = 0"
            sql += " ORDER BY sort_order, group_id"
            rows = conn.execute(sql).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["sort_order"] = d.get("sort_order") or 0
                out.append(GroupInfo(**d))
            return out

        return await self._exec(self._run, _do)

    async def upsert_groups(self, items: list[GroupInfo]) -> int:
        items = [g for g in items if g.group_id]  # 防御：拒绝空 group_id 落库

        def _do(conn: sqlite3.Connection):
            if not items:
                return 0
            now = int(time.time())
            conn.execute("BEGIN")
            try:
                n = 0
                for g in items:
                    cur = conn.execute(
                        """INSERT INTO groups
                             (group_id, group_name, role, display_name, sort_order,
                              label, join_time, last_scan_at,
                              used_space, total_space, file_count, limit_count,
                              album_count, essence_count, account_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(group_id) DO UPDATE SET
                             group_name=excluded.group_name,
                             role=excluded.role,
                             display_name=COALESCE(excluded.display_name, display_name),
                             last_scan_at=excluded.last_scan_at,
                             used_space=excluded.used_space,
                             total_space=excluded.total_space,
                             file_count=excluded.file_count,
                             limit_count=excluded.limit_count,
                             album_count=excluded.album_count,
                             essence_count=excluded.essence_count,
                             account_id=COALESCE(NULLIF(excluded.account_id, ''), groups.account_id),
                             managed=groups.managed
                        """,
                        (
                            g.group_id,
                            g.group_name,
                            g.role,
                            g.display_name,
                            g.sort_order,
                            g.label,
                            g.join_time or now,
                            g.last_scan_at or now,
                            g.used_space,
                            g.total_space,
                            g.file_count,
                            g.limit_count,
                            g.album_count,
                            g.essence_count,
                            g.account_id,
                        ),
                    )
                    n += cur.rowcount
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

        return await self._exec(self._run, _do)

    # groups 可更新列白名单（防注入，DoD 输入校验）
    _GROUP_FIELD_WHITELIST = frozenset(
        {
            "display_name",
            "label",
            "sort_order",
            "last_scan_at",
            "group_name",
            "role",
            "used_space",
            "total_space",
            "file_count",
            "limit_count",
        }
    )

    async def update_group_fields(self, group_id: str, **fields) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - self._GROUP_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid group fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE groups SET {sets} WHERE group_id=?",
                [*fields.values(), group_id],
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def sum_resource_sizes(self, group_id: str) -> int:
        """已用容量（索引精确统计）：群内 active 文件大小合计。"""

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COALESCE(SUM(size), 0) FROM resources "
                "WHERE group_id=? AND status='active'",
                (group_id,),
            ).fetchone()
            return int(row[0] or 0)

        return await self._exec(self._run, _do)

    async def set_groups_managed(self, group_ids: list[str], managed: int) -> None:
        """批量设置管理标记（0=从列表移除/停止管理，1=恢复）。"""

        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                for gid in group_ids:
                    conn.execute(
                        "UPDATE groups SET managed=? WHERE group_id=?", (managed, gid)
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._exec(self._run, _do)

    async def reorder_groups(self, ordered_ids: list[str]) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                for i, gid in enumerate(ordered_ids):
                    conn.execute(
                        "UPDATE groups SET sort_order=? WHERE group_id=?",
                        (i + 1, gid),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._exec(self._run, _do)

    _RESOURCE_FIELD_WHITELIST = frozenset(
        {"name", "folder_id", "folder_name", "status", "size", "mime", "meta", "tags"}
    )

    async def update_resource_tags(self, id: int, tags: list[str]) -> None:
        """标签覆盖写入（v1.3 信息整理；列表转 JSON 落 tags 列）。"""
        cleaned = sorted({str(t).strip() for t in tags if str(t).strip()})
        await self.update_resource_fields(
            id, tags=json.dumps(cleaned, ensure_ascii=False)
        )

    async def list_accounts(self) -> list[dict]:
        """账号聚合（v2.11）：各账号的群数量（仅统计 managed=1 的在线群）。"""

        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT account_id, COUNT(*) AS groups FROM groups "
                "WHERE account_id IS NOT NULL AND account_id != '' "
                "AND managed = 1 "
                "GROUP BY account_id ORDER BY groups DESC"
            ).fetchall()
            return [
                {"account_id": r["account_id"], "groups": r["groups"]} for r in rows
            ]

        return await self._exec(self._run, _do)

    async def fts_match(
        self, group_id: str | None, q: str, limit: int = 2000
    ) -> list[int]:
        """磁盘化全文检索（v2.9）：FTS5 trigram 子串匹配（≥3 字符词元），
        短词元（1-2 字符）回退 name LIKE；纯短词查询不依赖 FTS。

        复杂度与数据规模解耦：单次查询毫秒级，内存零占用。
        """

        def _do(conn: sqlite3.Connection):
            qs = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", (q or "").lower()))
            if not qs:
                return []
            terms = qs.split()
            long_t = [t for t in terms if len(t) >= 3]
            short_t = [t for t in terms if len(t) < 3]
            ids: set | None = None
            if long_t:
                expr = " AND ".join('"%s"' % t.replace('"', '""') for t in long_t)
                try:
                    # Everything 式：bm25 相关度排序 + 上限截断（防大结果集 IN 膨胀）
                    ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT rowid FROM resources_fts "
                            "WHERE resources_fts MATCH ? "
                            "ORDER BY rank LIMIT 500",
                            (expr,),
                        )
                    ]
                except sqlite3.OperationalError:
                    ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT rowid FROM resources_fts "
                            "WHERE resources_fts MATCH ? LIMIT 500",
                            (expr,),
                        )
                    ]
                ids = set(ids)
            like_sql = ""
            like_params: list = []
            if short_t:
                conds = []
                for t in short_t:
                    esc = (
                        t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    )
                    # 短词元（1-2 字符，trigram 无法索引）：名称/摘要/标签 三列 LIKE
                    conds.append(
                        "(lower(name) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(json_extract(meta, '$.summary'), '')) "
                        "LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(tags, '')) LIKE ? ESCAPE '\\')"
                    )
                    like_params.extend([f"%{esc}%"] * 3)
                like_sql = " AND " + " AND ".join(conds)
            if ids is not None:
                if not ids:
                    return []
                ph = ",".join("?" * len(ids))
                sql = (
                    f"SELECT id FROM resources WHERE status='active' "
                    f"AND id IN ({ph})"
                    + like_sql
                    + (" AND group_id=?" if group_id else "")
                    + f" LIMIT {int(limit)}"
                )
                params = list(ids) + like_params + ([str(group_id)] if group_id else [])
                return [r[0] for r in conn.execute(sql, params)]
            sql = (
                "SELECT id FROM resources WHERE status='active'"
                + like_sql
                + (" AND group_id=?" if group_id else "")
                + f" LIMIT {int(limit)}"
            )
            params = like_params + ([str(group_id)] if group_id else [])
            return [r[0] for r in conn.execute(sql, params)]

        return await self._exec(self._run, _do)

    async def tag_cloud(self, kind: str | None = None) -> list[dict]:
        """标签云聚合：active 资源的 tag → 计数（降序）。

        2026-09-01 W-9 分模块隔离：kind=None=全局（文件等全类型）；
        kind=album / kind=essence 仅聚合该类型资源标签（相册/精华各自独立标签云，
        不复用统一标签语义——标签表仍为 resources.tags 列，视图按模块隔离）。
        缓存 120s，按 kind 分区。
        """
        now = time.monotonic()
        key = kind or "*"
        cache = self._tag_cloud_cache.get(key)
        if cache and now - cache[0] < 120.0:
            return cache[1]

        def _do(conn: sqlite3.Connection):
            sql = (
                "SELECT tags FROM resources WHERE status='active' "
                "AND tags IS NOT NULL AND tags != '' AND tags != '[]'"
            )
            params: list = []
            if kind:
                sql += " AND type=?"
                params.append(kind)
            rows = conn.execute(sql, params).fetchall()
            counts: dict[str, int] = {}
            for r in rows:
                try:
                    for t in json.loads(r[0] or "[]"):
                        counts[t] = counts.get(t, 0) + 1
                except ValueError:
                    continue
            result = [
                {"tag": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            ]
            self._tag_cloud_cache[key] = (now, result)
            return result

        return await self._exec(self._run, _do)

    async def update_resource_fields(self, id: int, **fields) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - self._RESOURCE_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid resource fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            cur = conn.execute(
                f"UPDATE resources SET {sets}, updated_at=? WHERE id=?",
                [*fields.values(), int(time.time()), id],
            )
            conn.commit()
            return cur.rowcount

        await self._exec(self._run, _do)
        self._tag_cloud_cache = {}  # name/tags/meta 变更 → 标签云失效

    @staticmethod
    def _row_with_meta(row) -> dict:
        d = dict(row)
        if d.get("meta"):
            try:
                d["meta"] = json.loads(d["meta"])
            except ValueError:
                pass
        return d

    async def get_resource_by_resource_id(self, resource_id: str) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_with_meta(row)

        return await self._exec(self._run, _do)

    # ---------- 分卷（v4，docs/09 §14.1） ----------

    async def insert_volumes(self, items: list[VolumeInfo]) -> None:
        def _do(conn: sqlite3.Connection):
            try:
                for v in items:
                    conn.execute(
                        """INSERT INTO volumes
                             (parent_resource_id, seq, part_name, source_ref,
                              busid, size, sha256, status, upload_time, group_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(parent_resource_id, seq) DO UPDATE SET
                             part_name=excluded.part_name, status=excluded.status,
                             size=excluded.size, sha256=excluded.sha256,
                             group_id=COALESCE(excluded.group_id, group_id)
                        """,
                        (
                            v.parent_resource_id,
                            v.seq,
                            v.part_name,
                            v.source_ref,
                            v.busid,
                            v.size,
                            v.sha256,
                            v.status,
                            v.upload_time,
                            v.group_id,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await self._exec(self._run, _do)

    async def list_volumes(self, parent_resource_id: str) -> list[VolumeInfo]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT parent_resource_id, seq, part_name, source_ref, busid, "
                "size, sha256, status, upload_time, group_id FROM volumes "
                "WHERE parent_resource_id=? ORDER BY seq",
                (parent_resource_id,),
            ).fetchall()
            return [VolumeInfo(**dict(r)) for r in rows]

        return await self._exec(self._run, _do)

    _VOLUME_FIELD_WHITELIST = frozenset(
        {"source_ref", "busid", "sha256", "status", "part_name", "size"}
    )

    async def update_volume_fields(
        self, parent_resource_id: str, seq: int, **fields
    ) -> None:
        def _do(conn: sqlite3.Connection):
            unknown = set(fields) - self._VOLUME_FIELD_WHITELIST
            if unknown:
                raise ValueError(f"invalid volume fields: {sorted(unknown)}")
            if not fields:
                return
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE volumes SET {sets} WHERE parent_resource_id=? AND seq=?",
                [*fields.values(), parent_resource_id, seq],
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def backfill_volume_by_part(
        self, group_id: str, part_name: str, source_ref: str, busid: int
    ) -> int:
        """事件驱动回填：part 文件名匹配「同群 + 未就绪」的分卷。"""

        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                """UPDATE volumes SET source_ref=?, busid=?
                   WHERE part_name=?
                     AND (source_ref IS NULL OR source_ref='')
                     AND parent_resource_id LIKE ?
                """,
                (source_ref, busid, part_name, f"{group_id}:file:%"),
            )
            conn.commit()
            return cur.rowcount

        return await self._exec(self._run, _do)

    async def remove_volumes(self, parent_resource_id: str) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                "DELETE FROM volumes WHERE parent_resource_id=?", (parent_resource_id,)
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def count_active(self, group_id: str) -> int:
        """群内 active 文件计数（容量持久化兜底）。"""

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COUNT(*) FROM resources WHERE group_id=? AND status='active'",
                (group_id,),
            ).fetchone()
            return int(row[0] or 0)

        return await self._exec(self._run, _do)

    async def get_resource_any(self, id: int) -> dict | None:
        """按主键全局取资源（跨群兜底定位）。"""

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id=? AND status='active'", (id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("meta"):
                try:
                    d["meta"] = json.loads(d["meta"])
                except ValueError:
                    pass
            return d

        return await self._exec(self._run, _do)

    async def mark_missing_as_deleted(
        self, group_id: str, complete: bool, source_file_ids: set[str]
    ) -> int:
        """孤儿清理门控（DoD #5 / AC9）：complete=False 时禁止执行。"""
        if not complete:
            logger.warning(
                f"[group_cloud_storage] skip orphan cleanup for {group_id}: sync incomplete"
            )
            return 0
        if not source_file_ids:
            return 0

        def _do(conn: sqlite3.Connection):
            conn.execute("BEGIN")
            try:
                n = 0
                ids = sorted(source_file_ids)
                for i in range(0, len(ids), 150):  # v2.13: 500→150 凋零降速（写入速度的 1/3）
                    chunk = ids[i : i + 150]
                    marks = ",".join("?" for _ in chunk)
                    cur = conn.execute(
                        f"""
                        UPDATE resources SET status=?, updated_at=?
                        WHERE group_id=? AND type='file' AND status='active'
                          AND source_ref NOT IN ({marks})
                          AND (meta IS NULL OR meta NOT LIKE '%"volumes": true%')
                        """,
                        [
                            ResourceStatus.DELETED.value,
                            int(time.time()),
                            group_id,
                            *chunk,
                        ],
                    )
                    n += cur.rowcount
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

        result = await self._exec(self._run, _do)
        self._tag_cloud_cache = {}  # status 变化 → 标签云失效
        return result

    # ---------- 目录持久化管理（v7） ----------

    async def upsert_folders(self, group_id: str, folders) -> None:
        """目录实体入库（folder_id/folder_name/parent_id 幂等）。"""

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

        await self._exec(self._run, _do)

    async def upsert_album_essence(
        self, group_id: str, albums: list, essences: list, account_id: str = ""
    ) -> None:
        """资源化：相册条目 + 精华消息（统一资源目录，仅元数据/摘要，不占本地空间）。"""
        from core.domain.enums import ResourceType

        rows = []
        for a in albums:
            # 真实 NapCat 相册条目字段：{album_id, owner, name, desc, create_time,
            # modify_time, last_upload_time, upload_number, cover}（v9 实测修复）
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
                        "upload_number": int(a.get("upload_number", 0) or 0),
                    },
                )
            )
        for e in essences:
            # 真实 NapCat 响应：content 为消息段数组（[{type,data:{text/...}}]），
            # 兼容旧式纯字符串 content/text（v9 实测修复）
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
                    meta={"essence": True, "msg_type": etype, "summary": text[:200]},
                )
            )

        def _reconcile(conn: sqlite3.Connection):
            # 一致性对账：云端已不存在的相册/精华条目同步删除
            # （仅影响本次采集的群；增量未触达的群保持原样）
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

        await self._exec(self._run, _reconcile)
        await self.upsert_resources(rows)

    async def list_folders_detail(self, group_id: str) -> list[dict]:
        """群目录实体列表（file 列表 API 的目录下拉数据源）。"""

        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT folder_id, folder_name, parent_id, sort_order FROM folders "
                "WHERE group_id=? ORDER BY sort_order, folder_name",
                (group_id,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._exec(self._run, _do)

    async def clear_folders(self, group_id: str) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute("DELETE FROM folders WHERE group_id=?", (group_id,))
            conn.commit()

        await self._exec(self._run, _do)

    # ---------- 同步日志 ----------

    async def create_sync_log(self, log: SyncLog) -> int:
        def _do(conn: sqlite3.Connection):
            gid = (log.group_id or "unknown") if log.group_id is not None else "unknown"
            cur = conn.execute(
                "INSERT INTO sync_logs (group_id, kind, status, start_at) VALUES (?,?,?,?)",
                (gid, log.kind.value, log.status.value, log.start_at),
            )
            conn.commit()
            return cur.lastrowid

        return await self._exec(self._run, _do)

    async def finish_sync_log(self, log_id: int, result: SyncResult) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                """
                UPDATE sync_logs SET status=?, files_found=?, files_indexed=?,
                       complete=?, error=?, end_at=?
                WHERE id=?
                """,
                (
                    result.status.value,
                    result.files_found,
                    result.files_indexed,
                    int(result.complete),
                    result.error,
                    int(time.time()),
                    log_id,
                ),
            )
            conn.commit()

        await self._exec(self._run, _do)

    # ---------- 快照 ----------

    async def save_snapshot(self, snap: Snapshot) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                """
                INSERT INTO snapshots
                  (group_id, type, file_count, total_size, used_space,
                   total_space, detail, taken_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    snap.group_id,
                    snap.type,
                    snap.file_count,
                    snap.total_size,
                    snap.used_space,
                    snap.total_space,
                    json.dumps(snap.detail, ensure_ascii=False),
                    snap.taken_at,
                ),
            )
            conn.commit()

        await self._exec(self._run, _do)

    # ---------- 账号级生命周期管理 ----------

    async def mark_all_groups_managed(self, managed: int) -> int:
        """批量设置所有群的管理标记（启动时 managed=0 保护，扫描后恢复）。"""

        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                "UPDATE groups SET managed=? WHERE managed!=?",
                (managed, managed),
            )
            conn.commit()
            return cur.rowcount

        return await self._exec(self._run, _do)

    async def mark_account_groups_managed(self, account_id: str, managed: int) -> int:
        """按账号 ID 批量设置群管理标记（0=账号离线后隐藏，1=恢复）。"""
        if not account_id:
            return 0

        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                "UPDATE groups SET managed=? WHERE account_id=? AND managed!=?",
                (managed, account_id, managed),
            )
            conn.commit()
            return cur.rowcount

        result = await self._exec(self._run, _do)
        logger.info(
            f"[group_cloud_storage] account {account_id} groups "
            f"managed={managed}: {result} rows"
        )
        return result

    async def restore_account_groups(self, account_id: str) -> int:
        """账号恢复在线：将该账号的群 managed 重置为 1（扫描成功后调用）。"""
        return await self.mark_account_groups_managed(account_id, 1)

    # ---------- archive_map (REQ-03/09) ----------

    async def get_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> dict | None:
        """Get archive map entry for a specific resource and direction."""

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT resource_id, group_id, task_id, remote_path, "
                "direction, state, updated_at "
                "FROM archive_map "
                "WHERE group_id=? AND resource_id=? AND direction=?",
                (group_id, resource_id, direction),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

        return await self._exec(self._run, _do)

    async def upsert_archive_map(self, row: dict) -> None:
        """Insert or update archive map entry.

        Uses (resource_id, group_id, direction) as conflict key.
        remote_path is NOT part of the key because it changes on rename.
        """

        def _do(conn: sqlite3.Connection):
            conn.execute(
                "INSERT INTO archive_map "
                "(resource_id, group_id, task_id, remote_path, direction, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(resource_id, group_id, direction) "
                "DO UPDATE SET task_id=?, remote_path=?, state=?, updated_at=?",
                (
                    row["resource_id"],
                    row["group_id"],
                    row.get("task_id"),
                    row["remote_path"],
                    row["direction"],
                    row["state"],
                    row["updated_at"],
                    row.get("task_id"),
                    row["remote_path"],
                    row["state"],
                    row["updated_at"],
                ),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def clear_archive_map(
        self, group_id: str, resource_id: int, direction: str
    ) -> None:
        """Remove archive map entry for a specific resource and direction."""

        def _do(conn: sqlite3.Connection):
            conn.execute(
                "DELETE FROM archive_map "
                "WHERE group_id=? AND resource_id=? AND direction=?",
                (group_id, resource_id, direction),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def list_archive_map(
        self, *, states: tuple[str, ...], direction: str
    ) -> list[dict]:
        """List archive map entries filtered by state and direction."""

        def _do(conn: sqlite3.Connection):
            placeholders = ",".join("?" for _ in states)
            rows = conn.execute(
                f"SELECT resource_id, group_id, task_id, remote_path, "
                f"direction, state, updated_at "
                f"FROM archive_map "
                f"WHERE state IN ({placeholders}) AND direction=?",
                (*states, direction),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._exec(self._run, _do)

    async def update_archive_state(self, row: dict, state: str) -> None:
        """Update state of an archive map entry.

        Uses resource_id + group_id + direction as key (not remote_path,
        which may change due to rename operations).
        """

        def _do(conn: sqlite3.Connection):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE archive_map SET state=?, updated_at=? "
                "WHERE resource_id=? AND group_id=? AND direction=?",
                (state, now, row["resource_id"], row["group_id"], row["direction"]),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def update_archive_remote_path(
        self, resource_id: int, group_id: str, direction: str, new_remote_path: str
    ) -> None:
        """Update remote_path of an archive map entry (for rename operations)."""

        def _do(conn: sqlite3.Connection):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE archive_map SET remote_path=?, updated_at=? "
                "WHERE resource_id=? AND group_id=? AND direction=?",
                (new_remote_path, now, resource_id, group_id, direction),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def update_archive_state_by_task(self, task_id: str, state: str) -> None:
        """Update state of an archive map entry by task_id."""

        def _do(conn: sqlite3.Connection):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE archive_map SET state=?, updated_at=? WHERE task_id=?",
                (state, now, task_id),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def get_archive_map_by_task(self, task_id: str) -> dict | None:
        """Get archive map entry by bridge task id (OpenList task or fetch op id)."""

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT resource_id, group_id, task_id, remote_path, "
                "direction, state, updated_at "
                "FROM archive_map "
                "WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

        return await self._exec(self._run, _do)

    async def list_archived_done_ids(
        self, resource_ids: list[int], direction: str = "out"
    ) -> set[int]:
        """2026-09-01 N-02：返回给定资源 ID 中已归档完成（state=done）的 ID 集。

        文件状态筛选「在网盘」派生（SELECT 只读，不写库）。
        """

        def _do(conn: sqlite3.Connection):
            if not resource_ids:
                return set()
            marks = ",".join("?" for _ in resource_ids)
            rows = conn.execute(
                f"SELECT DISTINCT resource_id FROM archive_map "
                f"WHERE resource_id IN ({marks}) AND direction=? AND state='done'",
                (*resource_ids, direction),
            ).fetchall()
            return {r[0] for r in rows}

        return await self._exec(self._run, _do)

    # ---------- netdisk_meta（ADR-0004，N4） ----------

    async def upsert_netdisk_rows(self, rows: list[dict]) -> int:
        """浏览登记：INSERT OR IGNORE 幂等新增；不覆盖已有人工标注。"""

        def _do(conn: sqlite3.Connection):
            cur = conn.cursor()
            before = conn.total_changes
            for r in rows:
                cur.execute(
                    "INSERT OR IGNORE INTO netdisk_meta "
                    "(remote_path, name, is_dir, size, type, tags, registered_at, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["remote_path"],
                        r["name"],
                        int(r.get("is_dir") or 0),
                        int(r.get("size") or 0),
                        r.get("type") or "other",
                        r.get("tags") or "",
                        r["registered_at"],
                        r.get("indexed_at") or "",
                    ),
                )
            conn.commit()
            return conn.total_changes - before

        return await self._exec(self._run, _do)

    async def get_netdisk_meta(self, dir_prefix: str) -> list[dict]:
        """按目录前缀取标记行（remote_path LIKE dir_prefix%）。"""

        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT remote_path, name, is_dir, size, type, tags, "
                "registered_at, indexed_at "
                "FROM netdisk_meta WHERE remote_path LIKE ?",
                (dir_prefix + "%",),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._exec(self._run, _do)

    async def set_netdisk_tags(self, remote_path: str, tags: str) -> None:
        """设置单文件标签（覆盖式）。"""

        def _do(conn: sqlite3.Connection):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE netdisk_meta SET tags=?, registered_at=? WHERE remote_path=?",
                (tags, now, remote_path),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def mark_netdisk_indexed(self, remote_paths: list[str]) -> None:
        """深度索引回填 indexed_at。"""

        def _do(conn: sqlite3.Connection):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "UPDATE netdisk_meta SET indexed_at=? WHERE remote_path=?",
                [(now, p) for p in remote_paths],
            )
            conn.commit()

        await self._exec(self._run, _do)

    # ---------- 任务台账与操作流（v15，ADR-0005 经纠偏 D-6 实施） ----------

    _LEDGER_BREAKPOINT_KINDS = ("convert_volumes", "video_upload", "netdisk_index")

    @staticmethod
    def _now_ts() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    async def ledger_upsert(
        self,
        task_id: str,
        kind: str,
        target: str = "",
        payload: dict | None = None,
        state: str = "pending",
        retries: int = 0,
        error: str | None = None,
    ) -> None:
        """台账 upsert（状态机：pending/running/paused/retry/done/failed/cancelled）。"""

        def _do(conn: sqlite3.Connection):
            now = self._now_ts()
            conn.execute(
                """INSERT INTO op_ledger
                   (task_id, kind, target, payload, state, retries, error,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     state=excluded.state,
                     retries=excluded.retries,
                     error=CASE WHEN excluded.error IS NOT NULL THEN excluded.error
                                WHEN excluded.state IN ('done','cancelled') THEN NULL
                                ELSE op_ledger.error END,
                     updated_at=excluded.updated_at""",
                (
                    task_id,
                    kind,
                    target,
                    json.dumps(payload or {}, ensure_ascii=False),
                    state,
                    int(retries or 0),
                    error,
                    now,
                    now,
                ),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def ledger_get(self, task_id: str) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM op_ledger WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["payload"] = json.loads(d.get("payload") or "{}")
            return d

        return await self._exec(self._run, _do)

    async def ledger_query(
        self,
        state: str | None = None,
        kind: str | None = None,
        target: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            sql = "SELECT * FROM op_ledger WHERE 1=1"
            args: list = []
            if state:
                sql += " AND state=?"
                args.append(state)
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            if target:
                sql += " AND target=?"
                args.append(target)
            sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ? OFFSET ?"
            args += [int(limit), int(offset)]
            out = []
            for r in conn.execute(sql, args).fetchall():
                d = dict(r)
                d["payload"] = json.loads(d.get("payload") or "{}")
                out.append(d)
            return out

        return await self._exec(self._run, _do)

    async def ledger_reconcile(self) -> int:
        """启动对账（ADR-0005）：宿主重启后 running/paused/retry 一律非终态——
        断点续传白名单 kind 置 pending（候选重提），其余置 failed（重启中断）；
        未及执行的 pending 同样置 failed（内存队列已失）。返回受影响行数。"""

        def _do(conn: sqlite3.Connection):
            now = self._now_ts()
            n1 = conn.execute(
                """UPDATE op_ledger SET state='pending', updated_at=?
                   WHERE state IN ('running','paused','retry')
                     AND kind IN (?,?,?)""",
                (now,) + self._LEDGER_BREAKPOINT_KINDS,
            ).rowcount
            n2 = conn.execute(
                """UPDATE op_ledger SET state='failed',
                   error=COALESCE(error, 'interrupted by restart'),
                   updated_at=?
                   WHERE state IN ('running','paused','retry','pending')
                     AND kind NOT IN (?,?,?)""",
                (now,) + self._LEDGER_BREAKPOINT_KINDS,
            ).rowcount
            conn.commit()
            return n1 + n2

        return await self._exec(self._run, _do)

    async def ops_append(
        self, task_id: str, action: str, before: dict | None, after: dict | None
    ) -> None:
        """操作流追加（可逆操作 before/after 快照；直连操作 task_id 传 ''）。"""

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM op_ops WHERE task_id=?",
                (task_id,),
            ).fetchone()
            seq = row["s"] if row else 1
            conn.execute(
                """INSERT INTO op_ops (task_id, seq, action, before, after, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    task_id,
                    seq,
                    action,
                    json.dumps(before or {}, ensure_ascii=False),
                    json.dumps(after or {}, ensure_ascii=False),
                    self._now_ts(),
                ),
            )
            conn.commit()

        await self._exec(self._run, _do)

    async def ops_list(self, task_id: str) -> list[dict]:
        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM op_ops WHERE task_id=? ORDER BY seq, op_id",
                (task_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["before"] = json.loads(d.get("before") or "{}")
                d["after"] = json.loads(d.get("after") or "{}")
                out.append(d)
            return out

        return await self._exec(self._run, _do)

    async def ops_last_for_resource(self, action: str, resource_id: int) -> dict | None:
        """直连操作（无队列任务）定位：按资源定位最近一次操作流记录（如标签撤销）。"""

        def _do(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM op_ops WHERE action=? AND task_id='' "
                "ORDER BY op_id DESC LIMIT 200",
                (action,),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["before"] = json.loads(d.get("before") or "{}")
                d["after"] = json.loads(d.get("after") or "{}")
                if d["after"].get("id") == resource_id:
                    return d
            return None

        return await self._exec(self._run, _do)

    async def hide_account_groups(self, account_id: str, hidden: int) -> int:
        """账号离线 → 该账号全部群组隐藏（hidden=1，非删除）；恢复在线 → 0 重新显示。"""

        def _do(conn: sqlite3.Connection):
            cur = conn.execute(
                "UPDATE groups SET hidden=? WHERE account_id=?", (int(hidden), account_id)
            )
            conn.commit()
            return cur.rowcount

        return await self._exec(self._run, _do)
