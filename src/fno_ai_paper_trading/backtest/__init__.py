"""Backtesting layer — deterministic, paper-only.

Backtesting replays historical bars through a deterministic strategy, executes
signals through the paper broker only, and reports performance metrics. The
engine never places a live order and never requires network access or
credentials.
"""
from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult, EquityPoint

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "EquityPoint",
]