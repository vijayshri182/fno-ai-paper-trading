"""Canonical bar-interval tokens and per-vendor mappings.

Providers use different bucket labels for the same cadence (Upstox V3 uses a
``unit``/``interval`` pair such as ``minutes``/``5``, Kite uses labels such as
``5minute``). This module defines one canonical set of tokens and converts to
and from each vendor's notation, so research code never has to know a provider's
naming.

Canonical tokens (all decimals rounded to whole bars):
    ``1m`` ``3m`` ``5m`` ``10m`` ``15m`` ``30m``
    ``1h`` ``2h`` ``3h`` ``4h``
    ``1d`` ``1w`` ``1M``
"""
from __future__ import annotations

from typing import Any

CANONICAL_INTERVALS: tuple[str, ...] = (
    "1m", "3m", "5m", "10m", "15m", "30m",
    "1h", "2h", "3h", "4h",
    "1d", "1w", "1M",
)
_CANONICAL_SET = frozenset(CANONICAL_INTERVALS)

# Legacy bucket labels accepted by earlier providers (Kite Connect and the
# in-memory provider). Aliases resolve to the canonical token.
_LEGACY_ALIASES: dict[str, str] = {
    "minute": "1m",
    "1minute": "1m",
    "3minute": "3m",
    "5minute": "5m",
    "10minute": "10m",
    "15minute": "15m",
    "30minute": "30m",
    "60minute": "1h",
    "hour": "1h",
    "day": "1d",
    "daily": "1d",
    "week": "1w",
    "weekly": "1w",
    "month": "1M",
    "monthly": "1M",
}

# Upstox V3 ``unit`` / ``interval`` pairs (i.e. `GET /v3/historical-candle/...`).
UPSTOX_V3_UNIT_INTERVAL: dict[str, tuple[str, int]] = {
    "1m": ("minutes", 1),
    "3m": ("minutes", 3),
    "5m": ("minutes", 5),
    "10m": ("minutes", 10),
    "15m": ("minutes", 15),
    "30m": ("minutes", 30),
    "1h": ("hours", 1),
    "2h": ("hours", 2),
    "3h": ("hours", 3),
    "4h": ("hours", 4),
    "1d": ("days", 1),
    "1w": ("weeks", 1),
    "1M": ("months", 1),
}

# Kite Connect historical-candle bucket labels.
KITE_INTERVAL_TOKEN: dict[str, str] = {
    "1m": "minute",
    "3m": "3minute",
    "5m": "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h": "60minute",
    "1d": "day",
    "1w": "week",
    "1M": "month",
}

# Approximate bar length in minutes (used for spacing/gap validation). Week
# and month bars have no fixed minute length, so they map to ``None``.
_INTERVAL_MINUTES: dict[str, int | None] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "3h": 180,
    "4h": 240,
    "1d": 1440,
    "1w": None,
    "1M": None,
}


def is_valid_interval(value: Any) -> bool:
    """True when ``value`` resolves to a known canonical interval."""
    if not isinstance(value, str):
        return False
    return canonical_interval(value) is not None


def canonical_interval(value: Any) -> str | None:
    """Normalize a vendor/legacy bucket label to a canonical token.

    Returns ``None`` for unknown values (never raises), so callers can produce
    their own error messages with the supported list.
    """
    if not isinstance(value, str):
        return None
    token = value.strip()
    if token in _CANONICAL_SET:
        return token
    lowered = token.lower()
    # Case-insensitive canonical match, except the ambiguous minute/month pair
    # ("1M" is month and is only reachable as an exact "1M" token above).
    if lowered in _CANONICAL_SET and lowered != "1m":
        return lowered
    return _LEGACY_ALIASES.get(lowered)


def interval_minutes(value: Any) -> int | None:
    """Approximate bar length in minutes, or ``None`` for week/month bars."""
    token = canonical_interval(value)
    if token is None:
        return None
    return _INTERVAL_MINUTES[token]


def upstox_unit_interval(value: Any) -> tuple[str, int]:
    """Resolve ``value`` to an Upstox V3 ``(unit, interval)`` pair.

    Raises :class:`ValueError` for unknown intervals.
    """
    token = canonical_interval(value)
    if token is None:
        raise ValueError(
            f"unknown interval {value!r}; expected one of {CANONICAL_INTERVALS}"
        )
    return UPSTOX_V3_UNIT_INTERVAL[token]


def kite_interval_token(value: Any) -> str:
    """Resolve ``value`` to a Kite Connect historical bucket label.

    Raises :class:`ValueError` for unknown intervals.
    """
    token = canonical_interval(value)
    if token is None:
        raise ValueError(
            f"unknown interval {value!r}; expected one of {CANONICAL_INTERVALS}"
        )
    return KITE_INTERVAL_TOKEN[token]