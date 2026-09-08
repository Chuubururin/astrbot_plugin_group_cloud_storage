"""Security tests for the unified units module and its consumers.

Covers four threat classes identified in the code review:
1. ReDoS — parsers must reject adversarial inputs in bounded (linear) time.
   Before the linear-time rewrite, 10k-char inputs took 6.7–11.3s per call;
   these tests fail long before that (1s budget per parse).
2. Malformed-input matrix — bool / NaN / inf / oversized strings / oversized
   values / SQL-and-shell-looking payloads must be rejected or neutralized,
   never crash the process or smuggle through into bytes/seconds.
3. Format-output safety — format_* outputs are interpolated into innerHTML
   (pages/storage-ng) and bot messages; they must stay free of HTML
   metacharacters and quotes for every input class.
4. Config-key DoS — untrusted unit strings in config dicts cannot stall
   config validation or model construction.

Run: pytest tests/security/test_units_security.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.units import (  # noqa: E402
    format_duration,
    format_rate,
    format_size,
    parse_duration,
    parse_rate,
    parse_size,
)

MB = 1000 * 1000


# ---------------------------------------------------------------------------
# 1. ReDoS: adversarial inputs must parse or reject in bounded time.
#    The old combined regexes backtracked catastrophically on failure paths
#    (6.7s / 7.0s / 11.3s measured on these exact payloads at 10k chars).
# ---------------------------------------------------------------------------

REDOS_PAYLOADS = [
    # digit runs that never form a valid unit tail
    "9" * 10_000,
    "9" * 10_000 + "!",
    "9." * 5_000,
    # near-miss unit suffixes that used to force full backtracking
    "1" * 9_999 + "G",
    "1" * 9_998 + "MB",
    "5" * 9_999 + "B",
    "1" * 5_000 + "时" + "0" * 4_999 + "秒",
    "0" * 9_999 + "分",
    # mixed separators / whitespace near-misses
    " " * 5_000 + "1GB",
    "1GB" + " " * 5_000,
    ("1.5GB " * 2_000).strip(),
    # parenthesized / bracket bombs that old alternations choked on
    ("(" * 5_000) + "1GB" + (")" * 5_000),
    ("9" * 100 + "." + "9" * 9_900),
]

PARSE_FUNCS = [parse_size, parse_duration, parse_rate]


def _timed(fn, arg):
    t0 = time.perf_counter()
    try:
        result = fn(arg)
    except ValueError:
        result = "raised"
    return time.perf_counter() - t0, result


class TestRedosLinearTime:
    """Every parser must handle every adversarial payload in < 1s (linear).

    The linear rewrite made the worst case ~0.5ms; the 1s budget leaves a
    3-orders-of-magnitude margin so CI noise cannot cause flakes.
    """

    @pytest.mark.parametrize("fn", PARSE_FUNCS, ids=lambda f: f.__name__)
    @pytest.mark.parametrize("payload", REDOS_PAYLOADS, ids=lambda p: f"len{len(p)}")
    def test_bounded_time(self, fn, payload):
        elapsed, _ = _timed(fn, payload)
        assert elapsed < 1.0, (
            f"{fn.__name__} took {elapsed:.2f}s on a {len(payload)}-char "
            "payload — catastrophic backtracking regression?"
        )

    @pytest.mark.parametrize("fn", PARSE_FUNCS, ids=lambda f: f.__name__)
    def test_length_cap_rejects_immediately(self, fn):
        # >64 chars must hit the length guard, not the parser proper.
        for n in (65, 128, 1_000, 100_000):
            elapsed, _ = _timed(fn, "1" * n)
            assert elapsed < 0.05, f"{fn.__name__} not capped at {n} chars"

    def test_repeated_calls_scale_linearly(self):
        # 1000 calls on a 63-char worst-case-ish string: if parsing were
        # super-linear this would visibly degrade.
        probe = "1" * 60 + "G"
        t0 = time.perf_counter()
        for _ in range(1000):
            with pytest.raises(ValueError):
                parse_size(probe)
        assert time.perf_counter() - t0 < 2.0


# ---------------------------------------------------------------------------
# 2. Malformed-input matrix: reject cleanly, never crash, never smuggle.
# ---------------------------------------------------------------------------

class TestMalformedInputMatrix:
    """Bool / NaN / inf / overflow / injection-shaped inputs."""

    @pytest.mark.parametrize("fn", PARSE_FUNCS, ids=lambda f: f.__name__)
    def test_bool_rejected(self, fn):
        # bool is an int subclass; True/False must not parse as 1 MB / 1 s.
        for bad in (True, False):
            with pytest.raises(ValueError):
                fn(bad)

    @pytest.mark.parametrize("fn", PARSE_FUNCS, ids=lambda f: f.__name__)
    def test_nan_inf_rejected(self, fn):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                fn(bad)

    @pytest.mark.parametrize("fn", PARSE_FUNCS, ids=lambda f: f.__name__)
    def test_oversized_string_rejected(self, fn):
        for bad in ("x" * 65, "x" * 100_000):
            with pytest.raises(ValueError):
                fn(bad)

    @pytest.mark.parametrize("fn", PARSE_FUNCS, ids=lambda f: f.__name__)
    def test_value_cap(self, fn):
        # 10^9 is the max magnitude; 10^9+1 (in any unit) must be rejected.
        with pytest.raises(ValueError):
            parse_size("1000000001")
        with pytest.raises(ValueError):
            parse_duration("1000000001秒")
        with pytest.raises(ValueError):
            parse_rate("1000000001")

    def test_negative_numbers_rejected(self):
        for bad in ("-5", "-5GB", "-90秒", "-2MB/s"):
            with pytest.raises(ValueError):
                parse_size(bad)
            with pytest.raises(ValueError):
                parse_duration(bad)
            with pytest.raises(ValueError):
                parse_rate(bad)

    def test_negative_numeric_inputs_clamped(self):
        # Numeric path: negatives clamp to 0 across all three parsers
        # (negative sizes/durations/rates would bypass `or`-style fallbacks
        # and semantic checks at call sites).
        assert parse_size(-3) == 0
        assert parse_duration(-5) == 0.0
        assert parse_rate(-1) == 0.0

    def test_exponent_and_special_floats_rejected(self):
        for bad in ("1e5", "1E5", "0x10", "1_000", "Infinity", "NaN"):
            for fn in PARSE_FUNCS:
                with pytest.raises(ValueError):
                    fn(bad)

    def test_truncated_error_message(self):
        # Error text embeds at most 32 chars of the input (no log flooding).
        with pytest.raises(ValueError) as ei:
            parse_size("A" * 10_000)
        msg = str(ei.value)
        assert "AAAA" in msg
        assert len(msg) < 200

    def test_injection_shaped_payloads_rejected(self):
        # SQL / template / shell-shaped strings must be inert: rejected.
        payloads = [
            "1GB'; DROP TABLE files;--",
            "1GB'; DROP TABLE files;--",
            "'; SELECT 1;--",
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "javascript:alert(1)",
            "${1GB}",
            "{{1GB}}",
            "`id`",
            "$(rm -rf /)",
            "1GB\r\nSet-Cookie: x=1",
            "1GB\n2TB",
            "\x00\x01\x02",
        ]
        for p in payloads:
            for fn in PARSE_FUNCS:
                with pytest.raises(ValueError):
                    fn(p)

    def test_unicode_confusables_rejected(self):
        # Fullwidth digits / exotic unicode must not parse as numbers.
        for bad in ("１GB", "１ＧＢ", "½GB", "١٠"):
            for fn in PARSE_FUNCS:
                with pytest.raises(ValueError):
                    fn(bad)


class TestFormatOutputSafety:
    """format_* outputs reach innerHTML and bot messages; verify purity.

    The front-end copies (pages/storage-ng/utils/helpers.js) interpolate the
    same fields; these tests pin the backend contract those consumers rely on.
    """

    HOSTILE_INPUTS = [
        0, 1, -1, 500, 1.5 * MB, 2 * 10**12, 10**18, 10**30,
        float("inf"), float("-inf"), float("nan"),
        None, True, False, "1GB", "<script>", [], {}, object(),
    ]

    @pytest.mark.parametrize("bad", HOSTILE_INPUTS)
    def test_format_size_safe_chars(self, bad):
        out = format_size(bad)
        self._assert_pure(out, "MB", "GB", "TB")

    @pytest.mark.parametrize("bad", HOSTILE_INPUTS)
    def test_format_rate_safe_chars(self, bad):
        out = format_rate(bad)
        self._assert_pure(out, "MB/s", "GB/s")

    @pytest.mark.parametrize("bad", HOSTILE_INPUTS)
    def test_format_duration_safe_chars(self, bad):
        out = format_duration(bad)
        self._assert_pure(out, "秒", "分", "时")

    def _assert_pure(self, out, *allowed_units):
        # Only digits, dot, space and the allowed unit glyphs may appear.
        charset = set("0123456789. -") | set("".join(allowed_units))
        assert set(out) <= charset, f"unexpected chars {set(out) - charset!r} in {out!r}"
        for meta in ("<", ">", "&", '"', "'", "`", "\\", "{", "}", "$", ";"):
            assert meta not in out, f"metachar {meta!r} leaked into {out!r}"


# ---------------------------------------------------------------------------
# 4. Config-key DoS: bad unit strings in config dicts must not stall or crash
#    model construction / schema validation.
# ---------------------------------------------------------------------------

class TestConfigKeyDoS:
    """Adversarial strings placed into the string-unit config keys."""

    ADVERSARIAL_VALUES = REDOS_PAYLOADS[:6] + [
        "'; DROP TABLE config;--",
        "<script>alert(1)</script>",
        "9" * 63,
    ]

    @pytest.mark.parametrize("val", ADVERSARIAL_VALUES, ids=lambda v: f"len{len(v)}")
    def test_config_model_survives(self, val):
        from core.config.model import PluginConfig

        cfg = PluginConfig({
            "volume_threshold": val,
            "fetch_max_size": val,
            "bridge_min_size": val,
            "bridge_max_size": val,
        })
        # Unparseable strings fall through to legacy/default paths.
        assert isinstance(cfg.volume_threshold_bytes, int)
        assert isinstance(cfg.fetch_max_bytes, int)
        assert isinstance(cfg.bridge_min_bytes, int)
        assert isinstance(cfg.bridge_max_bytes, int)
        assert cfg.volume_threshold_bytes >= 0
        assert cfg.fetch_max_bytes >= 0

    @pytest.mark.parametrize("val", ADVERSARIAL_VALUES, ids=lambda v: f"len{len(v)}")
    def test_schema_validation_bounded(self, val):
        from core.config.schema import validate_config

        t0 = time.perf_counter()
        warnings = dict(validate_config({
            "volume_threshold": val,
            "fetch_max_size": val,
        }))
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"validate_config took {elapsed:.2f}s on {val[:20]!r}"
        for _key, msg in warnings.items():
            assert isinstance(msg, str)
            assert len(msg) < 500
