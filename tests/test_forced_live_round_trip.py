"""WS 7.24B OPTION B — deterministic tests for ``--force-one-lot-round-trip``.

Everything is offline and deterministic (MemoryExecutionAdapter behind the
real ExecutionAdapter interface, fixed clock, consent fabric only in the
test-signing helper, an explicitly pinned CALL/PUT leg). Twelve tests prove,
in order:

  1. flag absent            -> HOLD refusal is unchanged (forced off by default)
  2. flag on + gate CLOSED  -> REFUSED even with a pinned leg; gate stays mandatory
  3. flag on + gate open    -> forced CALL one-lot round trip -> flat -> PASS
  4. flag on + gate open    -> forced PUT one-lot round trip -> flat -> PASS
  5. flag on + pinned side absent -> REFUSED (no forced leg without a pinned leg)
  6. flag on, pinned CALL   -> the pinned CALL always wins over a PUT-side
     market signal (bias): only the pinned leg is forced
  7. flag on, full round trip -> both broker order ids captured (entry + exit)
     and differ; no re-entry
  8. flag on, pinned PUT    -> the pinned PUT always wins over a CALL-side
     market signal (bias); PUT round trip, flat
  9. flag absent + auto HOLD -> refusal unchanged; identical failure outcome
     as the pre-flag default
 10. flag on, gate CLOSED   -> must NEVER open the gate: fingerprint mismatch
     still refuses a forced round trip - no order
 11. autonomous loop CANNOT invoke the flag (flag token is absent from its argv spec)
 12. flag defaults OFF and isolated: manager constructed without the flag runs
     a normal single round trip (at most two orders), never a consent bypass

No real broker write is ever issued in any of these tests: every adapter is
the in-memory adapter, so no live/consent/gate state is ever changed. The
forced flag is exercised through the real LIVE_EXECUTION_TEST gate wiring so
"flag on + gate open" is a genuine open-gate run and "flag on + gate CLOSED"
is a genuine refusal.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.alerting.health import Watchdog
from fno_ai_paper_trading.config.settings import (
    Environment,
    LiveExecutionTestSettings,
    PaperSettings,
)
from fno_ai_paper_trading.execution.gate import ExecutionMode, LiveExecutionTestGate, consent_fingerprint
from fno_ai_paper_trading.execution.instrument import (
    resolve_fno_instrument,
)
from fno_ai_paper_trading.execution.manager import LiveExecutionTestManager
from fno_ai_paper_trading.execution.memory import MemoryExecutionAdapter
from fno_ai_paper_trading.execution.risk import RiskPreflight
from fno_ai_paper_trading.execution.signal import CallPutSignal, decide_call_put
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderStatus
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.risk.manager import RiskManager

NOW = datetime(2036, 9, 22, 12, 0)  # a Monday 12:00 IST — NSE session OPEN
KEY = "NSE_FO|NIFTY 24 DEC 2036 24500 CE"
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


def _underlying() -> object:
    return resolve_fno_instrument(
        underlying="NIFTY",
        expiry=date(2036, 12, 24),
        strike=Decimal("24500"),
        option_type="CE",
        exchange_token=KEY,
        lot_size=LOT,
    )


def _index_for_bar() -> Instrument:
    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="Nifty 50",
        exchange="NSE",
        exchange_token="NSE_INDEX|Nifty 50",
        lot_size=1,
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


def _call_bars(count: int = 29) -> list[MarketPrice]:
    """Flat run followed by one strong up bar: ends within the Watchdog
    freshness window and produces a BUY (CALL) crossover."""
    start = NOW - timedelta(minutes=5 * count)
    closes = [Decimal("24200")] * (count - 1) + [Decimal("24300")]
    return [
        _bar(close=value, ts=start + timedelta(minutes=5 * i))
        for i, value in enumerate(closes)
    ]


def _put_bars(count: int = 29) -> list[MarketPrice]:
    """Flat run followed by one strong down bar: fresh and produces a SELL
    (PUT) crossover."""
    start = NOW - timedelta(minutes=5 * count)
    closes = [Decimal("24200")] * (count - 1) + [Decimal("24100")]
    return [
        _bar(close=value, ts=start + timedelta(minutes=5 * i))
        for i, value in enumerate(closes)
    ]


def _open_consent(token: str) -> dict[str, object]:
    created = NOW - timedelta(hours=1)
    return {
        "operator": "tester",
        "purpose": "controlled forced one-lot round-trip test",
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + timedelta(hours=6)).isoformat(timespec="seconds"),
        "token_fingerprint_sha256": consent_fingerprint(token),
    }


class _TrackingAdapter(MemoryExecutionAdapter):
    """In-memory adapter that also records every placed order."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.placed_orders: list[Order] = []

    def place_order(self, order: Order, mode: ExecutionMode = ExecutionMode.LIVE_EXECUTION_TEST) -> object:
        ack = super().place_order(order, mode=mode)
        self.placed_orders.append(order)
        return ack


