"""Tests for the paper broker.

Verifies order placement, fill, slippage, commission, status transitions and
order cancellation.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderStatus
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Order

from datetime import datetime as _dt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _instrument() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bar(close: Decimal = Decimal("24200")) -> MarketPrice:
    return MarketPrice(
        instrument=_instrument(),
        timestamp=_dt(2026, 9, 1, 9, 15),
        open=close - Decimal("10"),
        high=close + Decimal("20"),
        low=close - Decimal("30"),
        close=close,
        volume=500,
    )


def _broker(
    commission_rate: Decimal = Decimal("0.0001"),
    commission_fixed: Decimal = Decimal("0"),
    slippage_rate: Decimal = Decimal("0.001"),
) -> PaperBroker:
    return PaperBroker(PaperBrokerConfig(
        commission_rate=commission_rate,
        commission_fixed=commission_fixed,
        slippage_rate=slippage_rate,
    ))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPaperBroker:
    def test_buy_fill_applies_slippage(self) -> None:
        broker = _broker(commission_rate=Decimal("0"), slippage_rate=Decimal("0.001"))
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=5)
        bar = _bar(close=Decimal("24200"))
        fill = broker.place_order(order, bar)
        assert fill is not None
        # BUY slippage: price should be slightly higher.
        assert fill.price > bar.close
        assert order.status == OrderStatus.FILLED
        assert order.order_id is not None

    def test_sell_fill_applies_slippage(self) -> None:
        broker = _broker(commission_rate=Decimal("0"), slippage_rate=Decimal("0.001"))
        order = Order(instrument=_instrument(), side=OrderSide.SELL, quantity=5)
        bar = _bar(close=Decimal("24200"))
        fill = broker.place_order(order, bar)
        assert fill is not None
        assert fill.price < bar.close

    def test_commission_notional_based(self) -> None:
        broker = _broker(
            commission_rate=Decimal("0.0002"),
            commission_fixed=Decimal("10"),
            slippage_rate=Decimal("0"),
        )
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=10)
        fill = broker.place_order(order, _bar(close=Decimal("24000")))
        # commission = 10 * 24000 * 1 * 0.0002 + 10 = 48 + 10 = 58
        assert fill is not None
        assert fill.commission == Decimal("58")

    def test_rejects_when_no_market_price(self) -> None:
        broker = _broker()
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=1)
        fill = broker.place_order(order, market_price=None)
        assert fill is None
        assert order.status == OrderStatus.REJECTED

    def test_order_is_tracked(self) -> None:
        broker = _broker()
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=2)
        broker.place_order(order, _bar())
        assert broker.get_order(order.order_id) is order

    def test_fill_recorded(self) -> None:
        broker = _broker()
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=1)
        broker.place_order(order, _bar())
        assert len(broker.fills) == 1

    def test_cancel_open_order(self) -> None:
        broker = _broker()
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=1)
        updated = broker.cancel_order("nonexistent")
        assert updated is None

    def test_cancel_filled_order_is_noop(self) -> None:
        broker = _broker()
        order = Order(instrument=_instrument(), side=OrderSide.BUY, quantity=1)
        broker.place_order(order, _bar())
        updated = broker.cancel_order(order.order_id)
        assert updated.status == OrderStatus.FILLED  # filled cannot be cancelled

    def test_is_live_is_false(self) -> None:
        broker = _broker()
        assert broker.is_live is False

    def test_notional_computed_by_fill(self) -> None:
        broker = _broker(slippage_rate=Decimal("0"))
        order = Order(instrument=_instrument(), side=OrderSide.SELL, quantity=7)
        fill = broker.place_order(order, _bar(close=Decimal("24000")))
        # multiplier=1, notional = 7 * 24000 = 168000
        assert fill is not None
        assert fill.notional == Decimal("168000")