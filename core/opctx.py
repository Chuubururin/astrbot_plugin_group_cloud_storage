"""Cross-layer operation context (contextvars).

`account_var` scopes OneBot calls to the account that actually owns the
target group: the op dispatcher sets it around group-targeted operations and
the runtime binding (adapter._bind_call_action) resolves the bot registered
for that account before falling back to the event bot / best_bot. This is
what makes multi-account deployments route file operations to the right
account instead of whichever bot spoke last.

Kept dependency-free so both core.runtime and core.application can import it
without cycles.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

account_var: ContextVar[str] = ContextVar("gcs_op_account", default="")


@contextmanager
def account_scope(account_id: str):
    """Scope OneBot calls to one account inside the block (empty = no scope)."""
    account_id = str(account_id or "")
    if not account_id:
        yield
        return
    token = account_var.set(account_id)
    try:
        yield
    finally:
        account_var.reset(token)
