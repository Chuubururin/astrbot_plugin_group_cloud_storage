"""Commands — command parsing helpers for application use cases.

The @filter.command decorators must remain in main.py (AstrBot framework
constraint).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commands.handlers import Services


# Type alias for the command handler services
CommandHandler = "Services"


def strip_command_params(text: str) -> str:
    """Extract command arguments (strip the /command prefix; tolerate an @bot prefix)."""
    if not text:
        return ""
    # Strip an @bot prefix
    if text.startswith("@"):
        parts = text.split(None, 1)
        text = parts[1] if len(parts) > 1 else ""
    # Strip a /command prefix
    if text.startswith("/"):
        parts = text.split(None, 1)
        text = parts[1] if len(parts) > 1 else ""
    return text.strip()


def parse_group_id(text: str) -> str:
    """Parse a group ID from the command arguments."""
    tokens = text.split()
    if tokens and tokens[0].isdigit() and len(tokens[0]) >= 5:
        return tokens[0]
    return ""


def parse_page_number(text: str, default: int = 1) -> int:
    """Parse a page number from the command arguments."""
    tokens = text.split()
    for token in tokens:
        if token.isdigit():
            try:
                return int(token)
            except ValueError:
                pass
    return default
