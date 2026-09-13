"""Tests for WS 7.9 — controlled Upstox live F&O execution integration test.

Everything is offline and deterministic: no network access, no real broker, no
real Upstox call. Coverage targets, in order:

  1. LIVE_EXECUTION_TEST gate (default-closed, consent + fingerprint + expiry)
  2. single-entry/single-exit state machine obligations
  3. CALL/PUT signal mapping and the frozen MA(5,21) decision
  4. F&O instrument resolution and client-side margin estimate
  5. RiskPreflight (session open, RiskManager, Watchdog STOP, margin cap, overnight)
  6. Upstox execution adapter (auth/quote/place/status/cancel/positions/errors)
  7. manager happy path (entry -> hold -> exit -> FLAT -> COMPLETE)
  8. manager refusal paths (HOLD signal, no confirm, gate closed no order)
  9. manager failure paths (rejected order, fill timeout, non-flat reconcile)
 10. audit redaction (no token can ever be persisted)
 11. alerts keep the PAPER TRADING label
 12. CLI smoke is opt-in: dry-run passes, gate-closed live send refuses
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.alerting.alerts import PAPER_TRADING_LABEL, Alert, AlertCategory, AlertLevel
from fno_ai_paper_trading.alerting.engine import AlertEngine, CollectingAlertSink
from fno_ai_paper_trading.alerting.health import Watchdog
from fno_ai_paper_trading.config.settings import (
    Environment,
    LiveExecutionTestSettings,
    PaperSettings,
    UpstoxSettings,
)
from fno_ai_paper_trading.data.errors import AuthenticationError, RateLimitError, UnavailableError
from fno_ai_paper_trading.data.market_hours import market_session
from fno_ai_paper_trading.execution.audit import ExecutionAudit, new_run_id, redact
from fno_ai_paper_trading.execution.errors import (
    InstrumentValidationError,
    UpstoxExecutionError,
    UpstoxOrderRejectedError,
)
from fno_ai_paper_trading.execution.gate import (
    ExecutionMode,
    GateDecision,
    LiveExecutionTestGate,
    consent_fingerprint,
    sha256_hex,
)
from fno_ai_paper_trading.execution.instrument import (
    estimate_required_margin,
    resolve_fno_instrument,
)
from fno_ai_paper_trading.execution.manager import LiveExecutionTestManager
from fno_ai_paper_trading.execution.memory import MemoryExecutionAdapter
from fno_ai_paper_trading.execution.risk import FINALIZATION_BUFFER_SECONDS, OvernightGuard, RiskPreflight
from fno_ai_paper_trading.execution.signal import (
    CallPutSignal,
    decide_call_put,
    signal_to_call_put,
)
from fno_ai_paper_trading.execution.state import (
    ALLOWED_TRANSITIONS,
    ExecutionTestState,
    ExecutionTestStateMachine,
)
from fno_ai_paper_trading.execution.upstox import (
    ExecutionAck,
    ExecutionAdapter,
    ExecutionPosition,
    UpstoxCredentials,
    UpstoxExecutionAdapter,
    _map_order_status,
)
from fno_ai_paper_trading.models.enums import (
    InstrumentType,
    MarketPhase,
    OrderSide,
    OrderStatus,
    Signal,
)
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.utils.functions import new_id
from fno_ai_paper_trading.utils.http import HttpError, HttpResponse

NOW = datetime(2026, 9, 14, 12, 0)  # a Monday 12:00 IST — NSE session OPEN
KEY = "NSE_FO|NIFTY 24 DEC 2026 24500 CE"
LOT = 75
PREMIUM = Decimal("250")


def _settings() -> PaperSettings:
    return PaperSettings(
        environment=Environment.PAPER,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("10000"),
        commission_rate=Decimal("0.0003"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0.001"),
    )


def _option() -> object:
    return resolve_fno_instrument(
        underlying="NIFTY",
        expiry=date(2026, 12, 24),
        strike=Decimal("24500"),
        option_type="CE",
        exchange_token=KEY,
        lot_size=LOT,
    )


def _bar(close: Decimal, ts: datetime) -> MarketPrice:
    return MarketPrice(
        instrument=_index_for_bar(),
        timestamp=ts,
        open=close - Decimal("1"),
        high=close + Decimal("2"),
        low=close - Decimal("3"),
        close=close,
        volume=1000,
        open_interest=4000,
    )


def _index_for_bar():
    from fno_ai_paper_trading.models.instruments import Instrument

    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="Nifty 50",
        exchange="NSE",
        exchange_token="NSE_INDEX|Nifty 50",
        lot_size=1,
    )


def _buy_bars(count: int = 29) -> list[MarketPrice]:
    """Flat run followed by one strong up bar: the last bar is a fresh BUY
    crossover (fast MA crosses above the slow MA there) while staying inside
    the Watchdog freshness window."""
    start = NOW - timedelta(minutes=5 * count)
    closes = [Decimal("24200")] * (count - 1) + [Decimal("24300")]
    return [
        _bar(close=value, ts=start + timedelta(minutes=5 * i))
        for i, value in enumerate(closes)
    ]


def _hold_bars(count: int = 30) -> list[MarketPrice]:
    start = NOW - timedelta(minutes=5 * count)
    return [_bar(close=Decimal("24200"), ts=start + timedelta(minutes=5 * i)) for i in range(count)]


def _gate_closed() -> LiveExecutionTestGate:
    return LiveExecutionTestGate(
        LiveExecutionTestSettings(enabled=False),
        now_fn=lambda: NOW,
    )


def _open_consent(token: str) -> dict[str, object]:
    created = NOW - timedelta(hours=1)
    return {
        "operator": "tester",
        "purpose": "controlled live execution integration test",
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + timedelta(hours=6)).isoformat(timespec="seconds"),
        "token_fingerprint_sha256": consent_fingerprint(token),
    }


def _memory(price: str = "250") -> MemoryExecutionAdapter:
    return MemoryExecutionAdapter(
        prices={KEY: Decimal(price)},
        account_id="tester-account",
        slippage=Decimal("0"),
    )


def _manager(adapter, *, live_enabled=False, confirm=False, consent=None, token="t", **kwargs):
    live = LiveExecutionTestSettings(enabled=live_enabled)
    gate = LiveExecutionTestGate(
        live,
        now_fn=lambda: NOW,
        token_loader=lambda: token,
        consent_loader=lambda: consent,
    )
    preflight = RiskPreflight(
        _settings(),
        RiskManager(_settings()),
        Watchdog(now=lambda: NOW),
        Decimal("100000"),
    )
    base = dict(
        settings=_settings(),
        live_settings=live,
        gate=gate,
        adapter=adapter,
        preflight=preflight,
        clock=lambda: NOW,
        sleep=lambda _s: None,
        confirm_live_enablement=confirm,
        hold_seconds=0.0,
        poll_seconds=0.0,
        order_timeout_seconds=30.0,
    )
    base.update(kwargs)
    return LiveExecutionTestManager(**base)


# ------------------------------------------------------------------ gate


class TestGate:
    def test_closed_by_default(self):
        decision = _gate_closed().decision()
        assert decision.ok is False
        assert decision.mode is ExecutionMode.PAPER
        assert any("enable" in r.lower() for r in decision.reasons)

    def test_open_when_env_flag_consent_and_token_match(self):
        token = "tok123"
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: token,
            consent_loader=lambda: _open_consent(token),
        )
        decision = gate.decision()
        assert decision.ok is True
        assert decision.mode is ExecutionMode.LIVE_EXECUTION_TEST

    def test_mode_is_paper_when_closed(self):
        assert _gate_closed().mode() is ExecutionMode.PAPER

    def test_expired_consent_refused(self):
        token = "tok123"
        consent = _open_consent(token)
        consent["expires_at"] = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: token,
            consent_loader=lambda: consent,
        )
        decision = gate.decision()
        assert decision.ok is False
        assert any("expired" in r for r in decision.reasons)

    def test_window_over_cap_refused(self):
        token = "tok123"
        consent = _open_consent(token)
        consent["created_at"] = (NOW - timedelta(hours=48)).isoformat(timespec="seconds")
        consent["expires_at"] = NOW.isoformat(timespec="seconds")
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True, expiry_hours=24.0),
            now_fn=lambda: NOW,
            token_loader=lambda: token,
            consent_loader=lambda: consent,
        )
        assert gate.decision().ok is False

    def test_fingerprint_mismatch_refused(self):
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: "other-token",
            consent_loader=lambda: _open_consent("original"),
        )
        decision = gate.decision()
        assert decision.ok is False
        assert any("fingerprint" in r for r in decision.reasons)

    def test_no_token_refused(self):
        gate = LiveExecutionTestGate(
            LiveExecutionTestSettings(enabled=True),
            now_fn=lambda: NOW,
            token_loader=lambda: "",
            consent_loader=lambda: _open_consent("tok"),
        )
        assert gate.decision().ok is False

    def test_consent_fingerprint_is_stable_sha256(self):
        assert consent_fingerprint("abc") == sha256_hex("abc")
        assert consent_fingerprint("abc") == consent_fingerprint("abc")

    def test_live_mode_never_returned(self):
        assert _gate_closed().mode() is not ExecutionMode.LIVE

    def test_decision_summary(self):
        summary = _gate_closed().decision().summary
        assert summary.startswith("gate closed")


# -------------------------------------------------------- state machine


class TestStateMachine:
    def test_single_entry_only(self):
        machine = ExecutionTestStateMachine()
        machine.transition(ExecutionTestState.AUTHENTICATING)
        machine.transition(ExecutionTestState.INSTRUMENT_VALIDATED)
        machine.give_entry("ORD-1")
        with pytest.raises(ValueError):
            machine.give_entry("ORD-2")
        assert machine.entry_given == 1

    def test_exit_refused_before_any_entry(self):
        machine = ExecutionTestStateMachine()
        with pytest.raises(ValueError):
            machine.give_exit("ORD-x")

    def test_single_exit_only(self):
        machine = ExecutionTestStateMachine()
        machine.transition(ExecutionTestState.AUTHENTICATING)
        machine.transition(ExecutionTestState.INSTRUMENT_VALIDATED)
        machine.give_entry("ORD-1")
        machine.transition(ExecutionTestState.ENTRY_ACKNOWLEDGED)
        machine.transition(ExecutionTestState.ENTRY_FILLED)
        machine.transition(ExecutionTestState.HOLDING)
        machine.give_exit("ORD-2")
        with pytest.raises(ValueError):
            machine.give_exit("ORD-3")
        assert machine.exit_given == 1

    def test_no_reentry_after_complete(self):
        machine = ExecutionTestStateMachine()
        machine.transition(ExecutionTestState.AUTHENTICATING)
        machine.transition(ExecutionTestState.INSTRUMENT_VALIDATED)
        machine.give_entry("ORD-1")
        machine.transition(ExecutionTestState.ENTRY_ACKNOWLEDGED)
        machine.transition(ExecutionTestState.ENTRY_FILLED)
        machine.transition(ExecutionTestState.HOLDING)
        machine.give_exit("ORD-2")
        machine.transition(ExecutionTestState.EXIT_ACKNOWLEDGED)
        machine.transition(ExecutionTestState.EXIT_FILLED)
        machine.transition(ExecutionTestState.FLAT_RECONCILED)
        machine.transition(ExecutionTestState.COMPLETE)
        assert machine.is_terminal
        with pytest.raises(ValueError):
            machine.give_entry("ORD-3")

    def test_bare_entry_request_transition_refused(self):
        machine = ExecutionTestStateMachine()
        with pytest.raises(ValueError):
            machine.transition(ExecutionTestState.ENTRY_REQUESTED)

    def test_fail_then_no_further_trading(self):
        machine = ExecutionTestStateMachine()
        machine.transition(ExecutionTestState.AUTHENTICATING)
        machine.fail("bad")
        assert machine.is_terminal
        with pytest.raises(ValueError):
            machine.transition(ExecutionTestState.AUTHENTICATING)

    def test_abort_only_pre_trade(self):
        machine = ExecutionTestStateMachine()
        machine.transition(ExecutionTestState.AUTHENTICATING)
        machine.abort("preflight")
        assert machine.state is ExecutionTestState.ABORTED

    def test_allowed_edges_use_only_known_states(self):
        for source, target in ALLOWED_TRANSITIONS:
            assert isinstance(source, ExecutionTestState)
            assert isinstance(target, ExecutionTestState)
        assert len(ALLOWED_TRANSITIONS) == len(set(ALLOWED_TRANSITIONS))


# ------------------------------------------------------------- signal


class TestSignal:
    def test_mapping(self):
        assert signal_to_call_put(Signal.BUY) is CallPutSignal.CALL
        assert signal_to_call_put(Signal.SELL) is CallPutSignal.PUT
        assert signal_to_call_put(Signal.HOLD) is CallPutSignal.NONE

    def test_order_side(self):
        assert CallPutSignal.CALL.order_side is OrderSide.BUY
        assert CallPutSignal.PUT.order_side is OrderSide.SELL

    def test_buy_bars_decide_call(self):
        decision = decide_call_put(_buy_bars())
        assert decision.leg is CallPutSignal.CALL
        assert decision.actionable

    def test_hold_bars_decide_none(self):
        decision = decide_call_put(_hold_bars())
        assert decision.leg is CallPutSignal.NONE
        assert not decision.actionable

    def test_insufficient_bars_are_hold(self):
        assert decide_call_put(_buy_bars()[:5]).leg is CallPutSignal.NONE


# ----------------------------------------------------------- instrument


class TestInstrument:
    def test_valid_option(self):
        instrument = _option()
        assert instrument.is_option()
        assert instrument.option_type == "CE"
        assert instrument.lot_size == LOT

    def test_expiry_must_be_future(self):
        with pytest.raises(InstrumentValidationError):
            resolve_fno_instrument(
                underlying="NIFTY",
                expiry=date(2020, 1, 1),
                strike=Decimal("24500"),
                option_type="CE",
                exchange_token=KEY,
                lot_size=75,
            )

    def test_key_requires_pipe(self):
        with pytest.raises(InstrumentValidationError):
            resolve_fno_instrument(
                underlying="NIFTY",
                expiry=date(2026, 12, 24),
                strike=Decimal("24500"),
                option_type="CE",
                exchange_token="NONEXISTENTKEY",
                lot_size=75,
            )

    def test_zero_lot_rejected(self):
        with pytest.raises(InstrumentValidationError):
            resolve_fno_instrument(
                underlying="NIFTY",
                expiry=date(2026, 12, 24),
                strike=Decimal("24500"),
                option_type="CE",
                exchange_token=KEY,
                lot_size=0,
            )

    def test_margin_estimate(self):
        instrument = _option()
        required = estimate_required_margin(instrument, Decimal("250"))
        assert required == max(Decimal("250") * 75 * Decimal("1.15"), Decimal("75") * Decimal("0.05"))

    def test_margin_requires_option(self):
        with pytest.raises(InstrumentValidationError):
            estimate_required_margin(_index_for_bar(), Decimal("250"))


# ------------------------------------------------------------- preflight


class TestRiskPreflight:
    def _preflight(self, settings=None, watchdog=None):
        return RiskPreflight(
            settings or _settings(),
            RiskManager(settings or _settings()),
            watchdog or Watchdog(now=lambda: NOW),
            Decimal("100000"),
        )

    def test_approved_when_all_ok(self):
        decision = self._preflight().evaluate(
            instrument=_option(),
            side=OrderSide.BUY,
            quantity=LOT,
            reference_price=PREMIUM,
            premium=PREMIUM,
            margin_fn=lambda p: estimate_required_margin(_option(), p),
            session=market_session(NOW),
            latest_bar_time=NOW - timedelta(minutes=1),
            components={"data_provider": True, "execution_adapter": True},
            now=NOW,
        )
        assert decision.ok is True

    def test_market_closed_rejected(self):
        before_open = datetime(2026, 9, 14, 8, 0)
        decision = self._preflight().evaluate(
            instrument=_option(),
            side=OrderSide.BUY,
            quantity=LOT,
            reference_price=PREMIUM,
            premium=PREMIUM,
            margin_fn=lambda p: estimate_required_margin(_option(), p),
            session=market_session(before_open),
            latest_bar_time=NOW - timedelta(minutes=1),
            components={},
            now=before_open,
        )
        assert decision.ok is False
        assert any("market not open" in r for r in decision.reasons)

    def test_watchdog_stop_rejected(self):
        # Bars older than the 5-minute staleness window force a STOP.
        decision = self._preflight().evaluate(
            instrument=_option(),
            side=OrderSide.BUY,
            quantity=LOT,
            reference_price=PREMIUM,
            premium=PREMIUM,
            margin_fn=lambda p: estimate_required_margin(_option(), p),
            session=market_session(NOW),
            latest_bar_time=NOW - timedelta(hours=2),
            components={"data_provider": True},
            now=NOW,
        )
        assert decision.ok is False
        assert any("STOP" in r for r in decision.reasons)

    def test_risk_manager_rejection_propagates(self):
        # A premium large enough to break the notional cap must be refused.
        decision = self._preflight().evaluate(
            instrument=_option(),
            side=OrderSide.BUY,
            quantity=10000,  # far beyond the quantity cap of 75
            reference_price=Decimal("5000"),
            premium=Decimal("5000"),
            margin_fn=lambda p: estimate_required_margin(_option(), p),
            session=market_session(NOW),
            latest_bar_time=NOW - timedelta(minutes=1),
            components={"data_provider": True},
            now=NOW,
        )
        assert decision.ok is False
        assert any("RiskManager" in r for r in decision.reasons)

    def test_margin_over_cap_rejected(self):
        decision = self._preflight().evaluate(
            instrument=_option(),
            side=OrderSide.BUY,
            quantity=LOT,
            reference_price=Decimal("4000"),
            premium=Decimal("4000"),
            margin_fn=lambda p: estimate_required_margin(_option(), p),
            session=market_session(NOW),
            latest_bar_time=NOW - timedelta(minutes=1),
            components={"data_provider": True},
            now=NOW,
        )
        assert decision.ok is False
        assert any("margin" in r for r in decision.reasons)

    def test_overnight_guard_blocks_late_entry(self):
        late = datetime(2026, 9, 14, 15, 20)
        decision = self._preflight().evaluate(
            instrument=_option(),
            side=OrderSide.BUY,
            quantity=LOT,
            reference_price=PREMIUM,
            premium=PREMIUM,
            margin_fn=lambda p: estimate_required_margin(_option(), p),
            session=market_session(late),
            latest_bar_time=NOW - timedelta(minutes=1),
            components={"data_provider": True},
            now=late,
        )
        assert decision.ok is False
        assert any("overnight" in r for r in decision.reasons)

    def test_overnight_guard_unit(self):
        guard = OvernightGuard(datetime(2026, 9, 14, 15, 30), hold_seconds=300, finalization_buffer_seconds=FINALIZATION_BUFFER_SECONDS)
        ok, _ = guard.allows_entry(datetime(2026, 9, 14, 12, 0))
        assert ok is True
        ok, message = guard.allows_entry(datetime(2026, 9, 14, 15, 20))
        assert ok is False
        assert "entry window too late" in message


# ------------------------------------------------------------------ upstox


class _FakeTransport:
    """Records requests and returns queued responses deterministically."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, str]] = []
        self.responses = responses or []
        self.queue = list(self.responses)

    def __call__(self, method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
        self.calls.append((method, url))
        if self.queue:
            return self.queue.pop(0)
        return HttpResponse(status=200, body=b"{}", url=url, headers={})


def _json_response(status: int, payload: dict, url: str = "https://api.upstox.com/x") -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode(), url=url, headers={})


