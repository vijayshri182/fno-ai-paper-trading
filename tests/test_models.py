"""Tests for the models package.

Covers Instrument, Order, Fill, Position, Trade and MarketPrice creation,
validation and core properties.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, OrderStatus, OrderType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill, Order
from fno_ai_paper_trading.models.position import Position, Trade

# ---------------------------------------------------------------------------
# Fixtures — deterministic sample objects
# ---------------------------------------------------------------------------

def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _call() -> Instrument:
    return Instrument(
        symbol="NIFTY1_24500_CE",
        instrument_type=InstrumentType.OPTION_CE,
        underlying_symbol="NIFTY",
        expiry=date(2026, 12, 24),
        strike=Decimal("24500"),
        option_type="CE",
    )


def _bar(instrument: Instrument, close: Decimal = Decimal("24300")) -> MarketPrice:
    return MarketPrice(
        instrument=instrument,
        timestamp=datetime(2026, 9, 1, 9, 15),
        open=close - Decimal("20"),
        high=close + Decimal("30"),
        low=close - Decimal("40"),
        close=close,
        volume=1000,
    )


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

class TestInstrument:
    def test_future_display_name(self) -> None:
        assert _future().display_name() == "NIFTY1"

    def test_option_display_name(self) -> None:
        assert "24500 CE" in _call().display_name()

    def test_symbol_is_stripped(self) -> None:
        inst = Instrument(
            symbol="  NIFTY1  ",
            instrument_type=InstrumentType.FUTURE,
            underlying_symbol=" NIFTY ",
        )
        assert inst.symbol == "NIFTY1"
        assert inst.underlying_symbol == "NIFTY"

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            Instrument(symbol="", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFTY")

    def test_empty_underlying_raises(self) -> None:
        with pytest.raises(ValueError, match="underlying"):
            Instrument(symbol="X", instrument_type=InstrumentType.FUTURE, underlying_symbol="")

    def test_option_requires_expiry(self) -> None:
        with pytest.raises(ValueError, match="expiry"):
            Instrument(symbol="C1", instrument_type=InstrumentType.OPTION_CE,
                       underlying_symbol="NIFT", expiry=None, strike=100, option_type="CE")

    def test_option_requires_strike(self) -> None:
        with pytest.raises(ValueError, match="strike"):
            Instrument(symbol="C2", instrument_type=InstrumentType.OPTION_CE,
                       underlying_symbol="NIFT", expiry=date(2026, 12, 1), strike=None, option_type="CE")

    def test_option_requires_valid_option_type(self) -> None:
        with pytest.raises(ValueError, match="option_type"):
            Instrument(symbol="C3", instrument_type=InstrumentType.OPTION_CE,
                       underlying_symbol="NIFT", expiry=date(2026, 12, 1),
                       strike=Decimal("100"), option_type="XX")

    def test_negative_lot_raises(self) -> None:
        with pytest.raises(ValueError):
            Instrument(symbol="X", instrument_type=InstrumentType.FUTURE,
                       underlying_symbol="NIFT", lot_size=-1)

    def test_strike_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="strike"):
            Instrument(symbol="C4", instrument_type=InstrumentType.OPTION_PE,
                       underlying_symbol="NIFT", expiry=date(2026, 12, 1),
                       strike=Decimal("-50"), option_type="PE")

    def test_non_option_with_zero_multiplier_is_fine(self) -> None:
        inst = Instrument(symbol="F1", instrument_type=InstrumentType.FUTURE,
                          underlying_symbol="NIFT", multiplier=1)
        assert inst.multiplier == 1


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

class TestOrder:
    def test_order_quantities_positive(self) -> None:
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=10)
        assert order.quantity == 10
        assert order.is_open is True

    def test_order_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            Order(instrument=_future(), side=OrderSide.BUY, quantity=0)

    def test_order_status_pending_by_default(self) -> None:
        order = Order(instrument=_future(), side=OrderSide.SELL, quantity=5)
        assert order.status == OrderStatus.PENDING

    def test_is_filled(self) -> None:
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=1)
        order.status = OrderStatus.FILLED
        assert order.is_filled is True


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------

class TestFill:
    def test_fill_stores_fields(self) -> None:
        fill = Fill(
            order_id="ORD_1", instrument=_future(), side=OrderSide.BUY,
            quantity=10, price=Decimal("24200"), commission=Decimal("50"),
        )
        assert fill.notional == Decimal("242000")

    def test_fill_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            Fill(order_id="ORD_2", instrument=_future(), side=OrderSide.BUY,
                 quantity=0, price=Decimal("100"), commission=Decimal("0"))

    def test_fill_negative_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="price"):
            Fill(order_id="ORD_3", instrument=_future(), side=OrderSide.BUY,
                 quantity=1, price=Decimal("-5"), commission=Decimal("0"))


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

class TestPosition:
    def test_long_position(self) -> None:
        pos = Position(instrument=_future(), quantity=10, average_entry_price=Decimal("24200"))
        assert pos.is_long
        assert pos.is_flat is False
        assert pos.unrealized_pnl(Decimal("24300")) == Decimal("1000")

    def test_short_position(self) -> None:
        pos = Position(instrument=_future(), quantity=-5, average_entry_price=Decimal("24200"))
        assert pos.is_short
        assert pos.unrealized_pnl(Decimal("24100")) == Decimal("500")
        assert pos.unrealized_pnl(Decimal("24300")) == Decimal("-500")

    def test_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-zero"):
            Position(instrument=_future(), quantity=0, average_entry_price=Decimal("24200"))

    def test_market_value(self) -> None:
        pos = Position(instrument=_future(), quantity=5, average_entry_price=Decimal("24200"))
        assert pos.market_value(Decimal("24200")) == Decimal("121000")


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class TestTrade:
    def test_trade_stores_fields(self) -> None:
        trade = Trade(
            trade_id="TRD_1", instrument=_future(), side=OrderSide.BUY,
            quantity=10, price=Decimal("24200"), commission=Decimal("20"),
        )
        assert trade.notional == Decimal("242000")
        assert trade.realized_pnl == Decimal("0")

    def test_trade_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            Trade(trade_id="TRD_2", instrument=_future(), side=OrderSide.BUY,
                  quantity=0, price=Decimal("100"), commission=Decimal("0"))


# ---------------------------------------------------------------------------
# MarketPrice
# ---------------------------------------------------------------------------

class TestMarketPrice:
    def test_bar_stores_fields(self) -> None:
        bar = _bar(_future(), close=Decimal("24400"))
        assert bar.close == Decimal("24400")
        assert bar.volume == 1000

    def test_high_must_be_at_least_open(self) -> None:
        with pytest.raises(ValueError, match="high"):
            MarketPrice(
                instrument=_future(), timestamp=datetime.now(),
                open=Decimal("100"), high=Decimal("90"), low=Decimal("80"),
                close=Decimal("100"), volume=10,
            )

    def test_low_must_be_at_most_close(self) -> None:
        with pytest.raises(ValueError, match="low"):
            MarketPrice(
                instrument=_future(), timestamp=datetime.now(),
                open=Decimal("100"), high=Decimal("110"), low=Decimal("105"),
                close=Decimal("100"), volume=10,
            )

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="open"):
            MarketPrice(
                instrument=_future(), timestamp=datetime.now(),
                open=Decimal("-1"), high=Decimal("5"), low=Decimal("-2"),
                close=Decimal("2"), volume=10,
            )

    def test_negative_volume_rejected(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            MarketPrice(
                instrument=_future(), timestamp=datetime.now(),
                open=Decimal("10"), high=Decimal("15"), low=Decimal("5"),
                close=Decimal("12"), volume=-1,
            )