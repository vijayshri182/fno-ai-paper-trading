"""Engineered decision-time features from validated bars (deterministic)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from fno_ai_paper_trading.ai.decision import FeatureValue
from fno_ai_paper_trading.features.indicators import (
    close_return,
    mean_squared_return,
    rsi,
    sma,
    volatility_ratio,
)
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.utils.functions import non_negative_int, positive_int


def _freeze_features(features: Mapping[str, FeatureValue]) -> Mapping[str, FeatureValue]:
    """Immutable snapshot of the computed feature set (no floats allowed)."""
    copied: dict[str, FeatureValue] = {}
    for name, value in features.items():
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
            raise TypeError(
                f"feature {name!r} must be a Decimal, int, or str; floats are not supported"
            )
        copied[name] = value
    return MappingProxyType(copied)


@dataclass(frozen=True)
class BarsFeatures:
    """Features computed at a single decision point (the provided timestamp).

    ``features`` maps names to ``FeatureValue`` values exactly as accepted by the
    AI decision-support boundary. No feature ever uses information from bars
    after the decision timestamp.
    """

    timestamp: datetime
    bar_count: int
    features: Mapping[str, FeatureValue]

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        object.__setattr__(self, "bar_count", positive_int(self.bar_count, "bar_count"))
        object.__setattr__(self, "features", _freeze_features(self.features))

    def feature(self, name: str) -> FeatureValue | None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("feature name must be a non-empty string")
        return self.features.get(name)


class FeatureEngineer:
    """Stateless, deterministic feature computation over a bar prefix.

    ``compute(bars)`` uses every bar in the prefix up to and including the last
    one — exactly the information available at that bar's decision time. The
    same bars always produce the same features.
    """

    def __init__(
        self,
        *,
        fast: int = 5,
        slow: int = 21,
        rsi_period: int = 14,
        volatility_short: int = 10,
        volatility_long: int = 30,
        min_bars: int | None = None,
    ) -> None:
        self.fast = positive_int(fast, "fast")
        self.slow = positive_int(slow, "slow")
        self.rsi_period = positive_int(rsi_period, "rsi_period")
        self.volatility_short = positive_int(volatility_short, "volatility_short")
        self.volatility_long = positive_int(volatility_long, "volatility_long")
        self.min_bars = min_bars if min_bars is None else positive_int(min_bars, "min_bars")
        if self.fast >= self.slow:
            raise ValueError("fast period must be < slow period")

    @property
    def required_bars(self) -> int:
        """Bars needed for the core feature set (matches strategy warm-up)."""
        return self.min_bars if self.min_bars is not None else self.slow + 1

    def compute(self, bars: Sequence[MarketPrice]) -> BarsFeatures | None:
        """Compute features at the last bar. Returns ``None`` when data is
        insufficient (fewer than ``required_bars``)."""
        if len(bars) < self.required_bars:
            return None
        last = bars[-1]
        features: dict[str, FeatureValue] = {
            "bars": len(bars),
            "open": last.open,
            "high": last.high,
            "low": last.low,
            "close": last.close,
            "volume": last.volume,
        }
        fast_ma = sma(bars, self.fast)
        slow_ma = sma(bars, self.slow)
        if fast_ma is not None:
            features["fast_ma"] = fast_ma
        if slow_ma is not None:
            features["slow_ma"] = slow_ma
        if fast_ma is not None and slow_ma is not None:
            if slow_ma != 0:
                features["ma_gap_pct"] = ((fast_ma - slow_ma) / slow_ma) * Decimal("100")
        return_1 = close_return(bars, 1)
        return_5 = close_return(bars, 5)
        if return_1 is not None:
            features["close_return_1"] = return_1
        if return_5 is not None:
            features["close_return_5"] = return_5
        rsi_value = rsi(bars, self.rsi_period)
        if rsi_value is not None:
            features["rsi"] = rsi_value
        short_vol = mean_squared_return(bars, self.volatility_short)
        long_vol = mean_squared_return(bars, self.volatility_long)
        if short_vol is not None:
            features["volatility_short"] = short_vol
        if long_vol is not None:
            features["volatility_long"] = long_vol
        ratio = volatility_ratio(
            bars, short=self.volatility_short, long=self.volatility_long
        )
        if ratio is not None:
            features["volatility_ratio"] = ratio
        return BarsFeatures(
            timestamp=last.timestamp,
            bar_count=len(bars),
            features=_freeze_features(features),
        )

    def compute_prefix(self, bars: Sequence[MarketPrice], index: int) -> BarsFeatures | None:
        """Compute features as seen at bar ``index`` of ``bars`` (inclusive).

        This deliberately feeds only ``bars[:index+1]`` to ``compute`` so a
        decision at ``index`` can never see future bars.
        """
        if index < 0 or index >= len(bars):
            raise IndexError("index out of range")
        return self.compute(bars[: index + 1])