class TestUpstoxAdapter:
    def _adapter(self, transport, dry_run=True):
        credentials = UpstoxCredentials(access_token="tok", api_key="apikey")
        return UpstoxExecutionAdapter(
            credentials=credentials,
            dry_run=dry_run,
            instrument_tokens={KEY: 123456},
            request=transport,
            now_fn=lambda: datetime(2026, 9, 14, 12, 0),
        )

    def test_get_account_id(self):
        transport = _FakeTransport([
            _json_response(200, {"data": {"user_id": "USER123"}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.get_account_id() == "USER123"

    def test_quote(self):
        transport = _FakeTransport([
            _json_response(200, {"data": {KEY: {"ohlc": {"close": "247.50"}}}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote(KEY) == Decimal("247.50")

    def test_dry_run_place_order_never_writes(self):
        transport = _FakeTransport()
        adapter = self._adapter(transport, dry_run=True)
        ack = adapter.place_order(Order(instrument=_option(), side=OrderSide.BUY, quantity=LOT), mode=ExecutionMode.PAPER)
        assert not transport.calls  # no HTTP call was made
        assert ack.provider_order_id.startswith("DRYRUN-")
        assert ack.dry_run is True

    def test_real_place_order_posts(self):
        transport = _FakeTransport([
            _json_response(200, {"data": {"order_id": "PX-1", "timestamp": "2026-09-14T12:00:00"}}),
        ])
        adapter = self._adapter(transport, dry_run=False)
        order = Order(instrument=_option(), side=OrderSide.BUY, quantity=LOT, order_id="ORD-local")
        ack = adapter.place_order(order)
        assert ack.provider_order_id == "PX-1"
        assert ack.order_id == "ORD-local"
        assert transport.calls[0][0] == "POST"
        assert transport.calls[0][1].endswith("/v2/order/place")

    def test_place_order_refuses_normal_live(self):
        adapter = self._adapter(_FakeTransport(), dry_run=False)
        with pytest.raises(UpstoxExecutionError):
            adapter.place_order(
                Order(instrument=_option(), side=OrderSide.BUY, quantity=LOT),
                mode=ExecutionMode.LIVE,
            )

    def test_missing_instrument_token_refused(self):
        adapter = self._adapter(_FakeTransport(), dry_run=False)
        with pytest.raises(UpstoxExecutionError):
            adapter.place_order(
                Order(instrument=_option_without_token(), side=OrderSide.BUY, quantity=LOT)
            )

    def test_get_order_status_maps_filled(self):
        transport = _FakeTransport([
            _json_response(200, {"data": [{"status": "completed", "filled_quantity": 75, "average_price": "249.00"}]}),
        ])
        adapter = self._adapter(transport, dry_run=False)
        status, detail = adapter.get_order_status("PX-1", LOT)
        assert status is OrderStatus.FILLED
        assert Decimal(str(detail["average_price"])) == Decimal("249.00")

    def test_get_order_status_rejected_raises(self):
        transport = _FakeTransport([
            _json_response(200, {"data": [{"status": "rejected", "filled_quantity": 0, "average_price": None}]}),
        ])
        adapter = self._adapter(transport, dry_run=False)
        with pytest.raises(UpstoxOrderRejectedError):
            adapter.get_order_status("PX-1", LOT)

    def test_cancel_order_deletes(self):
        transport = _FakeTransport([_json_response(200, {"success": True})])
        adapter = self._adapter(transport, dry_run=False)
        assert adapter.cancel_order("PX-1") is True
        assert transport.calls[0][0] == "DELETE"
        assert transport.calls[0][1].endswith("/v2/order/cancel/PX-1")

    def test_get_positions(self):
        transport = _FakeTransport([
            _json_response(200, {"data": [{"trading_symbol": KEY, "exchange": "NSE", "net_quantity": 75, "average_price": "249.00", "pnl": "12.00"}]}),
        ])
        adapter = self._adapter(transport, dry_run=False)
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == KEY
        assert positions[0].quantity == 75

    def test_error_mapping_surfaces_typed_errors(self):
        cases = {
            401: AuthenticationError,
            429: RateLimitError,
            400: UpstoxExecutionError,
        }
        for status, expected in cases.items():
            adapter = UpstoxExecutionAdapter(
                UpstoxCredentials(access_token="tok"),
                dry_run=False,
                request=lambda *_a, **_k: _raise_from(
                    HttpError(status, "https://api.upstox.com/v2/user/profile", b"{}")
                ),
            )
            with pytest.raises(expected):
                adapter.get_account_id()

    @staticmethod
    def test_map_order_status():
        assert _map_order_status("completed", 75, 75) is OrderStatus.FILLED
        assert _map_order_status("completed", 40, 75) is OrderStatus.PARTIALLY_FILLED
        assert _map_order_status("rejected", 0, 75) is OrderStatus.REJECTED
        assert _map_order_status("cancelled", 0, 75) is OrderStatus.CANCELLED
        assert _map_order_status("open", 0, 75) is OrderStatus.SUBMITTED

    def test_dry_run_order_status_unavailable(self):
        adapter = self._adapter(_FakeTransport(), dry_run=True)
        with pytest.raises(UpstoxExecutionError):
            adapter.get_order_status("DRYRUN-x", LOT)

    def test_credentials_never_serialize_token(self):
        credentials = UpstoxCredentials(access_token="supersecret")
        assert "supersecret" not in repr(credentials)


def _raise_from(error: Exception) -> None:
    raise error


def _option_without_token() -> object:
    from fno_ai_paper_trading.models.instruments import Instrument

    return Instrument(
        symbol="NIFTY 24500 CE",
        instrument_type=InstrumentType.OPTION_CE,
        underlying_symbol="NIFTY",
        expiry=date(2026, 12, 24),
        strike=Decimal("24500"),
        option_type="CE",
        exchange="NSE",
        exchange_token=None,
        lot_size=75,
        multiplier=1,
    )


# ---------------------------------------------------------------- manager


class TestManager:
    def test_happy_path_memory(self):
        audit_sink = []
        manager = _manager(
            _memory(),
            audit=ExecutionAudit(new_run_id(), sink=audit_sink.append),
            alert_engine=AlertEngine(sinks=[CollectingAlertSink()]),
        )
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.outcome == "PASS"
        assert result.stage is ExecutionTestState.COMPLETE
        assert result.position_flat is True
        assert result.entry_order_id
        assert result.exit_order_id
        assert result.mode is ExecutionMode.PAPER
        assert result.algorithm_health_note.startswith("UNCHANGED")
        # single entry + single exit were recorded
        kinds = [entry["kind"] for entry in audit_sink]
        assert kinds.count("entry_ack") == 1
        assert kinds.count("exit_ack") == 1
        assert "reconciled" in kinds

    def test_hold_signal_places_no_order(self):
        adapter = _memory()
        manager = _manager(adapter)
        result = manager.run(_option(), signal_bars=_hold_bars())
        assert result.outcome == "FAIL"
        assert result.stage is ExecutionTestState.ABORTED
        assert result.entry_order_id is None
        assert any("HOLD" in r for r in result.reasons)

    def test_side_preference_can_pin_put(self):
        result = _manager(_memory()).run(
            _option(), signal_bars=_hold_bars(), side_preference=CallPutSignal.PUT
        )
        # Pinning PUT still requires the signal: if the underlying is HOLD, the
        # manager refuses to trade rather than fabricate a direction.
        assert result.entry_order_id is None

    def test_real_adapter_without_confirm_refused(self):
        adapter = _memory()

        class Real(MemoryExecutionAdapter):
            pass

        adapter = Real(
            prices={KEY: PREMIUM},
            account_id="tester",
            slippage=Decimal("0"),
        )
        adapter.dry_run = False
        manager = _manager(adapter)
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.stage is ExecutionTestState.ABORTED
        assert any("confirm-live-enablement" in r for r in result.reasons)
        assert result.entry_order_id is None

    def test_real_adapter_with_confirm_but_gate_closed_refused(self):
        adapter = _memory()
        adapter.dry_run = False
        manager = _manager(adapter, live_enabled=False, confirm=True)
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.stage is ExecutionTestState.ABORTED
        assert any("gate" in r for r in result.reasons)
        assert result.entry_order_id is None

    def test_real_adapter_with_confirm_and_open_gate_executes(self):
        token = "tok"
        adapter = _memory()
        adapter.dry_run = False
        manager = _manager(
            adapter,
            live_enabled=True,
            confirm=True,
            consent=_open_consent(token),
            token=token,
        )
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.outcome == "PASS"
        assert result.mode is ExecutionMode.LIVE_EXECUTION_TEST
        assert result.entry_order_id
        assert result.exit_order_id

    def test_rejected_order_fails_without_exit(self):
        # A real-send adapter that reports REJECTED for the entry.
        class RejectingAdapter(ExecutionAdapter):
            dry_run = False

            def __init__(self):
                self.calls = []

            def get_account_id(self):
                return "tester"

            def quote(self, symbol):
                return PREMIUM

            def place_order(self, order, mode=ExecutionMode.LIVE_EXECUTION_TEST):
                self.calls.append(order.order_id)
                return ExecutionAck(order_id=order.order_id, provider_order_id="PX-rejected")

            def get_order_status(self, order_id, quantity):
                self.calls.append(("status", order_id))
                return OrderStatus.REJECTED, {"status": "rejected"}

            def cancel_order(self, order_id):
                self.calls.append(("cancel", order_id))
                return True

            def get_positions(self):
                return []

        adapter = RejectingAdapter()
        token = "tok"
        manager = _manager(
            adapter,
            live_enabled=True,
            confirm=True,
            token=token,
            consent=_open_consent(token),
        )
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.outcome == "FAIL"
        assert any("REJECTED" in r for r in result.reasons)
        # exactly one entry was ever attempted
        assert sum(1 for c in adapter.calls if not isinstance(c, tuple)) == 1
        assert result.exit_order_id is None

    def test_fill_timeout_cancels_and_fails(self):
        # A stuck order: status stays open past the timeout; then a cancel/exit.
        class StuckAdapter(MemoryExecutionAdapter):
            def get_order_status(self, order_id, quantity):
                return OrderStatus.SUBMITTED, {"status": "open"}

        adapter = StuckAdapter(prices={KEY: PREMIUM})
        adapter.dry_run = False
        token = "tok"
        manager = _manager(
            adapter,
            live_enabled=True,
            confirm=True,
            token=token,
            consent=_open_consent(token),
            order_timeout_seconds=1.0,
        )
        # Clock in the poll loop: fresh NOW for start + preflight, then past deadline.
        from itertools import count

        times = [NOW, NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=4)]
        counter = count()
        manager.clock = lambda: times[min(next(counter), len(times) - 1)]
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.outcome == "FAIL"
        assert any("timed out" in r for r in result.reasons)

    def test_non_flat_reconciliation_fails(self):
        # Exit fills but the broker still reports an open position.
        class LeakyAdapter(MemoryExecutionAdapter):
            def get_positions(self):
                return [ExecutionPosition(symbol=KEY, quantity=75)]

        adapter = LeakyAdapter(prices={KEY: PREMIUM})
        adapter.dry_run = False
        token = "tok"
        manager = _manager(
            adapter,
            live_enabled=True,
            confirm=True,
            token=token,
            consent=_open_consent(token),
        )
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert result.outcome == "FAIL"
        assert any("reconciliation" in r for r in result.reasons)
        assert result.position_flat is False

    def test_audit_records_redacted_payload(self):
        sink = []
        audit = ExecutionAudit("RUN-1", sink=sink.append)
        audit.record("debug", message="Bearer abcdef0123456789abcdef0123456789abcdef0123456789 token")
        line = sink[0]
        assert "Bearer <redacted>" in line["message"]
        assert "abcdef0123456789" not in line["message"]

    def test_redact_unit(self):
        assert redact("Bearer deadbeefdeadbeef" + "0" * 40) == "Bearer <redacted>"

    def test_alerts_carry_paper_label(self):
        sink = CollectingAlertSink()
        manager = _manager(
            _memory(),
            alert_engine=AlertEngine(sinks=[sink]),
        )
        manager.run(_option(), signal_bars=_buy_bars())
        assert sink.alerts
        for alert in sink.alerts:
            assert alert.environment == PAPER_TRADING_LABEL

    def test_audit_run_id_repointed_per_run(self):
        sink = []
        audit = ExecutionAudit("placeholder", sink=sink.append)
        manager = _manager(_memory(), audit=audit)
        result = manager.run(_option(), signal_bars=_buy_bars())
        assert audit.run_id == result.run_id
        assert all(line["run_id"] == result.run_id for line in sink)


# ------------------------------------------------------------- audit


class TestAudit:
    def test_new_run_id_prefix(self):
        assert new_run_id().startswith("LIVETEST_")

    def test_file_sink_writes_json_lines(self, tmp_path):
        audit = ExecutionAudit("RUN-9", path=tmp_path / "run.jsonl")
        audit.record("signal_decided", leg="CALL")
        audit.close()
        lines = (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["run_id"] == "RUN-9"
        assert payload["kind"] == "signal_decided"


# --------------------------------------------------------------- CLI


class TestCliOptIn:
    def _run(self, *args):
        python = sys.executable
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_live_execution_test.py"
        env = {
            "PATH": "C:\\Windows\\System32;C:\\Windows",
            "PYTHONIOENCODING": "utf-8",
        }
        return subprocess.run(
            [python, str(script), "--data-source", "smoke", "--out", "reports/execution/test_cli_summary.json", *args],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
        )

    def test_smoke_dry_run_passes(self):
        completed = self._run("--hold-seconds", "0")
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "PASS" in completed.stdout

    def test_live_send_refused_when_gate_closed(self):
        completed = self._run("--live")
        assert completed.returncode == 2
        assert "REFUSING" in completed.stdout or "REFUSING" in completed.stderr