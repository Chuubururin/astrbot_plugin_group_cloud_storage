"""L2 回归：adapters/onebot/base.py 的 retcode 读取不得裸逃 KeyError。

aiocqhttp 的 ActionFailed.retcode 是*属性*（内部 return self.result['retcode']），
响应体缺 retcode 键时抛 KeyError，而 getattr 只吞 AttributeError。裸 KeyError
逃出 _classify 后，调用方拿到非 OneBotApiError 异常 → 被队列当可重试，而不是
能力判定（1404 → UNSUPPORTED）。

Run: pytest tests/unit/test_onebot_retcode_regressions.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.onebot.base import NapCatBase  # noqa: E402
from core.domain.enums import (  # noqa: E402
    CapabilityState,
    OneBotApiError,
    OneBotErrorKind,
)


class _ActionFailedNoRetcode(Exception):
    """ActionFailed 最小替身：retcode 是属性，响应体缺键就抛 KeyError。"""

    @property
    def retcode(self):
        return {"wording": "unknown action"}["retcode"]


class _ActionFailed1404(Exception):
    @property
    def retcode(self):
        return 1404


def _base(src: Exception) -> NapCatBase:
    async def _call(_action, **_params):
        raise src

    return NapCatBase(_call, interval=0.0)


@pytest.mark.asyncio
async def test_missing_retcode_key_is_classified_as_remote_error():
    """响应体无 retcode 键 → 不得裸抛 KeyError，必须归类为
    OneBotApiError(REMOTE_ERROR)，且不标记能力状态（不误判 UNSUPPORTED）。"""
    base = _base(_ActionFailedNoRetcode())
    with pytest.raises(OneBotApiError) as ei:
        await base._call("send_group_msg", group_id="1")
    assert ei.value.kind == OneBotErrorKind.REMOTE_ERROR
    assert base.capability("send_group_msg") == CapabilityState.UNKNOWN


@pytest.mark.asyncio
async def test_retcode_1404_still_marks_unsupported():
    """语义不变：1404 → UNSUPPORTED（属性可读时行为与修复前一致）。"""
    base = _base(_ActionFailed1404())
    with pytest.raises(OneBotApiError) as ei:
        await base._call("no_such_action")
    assert ei.value.kind == OneBotErrorKind.UNSUPPORTED
    assert base.capability("no_such_action") == CapabilityState.UNSUPPORTED
