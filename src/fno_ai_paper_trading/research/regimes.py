"""Deterministic market-regime datasets for strategy research.

Each regime is an explicitly shaped, hand-verifiable price path. No random data
is used anywhere in this module — every close is the output of a fixed formula
or an explicit list, so tests and experiments are fully reproducible and avoid
binary floating point.

Regimes provided:
* sustained uptrend / sustained downtrend — brief counter-drift that reverses
  into a sustained monotone trend (guarantees an MA crossover near the pivot)
* sideways / choppy — oscillation around a flat level
* volatile — large swing amplitude with no strong drift
* trend reversal — strong directional move that flips
* low volatility — near-flat path with tiny oscillation
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.backtest.datasets import closes_to_bars
from fno_ai_paper_trading.models.instruments import Instrument


def build_sustained_uptrend(
    instrument: Instrument,
    bars: int = 120,
    start: Decimal = Decimal("100"),
    dip: Decimal = Decimal("2"),
    warm: int = 26,
    step: Decimal = Decimal("3"),
) -> list:
    """A long flat-ish counter-drift that reverses into a sustained rise.

    The opening ``warm`` bars decline by ``dip`` per bar and the remaining bars
    rise monotonically by ``step`` per bar. With the default ``warm=26`` the
    slow moving average is already computable and falling when the climb begins,
    so the fast average crosses above the slow one exactly once near the
    reversal (a hand-verifiable BUY) and stays above it — the position is simply
    held through the sustained uptrend. Shape is fully deterministic.
    """
    if bars <= 0:
        return []
    warm = min(warm, bars)
    closes: list[Decimal] = [start - Decimal(i) * dip for i in range(warm)]
    base = closes[-1]
    closes.extend(base + Decimal(k) * step for k in range(1, bars - warm + 1))
    return closes_to_bars(instrument, closes)


def build_sustained_downtrend(
    instrument: Instrument,
    bars: int = 120,
    start: Decimal = Decimal("300"),
    bump: Decimal = Decimal("2"),
    warm: int = 26,
    step: Decimal = Decimal("3"),
) -> list:
    """A long flat-ish counter-drift that reverses into a sustained decline.

    Mirror of :func:`build_sustained_uptrend`: the opening ``warm`` bars rise
    by ``bump`` per bar, then the price falls monotonically by ``step`` per bar.
    The fast average crosses below the slow one exactly once near the reversal
    (a hand-verifiable SELL) and stays below it. The falling phase is clamped at
    3 so the bar builder's low price stays non-negative for long series.
    """
    if bars <= 0:
        return []
    warm = min(warm, bars)
    closes: list[Decimal] = [start + Decimal(i) * bump for i in range(warm)]
    base = closes[-1]
    # Clamp at 3 so open/low stay non-negative in the bar builder (open=close-1, low=close-2).
    closes.extend(max(base - Decimal(k) * step, Decimal("3")) for k in range(1, bars - warm + 1))
    return closes_to_bars(instrument, closes)


def build_sideways_choppy(instrument: Instrument, bars: int = 36, level: Decimal = Decimal("100"), amplitude: Decimal = Decimal("3")) -> list:
    """Oscillates around ``level`` with a deterministic cycle."""
    closes: list[Decimal] = []
    for i in range(bars):
        wiggle = Decimal((i % 6) - 3)  # -3..2 deterministic cycle
        closes.append(level + wiggle * amplitude)
    return closes_to_bars(instrument, closes)


def build_volatile_market(instrument: Instrument, bars: int = 40, level: Decimal = Decimal("100"), amplitude: Decimal = Decimal("15")) -> list:
    """Large amplitude swings around a level; weak overall drift.

    Uses a deterministic 4-bar cycle so no floating point enters the data.
    """
    closes: list[Decimal] = []
    for i in range(bars):
        phase = i % 4
        swing = Decimal("-1") if phase < 2 else Decimal("1")
        closes.append(level + swing * amplitude)
    return closes_to_bars(instrument, closes)


def build_trend_reversal(
    instrument: Instrument,
    bars: int = 120,
    start: Decimal = Decimal("100"),
    dip: Decimal = Decimal("2"),
    warm: int = 26,
    rise: int = 90,
    step: Decimal = Decimal("3"),
) -> list:
    """A counter-drift, then a sustained rise, then a sustained fall.

    Opens with ``warm`` declining bars (fast average below the slow average),
    rises by ``step`` per bar for ``rise`` bars (a hand-verifiable BUY cross near
    the pivot), then falls by ``step`` per bar to the end (a hand-verifiable
    SELL cross shortly after the peak). The result is a single closed
    buy-then-sell round trip demonstrating realized P&L and commissions. The
    falling phase is clamped at 3.
    """
    if bars <= 0:
        return []
    warm = min(warm, bars)
    rise = min(rise, bars - warm)
    closes: list[Decimal] = [start - Decimal(i) * dip for i in range(warm)]
    base = closes[-1]
    up: list[Decimal] = [base + Decimal(k) * step for k in range(1, rise + 1)]
    top = up[-1]
    closes.extend(up)
    closes.extend(max(top - Decimal(k) * step, Decimal("3")) for k in range(1, bars - warm - rise + 1))
    return closes_to_bars(instrument, closes)


def build_low_volatility(instrument: Instrument, bars: int = 36, level: Decimal = Decimal("100")) -> list:
    """Near-flat path with tiny deterministic oscillation (<= 0.5)."""
    closes: list[Decimal] = []
    for i in range(bars):
        closes.append(level + (Decimal("0.5") if i % 2 else Decimal("-0.5")))
    return closes_to_bars(instrument, closes)


REGIME_BUILDERS: dict[str, object] = {
    "sustained_uptrend": build_sustained_uptrend,
    "sustained_downtrend": build_sustained_downtrend,
    "sideways_choppy": build_sideways_choppy,
    "volatile_market": build_volatile_market,
    "trend_reversal": build_trend_reversal,
    "low_volatility": build_low_volatility,
}


def regime_stats(bars: list) -> dict[str, Decimal]:
    """Verifiable stats over the close series (deterministic, Decimal only)."""
    closes = [b.close for b in bars]
    if not closes:
        return {"first": Decimal("0"), "last": Decimal("0"), "min": Decimal("0"), "max": Decimal("0"), "range": Decimal("0")}
    first = closes[0]
    last = closes[-1]
    low = min(closes)
    high = max(closes)
    return {
        "first": first,
        "last": last,
        "min": low,
        "max": high,
        "range": high - low,
    }