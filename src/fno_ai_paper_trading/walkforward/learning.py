"""Nightly evidence review, problem detection, hypothesis selection (WS 7.18).

The walk-forward engine is evidence-driven and aggregate-only: a single win or
loss can never change the algorithm, and every learning signal is a summary over
a trailing window of completed champion trades. This module provides the three
deterministic building blocks:

* :class:`EvidenceAggregator` -- accumulates completed champion round-trip
  outcomes, day by day (append-only; restorable from the resume checkpoint).
* :func:`detect_problem` -- the nightly problem/failure-pattern review.
* :func:`select_challengers` -- evidence-gated hypothesis selection (imports
  the fixed-parameter catalog; never tunes parameters).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from fno_ai_paper_trading.experience.enums import OutcomeKind
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.models.enums import Signal

from fno_ai_paper_trading.walkforward.catalog import (
    CATALOG,
    ChampionWindowStats,
    WindowSlice,
    select_challengers,
)
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ChampionTradeSlice:
    """One completed champion round trip, normalized for evidence math."""

    day: date
    side: str  # BUY | SELL
    regime_label: str
    realized_pnl: Decimal
    costs: Decimal
    win: bool
    version: str = "model_0"

    def to_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "side": self.side,
            "regime_label": self.regime_label,
            "realized_pnl": str(self.realized_pnl),
            "costs": str(self.costs),
            "win": self.win,
            "version": self.version,
        }

    @staticmethod
    def from_dict(payload: Mapping[str, object]) -> "ChampionTradeSlice":
        return ChampionTradeSlice(
            day=date.fromisoformat(str(payload["day"])),
            side=str(payload["side"]),
            regime_label=str(payload["regime_label"]),
            realized_pnl=Decimal(str(payload["realized_pnl"])),
            costs=Decimal(str(payload["costs"])),
            win=bool(payload["win"]),
            version=str(payload.get("version", "model_0")),
        )


def experience_to_slices(
    records: Sequence[ExperienceRecord],
) -> list[ChampionTradeSlice]:
    """Convert completed experience records into evidence slices.

    Only ``complete`` records with a realized outcome become evidence; open /
    pending / no-trade records never leak into the outcome math. The champion
    version that produced each trade is carried along so trailing evidence can
    be scoped to the algorithm actually in use.
    """
    slices: list[ChampionTradeSlice] = []
    for record in records:
        if record.outcome is None:
            continue
        realized = record.outcome.realized_pnl
        outcome = record.outcome.outcome
        if outcome is not None and outcome not in (
            OutcomeKind.WIN,
            OutcomeKind.LOSS,
            OutcomeKind.BREAKEVEN,
        ):
            continue
        slices.append(
            ChampionTradeSlice(
                day=record.decision.decision_timestamp.date(),
                side=record.decision.signal.value
                if record.decision.signal is not None
                else Signal.HOLD.value,
                regime_label=record.decision.regime_label or "UNKNOWN",
                realized_pnl=realized,
                costs=record.outcome.total_costs,
                win=bool(outcome is OutcomeKind.WIN),
                version=record.decision.strategy_version,
            )
        )
    return slices


def _stats_from_slices(slices: Iterable[ChampionTradeSlice]) -> tuple[ChampionWindowStats, Decimal]:
    by_bucket: dict[tuple[str, str], list[ChampionTradeSlice]] = {}
    costs = _ZERO
    totals_trades = 0
    totals_wins = 0
    totals_pnl = _ZERO
    for trade in slices:
        totals_trades += 1
        totals_wins += 1 if trade.win else 0
        totals_pnl += trade.realized_pnl
        costs += trade.costs
        by_bucket.setdefault((trade.regime_label, trade.side), []).append(trade)
    buckets: dict[tuple[str, str], WindowSlice] = {}
    for (label, side), batch in by_bucket.items():
        wins = sum(1 for t in batch if t.win)
        buckets[(label, side)] = WindowSlice(
            trades=len(batch),
            wins=wins,
            realized_pnl=sum((t.realized_pnl for t in batch), _ZERO),
        )
    stats = ChampionWindowStats(
        by_regime_side=buckets,
        total=WindowSlice(trades=totals_trades, wins=totals_wins, realized_pnl=totals_pnl),
    )
    return stats, costs


class EvidenceAggregator:
    """Append-only accumulator of completed champion evidence, by day."""

    def __init__(self) -> None:
        self._by_day: dict[str, list[ChampionTradeSlice]] = {}

    def add_day(
        self, day: date, slices: Sequence[ChampionTradeSlice]
    ) -> None:
        key = day.isoformat()
        existing = self._by_day.setdefault(key, [])
        existing.extend(slices)

    def restore_days(self, payload: Mapping[str, object]) -> None:
        """Rebuild from a resume checkpoint (deterministic)."""
        self._by_day = {}
        for day_key, raw in payload.items():
            raw_list = raw if isinstance(raw, (list, tuple)) else []
            self._by_day[str(day_key)] = [
                ChampionTradeSlice.from_dict(item) if isinstance(item, dict) else item
                for item in raw_list
            ]

    def dump_days(self) -> dict[str, list[dict[str, object]]]:
        return {
            day: [s.to_dict() for s in slices]
            for day, slices in sorted(self._by_day.items())
        }

    def days_cached(self) -> tuple[date, ...]:
        return tuple(sorted(date.fromisoformat(d) for d in self._by_day))

    def _within(
        self, end: date, window_days: int, version: str | None = None
    ) -> list[ChampionTradeSlice]:
        floor = end
        if window_days and window_days > 0:
            from datetime import timedelta

            floor = end - timedelta(days=window_days)
        out: list[ChampionTradeSlice] = []
        for day, slices in self._by_day.items():
            day_date = date.fromisoformat(day)
            if day_date < floor:
                continue
            if day_date > end:
                continue
            out.extend(slices)
        if version is not None:
            out = [s for s in out if s.version == version]
        return out

    def window_stats(
        self,
        end: date,
        window_days: int,
        version: str | None = None,
    ) -> tuple[ChampionWindowStats, Decimal]:
        """Trailing-window champion evidence as of ``end`` (never looks ahead)."""
        return _stats_from_slices(self._within(end, window_days, version))

    def totals(self) -> tuple[ChampionWindowStats, Decimal]:
        return _stats_from_slices(
            slice_
            for day in sorted(self._by_day)
            for slice_ in self._by_day[day]
        )


def detect_problem(
    window_stats: ChampionWindowStats,
    window_costs: Decimal,
    totals: ChampionWindowStats,
    totals_costs: Decimal,
    config: WalkForwardConfig,
) -> dict[str, str]:
    """Nightly failure-pattern review; the single most severe observed pattern.

    Returns ``{"pattern": ..., "description": ...}`` with ``pattern == "none"``
    when no repeatable problem is observed. Deterministic; `total_` statistics
    refer to the whole processed champion history so far, window_ statistics to
    the trailing review window.
    """
    minimum = config.min_hypothesis_regime_trades

    if (
        totals.total.trades >= minimum
        and totals_costs > _ZERO
        and totals.total.realized_pnl < _ZERO
        and abs(totals.total.realized_pnl) <= totals_costs
    ):
        return {
            "pattern": "cost_dominated",
            "description": (
                "champion net P&L is at or below its transaction frictions over "
                f"{totals.total.trades} completed trades (net "
                f"{_money(totals.total.realized_pnl)} vs costs "
                f"{_money(totals_costs)}) -- the edge does not clear frictions"
            ),
        }

    if totals.total.trades >= minimum and totals.total.realized_pnl < _ZERO:
        return {
            "pattern": "negative_expectancy",
            "description": (
                f"champion expectancy is negative over {totals.total.trades} "
                f"completed trades (net {_money(totals.total.realized_pnl)} "
                f"after costs of {_money(totals_costs)})"
            ),
        }

    worst: tuple[str, WindowSlice] | None = None
    for (label, side), slice_ in window_stats.by_regime_side.items():
        if slice_.trades < minimum:
            continue
        if slice_.realized_pnl >= _ZERO:
            continue
        if worst is None or slice_.realized_pnl < worst[1].realized_pnl:
            worst = (f"{label}|{side}", slice_)
    if worst is not None:
        label, slice_ = worst
        return {
            "pattern": "regime_side_losses",
            "description": (
                f"{label} champion trades lose {_money(slice_.realized_pnl)} "
                f"over {slice_.trades} trades in the trailing "
                f"{config.nightly_review_window_days}-day window"
            ),
        }

    return {"pattern": "none", "description": "no repeatable problem pattern observed"}


def _money(value: Decimal) -> str:
    return format(value, "f")


__all__ = [
    "CATALOG",
    "ChampionTradeSlice",
    "ChampionWindowStats",
    "EvidenceAggregator",
    "WindowSlice",
    "detect_problem",
    "experience_to_slices",
    "select_challengers",
]