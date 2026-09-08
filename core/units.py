"""Unified unit parsing and formatting — the single source of truth for all
measures (sizes, durations, rates) in this plugin.

Rules (project-wide):
- Storage sizes use the decimal base **1000**; display units are only
  TB / GB / MB (byte and KB are never shown).
- Bandwidth / transfer rates use the binary base **1024**; display units are
  only MB/s / GB/s (KB/s and below are never shown).
- Durations display only in 时 / 分 / 秒 (milliseconds are never shown).

Parsing accepts user-facing strings like "500MB", "1.5 GB", "90秒", "2时30分".

Security: values come from user-editable config and webapi payloads, so
parsers must be linear-time (no catastrophic regex backtracking — see the
_NUMBER token + manual unit match instead of one combined regex), length-
capped, and reject non-finite numbers. Errors raise ValueError with a
fixed message that embeds only a truncated repr of the input.
"""

from __future__ import annotations

import math
import re

# Maximum accepted length for any unit string. Real config values are a few
# characters; anything longer is rejected before parsing (DoS guard).
_MAX_UNIT_LEN = 64
# Maximum accepted magnitude: 10^9 TB. Larger values are meaningless for
# group storage and would overflow float→int conversions.
_MAX_PARSE_VALUE = 10**9

# ---------- storage sizes (base 1000; MB/TB/GB only) ----------

_SIZE_BASE = 1000
_SIZE_FACTORS = {
    "tb": _SIZE_BASE**4,
    "gb": _SIZE_BASE**3,
    "mb": _SIZE_BASE**2,
}

# Linear-time token: a plain decimal number (no exponent) — the unit suffix
# is matched separately by bounded string ops, avoiding regex backtracking.
_NUMBER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")


def _err(text, kinds: str) -> ValueError:
    shown = str(text)[:32]
    return ValueError(f"无法识别的{kinds}单位: {shown!r}（格式如 500MB、1.5GB）")


def _parse_number_unit(text, valid_units: dict, bare_multiplier: float, kinds: str):
    """Shared linear-time parser: decimal number + unit token.

    Returns the numeric value already multiplied out; raises ValueError on
    any malformed input. ``valid_units`` maps lowercase unit tokens to their
    multipliers; ``bare_multiplier`` applies when no unit suffix is present.
    """
    if text is None:
        return None
    if isinstance(text, bool):
        raise _err(text, kinds)
    if isinstance(text, (int, float)):
        # Numeric config input means the bare unit; NaN/inf are rejected.
        v = float(text)
        if not math.isfinite(v):
            raise _err(text, kinds)
        return v * bare_multiplier
    s = str(text).strip()
    if len(s) > _MAX_UNIT_LEN:
        raise _err(text, kinds)
    if not s:
        return None
    lowered = s.lower()
    # Longest-unit-first match against the tail; then the head is a number.
    multiplier = bare_multiplier
    matched_unit = False
    for unit in sorted(valid_units, key=len, reverse=True):
        if lowered.endswith(unit):
            multiplier = valid_units[unit]
            s_num = lowered[: -len(unit)].strip()
            matched_unit = True
            break
    if not matched_unit:
        s_num = lowered
    m = _NUMBER_RE.fullmatch(s_num)
    if not m:
        raise _err(text, kinds)
    v = float(m.group(1))
    if not math.isfinite(v) or v > _MAX_PARSE_VALUE:
        raise _err(text, kinds)
    return v * multiplier


def parse_size(text) -> int:
    """Parse a user-facing size string into bytes (base 1000).

    Accepts "500MB", "1.5 GB", "2TB"; a bare number means MB. Returns 0 for
    empty/None (call sites treat 0 as "unlimited" where applicable).
    Raises ValueError on unrecognized values so config mistakes surface early.
    """
    if text is None:
        return 0
    if isinstance(text, bool):
        raise _err(text, "大小")
    if isinstance(text, (int, float)):
        # Numeric input in the string-unit keys means MB; the raw-bytes legacy
        # keys keep their own numeric paths. Reject non-finite values and
        # clamp negatives to 0 — negative sizes are meaningless and would
        # bypass `or`-style fallbacks at call sites.
        v = float(text)
        if not math.isfinite(v):
            raise _err(text, "大小")
        return max(int(v * _SIZE_FACTORS["mb"]), 0)
    s = str(text).strip()
    if not s or s == "0":
        return 0
    v = _parse_number_unit(
        s,
        {"tb": _SIZE_FACTORS["tb"], "gb": _SIZE_FACTORS["gb"], "mb": _SIZE_FACTORS["mb"]},
        _SIZE_FACTORS["mb"],
        "大小",
    )
    return int(v)


