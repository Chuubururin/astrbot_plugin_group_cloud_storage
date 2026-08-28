"""PluginConfig —— 配置的类型安全包装（零第三方依赖）。

- get()：严格 dict 透传语义（保留各调用点的既有默认值，行为不变）
- typed properties：读 schema 默认值兜底 + 类型转换，转换失败回默认
- validate()：启动期告警（未知键/转换失败），不阻断运行
"""

from __future__ import annotations

DEFAULTS: dict = {
    "managed_groups": [],
    "global_admin_qqs": [],
    "request_interval_ms": 1000,
    "auto_index_upload_event": True,
    "auto_scan_interval_hours": 6,
    "auto_label": True,
    "page_size": 10,
    "essence_chunk_size": 4000,
    "video_segment_seconds": 599,
    "fetch_max_bytes": 2147483648,
    "fetch_timeout_sec": 180,
    "volume_compress_enabled": False,
    "volume_checksum_enabled": False,
    "download_server_enabled": False,
    "download_server_host": "127.0.0.1",
    "download_http_port": 6186,
    "download_ftp_port": 0,
    "download_token": "",
    "op_high_priority_kinds": [],
    # OpenList bridge configuration (REQ-05/11/18)
    "openlist_enabled": False,
    "openlist_base_url": "",
    "openlist_username": "",
    "openlist_password": "",
    "openlist_token": "",
    "openlist_dst_dir": "/",
    "openlist_dst_dir_template": "{group_id}/{filename}",
    "openlist_timeout_sec": 30.0,
    "openlist_allow_private_address": False,
    "openlist_poll_interval_sec": 0,
    "bridge_min_bytes": 0,
    "bridge_max_bytes": 0,
    # CT-9 分类与预览可配置（N6）：数据驱动默认表 + 配置覆盖
    "type_ext_overrides": {},
    "preview_policy": {},
}


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class PluginConfig:
    """包装插件配置 dict；get 与现有 config.get 调用完全兼容。"""

    def __init__(self, data: dict | "PluginConfig" | None = None):
        # 幂等构造：已包装实例直接复用内部 dict，避免 dict(x) 走序列协议下标访问
        if isinstance(data, PluginConfig):
            self._data = dict(data._data)
        else:
            self._data = dict(data or {})

    @property
    def raw(self) -> dict:
        return dict(self._data)

    def get(self, key: str, default=None):
        """dict.get 语义（键缺失时返回调用点 default，不用 schema 默认值）。"""
        return self._data.get(key, default)

    def __getitem__(self, key: str):
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def set(self, key: str, value) -> None:
        """写入配置（等价 dict[key]=v；用于 Page 配置中心运行时更新）。"""
        self._data[key] = value

    def set_many(self, updates: dict) -> None:
        """批量写入配置。"""
        self._data.update(updates)

    def update(self, updates: dict) -> None:
        """dict.update 语义（兼容既有调用点）。"""
        self._data.update(updates)

    def validate(self) -> list[tuple[str, str]]:
        """返回 [(key, message)] 告警；未知键与类型转换失败各一条。"""
        warnings: list[tuple[str, str]] = []
        for key in self._data:
            if key not in DEFAULTS:
                warnings.append((key, "未知配置键（schema 中不存在）"))
        for key in ("managed_groups", "global_admin_qqs", "op_high_priority_kinds"):
            if key in self._data and not isinstance(self._data[key], list):
                warnings.append(
                    (key, f"期望 list，实际 {type(self._data[key]).__name__}")
                )
        return warnings

    # ---- typed properties（schema 默认值兜底） ----

    def _as(self, key: str, cast, default):
        value = self._data.get(key, default)
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    @property
    def managed_groups(self) -> list[str]:
        value = self._data.get("managed_groups", DEFAULTS["managed_groups"])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def global_admin_qqs(self) -> list[str]:
        value = self._data.get("global_admin_qqs", DEFAULTS["global_admin_qqs"])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def op_high_priority_kinds(self) -> list[str]:
        value = self._data.get(
            "op_high_priority_kinds", DEFAULTS["op_high_priority_kinds"]
        )
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def request_interval_ms(self) -> int:
        return self._as("request_interval_ms", int, DEFAULTS["request_interval_ms"])

    @property
    def auto_index_upload_event(self) -> bool:
        value = self._data.get(
            "auto_index_upload_event", DEFAULTS["auto_index_upload_event"]
        )
        return _to_bool(value) if not isinstance(value, bool) else value

    @property
    def auto_scan_interval_hours(self) -> float:
        return self._as(
            "auto_scan_interval_hours", float, DEFAULTS["auto_scan_interval_hours"]
        )

    @property
    def auto_label(self) -> bool:
        value = self._data.get("auto_label", DEFAULTS["auto_label"])
        return _to_bool(value) if not isinstance(value, bool) else value

    @property
    def page_size(self) -> int:
        return self._as("page_size", int, DEFAULTS["page_size"])

    @property
    def essence_chunk_size(self) -> int:
        return self._as("essence_chunk_size", int, DEFAULTS["essence_chunk_size"])

    @property
    def video_segment_seconds(self) -> int:
        return self._as("video_segment_seconds", int, DEFAULTS["video_segment_seconds"])

    @property
    def fetch_max_bytes(self) -> int:
        return self._as("fetch_max_bytes", int, DEFAULTS["fetch_max_bytes"])

    @property
    def fetch_timeout_sec(self) -> int:
        return self._as("fetch_timeout_sec", int, DEFAULTS["fetch_timeout_sec"])

    @property
    def download_server_enabled(self) -> bool:
        value = self._data.get(
            "download_server_enabled", DEFAULTS["download_server_enabled"]
        )
        return _to_bool(value) if not isinstance(value, bool) else value

    @property
    def download_server_host(self) -> str:
        return self._as("download_server_host", str, DEFAULTS["download_server_host"])

    @property
    def download_http_port(self) -> int:
        return self._as("download_http_port", int, DEFAULTS["download_http_port"])

    @property
    def download_ftp_port(self) -> int:
        return self._as("download_ftp_port", int, DEFAULTS["download_ftp_port"])

    @property
    def download_token(self) -> str:
        return self._as("download_token", str, DEFAULTS["download_token"])

    # ---- OpenList bridge configuration ----

    @property
    def openlist_enabled(self) -> bool:
        value = self._data.get("openlist_enabled", DEFAULTS["openlist_enabled"])
        return _to_bool(value) if not isinstance(value, bool) else value

    @property
    def openlist_base_url(self) -> str:
        return self._as("openlist_base_url", str, DEFAULTS["openlist_base_url"])

    @property
    def openlist_username(self) -> str:
        return self._as("openlist_username", str, DEFAULTS["openlist_username"])

    @property
    def openlist_password(self) -> str:
        return self._as("openlist_password", str, DEFAULTS["openlist_password"])

    @property
    def openlist_token(self) -> str:
        return self._as("openlist_token", str, DEFAULTS["openlist_token"])

    @property
    def openlist_dst_dir(self) -> str:
        return self._as("openlist_dst_dir", str, DEFAULTS["openlist_dst_dir"])

    @property
    def openlist_dst_dir_template(self) -> str:
        return self._as(
            "openlist_dst_dir_template", str, DEFAULTS["openlist_dst_dir_template"]
        )

    @property
    def openlist_timeout_sec(self) -> float:
        return self._as("openlist_timeout_sec", float, DEFAULTS["openlist_timeout_sec"])

    @property
    def openlist_allow_private_address(self) -> bool:
        value = self._data.get(
            "openlist_allow_private_address", DEFAULTS["openlist_allow_private_address"]
        )
        return _to_bool(value) if not isinstance(value, bool) else value

    @property
    def openlist_poll_interval_sec(self) -> int:
        return self._as(
            "openlist_poll_interval_sec", int, DEFAULTS["openlist_poll_interval_sec"]
        )

    @property
    def bridge_min_bytes(self) -> int:
        return self._as("bridge_min_bytes", int, DEFAULTS["bridge_min_bytes"])

    @property
    def bridge_max_bytes(self) -> int:
        return self._as("bridge_max_bytes", int, DEFAULTS["bridge_max_bytes"])

    @property
    def volume_compress_enabled(self) -> bool:
        value = self._data.get("volume_compress_enabled", DEFAULTS["volume_compress_enabled"])
        return _to_bool(value) if not isinstance(value, bool) else value

    @property
    def volume_checksum_enabled(self) -> bool:
        value = self._data.get("volume_checksum_enabled", DEFAULTS["volume_checksum_enabled"])
        return _to_bool(value) if not isinstance(value, bool) else value
