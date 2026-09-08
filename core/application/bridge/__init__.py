"""Bridge domain - bidirectional bridging between group files and the
OpenList cloud drive (two-way transfer and conflict resolution).

Boundary with ingest: ingest is one-way collection from the cloud drive
into the resource library (metadata + volume persistence); bridge is
bidirectional transport between group files and OpenList (submit to cloud,
inbound to group, polling for receipts).
Base utilities (timestamp/path name/extension) live in
core.application.common; domain aliases are kept here.
"""
from __future__ import annotations

import secrets
import string

from core.application.common import path_basename as _basename  # noqa: F401  (re-export)
from core.application.common import split_ext as _split_ext  # noqa: F401  (re-export)
from core.application.common import utc_now_iso as _now  # noqa: F401  (re-export)


def _short_suffix() -> str:
    """Generate a CSPRNG short suffix for conflict resolution (unguessable)."""
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))


from .inbound import InboundMixin  # noqa: E402  (needs the aliases above)
from .polling import PollingMixin  # noqa: E402
from .recovery import RecoveryMixin  # noqa: E402
from .service import BridgeService  # noqa: E402
from .submit import SubmitMixin  # noqa: E402

__all__ = ["SubmitMixin", "InboundMixin", "PollingMixin", "RecoveryMixin", "BridgeService"]
