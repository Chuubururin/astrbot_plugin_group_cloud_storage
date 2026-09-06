"""Configuration schema — validation logic.

validate_config() warns at startup (unknown keys / conversion failures) and
never blocks execution.
"""

from __future__ import annotations

from .defaults import DEFAULTS


def validate_config(data: dict) -> list[tuple[str, str]]:
    """Returns [(key, message)] warnings; one each for unknown keys and conversion failures."""
    warnings: list[tuple[str, str]] = []
    for key in data:
        if key not in DEFAULTS:
            warnings.append((key, "未知配置键（schema 中不存在）"))
    for key in ("managed_groups", "global_admin_qqs", "op_high_priority_kinds"):
        if key in data and not isinstance(data[key], list):
            warnings.append(
                (key, f"期望 list，实际 {type(data[key]).__name__}")
            )
    return warnings
