"""Adaptive learning — outcome analysis, candidate generation & feedback loop.

Evidence-only by design: this package reads completed experience records and
produces deterministic summaries plus inert improvement *hypotheses* (WS 7.10),
and drives the continuous feedback loop over day batches (WS 7.13). Nothing in
here can change a strategy, bypass a risk control, or execute an order; the
hard safety boundary of §17f.7 is enforced structurally (the candidate objects
are data and the generating code has no dependency on the strategy, risk,
sizing, stop-loss, broker, or portfolio modules).
"""
from __future__ import annotations

from fno_ai_paper_trading.learning.candidates import (
    Candidate,
    CandidateConfig,
    CandidateGenerator,
    GenerationResult,
)
from fno_ai_paper_trading.learning.capture import CaptureResult, capture_from_day_bars, pair_round_trips
from fno_ai_paper_trading.learning.loop import (
    LOOP_DISCLAIMER,
    CycleVerdict,
    LearningCycleResult,
    LearningLoop,
    LearningLoopConfig,
    cycle_result_to_html,
    default_strategy_factories,
    resolve_active_champion,
)
from fno_ai_paper_trading.learning.outcome import (
    OutcomeAnalysis,
    PerAdvisoryStats,
    PerRegimeStats,
    PerSignalStats,
    analyze_outcomes,
)

__all__ = [
    "Candidate",
    "CandidateConfig",
    "CandidateGenerator",
    "CaptureResult",
    "CycleVerdict",
    "GenerationResult",
    "LearningCycleResult",
    "LearningLoop",
    "LearningLoopConfig",
    "LOOP_DISCLAIMER",
    "OutcomeAnalysis",
    "PerAdvisoryStats",
    "PerRegimeStats",
    "PerSignalStats",
    "analyze_outcomes",
    "capture_from_day_bars",
    "cycle_result_to_html",
    "default_strategy_factories",
    "pair_round_trips",
    "resolve_active_champion",
]