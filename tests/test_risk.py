"""Tests for the risk manager.

Verifies approval/rejection against position quantity limits, notional limits,
daily-loss limits and unknown instrument.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.config.settings import PaperSettings, Environment
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskDecision, RiskManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides) -> PaperSettings:
    defaults = dict(
        environment=Environment.PAPER,
        initial_capital=Decimal("100000"),
        max_position_quantity=10,
        max_order_notional=Decimal("200000"),
        max_daily_loss=Decimal("5000"),
        commission_rate=Decimal("0"),
        commission_fixed=Decimal("0"),
        slippage_rate=Decimal("0"),
    )
    defaults.update(overrides)
    return PaperSettings(**defaults)


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _risk(**overrides) -> RiskManager:
    return RiskManager(_settings(**overrides))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRiskManager:
    def test_approved_when_within_all_limits(self) -> None:
        manager = _risk()
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=5)
        portfolio = Portfolio(Decimal("100000"))
        decision = manager.evaluate(order, portfolio, fill_price=Decimal("24200"))
        assert decision.approved is True

    def test_rejects_unknown_instrument_symbol(self) -> None:
        manager = _risk()
        valid = Instrument(symbol="GOOD", instrument_type=InstrumentType.FUTURE, underlying_symbol="NIFT")
        # Force a blank symbol after construction to exercise the risk manager's defensive check.
        object.__setattr__(valid, "symbol", "  ")
        order = Order(instrument=valid, side=OrderSide.BUY, quantity=1)
        portfolio = Portfolio(Decimal("100000"))
        decision = manager.evaluate(order, portfolio, fill_price=Decimal("100"))
        assert decision.approved is False
        assert any("UNKNOWN_INSTRUMENT" in r for r in decision.reasons)

    def test_rejects_quantity_exceeding_limit(self) -> None:
        manager = _risk()
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=11)  # max 10
        portfolio = Portfolio(Decimal("100000"))
        decision = manager.evaluate(order, portfolio, fill_price=Decimal("24200"))
        assert decision.approved is False
        assert any("MAX_POSITION_QUANTITY" in r for r in decision.reasons)

    def test_rejects_notional_exceeding_limit(self) -> None:
        manager = _risk()
        # notional = 5 * 50000 * 1 = 250000 > 200000
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=5)
        portfolio = Portfolio(Decimal("100000"))
        decision = manager.evaluate(order, portfolio, fill_price=Decimal("50000"))
        assert decision.approved is False
        assert any("MAX_ORDER_NOTIONAL" in r for r in decision.reasons)

    def test_rejects_when_daily_loss_reached(self) -> None:
        manager = _risk()
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=1)
        portfolio = Portfolio(Decimal("100000"))
        # Daily loss = 5000 (matches limit) — should be rejected.
        decision = manager.evaluate(
            order, portfolio, fill_price=Decimal("24200"),
            realized_today=Decimal("-5000"),
        )
        assert decision.approved is False
        assert any("DAILY_LOSS_LIMIT" in r for r in decision.reasons)

    def test_approved_when_daily_loss_not_yet_reached(self) -> None:
        manager = _risk()
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=1)
        portfolio = Portfolio(Decimal("100000"))
        decision = manager.evaluate(
            order, portfolio, fill_price=Decimal("24200"),
            realized_today=Decimal("-4999"),
        )
        assert decision.approved is True

    def test_multiple_rejections_collected(self) -> None:
        manager = _risk(max_position_quantity=1, max_order_notional=Decimal("100"))
        order = Order(instrument=_future(), side=OrderSide.BUY, quantity=2)
        portfolio = Portfolio(Decimal("100000"))
        decision = manager.evaluate(order, portfolio, fill_price=Decimal("200"))
        # quantity 2 > max 1, notional 400 > max 100
        assert decision.approved is False
        assert len(decision.reasons) >= 2

    def test_decision_summary(self) -> None:
        decision = RiskDecision(approved=True)
        assert decision.summary == "approved"
        decision_rej = RiskDecision(approved=False, reasons=["A", "B"])
        assert "A" in decision_rej.summary
        assert "B" in decision_rej.summary