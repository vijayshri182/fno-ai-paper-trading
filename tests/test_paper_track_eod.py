"""Phase 6 — end-of-day closure, gate boundaries and overnight protection."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.position import Position
from fno_ai_paper_trading.paper_track.clock import FixedClock
from fno_ai_paper_trading.paper_track.errors import TrackPolicyViolation, TrackRecoveryError
from fno_ai_paper_trading.paper_track.policy import Gate, SessionPolicy
from fno_ai_paper_trading.paper_track.store import TrackStore, position_to_dict
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from tests.paper_track_testkit import DAY0, day_ticks, drive, make_engine


def _buy_first() -> "Strategy":
    from tests.test_paper_track_orders import CycleStrategy

    return CycleStrategy(plan={1: Signal.BUY})


def _tick_index(target: time) -> int:
    for i, tick in enumerate(day_ticks(DAY0)):
        if tick.time() == target:
            return i
    raise AssertionError(f"no tick at {target}")


def test_gate_table_at_boundaries():
    policy = SessionPolicy()
    day = DAY0
    cases = {
        time(9, 14): Gate.SKIP,
        time(9, 15): Gate.TRADING,
        time(9, 16): Gate.TRADING,
        time(15, 19): Gate.TRADING,
        time(15, 20): Gate.FLATTENING,
        time(15, 30): Gate.FLATTENING,
        time(15, 31): Gate.CLOSING,
        time(23, 0): Gate.CLOSING,
    }
    for moment, expected in cases.items():
        decision = policy.gate(datetime(day.year, day.month, day.day, moment.hour, moment.minute))
        assert decision.gate is expected, (moment, decision)


def test_weekend_and_holiday_gate_is_skip(tmp_path):
    policy = SessionPolicy()
    sunday = datetime(2026, 9, 27, 10, 0)
    assert policy.gate(sunday).gate is Gate.SKIP
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    engine.clock = FixedClock(sunday)
    result = engine.step()
    assert result.status.value == "SKIPPED"
    assert engine.broker.fills == []
    assert engine.counters["signals"] == 0


def test_entries_impossible_from_flatten_window(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=CycleStrategyShort())
    # Run ONLY the flatten-window ticks with an aggressive entry strategy.
    first = _tick_index(time(15, 20))
    for tick in day_ticks(DAY0)[first:]:
        engine.clock.set(tick)
        engine.step()
    assert engine.counters["entries_filled"] == 0
    orders, _fills = engine.broker.snapshot()
    assert orders == []
    assert engine.position_quantity == 0


class CycleStrategyShort(Strategy):
    name = "aggress"

    def analyze(self, bars):
        last = bars[-1]
        return SignalResult(Signal.BUY, instrument=last.instrument, timestamp=last.timestamp, reason="aggressive")


def test_eod_flatten_fires_at_1520(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    results = drive(engine, day_ticks(DAY0))
    for i, result in enumerate(results):
        if result.status.value == "FLATTENED":
            assert day_ticks(DAY0)[i].time() == time(15, 20)
            break
    else:
        pytest.fail("expected a FLATTENED tick at 15:20")
    assert engine.position_quantity == 0
    assert engine.eod_status == "FLATTENED"
    assert engine.counters["flatten_count"] == 1
    assert engine.counters["exits_filled"] == 1


def test_emergency_flatten_after_close(tmp_path):
    """The engine missed the whole flatten window; the CLOSING gate must still
    force-close any open position on the next tick (never sleeps overnight)."""
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    first_flatten = _tick_index(time(15, 20))
    ticks = day_ticks(DAY0)[2:first_flatten]  # 09:20 .. 15:15 (position opens, still open)
    for tick in ticks:
        engine.clock.set(tick)
        engine.step()
    assert engine.position_quantity > 0
    # Jump straight past the close: nothing running through 15:20-15:30.
    engine.clock.set(datetime(2026, 9, 21, 15, 31, 0))
    result = engine.step()
    assert engine.position_quantity == 0
    assert result.status.value == "FLATTENED"
    assert engine.eod_status == "FLATTENED"


def test_weekend_after_close_with_position_is_flattened(tmp_path):
    """Even a hopped weekend tick with a position must flatten, never sleep."""
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    first_flatten = _tick_index(time(15, 20))
    for tick in day_ticks(DAY0)[2:first_flatten]:
        engine.clock.set(tick)
        engine.step()
    assert engine.position_quantity > 0
    engine.clock.set(datetime(2026, 9, 21, 15, 36, 0))  # post-close poll
    engine.step()
    assert engine.position_quantity == 0


def test_day_rollover_flat_is_fine(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    drive(engine, day_ticks(DAY0))
    assert engine.position_quantity == 0
    nxt = datetime(2026, 9, 22, 9, 14, 0)
    engine.clock.set(nxt)
    result = engine.step()
    assert engine.day == date(2026, 9, 22)
    assert result.status.value == "SKIPPED"


def test_day_rollover_with_open_position_raises(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    first_flatten = _tick_index(time(15, 20))
    for tick in day_ticks(DAY0)[2:first_flatten]:  # position opened, still open
        engine.clock.set(tick)
        engine.step()
    assert engine.position_quantity > 0
    engine.clock.set(datetime(2026, 9, 22, 9, 14, 0))
    with pytest.raises(TrackPolicyViolation):
        engine.step()


def test_overnight_position_in_checkpoint_refused_on_load(tmp_path):
    store = TrackStore(tmp_path, "ovn")
    position = Position(
        instrument=engine_placeholder_instrument(),
        quantity=2,
        average_entry_price=Decimal("25000"),
        opened_at=datetime(2026, 9, 18, 12, 0),  # the previous Friday
    )
    symbol = position.instrument.symbol
    payload = {
        "schema_version": 1,
        "day": DAY0.isoformat(),
        "portfolio": {
            "cash": "90000",
            "positions": {symbol: position_to_dict(position)},
            "trade_history": [],
            "realized_pnl": "0",
        },
        "broker": {"orders": [], "fills": []},
        "history": [],
        "consumed": [],
        "last_processed": None,
        "equity_curve": ["100000"],
        "max_intraday_drawdown": "0",
        "eod_status": "NONE",
        "report_written": False,
        "entry_approvals": [],
        "counters": {},
    }
    store.save_checkpoint(DAY0, payload)
    engine, _, _ = make_engine(tmp_path, days=[DAY0], account="ovn")
    with pytest.raises(TrackRecoveryError):
        engine.load(DAY0)


def test_full_day_never_leaves_overnight_risk(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    drive(engine, day_ticks(DAY0))
    assert engine.position_quantity == 0
    assert engine.eod_status in ("FLAT", "FLATTENED")
    assert engine.portfolio.open_positions() == {}


def test_pre_open_tick_is_a_noop(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_buy_first())
    engine.clock.set(datetime(2026, 9, 21, 9, 14, 40))
    result = engine.step()
    assert result.status.value == "SKIPPED"
    assert engine.broker.fills == []
    assert engine.counters["signals"] == 0


def engine_placeholder_instrument():
    from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT

    return TRACK_INSTRUMENT()