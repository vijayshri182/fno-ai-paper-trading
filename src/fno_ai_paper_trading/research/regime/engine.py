"""Phase 7 — deterministic NIFTY market-regime engine (provider-independent).

This module is the market-context layer that later phases' options strategies
may consume: given normalized ``MarketPrice`` 5m bars it produces a
deterministic, decision-time :class:`MarketRegimeReport` describing *market
context* — never a trading recommendation and never an order.

Reuse contract (no duplication):

* direction/volatility classification: ``regime.detector.RegimeDetector``
  (WS 7.3, ``features.FeatureEngineer``) — REUSED unchanged;
* ATR: ``strategies.indicators.atr`` (Wilder) — REUSED unchanged;
* session phase: ``data.market_hours.market_phase`` — REUSED unchanged;
* bar model: ``models.market.MarketPrice`` — REUSED unchanged.

What Phase 7 adds:

* an explicit data-quality tri-state (``VALID / UNAVAILABLE / INVALID``) and
  an ``INSUFFICIENT_DATA`` headline regime (never fabricated values);
* a documented warm-up contract table (per indicator) plus warm-up status;
* deterministic evidence/reason strings, feature snapshot, session context,
  calculation and schema versions — everything needed for later audit;
* ``MarketRegimeReport`` as the clean ``MarketRegime -> OptionsStrategy``
  input contract (selection/trading are out of scope by construction).

No-look-ahead: every evaluation at bar ``i`` operates only on ``bars[:i+1]``;
``evaluate_prefix`` is the only public decision entry point used by consumers
and mirrors ``FeatureEngineer.compute_prefix`` semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from fno_ai_paper_trading.ai.decision import FeatureValue
from fno_ai_paper_trading.data.market_hours import market_phase
from fno_ai_paper_trading.features import FeatureEngineer
from fno_ai_paper_trading.models.enums import MarketPhase
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.regime import MarketRegime, RegimeDetector, TrendState, VolatilityState
from fno_ai_paper_trading.strategies.indicators import atr
from fno_ai_paper_trading.utils.functions import positive_int

ENGINE_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"

_DECIMAL_100 = Decimal("100")


class Direction(str, Enum):
    """Consumer-facing direction state (mapped from TrendState)."""

    BULLISH = "BULLISH"  # TrendState.UP
    BEARISH = "BEARISH"  # TrendState.DOWN
    NEUTRAL = "NEUTRAL"  # TrendState.SIDEWAYS


class MarketDataState(str, Enum):
    """Data-quality tri-state of the decision-time bar window."""

    VALID = "VALID"  # window intact and long enough to decide
    UNAVAILABLE = "UNAVAILABLE"  # window intact but too short (warm-up)
    INVALID = "INVALID"  # window structurally broken (bad bars / timestamps)


class WarmupStatus(str, Enum):
    SATISFIED = "SATISFIED"
    INSUFFICIENT = "INSUFFICIENT"


INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class RegimeEvidence:
    """One deterministic audit line (dimension -> field -> value -> reason)."""

    dimension: str
    field: str
    value: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "dimension": self.dimension,
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
        }


def _freeze(features: Mapping[str, FeatureValue]) -> Mapping[str, FeatureValue]:
    copied: dict[str, FeatureValue] = {}
    for name, value in features.items():
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
            raise TypeError(
                f"regime feature {name!r} must be Decimal, int or str; got {type(value).__name__}"
            )
        copied[name] = value
    return MappingProxyType(copied)


@dataclass(frozen=True)
class MarketRegimeReport:
    """Deterministic market-context snapshot at one decision timestamp.

    ``regime`` is ``INSUFFICIENT_DATA`` when the data-quality gate fails,
    otherwise ``"<DIRECTION>_<VOLATILITY>"`` (e.g. ``BULLISH_HIGH``). This is a
    market description; it carries no recommendation and exposes no
    credentials. ``has_regime`` is the equivalent boolean for consumers.
    """

    timestamp: datetime | None
    regime: str
    direction: Direction | None
    trend: TrendState | None
    volatility: VolatilityState | None
    data_state: MarketDataState
    warmup_status: WarmupStatus
    required_bars: int
    available_bars: int
    bar_count: int
    features: Mapping[str, FeatureValue]
    evidence: tuple[RegimeEvidence, ...]
    session: MarketPhase | None
    source: str
    schema_version: str
    engine_version: str

    @property
    def has_regime(self) -> bool:
        return self.data_state is MarketDataState.VALID

    def to_dict(self) -> dict[str, object]:
        """Stable, sortable dict form (the options-layer input contract)."""
        return dict(
            sorted(
                {
                    "timestamp": self.timestamp.isoformat() if self.timestamp is not None else None,
                    "regime": self.regime,
                    "has_regime": self.has_regime,
                    "direction": self.direction.value if self.direction is not None else None,
                    "trend": self.trend.value if self.trend is not None else None,
                    "volatility": self.volatility.value if self.volatility is not None else None,
                    "data_state": self.data_state.value,
                    "warmup_status": self.warmup_status.value,
                    "required_bars": self.required_bars,
                    "available_bars": self.available_bars,
                    "bar_count": self.bar_count,
                    "session": self.session.value if self.session is not None else None,
                    "source": self.source,
                    "schema_version": self.schema_version,
                    "engine_version": self.engine_version,
                    "features": dict(self.features),
                    "evidence": [e.to_dict() for e in self.evidence],
                }.items()
            )
        )


class MarketRegimeEngine:
    """Stateless deterministic regime engine over a bar prefix.

    Defaults match the reused WS 7.3 ``RegimeDetector`` (fast=5, slow=21,
    thresholds "0.05" / "1.5" / "0.7"). Thresholds are explicit engineering
    constants, not tuned on any protected-OOS data.
    """

    def __init__(
        self,
        *,
        fast: int = 5,
        slow: int = 21,
        atr_period: int = 14,
        structure_lookback: int = 10,
        trend_threshold_pct: int | float | str | Decimal = "0.05",
        volatility_high: int | float | str | Decimal = "1.5",
        volatility_low: int | float | str | Decimal = "0.7",
        source: str = "BARS_REPLAY",
        required_bars: int | None = None,
    ) -> None:
        self.fast = positive_int(fast, "fast")
        self.slow = positive_int(slow, "slow")
        self.atr_period = positive_int(atr_period, "atr_period")
        self.structure_lookback = positive_int(structure_lookback, "structure_lookback")
        self.source = str(source).strip() or "BARS_REPLAY"
        self.engineer = FeatureEngineer(fast=self.fast, slow=self.slow, min_bars=required_bars)
        self.detector = RegimeDetector(
            fast=self.fast,
            slow=self.slow,
            trend_threshold_pct=trend_threshold_pct,
            volatility_high=volatility_high,
            volatility_low=volatility_low,
            engineer=self.engineer,
        )
        if required_bars is not None and self.engineer.required_bars != int(required_bars):
            raise ValueError("required_bars must be None or a positive int >= slow+1")

    # ------------------------------------------------------------- contracts

    @property
    def required_bars(self) -> int:
        """Core decision gate: slow + 1 (22 bars for 5/21), reusing WS 7.3."""
        return self.engineer.required_bars

    @property
    def indicator_warmups(self) -> Mapping[str, int]:
        """Documented per-indicator warm-up (bars needed before non-None)."""
        return {
            "sma_fast": self.fast,
            "sma_slow": self.slow,
            "close_return_1": 2,
            "close_return_5": 6,
            "rsi_14": 15,
            "volatility_short_10": 11,
            "volatility_long_30": self.engineer.volatility_long + 1,
            "atr_14": self.atr_period + 1,
            "structure_range_10": self.structure_lookback,
            "core_decision": self.required_bars,
        }

    # --------------------------------------------------------------- decision

    def evaluate(self, bars: Sequence[MarketPrice]) -> MarketRegimeReport:
        """Decision at the last bar (== ``evaluate_prefix(bars, len-1)``)."""
        if not bars:
            return self._report_insufficient([], MarketDataState.UNAVAILABLE, "empty bar series")
        return self.evaluate_prefix(bars, len(bars) - 1)

    def evaluate_prefix(self, bars: Sequence[MarketPrice], index: int) -> MarketRegimeReport:
        """Decision as seen at ``index`` (inclusive); never uses later bars."""
        if index < 0 or index >= len(bars):
            raise IndexError("index out of range")
        prefix = list(bars[: index + 1])

        data_state, reason = self._data_gate(prefix)
        if data_state is not MarketDataState.VALID:
            return self._report_insufficient(prefix, data_state, reason)

        regime = self.detector.detect_prefix(bars, index)
        if regime is None:
            return self._report_insufficient(
                prefix, MarketDataState.UNAVAILABLE, "detector returned no regime (gate mismatch)"
            )

        features = dict(regime.features)
        features.update(self._structure_features(prefix))
        direction = _direction_of(regime.trend)
        evidence = self._evidence(prefix, regime, features)
        session = market_phase(prefix[-1].timestamp)
        return MarketRegimeReport(
            timestamp=regime.timestamp,
            regime=f"{direction.value}_{regime.volatility.value}",
            direction=direction,
            trend=regime.trend,
            volatility=regime.volatility,
            data_state=MarketDataState.VALID,
            warmup_status=WarmupStatus.SATISFIED,
            required_bars=self.required_bars,
            available_bars=len(prefix),
            bar_count=len(prefix),
            features=_freeze(features),
            evidence=tuple(evidence),
            session=session,
            source=self.source,
            schema_version=SCHEMA_VERSION,
            engine_version=ENGINE_VERSION,
        )

    def first_valid_index(self, bars: Sequence[MarketPrice]) -> int | None:
        """First bar index whose decision has ``has_regime`` (or ``None``)."""
        end = len(bars) - self.required_bars
        if end < 0:
            return None
        for index in range(end, len(bars)):
            if self.evaluate_prefix(bars, index).has_regime:
                return index
        return None

    # --------------------------------------------------------------- internals

    def _data_gate(self, prefix: Sequence[MarketPrice]) -> tuple[MarketDataState, str]:
        if not prefix:
            return MarketDataState.UNAVAILABLE, "empty bar window"
        previous: datetime | None = None
        for bar in prefix:
            if not isinstance(bar, MarketPrice):
                return (
                    MarketDataState.INVALID,
                    f"window contains a non-MarketPrice item ({type(bar).__name__})",
                )
            if not isinstance(bar.timestamp, datetime):
                return MarketDataState.INVALID, "bar timestamp is not a datetime"
            if previous is not None and bar.timestamp <= previous:
                return (
                    MarketDataState.INVALID,
                    "non-monotonic bar timestamps (decision-time window not causal)",
                )
            previous = bar.timestamp
        if len(prefix) < self.required_bars:
            return (
                MarketDataState.UNAVAILABLE,
                f"insufficient warm-up: {len(prefix)} < {self.required_bars} required",
            )
        return MarketDataState.VALID, "window intact and warm-up satisfied"

    def _structure_features(self, prefix: Sequence[MarketPrice]) -> dict[str, FeatureValue]:
        window = prefix[-self.structure_lookback :]
        closes = [bar.close for bar in window]
        highs = [bar.high for bar in window]
        lows = [bar.low for bar in window]
        features: dict[str, FeatureValue] = {}

        # ATR is causal over the whole prefix (all bars up to the decision).
        all_highs = [bar.high for bar in prefix]
        all_lows = [bar.low for bar in prefix]
        all_closes = [bar.close for bar in prefix]
        atr_series = atr(all_highs, all_lows, all_closes, period=self.atr_period)
        if atr_series:
            last_atr = atr_series[-1]
            if last_atr is not None:
                features["atr_14"] = last_atr
                if all_closes[-1] != 0:
                    features["normalized_atr_pct"] = (last_atr / all_closes[-1]) * _DECIMAL_100

        range_high = max(highs)
        range_low = min(lows)
        close = closes[-1]
        features["range_high"] = range_high
        features["range_low"] = range_low
        if range_high != 0:
            features["distance_from_high_pct"] = ((range_high - close) / range_high) * _DECIMAL_100
        if range_low != 0:
            features["distance_from_low_pct"] = ((close - range_low) / range_low) * _DECIMAL_100
            features["range_pct"] = ((range_high - range_low) / range_low) * _DECIMAL_100
        return features

    def _evidence(
        self,
        prefix: Sequence[MarketPrice],
        regime: MarketRegime,
        features: Mapping[str, FeatureValue],
    ) -> list[RegimeEvidence]:
        gap = features.get("ma_gap_pct")
        threshold = self.detector.trend_threshold_pct
        if isinstance(gap, Decimal):
            if gap > threshold:
                reason = f"fast/slow gap {gap}% > bull threshold {threshold}%"
            elif gap < -threshold:
                reason = f"fast/slow gap {gap}% < bear threshold -{threshold}%"
            else:
                reason = f"fast/slow gap {gap}% within neutral band +-{threshold}%"
            trend_value = regime.trend.value
        else:
            reason = "ma_gap_pct unavailable; direction treated as neutral (never fabricated)"
            trend_value = "SIDEWAYS"

        ratio = features.get("volatility_ratio")
        if isinstance(ratio, Decimal):
            volatility_value = regime.volatility.value
            volatility_reason = (
                f"volatility_ratio {ratio} vs high {self.detector.volatility_high} / "
                f"low {self.detector.volatility_low}"
            )
        else:
            volatility_value = VolatilityState.NORMAL.value
            volatility_reason = (
                "volatility_ratio unavailable (needs 31 bars); volatility treated as NORMAL, never fabricated"
            )

        return [
            RegimeEvidence("direction", "ma_gap_pct", str(gap) if isinstance(gap, Decimal) else "", reason),
            RegimeEvidence(
                "volatility",
                "volatility_ratio",
                str(ratio) if isinstance(ratio, Decimal) else "",
                volatility_reason,
            ),
            RegimeEvidence(
                "warmup",
                "required_bars",
                str(self.required_bars),
                f"available {len(prefix)} >= required {self.required_bars}",
            ),
            RegimeEvidence(
                "identity",
                "trend_state",
                trend_value,
                "trend_state reused from regime.detector.RegimeDetector",
            ),
            RegimeEvidence(
                "identity",
                "volatility_state",
                volatility_value,
                "volatility_state reused from regime.detector.RegimeDetector",
            ),
        ]

    def _report_insufficient(
        self,
        prefix: Sequence[MarketPrice],
        data_state: MarketDataState,
        reason: str,
    ) -> MarketRegimeReport:
        return MarketRegimeReport(
            timestamp=prefix[-1].timestamp if prefix else None,
            regime=INSUFFICIENT_DATA,
            direction=None,
            trend=None,
            volatility=None,
            data_state=data_state,
            warmup_status=(
                WarmupStatus.INSUFFICIENT
                if data_state is MarketDataState.UNAVAILABLE
                else WarmupStatus.SATISFIED
            ),
            required_bars=self.required_bars,
            available_bars=len(prefix),
            bar_count=len(prefix),
            features=_freeze({"bars": len(prefix)}),
            evidence=(
                RegimeEvidence("data", "data_state", data_state.value, reason),
                RegimeEvidence(
                    "warmup",
                    "required_bars",
                    str(self.required_bars),
                    f"available {len(prefix)} < required {self.required_bars}"
                    if len(prefix) < self.required_bars
                    else f"available {len(prefix)}; gate reason: {reason}",
                ),
            ),
            session=market_phase(prefix[-1].timestamp) if prefix else None,
            source=self.source,
            schema_version=SCHEMA_VERSION,
            engine_version=ENGINE_VERSION,
        )


def _direction_of(trend: TrendState) -> Direction:
    return {
        TrendState.UP: Direction.BULLISH,
        TrendState.DOWN: Direction.BEARISH,
        TrendState.SIDEWAYS: Direction.NEUTRAL,
    }[trend]


__all__ = [
    "DIRECTION_MAP",
    "ENGINE_VERSION",
    "INSUFFICIENT_DATA",
    "SCHEMA_VERSION",
    "Direction",
    "MarketDataState",
    "MarketRegimeEngine",
    "MarketRegimeReport",
    "RegimeEvidence",
    "WarmupStatus",
]

DIRECTION_MAP = {
    "UP": Direction.BULLISH,
    "DOWN": Direction.BEARISH,
    "SIDEWAYS": Direction.NEUTRAL,
}