"""Configuration defaults — default values for all config keys.

Add or remove config entries in this file; the schema picks them up
automatically.
"""

DEFAULTS: dict = {
    "managed_groups": [],
    "global_admin_qqs": [],
    # Base interval in seconds between QQ API calls (legacy ms key supported)
    "request_interval": 1.0,
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
    # String-unit sizes (base 1000): users fill "95MB" / "2GB"; the backend
    # parses them into bytes. Legacy numeric keys stay supported as aliases.
    "volume_threshold": "95MB",
    "fetch_max_size": "2GB",
    "bridge_min_size": "0",
    "bridge_max_size": "0",
    # Legacy byte-unit keys (deprecated; kept for backward compatibility)
    "volume_threshold_mb": 95,
    "fetch_max_bytes": 2147483648,
    "fetch_timeout_sec": 180,
    # Fetch pipeline SSRF gate: deny loopback/private/reserved addresses by
    # default (same semantics as openlist_allow_private_address)
    "fetch_allow_private_address": False,
    "download_server_enabled": False,
    "download_server_host": "127.0.0.1",
    # 发布地址（生成直链 / SMB UNC / SFTP 信息里的主机）。留空 = 沿用绑定地址；
    # 只有在"绑 127.0.0.1 但要发给别人 192.168.x.x"这类部署下才需要设。
    "download_public_host": "",
    "download_http_port": 6186,
    "download_sftp_port": 0,
    "download_smb_port": 0,
    "download_token": "",
    # Download-server cache housekeeping (see download_cache.sweep_cache):
    # the mkdtemp root used to grow until the next plugin reload, because
    # shutdown()'s rmtree was the only cleanup. 0 disables either rule.
    "download_cache_max_mb": 1024,
    "download_cache_ttl_hours": 24,
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
    # Legacy byte-unit keys (deprecated; kept for backward compatibility)
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
