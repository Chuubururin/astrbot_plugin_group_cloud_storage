"""SQL LIKE pattern helpers.

Centralizes wildcard escaping so every ``LIKE ?`` uses the same rules and the
matching SQL always declares ``ESCAPE '\\'``. Unescaped user input silently
changes match semantics: a keyword carrying ``_`` or ``%`` matched far more
rows than the user typed (``_`` alone matches any single character).

The escape character is a backslash, matching the ``ESCAPE '\\'`` clauses the
queries declare.
"""
from __future__ import annotations


def like_escape(text: str) -> str:
    """Escape LIKE metacharacters in a literal fragment.

    Backslash is escaped first so the escapes added for ``%``/``_`` are not
    doubled again.
    """
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def like_contains(text: str) -> str:
    """Pattern for a substring match (``%escaped%``)."""
    return f"%{like_escape(text)}%"


def like_prefix(text: str) -> str:
    """Pattern for a prefix match (``escaped%``)."""
    return f"{like_escape(text)}%"


def like_suffix(text: str) -> str:
    """Pattern for a suffix match (``%escaped``)."""
    return f"%{like_escape(text)}"


__all__ = ["like_escape", "like_contains", "like_prefix", "like_suffix"]
