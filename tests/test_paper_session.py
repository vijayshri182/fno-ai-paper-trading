"""Tests for the WS 6.4b deterministic paper session runtime.

Covers the ``PaperSession`` orchestrator against the V1 contract
(``docs/trading/PAPER_TRADING_V1.md`` §13): completed-candle cadence, duplicate
guard, phase/holiday gates, 22-bar warm-up, long-only signal mapping, daily-loss
policy (entries gated, exits executable at the cap), protective-stop ordering
(signal-first, stop-second, entry-candle excluded), injected clock/fill prices,
``--once`` no-new-candle behavior, deterministic replay, provider-failure
recovery, broker-rejection recording, accounting fail-safety, lifecycle and
zero persistence.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.broker.paper_broker import PaperBroker
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.errors import UnavailableError
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import (
    InstrumentType,
    OrderSide,
    OrderStatus,
    Signal,
)
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer
from fno_ai_paper_trading.risk.stop_loss import StopLossPolicy
from fno_ai_paper_trading.services.paper_session import PaperSession, _MISSING
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

BASE = datetime(2026, 9, 2, 9, 15)  # Wednesday, trading day, not a holiday
OPEN_NOW = datetime(2026, 9, 2, 12, 0)
FILL_CLOCK = datetime(2026, 9, 2, 10, 0)
NON_TRADING_NOW = datetime(2026, 9, 5, 12, 0)  # Saturday
HOLIDAY_NOW = datetime(2026, 4, 3, 13, 0)  # Good Friday (closed)
PRE_OPEN_NOW = datetime(2026, 9, 2, 9, 10)
CLOSED_NOW = datetime(2026, 9, 2, 16, 0)

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


class _ScriptedStrategy(Strategy):
    """Deterministic strategy: emits the configured signal at set bar indices."""

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


class _FailingPaperBroker(PaperBroker):
    """A PaperBroker that rejects every order at the execution layer."""

    def place_order(self, order, market_price=None):
        order.reject("simulated broker rejection")
        return None


class _FlakyProvider(InMemoryMarketDataProvider):
    """Provider that fails its first ``get_ohlcv`` call, then recovers."""

    def __init__(self, bars: list[MarketPrice]) -> None:
        super().__init__(instruments=[_index()], history={_index().symbol: bars})
        self._failures = 1

    def get_ohlcv(self, instrument, limit=None):
        if self._failures > 0:
            self._failures -= 1
            raise UnavailableError("provider unavailable (simulated)")
        return super().get_ohlcv(instrument, limit)


class _ExplodingPortfolio(Portfolio):
    """Portfolio whose accounting raises once a fill is applied."""

    def apply_fill(self, fill: Fill) -> Trade:
        raise ValueError("accounting exploded (simulated)")


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
    sizer: RiskBasedPositionSizer | None = None,
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


def _seed_at_loss_cap(holding_quantity: int = 0) -> Portfolio:
    """A portfolio already -2000 realized today (max_daily_loss 1000),
    optionally still holding ``holding_quantity`` lots long at the cap."""
    index = _index()
    portfolio = Portfolio(Decimal("100000"), long_only=True)
    if holding_quantity > 0:
        portfolio.positions[index.symbol] = Position(
            instrument=index,
            quantity=holding_quantity,
            average_entry_price=ENTRY,
            opened_at=BASE,
        )
        cost = holding_quantity * ENTRY
    else:
        cost = Decimal("0")
    portfolio.cash = Decimal("100000") - Decimal("2000") - cost
    portfolio.realized_pnl = Decimal("-2000")
    portfolio.trade_history.append(
        Trade(
            trade_id="T1",
            instrument=index,
            side=OrderSide.SELL,
            quantity=2,
            price=Decimal("23800"),
            commission=Decimal("0"),
            executed_at=BASE,
            realized_pnl=Decimal("-2000"),
        )
    )
    return portfolio


# ---------------------------------------------------------------------------
# Construction, environment guard, lifecycle
# ---------------------------------------------------------------------------


class TestConstructionAndEnvironment:
    def test_constructor_wires_defaults(self) -> None:
        session = _make_session(_bars(8, closes=[ENTRY] * 8), plan={2: Signal.BUY})
        assert isinstance(session.broker, PaperBroker)
        assert session.portfolio.initial_cash == Decimal("100000")
        assert session.portfolio.long_only is True
        assert session.sizer is not None
        assert session.stop_policy.stop_loss_pct == Decimal("0.02")
        assert session.interval_token == "5m"
        assert session.interval_minutes == 5
        assert session.warmup_bars == 1  # scripted strategy derives no slow period
        assert session.instrument.symbol == "NIFTY_INDEX"
        assert session.is_running

    def test_instrument_required_when_provider_exposes_many(self) -> None:
        with pytest.raises(ValueError, match="explicit instrument"):
            PaperSession(
                _settings(),
                InMemoryMarketDataProvider(),  # 3 default instruments
                _strategy({0: Signal.BUY}),
            )

    def test_paper_environment_required_to_start(self) -> None:
        session = _make_session(
            _bars(8), environment=Environment.DEVELOPMENT, started=False
        )
        with pytest.raises(EnvironmentError, match="PAPER"):
            session.start()
        assert not session.is_running

    def test_sandbox_override_allows_test_environment(self) -> None:
        session = _make_session(
            _bars(8),
            environment=Environment.TEST,
            allow_sandbox=True,
            started=False,
        )
        session.start()
        assert session.is_running

    def test_poll_requires_started_session(self) -> None:
        session = _make_session(_bars(8), started=False)
        with pytest.raises(RuntimeError, match="not running"):
            session.poll(when=OPEN_NOW)

    def test_non_paper_broker_rejected(self) -> None:
        class _DummyBroker(Broker):
            def place_order(self, order, market_price=None):
                return None

            def cancel_order(self, order_id):
                return None

            def get_order(self, order_id):
                return None

        with pytest.raises(RuntimeError, match="non-paper"):
            _make_session(_bars(8), broker=_DummyBroker())

    def test_unknown_and_weekly_intervals_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown session interval"):
            _make_session(_bars(8), interval="2d")
        with pytest.raises(ValueError, match="fixed-minute"):
            _make_session(_bars(8), interval="1w")

    def test_start_stop_lifecycle(self) -> None:
        session = _make_session(_bars(8), started=False)
        session.start()
        session.start()  # idempotent
        assert session.is_running
        session.stop()
        assert not session.is_running
        with pytest.raises(RuntimeError, match="not running"):
            session.poll(when=OPEN_NOW)
        session.start()
        assert session.is_running


# ---------------------------------------------------------------------------
# Completed-candle cadence and duplicate guard
# ---------------------------------------------------------------------------


class TestCompletedCandleCadence:
    def test_forming_bar_is_not_traded(self) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={3: Signal.BUY})
        result = session.poll(when=datetime(2026, 9, 2, 9, 34))  # bar 3 completes 09:35
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert result.consumed == 3
        assert session.orders_submitted == 0

    def test_completed_bar_is_traded(self) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={3: Signal.BUY})
        result = session.poll(when=datetime(2026, 9, 2, 9, 35))
        assert any(step.order_result is not None and step.order_result.fill is not None for step in result.steps)
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_duplicate_candle_is_not_reprocessed(self) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={3: Signal.BUY})
        first = session.poll(when=datetime(2026, 9, 2, 9, 36))
        second = session.poll(when=datetime(2026, 9, 2, 9, 37))
        assert first.consumed == 4
        assert second.empty
        assert session.orders_submitted == 1
        assert session.fills == 1
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2


# ---------------------------------------------------------------------------
# Phase and holiday gating
# ---------------------------------------------------------------------------


class TestPhaseAndHolidayGates:
    def test_no_trading_on_non_trading_day(self) -> None:
        bars = _bars(4, start=datetime(2026, 9, 5, 9, 15), closes=[ENTRY] * 4)
        session = _make_session(bars, plan={1: Signal.BUY})
        result = session.poll(when=datetime(2026, 9, 5, 12, 0))
        assert len(result.steps) == 4
        assert all(step.skipped for step in result.steps)
        assert all("not a trading day" in step.skip_reason for step in result.steps)
        assert session.orders_submitted == 0
        assert session.portfolio.open_positions() == {}

    def test_no_trading_on_holiday(self) -> None:
        bars = _bars(4, start=datetime(2026, 4, 3, 9, 15), closes=[ENTRY] * 4)
        session = _make_session(bars, plan={1: Signal.BUY})
        result = session.poll(when=HOLIDAY_NOW)
        assert all("not a trading day" in step.skip_reason for step in result.steps)
        assert session.portfolio.open_positions() == {}

    def test_no_trading_outside_open_phase(self) -> None:
        bars = _bars(4, start=datetime(2026, 9, 2, 8, 45), closes=[ENTRY] * 4)
        session = _make_session(bars, plan={1: Signal.BUY})
        result = session.poll(when=PRE_OPEN_NOW)
        assert len(result.steps) == 4
        assert all(step.skipped for step in result.steps)
        assert all("outside the NSE OPEN phase" in step.skip_reason for step in result.steps)
        assert session.orders_submitted == 0

    def test_no_trading_after_close(self) -> None:
        bars = _bars(4, closes=[ENTRY] * 4)
        session = _make_session(bars, plan={1: Signal.BUY})
        result = session.poll(when=CLOSED_NOW)
        assert len(result.steps) == 4
        assert all("outside the NSE OPEN phase" in step.skip_reason for step in result.steps)
        assert session.portfolio.open_positions() == {}


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_no_orders_before_warmup_complete(self) -> None:
        bars = _bars(24, closes=[ENTRY] * 24)
        session = _make_session(
            bars,
            plan={5: Signal.BUY},
            warmup_bars=22,
            use_default_sizer=False,
        )
        result = session.poll(when=_ts(23, BASE) + timedelta(minutes=5))
        assert result.consumed == 24
        assert session.orders_submitted == 0
        # The warm-up gate skips exactly the bars with fewer than 22 completed
        # predecessors (indices 0..20); the rest are plain HOLD candles.
        assert session.skipped_candles == 21
        assert session.portfolio.open_positions() == {}

    def test_signal_allowed_at_warmup_boundary(self) -> None:
        bars = _bars(24, closes=[ENTRY] * 24)
        session = _make_session(
            bars,
            plan={21: Signal.BUY},
            warmup_bars=22,
            use_default_sizer=False,
        )
        result = session.poll(when=_ts(21, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if not step.skipped and step.order_result is not None)
        assert buy.order_result.fill is not None
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 1


# ---------------------------------------------------------------------------
# Signal mapping (long-only)
# ---------------------------------------------------------------------------


class TestSignalMapping:
    def test_buy_when_flat_enters_sized(self) -> None:
        bars = _bars(8, closes=[ENTRY] * 8)
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        step = result.steps[2]
        assert step.sizing is not None
        assert step.sizing.approved
        assert step.sizing.quantity == 2  # 1% of 100k / 2% stop distance at 24000
        assert step.order_result.fill is not None
        assert step.order_result.fill.price == ENTRY
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_buy_while_long_is_ignored(self) -> None:
        bars = _bars(8, closes=[ENTRY, ENTRY, ENTRY, ENTRY, ENTRY, ENTRY, ENTRY, ENTRY])
        session = _make_session(bars, plan={2: Signal.BUY, 4: Signal.BUY})
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2
        result = session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        step = result.steps[0]  # only bar 4 is new in this poll
        assert step.skipped
        assert "already long" in step.skip_reason
        assert session.orders_submitted == 1
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_sell_closes_long(self) -> None:
        closes = [ENTRY] * 6 + [Decimal("24100")] + [ENTRY]
        bars = _bars(8, closes=closes)
        session = _make_session(bars, plan={2: Signal.BUY, 6: Signal.SELL})
        result = session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        sell = next(step for step in result.steps if step.signal is Signal.SELL)
        assert sell.order_result is not None
        assert sell.order_result.fill is not None
        assert sell.order_result.fill.price == Decimal("24100")
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert session.portfolio.open_positions() == {}
        assert session.trades == 2
        assert session.portfolio.realized_pnl == Decimal("200")

    def test_sell_on_flat_is_ignored(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={3: Signal.SELL})
        result = session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        step = next(step for step in result.steps if step.signal is Signal.SELL)
        assert step.skipped
        assert "flat" in step.skip_reason
        assert session.orders_submitted == 0

    def test_hold_never_creates_orders(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={})
        result = session.poll(when=_ts(7, BASE) + timedelta(minutes=5))
        assert result.consumed == 8
        assert all(not step.skipped for step in result.steps)
        assert session.orders_submitted == 0
        assert session.fills == 0
        assert session.trades == 0


# ---------------------------------------------------------------------------
# Daily-loss policy (entries gated, exits executable)
# ---------------------------------------------------------------------------


class TestDailyLossPolicy:
    def test_entry_blocked_at_loss_cap(self) -> None:
        portfolio = _seed_at_loss_cap()  # flat at the cap
        bars = _bars(4)
        session = _make_session(
            bars, plan={1: Signal.BUY}, portfolio=portfolio, use_default_sizer=False
        )
        result = session.poll(when=_ts(1, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        assert buy.skipped
        assert "daily-loss limit reached" in buy.skip_reason
        assert session.orders_submitted == 0
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0  # nothing entered

    def test_exit_stays_executable_at_loss_cap(self) -> None:
        portfolio = _seed_at_loss_cap(holding_quantity=2)
        bars = _bars(4)
        session = _make_session(
            bars, plan={1: Signal.SELL}, portfolio=portfolio, use_default_sizer=False
        )
        result = session.poll(when=_ts(1, BASE) + timedelta(minutes=5))
        sell = next(step for step in result.steps if step.signal is Signal.SELL)
        assert not sell.skipped
        assert sell.order_result is not None
        assert sell.order_result.fill is not None
        assert "signal exit at daily-loss cap" in sell.order_result.decision.reasons
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert session.portfolio.open_positions() == {}


# ---------------------------------------------------------------------------
# Protective stop integration (signal-first, stop-second)
# ---------------------------------------------------------------------------


class TestStopIntegration:
    def test_intrabar_stop_breach_exits_at_stop(self) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2, open_=24000, close=24000),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
            _bar(5),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(5, BASE) + timedelta(minutes=5))
        stop = result.steps[3]
        assert stop.stop_result is not None
        assert stop.stop_result.reference_price == STOP  # intrabar breach
        assert stop.stop_result.fill.price == STOP
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert session.trades == 2  # BUY + protective exit

    def test_gap_open_below_stop_exits_at_open(self) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2, open_=24000, close=24000),
            _bar(3, open_="23400", high="23450", low="23350", close="23400"),
            _bar(4),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        stop = result.steps[3]
        assert stop.stop_result is not None
        assert stop.stop_result.reference_price == Decimal("23400")  # open-through / gap
        assert stop.stop_result.fill.price == Decimal("23400")
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0

    def test_entry_candle_never_stopped_and_no_stop_when_safe(self) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2, open_=24000, low="23000", close=24000),  # breach but entry candle
            _bar(3, open_="24050", high="24090", low="23800", close="24080"),  # safe
            _bar(4, open_="24000", high="24040", low="23300", close="23900"),  # breach
            _bar(5),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(5, BASE) + timedelta(minutes=5))
        assert result.steps[2].stop_result is None  # entry candle excluded
        assert result.steps[3].stop_result is None  # safe candle, no exit
        assert result.steps[3].order_result is None
        assert result.steps[4].stop_result is not None
        assert result.steps[4].stop_result.fill.price == STOP
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert session.trades == 2


# ---------------------------------------------------------------------------
# Clock and fill prices
# ---------------------------------------------------------------------------


class TestClockAndFillPrices:
    def test_injected_clock_stamps_fills(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={1: Signal.BUY}, clock=lambda: FILL_CLOCK)
        session.poll()  # no explicit when: the clock is the source of truth
        assert session.now() == FILL_CLOCK
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2
        fill = session._trading.broker.fills[0]
        assert fill.filled_at == FILL_CLOCK

    def test_entry_fill_uses_completed_bar_close_not_forming_bar(self) -> None:
        # closes climb; the last provider bar is still forming at decision time.
        closes = [
            Decimal("24000"),
            Decimal("24010"),
            Decimal("24020"),
            Decimal("24030"),
            Decimal("24040"),
            Decimal("24050"),
        ]
        bars = _bars(6, closes=closes)
        session = _make_session(bars, plan={3: Signal.BUY})
        result = session.poll(when=datetime(2026, 9, 2, 9, 39))  # bar 3 done, bar 4 forming
        buy = next(step for step in result.steps if step.order_result is not None)
        assert buy.order_result.fill.price == Decimal("24030")  # not provider last (24050)
        assert buy.order_result.reference_price == Decimal("24030")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_replay_produces_identical_state(self) -> None:
        closes = [
            Decimal("24000"),
            Decimal("24000"),
            Decimal("24000"),
            Decimal("24100"),
            Decimal("24150"),
            Decimal("24100"),
            Decimal("24050"),
            Decimal("24100"),
        ]
        bars = _bars(8, closes=closes)

        def run() -> tuple:
            session = _make_session(
                bars, plan={2: Signal.BUY, 6: Signal.SELL}, clock=lambda: FILL_CLOCK
            )
            session.poll()
            fills = [(f.side.value, str(f.price), f.quantity) for f in session._trading.broker.fills]
            trades = [(t.side.value, str(t.price), str(t.realized_pnl)) for t in session.portfolio.trade_history]
            equity = session.portfolio.total_value(
                {bars[-1].instrument.symbol: bars[-1].close}
            )
            return (
                fills,
                trades,
                str(session.portfolio.cash),
                str(session.portfolio.realized_pnl),
                str(equity),
                session.orders_submitted,
                session.fills,
                session.trades,
            )

        assert run() == run()
        assert run()[6] == 2  # both orders filled
        assert run()[7] == 2  # both trades recorded
        # 2 lots entered at 24000, exited at 24050: 2 * 50 = 100 realized.
        assert run()[1][-1][2] == "100"


# ---------------------------------------------------------------------------
# --once / polling semantics
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_run_once_consumes_latest_candle(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.run_once(when=_ts(2, BASE) + timedelta(minutes=5))
        assert result.consumed == 3
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_run_once_no_new_candle_changes_nothing(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        when = _ts(2, BASE) + timedelta(minutes=5)
        session.run_once(when=when)
        before = (session.orders_submitted, session.fills, session.trades, session.portfolio.cash)
        result = session.run_once(when=when)  # same decision time: no new candle
        assert result.empty
        assert (session.orders_submitted, session.fills, session.trades, session.portfolio.cash) == before

    def test_run_loop_is_synchronous_and_deterministic(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        results = session.run_loop(polls=2, when=lambda: _ts(2, BASE) + timedelta(minutes=5))
        assert len(results) == 2
        assert results[0].consumed == 3
        assert results[1].empty


# ---------------------------------------------------------------------------
# Provider failure, broker rejection, accounting fail-safe, persistence
# ---------------------------------------------------------------------------


class TestFailureAndSafety:
    def test_provider_failure_recovers_next_poll(self) -> None:
        provider = _FlakyProvider(_bars(8))
        session = _make_session(_bars(8), provider=provider, plan={1: Signal.BUY})
        failed = session.poll(when=_ts(1, BASE) + timedelta(minutes=5))
        assert failed.provider_error is not None
        assert "unavailable" in failed.provider_error
        assert failed.empty
        assert session.consumed_candles == 0
        assert session.portfolio.open_positions() == {}

        recovered = session.poll(when=_ts(1, BASE) + timedelta(minutes=5))
        assert recovered.provider_error is None
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_broker_rejection_is_recorded(self) -> None:
        broker = _FailingPaperBroker()
        bars = _bars(8)
        session = _make_session(
            bars, plan={2: Signal.BUY}, broker=broker, use_default_sizer=False
        )
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        assert buy.order_result is not None
        assert buy.order_result.fill is None
        assert buy.order_result.order.rejection_reason == "simulated broker rejection"
        assert session.rejections == 1
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0

    def test_risk_manager_rejection_is_recorded(self) -> None:
        bars = _bars(8)
        # Quantity beyond max_position_quantity=75 must be rejected and recorded.
        session = _make_session(
            bars,
            plan={2: Signal.BUY},
            quantity=200,
            use_default_sizer=False,
        )
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        assert buy.order_result is not None
        assert buy.order_result.fill is None
        assert buy.order_result.order.rejection_reason is not None
        assert session.rejections == 1
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0

    def test_accounting_failure_is_survived(self) -> None:
        portfolio = _ExplodingPortfolio(Decimal("100000"), long_only=True)
        bars = _bars(8)
        session = _make_session(
            bars,
            plan={1: Signal.BUY},
            portfolio=portfolio,
            use_default_sizer=False,
        )
        result = session.poll(when=_ts(1, BASE) + timedelta(minutes=5))
        step = result.steps[1]
        assert step.error is not None
        assert "accounting exploded" in step.error
        assert session.is_running
        assert session.orders_submitted == 1
        assert session.rejections == 1
        assert session.fills == 0
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0

    def test_no_persistence_artefacts(self) -> None:
        before = set(os.listdir("."))
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY, 6: Signal.SELL})
        session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        assert not Path("paper_state").exists()
        assert set(os.listdir(".")) == before
        unattended = [p for p in Path(".").iterdir() if p.is_dir() and p.name not in before]
        assert unattended == []