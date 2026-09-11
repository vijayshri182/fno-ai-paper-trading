"""Candidate generation for adaptive learning (WS 7.10).

This is the *analysis* stage of §17f — it produces descriptive hypotheses
("learn WHY, do not react"). Candidates are inert data:

* they are NEVER applied to the strategy, risk controls, or paper broker;
* a candidate is only emitted once the evidence thresholds are met, so a single
  win or loss can never move the algorithm (``LOSS → immediately modify
  algorithm`` is forbidden by §17f.2);
* every candidate is explicitly a *hypothesis that still must clear the WS 7.11
  champion/challenger evaluation and the WS 7.12 promotion gate*.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from fno_ai_paper_trading.experience.enums import AdvisoryUsage
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.learning.outcome import (
    OutcomeAnalysis,
    PerAdvisoryStats,
    analyze_outcomes,
)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CandidateConfig:
    """Evidence thresholds that gate candidate generation."""

    min_completed: int = 20
    min_completed_per_regime: int = 10
    min_completed_per_advisory_bucket: int = 10
    win_rate_delta: Decimal = Decimal("0.05")


@dataclass(frozen=True)
class Candidate:
    """An inert learning hypothesis. Never applied by this module."""

    kind: str
    title: str
    hypothesis: str
    evidence: Mapping[str, str]
    created_at: datetime
    generator: str = "ws7.10-candidate"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "title": self.title,
            "hypothesis": self.hypothesis,
            "evidence": dict(self.evidence),
            "created_at": self.created_at.isoformat(),
            "generator": self.generator,
        }


@dataclass(frozen=True)
class GenerationResult:
    """The complete outcome of a candidate-generation pass."""

    summary: OutcomeAnalysis
    candidates: tuple[Candidate, ...]
    insufficient_reasons: tuple[str, ...]

    @property
    def emitted(self) -> bool:
        return len(self.candidates) > 0


class CandidateGenerator:
    """Generates descriptive improvement hypotheses from completed evidence."""

    def __init__(
        self,
        config: CandidateConfig | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._config = config or CandidateConfig()
        self._now = now

    def generate(self, records: Sequence[ExperienceRecord]) -> GenerationResult:
        summary = analyze_outcomes(records)
        reasons: list[str] = []
        candidates: list[Candidate] = []

        if summary.completed < self._config.min_completed:
            reasons.append(
                f"completed trades {summary.completed} below min_completed "
                f"{self._config.min_completed}"
            )
            return GenerationResult(summary=summary, candidates=(), insufficient_reasons=tuple(reasons))

        for stats in summary.per_regime:
            if stats.win_rate is None:
                continue
            if stats.completed < self._config.min_completed_per_regime:
                reasons.append(
                    f"regime {stats.regime_label!r} has {stats.completed} completed trades "
                    f"(below min_completed_per_regime {self._config.min_completed_per_regime})"
                )
                continue
            delta = stats.win_rate - (summary.win_rate or _ZERO)
            if delta >= self._config.win_rate_delta:
                candidates.append(
                    Candidate(
                        kind="regime_focus",
                        title=f"Regime {stats.regime_label} outperforms the average",
                        hypothesis=(
                            f"{stats.regime_label} wins {stats.win_rate:.2f} of trades vs the "
                            f"overall {summary.win_rate:.2f} (delta +{delta:.2f}) over "
                            f"{stats.completed} trades — hypothesis: evaluate a regime filter "
                            "that retains this regime and suppresses the underperformers "
                            "(WS 7.11 only; nothing is applied)."
                        ),
                        evidence={
                            "regime_label": stats.regime_label,
                            "completed": str(stats.completed),
                            "wins": str(stats.wins),
                            "regime_win_rate": str(stats.win_rate),
                            "overall_win_rate": str(summary.win_rate),
                            "delta": f"+{delta!s}",
                            "realized_pnl": str(stats.realized_pnl),
                        },
                        created_at=self._now(),
                    )
                )
            elif delta <= -self._config.win_rate_delta:
                candidates.append(
                    Candidate(
                        kind="regime_focus",
                        title=f"Regime {stats.regime_label} underperforms the average",
                        hypothesis=(
                            f"{stats.regime_label} wins {stats.win_rate:.2f} of trades vs the "
                            f"overall {summary.win_rate:.2f} (delta {delta:.2f}) over "
                            f"{stats.completed} trades — hypothesis: evaluate suppressing this "
                            "regime (HOLD bias) as a WS 7.11 challenger; nothing is applied."
                        ),
                        evidence={
                            "regime_label": stats.regime_label,
                            "completed": str(stats.completed),
                            "wins": str(stats.wins),
                            "regime_win_rate": str(stats.win_rate),
                            "overall_win_rate": str(summary.win_rate),
                            "delta": str(delta),
                            "realized_pnl": str(stats.realized_pnl),
                        },
                        created_at=self._now(),
                    )
                )

        accepted = _bucket(summary.per_advisory, AdvisoryUsage.ACCEPTED)
        rejected = _pooled_non_accepted(summary.per_advisory)
        if accepted is not None and rejected is not None:
            if accepted.completed < self._config.min_completed_per_advisory_bucket or rejected.completed < self._config.min_completed_per_advisory_bucket:
                if accepted.completed < self._config.min_completed_per_advisory_bucket:
                    reasons.append(
                        f"accepted-advisory bucket has {accepted.completed} completed trades "
                        f"(below min_completed_per_advisory_bucket {self._config.min_completed_per_advisory_bucket})"
                    )
                else:
                    reasons.append(
                        f"rejected/overridden advisory bucket has {rejected.completed} completed "
                        f"trades (below min_completed_per_advisory_bucket {self._config.min_completed_per_advisory_bucket})"
                    )
            else:
                delta = accepted.win_rate - rejected.win_rate if accepted.win_rate is not None and rejected.win_rate is not None else None
                if delta is not None and abs(delta) >= self._config.win_rate_delta:
                    candidates.append(
                        Candidate(
                            kind="advisory_alignment",
                            title="Advisory acceptance shows a meaningful outcome split",
                            hypothesis=(
                                f"accepted recommendations win at {accepted.win_rate:.2f} vs "
                                f"{rejected.win_rate:.2f} for rejected/overridden (delta {delta:+.2f}) "
                                f"over {accepted.completed + rejected.completed} trades — hypothesis: "
                                "evaluate an advisory-alignment challenger (WS 7.11); nothing is applied."
                            ),
                            evidence={
                                "accepted_completed": str(accepted.completed),
                                "rejected_completed": str(rejected.completed),
                                "accepted_win_rate": str(accepted.win_rate),
                                "rejected_win_rate": str(rejected.win_rate),
                                "delta": f"{delta:+.2f}",
                            },
                            created_at=self._now(),
                        )
                    )
        elif accepted is not None or rejected is not None:
            reasons.append(
                "advisory buckets need both accepted and rejected/overridden evidence"
            )

        return GenerationResult(
            summary=summary,
            candidates=tuple(candidates),
            insufficient_reasons=tuple(reasons),
        )


def _bucket(stats: Sequence[PerAdvisoryStats], usage: AdvisoryUsage) -> PerAdvisoryStats | None:
    return next((s for s in stats if s.usage is usage), None)


def _pooled_non_accepted(stats: Sequence[PerAdvisoryStats]) -> PerAdvisoryStats | None:
    pooled = [s for s in stats if s.usage in (AdvisoryUsage.REJECTED, AdvisoryUsage.OVERRIDDEN)]
    if not pooled:
        return None
    completed = sum(s.completed for s in pooled)
    wins = sum(s.wins for s in pooled)
    pnl = sum((s.realized_pnl for s in pooled), _ZERO)
    return PerAdvisoryStats(
        usage=AdvisoryUsage.REJECTED,
        completed=completed,
        wins=wins,
        win_rate=(Decimal(wins) / Decimal(completed)) if completed else None,
        realized_pnl=pnl,
    )