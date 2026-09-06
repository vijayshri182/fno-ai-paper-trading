"""F&O AI Paper Trading System — Phase 1 entry point.

This module is intentionally kept minimal: it wires together the
configuration, in-memory data provider, paper broker, risk manager and
portfolio, then demonstrates a small set of paper trades.

Running this module places ZERO real orders. The entire run is simulated.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running via `python src/main.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from decimal import Decimal

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import load_settings
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.services.trading_service import TradingService
from fno_ai_paper_trading.utils.logging import setup_logging


def _demo() -> None:
    """Run a small paper-trading demonstration.  No real orders are placed."""
    settings = load_settings()
    logger = setup_logging(settings.log_level)

    provider = InMemoryMarketDataProvider()
    broker = PaperBroker(
        PaperBrokerConfig(
            commission_rate=settings.commission_rate,
            commission_fixed=settings.commission_fixed,
            slippage_rate=settings.slippage_rate,
        )
    )
    portfolio = Portfolio(settings.initial_capital)
    risk_manager = RiskManager(settings)
    service = TradingService(settings, provider, risk_manager, broker, portfolio)

    instruments = provider.get_instruments()
    nifty_fut = instruments[0]
    nifty_ce = instruments[1]

    price = provider.get_last_price(nifty_fut)
    r1 = service.submit_order(nifty_fut, OrderSide.BUY, 10, reference_price=price)
    logger.info("decision: %s", r1.decision.summary)

    r2 = service.submit_order(nifty_ce, OrderSide.BUY, 5, reference_price=provider.get_last_price(nifty_ce))
    logger.info("decision: %s", r2.decision.summary)

    market_prices = {i.symbol: provider.get_last_price(i) for i in instruments}
    logger.info("cash          : %s", portfolio.cash)
    logger.info("unrealized P&L: %s", portfolio.unrealized_pnl(market_prices))
    logger.info("total value   : %s", portfolio.total_value(market_prices))

    logger.info("Phase 1 demo complete - all orders were paper only.")


def main() -> None:
    print("F&O AI Paper Trading System")
    _demo()


if __name__ == "__main__":
    main()
