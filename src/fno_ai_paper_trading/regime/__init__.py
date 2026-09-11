"""Market regime detection (WS 7.3).

Regime detection classifies validated bars into trend (up/down/sideways) and
volatility (low/normal/high) bands using deterministic, decision-time features.
Regimes are descriptive only — no trade, risk or execution decision is made
inside this package.
"""

from fno_ai_paper_trading.regime.detector import (
    MarketRegime,
    RegimeDetector,
    TrendState,
    VolatilityState,
)

__all__ = ["MarketRegime", "RegimeDetector", "TrendState", "VolatilityState"]