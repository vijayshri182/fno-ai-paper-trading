"""Phase 11 — options paper-trading simulation.

A deterministic, auditable, strictly-paper-only options trade-lifecycle layer
built on the Phases 5-10 pipeline: explicit entry events carrying regime,
selection, quality and risk results are gate- and identity-validated, then
simulated through an explicit lifecycle using the existing paper broker,
session policy and hashed persistence.

Safety: never a real/sandbox/live order; no ``execution.*`` imports; no
credentials; no wall-clock reads (an injected ``now_fn`` is required); no
options stop-loss invented (Phase 10 ``STOP_MODEL_NOT_YET_DEFINED`` surfaces as
an unavailable attempt); fill prices only ever derive from observed market data.
"""
from fno_ai_paper_trading.paper_track.options_paper.engine import (
    OptionsPaperConfig,
    OptionsPaperEngine,
)
from fno_ai_paper_trading.paper_track.options_paper.fill_model import (
    FILL_MODEL_VERSION,
    MarkBasis,
    fill_reference,
    mark_reference,
    premium_exposure,
)
from fno_ai_paper_trading.paper_track.options_paper.models import (
    EntryEvent,
    EntryFill,
    ExitEvent,
    ExitFill,
    Financials,
    LIFECYCLE_TRANSITIONS,
    LifecyclePhase,
    LifecycleRecord,
    MarkEvent,
    MarkToMarket,
    Reconciliation,
    Step,
    TERMINAL_PHASES,
)
from fno_ai_paper_trading.paper_track.options_paper.persistence import (
    OptionsPaperStore,
    OptionsPaperStoreError,
)
from fno_ai_paper_trading.paper_track.options_paper.report import (
    build_cumulative_report,
    build_daily_report,
    record_report,
    report_fingerprint,
)

__all__ = [
    "LIFECYCLE_TRANSITIONS",
    "TERMINAL_PHASES",
    "FILL_MODEL_VERSION",
    "MarkBasis",
    "LifecyclePhase",
    "EntryEvent",
    "EntryFill",
    "ExitEvent",
    "ExitFill",
    "Financials",
    "LifecycleRecord",
    "MarkEvent",
    "MarkToMarket",
    "Reconciliation",
    "Step",
    "OptionsPaperConfig",
    "OptionsPaperEngine",
    "OptionsPaperStore",
    "OptionsPaperStoreError",
    "fill_reference",
    "mark_reference",
    "premium_exposure",
    "record_report",
    "build_daily_report",
    "build_cumulative_report",
    "report_fingerprint",
]