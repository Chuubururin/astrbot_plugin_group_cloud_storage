"""Runtime __init__ — re-export kernel, lifecycle, events, commands."""
from .kernel import RuntimeKernel
from .lifecycle import LifecycleManager
from .adapter import RuntimeAdapter
from .events import handle_group_upload_event, handle_new_bot_discovered
from .commands import strip_command_params, parse_group_id, parse_page_number

__all__ = [
    "RuntimeKernel",
    "LifecycleManager",
    "RuntimeAdapter",
    "handle_group_upload_event",
    "handle_new_bot_discovered",
    "strip_command_params",
    "parse_group_id",
    "parse_page_number",
]
