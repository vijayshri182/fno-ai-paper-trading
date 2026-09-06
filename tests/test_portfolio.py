"""Tests for portfolio accounting.

Verifies cash changes, position tracking, average entry price, realized P&L
(long and short), unrealized P&L, total value, and today's realized P&L.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.portfolio.portfolio import Portfolio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1", instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY", lot_size=75, multiplier=1,
    )


def _call() -> Instrument:
    return Instrument(
        symbol="NIFTY1_24500_CE", instrument_type=InstrumentType.OPTION_CE,
        underlying_symbol="NIFTY", expiry=date(2026, 12, 24),
        strike=Decimal("24500"), option_type="CE", lot_size=75, multiplier=1,
    )


def _fill(
    instrument: Instrument,
    side: OrderSide,
    quantity: int,
    price: Decimal,
    commission: Decimal = Decimal("0"),
) -> Fill:
    return Fill(
        order_id="ORD_TEST",
        instrument=instrument,
        side=side,
        quantity=quantity,
        price=price,
        commission=commission,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPortfolio:
    def test_initial_cash(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        assert portfolio.cash == Decimal("100000")

    def test_initial_cash_requires_positive(self) -> None:
        with pytest.raises(ValueError):
            Portfolio(Decimal("0"))

    def test_buy_reduces_cash(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        fill = _fill(_future(), OrderSide.BUY, 10, Decimal("24000"))
        portfolio.apply_fill(fill)
        # 100000 - (10 * 24000 * 1) = 100000 - 240000 = -140000 (negative cash allowed in paper)
        assert portfolio.cash == Decimal("-140000")

    def test_buy_increases_cash_after_fee(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        fill = _fill(_future(), OrderSide.BUY, 1, Decimal("24000"), commission=Decimal("50"))
        portfolio.apply_fill(fill)
        assert portfolio.cash == Decimal("100000") - Decimal("24000") - Decimal("50")

    def test_sell_increases_cash(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        fill = _fill(_future(), OrderSide.SELL, 5, Decimal("24000"))
        portfolio.apply_fill(fill)
        assert portfolio.cash == Decimal("100000") + Decimal("120000")

    def test_sell_after_buy_realized_pnl_long(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 10, Decimal("24000")))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 10, Decimal("24500")))
        assert portfolio.realized_pnl == Decimal("5000")  # (24500-24000)*10

    def test_short_position_realized_pnl(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 5, Decimal("24200")))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 5, Decimal("23800")))
        assert portfolio.realized_pnl == Decimal("2000")  # (24200-23800)*5

    def test_partial_close_preserves_avg_entry(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 10, Decimal("24000")))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 4, Decimal("24500")))
        pos = portfolio.position_for("NIFTY1")
        assert pos is not None
        assert pos.quantity == 6
        assert pos.average_entry_price == Decimal("24000")  # unchanged

    def test_reversal_opens_new_direction(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 3, Decimal("24000")))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 5, Decimal("24500")))
        pos = portfolio.position_for("NIFTY1")
        assert pos is not None
        assert pos.quantity == -2
        assert pos.average_entry_price == Decimal("24500")

    def test_unrealized_pnl_long(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 10, Decimal("24000")))
        market_prices = {"NIFTY1": Decimal("24200")}
        assert portfolio.unrealized_pnl(market_prices) == Decimal("2000")

    def test_unrealized_pnl_short(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 5, Decimal("24200")))
        market_prices = {"NIFTY1": Decimal("24100")}
        assert portfolio.unrealized_pnl(market_prices) == Decimal("500")

    def test_market_value(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 5, Decimal("24000")))
        market_prices = {"NIFTY1": Decimal("24200")}
        assert portfolio.market_value(market_prices) == Decimal("121000")

    def test_total_value(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 10, Decimal("24000")))
        market_prices = {"NIFTY1": Decimal("24200")}
        assert portfolio.total_value(market_prices) == Decimal("-140000") + Decimal("242000")

    def test_empty_market_price_skips_position(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 5, Decimal("24000")))
        assert portfolio.unrealized_pnl({}) == Decimal("0")
        assert portfolio.market_value({}) == Decimal("0")

    def test_open_positions_only(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 10, Decimal("24000")))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 10, Decimal("24500")))
        assert len(portfolio.open_positions()) == 0

    def test_trade_history_recorded(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        portfolio.apply_fill(_fill(_future(), OrderSide.BUY, 10, Decimal("24000")))
        portfolio.apply_fill(_fill(_future(), OrderSide.SELL, 10, Decimal("24500")))
        assert len(portfolio.trade_history) == 2

    def test_realized_pnl_today_empty(self) -> None:
        portfolio = Portfolio(Decimal("100000"))
        assert portfolio.realized_pnl_today(date(2020, 1, 1)) == Decimal("0")