"""Phase 8 — deterministic option contract selection domain models.

Provider-independent contract *eligibility* selection: given a Phase 7
``MarketRegimeReport``, validated Phase 5 ``OptionChainSnapshot``(s) and an
explicit versioned ``ContractSelectionConfig``, the
:class:`ContractSelectionEngine` answers **"which contract(s) meet the
configured eligibility rules?"** — nothing more.

Hard boundaries (inherited from Phases 5-7):

* no BUY/SELL/order/risk/execution vocabulary anywhere in this package;
* *absence* means explicitly unavailable — missing OI/volume/IV/greeks are
  never turned into zero and never fabricated;
* nothing may read credentials or import execution/broker/Upstox code;
* selection at decision time ``t`` uses only the snapshot(s) available at ``t``
  and the regime computed at ``t`` (no look-ahead);
* the resolved strike must be an actual strike present in the chain — no
  synthetic strikes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.options.models import (
    OptionChainSnapshot,
    OptionQuote,
    OptionSide,
)
from fno_ai_paper_trading.research.regime import MarketRegimeReport


class SelectionOutcome(str, Enum):
    """Headline classification of one selection attempt."""

    SELECTED = "SELECTED"  # a contract qualified and was chosen
    NO_ELIGIBLE_CONTRACT = "NO_ELIGIBLE_CONTRACT"  # data fine, but nothing passed the rules
    UNAVAILABLE_DATA = "UNAVAILABLE_DATA"  # data needed for a decision is absent
    INVALID_DATA = "INVALID_DATA"  # present data is structurally invalid


class ExpiryPolicy(str, Enum):
    """Deterministic expiry-resolution policy (never a silent choice)."""

    NEAREST = "NEAREST"  # first non-expired expiry >= decision date (ascending)
    NEXT = "NEXT"  # the expiry immediately after the nearest non-expired one
    EXPLICIT = "EXPLICIT"  # a caller-supplied expiry must match exactly


class StrikePolicy(str, Enum):
    """Deterministic strike-resolution policy over the actual chain strikes."""

    ATM = "ATM"  # the available strike nearest to spot (tie: lower strike)
    ATM_OFFSET = "ATM_OFFSET"  # a signed offset from the ATM strike


class OffsetUnits(str, Enum):
    """How an ``ATM_OFFSET`` offset is interpreted."""

    STRIKES = "STRIKES"  # number of steps along the sorted available strikes
    POINTS = "POINTS"  # absolute price offset from the ATM strike (nearest available)


class SideSource(str, Enum):
    """Where the candidate CE/PE side comes from."""

    REGIME = "REGIME"  # BULLISH -> CE, BEARISH -> PE, NEUTRAL -> configurable
    FIXED = "FIXED"  # a caller-supplied side


class CandidateState(str, Enum):
    """Per-candidate data verdict (structural, not policy)."""

    VALID = "VALID"
    INVALID = "INVALID"  # structurally broken: duplicate key, expired, crossed, bad timestamp
    UNAVAILABLE = "UNAVAILABLE"  # a field required by configuration is absent


@dataclass(frozen=True)
class SelectionEvidence:
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
class ContractCandidateVerdict:
    """Deterministic verdict for one considered contract."""

    key: str
    state: CandidateState
    reasons: tuple[str, ...] = ()
    eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state.value,
            "reasons": list(self.reasons),
            "eligible": self.eligible,
        }


@dataclass(frozen=True)
class ContractSelectionRequest:
    """One selection attempt at a single decision instant.

    ``snapshots`` may cover several expiries (one snapshot each). Each snapshot
    must already be normalized (Phase 5) and, where applicable, validated.
    ``regime`` is the Phase 7 decision at the same instant (optional; required
    by the ``REGIME`` side source). ``expiry`` and ``option_side`` are used by
    the ``EXPLICIT`` / ``FIXED`` policies and are otherwise ignored.
    """

    underlying: Instrument
    timestamp: datetime
    snapshots: Sequence[OptionChainSnapshot]
    regime: MarketRegimeReport | None = None
    expiry: date | str | None = None
    option_side: OptionSide | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))


@dataclass(frozen=True)
class ContractSelectionResult:
    """Deterministic, auditable outcome of one selection attempt.

    ``selected_contract`` is the chosen :class:`OptionQuote` when ``outcome``
    is :attr:`SelectionOutcome.SELECTED`; otherwise ``None``. The result never
    contains a BUY/SELL recommendation and never exposes credentials.
    """

    outcome: SelectionOutcome
    timestamp: datetime
    underlying: Instrument
    selected_contract: OptionQuote | None
    resolved_expiry: date | None
    resolved_strike: Decimal | None
    resolved_side: OptionSide | None
    spot_price: Decimal | None
    regime_direction: str | None
    regime_headline: str | None
    expiry_policy: str
    strike_policy: str
    candidates_considered: tuple[str, ...]
    candidate_verdicts: tuple[ContractCandidateVerdict, ...]
    rejection_reasons: tuple[str, ...]
    chain_fingerprints: tuple[str, ...]
    evidence: tuple[SelectionEvidence, ...]
    rules_version: str
    schema_version: str
    engine_version: str

    @property
    def has_selection(self) -> bool:
        return self.outcome is SelectionOutcome.SELECTED

    @property
    def selected_key(self) -> str | None:
        return self.selected_contract.key if self.selected_contract is not None else None

    def _evidence_lines(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.evidence]

    def to_dict(self) -> dict[str, Any]:
        """Stable, sortable dict form (audit/handoff contract)."""
        return dict(
            sorted(
                {
                    "outcome": self.outcome.value,
                    "timestamp": self.timestamp.isoformat(),
                    "underlying": self.underlying.symbol,
                    "selected_contract": self.selected_contract.key if self.selected_contract is not None else None,
                    "resolved_expiry": self.resolved_expiry.isoformat() if self.resolved_expiry else None,
                    "resolved_strike": str(self.resolved_strike) if self.resolved_strike is not None else None,
                    "resolved_side": self.resolved_side.value if self.resolved_side else None,
                    "spot_price": str(self.spot_price) if self.spot_price is not None else None,
                    "regime_direction": self.regime_direction,
                    "regime_headline": self.regime_headline,
                    "expiry_policy": self.expiry_policy,
                    "strike_policy": self.strike_policy,
                    "candidates_considered": list(self.candidates_considered),
                    "candidate_verdicts": [v.to_dict() for v in self.candidate_verdicts],
                    "rejection_reasons": list(self.rejection_reasons),
                    "chain_fingerprints": list(self.chain_fingerprints),
                    "evidence": self._evidence_lines(),
                    "rules_version": self.rules_version,
                    "schema_version": self.schema_version,
                    "engine_version": self.engine_version,
                }.items()
            )
        )


__all__ = [
    "CandidateState",
    "ContractCandidateVerdict",
    "ContractSelectionRequest",
    "ContractSelectionResult",
    "ExpiryPolicy",
    "OffsetUnits",
    "SelectionEvidence",
    "SelectionOutcome",
    "SideSource",
    "StrikePolicy",
]