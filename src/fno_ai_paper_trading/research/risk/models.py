"""Phase 10 — deterministic options risk engine: domain models.

The risk layer sits between Phase 9 `TradeQualityResult` and the paper
layer. It answers only one question:

> Given an already-selected option contract and its validated market quality,
> what risk constraints apply, and is the candidate risk-eligible for the
> next paper-trading layer?

It never says "should we trade", never emits a BUY/SELL side, never builds
orders, and never touches execution/broker/Upstox code. `ELIGIBLE` is a
risk-eligibility assessment, not a trade recommendation.

Design rules (inherit Phases 5-9):

* every threshold lives in the versioned :class:`OptionsRiskConfig`;
  engineering defaults are labelled ``ENGINEERING_DEFAULT`` and mirror the
  *existing* paper-session rules (never invented here);
* outcome precedence is ``INVALID > UNAVAILABLE > BLOCKED > ELIGIBLE``,
  applied over every (binding) dimension in canonical order;
* *absence* is explicit: missing equity/P&L/position/lot-size/premium/stop
  stays ``None`` and is never converted to zero and never invented;
  missing position state must not silently become "no position";
* the option premium used for exposure is whatever the caller supplies as
  ``option_price`` (decision-time, e.g. the selected quote's ``last_price``);
  a missing premium is never replaced by a computed mid;
* lot size is *authoritative*: the caller must resolve it from the provider
  master contract metadata (Phase 6 Upstox master) and pass it in; when the
  caller does not know it the outcome is UNAVAILABLE — never assumed, never
  hard-coded here;
* a stop/risk model must be explicit: without one the risk-based quantity is
  UNAVAILABLE (``STOP_MODEL_NOT_YET_DEFINED``) and premium exposure is never
  equated to stop-loss risk;
* all money math stays ``Decimal``; the only rounding is whole-lot floor;
* no look-ahead: evaluation at ``t`` uses only artifacts stamped at/before
  ``t`` and the pre-existing configuration (no clock reads);
* provider-independent: no execution, broker, Upstox order, or
  order-construction imports anywhere in this package.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from fno_ai_paper_trading.research.quality.models import TradeQualityResult
from fno_ai_paper_trading.research.selection.models import ContractSelectionResult
from fno_ai_paper_trading.utils.functions import to_decimal


class RiskOutcome(str, Enum):
    """Headline classification of one risk evaluation."""

    ELIGIBLE = "ELIGIBLE"  # every binding risk dimension is satisfied
    BLOCKED = "BLOCKED"  # required data is present but an engaged rule fails
    UNAVAILABLE = "UNAVAILABLE"  # an engaged dimension lacks required input data
    INVALID = "INVALID"  # the supplied inputs violate structural invariants


class RiskState(str, Enum):
    """Per-dimension verdict (maps 1:1 to the four outcomes)."""

    VALID = "VALID"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class RiskDimension(str, Enum):
    """The risk dimensions this engine evaluates (canonical order)."""

    QUALITY = "quality"
    IDENTITY = "identity"
    TIMELINE = "timeline"
    ACCOUNT = "account"
    PREMIUM_EXPOSURE = "premium_exposure"
    QUANTITY = "quantity"
    DAILY_LOSS = "daily_loss"
    CONCENTRATION = "concentration"


DIMENSION_ORDER: tuple[RiskDimension, ...] = (
    RiskDimension.QUALITY,
    RiskDimension.IDENTITY,
    RiskDimension.TIMELINE,
    RiskDimension.ACCOUNT,
    RiskDimension.PREMIUM_EXPOSURE,
    RiskDimension.QUANTITY,
    RiskDimension.DAILY_LOSS,
    RiskDimension.CONCENTRATION,
)


@dataclass(frozen=True)
class RiskEvidence:
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
class RiskDimensionVerdict:
    """One dimension's deterministic verdict.

    Every dimension is binding: the outcome is the precedence-reduced state
    over all dimensions. ``rule`` names the engaged configuration rule and
    ``threshold`` the configured value so the audit trail is self-describing.
    """

    dimension: RiskDimension
    state: RiskState
    field: str
    value: str | None = None
    threshold: str | None = None
    rule: str = ""
    reason: str = ""
    binding: bool = True

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
class RiskAccountState:
    """Point-in-time account equity/P&L used for daily-loss and risk-budget math.

    Every field is ``None`` when the caller does not know it; an unknown field
    is *explicitly unavailable* and degrades the relevant dimension to
    UNAVAILABLE — it is never treated as zero. ``starting_day_equity`` is the
    equity recorded at session open (informational; the daily cap is applied
    against the consumed P&L).
    """

    current_equity: Decimal | None = None
    starting_day_equity: Decimal | None = None
    realized_pnl_today: Decimal | None = None
    unrealized_pnl_today: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("current_equity", "starting_day_equity", "realized_pnl_today", "unrealized_pnl_today"):
            value = getattr(self, name)
            if value is None:
                continue
            object.__setattr__(self, name, to_decimal(value))

    @property
    def daily_loss_consumed(self) -> Decimal | None:
        """Consumed daily loss (positive) when both P&L legs are known, else ``None``.

        A losing day consumes budget, so ``consumed = -(realized + unrealized)``.
        A profitable day yields a negative consumed value (budget not touched).
        """
        if self.realized_pnl_today is None or self.unrealized_pnl_today is None:
            return None
        return -(self.realized_pnl_today + self.unrealized_pnl_today)


@dataclass(frozen=True)
class RiskPositionState:
    """Scalar position/concentration aggregates supplied by the caller.

    ``current_contracts`` and ``premium_exposure`` describe the existing
    portfolio exposure and are required (missing -> UNAVAILABLE; never zero).
    The same-underlying / same-expiry / same-strike fields are only consumed
    when the matching concentration cap is configured; a parsed, enforced cap
    with a missing field degrades to UNAVAILABLE.
    """

    current_contracts: int | None = None
    premium_exposure: Decimal | None = None
    same_underlying_contracts: int | None = None
    same_expiry_contracts: int | None = None
    same_strike_contracts: int | None = None

    def __post_init__(self) -> None:
        if self.current_contracts is not None:
            if not isinstance(self.current_contracts, int) or isinstance(self.current_contracts, bool):
                raise ValueError("current_contracts must be an integer")
            if self.current_contracts < 0:
                raise ValueError("current_contracts must be >= 0")
            object.__setattr__(self, "current_contracts", self.current_contracts)
        for name in ("same_underlying_contracts", "same_expiry_contracts", "same_strike_contracts"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
            object.__setattr__(self, name, value)
        value = self.premium_exposure
        if value is not None:
            object.__setattr__(self, "premium_exposure", to_decimal(value))


@dataclass(frozen=True)
class RiskRequest:
    """One risk evaluation at decision time ``t``.

    ``selection`` is the Phase 8 result (``SELECTED`` with a selected contract)
    and ``quality`` the Phase 9 result for the same contract at ``t``.
    ``option_price`` is the decision-time premium used for exposure and stop
    math (e.g. ``selection.selected_contract.last_price``); paying that premium
    costs ``option_price * quantity * lot_size * multiplier`` rupees.

    ``lot_size`` is authoritative contract metadata (provider master), resolved
    by the caller; ``None`` means the caller does not know it and the engine
    reports UNAVAILABLE — never assumed here. ``requested_quantity`` is the
    candidate quantity in whole contracts (``None`` -> 1, an eligibility
    probe). ``stop_price`` is used only under the ``EXPLICIT_PRICE`` stop
    model.
    """

    decision_timestamp: datetime
    selection: ContractSelectionResult
    quality: TradeQualityResult
    account: RiskAccountState
    position: RiskPositionState
    option_price: Decimal | None = None
    requested_quantity: int | None = None
    stop_price: Decimal | None = None
    lot_size: int | None = None

    def __post_init__(self) -> None:
        if self.decision_timestamp.tzinfo is not None:
            raise ValueError("decision_timestamp must be a naive IST datetime")
        if self.option_price is not None:
            object.__setattr__(self, "option_price", to_decimal(self.option_price))
        if self.stop_price is not None:
            object.__setattr__(self, "stop_price", to_decimal(self.stop_price))
        if self.requested_quantity is not None:
            if not isinstance(self.requested_quantity, int) or isinstance(self.requested_quantity, bool):
                raise ValueError("requested_quantity must be an integer")
            if self.requested_quantity < 1:
                raise ValueError("requested_quantity must be >= 1")
            object.__setattr__(self, "requested_quantity", self.requested_quantity)
        if self.lot_size is not None:
            if not isinstance(self.lot_size, int) or isinstance(self.lot_size, bool):
                raise ValueError("lot_size must be an integer")
            if self.lot_size < 1:
                raise ValueError("lot_size must be >= 1")
            object.__setattr__(self, "lot_size", self.lot_size)


@dataclass(frozen=True)
class RiskResult:
    """Deterministic, auditable outcome of one risk evaluation.

    ``outcome`` is the precedence-reduced risk state. Report fields
    (``maximum_quantity``, ``allowed_quantity``, ``per_unit_risk``,
    ``account_risk_amount``, exposure figures, daily-loss status) are all
    computed from the request for the candidate quantity. The result is a
    risk-eligibility assessment only — never a trade recommendation and never
    an order.
    """

    outcome: RiskOutcome
    timestamp: datetime
    contract_key: str | None
    underlying_symbol: str | None
    quality_outcome: str
    selection_outcome: str
    selection_fingerprint: str | None
    dimensions: tuple[RiskDimensionVerdict, ...]
    rejection_reasons: tuple[str, ...]
    evidence: tuple[RiskEvidence, ...]
    composite: str | None
    # risk/quantity report (informational on non-ELIGIBLE outcomes)
    maximum_quantity: int | None
    allowed_quantity: int | None
    per_unit_risk: Decimal | None
    account_risk_amount: Decimal | None
    premium_unit_exposure: Decimal | None
    candidate_premium_exposure: Decimal | None
    max_premium_exposure: Decimal | None
    daily_loss_consumed: Decimal | None
    remaining_daily_budget: Decimal | None
    lot_size: int | None
    lot_count: int | None
    underlying_units: int | None
    stop_model: str | None
    stop_price_used: Decimal | None
    rules_version: str
    schema_version: str
    engine_version: str

    @property
    def is_eligible(self) -> bool:
        return self.outcome is RiskOutcome.ELIGIBLE

    @property
    def resolved_quantity(self) -> int:
        """The whole-lot quantity the engine actually evaluated (>= 1)."""
        return self.lot_count if self.lot_count is not None else 1

    def dimension(self, name: RiskDimension | str) -> RiskDimensionVerdict | None:
        wanted = RiskDimension(str(name)) if not isinstance(name, RiskDimension) else name
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
                    "quality_outcome": self.quality_outcome,
                    "selection_outcome": self.selection_outcome,
                    "selection_fingerprint": self.selection_fingerprint,
                    "dimensions": [d.to_dict() for d in self.dimensions],
                    "rejection_reasons": list(self.rejection_reasons),
                    "evidence": self._evidence_lines(),
                    "composite": self.composite,
                    "maximum_quantity": self.maximum_quantity,
                    "allowed_quantity": self.allowed_quantity,
                    "per_unit_risk": str(self.per_unit_risk) if self.per_unit_risk is not None else None,
                    "account_risk_amount": str(self.account_risk_amount) if self.account_risk_amount is not None else None,
                    "premium_unit_exposure": str(self.premium_unit_exposure) if self.premium_unit_exposure is not None else None,
                    "candidate_premium_exposure": str(self.candidate_premium_exposure)
                    if self.candidate_premium_exposure is not None
                    else None,
                    "max_premium_exposure": str(self.max_premium_exposure) if self.max_premium_exposure is not None else None,
                    "daily_loss_consumed": str(self.daily_loss_consumed) if self.daily_loss_consumed is not None else None,
                    "remaining_daily_budget": str(self.remaining_daily_budget)
                    if self.remaining_daily_budget is not None
                    else None,
                    "lot_size": self.lot_size,
                    "lot_count": self.lot_count,
                    "underlying_units": self.underlying_units,
                    "stop_model": self.stop_model,
                    "stop_price_used": str(self.stop_price_used) if self.stop_price_used is not None else None,
                    "rules_version": self.rules_version,
                    "schema_version": self.schema_version,
                    "engine_version": self.engine_version,
                }.items()
            )
        )


def _state_priority(state: RiskState) -> int:
    """Deterministic precedence: INVALID > UNAVAILABLE > BLOCKED > VALID."""
    return {
        RiskState.VALID: 0,
        RiskState.BLOCKED: 1,
        RiskState.UNAVAILABLE: 2,
        RiskState.INVALID: 3,
    }[state]


def _outcome_for(state: RiskState) -> RiskOutcome:
    return {
        RiskState.VALID: RiskOutcome.ELIGIBLE,
        RiskState.BLOCKED: RiskOutcome.BLOCKED,
        RiskState.UNAVAILABLE: RiskOutcome.UNAVAILABLE,
        RiskState.INVALID: RiskOutcome.INVALID,
    }[state]


__all__ = [
    "DIMENSION_ORDER",
    "RiskAccountState",
    "RiskDimension",
    "RiskDimensionVerdict",
    "RiskEvidence",
    "RiskOutcome",
    "RiskPositionState",
    "RiskRequest",
    "RiskResult",
    "RiskState",
    "_outcome_for",
    "_state_priority",
]