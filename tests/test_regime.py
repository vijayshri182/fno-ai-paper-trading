"""Tests for WS 7.3 deterministic market regime detection."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.regime import (
    MarketRegime,
    RegimeDetector,
    TrendState,
    VolatilityState,
)


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _closes(closes: list[int], start: Decimal = Decimal("24000")) -> list[MarketPrice]:
    base = datetime(2026, 9, 1, 9, 15)
    bars = []
    for i, close in enumerate(closes):
        value = Decimal(str(close))
        bars.append(
            MarketPrice(
                instrument=_future(),
                timestamp=base + timedelta(minutes=i),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=1000,
            )
        )
    return bars


def _uptrend(bars: int = 40) -> list[MarketPrice]:
    return _closes([24000 + 10 * i for i in range(bars)])


def _downtrend(bars: int = 40) -> list[MarketPrice]:
    return _closes([25000 - 10 * i for i in range(bars)])


def _sideways(bars: int = 40) -> list[MarketPrice]:
    return _closes([24200 for _ in range(bars)])


def _choppy(bars: int = 40) -> list[MarketPrice]:
    closes: list[int] = []
    value = 24200
    for i in range(bars):
        value += 60 if i % 2 == 0 else -60
        closes.append(value)
    return _closes(closes)


def _calm_then_choppy(calm: int = 30, choppy: int = 10) -> list[MarketPrice]:
    closes: list[int] = []
    value = 24000
    for _ in range(calm):
        value += 1
        closes.append(value)
    for _ in range(choppy):
        value += 60 if len(closes) % 2 == 0 else -60
        closes.append(value)
    return _closes(closes)


def _choppy_then_calm(choppy: int = 30, calm: int = 10) -> list[MarketPrice]:
    closes: list[int] = []
    value = 24200
    for i in range(choppy):
        value += 60 if i % 2 == 0 else -60
        closes.append(value)
    for _ in range(calm):
        value += 1
        closes.append(value)
    return _closes(closes)


class TestRegimeDetector:
    def test_uptrend_is_up(self) -> None:
        regime = RegimeDetector().detect(_uptrend())
        assert regime is not None
        assert regime.trend is TrendState.UP

    def test_downtrend_is_down(self) -> None:
        regime = RegimeDetector().detect(_downtrend())
        assert regime is not None
        assert regime.trend is TrendState.DOWN

    def test_sideways_is_sideways(self) -> None:
        regime = RegimeDetector().detect(_sideways())
        assert regime is not None
        assert regime.trend is TrendState.SIDEWAYS

    def test_choppy_tail_after_calm_is_high_volatility(self) -> None:
        regime = RegimeDetector().detect(_calm_then_choppy())
        assert regime is not None
        assert regime.volatility is VolatilityState.HIGH

    def test_calm_tail_after_choppy_is_low_volatility(self) -> None:
        regime = RegimeDetector().detect(_choppy_then_calm())
        assert regime is not None
        assert regime.volatility is VolatilityState.LOW

    def test_steady_uptrend_is_normal_volatility(self) -> None:
        regime = RegimeDetector().detect(_uptrend())
        assert regime is not None
        assert regime.volatility is VolatilityState.NORMAL

    def test_label_format(self) -> None:
        regime = RegimeDetector().detect(_uptrend())
        assert regime is not None
        assert regime.label == "up_normal"

    def test_insufficient_bars_returns_none(self) -> None:
        assert RegimeDetector().detect(_uptrend(bars=10)) is None

    def test_no_look_ahead_prefix_matches_full(self) -> None:
        detector = RegimeDetector()
        bars = build_crossing_ohlcv(_future())
        full = detector.detect(bars)
        prefix = detector.detect_prefix(bars, len(bars) - 1)
        assert full is not None and prefix is not None
        assert prefix.label == full.label

    def test_market_regime_rejects_bad_trend(self) -> None:
        with pytest.raises(TypeError, match="trend"):
            MarketRegime(
                trend="UP",  # type: ignore[arg-type]
                volatility=VolatilityState.NORMAL,
                timestamp=datetime(2026, 9, 1),
                bar_count=22,
                features={},
            )


class TestRegimeDetectorValidation:
    def test_volatility_high_must_exceed_low(self) -> None:
        with pytest.raises(ValueError, match="volatility_high"):
            RegimeDetector(volatility_high="0.5", volatility_low="1.0")

    def test_engineer_mismatch_rejected(self) -> None:
        from fno_ai_paper_trading.features import FeatureEngineer

        mismatch = FeatureEngineer(fast=3, slow=10)
        with pytest.raises(ValueError, match="engineer"):
            RegimeDetector(fast=5, slow=21, engineer=mismatch)

    def test_bad_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            RegimeDetector(trend_threshold_pct="-0.1")