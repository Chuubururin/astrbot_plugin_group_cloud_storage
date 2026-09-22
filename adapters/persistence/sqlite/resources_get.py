"""Resources single-row lookups — by uri / id / resource_id."""
from __future__ import annotations

import sqlite3

from .resources import _row_with_meta
from .state import StorePart


class ResourceGetMixin(StorePart):
    """Point reads on the resources table."""

    async def get_by_uri(self, uri: str) -> dict | None:
        """Resolve ``cloud://<group_id>/<type>/<id>`` to an active row.

        group_id and type are part of the URI identity, not decoration: the
        URI is a public, encodable handle (webapi/resources_mutation.py
        exposes it), so matching on the trailing id alone let
        ``cloud://<any group>/<any type>/<id>`` reach a row belonging to
        another group. A mismatch reads as "not found" (None), the same
        convention as an unknown id; malformed URIs still raise ValueError.
        """
        prefix = "cloud://"
        if not uri.startswith(prefix):
            raise ValueError("invalid resource uri")
        parts = uri[len(prefix) :].split("/")
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValueError("invalid resource uri")
        group_id, rtype, sid = parts[0], parts[1], int(parts[2])

        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id=? AND status='active' "
                "AND group_id=? AND type=?",
                (sid, group_id, rtype),
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def get_resource_detail(self, group_id: str, id: int) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND group_id = ? AND type='file'",
                (id, group_id),
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def get_resource_by_resource_id(self, resource_id: str) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)

    async def get_resource_any(self, id: int) -> dict | None:
        def _do(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM resources WHERE id=? AND status='active'", (id,)
            ).fetchone()
            if not row:
                return None
            return _row_with_meta(row)

        return await self._conn.exec(_do)
