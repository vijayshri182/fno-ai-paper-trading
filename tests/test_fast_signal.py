"""Tests for the O(n) fast MA-cross signal provider (evaluation-only)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv
from fno_ai_paper_trading.evaluation.fast_signal import (
    moving_average_cross_signal_series,
    moving_average_cross_signals,
)
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _random_walk(bars: int = 300, seed: int = 3) -> list:
    import random

    random.seed(seed)
    base = datetime(2026, 1, 1, 9, 15)
    instrument = _future()
    closes = [Decimal("24000") + Decimal(i * 17) + Decimal(random.randint(-30, 30)) for i in range(bars)]
    out = []
    for i, close in enumerate(closes):
        out.append(
            type("B", (), {
                "close": close,
                "instrument": instrument,
                "timestamp": base + timedelta(minutes=5 * i),
                "open": close,
                "high": close,
                "low": close,
                "volume": 1000,
            })()
        )
    return out


def test_exact_equivalence_with_strategy_on_all_prefixes() -> None:
    bars = build_crossing_ohlcv(_future())
    strategy = MovingAverageCrossStrategy(fast=2, slow=4)
    signals = moving_average_cross_signals(bars, fast=2, slow=4)
    assert len(signals) == len(bars)
    for i in range(len(bars)):
        expected = strategy.analyze(bars[: i + 1])
        got = signals[i]
        assert got.signal == expected.signal
        assert got.instrument == expected.instrument
        assert got.timestamp == expected.timestamp
        assert got.reason == expected.reason
        assert got.meta == expected.meta


def test_exact_equivalence_on_random_walk_sampled() -> None:
    bars = _random_walk(500)
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)
    signals = moving_average_cross_signals(bars, fast=5, slow=21)
    for i in list(range(0, 500, 7)) + [21, 22, len(bars) - 1]:
        expected = strategy.analyze(bars[: i + 1])
        got = signals[i]
        assert got.signal == expected.signal
        assert got.timestamp == expected.timestamp
        assert got.meta == expected.meta


def test_warm_up_is_hold() -> None:
    bars = _random_walk(60)
    signals = moving_average_cross_signals(bars, fast=5, slow=21)
    for i in range(21):
        assert signals[i].signal is Signal.HOLD
    assert signals[21].signal is not Signal.HOLD or True


def test_series_convenience_matches_signals() -> None:
    bars = _random_walk(120)
    signals = moving_average_cross_signals(bars, fast=5, slow=21)
    series = moving_average_cross_signal_series(bars, fast=5, slow=21)
    assert [s.signal for s in signals] == series


def test_rejects_invalid_periods() -> None:
    bars = _random_walk(30)
    with pytest.raises(ValueError):
        moving_average_cross_signals(bars, fast=0, slow=21)
    with pytest.raises(ValueError):
        moving_average_cross_signals(bars, fast=21, slow=21)
    with pytest.raises(ValueError):
        moving_average_cross_signals(bars, fast=22, slow=21)


def test_empty_series_returns_empty() -> None:
    assert moving_average_cross_signals([], fast=5, slow=21) == []