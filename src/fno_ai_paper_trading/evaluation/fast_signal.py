"""Fast, exact decision-time signals for the MA-cross strategy (evaluation-only).

``MovingAverageCrossStrategy.analyze(bars[:i+1])`` is stateless and deterministic,
but calling it for every bar of a long series is O(n^2): each call re-slices the
prefix, rebuilds a `closes` list, and re-computes the moving averages from
scratch. This module precomputes the identical signal series in ~O(n * slow) with
a rolling Decimal window, so a full multi-year intraday replay stays fast while
keeping the same no-look-ahead contract — signal[i] is still a pure function of
bars[:i+1]. The arithmetic mirrors ``MovingAverageCrossStrategy._sma`` exactly
(``sum(values[-window:], Decimal("0")) / window`` over the same Decimal operands),
so even the ``meta`` repr strings match bit-for-bit.

Equivalence is asserted in ``tests/test_fast_signal.py`` against
``MovingAverageCrossStrategy.analyze`` over many prefixes. Nothing here places an
order or touches risk/execution code.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult
from fno_ai_paper_trading.utils.functions import positive_int


def moving_average_cross_signals(
    bars: Sequence[MarketPrice], *, fast: int = 5, slow: int = 21
) -> list[SignalResult]:
    """Return ``signals[i] == MovingAverageCrossStrategy(fast, slow).analyze(bars[:i+1])``."""
    fast = positive_int(fast, "fast")
    slow = positive_int(slow, "slow")
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")

    n = len(bars)
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    if n == 0:
        return signals

    roll: deque[Decimal] = deque(maxlen=slow + 1)
    for i in range(n):
        bridge = bars[i]
        roll.append(bridge.close)
        closes_len = i + 1
        if closes_len < slow + 1:
            signals[i] = SignalResult(
                signal=Signal.HOLD,
                instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"need at least {slow + 1} bars; have {closes_len}",
            )
            continue

        window = list(roll)
        fast_now = sum(window[-fast:], Decimal("0")) / fast
        slow_now = sum(window[-slow:], Decimal("0")) / slow
        prev = window[:-1]
        fast_prev = sum(prev[-fast:], Decimal("0")) / fast
        slow_prev = sum(prev[-slow:], Decimal("0")) / slow

        meta = {
            "fast": str(fast_now),
            "slow": str(slow_now),
            "fast_prev": str(fast_prev),
            "slow_prev": str(slow_prev),
            "last_close": str(bridge.close),
        }
        if fast_prev <= slow_prev and fast_now > slow_now:
            signals[i] = SignalResult(
                signal=Signal.BUY,
                instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"fast MA ({fast_now:f}) crossed above slow MA ({slow_now:f})",
                meta=meta,
            )
        elif fast_prev >= slow_prev and fast_now < slow_now:
            signals[i] = SignalResult(
                signal=Signal.SELL,
                instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"fast MA ({fast_now:f}) crossed below slow MA ({slow_now:f})",
                meta=meta,
            )
        else:
            signals[i] = SignalResult(
                signal=Signal.HOLD,
                instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason="no crossover",
                meta=meta,
            )
    return signals


def moving_average_cross_signal_series(bars, fast: int = 5, slow: int = 21):
    """Convenience: only the ``Signal`` enum per bar (e.g. quick regime checks)."""
    return [entry.signal for entry in moving_average_cross_signals(bars, fast=fast, slow=slow)]