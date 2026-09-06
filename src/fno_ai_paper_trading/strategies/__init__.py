"""Strategy layer — Phase 2.

Strategies are pure decision functions over price history. They never place
orders; the strategy service converts their signals into paper orders through
the risk manager and the paper broker.
"""
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.strategies.engine import StrategyEngine
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy

__all__ = [
    "Strategy",
    "SignalResult",
    "StrategyEngine",
    "MovingAverageCrossStrategy",
]