"""Outcome analyzer for adaptive learning (WS 7.10).

Turns completed experience records into a deterministic, structured summary of
*what actually happened*: aggregate P&L, win rate, expectancy, profit factor,
and per-regime / per-signal / per-advisory breakdowns. It deliberately makes no
prescription — the candidate generator in :mod:`candidates` proposes hypotheses
only when the evidence thresholds are met.

Evidence discipline (per PROJECT_PLAN §17f):
* only ``complete`` records (full round-trip outcome) feed the outcome math;
* ``pending_outcome`` / ``no_trade`` records are counted but excluded — their
  final result is not known yet / there was no trade;
* a single loss (or win) never changes anything on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from fno_ai_paper_trading.experience.enums import AdvisoryUsage, OutcomeKind
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.models.enums import Signal

_ZERO = Decimal("0")


@dataclass(frozen=True)
class PerSignalStats:
    """Outcome statistics for one decision signal."""

    signal: Signal
    completed: int
    wins: int
    win_rate: Decimal | None
    realized_pnl: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "signal": self.signal.value,
            "completed": self.completed,
            "wins": self.wins,
            "win_rate": str(self.win_rate) if self.win_rate is not None else None,
            "realized_pnl": str(self.realized_pnl),
        }


@dataclass(frozen=True)
class PerRegimeStats:
    """Outcome statistics for one market-regime label."""

    regime_label: str
    completed: int
    wins: int
    win_rate: Decimal | None
    realized_pnl: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "regime_label": self.regime_label,
            "completed": self.completed,
            "wins": self.wins,
            "win_rate": str(self.win_rate) if self.win_rate is not None else None,
            "realized_pnl": str(self.realized_pnl),
        }


@dataclass(frozen=True)
class PerAdvisoryStats:
    """Outcome statistics for an advisory-usage bucket (advisory present)."""

    usage: AdvisoryUsage
    completed: int
    wins: int
    win_rate: Decimal | None
    realized_pnl: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "usage": self.usage.value,
            "completed": self.completed,
            "wins": self.wins,
            "win_rate": str(self.win_rate) if self.win_rate is not None else None,
            "realized_pnl": str(self.realized_pnl),
        }


@dataclass(frozen=True)
class OutcomeAnalysis:
    """Deterministic aggregate outcome summary over completed records."""

    completed: int
    wins: int
    losses: int
    breakeven: int
    pending_outcome: int
    no_trade: int
    gross_win: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    total_costs: Decimal
    win_rate: Decimal | None
    expectancy: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None
    per_regime: tuple[PerRegimeStats, ...]
    per_signal: tuple[PerSignalStats, ...]
    per_advisory: tuple[PerAdvisoryStats, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "completed": self.completed,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "pending_outcome": self.pending_outcome,
            "no_trade": self.no_trade,
            "gross_win": str(self.gross_win),
            "gross_loss": str(self.gross_loss),
            "net_pnl": str(self.net_pnl),
            "total_costs": str(self.total_costs),
            "win_rate": str(self.win_rate) if self.win_rate is not None else None,
            "expectancy": str(self.expectancy) if self.expectancy is not None else None,
            "avg_win": str(self.avg_win) if self.avg_win is not None else None,
            "avg_loss": str(self.avg_loss) if self.avg_loss is not None else None,
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "per_regime": [stats.to_dict() for stats in self.per_regime],
            "per_signal": [stats.to_dict() for stats in self.per_signal],
            "per_advisory": [stats.to_dict() for stats in self.per_advisory],
        }


def analyze_outcomes(records: Sequence[ExperienceRecord]) -> OutcomeAnalysis:
    """Summarize completed outcomes (evidence-only, never prescriptive)."""
    completed = [r for r in records if r.outcome is not None]
    pending = sum(1 for r in records if r.status == "pending_outcome")
    no_trade = sum(1 for r in records if r.status == "no_trade")

    wins = [r for r in completed if r.outcome.outcome is OutcomeKind.WIN]
    losses = [r for r in completed if r.outcome.outcome is OutcomeKind.LOSS]
    breakeven = [r for r in completed if r.outcome.outcome is OutcomeKind.BREAKEVEN]

    gross_win = _sum(r.outcome.realized_pnl for r in wins)
    gross_loss = _sum(-r.outcome.realized_pnl for r in losses)
    net_pnl = _sum(r.outcome.realized_pnl for r in completed)
    total_costs = _sum(r.outcome.total_costs for r in completed)

    win_rate = _ratio(len(wins), len(completed))
    expectancy = _ratio(net_pnl, len(completed))
    avg_win = _ratio(gross_win, len(wins)) if wins else None
    avg_loss = _ratio(-gross_loss, len(losses)) if losses else None
    profit_factor = _ratio(gross_win, gross_loss) if gross_loss > 0 else None

    return OutcomeAnalysis(
        completed=len(completed),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(breakeven),
        pending_outcome=pending,
        no_trade=no_trade,
        gross_win=gross_win,
        gross_loss=gross_loss,
        net_pnl=net_pnl,
        total_costs=total_costs,
        win_rate=win_rate,
        expectancy=expectancy,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        per_regime=_group_regime(completed),
        per_signal=_group_signal(completed),
        per_advisory=_group_advisory(completed),
    )


def _group_regime(completed: list[ExperienceRecord]) -> tuple[PerRegimeStats, ...]:
    grouped: dict[str, list[ExperienceRecord]] = {}
    for record in completed:
        grouped.setdefault(record.decision.regime_label or "UNKNOWN", []).append(record)
    build: list[PerRegimeStats] = []
    for label in sorted(grouped):
        batch = grouped[label]
        wins = sum(1 for r in batch if r.outcome.outcome is OutcomeKind.WIN)
        build.append(
            PerRegimeStats(
                regime_label=label,
                completed=len(batch),
                wins=wins,
                win_rate=_ratio(wins, len(batch)),
                realized_pnl=_sum(r.outcome.realized_pnl for r in batch),
            )
        )
    return tuple(build)


def _group_signal(completed: list[ExperienceRecord]) -> tuple[PerSignalStats, ...]:
    grouped: dict[Signal, list[ExperienceRecord]] = {}
    for record in completed:
        grouped.setdefault(record.decision.signal, []).append(record)
    build: list[PerSignalStats] = []
    for signal in sorted(grouped, key=lambda s: s.value):
        batch = grouped[signal]
        wins = sum(1 for r in batch if r.outcome.outcome is OutcomeKind.WIN)
        build.append(
            PerSignalStats(
                signal=signal,
                completed=len(batch),
                wins=wins,
                win_rate=_ratio(wins, len(batch)),
                realized_pnl=_sum(r.outcome.realized_pnl for r in batch),
            )
        )
    return tuple(build)


def _group_advisory(completed: list[ExperienceRecord]) -> tuple[PerAdvisoryStats, ...]:
    grouped: dict[AdvisoryUsage, list[ExperienceRecord]] = {}
    for record in completed:
        if record.advisory is None or not record.advisory.present:
            continue
        grouped.setdefault(record.advisory.usage, []).append(record)
    build: list[PerAdvisoryStats] = []
    for usage in (AdvisoryUsage.ACCEPTED, AdvisoryUsage.OVERRIDDEN, AdvisoryUsage.REJECTED):
        batch = grouped.get(usage)
        if not batch:
            continue
        wins = sum(1 for r in batch if r.outcome.outcome is OutcomeKind.WIN)
        build.append(
            PerAdvisoryStats(
                usage=usage,
                completed=len(batch),
                wins=wins,
                win_rate=_ratio(wins, len(batch)),
                realized_pnl=_sum(r.outcome.realized_pnl for r in batch),
            )
        )
    return tuple(build)


def _sum(values: Iterable[Decimal]) -> Decimal:
    total = _ZERO
    for value in values:
        total += value
    return total


def _ratio(numerator: int | Decimal, denominator) -> Decimal | None:
    if denominator is None or denominator <= 0:
        return None
    return Decimal(numerator) / Decimal(denominator)