def _adapter(*, dry_run: bool = True, premium: Decimal = PREMIUM) -> _TrackingAdapter:
    return _TrackingAdapter(
        prices={KEY: premium},
        account_id="tester",
        slippage=Decimal("0"),
        dry_run=dry_run,
    )


def _manager(
    adapter: _TrackingAdapter,
    *,
    force: bool = False,
    live_enabled: bool = True,
    confirm: bool = True,
    consent=None,
    token: str = "t",
    **kwargs,
) -> LiveExecutionTestManager:
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
        force_one_lot_round_trip=force,
    )
    base.update(kwargs)
    return LiveExecutionTestManager(**base)


# ---------------------------------------------------------------- 1-4


def test_flag_absent_hold_unchanged():
    """1. Default (no force flag): a HOLD strategy refuses; no order is placed."""
    a = _adapter()
    m = _manager(a, force=False)
    result = m.run(_underlying(), side_preference=CallPutSignal.NONE)
    assert result is not None
    assert result.outcome == "FAIL"
    assert any("HOLD" in r or "signal_hold" in r for r in result.reasons)
    assert a.placed_orders == []
    assert result.entry_order_id is None


def test_flag_gate_closed_refuses():
    """2. Flag present does NOT weaken the gate: gate CLOSED => still refused."""
    a = _adapter(dry_run=False)  # real-send path -> the gate is enforced
    m = _manager(a, force=True, consent=_open_consent("other-token"))
    result = m.run(_underlying(), signal_bars=_call_bars(), side_preference=CallPutSignal.CALL)
    assert result is not None
    assert result.outcome == "FAIL"
    assert any("gate" in r for r in result.reasons)
    assert a.placed_orders == []
    assert result.entry_order_id is None


def test_forced_call_round_trip():
    """3. Forced CALL: BUY 1 lot, confirm fill, SELL exactly that filled qty, flat."""
    a = _adapter(dry_run=False)
    m = _manager(a, force=True, consent=_open_consent("t"))
    result = m.run(_underlying(), signal_bars=_call_bars(), side_preference=CallPutSignal.CALL)
    assert result is not None
    assert result.outcome == "PASS"
    assert result.mode is ExecutionMode.LIVE_EXECUTION_TEST
    assert result.position_flat is True
    assert result.entry_order_id is not None and result.exit_order_id is not None
    assert len(a.placed_orders) == 2
    entry_fill, exit_fill = a.placed_orders
    assert entry_fill.side is OrderSide.BUY
    assert exit_fill.side is OrderSide.SELL
    assert exit_fill.quantity == entry_fill.quantity == LOT


def test_forced_put_round_trip():
    """Forced PUT: same one-lot round trip as CALL, flat PASS."""
    a = _adapter(dry_run=False)
    m = _manager(a, force=True, consent=_open_consent("t"))
    result = m.run(_underlying(), signal_bars=_put_bars(), side_preference=CallPutSignal.PUT)
    assert result is not None
    assert result.outcome == "PASS"
    assert result.position_flat is True
    assert len(a.placed_orders) == 2
    entry_fill, exit_fill = a.placed_orders
    assert entry_fill.side is OrderSide.SELL
    assert exit_fill.side is OrderSide.BUY
    assert exit_fill.quantity == entry_fill.quantity == LOT


# ---------------------------------------------------------------- 5-8


def test_forced_no_pinned_side_refuses():
    """5. Flag present does NOT invent a leg: no pinned CALL/PUT side =>
    the auto HOLD still refuses exactly as before the flag."""
    a = _adapter()
    m = _manager(a, force=True)
    result = m.run(_underlying(), side_preference=CallPutSignal.NONE)
    assert result is not None
    assert result.outcome == "FAIL"
    assert any("HOLD" in r or "signal_hold" in r for r in result.reasons)
    assert a.placed_orders == []
    assert result.entry_order_id is None


