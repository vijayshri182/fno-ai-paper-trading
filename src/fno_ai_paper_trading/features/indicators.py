"""Deterministic technical indicators over validated OHLCV bars.

All values are ``Decimal``; no floats leak into indicators. Indicators return
``None`` when there is not enough data — callers decide how to handle gaps.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.models.market import MarketPrice

DECIMAL_ONE_HUNDRED = Decimal("100")


def sma(bars: Sequence[MarketPrice], period: int, field: str = "close") -> Decimal | None:
    """Simple moving average of the last ``period`` bar values (Decimal)."""
    if period <= 0 or len(bars) < period:
        return None
    total = Decimal("0")
    for bar in bars[-period:]:
        total += getattr(bar, field)
    return total / period


def rsi(bars: Sequence[MarketPrice], period: int = 14) -> Decimal | None:
    """Relative Strength Index over the last ``period`` price changes.

    Uses simple (non-smoothed) average gains/losses for determinism. Neutral
    value 50 is returned when there are no gains and no losses.
    """
    if period <= 0 or len(bars) < period + 1:
        return None
    gains = Decimal("0")
    losses = Decimal("0")
    for index in range(len(bars) - period, len(bars)):
        change = bars[index].close - bars[index - 1].close
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        if avg_gain == 0:
            return Decimal("50")
        return DECIMAL_ONE_HUNDRED
    if avg_gain == 0:
        return Decimal("0")
    rs = avg_gain / avg_loss
    return DECIMAL_ONE_HUNDRED - (DECIMAL_ONE_HUNDRED / (Decimal("1") + rs))


def close_return(bars: Sequence[MarketPrice], lookback: int = 1) -> Decimal | None:
    """Percent return of the last close versus ``lookback`` bars earlier."""
    if lookback <= 0 or len(bars) < lookback + 1:
        return None
    prior = bars[-(lookback + 1)].close
    if prior == 0:
        return None
    return ((bars[-1].close - prior) / prior) * DECIMAL_ONE_HUNDRED


def mean_squared_return(bars: Sequence[MarketPrice], period: int = 20) -> Decimal | None:
    """Mean of squared one-bar returns over the last ``period`` changes."""
    if period <= 0 or len(bars) < period + 1:
        return None
    total = Decimal("0")
    excluded = 0
    for index in range(len(bars) - period, len(bars)):
        prior = bars[index - 1].close
        if prior == 0:
            excluded += 1
            continue
        move = (bars[index].close - prior) / prior
        total += move * move
    usable = period - excluded
    if usable <= 0:
        return None
    return total / usable


def volatility_ratio(
    bars: Sequence[MarketPrice], short: int = 10, long: int = 30
) -> Decimal | None:
    """Ratio of short-window volatility to a longer baseline window.

    ``> 1`` means recent volatility is elevated relative to the longer baseline.
    """
    short_msr = mean_squared_return(bars, short)
    long_msr = mean_squared_return(bars, long)
    if short_msr is None or long_msr is None:
        return None
    if long_msr == 0:
        return None
    return short_msr / long_msr