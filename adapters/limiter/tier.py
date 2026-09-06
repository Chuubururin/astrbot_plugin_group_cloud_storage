"""Action-tier rate limiting -- read actions are relaxed, write actions
keep the base interval.

The base interval comes from request_interval_ms (write and unknown
actions run at 1.0x, preserving the conservative risk-control stance);
known read-only actions are relaxed by READ_MULT so that parallelized
traversal and reconciliation are no longer serialized by the rate limiter.
"""

from __future__ import annotations

# Read-only actions (they only fetch data and never mutate cloud state).
# Unknown actions are always treated as writes (1.0x) -- it is safer to
# be slow than to miss a mutating call.
_READ_ACTIONS = frozenset({
    "get_group",
    "get_group_album_list",
    "get_group_album_media_list",
    "get_group_detail_info",
    "get_group_file_system_info",
    "get_group_file_url",
    "get_group_files_by_folder",
    "get_group_fs_info",
    "get_group_honor_info",
    "get_group_id",
    "get_group_info",
    "get_group_info_ex",
    "get_group_list",
    "get_group_member_info",
    "get_group_member_list",
    "get_group_root_files",
    "get_group_system_msg",
    "get_image",
    "get_login_info",
    "get_msg",
})

READ_MULT = 0.4


def interval_mult(action: str) -> float:
    """Return the interval multiplier for the action relative to the base
    interval (read actions < 1, write/unknown = 1)."""
    return READ_MULT if action in _READ_ACTIONS else 1.0
