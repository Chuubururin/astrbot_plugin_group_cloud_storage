"""WebAPI — Page backend API modules."""
from .webapi import register_page_apis, PLUGIN_NAME
from .webapi_base import _normalize_convert_to
from .resources import _aggregate_capacity, GROUP_TOTAL_DEFAULT

__all__ = [
    "register_page_apis",
    "PLUGIN_NAME",
    "_normalize_convert_to",
    "_aggregate_capacity",
    "GROUP_TOTAL_DEFAULT",
]
