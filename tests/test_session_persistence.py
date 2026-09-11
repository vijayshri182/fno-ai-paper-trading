"""Tests for the WS 6.5 session-state persistence layer.

Covers ``PaperSession.snapshot()`` / ``PaperSession.restore()``,
``PaperBroker.snapshot()`` / ``PaperBroker.restore()``, and the
``save_session`` / ``load_session`` disk persistence layer. Validates the full
round-trip (snapshot -> save -> load -> restore) and the restart-equivalence
invariant: after save/load/restore into a fresh session, the resumed session
must produce identical cash, positions, trade history (excluding generated IDs),
and session counters as an uninterrupted reference session given the same data
and clock.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.persistence.session_store import (
    DEFAULT_STATE_DIR,
    SCHEMA_VERSION,
    SessionSnapshot,
    StoredSession,
    load_session,
    save_session,
)
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.services.paper_session import (
    PaperSession,
    _MISSING,
)
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

# --------------------------------------------------------------------------- #
# Shared test helpers (mirror test_paper_session.py)
# --------------------------------------------------------------------------- #

BASE = datetime(2026, 9, 2, 9, 15)
OPEN_NOW = datetime(2026, 9, 2, 12, 0)
FILL_CLOCK = datetime(2026, 9, 2, 10, 0)
ENTRY = Decimal("24000")
STOP = Decimal("23520")  # ENTRY * 0.98


def _ts(index: int, start: datetime = BASE) -> datetime:
    return start + timedelta(minutes=5 * index)


def _index() -> Instrument:
    return Instrument(
        symbol="NIFTY_INDEX",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
    )


def _bar(
    index: int,
    *,
    open_: str | Decimal | None = None,
    high: str | Decimal | None = None,
    low: str | Decimal | None = None,
    close: str | Decimal | None = None,
    start: datetime = BASE,
) -> MarketPrice:
    open_ = ENTRY if open_ is None else Decimal(open_)
    close = ENTRY if close is None else Decimal(close)
    high = max(open_, close) + Decimal("20") if high is None else Decimal(high)
    low = min(open_, close) - Decimal("20") if low is None else Decimal(low)
    return MarketPrice(
        instrument=_index(),
        timestamp=_ts(index, start),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def _bars(count: int, *, start: datetime = BASE, closes: list[Decimal] | None = None) -> list[MarketPrice]:
    closes = closes if closes is not None else [None] * count
    return [_bar(i, close=closes[i], start=start) for i in range(count)]


def _provider(bars: list[MarketPrice]) -> InMemoryMarketDataProvider:
    index = _index()
    return InMemoryMarketDataProvider(instruments=[index], history={index.symbol: bars})


def _settings(environment: Environment = Environment.PAPER) -> PaperSettings:
    return PaperSettings(
        environment=environment,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("1000"),
        commission_rate=Decimal("0"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"),
    )


def _settings_with_cost() -> PaperSettings:
    return PaperSettings(
        environment=Environment.PAPER,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("10000"),
        commission_rate=Decimal("0.0003"),
        commission_fixed=Decimal("10"),
        slippage_rate=Decimal("0.001"),
    )


class _ScriptedStrategy(Strategy):
    name = "scripted"

    def __init__(self, instrument: Instrument, plan: dict[int, Signal]) -> None:
        self._instrument = instrument
        self._plan = dict(plan)

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        index = len(bars) - 1
        signal = self._plan.get(index, Signal.HOLD)
        return SignalResult(
            signal=signal,
            instrument=self._instrument,
            timestamp=bars[-1].timestamp,
            reason=f"scripted {signal.value} at bar {index}",
        )


def _strategy(plan: dict[int, Signal]) -> _ScriptedStrategy:
    return _ScriptedStrategy(_index(), plan)


def _make_session(
    bars: list[MarketPrice],
    *,
    plan: dict[int, Signal] | None = None,
    strategy: Strategy | None = None,
    provider: InMemoryMarketDataProvider | None = None,
    portfolio: Portfolio | None = None,
    broker: PaperBroker | None = None,
    clock: callable | None = None,
    warmup_bars: int | None = None,
    quantity: int = 1,
    sizer=None,
    use_default_sizer: bool = True,
    interval: str = "5m",
    environment: Environment = Environment.PAPER,
    allow_sandbox: bool = False,
    started: bool = True,
) -> PaperSession:
    if provider is None:
        provider = _provider(bars)
    if strategy is None:
        strategy = _strategy(plan or {})
    session = PaperSession(
        _settings(environment),
        provider,
        strategy,
        portfolio=portfolio,
        broker=broker,
        clock=clock if clock is not None else (lambda: OPEN_NOW),
        warmup_bars=warmup_bars,
        quantity=quantity,
        sizer=_MISSING if use_default_sizer else sizer,
        interval=interval,
        allow_sandbox=allow_sandbox,
    )
    if started:
        session.start()
    return session


def _make_session_with_cost(
    bars: list[MarketPrice],
    *,
    plan: dict[int, Signal] | None = None,
    clock: callable | None = None,
    warmup_bars: int | None = None,
    started: bool = True,
) -> PaperSession:
    return _make_session(
        bars,
        plan=plan or {},
        settings_override=None,
        clock=clock,
        warmup_bars=warmup_bars,
        started=started,
    )


def _sorted_trade_dicts(trades: list[Trade]) -> list[dict]:
    """Trade dicts sorted by (executed_at, side) for identity-agnostic comparison."""
    return sorted(
        [
            {
                "side": t.side.value,
                "quantity": t.quantity,
                "price": str(t.price),
                "commission": str(t.commission),
                "executed_at": t.executed_at.isoformat(),
                "realized_pnl": str(t.realized_pnl),
            }
            for t in trades
        ],
        key=lambda d: (d["executed_at"], d["side"]),
    )


def _sorted_fill_dicts(fills: list[Fill]) -> list[dict]:
    return sorted(
        [
            {
                "side": f.side.value,
                "quantity": f.quantity,
                "price": str(f.price),
                "commission": str(f.commission),
                "filled_at": f.filled_at.isoformat(),
            }
            for f in fills
        ],
        key=lambda d: (d["filled_at"], d["side"]),
    )


def _pos_dict(pos: Position) -> dict:
    return {
        "quantity": pos.quantity,
        "average_entry_price": str(pos.average_entry_price),
        "realized_pnl": str(pos.realized_pnl),
    }


def _assert_portfolios_equivalent(a: Portfolio, b: Portfolio) -> None:
    assert a.cash == b.cash, f"cash {a.cash} != {b.cash}"
    assert a.realized_pnl == b.realized_pnl, f"realized_pnl {a.realized_pnl} != {b.realized_pnl}"
    assert a.initial_cash == b.initial_cash
    assert a.long_only == b.long_only
    a_open = {s: _pos_dict(p) for s, p in a.open_positions().items()}
    b_open = {s: _pos_dict(p) for s, p in b.open_positions().items()}
    assert a_open == b_open, f"open positions {a_open} != {b_open}"
    assert _sorted_trade_dicts(a.trade_history) == _sorted_trade_dicts(b.trade_history)


# --------------------------------------------------------------------------- #
# File-level persistence
# --------------------------------------------------------------------------- #


class TestSaveAndLoad:
    def test_save_creates_payload_and_meta_files(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY})
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        stored = save_session(session.snapshot(), directory=tmp_path)
        assert stored.path.exists()
        assert stored.path.name.endswith(".json")
        meta_path = stored.path.with_suffix(".meta.json")
        assert meta_path.exists()

    def test_default_name_and_safe_name(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        stored = save_session(session.snapshot(), directory=tmp_path)
        assert stored.metadata["name"] == "paper_NIFTY_INDEX_5m"
        assert stored.path.name == "paper_NIFTY_INDEX_5m.json"

    def test_custom_name_used(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        stored = save_session(session.snapshot(), directory=tmp_path, name="my_session")
        assert stored.metadata["name"] == "my_session"
        assert (tmp_path / "my_session.json").exists()
        assert (tmp_path / "my_session.meta.json").exists()

    def test_unsafe_name_sanitized(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        stored = save_session(session.snapshot(), directory=tmp_path, name="bad/name:?*")
        assert "/" not in stored.metadata["name"]
        assert "?" not in stored.metadata["name"]
        assert "*" not in stored.metadata["name"]
        assert stored.path.exists()

    def test_load_round_trip_fields(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY})
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        snapshot = session.snapshot()
        stored = save_session(snapshot, directory=tmp_path)
        loaded = load_session(stored.path)

        assert loaded.snapshot.instrument.symbol == "NIFTY_INDEX"
        assert loaded.snapshot.interval_token == "5m"
        assert loaded.snapshot.warmup_bars == 1
        assert loaded.snapshot.quantity == 1
        assert loaded.snapshot.portfolio.cash == snapshot.portfolio.cash
        assert loaded.snapshot.portfolio.realized_pnl == snapshot.portfolio.realized_pnl
        assert loaded.snapshot.portfolio.initial_cash == snapshot.portfolio.initial_cash
        assert loaded.snapshot.portfolio.long_only == snapshot.portfolio.long_only
        assert loaded.snapshot.consumed_timestamps == snapshot.consumed_timestamps
        assert loaded.snapshot.orders_submitted == snapshot.orders_submitted
        assert loaded.snapshot.fills_count == snapshot.fills_count
        assert loaded.snapshot.trades_count == snapshot.trades_count
        assert loaded.snapshot.rejections == snapshot.rejections
        assert loaded.snapshot.skips == snapshot.skips

    def test_load_missing_meta_raises(self, tmp_path: Path) -> None:
        payload_path = tmp_path / "orphan.json"
        payload_path.write_text('{"schema_version": "1"}', encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="missing"):
            load_session(payload_path)

    def test_tampered_payload_raises_hash_error(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        stored = save_session(session.snapshot(), directory=tmp_path)
        payload = json.loads(stored.path.read_text(encoding="utf-8"))
        payload["warmup_bars"] = 999
        stored.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="state_hash mismatch"):
            load_session(stored.path)

    def test_corrupt_json_raises(self, tmp_path: Path) -> None:
        payload_path = tmp_path / "paper_X_5m.json"
        payload_path.write_text("NOT_JSON!!!", encoding="utf-8")
        meta_path = tmp_path / "paper_X_5m.meta.json"
        meta_path.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION}, indent=2), encoding="utf-8"
        )
        with pytest.raises((json.JSONDecodeError, ValueError)):
            load_session(payload_path)

    def test_unsupported_schema_raises(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        stored = save_session(session.snapshot(), directory=tmp_path)
        meta = json.loads(stored.path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        meta["schema_version"] = "99"
        stored.path.with_suffix(".meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="unsupported session schema"):
            load_session(stored.path)

    def test_state_hash_is_deterministic(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars, clock=lambda: FILL_CLOCK)
        snapshot = session.snapshot()
        hash_a = save_session(snapshot, directory=tmp_path / "a", name="h1").state_hash
        hash_b = save_session(snapshot, directory=tmp_path / "b", name="h1").state_hash
        assert hash_a == hash_b
        assert len(hash_a) == 64


# --------------------------------------------------------------------------- #
# Broker snapshot/restore
# --------------------------------------------------------------------------- #


class TestBrokerSnapshotRestore:
    def test_snapshot_captures_orders_and_fills(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        orders, fills = session.broker.snapshot()
        assert len(orders) == 1
        assert len(fills) == 1
        assert orders[0].order_id is not None
        assert orders[0].status.value == "FILLED"
        assert fills[0].price == ENTRY
        # Copies are detached
        assert orders[0] is not session.broker._orders[orders[0].order_id]

    def test_snapshot_is_stable_copy(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY, 5: Signal.SELL}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        snapshot = session.snapshot()
        session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        assert len(snapshot.orders) == 1
        assert snapshot.fills_count == 1
        assert snapshot.trades_count == 1
        assert session.fills == 2

    def test_restore_into_fresh_broker(self) -> None:
        broker = PaperBroker()
        live = PaperBroker()
        bars = _bars(4)
        session = _make_session(bars, plan={1: Signal.BUY}, broker=live, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        orders, fills = live.snapshot()
        broker.restore(orders, fills)
        assert broker.get_order(orders[0].order_id) is orders[0]
        assert broker.fills == fills

    def test_restore_into_non_empty_broker_raises(self) -> None:
        broker = PaperBroker()
        bars = _bars(4)
        session = _make_session(bars, plan={1: Signal.BUY}, broker=broker, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        with pytest.raises(RuntimeError, match="non-empty"):
            broker.restore(*broker.snapshot())


# --------------------------------------------------------------------------- #
# Session snapshot/restore (in-memory)
# --------------------------------------------------------------------------- #


class TestSessionSnapshotRestore:
    def test_round_trip_portfolio_fields(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        snap = session.snapshot()
        fresh_session = _make_session(
            bars, plan={}, clock=lambda: FILL_CLOCK, started=False,
        )
        fresh_session.restore(snap)
        _assert_portfolios_equivalent(session.portfolio, fresh_session.portfolio)
        assert fresh_session._consumed == session._consumed
        assert fresh_session._entry_candle == session._entry_candle
        assert fresh_session.orders_submitted == session.orders_submitted
        assert fresh_session.fills == session.fills
        assert fresh_session.trades == session.trades
        assert fresh_session.rejections == session.rejections
        assert fresh_session.skipped_candles == session.skipped_candles

    def test_restore_guards_running_session(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        snap = session.snapshot()
        with pytest.raises(RuntimeError, match="running"):
            session.restore(snap)

    def test_restore_guards_wrong_instrument_symbol(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars, started=False)
        snap = session.snapshot()
        modified_instrument = Instrument(
            symbol="BANKNIFTY",
            instrument_type=InstrumentType.INDEX,
            underlying_symbol="BANKNIFTY",
        )
        modified = replace(snap, instrument=modified_instrument)
        with pytest.raises(ValueError, match="does not match"):
            session.restore(modified)

    def test_restore_guards_wrong_interval(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars, started=False)
        snap = session.snapshot()
        modified = replace(snap, interval_token="15m")
        with pytest.raises(ValueError, match="does not match"):
            session.restore(modified)

    def test_restore_guards_wrong_warmup(self, tmp_path: Path) -> None:
        bars = _bars(24)
        session = _make_session(bars, warmup_bars=22, started=False)
        snap = session.snapshot()
        modified = replace(snap, warmup_bars=5)
        with pytest.raises(ValueError, match="does not match"):
            session.restore(modified)


# --------------------------------------------------------------------------- #
# Full save/load -> restore round-trip
# --------------------------------------------------------------------------- #


class TestSaveLoadRestoreRoundTrip:
    def test_round_trip_decimal_exactness_with_cost(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        settings = _settings_with_cost()
        provider = _provider(bars)
        session = PaperSession(
            settings,
            provider,
            _strategy({2: Signal.BUY, 5: Signal.SELL}),
            clock=lambda: FILL_CLOCK,
        )
        session.start()
        session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        restored = _make_session(
            bars, plan={}, clock=lambda: FILL_CLOCK, started=False,
        )
        restored.restore(loaded.snapshot)
        assert restored.portfolio.cash == session.portfolio.cash
        assert restored.portfolio.realized_pnl == session.portfolio.realized_pnl
        for symbol in session.portfolio.open_positions():
            orig = session.portfolio.position_for(symbol)
            rest = restored.portfolio.position_for(symbol)
            assert rest is not None
            assert rest.average_entry_price == orig.average_entry_price
            assert rest.quantity == orig.quantity
            assert rest.realized_pnl == orig.realized_pnl

    def test_initial_cash_preserved_after_restore(self, tmp_path: Path) -> None:
        closes = [ENTRY] * 8
        closes[6] = Decimal("24100")  # sell at a profit so cash != initial
        bars = _bars(8, closes=closes)
        session = _make_session(bars, plan={2: Signal.BUY, 6: Signal.SELL}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(7, BASE) + timedelta(minutes=5))
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        restored = _make_session(
            bars, plan={}, clock=lambda: FILL_CLOCK, started=False,
        )
        restored.restore(loaded.snapshot)
        assert restored.portfolio.initial_cash == Decimal("100000")
        assert restored.portfolio.cash != Decimal("100000")
        assert restored.portfolio.realized_pnl == session.portfolio.realized_pnl

    def test_consumed_timestamps_block_reprocessing(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        restored = _make_session(
            bars, plan={}, clock=lambda: FILL_CLOCK, started=False,
        )
        restored.restore(loaded.snapshot)
        restored.start()
        result = restored.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        assert result.empty
        assert restored.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_only_non_flat_positions_serialized(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY, 6: Signal.SELL}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(7, BASE) + timedelta(minutes=5))
        assert "NIFTY_INDEX" in session.portfolio.positions
        assert session.portfolio.position_for("NIFTY_INDEX").is_flat
        stored = save_session(session.snapshot(), directory=tmp_path)
        payload = json.loads(stored.path.read_text(encoding="utf-8"))
        assert payload["portfolio"]["positions"] == []

    def test_counters_restored(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(
            bars,
            plan={2: Signal.BUY, 6: Signal.SELL},
            clock=lambda: FILL_CLOCK,
            quantity=200,  # rejection (max_position_quantity=75)
            use_default_sizer=False,
        )
        session.poll(when=_ts(7, BASE) + timedelta(minutes=5))
        assert session.rejections == 1
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        restored = _make_session(
            bars, plan={}, clock=lambda: FILL_CLOCK, started=False,
        )
        restored.restore(loaded.snapshot)
        assert restored.orders_submitted == session.orders_submitted
        assert restored.fills == session.fills
        assert restored.trades == session.trades
        assert restored.rejections == session.rejections
        assert restored.skipped_candles == session.skipped_candles
        assert restored.consumed_candles == session.consumed_candles

    def test_order_ids_round_trip(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        orig_ids = {o.order_id for o in session.broker.snapshot()[0]}
        loaded_ids = {o.order_id for o in loaded.snapshot.orders}
        assert orig_ids == loaded_ids

    def test_fill_fields_preserved(self, tmp_path: Path) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        assert len(loaded.snapshot.fills) == 1
        fill = loaded.snapshot.fills[0]
        assert fill.price == ENTRY
        assert fill.side.value == "BUY"
        assert fill.quantity == 2  # default sizer size

    def test_stop_order_type_preserved(self, tmp_path: Path) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2, open_=24000, close=24000),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
        ]
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        assert session.trades == 2
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        stop_orders = [o for o in loaded.snapshot.orders if o.order_type == OrderType.STOP]
        assert len(stop_orders) == 1
        assert stop_orders[0].status.value == "FILLED"


# --------------------------------------------------------------------------- #
# Restart-equivalence: restored session produces same outcome as uninterrupted
# --------------------------------------------------------------------------- #


class TestRestartEquivalence:
    def test_full_restart_equivalence(self, tmp_path: Path) -> None:
        """Restore into a fresh session, continue, compare to an uninterrupted run."""
        closes = [ENTRY] * 12
        bars = _bars(12, closes=closes)
        clock = lambda: FILL_CLOCK
        plan = {2: Signal.BUY, 7: Signal.SELL}
        warmup = 1

        def run_reference() -> Portfolio:
            session = _make_session(
                bars, plan=plan, clock=clock, warmup_bars=warmup, quantity=3,
                use_default_sizer=False,
            )
            session.poll(when=_ts(10, BASE) + timedelta(minutes=5))
            return session.portfolio

        def run_with_restart() -> Portfolio:
            session = _make_session(
                bars, plan=plan, clock=clock, warmup_bars=warmup, quantity=3,
                use_default_sizer=False,
            )
            session.poll(when=_ts(5, BASE) + timedelta(minutes=5))
            stored = save_session(session.snapshot(), directory=tmp_path)
            loaded = load_session(stored.path)
            restored = _make_session(
                bars, plan=plan, clock=clock, warmup_bars=warmup, quantity=3,
                use_default_sizer=False, started=False,
            )
            restored.restore(loaded.snapshot)
            restored.start()
            restored.poll(when=_ts(10, BASE) + timedelta(minutes=5))
            return restored.portfolio

        ref = run_reference()
        rest = run_with_restart()
        _assert_portfolios_equivalent(ref, rest)

    def test_stop_fires_after_restore(self, tmp_path: Path) -> None:
        """Protective exit works after restore because entry_candle is persisted."""
        bars = [
            _bar(0),
            _bar(1),
            _bar(2, open_=24000, close=24000),
            _bar(3, open_="24050", high="24090", low="23800", close="24080"),
            _bar(4, open_="24000", high="24040", low="23300", close="23900"),
            _bar(5),
        ]
        plan = {2: Signal.BUY}
        clock = lambda: FILL_CLOCK
        # Run to bar 3 (entry at bar 2, bar 3 safe)
        session = _make_session(
            bars, plan=plan, clock=clock, warmup_bars=1, quantity=1,
            use_default_sizer=False,
        )
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 1

        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        restored = _make_session(
            bars, plan=plan, clock=clock, warmup_bars=1, quantity=1,
            use_default_sizer=False, started=False,
        )
        restored.restore(loaded.snapshot)
        restored.start()

        # Bar 4 breaches the stop
        result = restored.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        stop = result.steps[0]
        assert stop.stop_result is not None
        assert stop.stop_result.fill.price == STOP
        assert restored.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert restored.trades == 2


# --------------------------------------------------------------------------- #
# Structural checks
# --------------------------------------------------------------------------- #


class TestDesignInvariants:
    def test_no_backtest_import(self) -> None:
        src = Path(__file__).resolve().parents[1] / "src" / "fno_ai_paper_trading" / "persistence" / "session_store.py"
        import_lines = [
            line
            for line in src.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        assert not any("backtest" in line for line in import_lines)

    def test_gitignore_contains_paper_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert "paper_state" in gitignore

    def test_negative_realized_pnl_round_trips(self, tmp_path: Path) -> None:
        """A position partially closed at a loss has negative realized_pnl."""
        bars = _bars(12, closes=[ENTRY] * 12)
        plan = {2: Signal.BUY}
        # Force a partial sell by creating a custom strategy that emits BUY
        # at bar 2 and a split (partial) SELL via signal at bar 5. Actually
        # PaperSession only sells full current_quantity. Instead we manually
        # position a portfolio with negative realized_pnl on an open position.
        portfolio = Portfolio(Decimal("100000"), long_only=True)
        portfolio.positions["NIFTY_INDEX"] = Position(
            instrument=_index(),
            quantity=1,
            average_entry_price=ENTRY,
            opened_at=BASE + timedelta(minutes=10),
        )
        portfolio.cash = Decimal("100000") - ENTRY
        # Simulate a previous partial close at loss
        portfolio.positions["NIFTY_INDEX"].realized_pnl = Decimal("-200")
        portfolio.realized_pnl = Decimal("-200")
        portfolio.trade_history.append(
            Trade(
                trade_id="T_LOSS",
                instrument=_index(),
                side=OrderSide.SELL,
                quantity=1,
                price=ENTRY - Decimal("200"),
                commission=Decimal("0"),
                executed_at=BASE + timedelta(minutes=15),
                realized_pnl=Decimal("-200"),
            )
        )
        session = _make_session(bars, plan=plan, portfolio=portfolio, clock=lambda: FILL_CLOCK)
        snap = session.snapshot()
        assert snap.portfolio.position_for("NIFTY_INDEX").realized_pnl == Decimal("-200")
        stored = save_session(snap, directory=tmp_path)
        loaded = load_session(stored.path)
        assert loaded.snapshot.portfolio.position_for("NIFTY_INDEX").realized_pnl == Decimal("-200")
        restored = _make_session(
            bars, plan=plan, clock=lambda: FILL_CLOCK, started=False,
        )
        restored.restore(loaded.snapshot)
        assert restored.portfolio.position_for("NIFTY_INDEX").realized_pnl == Decimal("-200")
        assert restored.portfolio.realized_pnl == Decimal("-200")

    def test_protective_exit_order_type_through_save_load(self, tmp_path: Path) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2, open_=24000, close=24000),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
            _bar(5),
        ]
        session = _make_session(bars, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll(when=_ts(5, BASE) + timedelta(minutes=5))
        assert session.trades == 2
        stored = save_session(session.snapshot(), directory=tmp_path)
        loaded = load_session(stored.path)
        order_types = [o.order_type.value for o in loaded.snapshot.orders]
        assert "STOP" in order_types

    def test_snapshot_fields_are_frozen(self, tmp_path: Path) -> None:
        bars = _bars(4)
        session = _make_session(bars)
        snap = session.snapshot()
        with pytest.raises(AttributeError):
            snap.warmup_bars = 99
