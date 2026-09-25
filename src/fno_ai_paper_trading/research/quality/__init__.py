"""Phase 9 — deterministic option trade-quality engine.

The market-quality gate between Phase 8 contract selection and the Phase 10+
risk layer. Given a Phase 8 ``ContractSelectionResult`` and the Phase 5/6
``OptionChainSnapshot`` available at decision time ``t``, it answers only:

> "Is this selected option contract of sufficient observable market quality for
> the next risk layer to consider?"

* :class:`~fno_ai_paper_trading.research.quality.models.TradeQualityRequest` /
  :class:`TradeQualityResult` — audit-ready frozen models (never a trade signal);
* :class:`~fno_ai_paper_trading.research.quality.engine.TradeQualityConfig` —
  explicit, versioned quality rules (engineering constants, not OOS-tuned);
* :class:`TradeQualityEngine` — deterministic engine with outcome precedence
  ``INVALID > UNAVAILABLE > FAIL > PASS`` over binding dimensions.

Hard boundary: no BUY/SELL, no order, no execution, no risk/position sizing, no
credentials; upstream providers stay input-only.
"""
from fno_ai_paper_trading.research.quality.models import (
    DIMENSION_ORDER,
    QualityDimension,
    QualityDimensionVerdict,
    QualityEvidence,
    QualityOutcome,
    QualityState,
    TradeQualityRequest,
    TradeQualityResult,
)
from fno_ai_paper_trading.research.quality.engine import (
    PROVIDER_TIMESTAMP_CAVEAT,
    QUALITY_ENGINE_VERSION,
    QUALITY_RULES_VERSION,
    QUALITY_SCHEMA_VERSION,
    TradeQualityConfig,
    TradeQualityEngine,
    selection_fingerprint,
)

QUALITY_CONFIG_DEFAULT = TradeQualityConfig()

__all__ = [
    "DIMENSION_ORDER",
    "PROVIDER_TIMESTAMP_CAVEAT",
    "QUALITY_CONFIG_DEFAULT",
    "QUALITY_ENGINE_VERSION",
    "QUALITY_RULES_VERSION",
    "QUALITY_SCHEMA_VERSION",
    "QualityDimension",
    "QualityDimensionVerdict",
    "QualityEvidence",
    "QualityOutcome",
    "QualityState",
    "TradeQualityConfig",
    "TradeQualityEngine",
    "TradeQualityRequest",
    "TradeQualityResult",
    "selection_fingerprint",
]