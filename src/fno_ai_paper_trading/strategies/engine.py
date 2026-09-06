"""Strategy engine: replays a bar series through a strategy.

The engine keeps the strategy stateless: ``evaluate`` walks the series feeding
the prefix ``bars[:i]`` to ``strategy.analyze`` and collects one
:class:`SignalResult` per bar. This is deterministic and has no hidden state.
"""
from __future__ import annotations

from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy


class StrategyEngine:
    """Applies a :class:`Strategy` to a chronological bar series."""

    def __init__(self, strategy: Strategy) -> None:
        if not isinstance(strategy, Strategy):
            raise TypeError("engine requires a Strategy instance")
        self.strategy = strategy

    def evaluate(self, bars: list[MarketPrice]) -> list[SignalResult]:
        """Return one signal per bar (oldest first); early bars are HOLD."""
        if not bars:
            return []
        return [self.strategy.analyze(bars[: i + 1]) for i in range(len(bars))]

    def signals(self, bars: list[MarketPrice]) -> list[SignalResult]:
        """Return only the actionable (BUY/SELL) signals, oldest first."""
        return [result for result in self.evaluate(bars) if result.actionable]