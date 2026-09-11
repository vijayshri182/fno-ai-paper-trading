"""Experience store domain (WS 7.9) — durable paper-trade evidence.

Evidence-only infrastructure: decision-time context, realized trade outcomes,
and AI-advisory metadata. This package contains no execution path — nothing in
here can place an order, bypass risk controls, or feed the backtester future
data. The storage abstraction lives in
:mod:`fno_ai_paper_trading.persistence.experience_store`.
"""
from __future__ import annotations

from fno_ai_paper_trading.experience.classification import (
    classify_outcome,
    holding_seconds,
)
from fno_ai_paper_trading.experience.enums import (
    AdvisoryUsage,
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
    OutcomeKind,
)
from fno_ai_paper_trading.experience.queries import (
    ExperienceQuery,
    apply_query,
    count_by,
)
from fno_ai_paper_trading.experience.records import (
    EXPERIENCE_SCHEMA_VERSION,
    AdvisoryEvidence,
    DecisionContext,
    ExperienceRecord,
    TradeOutcome,
    decision_identity,
    make_experience_id,
)

__all__ = [
    "EXPERIENCE_SCHEMA_VERSION",
    "AdvisoryEvidence",
    "AdvisoryUsage",
    "DataQualityStatus",
    "DecisionContext",
    "DecisionStatus",
    "ExperienceQuery",
    "ExperienceRecord",
    "ExperienceSourceType",
    "OutcomeKind",
    "TradeOutcome",
    "apply_query",
    "classify_outcome",
    "count_by",
    "decision_identity",
    "holding_seconds",
    "make_experience_id",
]