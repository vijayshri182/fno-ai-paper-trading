"""WS 6.7 — deterministic session-level replay of the V1 acceptance criteria.

Offline, deterministic acceptance tests that replay the 30 criteria from
``docs/trading/PAPER_TRADING_V1.md`` §13 through the real
:class:`~fno_ai_paper_trading.services.paper_session.PaperSession` runtime
(no network, no credentials, no sleep). Every criterion in §13 has exactly one
test method named ``test_ac_XX_*`` (AC-01 .. AC-30), grouped by the spec's own
section headings:

* Risk sizing (1-6), Stop-loss (7-12), Long-only (13-14),
* Completed-candle cadence (15-18), Virtual capital and accounting (19-21),
* Deterministic behavior (22-23), Safety boundaries (24-30).

Where a criterion is a property owned by a consumed component (the sizer, the
stop executor, ``Portfolio.apply_fill``), the acceptance test still exercises it
*through the session* — via the recorded ``SessionStep.sizing`` / ``stop_result``
values — so the criteria are verified end-to-end on the running runtime, not
only in the component unit suites.

Documented deviation implemented on purpose: AC-08's literal "routed through
``RiskManager``" wording predates the WS 6.4 design decision that protective
stops deliberately do **not** consult the ``RiskManager``, so a protective exit
cannot be blocked by the daily-loss cap (see PROGRESS.md §6). The acceptance
test therefore asserts the exit still produces a paper ``Fill``/``Trade`` through
``PaperBroker`` -> ``Portfolio.apply_fill`` and is recorded, which is the
criterion's intent.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.broker.base import Broker
from fno_ai_paper_trading.broker.paper_broker import PaperBroker
from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import (
    InstrumentType,
    OrderSide,
    OrderType,
    Signal,
)
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.models.position import Position, Trade
from fno_ai_paper_trading.persistence.session_store import load_session, save_session
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.sizer import RiskBasedPositionSizer
from fno_ai_paper_trading.services.paper_session import PaperSession, _MISSING
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

BASE = datetime(2026, 9, 2, 9, 15)  # Wednesday, trading day, not a holiday
FILL_CLOCK = datetime(2026, 9, 2, 10, 0)

ENTRY = Decimal("24000")
STOP = Decimal("23520")  # ENTRY * 0.98
MULT = Decimal("1")


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


def _bars(count: int, *, start: datetime = BASE) -> list[MarketPrice]:
    return [_bar(i, start=start) for i in range(count)]


def _provider(bars: list[MarketPrice]) -> InMemoryMarketDataProvider:
    index = _index()
    return InMemoryMarketDataProvider(instruments=[index], history={index.symbol: bars})


def _settings(**overrides) -> PaperSettings:
    values = dict(
        environment=Environment.PAPER,
        initial_capital=Decimal("100000"),
        max_position_quantity=75,
        max_order_notional=Decimal("250000"),
        max_daily_loss=Decimal("1000"),
        commission_rate=Decimal("0"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"),
    )
    values.update(overrides)
    return PaperSettings(**values)


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
    settings: PaperSettings | None = None,
    allow_sandbox: bool = False,
    started: bool = True,
) -> PaperSession:
    if provider is None:
        provider = _provider(bars)
    if strategy is None:
        strategy = _strategy(plan or {})
    if settings is None:
        settings = _settings(environment=environment)
    else:
        settings = settings
    session = PaperSession(
        settings,
        provider,
        strategy,
        portfolio=portfolio,
        broker=broker,
        clock=clock if clock is not None else (lambda: FILL_CLOCK),
        warmup_bars=warmup_bars,
        quantity=quantity,
        sizer=_MISSING if use_default_sizer else sizer,
        interval=interval,
        allow_sandbox=allow_sandbox,
    )
    if started:
        session.start()
    return session


def _buying_steps(result) -> list:
    return [
        s
        for s in result.steps
        if s.signal is Signal.BUY and s.order_result is not None and s.order_result.fill is not None
    ]


def _all_recorded_money(session, result):
    """Yield every money figure the session recorded, for Decimal-only sweeps."""
    money = [
        session.portfolio.cash,
        session.portfolio.realized_pnl,
        session.portfolio.initial_cash,
    ]
    for step in result.steps:
        money.append(step.equity)
        if step.sizing is not None:
            money += [step.sizing.risk_amount, step.sizing.stop_distance, step.sizing.stop_price]
        for handled in (step.order_result, step.stop_result):
            if handled is None:
                continue
            if handled.reference_price is not None:
                money.append(handled.reference_price)
            if handled.fill is not None:
                money += [handled.fill.price, handled.fill.commission]
            if handled.trade is not None:
                money += [handled.trade.price, handled.trade.commission, handled.trade.realized_pnl]
    return money


# ---------------------------------------------------------------------------
# AC 1-6 — Risk sizing
# ---------------------------------------------------------------------------


class TestAcceptanceRiskSizing:
    def test_ac_01_sizing_formula_and_decimal(self) -> None:
        # qty = floor(risk_amount / (stop_distance * multiplier) / lot) * lot.
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        sizing = buy.sizing
        assert sizing is not None and sizing.approved
        expected_raw = Decimal("1000") / (Decimal("480") * MULT)  # 2.0833...
        assert sizing.quantity == int(expected_raw // Decimal("1")) * 1  # floor to lot 1 -> 2
        assert sizing.quantity == 2
        assert isinstance(sizing.risk_amount, Decimal)
        assert isinstance(sizing.stop_distance, Decimal)
        assert isinstance(sizing.stop_price, Decimal)
        assert sizing.stop_distance == Decimal("480")
        assert sizing.stop_price == Decimal("23520")

    def test_ac_02_equity_basis_is_current_not_initial(self) -> None:
        # risk_amount = 1% of *current* equity at the decision close.
        settings = _settings(max_daily_loss=Decimal("100000"))
        closes = [ENTRY] * 16
        closes[9] = Decimal("20000")  # a -8000 round trip, then re-enter
        bars = [_bar(i, close=closes[i]) for i in range(16)]
        session = _make_session(
            bars,
            settings=settings,
            plan={2: Signal.BUY, 9: Signal.SELL, 13: Signal.BUY},
        )
        result = session.poll(when=_ts(13, BASE) + timedelta(minutes=5))
        buys = _buying_steps(result)
        assert [s.sizing.risk_amount for s in buys] == [Decimal("1000"), Decimal("920")]
        # 2 lots at 100k equity; after -8000 realized, only 1 lot at 92k equity.
        assert [s.sizing.quantity for s in buys] == [2, 1]
        assert session.portfolio.realized_pnl == Decimal("-8000")  # closed SELL only; re-entry is unrealized
        assert session.portfolio.initial_cash == Decimal("100000")

    def test_ac_03_below_lot_skip_is_recorded(self) -> None:
        settings = _settings(initial_capital=Decimal("10000"))  # risk 100 < 480 -> qty 0
        bars = _bars(8)
        session = _make_session(bars, settings=settings, plan={2: Signal.BUY})
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        assert buy.sizing is not None and not buy.sizing.approved
        assert "below the minimum quantity increment" in buy.sizing.skip_reason
        assert buy.skipped and "sizing rejected" in buy.skip_reason
        assert session.orders_submitted == 0
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0

    def test_ac_04_notional_never_exceeds_available_cash(self) -> None:
        # A large fixed commission leaves the risk-sized 2 lots unaffordable, so
        # the cash bound reduces the entry to the largest affordable lot.
        settings = _settings(commission_fixed=Decimal("60000"))
        bars = _bars(8)
        session = _make_session(bars, settings=settings, plan={2: Signal.BUY})
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        sizing = buy.sizing
        assert sizing is not None and sizing.approved
        assert sizing.quantity == 1  # reduced from the risk-limited 2
        entry_notional = sizing.quantity * ENTRY * MULT
        estimated_commission = sizing.quantity * ENTRY * MULT * Decimal("0.0003") + Decimal("60000")
        assert entry_notional + estimated_commission <= session.portfolio.initial_cash
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 1

    def test_ac_05_position_and_risk_stable_over_the_trade(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY, 5: Signal.BUY})
        result = session.poll(when=_ts(5, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY and step.order_result is not None)
        assert buy.sizing is not None and buy.sizing.quantity == 2
        later = next(step for step in result.steps if step.skipped and "already long" in step.skip_reason)
        assert later.skip_reason == "already long; BUY ignored"
        assert session.orders_submitted == 1  # no re-size mid-position
        position = session.portfolio.position_for("NIFTY_INDEX")
        assert position is not None and position.quantity == 2
        # Risk fixed at entry: the stop stays 2% below the 24000 entry.
        assert session.stop_policy.stop_price(position) == STOP

    def test_ac_06_rounding_never_exceeds_the_risk_budget(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(step for step in result.steps if step.signal is Signal.BUY)
        sizing = buy.sizing
        assert sizing is not None and sizing.approved
        assert sizing.quantity * sizing.stop_distance * MULT <= sizing.risk_amount
        assert sizing.quantity * sizing.stop_distance * MULT == Decimal("960")  # 2 * 480


# ---------------------------------------------------------------------------
# AC 7-12 — Stop-loss
# ---------------------------------------------------------------------------


class TestAcceptanceStopLoss:
    def test_ac_07_stop_anchored_two_percent_below_entry(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        position = session.portfolio.position_for("NIFTY_INDEX")
        assert position is not None and position.average_entry_price == ENTRY
        assert session.stop_policy.stop_price(position) == ENTRY * Decimal("0.98") == STOP

    def test_ac_08_breach_forces_paper_exit_fill_and_trade(self) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        stop = result.steps[3]
        assert stop.stop_result is not None
        assert stop.stop_result.order.order_type == OrderType.STOP
        assert stop.stop_result.order.side == OrderSide.SELL
        fill = stop.stop_result.fill
        trade = stop.stop_result.trade
        assert fill is not None and trade is not None
        assert fill in session._trading.broker.fills
        assert trade in session.portfolio.trade_history
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        # The exit is a paper order through PaperBroker -> Portfolio.apply_fill
        # (protective exits deliberately bypass the RiskManager; see PROGRESS.md).

    def test_ac_09_stop_trade_records_timestamp_price_commission_pnl(self) -> None:
        settings = _settings(slippage_rate=Decimal("0.001"), commission_rate=Decimal("0.0003"))
        bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
        ]
        session = _make_session(bars, settings=settings, plan={2: Signal.BUY}, clock=lambda: FILL_CLOCK)
        result = session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        stop = result.steps[3].stop_result
        assert stop is not None and stop.fill is not None and stop.trade is not None
        entry_fill = result.steps[2].order_result.fill
        assert entry_fill is not None
        avg_entry = entry_fill.price  # 24000 * 1.001 = 24024 (entry slippage)
        assert avg_entry == ENTRY * Decimal("1.001")
        stop_ref = avg_entry * Decimal("0.98")  # 23543.52
        expected_exit_price = stop_ref * Decimal("0.999")  # SELL slippage
        assert stop.fill.price == expected_exit_price == Decimal("23519.97648")
        trade: Trade = stop.trade
        assert trade.executed_at == FILL_CLOCK == stop.fill.filled_at
        assert trade.price == stop.fill.price
        assert trade.commission == stop.fill.commission == Decimal("14.111985888")
        expected_realized = (stop.fill.price - avg_entry) * trade.quantity * MULT
        assert trade.realized_pnl == expected_realized
        for value in (trade.price, trade.commission, trade.realized_pnl, stop.fill.price):
            assert isinstance(value, Decimal)

    def test_ac_10_flat_after_stop_then_reentry_allowed(self) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
            _bar(5),
            _bar(6),
            _bar(7),
        ]
        session = _make_session(bars, plan={2: Signal.BUY, 7: Signal.BUY})
        result = session.poll(when=_ts(7, BASE) + timedelta(minutes=5))
        assert result.steps[3].stop_result is not None
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2  # re-entered
        assert session.trades == 3  # BUY, stop exit, BUY
        assert len(session._entry_candle) == 1  # re-entry candle recorded

    def test_ac_11_stop_never_leaves_a_short_position(self) -> None:
        bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        position = session.portfolio.positions.get("NIFTY_INDEX")
        assert position is None or position.quantity >= 0  # never negative

    def test_ac_12_stop_uses_worse_of_open_and_stop(self) -> None:
        # Intrabar breach (low <= stop, open > stop) -> exact stop price.
        bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
            _bar(4),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        stop = result.steps[3].stop_result
        assert stop is not None and stop.reference_price == STOP
        assert stop.fill.price == STOP
        # Gap / open-through (open <= stop) -> exit at the candle open.
        gap_bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="23400", close="23400"),
            _bar(4),
        ]
        session = _make_session(gap_bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(4, BASE) + timedelta(minutes=5))
        stop = result.steps[3].stop_result
        assert stop is not None and stop.reference_price == Decimal("23400")
        assert stop.fill.price == Decimal("23400")


# ---------------------------------------------------------------------------
# AC 13-14 — Long-only behavior
# ---------------------------------------------------------------------------


class TestAcceptanceLongOnly:
    def test_ac_13_buy_opens_sell_closes_to_exactly_zero(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY, 4: Signal.BUY, 6: Signal.SELL})
        result = session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        assert result.steps[2].order_result.fill is not None
        assert "already long" in result.steps[4].skip_reason
        sell = result.steps[6]
        assert not sell.skipped and sell.order_result is not None
        assert sell.order_result.order.quantity == 2  # closes the full long
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0  # exactly zero
        assert session.portfolio.open_positions() == {}
        # A SELL on a flat position is ignored as well (short-to-open is rejected).
        flat_session = _make_session(_bars(8), plan={3: Signal.SELL})
        result = flat_session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        sell = next(s for s in result.steps if s.signal is Signal.SELL)
        assert sell.skipped and "flat" in sell.skip_reason
        assert flat_session.orders_submitted == 0

    def test_ac_14_hold_never_creates_an_order(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={})
        result = session.poll(when=_ts(7, BASE) + timedelta(minutes=5))
        assert result.consumed == 8
        assert all(not step.skipped for step in result.steps)
        assert session.orders_submitted == 0 and session.fills == 0 and session.trades == 0


# ---------------------------------------------------------------------------
# AC 15-18 — Completed-candle cadence
# ---------------------------------------------------------------------------


class TestAcceptanceCadence:
    def test_ac_15_forming_bar_is_never_traded(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={3: Signal.BUY})
        early = session.poll(when=datetime(2026, 9, 2, 9, 34))  # bar 3 completes 09:35
        assert early.consumed == 3
        assert session.orders_submitted == 0
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        ready = session.poll(when=datetime(2026, 9, 2, 9, 35))
        assert ready.consumed == 1
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 2

    def test_ac_16_one_decision_per_candle_and_identical_on_refetch(self) -> None:
        bars = _bars(8)
        first = _make_session(bars, plan={2: Signal.BUY})
        r1 = first.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        r2 = first.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        assert r1.consumed == 3 and r2.empty
        assert first.orders_submitted == 1
        identical = _make_session(bars, plan={2: Signal.BUY})
        identical.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        expected = [
            (f.side.value, str(f.price), f.quantity) for f in first._trading.broker.fills
        ]
        actual = [(f.side.value, str(f.price), f.quantity) for f in identical._trading.broker.fills]
        assert actual == expected

    def test_ac_17_no_order_before_twenty_two_completed_bars(self) -> None:
        bars = _bars(24)
        session = _make_session(bars, plan={5: Signal.BUY}, warmup_bars=22, use_default_sizer=False)
        result = session.poll(when=_ts(23, BASE) + timedelta(minutes=5))
        assert result.consumed == 24
        assert session.orders_submitted == 0
        assert session.skipped_candles == 21
        boundary = _make_session(bars, plan={21: Signal.BUY}, warmup_bars=22, use_default_sizer=False)
        result = boundary.poll(when=_ts(21, BASE) + timedelta(minutes=5))
        assert boundary.portfolio.current_quantity("NIFTY_INDEX") == 1

    def test_ac_18_no_trading_outside_open_phase_or_on_holidays(self) -> None:
        for start, when, reason in (
            (datetime(2026, 9, 5, 9, 15), datetime(2026, 9, 5, 12, 0), "not a trading day"),
            (datetime(2026, 4, 3, 9, 15), datetime(2026, 4, 3, 13, 0), "not a trading day"),
            (datetime(2026, 9, 2, 8, 45), datetime(2026, 9, 2, 9, 10), "outside the NSE OPEN phase"),
        ):
            bars = _bars(4, start=start)
            session = _make_session(bars, plan={1: Signal.BUY})
            result = session.poll(when=when)
            assert len(result.steps) == 4
            assert all(step.skipped for step in result.steps)
            assert all(reason in step.skip_reason for step in result.steps)
            assert session.orders_submitted == 0
            assert session.portfolio.open_positions() == {}


# ---------------------------------------------------------------------------
# AC 19-21 — Virtual capital and accounting
# ---------------------------------------------------------------------------


class TestAcceptanceCapitalAccounting:
    def test_ac_19_initial_equity_and_total_value_at_close(self) -> None:
        bars = [_bar(i, close=closes) for i, closes in enumerate(
            [ENTRY, ENTRY, ENTRY, Decimal("24100")])]
        session = _make_session(bars, plan={2: Signal.BUY})
        result = session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        assert session.portfolio.initial_cash == Decimal("100000")
        final = result.steps[-1]
        symbol = "NIFTY_INDEX"
        marketed = session.portfolio.total_value({symbol: Decimal("24100")})
        assert final.equity == marketed == Decimal("100200")  # cash 52000 + 2 * 24100
        assert isinstance(final.equity, Decimal)

    def test_ac_20_fills_change_cash_exactly_as_apply_fill_specifies(self) -> None:
        settings = _settings(commission_rate=Decimal("0.0003"))
        closes = [ENTRY] * 6 + [Decimal("24100")] + [ENTRY]
        bars = [_bar(i, close=closes[i]) for i in range(8)]
        session = _make_session(bars, settings=settings, plan={2: Signal.BUY, 6: Signal.SELL})
        session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        assert session.portfolio.cash == Decimal("100171.14")
        assert session.portfolio.realized_pnl == Decimal("200")
        assert session.portfolio.realized_pnl_today(today=BASE.date()) == Decimal("200")
        buy = session.portfolio.trade_history[0]
        assert buy.side == OrderSide.BUY and buy.commission == Decimal("14.4")
        sell = session.portfolio.trade_history[1]
        assert sell.side == OrderSide.SELL and sell.price == Decimal("24100")
        assert sell.commission == Decimal("14.46")
        for value in (session.portfolio.cash, buy.commission, sell.price, sell.realized_pnl):
            assert isinstance(value, Decimal)

    def test_ac_21_once_with_no_new_candle_changes_nothing(self) -> None:
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY})
        when = _ts(2, BASE) + timedelta(minutes=5)
        session.run_once(when=when)
        before = (session.orders_submitted, session.fills, session.trades, session.portfolio.cash)
        again = session.run_once(when=when)
        assert again.empty
        assert (session.orders_submitted, session.fills, session.trades, session.portfolio.cash) == before


# ---------------------------------------------------------------------------
# AC 22-23 — Deterministic behavior
# ---------------------------------------------------------------------------


class TestAcceptanceDeterminism:
    def test_ac_22_identical_replay_is_identical(self) -> None:
        def build() -> list[MarketPrice]:
            bars = _bars(12)
            broken = bars[3]
            bars[3] = _bar(
                3,
                open_="24000",
                high=str(broken.high),
                low="23400",
                close="24000",
            )
            bars[11] = _bar(11, close="24100")
            return bars

        def run(tag: str) -> tuple:
            session = _make_session(
                build(), plan={2: Signal.BUY, 7: Signal.BUY, 11: Signal.SELL}, clock=lambda: FILL_CLOCK
            )
            result = session.poll(when=_ts(11, BASE) + timedelta(minutes=5))
            fills = [(f.side.value, str(f.price), f.quantity) for f in session._trading.broker.fills]
            trades = [(t.side.value, str(t.price), str(t.realized_pnl)) for t in session.portfolio.trade_history]
            return (
                tag,
                fills,
                trades,
                str(session.portfolio.cash),
                str(session.portfolio.realized_pnl),
                str(result.steps[-1].equity),
                session.orders_submitted,
                session.fills,
                session.trades,
                session.rejections,
                session.skipped_candles,
            )

        assert run("a")[1:] == run("b")[1:]  # the tag itself is intentionally different
        assert run("a")[6:] == (4, 4, 4, 0, 0)  # BUY, stop, BUY, SELL; rejected none

    def test_ac_23_no_float_in_any_numeric_path(self) -> None:
        def build() -> list[MarketPrice]:
            bars = _bars(12)
            bars[3] = _bar(3, open_="24050", high="24080", low="23400", close="23900")
            bars[11] = _bar(11, close="24100")
            return bars

        session = _make_session(build(), plan={2: Signal.BUY, 7: Signal.BUY, 11: Signal.SELL})
        result = session.poll(when=_ts(11, BASE) + timedelta(minutes=5))
        for value in _all_recorded_money(session, result):
            assert isinstance(value, Decimal) and not isinstance(value, float)
            assert value.is_finite()


# ---------------------------------------------------------------------------
# AC 24-30 — Safety boundaries
# ---------------------------------------------------------------------------


class TestAcceptanceSafety:
    def test_ac_24_session_uses_only_the_paper_broker(self) -> None:
        session = _make_session(_bars(8))
        assert isinstance(session.broker, PaperBroker)
        assert session.broker.is_live is False

        class _DummyBroker(Broker):
            def place_order(self, order, market_price=None):
                return None

            def cancel_order(self, order_id):
                return None

            def get_order(self, order_id):
                return None

        with pytest.raises(RuntimeError, match="non-paper"):
            _make_session(_bars(8), broker=_DummyBroker())

    def test_ac_25_no_order_capable_vendor_surface(self) -> None:
        session = _make_session(_bars(8))
        assert not hasattr(session.provider, "place_order")
        assert not hasattr(InMemoryMarketDataProvider, "place_order")
        assert isinstance(session.broker, PaperBroker) and not session.broker.is_live

    def test_ac_26_no_credentials_logged_or_written(self, caplog) -> None:
        before = set(os.listdir("."))
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY, 6: Signal.SELL})
        with caplog.at_level(logging.DEBUG):
            session.poll(when=_ts(6, BASE) + timedelta(minutes=5))
        text = caplog.text.lower()
        for needle in ("access_token", "bearer ", "token=", "client_secret"):
            assert needle not in text
        assert set(os.listdir(".")) == before
        assert not Path("paper_state").exists()

    def test_ac_27_violations_are_rejected_or_skipped_with_reason(self) -> None:
        # SELL-to-open: ignored with a recorded reason.
        flat = _make_session(_bars(8), plan={3: Signal.SELL})
        result = flat.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        sell = next(s for s in result.steps if s.signal is Signal.SELL)
        assert sell.skipped and "flat" in sell.skip_reason
        assert flat.orders_submitted == 0
        # Notional exceeding the configured cap: rejected, never silently filled.
        bars = [_bar(i, close=Decimal("130000") if i == 2 else ENTRY) for i in range(8)]
        big = _make_session(bars, plan={2: Signal.BUY}, quantity=2, use_default_sizer=False)
        result = big.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(s for s in result.steps if s.signal is Signal.BUY)
        assert buy.order_result is not None and buy.order_result.fill is None
        assert "MAX_ORDER_NOTIONAL_EXCEEDED" in buy.order_result.order.rejection_reason
        assert big.rejections == 1 and big.portfolio.current_quantity("NIFTY_INDEX") == 0
        # Duplicate candle: never reprocessed (one order, one fill).
        session = _make_session(_bars(8), plan={2: Signal.BUY})
        session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        assert session.orders_submitted == 1 and session.fills == 1

    def test_ac_28_start_blocked_outside_paper_environment(self) -> None:
        blocked = _make_session(_bars(8), environment=Environment.DEVELOPMENT, started=False)
        with pytest.raises(EnvironmentError, match="PAPER"):
            blocked.start()
        assert not blocked.is_running
        sandbox = _make_session(
            _bars(8), environment=Environment.TEST, allow_sandbox=True, started=False
        )
        sandbox.start()
        assert sandbox.is_running

    def test_ac_29_risk_manager_limits_gate_and_rejected_never_fills(self) -> None:
        # max_position_quantity: 200 > 75 -> rejected, nothing filled.
        bars = _bars(8)
        session = _make_session(bars, plan={2: Signal.BUY}, quantity=200, use_default_sizer=False)
        result = session.poll(when=_ts(2, BASE) + timedelta(minutes=5))
        buy = next(s for s in result.steps if s.signal is Signal.BUY)
        assert buy.order_result is not None and buy.order_result.fill is None
        assert "MAX_POSITION_QUANTITY_EXCEEDED" in buy.order_result.order.rejection_reason
        assert session.rejections == 1
        assert session.portfolio.current_quantity("NIFTY_INDEX") == 0
        assert session.portfolio.cash == Decimal("100000")  # cash untouched
        # max_daily_loss: realized -2000 <= -1000 suspends entries with a reason.
        portfolio = Portfolio(Decimal("100000"), long_only=True)
        portfolio.cash = Decimal("98000")
        portfolio.realized_pnl = Decimal("-2000")
        portfolio.trade_history.append(
            Trade(
                trade_id="LOSS",
                instrument=_index(),
                side=OrderSide.SELL,
                quantity=1,
                price=Decimal("23800"),
                commission=Decimal("0"),
                executed_at=BASE,
                realized_pnl=Decimal("-2000"),
            )
        )
        session = _make_session(_bars(8), plan={1: Signal.BUY}, portfolio=portfolio, use_default_sizer=False)
        result = session.poll(when=_ts(1, BASE) + timedelta(minutes=5))
        buy = next(s for s in result.steps if s.signal is Signal.BUY)
        assert buy.skipped and "daily-loss limit reached" in buy.skip_reason
        assert session.orders_submitted == 0 and session.fills == 0

    def test_ac_30_persistence_confined_to_paper_state(self, tmp_path) -> None:
        # Snapshot artifacts land strictly in the configured directory.
        bars = [
            _bar(0),
            _bar(1),
            _bar(2),
            _bar(3, open_="24050", high="24080", low="23400", close="23900"),
        ]
        session = _make_session(bars, plan={2: Signal.BUY})
        session.poll(when=_ts(3, BASE) + timedelta(minutes=5))
        before_cwd = set(os.listdir("."))
        stored = save_session(session.snapshot(), directory=tmp_path, name="acceptance")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["acceptance.json", "acceptance.meta.json"]
        assert stored.path == tmp_path / "acceptance.json"
        assert stored.state_hash  # integrity hash present
        # Everything is loadable and restorable, all from within tmp_path.
        loaded = load_session(tmp_path / "acceptance.json")
        stopped = _make_session(bars, plan={2: Signal.BUY}, started=False)
        stopped.restore(loaded.snapshot)
        assert stopped.orders_submitted == session.orders_submitted
        assert stopped.portfolio.cash == session.portfolio.cash
        assert stopped.consumed_candles == session.consumed_candles
        # The runtime session itself writes nothing outside paper_state.
        assert set(os.listdir(".")) == before_cwd
        assert not Path("paper_state").exists()
        # paper_state/ is git-ignored.
        gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        assert "paper_state" in gitignore