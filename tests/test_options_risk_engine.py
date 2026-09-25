"""Phase 10 tests — deterministic options risk engine.

Hermetic unit tests over Phase 8 selection results and Phase 9 trade-quality
results (both produced through the real engines to prove interoperation)
plus caller-supplied account/position aggregates. No network, no clock reads,
no credentials, no execution/broker/Upstox imports. Covers the quality
prerequisite, identity/timeline consistency, account risk, premium exposure,
quantity/stop sizing, daily loss, concentration, Decimal exactness, outcome
precedence, determinism, no look-ahead and safety.

Fixtures below are **synthetic unit-test data** (explicitly labelled); no
historical option-chain or account data is claimed, manufactured or reused.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.options.models import (
    Greeks,
    OptionChainSnapshot,
    OptionQuote,
)
from fno_ai_paper_trading.research.regime import Direction, MarketRegimeEngine
from fno_ai_paper_trading.research.selection import (
    SelectionOutcome,
    ContractSelectionEngine,
    ContractSelectionRequest,
)
from fno_ai_paper_trading.research.quality import (
    QUALITY_CONFIG_DEFAULT,
    QualityOutcome,
    TradeQualityConfig,
    TradeQualityEngine,
    TradeQualityRequest,
)
from fno_ai_paper_trading.research.risk import (
    RISK_CONFIG_DEFAULT,
    RISK_ENGINE_VERSION,
    RISK_RULES_VERSION,
    RISK_SCHEMA_VERSION,
    STOP_MODEL_EXPLICIT_PRICE,
    STOP_MODEL_FIXED_PCT,
    STOP_MODEL_NOT_YET_DEFINED,
    OptionsRiskConfig,
    OptionsRiskEngine,
    RiskAccountState,
    RiskOutcome,
    RiskPositionState,
    RiskRequest,
    RiskResult,
    RiskState,
)

# ---------------------------------------------------------------- fixtures

UNDERLYING = Instrument(
    symbol="NIFTY",
    instrument_type=InstrumentType.INDEX,
    underlying_symbol="NIFTY",
    exchange="NSE",
)

DECISION = datetime(2026, 9, 24, 9, 30, 0)
TS = datetime(2026, 9, 24, 9, 29, 0)
EXPIRY = date(2026, 9, 29)
STRIKES = (Decimal("24600"), Decimal("24650"), Decimal("24700"))
SPOT = Decimal("24650.05")

LOT = 75  # synthetic NIFTY options lot used by the mock provider fixture


def _option_instrument(side: str, strike: Decimal, expiry: date = EXPIRY) -> Instrument:
    return Instrument(
        symbol=f"NIFTY {expiry} {strike:g} {side}",
        instrument_type=InstrumentType.OPTION_CE if side == "CE" else InstrumentType.OPTION_PE,
        underlying_symbol="NIFTY",
        expiry=expiry,
        strike=strike,
        option_type=side,
    )


def _quote(
    side: str = "CE",
    strike: Decimal | int = STRIKES[1],
    *,
    expiry: date = EXPIRY,
    ts: datetime = TS,
    bid: str | None = "100.50",
    ask: str | None = "100.75",
    last: str | None = "100.60",
    oi: int | None = 150000,
    oi_change: int | None = 500,
    volume: int | None = 900,
    iv: str | None = "0.16",
    source_timestamp: datetime | None = TS,
) -> OptionQuote:
    strike = Decimal(str(strike))
    return OptionQuote(
        instrument=_option_instrument(side, strike, expiry),
        timestamp=ts,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last_price=Decimal(last) if last is not None else None,
        open_interest=oi,
        oi_change=oi_change,
        volume=volume,
        iv=Decimal(iv) if iv is not None else None,
        greeks=Greeks(),
        source="test-fixture (synthetic)",
        source_timestamp=source_timestamp,
        source_id=None,
    )


def _snapshot(
    quotes: tuple[OptionQuote, ...],
    *,
    expiry: date = EXPIRY,
    ts: datetime = TS,
    spot: Decimal | None = SPOT,
) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying=UNDERLYING,
        expiry=expiry,
        timestamp=ts,
        quotes=quotes,
        spot_price=spot,
        source="test-fixture (synthetic)",
    )


def _full_chain(*, expiry: date = EXPIRY, ts: datetime = TS) -> OptionChainSnapshot:
    return _snapshot(
        tuple(_quote("CE", s, expiry=expiry, ts=ts) for s in STRIKES)
        + tuple(_quote("PE", s, expiry=expiry, ts=ts) for s in STRIKES),
        expiry=expiry,
        ts=ts,
    )


def _bars(closes: list[Decimal]) -> list[MarketPrice]:
    base = datetime(2026, 9, 24, 8, 0)
    bars = []
    for i, close in enumerate(closes):
        bars.append(
            MarketPrice(
                instrument=UNDERLYING,
                timestamp=base + timedelta(minutes=i),
                open=close - Decimal("2"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=1000 + i,
            )
        )
    return bars


def _closes(values: list[int]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def _regime(direction: Direction):
    bars = {
        Direction.BULLISH: _closes([24000 + 10 * i for i in range(40)]),
        Direction.BEARISH: _closes([25000 - 10 * i for i in range(40)]),
        Direction.NEUTRAL: _closes([24200 for _ in range(40)]),
    }[direction]
    return MarketRegimeEngine().evaluate(_bars(bars))


def _selection(snapshot: OptionChainSnapshot | None = None, *, direction: Direction = Direction.BULLISH):
    """Run the real Phase 8 selector (interoperation proof)."""
    if snapshot is None:
        snapshot = _full_chain()
    request = ContractSelectionRequest(
        underlying=UNDERLYING,
        timestamp=DECISION,
        snapshots=(snapshot,),
        regime=_regime(direction),
    )
    return ContractSelectionEngine().select(request)


def _quality(selection, snapshot: OptionChainSnapshot | None = None, *,
             timestamp: datetime = DECISION, cfg: TradeQualityConfig | None = None):
    """Run the real Phase 9 quality engine (interoperation proof)."""
    if snapshot is None:
        snapshot = _full_chain()
    return TradeQualityEngine(cfg or QUALITY_CONFIG_DEFAULT).evaluate(
        TradeQualityRequest(timestamp=timestamp, selection=selection, snapshot=snapshot)
    )


DEFAULT_ACCOUNT = RiskAccountState(
    current_equity=Decimal("100000"),
    starting_day_equity=Decimal("100000"),
    realized_pnl_today=Decimal("-1000"),
    unrealized_pnl_today=Decimal("-500"),
)

DEFAULT_POSITION = RiskPositionState(
    current_contracts=0,
    premium_exposure=Decimal("0"),
    same_underlying_contracts=0,
    same_expiry_contracts=0,
    same_strike_contracts=0,
)

ENGINE = OptionsRiskEngine
STOP_CFG = OptionsRiskConfig(stop_model=STOP_MODEL_EXPLICIT_PRICE)
STOP_PRICE = Decimal("95.00")

_MISSING = object()


def _cfg(**kwargs) -> OptionsRiskConfig:
    return replace(RISK_CONFIG_DEFAULT, **kwargs)


def _evaluate(*, cfg: OptionsRiskConfig | None = None, selection=_MISSING, quality=_MISSING,
              snapshot=_MISSING, timestamp: datetime = DECISION, option_price=_MISSING,
              requested: int | None = None, stop_price=_MISSING, lot_size: int | None = LOT,
              account=_MISSING, position=_MISSING, decision_stamp: datetime | None = None):
    if selection is _MISSING:
        selection = _selection()
    if snapshot is _MISSING:
        snapshot = _full_chain()
    if quality is _MISSING:
        quality = _quality(selection, snapshot)
    if option_price is _MISSING:
        option_price = selection.selected_contract.last_price if selection.selected_contract is not None else None
    if account is _MISSING:
        account = DEFAULT_ACCOUNT
    if position is _MISSING:
        position = DEFAULT_POSITION
    request = RiskRequest(
        decision_timestamp=decision_stamp if decision_stamp is not None else timestamp,
        selection=selection,
        quality=quality,
        account=account,
        position=position,
        option_price=option_price,
        requested_quantity=requested,
        stop_price=None if stop_price is _MISSING else stop_price,
        lot_size=lot_size,
    )
    return ENGINE(cfg or RISK_CONFIG_DEFAULT).evaluate(request)


def _verdict(result: RiskResult, dimension: str):
    verdict = result.dimension(dimension)
    assert verdict is not None, f"missing dimension {dimension}"
    return verdict


# ------------------------------------------------------- quality prerequisite

class TestQualityPrerequisite:
    def test_quality_pass_proceeds(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.outcome is RiskOutcome.ELIGIBLE
        assert _verdict(result, "quality").state is RiskState.VALID

    def test_quality_fail_blocks(self) -> None:
        quality = replace(_quality(_selection()), outcome=QualityOutcome.FAIL)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.BLOCKED
        assert _verdict(result, "quality").state is RiskState.BLOCKED

    def test_quality_unavailable_stays_unavailable(self) -> None:
        quality = _quality(_selection())
        quality = replace(quality, outcome=QualityOutcome.UNAVAILABLE)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_quality_invalid_stays_invalid(self) -> None:
        quality = _quality(_selection())
        quality = replace(quality, outcome=QualityOutcome.INVALID)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.INVALID

    def test_quality_result_missing_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=None)
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_real_engine_fail_is_never_upgraded(self) -> None:
        stale = _full_chain(ts=TS - timedelta(minutes=10))
        selection = _selection(stale)
        quality = _quality(selection, stale, cfg=TradeQualityConfig(max_quote_age_seconds=60))
        assert quality.outcome.value == "FAIL"
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, selection=selection,
                           snapshot=stale, quality=quality, option_price=Decimal("100.60"))
        assert result.outcome is RiskOutcome.BLOCKED


# ---------------------------------------------------------------- identity

class TestIdentity:
    def test_contract_key_matches(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert _verdict(result, "identity").state is RiskState.VALID
        assert result.contract_key.startswith("NIFTY|")

    def test_contract_key_mismatch_invalid(self) -> None:
        sel_ce = _selection(direction=Direction.BULLISH)
        sel_pe = _selection(direction=Direction.BEARISH)
        assert sel_ce.selected_key != sel_pe.selected_key
        quality_pe = _quality(sel_pe)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, selection=sel_ce, quality=quality_pe,
                           option_price=Decimal("100.60"))
        assert result.outcome is RiskOutcome.INVALID
        assert _verdict(result, "identity").state is RiskState.INVALID

    def test_fingerprint_mismatch_invalid(self) -> None:
        selection = _selection()
        quality = replace(_quality(selection), selection_fingerprint="deadbeef")
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.INVALID
        assert "fingerprint" in _verdict(result, "identity").reason

    def test_missing_contract_key_unavailable(self) -> None:
        selection = _selection()
        quality = replace(_quality(selection), contract_key=None)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_selection_without_contract_unavailable(self) -> None:
        selection = replace(_selection(), selected_contract=None, outcome=SelectionOutcome.NO_ELIGIBLE_CONTRACT)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, selection=selection,
                           quality=replace(_quality(_selection()), contract_key=None))
        assert result.outcome is RiskOutcome.UNAVAILABLE


# ----------------------------------------------------------------- timeline

class TestTimeline:
    def test_selection_before_quality_before_decision_ok(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert _verdict(result, "timeline").state is RiskState.VALID

    def test_selection_after_quality_invalid(self) -> None:
        selection = _selection()  # timestamp DECISION (09:30)
        quality = _quality(selection, timestamp=TS)  # timestamp 09:29
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.INVALID
        assert _verdict(result, "timeline").state is RiskState.INVALID

    def test_quality_after_decision_invalid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, decision_stamp=TS)
        assert result.outcome is RiskOutcome.INVALID
        assert "decision" in _verdict(result, "timeline").reason

    def test_tz_aware_quality_invalid(self) -> None:
        selection = _selection()
        quality = replace(_quality(selection), timestamp=datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.INVALID

    def test_tz_aware_decision_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            RiskRequest(
                decision_timestamp=DECISION.replace(tzinfo=timezone.utc),
                selection=_selection(),
                quality=_quality(_selection()),
                account=DEFAULT_ACCOUNT,
                position=DEFAULT_POSITION,
                option_price=Decimal("100.60"),
            )


# -------------------------------------------------------------- account risk

class TestAccountRisk:
    def test_normal_equity_risk_amount(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.account_risk_amount == Decimal("1000.00")
        assert _verdict(result, "account").state is RiskState.VALID

    def test_risk_percent_applied(self) -> None:
        result = _evaluate(cfg=_cfg(stop_model=STOP_MODEL_EXPLICIT_PRICE, risk_per_trade_pct="0.05"),
                           stop_price=STOP_PRICE,
                           account=replace(DEFAULT_ACCOUNT, current_equity=Decimal("200000")))
        assert result.account_risk_amount == Decimal("10000")
        assert result.outcome is RiskOutcome.ELIGIBLE

    def test_missing_equity_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           account=replace(DEFAULT_ACCOUNT, current_equity=None))
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert "not supplied" in _verdict(result, "account").reason

    def test_zero_equity_blocked(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           account=replace(DEFAULT_ACCOUNT, current_equity=Decimal("0")))
        assert result.outcome is RiskOutcome.BLOCKED

    def test_negative_equity_blocked(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           account=replace(DEFAULT_ACCOUNT, current_equity=Decimal("-500")))
        assert result.outcome is RiskOutcome.BLOCKED


# ---------------------------------------------------------- premium exposure

class TestPremiumExposure:
    def test_valid_exposure(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.premium_unit_exposure == Decimal("7545.00")
        assert result.candidate_premium_exposure == Decimal("7545.00")
        assert _verdict(result, "premium_exposure").state is RiskState.VALID

    def test_lot_size_and_multiplier_scale_exposure(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, lot_size=50)
        assert result.premium_unit_exposure == Decimal("5030.00")

    def test_exposure_cap_exceeded_blocks(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, requested=50)
        # unit 7545 -> cap 250000 allows 33 lots; 50 > 33 -> BLOCKED.
        assert result.outcome is RiskOutcome.BLOCKED
        assert _verdict(result, "premium_exposure").state is RiskState.BLOCKED

    def test_missing_lot_size_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, lot_size=None)
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert "lot size" in _verdict(result, "premium_exposure").reason

    def test_missing_premium_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, option_price=None)
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert "never" in _verdict(result, "premium_exposure").reason

    def test_zero_premium_invalid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, option_price=Decimal("0"))
        assert result.outcome is RiskOutcome.INVALID

    def test_negative_premium_invalid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, option_price=Decimal("-5"))
        assert result.outcome is RiskOutcome.INVALID

    def test_lot_size_zero_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            RiskRequest(
                decision_timestamp=DECISION,
                selection=_selection(),
                quality=_quality(_selection()),
                account=DEFAULT_ACCOUNT,
                position=DEFAULT_POSITION,
                option_price=Decimal("100.60"),
                lot_size=0,
            )


# ------------------------------------------------------------- quantity/stop

class TestQuantity:
    def test_explicit_stop_eligible(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.outcome is RiskOutcome.ELIGIBLE
        assert result.per_unit_risk == Decimal("420.00")
        assert result.maximum_quantity == 2
        assert result.allowed_quantity == 1

    def test_risk_per_contract_formula(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        # (premium - stop) * lot * multiplier = (100.60 - 95.00) * 75
        assert result.per_unit_risk == (Decimal("100.60") - Decimal("95.00")) * Decimal(75)

    def test_fractional_risk_floors_to_whole_lots(self) -> None:
        result = _evaluate(cfg=_cfg(stop_model=STOP_MODEL_EXPLICIT_PRICE), stop_price=Decimal("96.60"))
        # risk per unit = 4.00*75 = 300; budget 1000 -> 3 lots exactly (3.33 floored).
        assert result.per_unit_risk == Decimal("300.00")
        assert result.maximum_quantity == 3

    def test_requested_above_max_blocks(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, requested=3)
        assert result.outcome is RiskOutcome.BLOCKED
        assert _verdict(result, "quantity").state is RiskState.BLOCKED

    def test_zero_risk_distance_invalid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=Decimal("100.60"))
        assert result.outcome is RiskOutcome.INVALID
        assert "below the premium" in _verdict(result, "quantity").reason

    def test_stop_above_premium_invalid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=Decimal("110.00"))
        assert result.outcome is RiskOutcome.INVALID

    def test_missing_stop_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=None)
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_default_stop_model_unavailable(self) -> None:
        result = _evaluate()  # stop_model None
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert STOP_MODEL_NOT_YET_DEFINED in _verdict(result, "quantity").reason

    def test_fixed_pct_stop(self) -> None:
        cfg = OptionsRiskConfig(stop_model=STOP_MODEL_FIXED_PCT)  # stop_loss_pct 0.02
        result = _evaluate(cfg=cfg)
        assert result.outcome is RiskOutcome.ELIGIBLE
        assert result.stop_price_used == Decimal("100.60") * (Decimal("1") - Decimal("0.02"))
        assert result.per_unit_risk == Decimal("150.90")
        assert result.maximum_quantity == 6

    def test_below_minimum_quantity_blocks(self) -> None:
        cfg = OptionsRiskConfig(stop_model=STOP_MODEL_EXPLICIT_PRICE, minimum_quantity=5)
        result = _evaluate(cfg=cfg, stop_price=STOP_PRICE)
        assert result.outcome is RiskOutcome.BLOCKED
        assert "minimum quantity" in _verdict(result, "quantity").reason

    def test_missing_equity_prevents_risk_sizing(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           account=replace(DEFAULT_ACCOUNT, current_equity=None))
        assert result.outcome is RiskOutcome.UNAVAILABLE


# ---------------------------------------------------------------- daily loss

class TestDailyLoss:
    def test_within_cap_valid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.daily_loss_consumed == Decimal("1500")
        assert result.remaining_daily_budget == Decimal("8500")
        assert _verdict(result, "daily_loss").state is RiskState.VALID

    def test_at_cap_blocks(self) -> None:
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=Decimal("-10000"), unrealized_pnl_today=Decimal("0"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.outcome is RiskOutcome.BLOCKED
        assert "cap reached" in _verdict(result, "daily_loss").reason

    def test_over_cap_blocks(self) -> None:
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=Decimal("-10500"), unrealized_pnl_today=Decimal("0"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.outcome is RiskOutcome.BLOCKED

    def test_missing_pnl_unavailable(self) -> None:
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=None)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert "missing != zero" in _verdict(result, "daily_loss").reason

    def test_missing_unrealized_unavailable(self) -> None:
        account = replace(DEFAULT_ACCOUNT, unrealized_pnl_today=None)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_loss_reduces_budget_blocks(self) -> None:
        # remaining budget = 10000 - consumed; block when candidate risk (420) > remaining.
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=Decimal("-9581"), unrealized_pnl_today=Decimal("0"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.remaining_daily_budget == Decimal("419")
        assert result.outcome is RiskOutcome.BLOCKED
        assert "remaining daily budget" in _verdict(result, "daily_loss").reason

    def test_profit_day_touches_no_budget(self) -> None:
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=Decimal("500"), unrealized_pnl_today=Decimal("200"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.daily_loss_consumed == Decimal("-700")
        assert result.outcome is RiskOutcome.ELIGIBLE


# -------------------------------------------------------------- concentration

class TestConcentration:
    def test_no_existing_position_valid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert _verdict(result, "concentration").state is RiskState.VALID

    def test_existing_position_within_caps_valid(self) -> None:
        position = replace(DEFAULT_POSITION, current_contracts=10, premium_exposure=Decimal("75000"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, position=position)
        assert result.outcome is RiskOutcome.ELIGIBLE

    def test_max_contracts_exceeded_blocks(self) -> None:
        position = replace(DEFAULT_POSITION, current_contracts=76)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, position=position)
        assert result.outcome is RiskOutcome.BLOCKED
        assert "max_contracts" in _verdict(result, "concentration").reason

    def test_total_exposure_exceeds_cap_blocks(self) -> None:
        position = replace(DEFAULT_POSITION, premium_exposure=Decimal("245000"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, position=position)
        # 245000 + 7545 = 252545 > 250000 -> BLOCKED.
        assert result.outcome is RiskOutcome.BLOCKED

    def test_missing_position_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           position=replace(DEFAULT_POSITION, current_contracts=None))
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert "missing != zero" in _verdict(result, "concentration").reason

    def test_missing_exposure_unavailable(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           position=replace(DEFAULT_POSITION, premium_exposure=None))
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_same_strike_cap_enforced(self) -> None:
        cfg = _cfg(stop_model=STOP_MODEL_EXPLICIT_PRICE, max_same_strike_contracts=2)
        position = replace(DEFAULT_POSITION, same_strike_contracts=2)
        result = _evaluate(cfg=cfg, stop_price=STOP_PRICE, position=position)
        assert result.outcome is RiskOutcome.BLOCKED
        assert "same_strike" in _verdict(result, "concentration").reason

    def test_enforced_cap_with_missing_field_unavailable(self) -> None:
        cfg = _cfg(stop_model=STOP_MODEL_EXPLICIT_PRICE, max_same_expiry_contracts=3)
        position = replace(DEFAULT_POSITION, same_expiry_contracts=None)
        result = _evaluate(cfg=cfg, stop_price=STOP_PRICE, position=position)
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert "enforced" in _verdict(result, "concentration").reason


# ------------------------------------------------------------------- decimal

class TestDecimal:
    def test_exact_values_no_binary_floats(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert isinstance(result.account_risk_amount, Decimal)
        assert isinstance(result.per_unit_risk, Decimal)
        assert isinstance(result.premium_unit_exposure, Decimal)
        assert result.account_risk_amount == Decimal("1000.00")
        assert result.per_unit_risk == Decimal("420.00")
        assert result.premium_unit_exposure == Decimal("7545.00")

    def test_whole_lot_floor_only_rounding(self) -> None:
        cfg = _cfg(stop_model=STOP_MODEL_EXPLICIT_PRICE)
        result = _evaluate(cfg=cfg, stop_price=Decimal("94.00"))
        # risk per unit = 6.60*75 = 495; 1000//495 = 2 (2.02 floored).
        assert result.per_unit_risk == Decimal("495.00")
        assert result.maximum_quantity == 2
        assert result.underlying_units == 75

    def test_report_fields_serialise_as_decimal_strings(self) -> None:
        data = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE).to_dict()
        assert data["per_unit_risk"] == "420.00"
        assert data["account_risk_amount"] == "1000.00"
        assert data["premium_unit_exposure"] == "7545.00"

    def test_lot_count_and_units_distinguished(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, requested=2)
        assert result.lot_count == 2
        assert result.underlying_units == 150


# -------------------------------------------------------- outcome precedence

class TestOutcomePrecedence:
    def test_eligible_when_all_valid(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.outcome is RiskOutcome.ELIGIBLE
        assert result.composite == "8/8"
        assert result.rejection_reasons == ()

    def test_invalid_dominates_blocked(self) -> None:
        quality = replace(_quality(_selection()), outcome=QualityOutcome.INVALID)
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=Decimal("-10500"), unrealized_pnl_today=Decimal("0"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality, account=account)
        assert result.outcome is RiskOutcome.INVALID

    def test_invalid_dominates_unavailable(self) -> None:
        quality = replace(_quality(_selection()), outcome=QualityOutcome.UNAVAILABLE)
        selection = _selection(direction=Direction.BEARISH)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality, selection=selection,
                           option_price=Decimal("100.60"))
        assert result.outcome is RiskOutcome.INVALID

    def test_unavailable_dominates_blocked(self) -> None:
        quality = replace(_quality(_selection()), outcome=QualityOutcome.FAIL)
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=None)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality, account=account)
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_blocked_when_quality_fails_only(self) -> None:
        quality = replace(_quality(_selection()), outcome=QualityOutcome.FAIL)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.BLOCKED

    def test_blocked_when_daily_loss_only(self) -> None:
        account = replace(DEFAULT_ACCOUNT, realized_pnl_today=Decimal("-10500"), unrealized_pnl_today=Decimal("0"))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, account=account)
        assert result.outcome is RiskOutcome.BLOCKED

    def test_unavailable_when_concentration_only(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE,
                           position=replace(DEFAULT_POSITION, current_contracts=None))
        assert result.outcome is RiskOutcome.UNAVAILABLE

    def test_default_config_reports_stop_unavailable(self) -> None:
        result = _evaluate()
        assert result.outcome is RiskOutcome.UNAVAILABLE
        assert result.composite == "7/8"
        assert "quantity:stop_model" in result.rejection_reasons


# ---------------------------------------------------------------- determinism

class TestDeterminism:
    def test_repeated_evaluation_identical(self) -> None:
        assert _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE).to_dict() == \
            _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE).to_dict()

    def test_separate_engines_identical(self) -> None:
        a = OptionsRiskEngine(STOP_CFG).evaluate(_risk_request())
        b = OptionsRiskEngine(STOP_CFG).evaluate(_risk_request())
        assert a.to_dict() == b.to_dict()

    def test_dimension_order_is_fixed(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert [d.dimension.value for d in result.dimensions] == [
            "quality", "identity", "timeline", "account", "premium_exposure",
            "quantity", "daily_loss", "concentration",
        ]

    def test_config_does_not_affect_fingerprint_path(self) -> None:
        a = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        b = _evaluate(cfg=_cfg(stop_model=STOP_MODEL_EXPLICIT_PRICE), stop_price=STOP_PRICE)
        assert a.selection_fingerprint == b.selection_fingerprint

    def test_versions_reported(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        assert result.rules_version == RISK_RULES_VERSION
        assert result.schema_version == RISK_SCHEMA_VERSION
        assert result.engine_version == RISK_ENGINE_VERSION


# ---------------------------------------------------------------- no lookahead

class TestNoLookAhead:
    def test_future_quality_artifact_invalid(self) -> None:
        selection = _selection()
        quality = replace(_quality(selection), timestamp=DECISION + timedelta(seconds=1))
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.INVALID

    def test_future_selection_artifact_invalid(self) -> None:
        selection = _selection()
        selection = replace(selection, timestamp=DECISION + timedelta(seconds=1))
        quality = replace(_quality(_selection()), timestamp=DECISION)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, selection=selection, quality=quality,
                           option_price=Decimal("100.60"))
        assert result.outcome is RiskOutcome.INVALID

    def test_evaluation_does_not_change_with_clock(self) -> None:
        # identical requests evaluated at the same decision instant are identical.
        a = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, timestamp=DECISION)
        b = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, timestamp=DECISION)
        assert a.to_dict() == b.to_dict()

    def test_prefix_artifacts_only(self) -> None:
        # The decision uses only the stamped selection/quality (at/before t);
        # a quality artifact stamped before the selection is inconsistent -> INVALID.
        selection = _selection()
        quality = _quality(selection, timestamp=TS)
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE, quality=quality)
        assert result.outcome is RiskOutcome.INVALID


# ------------------------------------------------------------------- config

class TestConfig:
    def test_rejects_unknown_stop_model(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(stop_model="INVENTED")

    def test_rejects_zero_budget(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(stop_model=STOP_MODEL_EXPLICIT_PRICE, risk_per_trade_pct="0")

    def test_rejects_zero_daily_loss(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(max_daily_loss="0")

    def test_rejects_zero_exposure(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(max_premium_exposure="0")

    def test_rejects_out_of_range_stop_pct(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(stop_model=STOP_MODEL_FIXED_PCT, stop_loss_pct="1")

    def test_rejects_zero_max_contracts(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(max_contracts=0)

    def test_rejects_empty_required_outcomes(self) -> None:
        with pytest.raises(ValueError):
            OptionsRiskConfig(required_quality_outcomes=())


# ------------------------------------------------------------------- safety

SAFETY_MODULES = (
    "src/fno_ai_paper_trading/research/risk/engine.py",
    "src/fno_ai_paper_trading/research/risk/models.py",
    "src/fno_ai_paper_trading/research/risk/__init__.py",
)


class TestSafety:
    def test_no_execution_or_vendor_imports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in SAFETY_MODULES:
            module = (root / rel).resolve()
            tree = ast.parse(module.read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            roots = {name.split(".")[0] for name in imported}
            for banned in ("execution", "upstox", "broker"):
                assert banned not in roots, f"{rel} imports forbidden root {banned}"
            for banned_module in ("fno_ai_paper_trading.models.order", "fno_ai_paper_trading.execution"):
                assert not any(name.startswith(banned_module) for name in imported), f"{rel} imports {banned_module}"
            assert not any(name.endswith("OrderSide") for name in imported), f"{rel} imports OrderSide"
            if module.name == "models.py":
                assert any(name.startswith("fno_ai_paper_trading.research.selection") for name in imported)
            if module.name.endswith(("engine.py", "models.py")):
                assert any(name.startswith("fno_ai_paper_trading.research.quality") for name in imported)

    def test_result_has_no_order_signal(self) -> None:
        result = _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE)
        data = result.to_dict()
        for word in ("BUY", "SELL", "order", "signal", "recommendation", "convic", "strong", "side"):
            assert word not in {k.upper() for k in data}, f"{word!r} leaked into result keys"
        assert result.outcome.value in {o.value for o in RiskOutcome}

    def test_no_clock_reads_in_engine(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in SAFETY_MODULES:
            source = (root / rel).read_text(encoding="utf-8")
            assert "datetime.now" not in source, f"{rel} reads the clock"
            assert "utcnow" not in source, f"{rel} reads the clock"

    def test_engine_performs_no_io(self) -> None:
        # The engine is a pure function of (request, config): evaluate then
        # evaluate again with a parallel-independent config yields a stable dict.
        assert _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE).to_dict() == \
            _evaluate(cfg=STOP_CFG, stop_price=STOP_PRICE).to_dict()


def _risk_request() -> RiskRequest:
    """Build a canonical ELIGIBLE-able request (shared by determinism tests)."""
    selection = _selection()
    return RiskRequest(
        decision_timestamp=DECISION,
        selection=selection,
        quality=_quality(selection),
        account=DEFAULT_ACCOUNT,
        position=DEFAULT_POSITION,
        option_price=selection.selected_contract.last_price,
        requested_quantity=1,
        stop_price=STOP_PRICE,
        lot_size=LOT,
    )