"""Phase 10 — deterministic options risk engine.

The risk gate between Phase 9 trade quality and the paper layer. Given an
already-selected option contract (Phase 8) and its validated market quality
(Phase 9) plus caller-supplied account/position aggregates, it answers only:

> "What risk constraints apply, and is this candidate risk-eligible for the
> next paper-trading layer?"

* :class:`~fno_ai_paper_trading.research.risk.models.RiskRequest` /
  :class:`RiskResult` — audit-ready frozen models (never a trade signal,
  never an order);
* :class:`~fno_ai_paper_trading.research.risk.engine.OptionsRiskConfig` —
  explicit, versioned risk rules (engineering constants mirroring the paper
  rules, not OOS-tuned);
* :class:`OptionsRiskEngine` — deterministic engine with outcome precedence
  ``INVALID > UNAVAILABLE > BLOCKED > ELIGIBLE`` over binding dimensions,
  all-Decimal math and whole-lot flooring.

Hard boundary: no BUY/SELL, no order construction, no execution/broker/Upstox
order code, no clock reads, no credentials; upstream research providers stay
input-only. ``ELIGIBLE`` is a risk-eligibility assessment only.
"""
from fno_ai_paper_trading.research.risk.engine import (
    STOP_MODEL_EXPLICIT_PRICE,
    STOP_MODEL_FIXED_PCT,
    STOP_MODEL_NOT_YET_DEFINED,
    OptionsRiskConfig,
    OptionsRiskEngine,
    RISK_ENGINE_VERSION,
    RISK_RULES_VERSION,
    RISK_SCHEMA_VERSION,
)
from fno_ai_paper_trading.research.risk.models import (
    DIMENSION_ORDER,
    RiskAccountState,
    RiskDimension,
    RiskDimensionVerdict,
    RiskEvidence,
    RiskOutcome,
    RiskPositionState,
    RiskRequest,
    RiskResult,
    RiskState,
)
from fno_ai_paper_trading.research.quality.engine import selection_fingerprint

RISK_CONFIG_DEFAULT = OptionsRiskConfig()

__all__ = [
    "DIMENSION_ORDER",
    "RISK_CONFIG_DEFAULT",
    "RISK_ENGINE_VERSION",
    "RISK_RULES_VERSION",
    "RISK_SCHEMA_VERSION",
    "STOP_MODEL_EXPLICIT_PRICE",
    "STOP_MODEL_FIXED_PCT",
    "STOP_MODEL_NOT_YET_DEFINED",
    "OptionsRiskConfig",
    "OptionsRiskEngine",
    "RiskAccountState",
    "RiskDimension",
    "RiskDimensionVerdict",
    "RiskEvidence",
    "RiskOutcome",
    "RiskPositionState",
    "RiskRequest",
    "RiskResult",
    "RiskState",
    "selection_fingerprint",
]