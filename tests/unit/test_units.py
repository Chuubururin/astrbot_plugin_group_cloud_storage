"""Unified unit parsing/formatting tests (core/units.py) and the config
string-unit keys built on top of them.

Run: pytest tests/unit/test_units.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config.model import PluginConfig  # noqa: E402
from core.config.schema import validate_config  # noqa: E402
from core.units import (  # noqa: E402
    format_duration,
    format_rate,
    format_size,
    parse_duration,
    parse_rate,
    parse_size,
)

KB = 1000
MB = KB * 1000
GB = MB * 1000
TB = GB * 1000


class TestParseSize:
    """Storage parsing: base 1000, units MB/GB/TB, bare number = MB."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("500MB", 500 * MB),
            ("500 MB", 500 * MB),
            ("500mb", 500 * MB),
            ("1.5GB", int(1.5 * GB)),
            ("2 TB", 2 * TB),
            ("95", 95 * MB),  # bare number means MB
            ("0", 0),
            ("", 0),
            (None, 0),
            (300, 300 * MB),  # numeric input treated as MB
        ],
    )
    def test_parse(self, text, expected):
        assert parse_size(text) == expected

    @pytest.mark.parametrize("bad", ["10kb", "1KB", "abc", "5B", "1.2.3GB", "90秒"])
    def test_rejects_small_or_unknown_units(self, bad):
        with pytest.raises(ValueError):
            parse_size(bad)


class TestFormatSize:
    """Storage formatting: base 1000, floor at MB — no byte/KB output."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0 MB"),
            (None, "0 MB"),
            (500, "0.1 MB"),  # sub-MB rounds up to the MB floor
            (1.4 * MB, "1.4 MB"),
            (1.5 * MB, "1.5 MB"),
            (5 * GB, "5.00 GB"),
            (6 * TB, "6.00 TB"),
        ],
    )
    def test_format(self, n, expected):
        assert format_size(n) == expected

    @pytest.mark.parametrize(
        "n", [1, 512, KB, 999 * KB, 1024, 1024 * 1024]
    )
    def test_never_shows_byte_or_kb(self, n):
        out = format_size(n)
        assert "B" not in out.replace("MB", "")
        assert "KB" not in out


class TestDuration:
    """Durations: 时/分/秒 only; ms is not a parseable unit anywhere."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("90", 90.0), ("90秒", 90.0), ("2分", 120.0), ("1.5时", 5400.0),
         ("2时30分", 9000.0), (45, 45.0), ("", 0.0), (None, 0.0)],
    )
    def test_parse(self, text, expected):
        assert parse_duration(text) == expected

    def test_rejects_ms(self):
        with pytest.raises(ValueError):
            parse_duration("500ms")

    @pytest.mark.parametrize(
        ("sec", "expected"),
        [(45, "45秒"), (125, "2分05秒"), (60, "1分"), (3725, "1时02分05秒"), (0, "0秒")],
    )
    def test_format(self, sec, expected):
        assert format_duration(sec) == expected


class TestRate:
    """Bandwidth: binary base 1024; MB/s floor, GB/s for large rates."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("10 MB/s", 10 * 1024 * 1024),
            ("10mbps", 10 * 1024 * 1024),
            ("1.5 GB/s", 1.5 * 1024**3),
            ("4", 4 * 1024 * 1024),  # bare number means MB/s
            ("", 0.0),
        ],
    )
    def test_parse(self, text, expected):
        assert parse_rate(text) == pytest.approx(expected)

    def test_parse_rejects_kbps(self):
        with pytest.raises(ValueError):
            parse_rate("512 KB/s")

    @pytest.mark.parametrize(
        ("bps", "expected"),
        [
            (0, "0 MB/s"),
            (512 * 1024, "0.5 MB/s"),
            (2 * 1024 * 1024, "2.0 MB/s"),
            (1.5 * 1024**3, "1.50 GB/s"),
        ],
    )
    def test_format(self, bps, expected):
        assert format_rate(bps) == expected

    def test_format_never_shows_kbps(self):
        assert "KB/s" not in format_rate(100)


class TestConfigStringUnits:
    """Config resolution: string-unit keys win; legacy keys still work."""

    def test_new_string_keys(self):
        cfg = PluginConfig({
            "fetch_max_size": "500MB",
            "volume_threshold": "1GB",
            "bridge_min_size": "10MB",
            "bridge_max_size": "2GB",
        })
        assert cfg.fetch_max_bytes == 500 * MB
        assert cfg.volume_threshold_bytes == GB
        assert cfg.bridge_min_bytes == 10 * MB
        assert cfg.bridge_max_bytes == 2 * GB

    def test_legacy_byte_keys_fallback(self):
        cfg = PluginConfig({
            "fetch_max_bytes": 10 * 1024 * 1024,
            "bridge_min_bytes": 5 * MB,
            "bridge_max_bytes": GB,
        })
        assert cfg.fetch_max_bytes == 10 * 1024 * 1024
        assert cfg.bridge_min_bytes == 5 * MB
        assert cfg.bridge_max_bytes == GB

    def test_legacy_volume_threshold_mb_is_mb(self):
        cfg = PluginConfig({"volume_threshold_mb": 120})
        assert cfg.volume_threshold_bytes == 120 * 1024 * 1024

    def test_string_key_wins_over_legacy(self):
        cfg = PluginConfig({
            "fetch_max_size": "1GB",
            "fetch_max_bytes": 10 * 1024 * 1024,
        })
        assert cfg.fetch_max_bytes == GB

    def test_defaults(self):
        cfg = PluginConfig({})
        assert cfg.fetch_max_bytes == 2 * 1024**3
        assert cfg.volume_threshold_bytes == 95 * MB
        assert cfg.bridge_min_bytes == 0
        assert cfg.bridge_max_bytes == 0

    def test_bare_number_means_mb(self):
        cfg = PluginConfig({"bridge_min_size": 10})
        assert cfg.bridge_min_bytes == 10 * MB

    def test_unparseable_falls_to_legacy(self):
        cfg = PluginConfig({"fetch_max_size": "abc", "fetch_max_bytes": 2048})
        assert cfg.fetch_max_bytes == 2048

    def test_schema_warns_on_bad_units(self):
        warnings = dict(
            validate_config({"fetch_max_size": "nonsense", "volume_threshold": "5xx"})
        )
        assert "fetch_max_size" in warnings
        assert "volume_threshold" in warnings

    def test_schema_accepts_good_units(self):
        warnings = dict(validate_config({"fetch_max_size": "2GB"}))
        assert "fetch_max_size" not in warnings
