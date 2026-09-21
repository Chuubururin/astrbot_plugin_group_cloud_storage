"""H4 regression — a successful liveness retry must NOT evict the bot.

purge_stale_bots() used to treat a SUCCESSFUL second get_login_info as
offline evidence: it recorded the account in stale_account_ids, discarded
it from _online_account_ids and dropped the bot from self.bots (it never
reached the `alive` list). A transient timeout on the first probe
consequently hid a live account's groups and misrouted its operations.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.platform import PlatformBotResolver  # noqa: E402


class _FlakyBot:
    """Times out on the first probe, succeeds on the second."""

    def __init__(self, account_id="10001"):
        self._account_id = account_id
        self.calls = 0

    async def call_action(self, action):
        self.calls += 1
        if self.calls == 1:
            raise asyncio.TimeoutError("transient")
        return {"user_id": self._account_id}


class _DeadBot:
    def __init__(self):
        self.calls = 0

    async def call_action(self, action):
        self.calls += 1
        raise asyncio.TimeoutError("dead")


def _resolver(bots):
    r = PlatformBotResolver(context=None, config=None)
    r.bots = list(bots)
    return r


def test_transient_timeout_then_success_keeps_the_bot_alive():
    bot = _FlakyBot("10001")
    r = _resolver([bot])
    stale, alive = asyncio.run(r.purge_stale_bots())
    assert stale == [], "a successful probe must never be recorded as offline"
    assert [acc for acc, _ in alive] == ["10001"]
    assert r.bots == [bot], "the live bot must survive the purge"
    assert "10001" in r._online_account_ids


def test_both_probes_failing_records_the_last_known_account():
    bot = _DeadBot()
    r = _resolver([bot])
    r._account_bots["20002"] = bot
    r._online_account_ids.add("20002")
    stale, alive = asyncio.run(r.purge_stale_bots())
    assert stale == ["20002"]
    assert alive == []
    assert r.bots == []
    assert "20002" not in r._online_account_ids
