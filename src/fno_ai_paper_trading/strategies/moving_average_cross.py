"""Moving-average crossover strategy.

Compares a fast and a slow simple moving average of the close. A crossover of
the fast average above the slow one is a BUY signal; the opposite crossover is
a SELL signal; otherwise the strategy holds. Fully deterministic and stateless —
the output depends only on the supplied bar series.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.utils.functions import positive_int


def _sma(values: list[Decimal], window: int) -> Decimal:
    return sum(values[-window:], Decimal("0")) / window


class MovingAverageCrossStrategy(Strategy):
    """BUY/SELL on fast/slow simple moving-average crossovers of the close."""

    name = "moving_average_cross"

    def __init__(self, fast: int = 5, slow: int = 21) -> None:
        self.fast = positive_int(fast, "fast")
        self.slow = positive_int(slow, "slow")
        if self.fast >= self.slow:
            raise ValueError("fast period must be shorter than slow period")

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        if len(bars) < self.slow + 1:
            return SignalResult(
                signal=Signal.HOLD,
                instrument=bars[-1].instrument if bars else None,
                timestamp=bars[-1].timestamp if bars else None,
                reason=f"need at least {self.slow + 1} bars; have {len(bars)}",
            )

        closes = [bar.close for bar in bars]
        fast_now = _sma(closes, self.fast)
        slow_now = _sma(closes, self.slow)
        fast_prev = _sma(closes[:-1], self.fast)
        slow_prev = _sma(closes[:-1], self.slow)
        last = bars[-1]

        meta = {
            "fast": str(fast_now),
            "slow": str(slow_now),
            "fast_prev": str(fast_prev),
            "slow_prev": str(slow_prev),
            "last_close": str(last.close),
        }

        if fast_prev <= slow_prev and fast_now > slow_now:
            return SignalResult(
                signal=Signal.BUY,
                instrument=last.instrument,
                timestamp=last.timestamp,
                reason=f"fast MA ({fast_now:f}) crossed above slow MA ({slow_now:f})",
                meta=meta,
            )
        if fast_prev >= slow_prev and fast_now < slow_now:
            return SignalResult(
                signal=Signal.SELL,
                instrument=last.instrument,
                timestamp=last.timestamp,
                reason=f"fast MA ({fast_now:f}) crossed below slow MA ({slow_now:f})",
                meta=meta,
            )
        return SignalResult(
            signal=Signal.HOLD,
            instrument=last.instrument,
            timestamp=last.timestamp,
            reason="no crossover",
            meta=meta,
        )