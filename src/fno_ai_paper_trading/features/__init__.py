"""Deterministic decision-time feature engineering (WS 7.2).

Features are pure, stateless, and Decimal-based. They are decision-time only:
a feature computed at bar *i* never uses data from bars after *i*. Feature sets
are directly consumable by the AI decision-support boundary
(``ai.DecisionContext.features``).
"""

from fno_ai_paper_trading.features.base import BarsFeatures, FeatureEngineer
from fno_ai_paper_trading.features.indicators import (
    close_return,
    mean_squared_return,
    rsi,
    sma,
    volatility_ratio,
)

__all__ = [
    "BarsFeatures",
    "FeatureEngineer",
    "close_return",
    "mean_squared_return",
    "rsi",
    "sma",
    "volatility_ratio",
]