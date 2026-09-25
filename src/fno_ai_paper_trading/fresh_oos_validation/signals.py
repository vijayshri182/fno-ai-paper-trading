"""Deterministic, decision-time replay engines for the fresh window.

Two engines are used by the controlled validation job:

* ``control_ma_signals`` -- the frozen MA(5,21) crossover (the champion control).
* ``algo_004_signals`` -- OUR-ALGO-004 under its frozen coherent single-slot
  reversal-flat-hold semantics (fast/slow crossover entry, ATR stop both sides,
  max-hold-days, slope-window confluence-flat with reversal suppression).

Both consume ONLY bar history ``closes[:i+1]`` at bar ``i`` -- no look-ahead --
and emit a target stream of ``LONG`` / ``SHORT`` / ``FLAT``.  Every position is
quantity 1 and signals are computed from ``MarketPrice`` objects already in
chronological order (the pool loader guarantees this).

These are *fresh-window validation instruments*, documented in the outcome's
evidence limitations; they never modify any protected research artifact.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.fresh_oos_validation.protocol import FROZEN_ALGO_PARAMS

FLAT = "FLAT"
LONG = "LONG"
SHORT = "SHORT"


def _sma(closes: Sequence[Decimal], index: int, window: int) -> Decimal | None:
    if index + 1 < window:
        return None
    total = sum(closes[index + 1 - window : index + 1], Decimal("0"))
    return total / Decimal(window)


def control_ma_signals(
    bars: Sequence[MarketPrice], *, fast: int = 5, slow: int = 21
) -> list[str]:
    """MA(fast)/(slow) crossover target stream (champion control).

    Decision time = bar close: the target at bar ``i`` uses only closes up to
    ``i``.  The position toggles LONG/SHORT on crossover and is FLAT before the
    slow window is warm.
    """
    closes = [bar.close for bar in bars]
    target: str = FLAT
    out: list[str] = []
    for index in range(len(bars)):
        fast_ma = _sma(closes, index, fast)
        slow_ma = _sma(closes, index, slow)
        if fast_ma is not None and slow_ma is not None:
            if fast_ma > slow_ma:
                target = LONG
            elif fast_ma < slow_ma:
                target = SHORT
        out.append(target)
    return out


def _atr(bars: Sequence[MarketPrice], index: int, window: int) -> Decimal | None:
    if index + 1 < window:
        return None
    values: list[Decimal] = []
    for i in range(index + 1 - window, index + 1):
        bar = bars[i]
        high_low = bar.high - bar.low
        previous_close = bars[i - 1].close if i > 0 else bar.open
        high_prev = bar.high - previous_close
        low_prev = previous_close - bar.low
        values.append(max(high_low, abs(high_prev), abs(low_prev)))
    return sum(values, Decimal("0")) / Decimal(window)


def algo_004_signals(
    bars: Sequence[MarketPrice],
    *,
    fast: int = int(FROZEN_ALGO_PARAMS["fast"]),
    slow: int = int(FROZEN_ALGO_PARAMS["slow"]),
    slope_window: int = int(FROZEN_ALGO_PARAMS["slope_window"]),
    lookback: int = int(FROZEN_ALGO_PARAMS["lookback"]),
    stop_atr_mult: float = float(FROZEN_ALGO_PARAMS["stop_atr_mult"]),
    max_hold_days: int = int(FROZEN_ALGO_PARAMS["max_hold_days"]),
) -> list[str]:
    """OUR-ALGO-004 coherent single-slot reversal-flat-hold target stream.

    Coherent semantics (research-consistent, decision-time, single-slot):

    * entry on fast/slow crossover (LONG over, SHORT under);
    * a cross to the opposite side is a REVERSAL: close in the same bar and
      re-enter the opposite leg the next bar window (one slot, never both);
    * exits by priority: reversal cross > confluence-flat > stop > max hold;
    * confluence-flat (fast MA flattened over ``slope_window``) is SUPPRESSED
      once for a position entered right after a reversal (the documented
      reversal-flat-hold treatment keeps holding past the first flat signal);
    * a both-sides ATR stop (``stop_atr_mult`` x ATR over ``lookback``) and a
      ``max_hold_days`` limit exit independently at bar close.

    Returns one target per bar.  Parameters default to the frozen OUR-ALGO-004
    literals; the job always passes them explicitly and the gate re-pins them.
    """
    closes = [bar.close for bar in bars]
    target: str = FLAT
    entry_index: int | None = None
    reversal_entry = False
    suppression_used = False
    out: list[str] = []
    previous_fast: Decimal | None = None
    previous_slow: Decimal | None = None

    for index in range(len(bars)):
        fast_ma = _sma(closes, index, fast)
        slow_ma = _sma(closes, index, slow)
        atr_value = _atr(bars, index, lookback)
        crossed = _crossed(fast_ma, slow_ma, previous_fast, previous_slow)

        if target == FLAT:
            if crossed is not None:
                target = crossed
                entry_index = index
                reversal_entry = False
                suppression_used = False
        else:
            if crossed == _opposite(target):
                target = crossed
                entry_index = index
                reversal_entry = True
                suppression_used = False
            elif atr_value is not None and _stop_triggered(
                bars, index, target, atr_value, stop_atr_mult
            ):
                target = FLAT
            elif held_sessions(bars, entry_index, index) >= max_hold_days:
                target = FLAT
            elif _confluence_flat(closes, index, fast, slow, slope_window):
                if reversal_entry and not suppression_used:
                    suppression_used = True
                else:
                    target = FLAT

        previous_fast = fast_ma
        previous_slow = slow_ma
        out.append(target)

    return out


def held_sessions(bars: Sequence[MarketPrice], entry_index: int | None, index: int) -> int:
    """Distinct trading days held from ``entry_index`` through ``index`` inclusive."""
    if entry_index is None:
        return 0
    return len({bar.timestamp.date() for bar in bars[entry_index : index + 1]})


def _crossed(
    fast_ma: Decimal | None,
    slow_ma: Decimal | None,
    previous_fast: Decimal | None,
    previous_slow: Decimal | None,
) -> str | None:
    """LONG on an up-cross, SHORT on a down-cross, else None (decision-time)."""
    if fast_ma is None or slow_ma is None or previous_fast is None or previous_slow is None:
        return None
    if fast_ma > slow_ma and previous_fast <= previous_slow:
        return LONG
    if fast_ma < slow_ma and previous_fast >= previous_slow:
        return SHORT
    return None


def _opposite(target: str) -> str | None:
    if target == LONG:
        return SHORT
    if target == SHORT:
        return LONG
    return None


def _stop_triggered(
    bars: Sequence[MarketPrice],
    index: int,
    target: str,
    atr_value: Decimal,
    stop_atr_mult: float,
) -> bool:
    distance = Decimal(stop_atr_mult) * atr_value
    bar = bars[index]
    if target == LONG:
        return bar.low <= bar.close - distance
    if target == SHORT:
        return bar.high >= bar.close + distance
    return False


def _confluence_flat(
    closes: Sequence[Decimal], index: int, fast: int, slow: int, slope_window: int
) -> bool:
    """Confluence-flat: the fast MA has flattened relative to the slow MA.

    Measured over the last ``slope_window`` bars; the fast slope has
    decelerated below a quarter of the slow slope and is small relative to the
    last close.  Deterministic and decision-time only.
    """
    if index + 1 < slope_window + 1:
        return False
    fast_values = [
        _sma(closes, i, fast) for i in range(index + 1 - slope_window, index + 1)
    ]
    slow_values = [
        _sma(closes, i, slow) for i in range(index + 1 - slope_window, index + 1)
    ]
    fast_values = [v for v in fast_values if v is not None]
    slow_values = [v for v in slow_values if v is not None]
    if len(fast_values) < 2 or len(slow_values) < 2:
        return False
    fast_slope = fast_values[-1] - fast_values[0]
    slow_slope = slow_values[-1] - slow_values[0]
    last_close = closes[index]
    if last_close == 0:
        return False
    return abs(fast_slope) < (abs(slow_slope) / 4) and (
        abs(fast_slope) / abs(last_close) < Decimal("0.0004")
    )