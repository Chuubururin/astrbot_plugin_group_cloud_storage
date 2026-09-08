"""PluginConfig — type-safe wrapper over plugin configuration (no third-party deps).

- get(): strict dict.get passthrough semantics (call sites keep their own
  defaults)
- typed properties: schema defaults as fallback + type conversion, falling
  back to the default on conversion failure
- validate(): startup warnings (unknown keys / conversion failures), never
  blocks execution
"""

from __future__ import annotations

from ..units import parse_size
from .defaults import DEFAULTS, _to_bool
from .schema import validate_config


class PluginConfig:
    """Wraps the plugin config dict; get() follows dict.get semantics."""

    def __init__(self, data: dict | "PluginConfig" | None = None):
        # Idempotent construction: reuse the inner dict of an already-wrapped
        # instance, avoiding dict(x) subscript access via the sequence protocol
        if isinstance(data, PluginConfig):
            self._data = dict(data._data)
        else:
            self._data = dict(data or {})

    @property
    def raw(self) -> dict:
        return dict(self._data)

    def get(self, key: str, default=None):
        """dict.get semantics (missing keys return the call-site default, not schema default)."""
        return self._data.get(key, default)

    def __getitem__(self, key: str):
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def set(self, key: str, value) -> None:
        """Write config (like dict[key]=v; used by the Page config center for runtime updates)."""
        self._data[key] = value

    def update(self, updates: dict) -> None:
        """dict.update semantics."""
        self._data.update(updates)

    def validate(self) -> list[tuple[str, str]]:
        """Returns [(key, message)] warnings; one each for unknown keys and conversion failures."""
        return validate_config(self._data)

    # ---- typed properties (schema defaults as fallback) ----

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
    def request_interval(self) -> float:
        """Base QQ API interval in seconds: the second-unit key wins; the
        legacy millisecond key applies when the new key is unset.
        """
        value = self._data.get("request_interval")
        if value is not None:
            try:
                v = float(value)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        return self.request_interval_ms / 1000.0

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
    def volume_threshold_mb(self) -> int:
        return self._as("volume_threshold_mb", int, DEFAULTS["volume_threshold_mb"])

    # ---- string-unit size keys (base 1000; legacy byte keys as fallback) ----

    def _size_property(
        self,
        key: str,
        legacy_key: str,
        default_bytes: int,
        *,
        legacy_unit: str = "bytes",
    ) -> int:
        """Resolve a size config to bytes.

        Priority: the new string-unit key ("95MB"/"2GB", base 1000; a bare
        number means MB) > the legacy key > the schema default. Unparseable
        strings fall through to the legacy/default path (a validation warning
        already surfaced at startup). ``legacy_unit`` distinguishes the two
        legacy key families: raw bytes (fetch/bridge) vs MB counts
        (volume_threshold_mb).
        """
        value = self._data.get(key)
        if value is not None and str(value).strip() not in ("",):
            try:
                return parse_size(value)
            except ValueError:
                pass
        # Legacy key: raw bytes, or an MB count for volume_threshold_mb
        legacy = self._data.get(legacy_key)
        if legacy is not None:
            try:
                n = max(int(legacy), 0)
                return n * 1024 * 1024 if legacy_unit == "mb" else n
            except (TypeError, ValueError):
                pass
        return default_bytes

    @property
    def volume_threshold_bytes(self) -> int:
        default = parse_size(DEFAULTS["volume_threshold"])
        return self._size_property(
            "volume_threshold", "volume_threshold_mb", default, legacy_unit="mb"
        )

    @property
    def fetch_max_bytes(self) -> int:
        return self._size_property(
            "fetch_max_size", "fetch_max_bytes", DEFAULTS["fetch_max_bytes"]
        )

    @property
    def bridge_min_bytes(self) -> int:
        return self._size_property(
            "bridge_min_size", "bridge_min_bytes", DEFAULTS["bridge_min_bytes"]
        )

    @property
    def bridge_max_bytes(self) -> int:
        return self._size_property(
            "bridge_max_size", "bridge_max_bytes", DEFAULTS["bridge_max_bytes"]
        )

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
    def download_sftp_port(self) -> int:
        return self._as("download_sftp_port", int, DEFAULTS["download_sftp_port"])

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
