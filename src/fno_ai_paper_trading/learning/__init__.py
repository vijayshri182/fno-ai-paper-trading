"""Adaptive learning — outcome analysis & candidate generation (WS 7.10).

Evidence-only by design: this package reads completed experience records and
produces deterministic summaries plus inert improvement *hypotheses*. Nothing
in here can change a strategy, bypass a risk control, or execute an order; the
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
    "GenerationResult",
    "OutcomeAnalysis",
    "PerAdvisoryStats",
    "PerRegimeStats",
    "PerSignalStats",
    "analyze_outcomes",
]