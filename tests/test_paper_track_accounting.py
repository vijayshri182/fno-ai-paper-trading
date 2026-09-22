"""Phase 7 — independent accounting: derived P&L cross-checks the portfolio."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.paper_track.accounting import (
    gross_pnl,
    max_drawdown,
    net_pnl,
    total_commission,
    verify_accounting,
)
from fno_ai_paper_trading.utils.functions import new_id
from tests.paper_track_testkit import DAY0, day_ticks, drive, make_engine


def _trade(side, quantity, price, commission, realized: Decimal) -> Trade:
    return Trade(
        trade_id=new_id("TRD"),
        instrument=make_engine_instrument(),
        side=side,
        quantity=quantity,
        price=Decimal(str(price)),
        commission=Decimal(str(commission)),
        executed_at=datetime(2026, 9, 21, 12, 0),
        realized_pnl=Decimal(str(realized)),
    )


def _fill(side, quantity, price, commission) -> Fill:
    return Fill(
        order_id=new_id("ORD"),
        instrument=make_engine_instrument(),
        side=side,
        quantity=quantity,
        price=Decimal(str(price)),
        commission=Decimal(str(commission)),
        filled_at=datetime(2026, 9, 21, 12, 5),
    )


def make_engine_instrument():
    from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT

    return TRACK_INSTRUMENT()


def test_pnl_helpers():
    buys = [_trade(OrderSide.BUY, 2, Decimal(25000), Decimal("15.00"), Decimal("0")),
            _trade(OrderSide.BUY, 2, Decimal(25000), Decimal("15.00"), Decimal("0"))]
    sells = [_trade(OrderSide.SELL, 2, Decimal(25100), Decimal("15.06"), Decimal("199.88"))]
    fills = [_fill(OrderSide.BUY, 2, Decimal(25000), Decimal("15.00")),
             _fill(OrderSide.BUY, 2, Decimal(25000), Decimal("15.00")),
             _fill(OrderSide.SELL, 2, Decimal(25100), Decimal("15.06"))]
    assert gross_pnl(buys + sells) == Decimal("199.88")
    assert total_commission(fills) == Decimal("45.06")
    assert net_pnl(buys + sells, fills) == Decimal("199.88") - Decimal("45.06")


def test_max_drawdown_edge_cases():
    assert max_drawdown([]) == Decimal("0")
    assert max_drawdown([Decimal("100000")]) == Decimal("0")
    rising = [Decimal(x) for x in (100, 101, 102, 103)]
    assert max_drawdown(rising) == Decimal("0")
    with_dip = [Decimal(x) for x in (100, 110, 90, 120, 100)]
    assert max_drawdown(with_dip) == Decimal("20")
    spread = [Decimal(x) for x in (100, 120, 95, 130, 90)]
    assert max_drawdown(spread) == Decimal("40")


def test_verify_accounting_matches_portfolio(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    snapshot = verify_accounting(
        trades=engine.portfolio.trade_history,
        fills=engine.broker.fills,
        initial_cash=engine.portfolio.initial_cash,
        cash=engine.portfolio.cash,
        realized_pnl=engine.portfolio.realized_pnl,
        flat=True,
    )
    assert snapshot.consistent, snapshot.violations
    assert snapshot.cash_consistent
    assert snapshot.net_pnl == snapshot.gross_pnl - snapshot.total_commission
    assert snapshot.cash == engine.portfolio.cash


def test_cash_identity_after_profitable_day(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    expected = engine.portfolio.initial_cash + gross_pnl(engine.portfolio.trade_history) - total_commission(engine.broker.fills)
    assert engine.portfolio.cash == expected


def test_cash_identity_after_losing_day(tmp_path):
    from fno_ai_paper_trading.models.enums import Signal
    from tests.paper_track_testkit import closes_feed
    from tests.test_paper_track_orders import CycleStrategy

    strat = CycleStrategy(plan={2: Signal.BUY, 6: Signal.SELL})
    closes = [25000, 25020, 25050, 25040, 24980, 24700, 24500, 24300] + [24300] * 67

    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=strat)
    engine.bars_source = closes_feed(closes)
    drive(engine, day_ticks(DAY0))
    assert engine.portfolio.realized_pnl < 0
    expected = engine.portfolio.initial_cash + gross_pnl(engine.portfolio.trade_history) - total_commission(engine.broker.fills)
    assert engine.portfolio.cash == expected


def test_floating_state_skips_cash_identity_demand(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    start = cnt = 0
    for tick in day_ticks(DAY0)[2:]:
        if engine.position_quantity > 0:
            snapshot = verify_accounting(
                trades=engine.portfolio.trade_history,
                fills=engine.broker.fills,
                initial_cash=engine.portfolio.initial_cash,
                cash=engine.portfolio.cash,
                realized_pnl=engine.portfolio.realized_pnl,
                flat=False,
            )
            assert snapshot.consistent  # no cash identity demanded mid-position
            assert snapshot.realized_pnl == gross_pnl(engine.portfolio.trade_history)
            break
        engine.clock.set(tick)
        engine.step()


def test_tampered_realized_pnl_flagged_by_verifier(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    snapshot = verify_accounting(
        trades=engine.portfolio.trade_history,
        fills=engine.broker.fills,
        initial_cash=engine.portfolio.initial_cash,
        cash=engine.portfolio.cash,
        realized_pnl=engine.portfolio.realized_pnl + Decimal("1"),
        flat=True,
    )
    assert not snapshot.consistent
    assert snapshot.violations


def test_engine_max_drawdown_matches_helper(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    assert engine.max_intraday_drawdown == max_drawdown(engine.equity_curve)
    assert engine.max_intraday_drawdown >= Decimal("0")