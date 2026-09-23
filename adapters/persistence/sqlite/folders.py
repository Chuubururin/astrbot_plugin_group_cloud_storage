"""Folders domain — folder CRUD (album/essence ingest moved to resources_write)."""
from __future__ import annotations

import sqlite3

from .state import StorePart


class FoldersMixin(StorePart):
    """Folder CRUD. Album/essence ingest lives with the resources write
    domain (resources_write.ResourceWriteMixin) -- it builds resource rows
    and shares the upsert transaction machinery."""

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

    async def rename_folder(self, group_id: str, folder_id: str, folder_name: str) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE folders SET folder_name=? WHERE group_id=? AND folder_id=?",
                (folder_name, group_id, folder_id),
            )
            conn.commit()

        await self._conn.exec(_do)

    async def delete_folder(self, group_id: str, folder_id: str) -> None:
        def _do(conn: sqlite3.Connection):
            conn.execute(
                "DELETE FROM folders WHERE group_id=? AND folder_id=?",
                (group_id, folder_id),
            )
            conn.commit()

        await self._conn.exec(_do)
