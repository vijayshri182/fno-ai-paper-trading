"""Schema version and canonical code sets used by the observability layer."""
from __future__ import annotations

SCHEMA_VERSION = 1

FROZEN_NO_TRADE_CODES: frozenset[str] = frozenset({
    "SIDEWAYS_MARKET", "WEAK_TREND", "LOW_VOLATILITY", "EXTREME_VOLATILITY",
    "NO_PULLBACK", "NO_MOMENTUM", "NO_BREAKOUT", "POOR_RISK_REWARD",
    "INSUFFICIENT_EXPECTED_EDGE", "HIGH_COST", "TIME_CUTOFF", "RISK_LIMIT", "DAILY_LOSS_LIMIT",
})

MECHANICAL_CODES: frozenset[str] = frozenset({"WARM_UP", "HOLDING", "TIME_NOT_OPEN"})

ALL_KNOWN_REASONS: frozenset[str] = FROZEN_NO_TRADE_CODES | MECHANICAL_CODES

# Mapping reason → coarse category for no_trade_reasons table
_REASON_CATEGORY: dict[str, str] = {
    "SIDEWAYS_MARKET": "regime", "WEAK_TREND": "regime",
    "LOW_VOLATILITY": "volatility", "EXTREME_VOLATILITY": "volatility",
    "NO_PULLBACK": "signal", "NO_MOMENTUM": "momentum", "NO_BREAKOUT": "signal",
    "POOR_RISK_REWARD": "risk", "INSUFFICIENT_EXPECTED_EDGE": "risk",
    "HIGH_COST": "cost", "TIME_CUTOFF": "time", "RISK_LIMIT": "risk",
    "DAILY_LOSS_LIMIT": "risk",
    "WARM_UP": "mechanical", "HOLDING": "mechanical", "TIME_NOT_OPEN": "time",
}


def reason_category(code: str) -> str:
    if code in _REASON_CATEGORY:
        return _REASON_CATEGORY[code]
    lower = code.lower()
    if "volatil" in lower:
        return "volatility"
    if "regime" in lower or "market" in lower:
        return "regime"
    if "time" in lower or "cutoff" in lower or "exit" in lower:
        return "time"
    if "risk" in lower or "loss" in lower:
        return "risk"
    if "cost" in lower:
        return "cost"
    if "momentum" in lower:
        return "momentum"
    return "other"
