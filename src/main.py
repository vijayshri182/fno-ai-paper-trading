"""F&O AI Paper Trading System — Phase 1 & 2 entry point.

This module is intentionally kept minimal: it wires together the configuration,
data provider, strategy, paper broker, risk manager and portfolio, then runs two
small demonstrations — the Phase 1 paper-trading walkthrough and a Phase 2
strategy run (moving average crossover) over a deterministic bar series.

Running this module places ZERO real orders. The entire run is simulated, and
every order executes through the risk manager and the paper broker.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running via `python src/main.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from decimal import Decimal

from fno_ai_paper_trading.broker.paper_broker import PaperBroker, PaperBrokerConfig
from fno_ai_paper_trading.config.settings import load_settings
from fno_ai_paper_trading.data.mock_provider import InMemoryMarketDataProvider, build_crossing_ohlcv
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.portfolio.portfolio import Portfolio
from fno_ai_paper_trading.risk.manager import RiskManager
from fno_ai_paper_trading.services.strategy_service import StrategyService
from fno_ai_paper_trading.services.trading_service import TradingService
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy
from fno_ai_paper_trading.utils.logging import get_logger, setup_logging


def _settings() -> tuple:
    settings = load_settings()
    setup_logging(settings.log_level)
    return settings


def _demo() -> None:
    """Run a small paper-trading demonstration.  No real orders are placed."""
    settings = _settings()
    logger = get_logger(__name__)

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


def _strategy_demo() -> None:
    """Run a paper-only strategy demo over a deterministic bar series.

    The series (built by ``build_crossing_ohlcv``) forces a genuine fast/slow
    moving-average crossover in both directions, so the demo produces both a
    BUY and a SELL signal. Instruments are supplied by the sample plugin — no
    live instrument fetch and no network call happens here.
    """
    settings = _settings()
    logger = get_logger(__name__)

    provider = InMemoryMarketDataProvider()
    nifty_fut = provider.get_instruments()[0]
    bars = build_crossing_ohlcv(nifty_fut)

    broker = PaperBroker(
        PaperBrokerConfig(
            commission_rate=settings.commission_rate,
            commission_fixed=settings.commission_fixed,
            slippage_rate=settings.slippage_rate,
        )
    )
    portfolio = Portfolio(settings.initial_capital)
    risk_manager = RiskManager(settings)
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)
    service = StrategyService(
        settings, provider, risk_manager, broker, portfolio,
        strategy=strategy, quantity=10,
    )

    decisions = service.run(bars)
    actionable = [d for d in decisions if d.result.actionable]
    logger.info("strategy '%s' evaluated over %d bars", strategy.name, len(bars))
    for d in actionable:
        order = d.order_result.order if d.order_result else None
        status = order.status.value if order else "-"
        logger.info(
            "signal %-5s @ %s | approved=%s order=%s | %s",
            d.result.signal.value,
            d.result.timestamp,
            d.executed,
            status,
            d.result.reason,
        )

    market_prices = {i.symbol: provider.get_last_price(i) for i in provider.get_instruments()}
    logger.info("cash          : %s", portfolio.cash)
    logger.info("unrealized P&L: %s", portfolio.unrealized_pnl(market_prices))
    logger.info("total value   : %s", portfolio.total_value(market_prices))
    logger.info("Phase 2 strategy demo complete - all orders were paper only.")


def main() -> None:
    print("F&O AI Paper Trading System")
    _demo()
    print("")
    _strategy_demo()


if __name__ == "__main__":
    main()