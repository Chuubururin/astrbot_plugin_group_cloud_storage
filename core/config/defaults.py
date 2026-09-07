"""Configuration defaults — default values for all config keys.

Add or remove config entries in this file; the schema picks them up
automatically.
"""

DEFAULTS: dict = {
    "managed_groups": [],
    "global_admin_qqs": [],
    "request_interval_ms": 1000,
    "max_concurrent_scans": 8,
    "auto_index_upload_event": True,
    "auto_scan_interval_hours": 6,
    # Group info TTL (scan_schedule): due groups rescan so capacity/album/essence counts stay fresh
    "group_info_ttl_hours": 24,
    "auto_label": True,
    "page_size": 10,
    "essence_chunk_size": 4000,
    "video_segment_seconds": 599,
    "volume_threshold_mb": 95,
    "fetch_max_bytes": 2147483648,
    "fetch_timeout_sec": 180,
    # Fetch pipeline SSRF gate: deny loopback/private/reserved addresses by
    # default (same semantics as openlist_allow_private_address)
    "fetch_allow_private_address": False,
    "download_server_enabled": False,
    "download_server_host": "127.0.0.1",
    "download_http_port": 6186,
    "download_ftp_port": 0,
    "download_smb_port": 0,
    "download_token": "",
    "op_high_priority_kinds": [],
    "database_admin_token": "",
    # OpenList bridge configuration 
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
    # Configurable classification and preview: data-driven default tables + config overrides
    "type_ext_overrides": {},
    "preview_policy": {},
}


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
