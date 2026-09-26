"""Phase 11 tests — deterministic options paper-trading simulation.

Hermetic unit tests over explicit paper-entry events built through the real
Phases 5-10 engines (interoperation proof), then driven through the paper
lifecycle engine. No network, no wall-clock reads (engine requires an injected
clock), no credentials, no ``execution.*``/Upstox imports in the module under
test. Covers upstream gate enforcement (selection/quality/risk/regime/snapshot),
identity/fingerprint/timestamp pins (no reselection, no gate bypass, nothing
avoidable repaired), lifecycle transitions/idempotency/duplicate conflicts/
recovery/reconciliation, fill references with missing/invalid/crossed data,
Decimal precision / lot size / premium exposure / P&L / fees / slippage, no
premium-exposure<->stop-loss conflation, session/EOD, determinism, import safety
and paper-only reporting.

Fixtures are **synthetic unit-test data** (explicitly labelled); no historical
option-chain, account or market data is claimed, manufactured or reused.
"""
from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBrokerConfig
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.options_paper import (
    EntryEvent,
    ExitEvent,
    FILL_MODEL_VERSION,
    LIFECYCLE_TRANSITIONS,
    LifecyclePhase,
    LifecycleRecord,
    MarkEvent,
    OptionsPaperConfig,
    OptionsPaperEngine,
    OptionsPaperStore,
    OptionsPaperStoreError,
    build_cumulative_report,
    build_daily_report,
    record_report,
)
from fno_ai_paper_trading.research.options.models import Greeks, OptionChainSnapshot, OptionQuote
from fno_ai_paper_trading.research.quality import (
    QUALITY_CONFIG_DEFAULT,
    QualityOutcome,
    TradeQualityEngine,
    TradeQualityRequest,
)
from fno_ai_paper_trading.research.regime import (
    MarketDataState,
    Direction,
    MarketRegimeEngine,
)
from fno_ai_paper_trading.research.risk import (
    RISK_CONFIG_DEFAULT,
    STOP_MODEL_EXPLICIT_PRICE,
    OptionsRiskConfig,
    OptionsRiskEngine,
    RiskAccountState,
    RiskOutcome,
    RiskPositionState,
    RiskRequest,
)
from fno_ai_paper_trading.research.selection import (
    ContractSelectionEngine,
    ContractSelectionRequest,
    SelectionOutcome,
)

# ---------------------------------------------------------------- fixtures

UNDERLYING = Instrument(
    symbol="NIFTY",
    instrument_type=InstrumentType.INDEX,
    underlying_symbol="NIFTY",
    exchange="NSE",
)

DECISION = datetime(2026, 9, 24, 9, 30, 0)  # Thursday, continuous-trading session
TS = datetime(2026, 9, 24, 9, 29, 0)
EXIT_TS = datetime(2026, 9, 24, 12, 0, 0)
WEEKEND_TS = datetime(2026, 9, 26, 12, 0, 0)  # Saturday
EXPIRY = date(2026, 9, 29)
STRIKES = (Decimal("24600"), Decimal("24650"), Decimal("24700"))
SPOT = Decimal("24650.05")
LOT = 75  # synthetic NIFTY options lot (provider master convention)

STOP_PRICE = Decimal("95.00")
STOP_CFG = OptionsRiskConfig(stop_model=STOP_MODEL_EXPLICIT_PRICE)

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


