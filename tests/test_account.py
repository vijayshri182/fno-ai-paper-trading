"""Tests for the PaperAccount virtual-account model and long-only guard.

Verifies that a V1 paper account starts from configured capital, delegates
accounting to its embedded Portfolio and rejects any fill that would create a
short position.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.portfolio.account import PaperAccount
from fno_ai_paper_trading.portfolio.portfolio import Portfolio


def _nifty() -> Instrument:
    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


def _fill(
    side: OrderSide,
    quantity: int,
    price: Decimal,
    commission: Decimal = Decimal("0"),
    instrument: Instrument | None = None,
) -> Fill:
    return Fill(
        order_id="ORD_TEST",
        instrument=instrument or _nifty(),
        side=side,
        quantity=quantity,
        price=price,
        commission=commission,
    )


CAPITAL = Decimal("100000")


# -----------------------------------------------------------------------------
# PaperAccount construction
# -----------------------------------------------------------------------------

class TestPaperAccountConstruction:
    def test_valid_construction(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        assert account.account_id == "ACC_1"
        assert account.initial_capital == CAPITAL
        assert account.cash == CAPITAL
        assert account.portfolio.initial_cash == CAPITAL

    def test_long_only_is_default(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        assert account.long_only is True
        assert account.portfolio.long_only is True

    def test_can_disable_long_only(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL, long_only=False)
        assert account.long_only is False
        assert account.portfolio.long_only is False

    def test_empty_account_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="account_id"):
            PaperAccount(account_id="   ", initial_capital=CAPITAL)

    def test_zero_initial_capital_rejected(self) -> None:
        with pytest.raises(ValueError, match="initial_capital"):
            PaperAccount(account_id="ACC_2", initial_capital=Decimal("0"))

    def test_negative_initial_capital_rejected(self) -> None:
        with pytest.raises(ValueError, match="initial_capital"):
            PaperAccount(account_id="ACC_2", initial_capital=Decimal("-1000"))

    def test_account_id_is_stripped(self) -> None:
        account = PaperAccount(account_id="  ACC_3  ", initial_capital=CAPITAL)
        assert account.account_id == "ACC_3"


# -----------------------------------------------------------------------------
# Account state after fills
# -----------------------------------------------------------------------------

class TestPaperAccountFillUpdates:
    def test_buy_opens_long_and_reduces_cash(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 5, Decimal("24000")))
        assert account.current_quantity("Nifty 50") == 5
        assert account.cash == CAPITAL - Decimal("120000")

    def test_sell_closes_long_and_updates_realized_pnl(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 5, Decimal("24000")))
        account.apply_fill(_fill(OrderSide.SELL, 5, Decimal("24500")))
        assert account.current_quantity("Nifty 50") == 0
        assert account.positions == {}
        assert account.realized_pnl == Decimal("2500")
        assert len(account.portfolio.trade_history) == 2

    def test_equity_at_price(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 2, Decimal("24000")))
        # Cash 100000 - 48000 = 52000; market value 2 * 24500 = 49000.
        assert account.equity({"Nifty 50": Decimal("24500")}) == Decimal("101000")

    def test_tracks_open_position(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 3, Decimal("24000")))
        positions = account.positions
        assert len(positions) == 1
        assert positions["Nifty 50"].quantity == 3


# -----------------------------------------------------------------------------
# Long-only invariant
# -----------------------------------------------------------------------------

class TestLongOnlyInvariant:
    def test_sell_to_open_rejected(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        with pytest.raises(ValueError, match="long-only"):
            account.apply_fill(_fill(OrderSide.SELL, 5, Decimal("24000")))

    def test_oversell_rejected(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 3, Decimal("24000")))
        with pytest.raises(ValueError, match="long-only"):
            account.apply_fill(_fill(OrderSide.SELL, 5, Decimal("24500")))

    def test_exact_close_allowed(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 3, Decimal("24000")))
        account.apply_fill(_fill(OrderSide.SELL, 3, Decimal("24500")))
        assert account.current_quantity("Nifty 50") == 0
        assert account.realized_pnl == Decimal("1500")

    def test_add_to_long_allowed(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 2, Decimal("24000")))
        account.apply_fill(_fill(OrderSide.BUY, 3, Decimal("24100")))
        assert account.current_quantity("Nifty 50") == 5
        avg = account.positions["Nifty 50"].average_entry_price
        expected = (Decimal("24000") * 2 + Decimal("24100") * 3) / 5
        assert avg == expected

    def test_rejected_fill_does_not_corrupt_state(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        account.apply_fill(_fill(OrderSide.BUY, 3, Decimal("24000")))
        before_cash = account.cash
        before_trades = list(account.portfolio.trade_history)
        with pytest.raises(ValueError, match="long-only"):
            account.apply_fill(_fill(OrderSide.SELL, 10, Decimal("24500")))
        assert account.cash == before_cash
        assert account.portfolio.trade_history == before_trades
        assert account.current_quantity("Nifty 50") == 3

    def test_shorts_still_allowed_on_generic_portfolio(self) -> None:
        portfolio = Portfolio(CAPITAL)
        portfolio.apply_fill(_fill(OrderSide.SELL, 5, Decimal("24000")))
        assert portfolio.current_quantity("Nifty 50") == -5


# -----------------------------------------------------------------------------
# Integration with PaperBroker
# -----------------------------------------------------------------------------

class TestPaperAccountBrokerIntegration:
    def test_broker_fill_opens_long(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        broker = PaperBroker(PaperBrokerConfig())
        bar = MarketPrice(
            instrument=_nifty(),
            timestamp=datetime(2026, 9, 1, 9, 15),
            open=Decimal("23900"),
            high=Decimal("24200"),
            low=Decimal("23800"),
            close=Decimal("24100"),
            volume=1000,
        )
        from fno_ai_paper_trading.models.order import Order

        order = Order(instrument=_nifty(), side=OrderSide.BUY, quantity=2)
        fill = broker.place_order(order, bar)
        assert fill is not None
        account.apply_fill(fill)
        assert account.current_quantity("Nifty 50") == 2
        assert account.cash < CAPITAL

    def test_sell_order_from_broker_rejected_by_long_only_account(self) -> None:
        account = PaperAccount(account_id="ACC_1", initial_capital=CAPITAL)
        broker = PaperBroker(PaperBrokerConfig())
        bar = MarketPrice(
            instrument=_nifty(),
            timestamp=datetime(2026, 9, 1, 9, 15),
            open=Decimal("23900"),
            high=Decimal("24200"),
            low=Decimal("23800"),
            close=Decimal("24100"),
            volume=1000,
        )
        from fno_ai_paper_trading.models.order import Order

        order = Order(instrument=_nifty(), side=OrderSide.SELL, quantity=2)
        fill = broker.place_order(order, bar)
        assert fill is not None
        with pytest.raises(ValueError, match="long-only"):
            account.apply_fill(fill)
