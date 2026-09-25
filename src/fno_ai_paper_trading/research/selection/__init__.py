"""Phase 8 — deterministic option contract selector.

Provider-neutral *eligibility* selection layered on the Phase 5 option models,
Phase 6-verified chain data and the Phase 7 market regime:

* :class:`~fno_ai_paper_trading.research.selection.models.ContractSelectionRequest` /
  :class:`ContractSelectionResult` — audit-ready frozen models (never a trade signal);
* :class:`~fno_ai_paper_trading.research.selection.selector.ContractSelectionConfig` —
  explicit, versioned selection rules (engineering constants, not OOS-tuned);
* :class:`ContractSelectionEngine` — deterministic engine answering only
  "which contract(s) meet the configured eligibility rules?".

Hard boundary: no BUY/SELL, no order, no execution, no risk, no credentials;
upstream providers stay input-only.
"""
from fno_ai_paper_trading.research.selection.models import (
    CandidateState,
    ContractCandidateVerdict,
    ContractSelectionRequest,
    ContractSelectionResult,
    ExpiryPolicy,
    OffsetUnits,
    SelectionEvidence,
    SelectionOutcome,
    SideSource,
    StrikePolicy,
)
from fno_ai_paper_trading.research.selection.selector import (
    CONTRACT_CONFIG_DEFAULT,
    ENGINE_VERSION,
    SCHEMA_VERSION,
    SELECTOR_RULES_VERSION,
    TIE_BREAK_ORDER,
    ContractSelectionConfig,
    ContractSelectionEngine,
    pool_fingerprint,
    stable_candidate_key,
)

__all__ = [
    "CONTRACT_CONFIG_DEFAULT",
    "CandidateState",
    "ContractCandidateVerdict",
    "ContractSelectionConfig",
    "ContractSelectionEngine",
    "ContractSelectionRequest",
    "ContractSelectionResult",
    "ENGINE_VERSION",
    "ExpiryPolicy",
    "OffsetUnits",
    "SCHEMA_VERSION",
    "SELECTOR_RULES_VERSION",
    "SelectionEvidence",
    "SelectionOutcome",
    "SideSource",
    "StrikePolicy",
    "TIE_BREAK_ORDER",
    "pool_fingerprint",
    "stable_candidate_key",
]