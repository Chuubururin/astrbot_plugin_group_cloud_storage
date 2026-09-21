"""Status convergence policy -- explicit transition tables + their SQL form.

One logical file is identified by ``(group_id, type, name)`` (``logical_key``
in the schema). A row's *status* records what happened to it, and a re-index
must not silently undo a state the user chose. That rule used to live inline
as hand-written ``CASE`` expressions in two adapters, with divergent semantics
(``resources.py`` let an incoming non-active row win; ``volumes.py`` kept
``uploaded`` forever).

The tables below are the single place where those decisions are written down.
Because ``ON CONFLICT ... DO UPDATE`` cannot call Python per row, each table
also renders its own SQL ``CASE``: :func:`resource_status_sql` and
:func:`volume_status_sql`. The Python view and the SQL view are generated from
the same source, so they cannot drift -- and ``tests/unit/test_status_policy``
pins the SQL text to the table.
"""
from __future__ import annotations

ANY = "*"

# ---- resources: re-index convergence -------------------------------------
#
# Columns: (existing status, incoming status) -> merged status.
#
# Invariants, in priority order:
#   1. An incoming non-active row always wins. It reports something the
#      existing row cannot know: the cloud listing no longer contains the
#      file (``deleted``) or the user archived it (``archived``).
#   2. ``active`` revives ``deleted``. Re-appearing in a listing means the
#      file is back; keeping the tombstone would hide it forever.
#   3. ``active`` must NOT clobber a *user* state. ``archived`` is set by an
#      explicit user action, and a passive re-index must not undo it.
RESOURCE_UPSERT_TRANSITIONS: dict[tuple[str, str], str] = {
    ("active", "active"): "active",
    ("deleted", "active"): "active",
    ("archived", "active"): "archived",
    (ANY, "active"): "active",
    (ANY, "archived"): "archived",
    (ANY, "deleted"): "deleted",
}

# ---- volumes: (re)entry of the upload pipeline ---------------------------
#
# ``insert_volumes`` is called again on every (re)entry, always with
# ``status="pending"``. A plain ``status = excluded.status`` therefore demoted
# a finished part back to pending, so the resume skip (``status ==
# "uploaded"``) never matched and pause->resume re-uploaded *every* part.
# ``uploaded`` is terminal: only an explicit purge may leave it. Intermediate
# ``uploading`` rows (a crashed attempt) still fall back to the incoming
# status so a retry can pick them up again.
VOLUME_UPSERT_TRANSITIONS: dict[tuple[str, str], str] = {
    ("uploaded", ANY): "uploaded",
    # Everything else adopts the incoming status (including ``uploading`` rows
    # from a crashed attempt, which a retry must be able to redo). Stated
    # explicitly rather than left to a fallback so an unlisted *future* status
    # still raises instead of being silently absorbed.
    (ANY, ANY): ANY,
}


def _resolve(
    table: dict[tuple[str, str], str], existing: str, incoming: str
) -> str:
    """Look up ``existing`` vs ``incoming``, falling back to wildcards.

    Resolution order: exact pair, wildcard on the existing side, wildcard on
    the incoming side. A table that matches nothing raises rather than
    guessing -- an unlisted pair is a modelling gap, not a value to invent.
    """
    for key in ((existing, incoming), (ANY, incoming), (existing, ANY), (ANY, ANY)):
        if key in table:
            return incoming if table[key] == ANY else table[key]
    raise KeyError(f"no status transition for ({existing!r}, {incoming!r})")


def merge_resource_status(existing: str | None, incoming: str) -> str:
    """Status a resource row keeps after an upsert of ``incoming``.

    ``existing is None`` means the row does not exist yet and the incoming
    status is written as-is.
    """
    if existing is None:
        return incoming
    return _resolve(RESOURCE_UPSERT_TRANSITIONS, existing, incoming)


def merge_volume_status(existing: str | None, incoming: str) -> str:
    """Status a volume row keeps after ``insert_volumes``."""
    if existing is None:
        return incoming
    return _resolve(VOLUME_UPSERT_TRANSITIONS, existing, incoming)


# ---- SQL projections -----------------------------------------------------
#
# ON CONFLICT cannot call Python per row, so the transition tables are also
# rendered as SQL. Both strings below are derived from the tables above by
# construction (every "existing wins" row becomes a WHEN), which is why they
# live in this module rather than at the two call sites.

def resource_status_sql() -> str:
    """SQL ``CASE`` for the resources upsert, from RESOURCE_UPSERT_TRANSITIONS.

    Only the "existing row wins" rows need a WHEN: any pair not listed falls
    through to ``excluded.status``, which is already the incoming value.
    """
    whens = [
        f"WHEN status = '{old}' AND excluded.status = '{new}' THEN status"
        for (old, new), keep in sorted(RESOURCE_UPSERT_TRANSITIONS.items())
        if old != ANY and new != ANY and keep == old
    ]
    body = "\n".join(f"                              {w}" for w in whens)
    return f"CASE\n{body}\n                              ELSE excluded.status END"


def volume_status_sql() -> str:
    """SQL ``CASE`` for insert_volumes, from VOLUME_UPSERT_TRANSITIONS."""
    whens = [
        f"WHEN status = '{old}' THEN status"
        for (old, _new), keep in sorted(VOLUME_UPSERT_TRANSITIONS.items())
        if old != ANY and keep == old
    ]
    body = "\n".join(f"                                     {w}" for w in whens)
    return f"CASE\n{body}\n                                     ELSE excluded.status END"


__all__ = [
    "ANY",
    "RESOURCE_UPSERT_TRANSITIONS",
    "VOLUME_UPSERT_TRANSITIONS",
    "merge_resource_status",
    "merge_volume_status",
    "resource_status_sql",
    "volume_status_sql",
]
