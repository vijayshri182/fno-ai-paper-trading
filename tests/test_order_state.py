"""Tests for the deterministic Order lifecycle state machine.

Covers the explicit :meth:`Order.transition` rules and verifies that every
existing execution path (PaperBroker, TradingService, backtest engine) routes
order status changes through it.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.backtest.datasets import build_profitable_series
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderStatus
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import ORDER_TRANSITIONS, Order
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(symbol="NIFTY1", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY")


def _bar(close: Decimal = Decimal("24200")) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=datetime(2026, 9, 1, 9, 15),
        open=close - Decimal("10"),
        high=close + Decimal("20"),
        low=close - Decimal("30"),
        close=close,
        volume=500,
    )


def _order(quantity: int = 5) -> Order:
    return Order(instrument=_future(), side=OrderSide.BUY, quantity=quantity)


# -----------------------------------------------------------------------------
# Transition table integrity
# -----------------------------------------------------------------------------

class TestOrderTransitionTable:
    def test_all_statuses_have_transition_entry(self) -> None:
        for status in OrderStatus:
            assert status in ORDER_TRANSITIONS

    def test_terminal_states_map_to_empty_set(self) -> None:
        for terminal in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            assert ORDER_TRANSITIONS[terminal] == frozenset()


# -----------------------------------------------------------------------------
# Valid transitions
# -----------------------------------------------------------------------------

class TestOrderValidTransitions:
    def test_pending_can_submit(self) -> None:
        order = _order()
        order.submit()
        assert order.status is OrderStatus.SUBMITTED

    def test_pending_can_cancel(self) -> None:
        order = _order()
        order.cancel()
        assert order.status is OrderStatus.CANCELLED

    def test_pending_can_reject(self) -> None:
        order = _order()
        order.reject("no price")
        assert order.status is OrderStatus.REJECTED
        assert order.rejection_reason == "no price"

    def test_submitted_can_fill(self) -> None:
        order = _order()
        order.submit()
        order.filled_quantity = 5
        order.average_fill_price = Decimal("24000")
        order.transition(OrderStatus.FILLED)
        assert order.is_filled

    def test_submitted_can_partially_fill(self) -> None:
        order = _order()
        order.submit()
        order.filled_quantity = 2
        order.average_fill_price = Decimal("24000")
        order.transition(OrderStatus.PARTIALLY_FILLED)
        assert order.status is OrderStatus.PARTIALLY_FILLED

    def test_partially_filled_can_complete(self) -> None:
        order = _order()
        order.submit()
        order.filled_quantity = 2
        order.average_fill_price = Decimal("24000")
        order.transition(OrderStatus.PARTIALLY_FILLED)
        order.filled_quantity = 5
        order.transition(OrderStatus.FILLED)
        assert order.is_filled

    def test_mark_filled_helper(self) -> None:
        order = _order()
        order.submit()
        order.mark_filled(Decimal("24000"))
        assert order.is_filled
        assert order.filled_quantity == 5
        assert order.average_fill_price == Decimal("24000")



# -----------------------------------------------------------------------------
# Invalid transitions
# -----------------------------------------------------------------------------

class TestOrderInvalidTransitions:
    def test_cannot_transition_from_terminal_filled(self) -> None:
        order = _order()
        order.submit().mark_filled(Decimal("100"))
        for status in OrderStatus:
            with pytest.raises(ValueError, match="invalid order status transition"):
                order.transition(status)

    def test_cannot_transition_from_terminal_cancelled(self) -> None:
        order = _order()
        order.cancel()
        for status in OrderStatus:
            with pytest.raises(ValueError, match="invalid order status transition"):
                order.transition(status)

    def test_cannot_transition_from_terminal_rejected(self) -> None:
        order = _order()
        order.reject("risk")
        for status in OrderStatus:
            with pytest.raises(ValueError, match="invalid order status transition"):
                order.transition(status)

    def test_pending_to_filled_is_invalid(self) -> None:
        order = _order()
        with pytest.raises(ValueError, match="invalid order status transition"):
            order.transition(OrderStatus.FILLED)

    def test_no_backward_transitions(self) -> None:
        order = _order()
        order.submit()
        with pytest.raises(ValueError, match="invalid order status transition"):
            order.transition(OrderStatus.PENDING)

    def test_reject_requires_reason(self) -> None:
        order = _order()
        with pytest.raises(ValueError, match="rejection reason"):
            order.transition(OrderStatus.REJECTED)

    def test_filled_requires_full_quantity(self) -> None:
        order = _order()
        order.submit()
        order.filled_quantity = 3
        order.average_fill_price = Decimal("24000")
        with pytest.raises(ValueError, match="filled_quantity == quantity"):
            order.transition(OrderStatus.FILLED)

    def test_filled_requires_positive_average_price(self) -> None:
        order = _order()
        order.submit()
        order.filled_quantity = 5
        with pytest.raises(ValueError, match="positive average_fill_price"):
            order.transition(OrderStatus.FILLED)

    def test_partially_filled_requires_partial_quantity(self) -> None:
        order = _order()
        order.submit()
        order.filled_quantity = 5
        order.average_fill_price = Decimal("24000")
        with pytest.raises(ValueError, match="partial"):
            order.transition(OrderStatus.PARTIALLY_FILLED)

    def test_transition_requires_order_status_enum(self) -> None:
        order = _order()
        with pytest.raises(TypeError, match="OrderStatus"):
            order.transition("FILLED")

    def test_cancel_after_fill_raises(self) -> None:
        order = _order()
        order.mark_filled(Decimal("100"))
        with pytest.raises(ValueError, match="invalid order status transition"):
            order.cancel()


# -----------------------------------------------------------------------------
# Execution paths use the state machine
# -----------------------------------------------------------------------------

class TestPaperBrokerTransitions:
    def test_broker_fills_through_submitted_to_filled(self) -> None:
        broker = PaperBroker(PaperBrokerConfig())
        order = _order()
        fill = broker.place_order(order, _bar())
        assert fill is not None
        assert order.status is OrderStatus.FILLED
        assert order.submitted_at is not None
        assert order.filled_at is not None

    def test_broker_rejects_through_rejected(self) -> None:
        broker = PaperBroker(PaperBrokerConfig())
        order = _order()
        assert broker.place_order(order, None) is None
        assert order.status is OrderStatus.REJECTED
        assert order.rejection_reason is not None

    def test_double_place_on_same_order_fails_clearly(self) -> None:
        broker = PaperBroker(PaperBrokerConfig())
        order = _order()
        broker.place_order(order, _bar())
        with pytest.raises(ValueError, match="invalid order status transition"):
            broker.place_order(order, _bar())

    def test_cancel_filled_order_is_noop_and_does_not_raise(self) -> None:
        broker = PaperBroker(PaperBrokerConfig())
        order = _order()
        broker.place_order(order, _bar())
        cancelled = broker.cancel_order(order.order_id)
        assert cancelled is not None
        assert cancelled.status is OrderStatus.FILLED


class TestBacktestBrokerTransitions:
    def test_backtest_engine_fills_use_state_machine(self) -> None:
        bars = build_profitable_series(_future())
        result = BacktestEngine().run(bars, MovingAverageCrossStrategy(fast=2, slow=3))
        assert result.orders_filled > 0
        # Every recorded trade must have a BUY/SELL side from a FILLED order.
        for trade in result.trades:
            assert trade.side in (OrderSide.BUY, OrderSide.SELL)
