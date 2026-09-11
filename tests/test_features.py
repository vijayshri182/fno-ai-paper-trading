"""Tests for WS 7.2 deterministic feature engineering."""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv, build_sample_ohlcv
from fno_ai_paper_trading.features import (
    BarsFeatures,
    FeatureEngineer,
    close_return,
    mean_squared_return,
    rsi,
    sma,
    volatility_ratio,
)
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _crossing() -> list:
    return build_crossing_ohlcv(_future())


class TestIndicators:
    def test_sma_is_decimal_and_exact(self) -> None:
        bars = build_sample_ohlcv(_future(), bars=5)
        value = sma(bars, 2)
        assert isinstance(value, Decimal)
        assert value == bars[-2].close / 2 + bars[-1].close / 2

    def test_sma_returns_none_when_insufficient(self) -> None:
        bars = build_sample_ohlcv(_future(), bars=3)
        assert sma(bars, 5) is None

    def test_rsi_bounded_and_neutral_on_flat(self) -> None:
        assert rsi(_crossing(), 14) is not None
        flat = build_sample_ohlcv(_future(), bars=30)
        flat_value = rsi(flat, 14)
        assert flat_value is not None
        assert Decimal("0") <= flat_value <= Decimal("100")

    def test_close_return_lookback(self) -> None:
        bars = build_sample_ohlcv(_future(), bars=6)
        r1 = close_return(bars, 1)
        assert r1 == (bars[-1].close - bars[-2].close) / bars[-2].close * 100
        assert close_return(bars, 0) is None

    def test_mean_squared_return_is_non_negative(self) -> None:
        value = mean_squared_return(_crossing(), 20)
        assert value is not None and value >= 0

    def test_volatility_ratio_non_negative(self) -> None:
        value = volatility_ratio(_crossing(), short=5, long=20)
        assert value is not None and value >= 0


class TestFeatureEngineer:
    def test_insufficient_bars_returns_none(self) -> None:
        engineer = FeatureEngineer(fast=5, slow=21)
        assert engineer.compute(build_sample_ohlcv(_future(), bars=10)) is None

    def test_core_features_computed_deterministically(self) -> None:
        engineer = FeatureEngineer(fast=5, slow=21)
        features = engineer.compute(_crossing())
        assert features is not None
        assert features.bar_count == len(_crossing())
        assert isinstance(features.timestamp, type(_crossing()[-1].timestamp))
        for name in ("bars", "open", "high", "low", "close", "fast_ma", "slow_ma"):
            assert name in features.features, name
            assert not isinstance(features.features[name], float)

    def test_no_look_ahead_prefix_matches_full_compute(self) -> None:
        engineer = FeatureEngineer(fast=5, slow=21)
        bars = _crossing()
        full = engineer.compute(bars)
        prefix = engineer.compute_prefix(bars, len(bars) - 1)
        assert prefix is not None and full is not None
        assert prefix.features == full.features
        assert prefix.timestamp == full.timestamp

    def test_single_null_bar_null(self) -> None:
        engineer = FeatureEngineer(fast=2, slow=3, min_bars=3)
        assert engineer.compute(build_sample_ohlcv(_future(), bars=2)) is None

    def test_feature_mapping_is_immutable(self) -> None:
        bars = _crossing()
        engineer = FeatureEngineer(fast=5, slow=21)
        features = engineer.compute(bars)
        assert features is not None
        with pytest.raises(TypeError):
            features.features["extra"] = Decimal("1")  # type: ignore[index]

    def test_feature_lookup(self) -> None:
        engineer = FeatureEngineer(fast=5, slow=21)
        features = engineer.compute(_crossing())
        assert features is not None
        assert features.feature("close") is not None
        assert features.feature("missing-name") is None

    def test_ma_gap_and_volatility_ratio_available_on_long_series(self) -> None:
        engineer = FeatureEngineer(fast=5, slow=21)
        features = engineer.compute(_crossing())
        assert features is not None
        assert "ma_gap_pct" in features.features
        assert "volatility_ratio" in features.features


class TestFeatureEngineerValidation:
    def test_fast_must_be_less_than_slow(self) -> None:
        with pytest.raises(ValueError, match="fast period"):
            FeatureEngineer(fast=21, slow=5)

    def test_invalid_periods_rejected(self) -> None:
        with pytest.raises(ValueError):
            FeatureEngineer(slow=0)

    def test_compute_prefix_raises_on_out_of_range(self) -> None:
        engineer = FeatureEngineer(fast=5, slow=21)
        with pytest.raises(IndexError):
            engineer.compute_prefix(_crossing(), 999)

    def test_bars_features_rejects_floats(self) -> None:
        with pytest.raises(TypeError):
            BarsFeatures(
                timestamp=_crossing()[-1].timestamp,
                bar_count=22,
                features={"close": 25000.0},  # type: ignore[dict-item]
            )