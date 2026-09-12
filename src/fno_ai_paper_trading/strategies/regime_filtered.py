"""Regime-filtered moving-average crossover challenger (WS 7.11).

A *challenger* candidate only: it wraps the frozen MA(5,21) crossover and
suppresses BUY entries when the market regime at the decision bar is not in the
allowed trend set (e.g. suppress entries while the fast/slow gap is flat or
falling). SELL exits and HOLD pass through unchanged, so the challenger can
never widen an open risk state relative to the baseline — it can only stay flat
longer. The frozen baseline MA(5,21) itself is never modified.

Discipline mirrors the detector: the regime is classified from the same
decision-time feature prefix (``RegimeDetector.detect_prefix``) that the AI
decision-support boundary consumes, so a decision at bar *i* never uses bars
after *i*. This challenger is evaluation-only; nothing in this module places
orders or changes risk controls.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.regime.detector import RegimeDetector, TrendState
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy
from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_int


class RegimeFilteredMovingAverageCross(Strategy):
    """MA(5,21) crossover gated by an allowed-trend regime filter.

    BUY signals from the underlying crossover are rejected when the regime trend
    at the decision bar is not one of ``allowed_trends``. SELL and HOLD signals
    pass through untouched. Fully deterministic and stateless.
    """

    name = "regime_filtered_ma_cross"

    def __init__(
        self,
        fast: int = 5,
        slow: int = 21,
        trend_threshold_pct: int | float | str | Decimal = "0.05",
        allowed_trends: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        positive_int(fast, "fast")
        positive_int(slow, "slow")
        if fast >= slow:
            raise ValueError("fast period must be shorter than slow period")
        self.fast = fast
        self.slow = slow
        self.trend_threshold_pct = non_negative_decimal(
            trend_threshold_pct, "trend_threshold_pct"
        )
        allowed = allowed_trends if allowed_trends is not None else ("UP",)
        if not allowed:
            raise ValueError("allowed_trends must not be empty")
        states: list[TrendState] = []
        for item in allowed:
            if not isinstance(item, str) or not item.strip():
                raise TypeError("allowed_trends entries must be non-empty strings")
            states.append(TrendState(item.upper()))
        self.allowed_trends = frozenset(states)
        self._base = MovingAverageCrossStrategy(fast=fast, slow=slow)
        self._detector = RegimeDetector(
            fast=fast, slow=slow, trend_threshold_pct=self.trend_threshold_pct
        )

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        result = self._base.analyze(bars)
        if not bars:
            return result

        regime = self._detector.detect_prefix(bars, len(bars) - 1)
        regime_meta: dict[str, object] = {
            "regime_label": regime.label if regime is not None else "unknown",
            "regime_trend": regime.trend.value if regime is not None else "unknown",
            "regime_volatility": (
                regime.volatility.value if regime is not None else "unknown"
            ),
            "regime_filtered": False,
        }
        if result.signal is Signal.BUY and (
            regime is None or regime.trend not in self.allowed_trends
        ):
            return SignalResult(
                signal=Signal.HOLD,
                instrument=result.instrument,
                timestamp=result.timestamp,
                reason=(
                    f"crossover BUY suppressed: regime trend "
                    f"{regime_meta['regime_trend']} not in allowed "
                    f"{sorted(t.value for t in self.allowed_trends)}"
                ),
                meta={**result.meta, **regime_meta, "regime_filtered": True},
            )
        return SignalResult(
            signal=result.signal,
            instrument=result.instrument,
            timestamp=result.timestamp,
            reason=result.reason,
            meta={**result.meta, **regime_meta},
        )