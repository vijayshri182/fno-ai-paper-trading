"""Continuous feedback / learning loop orchestrator (WS 7.13).

Implements the §17f.3 loop as a repeatable, evidence-only program over day
batches:

    -> Paper Trade (champion replay)
    -> Capture Decision + Execution + Outcome   (WS 7.13 capture bridge)
    -> Store Experience                          (WS 7.9 ExperienceStore)
    -> Analyze Outcome / Generate Candidate       (WS 7.10)
    -> Champion vs Challenger                     (WS 7.11)
    -> Promotion Gate                             (WS 7.12)
    -> Approved Model (registry) -> Paper Trading (next cycle's champion)
    -> Repeat

Each :meth:`LearningLoop.run_cycle` is one pass over a batch of day-bars. The
champion of a cycle comes from the caller (or, with ``resolve_active_champion``,
from the version registry — so a candidate promoted in cycle *k* becomes cycle
*k+1*'s champion). A promotion is only ever bookkeeping: it writes an evidence
trail into the append-only registry. Nothing in this module imports broker,
portfolio, risk, sizing, or execution code, and it can never enable live
trading or weaken risk controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Sequence

from fno_ai_paper_trading.evaluation.champion_challenger import ChampionChallenger
from fno_ai_paper_trading.evaluation.five_year import DayBars, PeriodSplitConfig
from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
from fno_ai_paper_trading.experience.enums import ExperienceSourceType
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.learning.capture import CaptureResult, capture_from_day_bars
from fno_ai_paper_trading.learning.candidates import (
    Candidate,
    CandidateGenerator,
    GenerationResult,
)
from fno_ai_paper_trading.promotion.gate import PromotionCriteria, PromotionGate
from fno_ai_paper_trading.promotion.registry import ModelVersion, VersionRegistry
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.research.report import CSS, escape, table
from fno_ai_paper_trading.strategies import (
    MovingAverageCrossStrategy,
    RegimeFilteredMovingAverageCross,
)
from fno_ai_paper_trading.strategies.base import Strategy

LOOP_DISCLAIMER = (
    "Continuous feedback loop (WS 7.13). Experience capture, candidate "
    "hypotheses, champion/challenger evidence and promotions are paper-only "
    "bookkeeping: no live order is ever placed, no risk control is ever "
    "weakened, and promotion never enables live trading."
)


@dataclass(frozen=True)
class LearningLoopConfig:
    """Deterministic settings for one loop instance."""

    split: PeriodSplitConfig = field(default_factory=PeriodSplitConfig)
    criteria: PromotionCriteria = field(default_factory=PromotionCriteria)
    timeframe: str = "1m"
    strategy_version: str = "1.0"
    feature_version: str = "1.0"
    source: ExperienceSourceType = ExperienceSourceType.HISTORICAL_REPLAY

    def __post_init__(self) -> None:
        if not isinstance(self.split, PeriodSplitConfig):
            raise TypeError("split must be a PeriodSplitConfig")
        if not isinstance(self.criteria, PromotionCriteria):
            raise TypeError("criteria must be a PromotionCriteria")


@dataclass(frozen=True)
class CycleVerdict:
    """Gate outcome for one challenger inside a learning cycle."""

    challenger_name: str
    decision: str  # PROMOTE | REJECT
    promoted_version: str | None = None
    beats_champion: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class LearningCycleResult:
    """Deterministic record of one learning-loop cycle."""

    cycle: int
    title: str
    champion_name: str
    day_count: int
    period_counts: Mapping[str, int]
    captured_records: int
    appended: int
    duplicates: int
    hypotheses: tuple[Candidate, ...]
    hypothesis_blockers: tuple[str, ...]
    verdicts: tuple[CycleVerdict, ...]
    promotion: bool
    created_at: str
    disclaimer: str = LOOP_DISCLAIMER

    @property
    def promoted_versions(self) -> tuple[str, ...]:
        return tuple(
            verdict.promoted_version
            for verdict in self.verdicts
            if verdict.promoted_version is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cycle": self.cycle,
            "title": self.title,
            "champion_name": self.champion_name,
            "day_count": self.day_count,
            "period_counts": dict(self.period_counts),
            "captured_records": self.captured_records,
            "appended": self.appended,
            "duplicates": self.duplicates,
            "hypotheses": [candidate.to_dict() for candidate in self.hypotheses],
            "hypothesis_blockers": list(self.hypothesis_blockers),
            "verdicts": [
                {
                    "challenger_name": verdict.challenger_name,
                    "decision": verdict.decision,
                    "promoted_version": verdict.promoted_version,
                    "beats_champion": verdict.beats_champion,
                    "reasons": list(verdict.reasons),
                }
                for verdict in self.verdicts
            ],
            "promotion": self.promotion,
            "created_at": self.created_at,
            "disclaimer": self.disclaimer,
        }


def default_strategy_factories() -> dict[str, Callable[[], Strategy]]:
    """Factories for the implemented strategies (WS 7.12/7.11 names)."""
    return {
        "moving_average_cross": lambda: MovingAverageCrossStrategy(fast=5, slow=21),
        "regime_filtered_ma_cross": lambda: RegimeFilteredMovingAverageCross(
            allowed_trends=("UP",)
        ),
    }


def resolve_active_champion(
    registry: VersionRegistry,
    factories: Mapping[str, Callable[[], Strategy]],
) -> Strategy:
    """Resolve the registry's ACTIVE champion into a runnable strategy.

    Raises when the registry has no champion or when no factory exists for the
    active model's strategy name — the loop never guesses and never silently
    falls back to the baseline.
    """
    version = registry.active
    if version is None:
        raise ValueError("version registry has no active champion")
    builder = factories.get(version.strategy_name)
    if builder is None:
        raise ValueError(
            f"no strategy factory for active model {version.strategy_name!r}"
        )
    return builder()


def _params_for(strategy: Strategy) -> dict[str, object]:
    params: dict[str, object] = {}
    for name in ("fast", "slow", "allowed_trends", "trend_threshold_pct"):
        if hasattr(strategy, name):
            value = getattr(strategy, name)
            if isinstance(value, (list, tuple)):
                params[name] = list(value)
            else:
                params[name] = value
    return params


def resolve_champion_from(
    registry: VersionRegistry,
    strategies: Mapping[str, Callable[[], Strategy]] | None = None,
) -> Strategy | None:
    """Compatibility helper: returns ``None`` instead of raising on no champion."""
    try:
        return resolve_active_champion(registry, strategies or default_strategy_factories())
    except ValueError:
        return None


class LearningLoop:
    """Drives a full feedback cycle per call over a batch of day-bars."""

    def __init__(
        self,
        *,
        store=None,
        registry: VersionRegistry | None = None,
        config: LearningLoopConfig | None = None,
        evaluator: HistoricalEvaluator | None = None,
        detector: RegimeDetector | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.store = store
        self.registry = registry
        self.config = config or LearningLoopConfig()
        self._evaluator = evaluator or HistoricalEvaluator()
        self._detector = detector
        self._now = now

    # ------------------------------------------------------------------ capture

    def capture(self, days: Sequence[DayBars], strategy: Strategy) -> CaptureResult:
        """Paper Trade -> Store Experience for ``strategy`` over ``days``."""
        return capture_from_day_bars(
            days,
            strategy,
            self._evaluator,
            strategy_version=self.config.strategy_version,
            timeframe=self.config.timeframe,
            feature_version=self.config.feature_version,
            source=self.config.source,
            detector=self._detector,
        )

    # -------------------------------------------------------------------- cycle

    def run_cycle(
        self,
        days: Sequence[DayBars],
        *,
        champion: Strategy,
        candidates: Sequence[Strategy],
        cycle: int = 0,
        title: str | None = None,
    ) -> LearningCycleResult:
        """Run one complete paper-only feedback loop stage over ``days``."""
        ordered = sorted(days, key=lambda item: item.day)
        runner = ChampionChallenger(
            config=self._evaluator.config,
            split=self.config.split,
            evaluator=self._evaluator,
        )
        comparison = runner.run_days(
            ordered, champion, list(candidates), title=title or f"learning-loop-cycle-{cycle}"
        )

        captured = self.capture(ordered, champion)
        appended = 0
        duplicates = 0
        if self.store is not None:
            merged = self.store.merge(captured.records)
            appended = merged.appended
            duplicates = merged.duplicates

        generation: GenerationResult | None = None
        if captured.records:
            generation = CandidateGenerator().generate(list(self.store.all()) if self.store is not None else captured.records)

        verdicts: list[CycleVerdict] = []
        promotion = False
        for candidate in candidates:
            gate = PromotionGate()
            verdict = gate.evaluate_from_comparison(
                comparison, candidate.name, criteria=self.config.criteria
            )
            promoted_version: str | None = None
            if verdict.promoted and self.registry is not None and self.registry.active is not None:
                version = self.registry.promote(
                    strategy_name=candidate.name,
                    strategy_params=_params_for(candidate),
                    feature_version=self.config.feature_version,
                    description=f"promoted by learning loop cycle {cycle}",
                    evidence=dict(verdict.evidence),
                )
                promoted_version = version.version_id
                promotion = True
            verdicts.append(
                CycleVerdict(
                    challenger_name=candidate.name,
                    decision=verdict.decision,
                    promoted_version=promoted_version,
                    beats_champion=verdict.promoted,
                    reasons=tuple(verdict.reasons),
                )
            )

        return LearningCycleResult(
            cycle=cycle,
            title=title or f"learning-loop-cycle-{cycle}",
            champion_name=champion.name,
            day_count=len(ordered),
            period_counts=dict(comparison.period_counts),
            captured_records=captured.round_trips,
            appended=appended,
            duplicates=duplicates,
            hypotheses=tuple(generation.candidates) if generation is not None else (),
            hypothesis_blockers=(
                tuple(generation.insufficient_reasons) if generation is not None else ()
            ),
            verdicts=tuple(verdicts),
            promotion=promotion,
            created_at=self._now().isoformat(timespec="seconds"),
        )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def cycle_result_to_html(result: LearningCycleResult, title: str | None = None) -> str:
    """Self-contained HTML page for one learning-loop cycle."""
    verdict_rows = [
        [
            escape(v.challenger_name),
            escape(v.decision),
            v.promoted_version or "",
            "yes" if v.beats_champion else "no",
        ]
        for v in result.verdicts
    ]
    hypothesis_rows = [
        [escape(h.kind), escape(h.title), escape(h.hypothesis)] for h in result.hypotheses
    ]
    blocks = [
        f"<header><h1>{escape(title or result.title)} — Learning loop cycle {result.cycle}</h1>"
        f"<p>Champion {escape(result.champion_name)} &middot; {result.day_count} day(s) "
        f"&middot; captured {result.captured_records} trade(s), appended "
        f"{result.appended}, duplicates {result.duplicates} &middot; generated "
        f"{escape(result.created_at)}</p></header>",
        f"<div class='note'>{escape(result.disclaimer)}</div>",
        f"<section><h2>Promotion gate verdicts</h2>"
        + table(["Challenger", "Decision", "Promoted version", "Beats champion"], verdict_rows)
        + "</section>",
        f"<section><h2>Generated hypotheses</h2>"
        + (
            table(["Kind", "Title", "Hypothesis"], hypothesis_rows)
            if hypothesis_rows
            else "<p>No candidate hypothesis cleared the evidence thresholds "
            f"({'; '.join(result.hypothesis_blockers) or 'no completed trades'}).</p>"
        )
        + "</section>",
    ]
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
        f"<body><div class='wrap'>{''.join(blocks)}"
        f"<div class='footer'>Continuous feedback loop &middot; paper trading only &middot; evidence, not live orders</div>"
        f"</div></body></html>"
    )