def format_size(n) -> str:
    """Format a byte count with base 1000; smallest display unit is MB.

    Sub-MB values round up to "0.1 MB" rather than showing bytes/KB; 0 renders
    as "0 MB" so no byte-level magnitudes ever reach the UI. Non-finite or
    unrepresentable inputs render as "0 MB" instead of raising.
    """
    try:
        n = int(n or 0)
    except (TypeError, ValueError, OverflowError):
        return "0 MB"
    if n <= 0:
        return "0 MB"
    for factor, unit in ((_SIZE_FACTORS["tb"], "TB"), (_SIZE_FACTORS["gb"], "GB")):
        if n >= factor:
            return f"{n / factor:.2f} {unit}"
    # MB floor: never display KB or byte magnitudes
    return f"{max(n / _SIZE_FACTORS['mb'], 0.1):.1f} MB"


# ---------- durations (时/分/秒 only) ----------

# Compound form "1时30分15秒" — each component is an optional bounded number.
# Anchored and linear: each group is a fixed decimal token, no nesting.
_DURATION_RE = re.compile(
    r"^(?:([0-9]{1,5})时)?(?:([0-9]{1,5})分)?(?:([0-9]{1,5})(?:\.[0-9]{1,3})?秒)?$",
    re.IGNORECASE,
)


def parse_duration(text) -> float:
    """Parse a duration string ("90秒", "2时30分", "1.5时") into seconds.

    A bare number means seconds. Raises ValueError on unrecognized units —
    milliseconds are not an accepted unit anywhere in this plugin.
    """
    if text is None:
        return 0.0
    if isinstance(text, bool):
        raise _err(text, "时长")
    if isinstance(text, (int, float)):
        v = float(text)
        if not math.isfinite(v):
            raise _err(text, "时长")
        return max(v, 0.0)
    s = str(text).strip()
    if not s:
        return 0.0
    if len(s) > _MAX_UNIT_LEN:
        raise _err(text, "时长")
    lowered = s.lower()
    m = _DURATION_RE.match(lowered)
    if m:
        h, mi, sec = m.groups()
        return float(h or 0) * 3600.0 + float(mi or 0) * 60.0 + float(sec or 0)
    # Single-unit forms: "90秒" / "1.5时" / "2分" (unit spellings incl. aliases)
    single = _parse_number_unit(
        lowered,
        {"小时": 3600.0, "时": 3600.0, "h": 3600.0,
         "分钟": 60.0, "分": 60.0, "min": 60.0,
         "秒": 1.0, "s": 1.0},
        1.0,
        "时长",
    )
    if single is not None:
        return single
    raise _err(text, "时长")


def format_duration(seconds) -> str:
    """Format seconds as 时/分/秒 (e.g. "1时04分05秒", "3分", "45秒").

    Values under a minute render as whole seconds; sub-second magnitudes are
    never shown (milliseconds are forbidden units). Non-finite inputs render
    as "0秒" instead of raising.
    """
    try:
        total = int(round(max(float(seconds or 0), 0.0)))
    except (TypeError, ValueError, OverflowError):
        return "0秒"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}时{m:02d}分{s:02d}秒"
    if m:
        return f"{m}分{s:02d}秒" if s else f"{m}分"
    return f"{s}秒"


# ---------- transfer rates (base 1024; MB/s and GB/s only) ----------

_RATE_BASE = 1024
_RATE_FACTORS = {"gbps": _RATE_BASE**3, "mbps": _RATE_BASE**2}


def parse_rate(text) -> float:
    """Parse a bandwidth string ("10 MB/s", "1.5 GB/s") into bytes-per-second
    using the binary base 1024. A bare number means MB/s.
    """
    v = _parse_number_unit(
        text,
        {"gb/s": _RATE_FACTORS["gbps"], "gbps": _RATE_FACTORS["gbps"],
         "mb/s": _RATE_FACTORS["mbps"], "mbps": _RATE_FACTORS["mbps"]},
        _RATE_FACTORS["mbps"],
        "带宽",
    )
    return 0.0 if v is None else max(float(v), 0.0)


def format_rate(bytes_per_second) -> str:
    """Format bytes/s as a bandwidth string with base 1024; smallest display
    unit is MB/s (KB/s and below are never shown). Non-finite inputs render
    as "0 MB/s" instead of raising.
    """
    try:
        v = float(bytes_per_second or 0)
    except (TypeError, ValueError, OverflowError):
        return "0 MB/s"
    if not math.isfinite(v) or v <= 0:
        return "0 MB/s"
    if v >= _RATE_FACTORS["gbps"]:
        return f"{v / _RATE_FACTORS['gbps']:.2f} GB/s"
    return f"{max(v / _RATE_FACTORS['mbps'], 0.1):.1f} MB/s"


__all__ = [
    "parse_size", "format_size",
    "parse_duration", "format_duration",
    "parse_rate", "format_rate",
]
