"""W-3b 契约测试：DNS 校验必须离开同步构造路径。

根因（已实证：``OpenListClient()`` 一次构造触发 1 次 ``getaddrinfo``）
--------------------------------------------------------------------
``OpenListClient.__init__`` 调用 ``validate_base_url``，对主机名会走
``_check_dns`` → **阻塞** ``socket.getaddrinfo``。而该构造器是从
``bootstrap.build_components`` 调用的，即 AstrBot 的**同步插件装配路径**——
那里没有事件循环可让出。解析器慢或卡住，整个插件装载就被拖住，
其它插件也一起排队。

同文件早有成熟惯例：``assert_fetch_url_allowed`` / ``resolve_and_pin_ip``
都标注「blocking call; async callers should wrap it in asyncio.to_thread」，
``core/application/transfer.py`` 正是这么用的。

契约
----
1. 构造期只做**零 I/O** 的结构校验：scheme、hostname、字面 IP 范围。
   ⇒ 构造期间 ``getaddrinfo`` 调用数必须为 **0**。
   ⇒ 非法 scheme / 缺 hostname / 受限字面 IP 仍要在构造期立刻抛错（响亮）。
2. DNS 校验延后到首次使用时执行，且**必须经 ``asyncio.to_thread``**，
   不得阻塞事件循环。
3. 延后不等于取消：受限地址 / 解析失败必须在首次使用时抛出与原先**同样**
   的 ``ExternalApiError``，错误语义（消息、提示键）不许退化。
4. ``validate_base_url`` 的对外语义与签名**不变**（既有调用方与测试依赖它）。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.modules.setdefault("astrbot", MagicMock())
sys.modules.setdefault("astrbot.api", MagicMock())
sys.modules.setdefault("astrbot.api.logger", MagicMock())

from adapters.external.base import (  # noqa: E402
    ExternalApiError,
    validate_base_url,
    validate_base_url_structure,
    validate_hostname_dns,
)
from adapters.external.openlist import OpenListClient  # noqa: E402

RESOLVABLE_HOST_URL = "https://example.com:5244"


@pytest.fixture
def dns_spy():
    """Count getaddrinfo calls without changing resolution behaviour."""
    calls = []
    real = socket.getaddrinfo

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    with patch.object(socket, "getaddrinfo", spy):
        yield calls


# ---------- 核心：构造期不得做 DNS ----------


def test_construction_performs_no_dns_lookup(dns_spy):
    """W-3b 的主断言：构造期 getaddrinfo 调用数为 0。"""
    OpenListClient(base_url=RESOLVABLE_HOST_URL, token="t")
    assert dns_spy == [], f"构造期不该解析 DNS，实际调用了 {len(dns_spy)} 次"


def test_construction_of_a_private_hostname_is_also_dns_free(dns_spy):
    """即便 allow_private=False（本该走 DNS 分支）也不得在构造期解析。"""
    OpenListClient(
        base_url="http://openlist:5244",
        token="t",
        allow_private_address=False,
    )
    assert dns_spy == []


def test_construction_records_the_normalized_base_url():
    """延后校验不得影响 base_url 归一化结果。"""
    c = OpenListClient(base_url="https://example.com:5244", token="t")
    assert c._base_url == "https://example.com:5244"
    c2 = OpenListClient(base_url="https://example.com", token="t")
    assert c2._base_url == "https://example.com"
    c3 = OpenListClient(base_url="http://10.0.0.1:5244", token="t",
                        allow_private_address=True)
    assert c3._base_url == "http://10.0.0.1:5244"


def test_construction_marks_validation_as_pending():
    c = OpenListClient(base_url=RESOLVABLE_HOST_URL, token="t")
    assert c._validated is False


# ---------- 构造期仍须响亮拒绝「结构性」错误 ----------


def test_construction_still_rejects_a_bad_scheme():
    """scheme 检查是纯解析，必须留在构造期——配置错要立刻炸。"""
    with pytest.raises(ExternalApiError, match="scheme"):
        OpenListClient(base_url="ftp://example.com:5244", token="t")


def test_construction_still_rejects_a_missing_hostname():
    with pytest.raises(ExternalApiError, match="hostname"):
        OpenListClient(base_url="http://", token="t")


def test_construction_still_rejects_a_restricted_literal_ip():
    """字面 IP 无需 DNS，构造期就能判——也必须判。"""
    with pytest.raises(ExternalApiError, match="restricted address"):
        OpenListClient(base_url="http://10.0.0.1:5244", token="t")


def test_construction_accepts_a_restricted_literal_ip_when_allowed():
    c = OpenListClient(
        base_url="http://10.0.0.1:5244", token="t", allow_private_address=True
    )
    assert c._base_url == "http://10.0.0.1:5244"


# ---------- 延后校验：异步、且不阻塞事件循环 ----------


@pytest.mark.asyncio
async def test_deferred_validation_runs_off_the_event_loop():
    """必须经 asyncio.to_thread —— 在别的线程里跑。"""
    import threading

    main_thread = threading.get_ident()
    seen = {}

    def record_thread(*args, **kwargs):
        seen["thread"] = threading.get_ident()

    c = OpenListClient(base_url=RESOLVABLE_HOST_URL, token="t")
    with patch(
        "adapters.external.openlist.validate_hostname_dns",
        side_effect=record_thread,
    ):
        await c._ensure_validated()

    assert "thread" in seen, "延后校验没有被调用"
    assert seen["thread"] != main_thread, "延后校验跑在主线程，会阻塞事件循环"


@pytest.mark.asyncio
async def test_deferred_validation_is_idempotent():
    """校验只跑一次，重复调用不再触发 DNS。"""
    c = OpenListClient(base_url=RESOLVABLE_HOST_URL, token="t")
    with patch(
        "adapters.external.openlist.validate_hostname_dns"
    ) as spy:
        await c._ensure_validated()
        await c._ensure_validated()
        await c._ensure_client()
    assert spy.call_count == 1
    assert c._validated is True


@pytest.mark.asyncio
async def test_restricted_address_still_raises_during_client_use():
    """延后不等于放过：受限地址必须在使用期抛错，且经 to_thread 到达。

    注意责任划分：字面 IP 由结构检查负责（见
    ``test_construction_still_rejects_a_restricted_literal_ip``），
    ``validate_hostname_dns`` 对字面 IP 是 no-op。这里验证的是**主机名**
    解析成受限地址的那条路——即 W-3b 真正搬走的那条。
    """
    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]

    c = OpenListClient(base_url="http://internal.example:5244", token="t")
    with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(ExternalApiError, match="restricted address"):
            await c._ensure_validated()


@pytest.mark.asyncio
async def test_dns_failure_still_surfaces_at_first_use():
    """解析失败必须透出，且消息里点明主机名。"""
    with patch.object(
        socket, "getaddrinfo", side_effect=socket.gaierror("nope")
    ):
        with pytest.raises(ExternalApiError, match="DNS resolution failed"):
            validate_hostname_dns("http://does-not-resolve.invalid:5244")


@pytest.mark.asyncio
async def test_client_creation_triggers_the_deferred_check():
    """_ensure_client 是真正的「首次使用」闸门。"""
    c = OpenListClient(base_url=RESOLVABLE_HOST_URL, token="t")
    assert c._validated is False
    with patch(
        "adapters.external.openlist.validate_hostname_dns"
    ) as spy:
        client = await c._ensure_client()
    assert spy.call_count == 1
    assert c._validated is True
    await client.aclose()


# ---------- validate_base_url 对外语义不变 ----------


def test_validate_base_url_still_does_both_halves():
    """组合函数：结构 + DNS，一次性完成（同步调用方的旧行为）。"""
    assert validate_base_url("https://example.com:5244") == "https://example.com:5244"


def test_validate_base_url_still_rejects_scheme_and_host():
    with pytest.raises(ExternalApiError, match="scheme"):
        validate_base_url("ftp://example.com")
    with pytest.raises(ExternalApiError, match="hostname"):
        validate_base_url("http://")


def test_validate_base_url_still_rejects_restricted_ip():
    with pytest.raises(ExternalApiError, match="restricted address"):
        validate_base_url("http://127.0.0.1:5244")
    assert validate_base_url("http://127.0.0.1:5244", allow_private=True) == (
        "http://127.0.0.1:5244"
    )


def test_validate_base_url_still_runs_dns_for_a_hostname(dns_spy):
    """旧调用方依赖「组合函数会解析 DNS」——不能悄悄变成不解析。"""
    validate_base_url("https://example.com:5244")
    assert len(dns_spy) == 1


# ---------- 结构检查单独用：零 I/O ----------


def test_structure_check_is_dns_free(dns_spy):
    validate_base_url_structure("https://example.com:5244")
    assert dns_spy == []


def test_structure_check_normalizes():
    assert validate_base_url_structure("https://example.com") == "https://example.com"
    assert (
        validate_base_url_structure("http://h:8080") == "http://h:8080"
    )
    assert (
        validate_base_url_structure("http://10.0.0.1", allow_private=True)
        == "http://10.0.0.1"
    )


def test_hostname_dns_is_a_noop_for_literal_ips(dns_spy):
    validate_hostname_dns("http://10.0.0.1:5244", allow_private=True)
    validate_hostname_dns("http://10.0.0.1:5244")  # literal IP, nothing to resolve
    assert dns_spy == []


def test_hostname_dns_is_a_noop_when_private_allowed(dns_spy):
    validate_hostname_dns("https://example.com:5244", allow_private=True)
    assert dns_spy == []