class _Clock:
    """Deterministic test clock (never reads the wall clock)."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t


def _option_instrument(
    side: str, strike: Decimal, expiry: date = EXPIRY, lot_size: int = LOT
) -> Instrument:
    return Instrument(
        symbol=f"NIFTY {expiry} {strike:g} {side}",
        instrument_type=InstrumentType.OPTION_CE if side == "CE" else InstrumentType.OPTION_PE,
        underlying_symbol="NIFTY",
        expiry=expiry,
        strike=strike,
        option_type=side,
        lot_size=lot_size,
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
    lot_size: int = LOT,
) -> OptionQuote:
    return OptionQuote(
        instrument=_option_instrument(side, Decimal(str(strike)), expiry, lot_size=lot_size),
        timestamp=ts,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last_price=Decimal(last) if last is not None else None,
        open_interest=150000,
        oi_change=500,
        volume=900,
        iv=Decimal("0.16"),
        greeks=Greeks(),
        source="test-fixture (synthetic)",
        source_timestamp=ts,
    )


def _snapshot(*, ts: datetime = TS) -> OptionChainSnapshot:
    quotes = tuple(_quote("CE", s) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES)
    return OptionChainSnapshot(
        underlying=UNDERLYING,
        expiry=EXPIRY,
        timestamp=ts,
        quotes=quotes,
        spot_price=SPOT,
        source="test-fixture (synthetic)",
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


def _regime_unavailable() -> object:
    """A regime report whose data is explicitly unavailable (never has_regime)."""
    return replace(_regime(Direction.BULLISH), data_state=MarketDataState.UNAVAILABLE)


def _selection(snapshot=None, direction: Direction = Direction.BULLISH):
    if snapshot is None:
        snapshot = _snapshot()
    request = ContractSelectionRequest(
        underlying=UNDERLYING,
        timestamp=DECISION,
        snapshots=(snapshot,),
        regime=_regime(direction),
    )
    return ContractSelectionEngine().select(request)


def _quality(selection, snapshot=None):
    if snapshot is None:
        snapshot = _snapshot()
    return TradeQualityEngine(QUALITY_CONFIG_DEFAULT).evaluate(
        TradeQualityRequest(timestamp=DECISION, selection=selection, snapshot=snapshot)
    )


def _risk(
    selection,
    quality,
    *,
    config: OptionsRiskConfig = STOP_CFG,
    account: RiskAccountState = DEFAULT_ACCOUNT,
    position: RiskPositionState = DEFAULT_POSITION,
    requested: int = 1,
    stop_price: Decimal | None = STOP_PRICE,
    option_price: Decimal | None = None,
    lot_size: int = LOT,
    decision_stamp: datetime = DECISION,
):
    if option_price is None:
        option_price = (
            selection.selected_contract.last_price
            if selection.selected_contract is not None
            else None
        )
    request = RiskRequest(
        decision_timestamp=decision_stamp,
        selection=selection,
        quality=quality,
        account=account,
        position=position,
        option_price=option_price,
        requested_quantity=requested,
        stop_price=stop_price,
        lot_size=lot_size,
    )
    return OptionsRiskEngine(config).evaluate(request)


def _entry_event(
    *,
    event_id: str = "entry-1",
    decision: datetime = DECISION,
    quantity: int = 1,
    snapshot=None,
    direction: Direction = Direction.BULLISH,
    selection=None,
    quality=None,
    risk=None,
) -> EntryEvent:
    if snapshot is None:
        snapshot = _snapshot()
    if selection is None:
        selection = _selection(snapshot, direction)
    if quality is None:
        quality = _quality(selection, snapshot)
    if risk is None:
        option_price = (
            selection.selected_contract.last_price
            if selection.selected_contract is not None
            else None
        )
        risk = _risk(selection, quality, option_price=option_price)
    return EntryEvent(
        event_id=event_id,
        decision_timestamp=decision,
        quantity=quantity,
        snapshot=snapshot,
        regime=_regime(direction),
        selection=selection,
        quality=quality,
        risk=risk,
        direction="LONG",
    )


def _engine(
    clock: _Clock | None = None, config: OptionsPaperConfig | None = None
) -> tuple[OptionsPaperEngine, _Clock]:
    if clock is None:
        clock = _Clock(DECISION)
    return OptionsPaperEngine(now_fn=clock.now, config=config), clock


def _exit_event(
    *,
    event_id: str = "exit-1",
    ts: datetime = EXIT_TS,
    side: str = "CE",
    strike: Decimal | int = STRIKES[1],
    bid: str | None = "100.20",
    ask: str | None = "100.45",
    last: str | None = "100.30",
    reason: str | None = "EXPLICIT",
) -> ExitEvent:
    return ExitEvent(
        event_id=event_id,
        timestamp=ts,
        quote=_quote(side, strike, ts=ts, bid=bid, ask=ask, last=last),
        reason=reason,
    )


def _open_position(engine: OptionsPaperEngine, clock: _Clock, event: EntryEvent):
    record, created = engine.submit_entry(event)
    assert created
    clock.set(EXIT_TS)
    return record


def _round_trip(record: LifecycleRecord) -> LifecycleRecord:
    return LifecycleRecord.from_dict(record.to_dict())


def _mask(record: LifecycleRecord) -> str:
    text = str(record.to_dict())
    return re.sub(r"ORD_[0-9a-f]+", "ORD_x", text)


# ------------------------------------------------------------ gate enforcement

class TestUpstreamGates:
    def test_default_risk_unavailable_preserved_no_trade(self) -> None:
        engine, _clock = _engine()
        selection = _selection()
        quality = _quality(selection)
        risk_unavailable = _risk(selection, quality, config=RISK_CONFIG_DEFAULT)
        assert risk_unavailable.outcome is RiskOutcome.UNAVAILABLE
        record, created = engine.submit_entry(
            _entry_event(selection=selection, quality=quality, risk=risk_unavailable)
        )
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "phase-10 gate" in record.reasons[0]
        assert "STOP_MODEL_NOT_YET_DEFINED" in record.reasons[0]
        assert engine.broker_fills() == 0
        assert engine.open_positions() == ()

    def test_quality_fail_no_order(self) -> None:
        engine, _clock = _engine()
        quality = replace(_quality(_selection()), outcome=QualityOutcome.FAIL)
        record, created = engine.submit_entry(_entry_event(quality=quality))
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "phase-9 gate" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_quality_unavailable_no_trade(self) -> None:
        engine, _clock = _engine()
        quality = replace(_quality(_selection()), outcome=QualityOutcome.UNAVAILABLE)
        record, _ = engine.submit_entry(_entry_event(quality=quality))
        assert record.phase is LifecyclePhase.REJECTED
        assert "phase-9 gate" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_no_selected_contract_no_order(self) -> None:
        engine, _clock = _engine()
        selection = replace(
            _selection(), selected_contract=None, outcome=SelectionOutcome.NO_ELIGIBLE_CONTRACT
        )
        record, created = engine.submit_entry(_entry_event(selection=selection))
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "phase-8 gate" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_risk_blocked_no_order(self) -> None:
        engine, _clock = _engine()
        quality = _quality(_selection())
        risk = replace(_risk(_selection(), quality), outcome=RiskOutcome.BLOCKED)
        record, created = engine.submit_entry(_entry_event(quality=quality, risk=risk))
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "phase-10 gate" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_regime_unavailable_rejected(self) -> None:
        engine, _clock = _engine()
        event = replace(_entry_event(), regime=_regime_unavailable())
        record, created = engine.submit_entry(event)
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "phase-7 gate" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_daily_loss_blocked_no_order(self) -> None:
        engine, _clock = _engine()
        broke_account = replace(
            DEFAULT_ACCOUNT,
            realized_pnl_today=Decimal("-9999"),
            unrealized_pnl_today=Decimal("-9999"),
        )
        selection = _selection()
        quality = _quality(selection)
        risk = _risk(selection, quality, account=broke_account)
        assert risk.outcome is RiskOutcome.BLOCKED
        record, _ = engine.submit_entry(
            _entry_event(selection=selection, quality=quality, risk=risk)
        )
        assert record.phase is LifecyclePhase.REJECTED
        assert "phase-10 gate" in record.reasons[0]
        assert engine.broker_fills() == 0


# ------------------------------------------------------- identity / pins / no repair

class TestIdentityPins:
    def _reject_and_no_order(self, event: EntryEvent, needle: str) -> None:
        engine, _clock = _engine()
        record, created = engine.submit_entry(event)
        assert created and record.phase is LifecyclePhase.REJECTED
        assert any(needle in why for why in record.reasons), record.reasons
        assert engine.broker_fills() == 0

    def test_quality_fingerprint_mismatch_rejected(self) -> None:
        event = _entry_event()
        event = replace(
            event, quality=replace(event.quality, selection_fingerprint="deadbeef")
        )
        self._reject_and_no_order(event, "fingerprint mismatch: quality")

    def test_risk_fingerprint_mismatch_rejected(self) -> None:
        event = _entry_event()
        event = replace(event, risk=replace(event.risk, selection_fingerprint="deadbeef"))
        self._reject_and_no_order(event, "fingerprint mismatch: risk")

    def test_contract_key_mismatch_rejected(self) -> None:
        event = _entry_event()
        other = "NIFTY|other"
        event = replace(
            event,
            quality=replace(event.quality, contract_key=other),
            risk=replace(event.risk, contract_key=other),
        )
        self._reject_and_no_order(event, "contract identity mismatch")

    def test_risk_timestamp_mismatch_rejected(self) -> None:
        selection = _selection()
        quality = _quality(selection)
        risk = _risk(selection, quality, decision_stamp=DECISION + timedelta(minutes=5))
        event = _entry_event(selection=selection, quality=quality, risk=risk)
        self._reject_and_no_order(event, "decision timestamp mismatch")

    def test_quantity_exceeds_risk_cap_rejected(self) -> None:
        event = _entry_event(quantity=2)  # risk evaluated requested=1 -> allowed=1
        self._reject_and_no_order(event, "quantity exceeds the risk cap")

    def test_lot_size_mismatch_rejected(self) -> None:
        selection = _selection()
        quality = _quality(selection)
        risk = _risk(selection, quality, lot_size=50)
        event = _entry_event(selection=selection, quality=quality, risk=risk)
        self._reject_and_no_order(event, "lot size mismatch")

    def test_exposure_mismatch_rejected(self) -> None:
        selection = _selection()
        quality = _quality(selection)
        risk = replace(_risk(selection, quality), candidate_premium_exposure=Decimal("999999"))
        event = _entry_event(selection=selection, quality=quality, risk=risk)
        self._reject_and_no_order(event, "premium exposure mismatch")

    def test_missing_decision_premium_rejected(self) -> None:
        snapshot = _snapshot()
        selection = _selection(snapshot)
        selection = replace(
            selection,
            selected_contract=replace(selection.selected_contract, last_price=None),
        )
        quality = _quality(selection, snapshot)
        risk = _risk(selection, quality, option_price=Decimal("100.60"))
        assert risk.outcome is RiskOutcome.ELIGIBLE
        event = _entry_event(snapshot=snapshot, selection=selection, quality=quality, risk=risk)
        self._reject_and_no_order(event, "no decision premium")

    def test_unavailable_never_repairs(self) -> None:
        engine, _clock = _engine()
        event = _entry_event()
        event = replace(event, risk=replace(event.risk, outcome=RiskOutcome.UNAVAILABLE))
        record, _ = engine.submit_entry(event)
        assert record.phase is LifecyclePhase.REJECTED
        assert "UNAVAILABLE" in " ".join(record.reasons)
        assert engine.broker_fills() == 0


# ---------------------------------------------------------------- lifecycle

class TestLifecycle:
    def test_happy_path_full_trajectory(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        assert record.phase is LifecyclePhase.POSITION_OPEN
        record, applied = engine.request_exit(_exit_event())
        assert applied
        assert record.phase is LifecyclePhase.POSITION_CLOSED
        expected = [
            LifecyclePhase.CANDIDATE,
            LifecyclePhase.PAPER_ORDER_CREATED,
            LifecyclePhase.PAPER_FILLED,
            LifecyclePhase.POSITION_OPEN,
            LifecyclePhase.EXIT_REQUESTED,
            LifecyclePhase.EXIT_FILLED,
            LifecyclePhase.POSITION_CLOSED,
        ]
        assert [step.phase for step in record.history] == expected
        assert record.entry is not None and record.exit is not None
        assert record.financials.gross_realized_pnl is not None
        assert engine.broker_fills() == 2

    def test_duplicate_identical_event_is_idempotent(self) -> None:
        engine, _clock = _engine()
        event = _entry_event()
        first, created1 = engine.submit_entry(event)
        assert created1
        again, created2 = engine.submit_entry(event)
        assert not created2 and again.record_id == first.record_id
        assert engine.broker_fills() == 1
        assert len(engine.records()) == 1

    def test_conflicting_duplicate_rejected_original_untouched(self) -> None:
        engine, _clock = _engine()
        original, _ = engine.submit_entry(_entry_event())
        conflict = _entry_event(event_id="entry-1", quantity=2)
        rejected, created = engine.submit_entry(conflict)
        assert created and rejected.phase is LifecyclePhase.REJECTED
        assert "duplicate_conflict" in rejected.reasons[0]
        keep = engine.record_for_event("entry-1")
        assert keep.record_id == original.record_id
        assert keep.quantity == 1 and keep.phase is LifecyclePhase.POSITION_OPEN
        assert engine.broker_fills() == 1
        assert len(engine.records()) == 1

    def test_exit_for_unknown_contract_noop(self) -> None:
        engine, clock = _engine()
        _open_position(engine, clock, _entry_event())
        record, applied = engine.request_exit(_exit_event(strike=STRIKES[2]))
        assert applied is False and record is None
        assert len(engine.open_positions()) == 1

    def test_out_of_order_exit_ignored(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        record, applied = engine.request_exit(_exit_event(ts=DECISION - timedelta(minutes=1)))
        assert applied is False and record.phase is LifecyclePhase.POSITION_OPEN
        assert "precedes entry fill" in record.limitations[-1]
        assert engine.broker_fills() == 1

    def test_exit_retry_closes_after_missing_data(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        requested, applied = engine.request_exit(_exit_event(bid=None, last=None, event_id="exit-a"))
        assert applied and requested.phase is LifecyclePhase.EXIT_REQUESTED
        assert requested.exit is None and engine.broker_fills() == 1
        closed, applied2 = engine.request_exit(_exit_event(event_id="exit-b"))
        assert applied2 and closed.phase is LifecyclePhase.POSITION_CLOSED
        assert closed.exit is not None and engine.broker_fills() == 2

    def test_reconcile_open_position_recorded_not_reconciled(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        result = engine.reconcile(record.record_id)
        assert result.phase is LifecyclePhase.POSITION_OPEN
        assert result.reconciliation is not None and result.reconciliation.ok is False
        assert result.reconciliation.violations == (
            "position is not POSITION_CLOSED; cannot reconcile",
        )
        assert "not closed" in result.limitations[-1]

    def test_reconcile_clean_closed(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        closed, _ = engine.request_exit(_exit_event())
        result = engine.reconcile(closed.record_id)
        assert result.phase is LifecyclePhase.RECONCILED
        assert result.reconciliation.ok is True and result.reconciliation.violations == ()

    def test_no_invalid_transition_ever_created(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        closed, _ = engine.request_exit(_exit_event())
        history = closed.history
        for previous, current in zip(history, history[1:]):
            assert current.phase in LIFECYCLE_TRANSITIONS[previous.phase]

    def test_weekend_entry_rejected_session_gate(self) -> None:
        engine, _clock = _engine()
        event = _entry_event(event_id="weekend", decision=WEEKEND_TS)
        record, created = engine.submit_entry(event)
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "trading session" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_weekend_exit_ignored_session_gate(self) -> None:
        engine, clock = _engine()
        record = _open_position(engine, clock, _entry_event())
        record, applied = engine.request_exit(_exit_event(ts=WEEKEND_TS))
        assert applied is False and record.phase is LifecyclePhase.POSITION_OPEN
        assert "session gate" in record.limitations[-1]
        assert engine.broker_fills() == 1


# ------------------------------------------------------------ recovery / persistence

class TestRecoveryAndPersistence:
    def test_persist_and_restart_recovery(self, tmp_path: Path) -> None:
        store = OptionsPaperStore(tmp_path, account="acc")
        engine, clock = _engine()
        event = _entry_event()
        opened, _ = engine.submit_entry(event)
        store.save_record(opened)
        assert store.load_verified(opened.record_id).phase is LifecyclePhase.POSITION_OPEN

        restarted = OptionsPaperEngine(now_fn=clock.now)
        restored = restarted.restore(store.load_all())
        assert restored == 1
        reopened = restarted.record_for_event(event.event_id)
        assert reopened.contract_key == event.selection.selected_key
        clock.set(EXIT_TS)
        closed, _ = restarted.request_exit(_exit_event())
        assert closed.phase is LifecyclePhase.POSITION_CLOSED
        store.save_record(closed)
        assert closed.financials.net_realized_pnl is not None

    def test_restarted_engine_restore_rejects_conflicts(self, tmp_path: Path) -> None:
        store = OptionsPaperStore(tmp_path, account="acc")
        engine, clock = _engine()
        opened, _ = engine.submit_entry(_entry_event())
        store.save_record(opened)
        restarted = OptionsPaperEngine(now_fn=clock.now)
        restarted.restore(store.load_all())
        with pytest.raises(ValueError, match="duplicate record"):
            restarted.restore(store.load_all())

    def test_store_round_trip_and_tamper_detection(self, tmp_path: Path) -> None:
        store = OptionsPaperStore(tmp_path, account="acc")
        engine, _clock = _engine()
        record, _ = engine.submit_entry(_entry_event())
        assert record.to_dict() == _round_trip(record).to_dict()
        store.save_record(record)
        loaded = store.load_verified(record.record_id)
        assert loaded.to_dict() == record.to_dict()
        path = store._path(record.record_id)
        tampered = path.read_text().replace('"quantity":1', '"quantity":9')
        path.write_text(tampered, encoding="utf-8")
        with pytest.raises(OptionsPaperStoreError, match="sha256 mismatch"):
            store.load_verified(record.record_id)


# --------------------------------------------------------------- fills / financials

class TestFillsAndFinancials:
    def _opened(self):
        engine, clock = _engine()
        return engine, _open_position(engine, clock, _entry_event())

    def test_entry_fills_at_ask_plus_slippage(self) -> None:
        engine, opened = self._opened()
        ask = Decimal("100.75")
        assert opened.entry.reference_used == "ask"
        assert opened.entry.fill_price == ask * Decimal("1.001")
        assert opened.entry.premium_value == Decimal("1") * opened.entry.fill_price * 1
        assert opened.entry.premium_exposure == Decimal("100.60") * 1 * LOT * 1
        assert opened.entry.candidate_premium_exposure == opened.entry.premium_exposure

    def test_exact_net_pnl_gross_and_fees(self) -> None:
        engine, clock = _engine()
        opened, created = engine.submit_entry(_entry_event())
        assert created
        clock.set(EXIT_TS)
        closed, _ = engine.request_exit(_exit_event())
        entry = closed.entry.fill_price
        exit_price = Decimal("100.20") * Decimal("0.999")
        gross = (exit_price - entry) * 1 * LOT * 1
        fees = closed.entry.commission + closed.exit.commission
        assert closed.financials.gross_realized_pnl == gross
        assert closed.financials.net_realized_pnl == gross - fees
        assert closed.financials.entry_commission == closed.entry.commission
        assert closed.financials.exit_commission == closed.exit.commission
        assert closed.financials.close_day == "2026-09-24"

    def test_entry_last_price_fallback(self) -> None:
        engine, _clock = _engine()
        event = _entry_event()
        no_ask = replace(event.selection.selected_contract, ask=None)
        event = replace(event, selection=replace(event.selection, selected_contract=no_ask))
        opened, _ = engine.submit_entry(event)
        assert opened.entry.reference_used == "last_price"
        assert opened.entry.fill_price == Decimal("100.60") * Decimal("1.001")

    def test_entry_missing_reference_rejected_no_fill(self) -> None:
        engine, _clock = _engine()
        event = _entry_event()
        no_ref = replace(event.selection.selected_contract, ask=None, last_price=None)
        event = replace(event, selection=replace(event.selection, selected_contract=no_ref))
        record, created = engine.submit_entry(event)
        assert created and record.phase is LifecyclePhase.REJECTED
        assert any("no decision premium" in why or "no fill reference" in why for why in record.reasons)
        assert engine.broker_fills() == 0

    def test_crossed_entry_market_rejected(self) -> None:
        engine, _clock = _engine()
        event = _entry_event()
        crossed = replace(
            event.selection.selected_contract, bid=Decimal("101.00"), ask=Decimal("100.90")
        )
        event = replace(event, selection=replace(event.selection, selected_contract=crossed))
        record, created = engine.submit_entry(event)
        assert created and record.phase is LifecyclePhase.REJECTED
        assert "crossed market" in record.reasons[0]
        assert engine.broker_fills() == 0

    def test_exit_uses_bid_minus_slippage(self) -> None:
        engine, clock = _engine()
        opened, _ = engine.submit_entry(_entry_event())
        clock.set(EXIT_TS)
        closed, _ = engine.request_exit(_exit_event(bid="100.20"))
        assert closed.exit.reference_used == "bid"
        assert closed.exit.fill_price == Decimal("100.20") * Decimal("0.999")

    def test_exit_missing_reference_limitation_no_fabrication(self) -> None:
        engine, clock = _engine()
        _open_position(engine, clock, _entry_event())
        record, applied = engine.request_exit(_exit_event(bid=None, last=None))
        assert applied
        assert record.phase is LifecyclePhase.EXIT_REQUESTED
        assert "no exit fill reference" in record.limitations[-1]
        assert record.limitations[-1].endswith("position remains open")
        assert record.exit is None
        assert engine.broker_fills() == 1

    def test_exit_crossed_market_limitation(self) -> None:
        engine, clock = _engine()
        _open_position(engine, clock, _entry_event())
        record, applied = engine.request_exit(_exit_event(bid="101.00", ask="100.90"))
        assert applied and record.phase is LifecyclePhase.EXIT_REQUESTED
        assert "crossed" in record.limitations[-1]
        assert record.exit is None
        assert engine.broker_fills() == 1

    def test_no_premium_exposure_stop_loss_conflation(self) -> None:
        engine, opened = self._opened()
        assert opened.entry.premium_exposure == opened.entry.candidate_premium_exposure
        data = opened.to_dict()
        assert "premium_exposure" in data["entry"]
        assert "premium_value" in data["entry"]
        assert "stop_price_used" not in data["entry"]
        assert "stop_model" not in data["entry"]
        assert "no paper stop-loss simulated" in opened.limitations[-1]

    def test_slippage_override(self) -> None:
        config = OptionsPaperConfig(
            broker_config=replace(
                PaperBrokerConfig(), slippage_rate=Decimal("0.0005"),
            )
        )
        engine, _clock = _engine(config=config)
        opened, _ = engine.submit_entry(_entry_event())
        assert opened.entry.reference_used == "ask"
        assert opened.entry.fill_price == Decimal("100.75") * Decimal("1.0005")
        assert opened.entry.slippage_rate == Decimal("0.0005")

    def test_mark_reliable_sets_unrealized(self) -> None:
        engine, clock = _engine()
        opened = _open_position(engine, clock, _entry_event())
        marked = engine.mark_market(
            opened.record_id,
            MarkEvent(event_id="m1", timestamp=EXIT_TS, quote=_quote(last="102.00")),
        )
        expected = (Decimal("102.00") - opened.entry.fill_price) * 1 * LOT * 1
        assert marked.financials.unrealized_latest == expected
        assert marked.financials.marks[-1].reference_used == "last_price"

    def test_mark_unreliable_is_limitation(self) -> None:
        engine, clock = _engine()
        opened = _open_position(engine, clock, _entry_event())
        marked = engine.mark_market(
            opened.record_id,
            MarkEvent(
                event_id="m2", timestamp=EXIT_TS,
                quote=_quote(bid=None, ask=None, last=None),
            ),
        )
        assert marked.financials.unrealized_latest is None
        assert any("unreliable mark" in lim for lim in marked.limitations)

    def test_mark_on_different_contract_ignored(self) -> None:
        engine, clock = _engine()
        opened = _open_position(engine, clock, _entry_event())
        marked = engine.mark_market(
            opened.record_id,
            MarkEvent(event_id="m3", timestamp=EXIT_TS, quote=_quote(strike=STRIKES[2])),
        )
        assert marked.financials.unrealized_latest is None
        assert any("different contract" in lim for lim in marked.limitations)


# --------------------------------------------------------------- determinism / safety

class TestDeterminismAndSafety:
    def test_identical_runs_match_modulo_order_ids(self) -> None:
        a, _clock_a = _engine()
        b, _clock_b = _engine()
        ra, _ = a.submit_entry(_entry_event())
        rb, _ = b.submit_entry(_entry_event())
        assert _mask(ra) == _mask(rb)

    def test_no_wall_clock_reads_in_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in [
            "src/fno_ai_paper_trading/paper_track/options_paper/engine.py",
            "src/fno_ai_paper_trading/paper_track/options_paper/models.py",
            "src/fno_ai_paper_trading/paper_track/options_paper/fill_model.py",
            "src/fno_ai_paper_trading/paper_track/options_paper/persistence.py",
            "src/fno_ai_paper_trading/paper_track/options_paper/report.py",
        ]:
            path = root / rel
            source = path.read_text(encoding="utf-8")
            assert "datetime.now" not in source, f"{rel} reads the clock"
        engine_source = (root / "src/fno_ai_paper_trading/paper_track/options_paper/engine.py").read_text(
            encoding="utf-8"
        )
        assert "now_fn" in engine_source

    def test_import_safety_no_execution_upstox_credentials(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = root / "src/fno_ai_paper_trading/paper_track/options_paper"
        for module in package.glob("*.py"):
            source = module.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            roots = {name.split(".")[0] for name in imported}
            for banned in ("execution", "upstox"):
                assert banned not in roots, f"{module.name} imports forbidden root {banned}"
            for banned_module in (
                "fno_ai_paper_trading.execution",
                "fno_ai_paper_trading.broker.base",
                "fno_ai_paper_trading.credentials",
            ):
                assert not any(name.startswith(banned_module) for name in imported), (
                    f"{module.name} imports forbidden module {banned_module}"
                )

    def test_fill_model_versioned(self) -> None:
        assert FILL_MODEL_VERSION == "options-paper-fill-1"

    def test_records_are_always_paper_only(self) -> None:
        engine, clock = _engine()
        opened, _ = engine.submit_entry(_entry_event())
        clock.set(EXIT_TS)
        closed, _ = engine.request_exit(_exit_event())
        day = date(2026, 9, 24)
        daily = build_daily_report(engine.records(), day)
        assert daily["aggregate"]["paper_only"] is True
        assert daily["aggregate"]["fill_model_version"] == FILL_MODEL_VERSION
        assert record_report(closed)["paper_only"] is True
        cumulative = build_cumulative_report(engine.records())
        assert cumulative["aggregate"]["paper_only"] is True

    def test_report_fingerprint_stable_across_engines(self) -> None:
        a, clock_a = _engine()
        b, clock_b = _engine()
        a.submit_entry(_entry_event())
        b.submit_entry(_entry_event())
        clock_a.set(EXIT_TS)
        clock_b.set(EXIT_TS)
        a.request_exit(_exit_event())
        b.request_exit(_exit_event())
        day = date(2026, 9, 24)
        reports = [build_daily_report(engine.records(), day) for engine in (a, b)]
        assert reports[0]["aggregate"] == reports[1]["aggregate"]
        assert reports[0]["fingerprint"] == reports[1]["fingerprint"]
        assert reports[0]["fingerprint"] != ""

        c, _clock_c = _engine()
        c.submit_entry(_entry_event(event_id="c-rejected", quantity=2))
        rejected_only = build_daily_report(c.records(), day)
        assert rejected_only["aggregate"] != reports[0]["aggregate"]
        assert rejected_only["fingerprint"] != reports[0]["fingerprint"]

    def test_counts_views(self) -> None:
        engine, _clock = _engine()
        engine.submit_entry(_entry_event(event_id="good"))
        engine.submit_entry(_entry_event(event_id="bad", quantity=2))
        counts = engine.counts()
        assert counts["total"] == 2 and counts["rejected"] == 1 and counts["open"] == 1
        assert len(engine.records()) == 2 and len(engine.open_positions()) == 1

    def test_engine_constructor_requires_clock(self) -> None:
        with pytest.raises(ValueError, match="injected now_fn"):
            OptionsPaperEngine(now_fn=None)

    def test_direction_long_only(self) -> None:
        event = _entry_event()
        with pytest.raises(ValueError, match="LONG"):
            replace(event, direction="SHORT")