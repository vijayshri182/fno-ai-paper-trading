"""Phase 12 — leakage-safe options research and OOS evaluation framework.

Composes the existing infrastructure (frozen ``options_data_contract`` schema,
Phase 5 option-chain models/validation, Phase 8-10 gates, Phase 11 paper
simulation, fresh-OOS/protected-OOS registries) into a reproducible evaluation
capability. The historical-data capability gate is the first checkpoint; when no
verified historical options dataset exists the phase stops there and fabricates
nothing.

Every module here is deterministic, read-only and credential-free; synthetic
fixtures are explicitly labelled and can never be represented as observed market
data.
"""
from __future__ import annotations

from fno_ai_paper_trading.options_research.capability import (
    HISTORICAL_OPTIONS_DATA_INVALID,
    HISTORICAL_OPTIONS_DATA_PARTIAL,
    HISTORICAL_OPTIONS_DATA_READY,
    HISTORICAL_OPTIONS_DATA_UNAVAILABLE,
    DatasetFinding,
    ReadinessAssessment,
    SourceFinding,
    assess_readiness,
    classify_source,
)
from fno_ai_paper_trading.options_research.dataset import (
    DatasetValidation,
    validate_dataset_directory,
    validate_rows,
)
from fno_ai_paper_trading.options_research.metrics import (
    LedgerRow,
    LedgerSample,
    OptionsResearchMetrics,
    compute_metrics,
    sample_from_daily_report,
)
from fno_ai_paper_trading.options_research.protocol import (
    STRATEGY_SPECIFICATION_REQUIRED,
    ResearchProtocol,
    default_protocol,
    freeze,
    protocol_fingerprint,
)
from fno_ai_paper_trading.options_research.windows import (
    PROTECTED_OOS_END,
    PROTECTED_OOS_START,
    ConsumedWindowRegistry,
    ProtectedOosRefusal,
    SplitPlan,
    check_no_lookahead,
    classify_day,
    validate_split,
    verify_no_protected_reuse,
)

__all__ = [
    "HISTORICAL_OPTIONS_DATA_READY",
    "HISTORICAL_OPTIONS_DATA_PARTIAL",
    "HISTORICAL_OPTIONS_DATA_UNAVAILABLE",
    "HISTORICAL_OPTIONS_DATA_INVALID",
    "STRATEGY_SPECIFICATION_REQUIRED",
    "SourceFinding",
    "DatasetFinding",
    "ReadinessAssessment",
    "assess_readiness",
    "classify_source",
    "DatasetValidation",
    "validate_dataset_directory",
    "validate_rows",
    "ResearchProtocol",
    "default_protocol",
    "protocol_fingerprint",
    "freeze",
    "PROTECTED_OOS_START",
    "PROTECTED_OOS_END",
    "SplitPlan",
    "validate_split",
    "classify_day",
    "verify_no_protected_reuse",
    "check_no_lookahead",
    "ProtectedOosRefusal",
    "ConsumedWindowRegistry",
    "LedgerRow",
    "LedgerSample",
    "OptionsResearchMetrics",
    "compute_metrics",
    "sample_from_daily_report",
]