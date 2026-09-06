"""Tests for the strategy layer: moving-average crossover + engine.

Uses the deterministic crossing series so BUY-then-SELL behaviour is exact.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv, build_sample_ohlcv
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy, StrategyEngine


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _series(bars: int = 5) -> list:
    return build_sample_ohlcv(_future(), bars=bars)


class TestMovingAverageCrossStrategy:
    def test_insufficient_bars_returns_hold(self) -> None:
        strategy = MovingAverageCrossStrategy(fast=5, slow=21)
        result = strategy.analyze(_series(bars=10))
        assert result.signal is Signal.HOLD
        assert "22" in result.reason  # slow + 1 bars required

    def test_crossing_series_produces_buy_then_sell(self) -> None:
        strategy = MovingAverageCrossStrategy(fast=5, slow=21)
        engine = StrategyEngine(strategy)
        results = engine.signals(build_crossing_ohlcv(_future()))

        assert results, "expected at least one actionable signal"
        assert results[0].signal is Signal.BUY
        assert any(r.signal is Signal.SELL for r in results)
        buy_index = next(i for i, r in enumerate(results) if r.signal is Signal.BUY)
        sell_index = next(i for i, r in enumerate(results) if r.signal is Signal.SELL)
        assert buy_index < sell_index

    def test_flat_series_is_hold(self) -> None:
        # build_sample_ohlcv rises linearly -> no crossover -> all HOLD.
        strategy = MovingAverageCrossStrategy(fast=2, slow=3)
        results = StrategyEngine(strategy).evaluate(_series(bars=10))
        assert all(r.signal is Signal.HOLD for r in results)

    def test_meta_contains_indicator_levels(self) -> None:
        strategy = MovingAverageCrossStrategy(fast=2, slow=3)
        result = strategy.analyze(build_crossing_ohlcv(_future()))
        assert "fast" in result.meta and "slow" in result.meta
        Decimal(result.meta["fast"])
        Decimal(result.meta["slow"])

    def test_deterministic_output(self) -> None:
        bars = build_crossing_ohlcv(_future())
        first = StrategyEngine(MovingAverageCrossStrategy()).evaluate(bars)
        second = StrategyEngine(MovingAverageCrossStrategy()).evaluate(bars)
        assert [(r.signal, r.reason) for r in first] == [(r.signal, r.reason) for r in second]

    def test_fast_must_be_shorter_than_slow(self) -> None:
        with pytest.raises(ValueError, match="fast"):
            MovingAverageCrossStrategy(fast=21, slow=21)
        with pytest.raises(ValueError, match="fast"):
            MovingAverageCrossStrategy(fast=10, slow=5)

    def test_non_positive_periods_rejected(self) -> None:
        with pytest.raises(ValueError):
            MovingAverageCrossStrategy(fast=0, slow=21)
        with pytest.raises(ValueError):
            MovingAverageCrossStrategy(fast=5, slow=-3)


class TestStrategyEngine:
    def test_evaluate_returns_one_result_per_bar(self) -> None:
        strategy = MovingAverageCrossStrategy(fast=2, slow=3)
        bars = _series(bars=10)
        results = StrategyEngine(strategy).evaluate(bars)
        assert len(results) == len(bars)

    def test_signals_filters_hold(self) -> None:
        strategy = MovingAverageCrossStrategy(fast=5, slow=21)
        results = StrategyEngine(strategy).signals(build_crossing_ohlcv(_future()))
        assert results and all(r.actionable for r in results)

    def test_empty_series(self) -> None:
        strategy = MovingAverageCrossStrategy(fast=2, slow=3)
        assert StrategyEngine(strategy).evaluate([]) == []

    def test_engine_requires_strategy(self) -> None:
        with pytest.raises(TypeError):
            StrategyEngine("not-a-strategy")  # type: ignore[arg-type]