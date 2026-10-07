"""Configuration schema — validation logic.

validate_config() warns at startup (unknown keys / conversion failures) and
never blocks execution.
"""

from __future__ import annotations

import ipaddress
import re

from ..units import parse_size
from .defaults import DEFAULTS, _to_bool

# "host:port" typed into download_server_host (the port keys exist for that):
# a trailing ":digits" that is not part of a valid IP literal (bare IPv6
# addresses like ::1 or 2001:db8::1 are legitimate and unflagged).
_HOST_PORT_RE = re.compile(r":\d+$")


def _suspect_host(host: str) -> bool:
    if any(c.isspace() for c in host):
        return True
    if ":" not in host or not _HOST_PORT_RE.search(host):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True  # not a bare IP literal -> it really is host:port
    return False


def validate_config(data: dict) -> list[tuple[str, str]]:
    """Returns [(key, message)] warnings; one each for unknown keys, conversion
    failures, and semantic misconfiguration."""
    warnings: list[tuple[str, str]] = []
    for key in data:
        if key not in DEFAULTS:
            warnings.append((key, "未知配置键（schema 中不存在）"))
    for key in ("managed_groups", "global_admin_qqs", "op_high_priority_kinds"):
        if key in data and not isinstance(data[key], list):
            warnings.append(
                (key, f"期望 list，实际 {type(data[key]).__name__}")
            )
    # Semantic: download server enabled without token → auth bypass
    enabled = data.get("download_server_enabled", False)
    token = str(data.get("download_token", "") or "")
    if enabled and not token:
        warnings.append(
            (
                "download_token",
                "download_server_enabled=true 但 download_token 为空——"
                "下载服务将以无认证模式运行（不安全）。"
                "已启用 fail-closed 保护：服务实际不会启动。"
                "请设置 download_token 后重载。",
            )
        )
    # Semantic: bridge enabled without a base URL -> the whole OpenList
    # subset (netdisk/bridge/离线下载) silently degrades to "not configured"
    if _to_bool(data.get("openlist_enabled", False)) and not str(
        data.get("openlist_base_url", "") or ""
    ).strip():
        warnings.append(
            (
                "openlist_base_url",
                "openlist_enabled=true 但 openlist_base_url 为空——"
                "网盘/桥接/离线下载子集将不可用。",
            )
        )
    # Semantic: the bind host feeds download_url()/http_base() verbatim, so
    # whitespace or a host:port value produces broken download addresses
    # ("http://h:6186:6186/..."). Advisory only; bare IPv6 literals pass.
    host = str(data.get("download_server_host", "") or "")
    if host and _suspect_host(host):
        warnings.append(
            (
                "download_server_host",
                f"格式可疑：{host!r}——应为 IP 或主机名，端口请用 "
                "download_http_port / download_sftp_port 配置。",
            )
        )
    # Semantic: database admin token empty → admin endpoints stay closed
    db_token = str(data.get("database_admin_token", "") or "")
    if not db_token:
        warnings.append(
            (
                "database_admin_token",
                "database_admin_token 为空——数据库管理接口已关闭（fail-closed）。"
                "需要备份/恢复/重置数据库时请设置令牌后重载。",
            )
        )
    # Semantic: string-unit size keys must parse ("95MB", "2GB", base 1000);
    # bare numbers are read as MB. Unparseable values warn (never block).
    for key, _floor_hint in (
        ("volume_threshold", "10MB"),
        ("fetch_max_size", "1MB"),
        ("bridge_min_size", None),
        ("bridge_max_size", None),
    ):
        if key not in data:
            continue
        value = data[key]
        try:
            parse_size(value)
        except ValueError:
            # Truncate: the value is user-supplied and unbounded in length.
            shown = str(value)[:40]
            warnings.append(
                (
                    key,
                    f"大小格式无法识别（{shown!r}），支持 MB/GB/TB 如 500MB、1.5GB；"
                    "已回退为默认值",
                )
            )
    # Semantic: volume_threshold_mb must be a positive integer >= 10
    vtb = data.get("volume_threshold_mb")
    if vtb is not None:
        try:
            vtb_int = int(vtb)
            if vtb_int < 10:
                warnings.append(
                    ("volume_threshold_mb", f"阈值过小（{vtb_int}MB），已回退为默认 95MB")
                )
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(float("inf")) — validate_config must warn, not
            # raise (it is called unconditionally before assembly).
            warnings.append(
                ("volume_threshold_mb", f"期望 int，实际 {type(vtb).__name__}，已回退为默认 95MB")
            )
    return warnings
