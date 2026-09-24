"""下载服务的监听地址与发布地址分离。

`download_server_host` 过去一个值同时决定"绑哪张网卡"和"链接里写出什么主机"，
于是"绑 A 发 B"表达不出来，而绑 `0.0.0.0` 会把字面量
`http://0.0.0.0:6186` 发给客户端——那个地址在客户端解析到客户端自己。

这里钉四件事：留空回落、分离生效、通配发布地址 fail-closed、以及 SSRF 信任集
同时收录两个地址（否则自家 bridge_out 取件会被自己的闸门拦下）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.application import download_server as _ds  # noqa: E402
from core.application.download_server import DownloadServerService  # noqa: E402
from core.application.transfer import download_endpoint_origins  # noqa: E402

_BASE = {
    "download_server_enabled": True,
    "download_token": "t",
    "download_http_port": 6186,
}


def _svc(**cfg) -> DownloadServerService:
    return DownloadServerService(None, {**_BASE, **cfg})


def _capture_binds(monkeypatch) -> list[tuple[str, int]]:
    """拦下 HTTP 绑定，返回 (host, port) 序列；不让真端口起来。"""
    binds: list[tuple[str, int]] = []

    async def _fake(handler, host, port, **kw):
        binds.append((host, port))
        return object()

    monkeypatch.setattr(_ds.asyncio, "start_server", _fake)
    return binds


def test_public_host_falls_back_to_the_bind_host():
    svc = _svc(download_server_host="192.168.1.10")
    assert svc.public_host == "192.168.1.10"
    assert svc.http_base() == "http://192.168.1.10:6186"
    assert svc.sftp_info()["host"] == "192.168.1.10"


def test_bind_and_publish_are_different_addresses():
    svc = _svc(download_server_host="0.0.0.0", download_public_host="astrbot")
    assert svc.host == "0.0.0.0", "监听地址不该被发布地址改动"
    assert svc.public_host == "astrbot"
    assert svc.http_base() == "http://astrbot:6186"
    assert svc.sftp_info()["host"] == "astrbot"


@pytest.mark.asyncio
async def test_http_binds_the_listen_host_not_the_public_one(monkeypatch):
    binds = _capture_binds(monkeypatch)
    svc = _svc(download_server_host="127.0.0.1", download_public_host="192.168.1.10")
    await svc.start()
    assert binds == [("127.0.0.1", 6186)]
    assert svc.http_base() == "http://192.168.1.10:6186"


@pytest.mark.asyncio
@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
async def test_explicit_wildcard_publish_address_fails_closed(monkeypatch, wildcard):
    """把通配地址显式填进 download_public_host：意图是对外发布，我们无法替它猜
    一个地址，只能不启动，而不是发一条对谁都不可用的链接。"""
    binds = _capture_binds(monkeypatch)
    svc = _svc(download_server_host="127.0.0.1", download_public_host=wildcard)
    await svc.start()
    assert svc.enabled is False
    assert binds == [], "通配发布地址下不该有任何绑定"


@pytest.mark.asyncio
@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
async def test_wildcard_bind_without_a_publish_host_keeps_working(monkeypatch, wildcard):
    """旧配置（只把 download_server_host 填成通配、没有新键）不得被上面那条误伤：
    那是本机自用一直能用的部署。发布面回落到回环并提示如何放行外部客户端。"""
    binds = _capture_binds(monkeypatch)
    svc = _svc(download_server_host=wildcard)
    await svc.start()
    assert svc.enabled is True
    assert binds == [(wildcard, 6186)], "绑的仍是配置里的通配地址"
    assert svc.http_base() == "http://127.0.0.1:6186"


@pytest.mark.asyncio
async def test_wildcard_bind_with_a_concrete_publish_address_starts(monkeypatch):
    """对照组：被拒的是"发布 0.0.0.0"，不是"绑定 0.0.0.0"——后者是合法部署。"""
    binds = _capture_binds(monkeypatch)
    svc = _svc(download_server_host="0.0.0.0", download_public_host="192.168.1.10")
    await svc.start()
    assert svc.enabled is True
    assert binds == [("0.0.0.0", 6186)]


def test_trusted_origins_cover_both_addresses():
    """SSRF 信任集：取件拨的是发布地址，只放行监听地址会拦下自家转存。"""
    cfg = {
        "download_server_enabled": True,
        "download_server_host": "127.0.0.1",
        "download_public_host": "astrbot",
        "download_http_port": 6186,
    }
    origins = download_endpoint_origins(cfg)
    assert ("127.0.0.1", 6186) in origins
    assert ("astrbot", 6186) in origins


def test_trusted_origins_ignore_an_empty_public_host():
    """留空回落时不要往信任集里塞一个空主机名。"""
    origins = download_endpoint_origins({
        "download_server_enabled": True,
        "download_server_host": "127.0.0.1",
        "download_public_host": "",
        "download_http_port": 6186,
    })
    assert origins == {("127.0.0.1", 6186)}
