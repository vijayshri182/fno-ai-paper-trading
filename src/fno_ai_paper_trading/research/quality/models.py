"""Phase 9 — deterministic option trade-quality domain models.

The market-quality layer that consumes a Phase 8 ``ContractSelectionResult``
plus the Phase 5/6 ``OptionChainSnapshot`` available at decision time ``t`` and
answers **"is this selected option contract of sufficient observable market
quality for the next risk layer to consider?"** — nothing more.

Hard boundaries (inherit Phases 5-8):

* the engine never decides *whether to trade* and never emits a BUY/SELL side;
* *absence* is explicit: missing OI/volume/IV/greeks/premium stay missing and
  are never converted to zero and never fabricated;
* ``greeks_rho`` is deliberately never mandatory (Phase 6 verified: the
  provider chain does not supply rho);
* ``quote.timestamp`` is the adapter's receive instant, not provider market
  time (Phase 6 verified limitation) — this is carried in evidence, never
  hidden;
* no look-ahead: evaluation at ``t`` uses only the artifacts stamped at/before
  ``t`` plus the pre-existing configuration (no clock reads);
* forbidden imports: execution, broker, Upstox order code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

from fno_ai_paper_trading.research.options.models import (
    OptionChainSnapshot,
    OptionQuote,
)
from fno_ai_paper_trading.research.selection import ContractSelectionResult


class QualityOutcome(str, Enum):
    """Headline classification of one trade-quality evaluation."""

    PASS = "PASS"  # every engaged (mandatory) quality check passes
    FAIL = "FAIL"  # required observable data exists but an engaged rule fails
    UNAVAILABLE = "UNAVAILABLE"  # an engaged dimension lacks required provider data
    INVALID = "INVALID"  # the supplied data violates structural invariants


class QualityState(str, Enum):
    """Per-dimension verdict (maps 1:1 to the four outcomes)."""

    VALID = "VALID"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class QualityDimension(str, Enum):
    """The quality dimensions this engine can evaluate (canonical order)."""

    FRESHNESS = "freshness"
    BID_ASK = "bid_ask"
    PREMIUM = "premium"
    LIQUIDITY = "liquidity"
    IV = "iv"
    GREEKS = "greeks"
    SELECTION_CONSISTENCY = "selection_consistency"


DIMENSION_ORDER: tuple[QualityDimension, ...] = (
    QualityDimension.SELECTION_CONSISTENCY,
    QualityDimension.FRESHNESS,
    QualityDimension.BID_ASK,
    QualityDimension.PREMIUM,
    QualityDimension.LIQUIDITY,
    QualityDimension.IV,
    QualityDimension.GREEKS,
)


@dataclass(frozen=True)
class QualityEvidence:
    """One deterministic audit line (dimension -> field -> value -> reason)."""

    dimension: str
    field: str
    value: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "dimension": self.dimension,
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class QualityDimensionVerdict:
    """One dimension's deterministic verdict.

    ``binding`` marks whether the dimension counts toward the overall outcome
    (rule-engaged or explicitly required). Non-binding dimensions are reported
    for transparency but never degrade the outcome.
    """

    dimension: QualityDimension
    state: QualityState
    field: str
    value: str | None = None
    threshold: str | None = None
    rule: str = ""
    reason: str = ""
    binding: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "state": self.state.value,
            "field": self.field,
            "value": self.value,
            "threshold": self.threshold,
            "rule": self.rule,
            "reason": self.reason,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class TradeQualityRequest:
    """One trade-quality evaluation at decision time ``t``.

    ``selection`` is the Phase 8 result (must be ``SELECTED`` with a
    ``selected_contract``); ``snapshot`` is the normalized Phase 5 chain
    snapshot available at ``t``; ``expected_selection_fingerprint`` optionally
    pins the caller's expectation of the selection audit fingerprint.
    """

    timestamp: datetime
    selection: ContractSelectionResult
    snapshot: OptionChainSnapshot | None = None
    expected_selection_fingerprint: str | None = None


@dataclass(frozen=True)
class TradeQualityResult:
    """Deterministic, auditable outcome of one trade-quality evaluation.

    ``contract_key`` is the assessed contract when resolvable. The result is an
    eligibility assessment only — never a trade recommendation and never an
    exposure of credentials or raw provider payloads.
    """

    outcome: QualityOutcome
    timestamp: datetime
    contract_key: str | None
    underlying_symbol: str | None
    selection_outcome: str
    selection_timestamp: datetime | None
    selection_fingerprint: str | None
    chain_fingerprint: str | None
    dimensions: tuple[QualityDimensionVerdict, ...]
    rejection_reasons: tuple[str, ...]
    evidence: tuple[QualityEvidence, ...]
    composite: str | None
    rules_version: str
    schema_version: str
    engine_version: str

    @property
    def has_passed(self) -> bool:
        return self.outcome is QualityOutcome.PASS

    def dimension(self, name: QualityDimension | str) -> QualityDimensionVerdict | None:
        wanted = QualityDimension(str(name)) if not isinstance(name, QualityDimension) else name
        return next((d for d in self.dimensions if d.dimension is wanted), None)

    def _evidence_lines(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.evidence]

    def to_dict(self) -> dict[str, Any]:
        """Stable, sortable dict form (audit/handoff contract)."""
        return dict(
            sorted(
                {
                    "outcome": self.outcome.value,
                    "timestamp": self.timestamp.isoformat(),
                    "contract_key": self.contract_key,
                    "underlying_symbol": self.underlying_symbol,
                    "selection_outcome": self.selection_outcome,
                    "selection_timestamp": self.selection_timestamp.isoformat()
                    if self.selection_timestamp is not None
                    else None,
                    "selection_fingerprint": self.selection_fingerprint,
                    "chain_fingerprint": self.chain_fingerprint,
                    "dimensions": [d.to_dict() for d in self.dimensions],
                    "rejection_reasons": list(self.rejection_reasons),
                    "evidence": self._evidence_lines(),
                    "composite": self.composite,
                    "rules_version": self.rules_version,
                    "schema_version": self.schema_version,
                    "engine_version": self.engine_version,
                }.items()
            )
        )


def _state_priority(state: QualityState) -> int:
    """Deterministic precedence: INVALID > UNAVAILABLE > FAIL > PASS."""
    return {
        QualityState.VALID: 0,
        QualityState.FAIL: 1,
        QualityState.UNAVAILABLE: 2,
        QualityState.INVALID: 3,
    }[state]


def _outcome_for(state: QualityState) -> QualityOutcome:
    return {
        QualityState.VALID: QualityOutcome.PASS,
        QualityState.FAIL: QualityOutcome.FAIL,
        QualityState.UNAVAILABLE: QualityOutcome.UNAVAILABLE,
        QualityState.INVALID: QualityOutcome.INVALID,
    }[state]


__all__ = [
    "DIMENSION_ORDER",
    "QualityDimension",
    "QualityDimensionVerdict",
    "QualityEvidence",
    "QualityOutcome",
    "QualityState",
    "TradeQualityRequest",
    "TradeQualityResult",
    "_outcome_for",
    "_state_priority",
]