def test_forced_pinned_call_beats_put_bias():
    """6. Flag + pinned CALL always wins over a PUT-side market signal: the
    pinned CALL leg is the only leg forced; the market bias never overrides it."""
    assert decide_call_put(_put_bars()).leg is CallPutSignal.PUT  # the bias exists
    a = _adapter(dry_run=False)
    m = _manager(a, force=True, consent=_open_consent("t"))
    result = m.run(_underlying(), signal_bars=_put_bars(), side_preference=CallPutSignal.CALL)
    assert result is not None
    assert result.outcome == "PASS"
    assert result.position_flat is True
    assert len(a.placed_orders) == 2
    entry_fill, exit_fill = a.placed_orders
    assert entry_fill.side is OrderSide.BUY  # pinned CALL, not the PUT bias
    assert exit_fill.side is OrderSide.SELL
    assert exit_fill.quantity == entry_fill.quantity == LOT


def test_forced_exact_entry_exit_id_pair():
    """7. Both broker order ids of a forced round trip are always captured
    (entry + exit) and differ; no extra leg (no re-entry)."""
    a = _adapter(dry_run=False)
    m = _manager(a, force=True, consent=_open_consent("t"))
    result = m.run(_underlying(), signal_bars=_call_bars(), side_preference=CallPutSignal.CALL)
    assert result is not None
    assert result.outcome == "PASS"
    assert result.entry_order_id is not None
    assert result.exit_order_id is not None
    assert result.entry_order_id != result.exit_order_id
    assert len(a.placed_orders) == 2


def test_forced_put_wins_over_call_bias():
    """8. Pinned PUT leg wins over a CALL-biased market; PUT round trip, flat."""
    assert decide_call_put(_call_bars()).leg is CallPutSignal.CALL  # the bias exists
    a = _adapter(dry_run=False)
    m = _manager(a, force=True, consent=_open_consent("t"))
    result = m.run(_underlying(), signal_bars=_call_bars(), side_preference=CallPutSignal.PUT)
    assert result is not None
    assert result.outcome == "PASS"
    assert result.position_flat is True
    assert len(a.placed_orders) == 2
    entry_fill, exit_fill = a.placed_orders
    assert entry_fill.side is OrderSide.SELL  # pinned PUT, not the CALL bias
    assert exit_fill.side is OrderSide.BUY
    assert exit_fill.quantity == entry_fill.quantity == LOT


# ---------------------------------------------------------------- 9-12


def test_flag_absent_auto_hold_unchanged():
    """9. Flag absent + no pinned side (auto HOLD): refusal unchanged; identical
    failure outcome as the pre-flag default."""
    a = _adapter()
    m = _manager(a, force=False)
    result = m.run(_underlying(), side_preference=CallPutSignal.NONE)
    assert result is not None
    assert result.outcome == "FAIL"
    assert any("HOLD" in r or "signal_hold" in r for r in result.reasons)
    assert a.placed_orders == []


def test_forced_gate_closed_still_refuses_flagged():
    """10. Flag present must NEVER open the gate: with the consent fingerprint
    mismatched (gate closed), a forced round trip is still refused - no order."""
    a = _adapter(dry_run=False)
    m = _manager(a, force=True, consent=_open_consent("wrong-token"))
    result = m.run(_underlying(), signal_bars=_call_bars(), side_preference=CallPutSignal.CALL)
    assert result is not None
    assert result.outcome == "FAIL"
    assert any("fingerprint" in r or "gate" in r for r in result.reasons)
    assert a.placed_orders == []
    assert result.entry_order_id is None


def test_autonomous_loop_cannot_invoke_flag():
    """11. The production autonomous runner has NO CLI token for this seam:
    the forced-flag word never appears in its argv spec or runner body."""
    exporter = Path(__file__).resolve().parents[1] / "scripts" / "run_autonomous_loop.py"
    spec = importlib.util.spec_from_file_location("run_autonomous_loop", exporter)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = exporter.read_text(encoding="utf-8")
    assert "force-one-lot-round-trip" not in source
    assert "force_one_lot_round_trip" not in source
    assert hasattr(module, "main")


def test_flag_defaults_off_and_isolated():
    """12. The seam is OFF by default and stays isolated from the live real-send
    path: manager construction without the flag exposes exactly two orders max
    per run and never a real-send consent bypass."""
    a = _adapter(dry_run=False)
    m = _manager(a, force=False, consent=_open_consent("t"))
    assert m.force_one_lot_round_trip is False
    result = m.run(_underlying(), signal_bars=_call_bars(), side_preference=CallPutSignal.CALL)
    assert result is not None and result.outcome == "PASS"
    assert len(a.placed_orders) <= 2