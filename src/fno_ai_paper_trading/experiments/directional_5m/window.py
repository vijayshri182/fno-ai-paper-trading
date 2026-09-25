"""Single-completed-candle aggregation for the 5-minute directional experiment.

A decision after a completed 5-minute candle is *ready* only when the candle
that opened at ``moment - 5m`` has been consumed and is completed (its
``timestamp + 5m == moment``). Duplicates and out-of-order candles are rejected
(logged as data anomalies); a missing or not-yet-complete candle never produces
a decision - the aggregator answers instead with an explicit incomplete note.

This deliberately mirrors :mod:`directional_15m.window`'s strictness but with a
single-candle cadence (``window_bars == 1``): no 3-bar window is ever formed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fno_ai_paper_trading.models.market import MarketPrice

__all__ = ["WindowResult", "BarCompletionAggregator"]


@dataclass(frozen=True)
class WindowResult:
    """Outcome of asking the aggregator for the decision candle at one moment."""

    moment: datetime
    window: tuple[MarketPrice, ...]
    complete: bool
    note: str

    @property
    def reference_bar(self) -> MarketPrice | None:
        """The decision reference candle (the completed candle of the moment)."""
        return self.window[-1] if self.complete and self.window else None


class BarCompletionAggregator:
    """Consumes validated completed candles once and answers ``decision_at``.

    ``decision_at(moment)`` completes only when the candle that opens at
    ``moment - bar_minutes`` is present in the consumed history *and* is
    completed exactly at ``moment``.
    """

    def __init__(self, *, bar_minutes: int = 5) -> None:
        if bar_minutes <= 0:
            raise ValueError("bar_minutes must be positive")
        self.bar_minutes = bar_minutes
        self.history: list[MarketPrice] = []
        self.consumed: set[datetime] = set()
        self.anomalies: list[str] = []

    # --------------------------------------------------------------- intake

    def ingest(self, bar: MarketPrice) -> bool:
        """Consume one completed candle exactly once. Returns False (with a
        recorded anomaly) for duplicates or out-of-order deliveries.
        """
        if bar.timestamp in self.consumed:
            self.anomalies.append(f"duplicate candle {bar.timestamp:%Y-%m-%d %H:%M}")
            return False
        if self.history and bar.timestamp <= self.history[-1].timestamp:
            self.anomalies.append(
                f"out-of-order candle {bar.timestamp:%Y-%m-%d %H:%M} after "
                f"{self.history[-1].timestamp:%Y-%m-%d %H:%M}"
            )
            return False
        self.history.append(bar)
        self.consumed.add(bar.timestamp)
        return True

    # -------------------------------------------------------------- lookups

    def completed_by(self, moment: datetime) -> list[MarketPrice]:
        return [
            bar
            for bar in self.history
            if bar.timestamp + timedelta(minutes=self.bar_minutes) <= moment
        ]

    def decision_at(self, moment: datetime) -> WindowResult:
        """The decision ready at ``moment``. Complete only when the candle that
        opened at ``moment - bar_minutes`` is present and completed exactly at
        ``moment`` (``timestamp + bar_minutes == moment``).
        """
        expected_open = moment - timedelta(minutes=self.bar_minutes)
        for bar in self.history:
            if bar.timestamp != expected_open:
                continue
            if bar.timestamp + timedelta(minutes=self.bar_minutes) != moment:
                note = (
                    f"candle not yet complete at {moment:%H:%M}: candle "
                    f"{bar.timestamp:%H:%M} completes later"
                )
                return WindowResult(moment=moment, window=(), complete=False, note=note)
            return WindowResult(
                moment=moment,
                window=(bar,),
                complete=True,
                note="weekday candle complete",
            )
        note = (
            f"no completed candle at {moment:%H:%M}: expected opening "
            f"{expected_open:%H:%M}"
        )
        return WindowResult(moment=moment, window=(), complete=False, note=note)