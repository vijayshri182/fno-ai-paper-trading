"""Market regime detection over validated bars (deterministic, WS 7.3).

Regime classification is derived from the same decision-time features the AI
boundary consumes (``features.FeatureEngineer``), so a regime label for bar *i*
never uses bars after *i*. Regimes are descriptive, not prescriptive: a detected
regime does not itself place trades or change risk controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from fno_ai_paper_trading.ai.decision import FeatureValue
from fno_ai_paper_trading.features import BarsFeatures, FeatureEngineer
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_int


class TrendState(str, Enum):
    """Trend direction inferred from the MA fast/slow relationship."""

    UP = "UP"
    DOWN = "DOWN"
    SIDEWAYS = "SIDEWAYS"


class VolatilityState(str, Enum):
    """Volatility band inferred from the short/long variance ratio."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


@dataclass(frozen=True)
class MarketRegime:
    """A single deterministic regime classification at a point in time."""

    trend: TrendState
    volatility: VolatilityState
    timestamp: datetime
    bar_count: int
    features: Mapping[str, FeatureValue]

    @property
    def label(self) -> str:
        """Compact label, e.g. ``up_normal`` or ``sideways_high``."""
        return f"{self.trend.value.lower()}_{self.volatility.value.lower()}"

    def __post_init__(self) -> None:
        if not isinstance(self.trend, TrendState):
            raise TypeError("trend must be a TrendState")
        if not isinstance(self.volatility, VolatilityState):
            raise TypeError("volatility must be a VolatilityState")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        object.__setattr__(self, "bar_count", positive_int(self.bar_count, "bar_count"))
        copied: dict[str, FeatureValue] = dict(self.features)
        object.__setattr__(self, "features", MappingProxyType(copied))


class RegimeDetector:
    """Stateless, deterministic regime classifier over a bar prefix.

    Trend is a function of ``ma_gap_pct`` (fast vs slow SMA); volatility is a
    function of the short/long variance ratio. Thresholds are configurable and
    default to neutral, well-documented values.
    """

    def __init__(
        self,
        *,
        fast: int = 5,
        slow: int = 21,
        trend_threshold_pct: int | float | str | Decimal = "0.05",
        volatility_high: int | float | str | Decimal = "1.5",
        volatility_low: int | float | str | Decimal = "0.7",
        engineer: FeatureEngineer | None = None,
    ) -> None:
        positive_int(fast, "fast")
        positive_int(slow, "slow")
        self.trend_threshold_pct = non_negative_decimal(
            trend_threshold_pct, "trend_threshold_pct"
        )
        self.volatility_high = non_negative_decimal(volatility_high, "volatility_high")
        self.volatility_low = non_negative_decimal(volatility_low, "volatility_low")
        if self.volatility_high <= self.volatility_low:
            raise ValueError("volatility_high must be > volatility_low")
        self._engineer = engineer or FeatureEngineer(fast=fast, slow=slow)
        if self._engineer.fast != fast or self._engineer.slow != slow:
            raise ValueError("engineer fast/slow must match detector fast/slow")

    def detect(self, bars: Sequence[MarketPrice]) -> MarketRegime | None:
        """Classify the regime at the last bar. ``None`` when data is insufficient."""
        features = self._engineer.compute(bars)
        if features is None:
            return None
        return self._classify(features)

    def detect_prefix(
        self, bars: Sequence[MarketPrice], index: int
    ) -> MarketRegime | None:
        """Classify the regime as seen at bar ``index`` (never using later bars)."""
        if index < 0 or index >= len(bars):
            raise IndexError("index out of range")
        features = self._engineer.compute_prefix(bars, index)
        if features is None:
            return None
        return self._classify(features)

    def _classify(self, features: BarsFeatures) -> MarketRegime:
        values = features.features
        gap = values.get("ma_gap_pct")
        if isinstance(gap, Decimal):
            if gap > self.trend_threshold_pct:
                trend = TrendState.UP
            elif gap < -self.trend_threshold_pct:
                trend = TrendState.DOWN
            else:
                trend = TrendState.SIDEWAYS
        else:
            trend = TrendState.SIDEWAYS
        ratio = values.get("volatility_ratio")
        if isinstance(ratio, Decimal):
            if ratio > self.volatility_high:
                volatility = VolatilityState.HIGH
            elif ratio < self.volatility_low:
                volatility = VolatilityState.LOW
            else:
                volatility = VolatilityState.NORMAL
        else:
            volatility = VolatilityState.NORMAL
        return MarketRegime(
            trend=trend,
            volatility=volatility,
            timestamp=features.timestamp,
            bar_count=features.bar_count,
            features=values,
        )