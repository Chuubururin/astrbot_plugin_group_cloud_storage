"""回归：SSRF 受限地址判定必须解开 IPv6 隧道/兼容形态内嵌的 IPv4。

攻击面：6to4（2002:<私网IPv4>）、Teredo（2001:0000:<...><私网IPv4取反>）、
deprecated IPv4-compatible（::<私网IPv4>）在字面上都是"全球单播"，不解包
就能把 http://[2002:7f00:1]/ 这类地址洗成看起来合法的公网目标。
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.external.base import _is_restricted_ip  # noqa: E402


@pytest.mark.parametrize("text", [
    "127.1.2.3",
    "10.0.0.1", "192.168.9.9", "169.254.0.1", "172.16.0.1", "0.0.0.0",
    "::1", "::ffff:127.0.0.1", "fc00::abcd", "fd12:3456::1",
    "::127.0.0.1",                      # IPv4-compatible ::/96
    "2002:7f00:1::",                    # 6to4 -> 127.0.0.1
    "2002:0a00:1::",                    # 6to4 -> 10.0.0.1
    "2001:0000:0:0:0:0:80ff:fffe",      # Teredo peer -> 127.0.0.1
])
def test_restricted(text):
    assert _is_restricted_ip(ipaddress.ip_address(text)), f"{text} 应判受限"


@pytest.mark.parametrize("text", [
    "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111",
    "2002:0808:808::",                  # 6to4 -> 8.8.8.8（公网，放行）
    "2001:db8:1::5",                    # 文档段——非私网，不在拦截语义内
])
def test_allowed(text):
    assert not _is_restricted_ip(ipaddress.ip_address(text)), f"{text} 不应误伤"
