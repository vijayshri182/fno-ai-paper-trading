"""Strict 15-minute window construction over completed 5-minute candles.

A decision window is *complete* only when the three candles that open at
``moment - 15``, ``moment - 10`` and ``moment - 5`` minutes are all present
in the consumed history and all completed (each ``timestamp + 5m <= moment``).
Duplicates and out-of-order candles are rejected (logged as data anomalies);
an incomplete or malformed window never produces a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fno_ai_paper_trading.models.market import MarketPrice

__all__ = ["WindowResult", "DecisionWindowAggregator"]


@dataclass(frozen=True)
class WindowResult:
    """Outcome of asking the aggregator for the window at one moment."""

    moment: datetime
    window: tuple[MarketPrice, ...]
    complete: bool
    note: str

    @property
    def reference_bar(self) -> MarketPrice | None:
        """The decision reference candle (the last bar of a complete window)."""
        return self.window[-1] if self.complete and self.window else None


class DecisionWindowAggregator:
    """Consumes validated candles once and answers ``decision_at(moment)``."""

    def __init__(
        self,
        *,
        bar_minutes: int = 5,
        window_bars: int = 3,
        decision_minutes: int = 15,
    ) -> None:
        if window_bars != 3:
            raise ValueError("the approved contract fixes exactly 3 candles per window")
        self.bar_minutes = bar_minutes
        self.window_bars = window_bars
        self.decision_minutes = decision_minutes
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
        """The window for ``moment``. Complete only when the exact triple
        ``{moment-15m, moment-10m, moment-5m}`` has all three candles and the
        last of them just completed at ``moment``.
        """
        expected = {
            moment - timedelta(minutes=step)
            for step in range(self.bar_minutes, self.bar_minutes * self.window_bars + 1,
                              self.bar_minutes)
        }
        window = tuple(bar for bar in self.history if bar.timestamp in expected)
        present = {bar.timestamp for bar in window}
        if len(window) != self.window_bars or present != expected:
            missing = sorted(dt.time() for dt in expected - present)
            note = (
                f"incomplete window at {moment:%H:%M}: missing candle(s) "
                + ", ".join(f"{t:%H:%M}" for t in missing)
            )
            return WindowResult(moment=moment, window=(), complete=False, note=note)
        last = window[-1]
        if last.timestamp + timedelta(minutes=self.bar_minutes) != moment:
            note = (
                f"window not yet complete at {moment:%H:%M}: last candle "
                f"{last.timestamp:%H:%M} completes later"
            )
            return WindowResult(moment=moment, window=(), complete=False, note=note)
        return WindowResult(
            moment=moment, window=tuple(window), complete=True, note="weekday window complete"
        )