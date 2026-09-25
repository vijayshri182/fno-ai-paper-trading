"""Phase 10 — deterministic options risk engine.

:class:`OptionsRiskEngine` consumes a Phase 8 :class:`ContractSelectionResult`
and the Phase 9 :class:`TradeQualityResult` for the same selected contract at
decision time ``t`` plus caller-supplied account/position aggregates, and
decides whether the candidate is *risk-eligible* for the next paper-trading
layer. It answers only that question — it never emits a trade signal and never
constructs an order.

Design rules (inherit Phases 5-9):

* every threshold lives in the versioned :class:`OptionsRiskConfig`;
  engineering defaults are labelled ``ENGINEERING_DEFAULT`` and mirror the
  *existing* paper rules (``risk_per_trade_pct``/``stop_loss_pct``/max
  quantities/notional/daily loss from :class:`PaperSettings`); none were tuned
  on any protected-OOS data;
* outcome precedence is ``INVALID > UNAVAILABLE > BLOCKED > ELIGIBLE`` and is
  applied over every binding dimension in canonical order (there are no
  advisory dimensions in the risk layer);
* *absence* is explicit: missing equity / P&L / position / lot-size / premium /
  stop stay ``None`` and are never converted to zero and never invented;
* quality prerequisite: ``INVALID -> INVALID``, ``UNAVAILABLE -> UNAVAILABLE``,
  an outcome in ``required_quality_outcomes`` (default ``("PASS",)``) is
  satisfied, anything else -> BLOCKED (Phase 9 is never bypassed or weakened);
* identity/timeline prerequisites: the selection and the quality analysis must
  name the same contract and agree on the selection audit fingerprint, and
  timestamps must be ordered ``selection <= quality <= decision``; any
  inconsistency or future/tz-aware timestamp -> INVALID (never silently
  reselected or repaired);
* lot size is authoritative (``request.lot_size``, caller-resolved from the
  provider master); missing -> UNAVAILABLE; never assumed or hard-coded;
* a stop/risk model must be explicit: without one the risk-based quantity is
  UNAVAILABLE (``STOP_MODEL_NOT_YET_DEFINED``); premium exposure is never
  equated to stop-loss risk;
* all money math stays ``Decimal``; the only rounding is whole-lot floor
  (``Decimal //``), quantities are always whole lots;
* no look-ahead: evaluation at ``t`` uses only artifacts stamped at/before
  ``t`` and the pre-existing configuration (this module never reads the
  clock);
* forbidden imports: execution, broker, Upstox order code, order construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.research.quality.engine import selection_fingerprint
from fno_ai_paper_trading.research.quality.models import QualityOutcome
from fno_ai_paper_trading.research.risk.models import (
    DIMENSION_ORDER,
    RiskDimension,
    RiskDimensionVerdict,
    RiskEvidence,
    RiskOutcome,
    RiskRequest,
    RiskResult,
    RiskState,
    _outcome_for,
    _state_priority,
)
from fno_ai_paper_trading.utils.functions import to_decimal

RISK_ENGINE_VERSION = "1.0.0"
RISK_SCHEMA_VERSION = "1.0.0"
RISK_RULES_VERSION = "1.0.0"

STOP_MODEL_NOT_YET_DEFINED = "STOP_MODEL_NOT_YET_DEFINED"
STOP_MODEL_EXPLICIT_PRICE = "EXPLICIT_PRICE"
STOP_MODEL_FIXED_PCT = "FIXED_PCT"

_DECIMAL_ONE = Decimal(1)
_DECIMAL_ZERO = Decimal(0)


@dataclass(frozen=True)
class OptionsRiskConfig:
    """Explicit, versioned options-risk rules.

    Engineering defaults mirror the *existing* paper-session rules
    (:class:`PaperSettings` and the paper session policy) so the risk engine
    preserves, never weakens, the project's established controls. Every
    threshold is labelled ``ENGINEERING_DEFAULT``; none was tuned on any
    protected-OOS data. Nothing here is a "safe / high-conviction" label.

    ``stop_model`` is ``None`` by default: without an explicit stop/risk model
    the risk-based quantity is UNAVAILABLE (``STOP_MODEL_NOT_YET_DEFINED``)
    and the engine cannot declare the candidate ELIGIBLE. Set it to
    ``"EXPLICIT_PRICE"`` (caller supplies ``request.stop_price``) or
    ``"FIXED_PCT"`` (fixed-percentage stop, reusing the paper session's
    fixed-percent convention) to enable risk-based sizing.
    """

    version: str = RISK_RULES_VERSION

    # quality prerequisite (always engaged) --------------------------------
    required_quality_outcomes: tuple[str, ...] = ("PASS",)

    # account risk (always engaged) ----------------------------------------
    risk_per_trade_pct: Decimal = field(default_factory=lambda: Decimal("0.01"))  # ENGINEERING_DEFAULT == PaperSettings.paper_risk_per_trade_pct
    max_daily_loss: Decimal = field(default_factory=lambda: Decimal("10000"))  # ENGINEERING_DEFAULT == PaperSettings.max_daily_loss

    # premium exposure (always engaged) ------------------------------------
    max_premium_exposure: Decimal = field(default_factory=lambda: Decimal("250000"))  # ENGINEERING_DEFAULT == PaperSettings.max_order_notional
    max_contracts: int = 75  # ENGINEERING_DEFAULT == PaperSettings.max_position_quantity
    minimum_quantity: int = 1  # smallest whole-lot count permitted

    # stop/risk model (None -> STOP_MODEL_NOT_YET_DEFINED) ------------------
    stop_model: str | None = None
    stop_loss_pct: Decimal = field(default_factory=lambda: Decimal("0.02"))  # ENGINEERING_DEFAULT == PaperSettings.paper_stop_loss_pct (FIXED_PCT only)

    # concentration caps (None = not enforced) -----------------------------
    max_same_underlying_contracts: int | None = None
    max_same_expiry_contracts: int | None = None
    max_same_strike_contracts: int | None = None

    # audit ----------------------------------------------------------------
    include_composite: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_quality_outcomes", tuple(str(v) for v in self.required_quality_outcomes))
        if not self.required_quality_outcomes:
            raise ValueError("required_quality_outcomes must not be empty")
        object.__setattr__(self, "risk_per_trade_pct", to_decimal(self.risk_per_trade_pct))
        object.__setattr__(self, "max_daily_loss", to_decimal(self.max_daily_loss))
        object.__setattr__(self, "max_premium_exposure", to_decimal(self.max_premium_exposure))
        object.__setattr__(self, "stop_loss_pct", to_decimal(self.stop_loss_pct))
        if self.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be > 0")
        if self.max_daily_loss <= 0:
            raise ValueError("max_daily_loss must be > 0")
        if self.max_premium_exposure <= 0:
            raise ValueError("max_premium_exposure must be > 0")
        if self.stop_loss_pct <= 0 or self.stop_loss_pct >= 1:
            raise ValueError("stop_loss_pct must be in (0, 1)")
        if self.max_contracts < 1:
            raise ValueError("max_contracts must be >= 1")
        if self.minimum_quantity < 1:
            raise ValueError("minimum_quantity must be >= 1")
        for name in (
            "max_same_underlying_contracts",
            "max_same_expiry_contracts",
            "max_same_strike_contracts",
        ):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be None or >= 1")
        if self.stop_model is not None:
            normalized = str(self.stop_model).strip().upper()
            if normalized not in (STOP_MODEL_EXPLICIT_PRICE, STOP_MODEL_FIXED_PCT):
                raise ValueError(f"unknown stop_model {self.stop_model!r}; expected EXPLICIT_PRICE or FIXED_PCT")
            object.__setattr__(self, "stop_model", normalized)

    @property
    def stop_defined(self) -> bool:
        return self.stop_model is not None


class OptionsRiskEngine:
    """Deterministic, pure options-risk evaluator.

    The engine is a function of ``(request, config)``: it performs no I/O, no
    clock reads, no network, and no order/execution activity.
    """

    def __init__(self, config: OptionsRiskConfig | None = None) -> None:
        self.config = config if config is not None else OptionsRiskConfig()

    # ------------------------------------------------------------------ api

    def evaluate(self, request: RiskRequest) -> RiskResult:
        if not isinstance(request, RiskRequest):
            raise TypeError("request must be a RiskRequest")
        config = self.config

        verdicts: dict[RiskDimension, RiskDimensionVerdict] = {}
        evidence: list[RiskEvidence] = []
        report: dict[str, object] = {}
        for dimension in DIMENSION_ORDER:
            handler = getattr(self, f"_check_{dimension.value}")
            verdict, ev_lines, rep = handler(request, config)
            verdicts[dimension] = verdict
            evidence.extend(ev_lines)
            report.update(rep)

        report.setdefault("lot_size", request.lot_size)
        report.setdefault("max_premium_exposure", config.max_premium_exposure)

        outcome = RiskOutcome.ELIGIBLE
        worst = RiskState.VALID
        for verdict in verdicts.values():
            if _state_priority(verdict.state) > _state_priority(worst):
                worst = verdict.state
        outcome = _outcome_for(worst)

        reasons: list[str] = []
        for verdict in verdicts.values():
            if verdict.state is not RiskState.VALID:
                reasons.append(f"{verdict.dimension.value}:{verdict.rule}")

        composite = None
        if config.include_composite:
            composite = f"{sum(1 for v in verdicts.values() if v.state is RiskState.VALID)}/{len(verdicts)}"

        maximum_quantity = self._report_maximum(report)
        requested = int(report.get("lot_count") or 1)

        selection = request.selection
        contract_key = request.selection.selected_key if selection is not None else None
        underlying = (
            request.selection.selected_contract.instrument.underlying_symbol
            if (selection is not None and request.selection.selected_contract is not None)
            else None
        )

        return RiskResult(
            outcome=outcome,
            timestamp=request.decision_timestamp,
            contract_key=contract_key,
            underlying_symbol=underlying,
            quality_outcome=request.quality.outcome.value if request.quality is not None else "MISSING",
            selection_outcome=request.selection.outcome.value if request.selection is not None else "MISSING",
            selection_fingerprint=request.quality.selection_fingerprint if request.quality is not None else None,
            dimensions=tuple(verdicts[d] for d in DIMENSION_ORDER if d in verdicts),
            rejection_reasons=tuple(reasons),
            evidence=tuple(evidence),
            composite=composite,
            maximum_quantity=maximum_quantity,
            allowed_quantity=min(requested, maximum_quantity) if maximum_quantity is not None else requested,
            per_unit_risk=report.get("per_unit_risk"),
            account_risk_amount=report.get("account_risk_amount"),
            premium_unit_exposure=report.get("premium_unit_exposure"),
            candidate_premium_exposure=report.get("candidate_premium_exposure"),
            max_premium_exposure=report.get("max_premium_exposure"),
            daily_loss_consumed=report.get("daily_loss_consumed"),
            remaining_daily_budget=report.get("remaining_daily_budget"),
            lot_size=report.get("lot_size"),
            lot_count=report.get("lot_count"),
            underlying_units=report.get("underlying_units"),
            stop_model=report.get("stop_model"),
            stop_price_used=report.get("stop_price_used"),
            rules_version=config.version,
            schema_version=RISK_SCHEMA_VERSION,
            engine_version=RISK_ENGINE_VERSION,
        )

    # ------------------------------------------------------------ dimensions

    def _check_quality(self, request: RiskRequest, config: OptionsRiskConfig):
        quality = request.quality
        if quality is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUALITY, RiskState.UNAVAILABLE, "quality.outcome", rule="required_quality_outcomes",
                    reason="trade-quality result missing",
                ),
                [RiskEvidence("quality", "outcome", "MISSING", "quality prerequisite cannot be checked")],
                {},
            )
        if quality.outcome is QualityOutcome.INVALID:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUALITY, RiskState.INVALID, "quality.outcome", quality.outcome.value,
                    rule="required_quality_outcomes", reason="Phase 9 declared the inputs structurally invalid",
                ),
                [RiskEvidence("quality", "outcome", quality.outcome.value, "INVALID quality is never upgraded")],
                {},
            )
        if quality.outcome is QualityOutcome.UNAVAILABLE:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUALITY, RiskState.UNAVAILABLE, "quality.outcome", quality.outcome.value,
                    rule="required_quality_outcomes", reason="Phase 9 could not assess quality",
                ),
                [RiskEvidence("quality", "outcome", quality.outcome.value, "UNAVAILABLE quality stays unavailable")],
                {},
            )
        if quality.outcome.value in config.required_quality_outcomes:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUALITY, RiskState.VALID, "quality.outcome", quality.outcome.value,
                    threshold="/".join(sorted(config.required_quality_outcomes)), rule="required_quality_outcomes",
                    reason="quality prerequisite satisfied",
                ),
                [RiskEvidence("quality", "outcome", quality.outcome.value, "required quality outcome satisfied")],
                {},
            )
        return (
            RiskDimensionVerdict(
                RiskDimension.QUALITY, RiskState.BLOCKED, "quality.outcome", quality.outcome.value,
                threshold="/".join(sorted(config.required_quality_outcomes)), rule="required_quality_outcomes",
                reason="quality outcome not in required set (BLOCKED, never bypassed)",
            ),
            [RiskEvidence("quality", "outcome", quality.outcome.value, "not among required quality outcomes")],
            {},
        )

    def _check_identity(self, request: RiskRequest, config: OptionsRiskConfig):
        selection = request.selection
        quality = request.quality
        state = RiskState.VALID
        reason = "selection and quality name the same selected contract"
        value = None
        if selection is None:
            state, reason = RiskState.UNAVAILABLE, "selection result missing"
        elif selection.selected_contract is None:
            state, reason = RiskState.UNAVAILABLE, "selection has no selected contract"
        elif quality is None or quality.contract_key is None:
            state, reason = RiskState.UNAVAILABLE, "quality result does not identify a contract"
        elif quality.contract_key != selection.selected_key:
            state = RiskState.INVALID
            reason = f"quality assessed {quality.contract_key} but selection picked {selection.selected_key}"
            value = f"{quality.contract_key} != {selection.selected_key}"
        elif (
            quality.selection_fingerprint is not None
            and quality.selection_fingerprint != selection_fingerprint(selection)
        ):
            state = RiskState.INVALID
            reason = "quality audit fingerprint does not match the supplied selection"
            value = "fingerprint mismatch"
        return (
            RiskDimensionVerdict(
                RiskDimension.IDENTITY, state, "identity", value=value,
                threshold=None, rule="identity_consistency", reason=reason,
            ),
            [
                RiskEvidence("identity", "contract_key", str(quality.contract_key) if quality is not None else None, reason),
                RiskEvidence(
                    "identity", "quality_fingerprint",
                    str(quality.selection_fingerprint) if quality is not None else None,
                    reason,
                ),
            ],
            {},
        )

    def _check_timeline(self, request: RiskRequest, config: OptionsRiskConfig):
        selection = request.selection
        quality = request.quality
        state = RiskState.VALID
        reason = "selection <= quality <= decision and every timestamp is naive IST"
        value = f"{selection.timestamp.isoformat() if selection is not None else None} <= {quality.timestamp.isoformat() if quality is not None else None} <= {request.decision_timestamp.isoformat()}"
        if selection is None or quality is None:
            state, reason = RiskState.UNAVAILABLE, "cannot order timestamps without selection/quality"
        elif request.decision_timestamp.tzinfo is not None or selection.timestamp.tzinfo is not None or quality.timestamp.tzinfo is not None:
            state, reason = RiskState.INVALID, "tz-aware timestamps are not supported (naive IST expected)"
        elif selection.timestamp > quality.timestamp:
            state, reason = RiskState.INVALID, "selection timestamp is after the quality timestamp"
        elif quality.timestamp > request.decision_timestamp:
            state, reason = RiskState.INVALID, "quality timestamp is after the decision timestamp (future artifact)"
        elif selection.timestamp > request.decision_timestamp:
            state, reason = RiskState.INVALID, "selection timestamp is after the decision timestamp (future artifact)"
        return (
            RiskDimensionVerdict(
                RiskDimension.TIMELINE, state, "timeline", value=value,
                rule="timeline_ordering", reason=reason,
            ),
            [
                RiskEvidence("timeline", "selection", str(selection.timestamp.isoformat()) if selection is not None else None, reason),
                RiskEvidence("timeline", "quality", str(quality.timestamp.isoformat()) if quality is not None else None, reason),
                RiskEvidence("timeline", "decision", request.decision_timestamp.isoformat(), reason),
            ],
            {},
        )

    def _check_account(self, request: RiskRequest, config: OptionsRiskConfig):
        account = request.account
        equity = account.current_equity if account is not None else None
        if equity is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.ACCOUNT, RiskState.UNAVAILABLE, "account.current_equity", rule="account_risk",
                    reason="current equity is not supplied (missing != zero)",
                ),
                [RiskEvidence("account", "current_equity", "None", "equity never assumed")],
                {},
            )
        if equity <= 0:
            return (
                RiskDimensionVerdict(
                    RiskDimension.ACCOUNT, RiskState.BLOCKED, "account.current_equity", str(equity),
                    rule="account_risk", reason="non-positive equity cannot back a candidate",
                ),
                [RiskEvidence("account", "current_equity", str(equity), "non-positive equity")],
                {},
            )
        account_risk_amount = equity * config.risk_per_trade_pct
        return (
            RiskDimensionVerdict(
                RiskDimension.ACCOUNT, RiskState.VALID, "account.current_equity", str(equity),
                threshold=str(config.risk_per_trade_pct), rule="account_risk",
                reason=f"account risk amount {account_risk_amount}",
            ),
            [RiskEvidence("account", "account_risk_amount", str(account_risk_amount), "equity * risk_per_trade_pct")],
            {"account_risk_amount": account_risk_amount},
        )

    def _check_premium_exposure(self, request: RiskRequest, config: OptionsRiskConfig):
        premium = request.option_price
        lot_size = request.lot_size
        requested = request.requested_quantity if request.requested_quantity is not None else 1
        selection = request.selection
        multiplier = (
            selection.selected_contract.instrument.multiplier
            if (selection is not None and selection.selected_contract is not None)
            else None
        )

        def out(state, field, reason, extra_evidence=(), report=None):
            return (
                RiskDimensionVerdict(
                    RiskDimension.PREMIUM_EXPOSURE, state, field, rule="premium_exposure", reason=reason,
                ),
                list(extra_evidence),
                report or {},
            )

        if premium is None:
            return out(
                RiskState.UNAVAILABLE, "option_price",
                "option premium not supplied (never replaced by a computed mid)",
                [RiskEvidence("premium_exposure", "option_price", "None", "premium never fabricated")],
            )
        if premium <= 0:
            return out(
                RiskState.INVALID, "option_price",
                "non-positive option premium is structurally invalid",
                [RiskEvidence("premium_exposure", "option_price", str(premium), "non-positive premium")],
            )
        if lot_size is None:
            return out(
                RiskState.UNAVAILABLE, "lot_size",
                "authoritative lot size is not supplied (never assumed)",
                [RiskEvidence("premium_exposure", "lot_size", "None", "lot size never hard-coded")],
            )
        if multiplier is None:
            return out(
                RiskState.UNAVAILABLE, "multiplier",
                "contract multiplier is not available",
                [RiskEvidence("premium_exposure", "multiplier", "None", "multiplier missing")],
            )

        unit_exposure = premium * lot_size * multiplier
        candidate_exposure = unit_exposure * requested
        report = {
            "premium_unit_exposure": unit_exposure,
            "candidate_premium_exposure": candidate_exposure,
            "max_premium_exposure": config.max_premium_exposure,
        }
        if config.max_premium_exposure is not None:
            max_by_exposure = int(config.max_premium_exposure // unit_exposure)
            report["max_by_exposure"] = max_by_exposure
            if requested > max_by_exposure:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.PREMIUM_EXPOSURE, RiskState.BLOCKED, "premium_exposure",
                        value=str(candidate_exposure), threshold=str(config.max_premium_exposure),
                        rule="premium_exposure",
                        reason=f"candidate premium exposure {candidate_exposure} exceeds cap {config.max_premium_exposure}",
                    ),
                    [RiskEvidence("premium_exposure", "candidate", str(candidate_exposure), "exceeds max_premium_exposure")],
                    report,
                )
        return (
            RiskDimensionVerdict(
                RiskDimension.PREMIUM_EXPOSURE, RiskState.VALID, "premium_exposure",
                value=str(candidate_exposure), threshold=str(config.max_premium_exposure),
                rule="premium_exposure",
                reason=f"unit exposure {unit_exposure} x {requested} contract(s)",
            ),
            [
                RiskEvidence("premium_exposure", "unit", str(unit_exposure), "premium * lot_size * multiplier"),
                RiskEvidence("premium_exposure", "candidate", str(candidate_exposure), "premium * qty * lot_size * multiplier"),
            ],
            report,
        )

    def _check_quantity(self, request: RiskRequest, config: OptionsRiskConfig):
        requested = request.requested_quantity if request.requested_quantity is not None else 1
        premium = request.option_price
        lot_size = request.lot_size
        selection = request.selection
        multiplier = (
            selection.selected_contract.instrument.multiplier
            if (selection is not None and selection.selected_contract is not None)
            else None
        )
        report: dict[str, object] = {
            "per_unit_risk": None,
            "stop_price_used": None,
            "stop_model": config.stop_model or STOP_MODEL_NOT_YET_DEFINED,
            "lot_count": requested,
            "underlying_units": requested * lot_size if lot_size is not None else None,
        }
        account_risk_amount = (
            request.account.current_equity * config.risk_per_trade_pct
            if request.account is not None and request.account.current_equity is not None
            else None
        )

        stop_price_used = None
        per_unit_risk = None

        if config.stop_model is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUANTITY, RiskState.UNAVAILABLE, "config.stop_model",
                    rule="stop_model",
                    reason="STOP_MODEL_NOT_YET_DEFINED: no explicit stop/risk model; risk-based quantity unavailable",
                ),
                [RiskEvidence("quantity", "stop_model", STOP_MODEL_NOT_YET_DEFINED, "risk-based sizing unavailable")],
                report,
            )
        if premium is None or lot_size is None or multiplier is None or account_risk_amount is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUANTITY, RiskState.UNAVAILABLE, "quantity_basis",
                    rule="stop_model",
                    reason="cannot size against risk without premium, lot size, multiplier and account risk amount",
                ),
                [RiskEvidence("quantity", "basis", "missing", "risk-based sizing requires premium/lot/multiplier/equity")],
                report,
            )

        if config.stop_model == STOP_MODEL_EXPLICIT_PRICE:
            if request.stop_price is None:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.QUANTITY, RiskState.UNAVAILABLE, "stop_price",
                        rule="stop_model", reason="EXPLICIT_PRICE requires request.stop_price",
                    ),
                    [RiskEvidence("quantity", "stop_price", "None", "EXPLICIT_PRICE stop price missing")],
                    report,
                )
            if request.stop_price <= 0:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.QUANTITY, RiskState.INVALID, "stop_price",
                        value=str(request.stop_price), rule="stop_model", reason="stop price must be positive",
                    ),
                    [RiskEvidence("quantity", "stop_price", str(request.stop_price), "non-positive stop price")],
                    report,
                )
            if request.stop_price >= premium:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.QUANTITY, RiskState.INVALID, "stop_price",
                        value=str(request.stop_price), rule="stop_model",
                        reason="protective stop must sit strictly below the premium (long option)",
                    ),
                    [RiskEvidence("quantity", "stop_price", str(request.stop_price), "stop not below premium")],
                    report,
                )
            stop_price_used = request.stop_price
            per_unit_risk = (premium - stop_price_used) * lot_size * multiplier
            rule = "stop_model:EXPLICIT_PRICE"
            ev = RiskEvidence("quantity", "per_unit_risk", str(per_unit_risk), "(premium - stop) * lot_size * multiplier")
        else:  # FIXED_PCT
            stop_price_used = premium * (_DECIMAL_ONE - config.stop_loss_pct)
            per_unit_risk = premium * config.stop_loss_pct * lot_size * multiplier
            rule = "stop_model:FIXED_PCT"
            ev = RiskEvidence("quantity", "per_unit_risk", str(per_unit_risk), "premium * stop_loss_pct * lot_size * multiplier")
        report["per_unit_risk"] = per_unit_risk
        report["stop_price_used"] = stop_price_used

        if per_unit_risk <= 0:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUANTITY, RiskState.INVALID, "per_unit_risk",
                    value=str(per_unit_risk), rule=rule, reason="degenerate zero/negative risk distance",
                ),
                [RiskEvidence("quantity", "per_unit_risk", str(per_unit_risk), "non-positive risk per unit")],
                report,
            )

        max_by_risk = max(0, int(account_risk_amount // per_unit_risk))
        report["max_by_risk"] = max_by_risk
        if max_by_risk < config.minimum_quantity:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUANTITY, RiskState.BLOCKED, "max_by_risk",
                    value=str(max_by_risk), threshold=str(config.minimum_quantity), rule=rule,
                    reason=f"risk budget cannot cover the minimum quantity of {config.minimum_quantity}",
                ),
                [
                    RiskEvidence("quantity", "max_by_risk", str(max_by_risk), "below minimum quantity"),
                    ev,
                ],
                report,
            )
        if requested > max_by_risk:
            return (
                RiskDimensionVerdict(
                    RiskDimension.QUANTITY, RiskState.BLOCKED, "quantity",
                    value=str(requested), threshold=str(max_by_risk), rule=rule,
                    reason=f"requested {requested} exceeds maximum whole-lot quantity {max_by_risk}",
                ),
                [
                    RiskEvidence("quantity", "requested", str(requested), "exceeds max_by_risk"),
                    ev,
                ],
                report,
            )
        return (
            RiskDimensionVerdict(
                RiskDimension.QUANTITY, RiskState.VALID, "quantity",
                value=str(requested), threshold=str(max_by_risk), rule=rule,
                reason=f"requested {requested} within risk budget (max {max_by_risk})",
            ),
            [
                RiskEvidence("quantity", "requested", str(requested), "within risk budget"),
                ev,
            ],
            report,
        )

    def _check_daily_loss(self, request: RiskRequest, config: OptionsRiskConfig):
        account = request.account
        requested = request.requested_quantity if request.requested_quantity is not None else 1
        consumed = account.daily_loss_consumed if account is not None else None
        if consumed is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.DAILY_LOSS, RiskState.UNAVAILABLE, "account.pnl_today", rule="daily_loss",
                    reason="daily P&L is not supplied (missing != zero loss)",
                ),
                [RiskEvidence("daily_loss", "pnl_today", "None", "realized/unrealized today unknown")],
                {},
            )
        remaining = max(_DECIMAL_ZERO, config.max_daily_loss - consumed)
        report = {"daily_loss_consumed": consumed, "remaining_daily_budget": remaining}
        if consumed >= config.max_daily_loss:
            return (
                RiskDimensionVerdict(
                    RiskDimension.DAILY_LOSS, RiskState.BLOCKED, "daily_loss_consumed", str(consumed),
                    threshold=str(config.max_daily_loss), rule="daily_loss", reason="daily loss cap reached",
                ),
                [RiskEvidence("daily_loss", "consumed", str(consumed), "at/over max_daily_loss")],
                report,
            )
        per_unit_risk = None
        selection = request.selection
        premium = request.option_price
        lot_size = request.lot_size
        multiplier = (
            selection.selected_contract.instrument.multiplier
            if (selection is not None and selection.selected_contract is not None)
            else None
        )
        if config.stop_defined and premium is not None and lot_size is not None and multiplier is not None:
            if config.stop_model == STOP_MODEL_EXPLICIT_PRICE and request.stop_price is not None and request.stop_price < premium:
                per_unit_risk = (premium - request.stop_price) * lot_size * multiplier
            elif config.stop_model == STOP_MODEL_FIXED_PCT:
                per_unit_risk = premium * config.stop_loss_pct * lot_size * multiplier
        candidate_risk = per_unit_risk * requested if per_unit_risk is not None else None
        if candidate_risk is not None and candidate_risk > remaining:
            return (
                RiskDimensionVerdict(
                    RiskDimension.DAILY_LOSS, RiskState.BLOCKED, "candidate_risk", str(candidate_risk),
                    threshold=str(remaining), rule="daily_loss",
                    reason="candidate risk exceeds remaining daily budget",
                ),
                [RiskEvidence("daily_loss", "candidate_risk", str(candidate_risk), "exceeds remaining budget")],
                report,
            )
        return (
            RiskDimensionVerdict(
                RiskDimension.DAILY_LOSS, RiskState.VALID, "daily_loss_consumed", str(consumed),
                threshold=str(remaining), rule="daily_loss",
                reason=f"consumed {consumed} of {config.max_daily_loss}; budget left {remaining}",
            ),
            [RiskEvidence("daily_loss", "consumed", str(consumed), "within max_daily_loss")],
            report,
        )

    def _check_concentration(self, request: RiskRequest, config: OptionsRiskConfig):
        requested = request.requested_quantity if request.requested_quantity is not None else 1
        position = request.position
        current = position.current_contracts if position is not None else None
        exposure = position.premium_exposure if position is not None else None
        candidate_exposure = None
        premium = request.option_price
        lot_size = request.lot_size
        selection = request.selection
        multiplier = (
            selection.selected_contract.instrument.multiplier
            if (selection is not None and selection.selected_contract is not None)
            else None
        )
        if premium is not None and lot_size is not None and multiplier is not None:
            candidate_exposure = premium * lot_size * multiplier * requested
        report: dict[str, object] = {"max_by_contracts": None}

        if current is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.CONCENTRATION, RiskState.UNAVAILABLE, "position.current_contracts",
                    rule="concentration",
                    reason="existing position state is missing (missing != zero)",
                ),
                [RiskEvidence("concentration", "current_contracts", "None", "position state never assumed")],
                report,
            )
        if exposure is None:
            return (
                RiskDimensionVerdict(
                    RiskDimension.CONCENTRATION, RiskState.UNAVAILABLE, "position.premium_exposure",
                    rule="concentration", reason="existing premium exposure is missing",
                ),
                [RiskEvidence("concentration", "premium_exposure", "None", "existing exposure never assumed")],
                report,
            )

        total_contracts = current + requested
        report["max_by_contracts"] = max(0, config.max_contracts - current)
        if total_contracts > config.max_contracts:
            return (
                RiskDimensionVerdict(
                    RiskDimension.CONCENTRATION, RiskState.BLOCKED, "contracts",
                    value=str(total_contracts), threshold=str(config.max_contracts), rule="concentration",
                    reason=f"existing + candidate ({total_contracts}) exceeds max_contracts {config.max_contracts}",
                ),
                [RiskEvidence("concentration", "contracts", str(total_contracts), "exceeds max_contracts")],
                report,
            )
        if config.max_premium_exposure is not None and candidate_exposure is not None:
            total_exposure = exposure + candidate_exposure
            if total_exposure > config.max_premium_exposure:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.CONCENTRATION, RiskState.BLOCKED, "total_premium_exposure",
                        value=str(total_exposure), threshold=str(config.max_premium_exposure),
                        rule="concentration", reason="total premium exposure exceeds cap",
                    ),
                    [RiskEvidence("concentration", "total_exposure", str(total_exposure), "exceeds total exposure cap")],
                    report,
                )
        for field, cap in (
            ("same_underlying_contracts", config.max_same_underlying_contracts),
            ("same_expiry_contracts", config.max_same_expiry_contracts),
            ("same_strike_contracts", config.max_same_strike_contracts),
        ):
            if cap is None:
                continue
            value = getattr(position, field)
            if value is None:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.CONCENTRATION, RiskState.UNAVAILABLE, f"position.{field}",
                        threshold=str(cap), rule="concentration",
                        reason=f"position.{field} missing but cap {cap} is enforced",
                    ),
                    [RiskEvidence("concentration", field, "None", "cap enforced but field missing")],
                    report,
                )
            if value + requested > cap:
                return (
                    RiskDimensionVerdict(
                        RiskDimension.CONCENTRATION, RiskState.BLOCKED, field,
                        value=str(value + requested), threshold=str(cap), rule="concentration",
                        reason=f"existing {value} + candidate {requested} exceeds {field} cap {cap}",
                    ),
                    [RiskEvidence("concentration", field, str(value + requested), "exceeds concentration cap")],
                    report,
                )
        return (
            RiskDimensionVerdict(
                RiskDimension.CONCENTRATION, RiskState.VALID, "concentration",
                value=str(current + requested), rule="concentration",
                reason=f"existing {current} contract(s) + candidate {requested} within caps",
            ),
            [RiskEvidence("concentration", "contracts", str(total_contracts), "within max_contracts and caps")],
            report,
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _report_maximum(report: dict[str, object]) -> int | None:
        """Smallest binding whole-lot cap (risk/exposure/contracts/concentration)."""
        candidates = [
            report.get("max_by_risk"),
            report.get("max_by_exposure"),
            report.get("max_by_contracts"),
        ]
        present = [int(c) for c in candidates if isinstance(c, int)]
        if not present:
            return None
        return min(present)


__all__ = [
    "OptionsRiskConfig",
    "OptionsRiskEngine",
    "RISK_ENGINE_VERSION",
    "RISK_RULES_VERSION",
    "RISK_SCHEMA_VERSION",
    "STOP_MODEL_EXPLICIT_PRICE",
    "STOP_MODEL_FIXED_PCT",
    "STOP_MODEL_NOT_YET_DEFINED",
]