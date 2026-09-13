"""Tests for the research candidate signal providers (WS 7.16).

Each candidate is pre-registered, decision-time only, and deterministic. These
tests verify the two equal-access layers agree bit-for-bit, that no provider
reads information from future bars, and that the parameters are validated.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.datasets import closes_to_bars
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.strategies.research_candidates import CANDIDATES


def _instrument() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _trendy_bars(n: int = 720) -> list:
    """Deterministic close sequence with several crossovers both ways."""
    closes: list[Decimal] = []
    price = Decimal("100")
    for i in range(n):
        segment = (i // 90) % 4
        if segment in (0, 2):
            price += Decimal("0.7")
        else:
            price -= Decimal("0.7")
        closes.append(price)
    return closes_to_bars(_instrument(), closes, start=datetime(2026, 1, 5, 9, 15))


@pytest.fixture(scope="module")
def bars():
    return _trendy_bars()


CASES = [
    ("c1_long_only_ma_cross", {"fast": 5, "slow": 21}),
    ("c2_slow_long_only_ma_cross", {"fast": 20, "slow": 50}),
    ("c3_momentum_gated_ma_cross", {"fast": 5, "slow": 21, "momentum_bars": 5}),
    ("c4_trend_gated_ma_cross", {"fast": 5, "slow": 21, "trend_threshold_pct": "0.05"}),
    ("c5_donchian_breakout", {"entry_channel": 20, "exit_channel": 10}),
]


@pytest.mark.parametrize(("name", "params"), CASES)
def test_provider_equals_strategy_analyze(bars, name, params):
    """O(n) provider must equal the Strategy analyze per-prefix, bit-for-bit."""
    strategy = CANDIDATES[name]["strategy"](**params)
    stream = CANDIDATES[name]["provider"](bars, **params)
    assert len(stream) == len(bars)
    for i in range(0, len(bars), 13):
        prefix = bars[: i + 1]
        expected = strategy.analyze(prefix)
        actual = stream[i]
        assert actual.signal == expected.signal
        assert actual.instrument == expected.instrument
        assert actual.timestamp == expected.timestamp
        assert actual.reason == expected.reason
        assert actual.meta == expected.meta


@pytest.mark.parametrize(("name", "params"), CASES)
def test_no_look_ahead(bars, name, params):
    """Changing a future bar must not change any earlier signal."""
    provider = CANDIDATES[name]["provider"]
    baseline = provider(bars, **params)
    for cut in (40, 200, 400):
        modified = _trendy_bars(len(bars))
        future = modified[cut:]
        replacement = [type(future[0])(
            instrument=b.instrument,
            timestamp=b.timestamp,
            open=b.open + Decimal("50"),
            high=b.high + Decimal("50"),
            low=b.low + Decimal("50"),
            close=b.close + Decimal("50"),
            volume=b.volume,
        ) for b in future]
        modified[cut:] = replacement
        perturbed = provider(modified, **params)
        for i in range(0, cut):
            assert perturbed[i].signal == baseline[i].signal
            assert perturbed[i].timestamp == baseline[i].timestamp


@pytest.mark.parametrize(("name", "params"), CASES)
def test_determinism(bars, name, params):
    provider = CANDIDATES[name]["provider"]
    first = provider(bars, **params)
    second = provider(bars, **params)
    assert [s.signal for s in first] == [s.signal for s in second]
    assert [s.meta for s in first] == [s.meta for s in second]


def test_invalid_params_rejected(bars):
    with pytest.raises(ValueError):
        CANDIDATES["c1_long_only_ma_cross"]["provider"](bars, fast=21, slow=5)
    with pytest.raises(ValueError):
        CANDIDATES["c5_donchian_breakout"]["provider"](
            bars, entry_channel=10, exit_channel=20
        )


@pytest.mark.parametrize(("name", "params"), CASES)
def test_replay_matches_strategy_engine(bars, name, params):
    """Engine run with signals= yields identical round trips to strategy analyze."""
    strategy = CANDIDATES[name]["strategy"](**params)
    signals = CANDIDATES[name]["provider"](bars, **params)
    engine = BacktestEngine()
    cfg = BacktestConfig()
    via_signals = engine.run(bars, strategy, cfg, signals=signals)
    via_analyze = engine.run(bars, strategy, cfg)
    assert via_signals.total_pnl == via_analyze.total_pnl
    assert len(via_signals.trades) == len(via_analyze.trades)
    for a, b in zip(via_signals.trades, via_analyze.trades):
        assert a.side == b.side
        assert a.price == b.price
        assert a.realized_pnl == b.realized_pnl


def test_champion_provider_registered():
    from fno_ai_paper_trading.evaluation.fast_signal import (
        moving_average_cross_signals as champ,
    )

    bars = _trendy_bars(300)
    a = champ(bars, fast=5, slow=21)
    b = moving_average_cross_signals(bars, fast=5, slow=21)
    assert [s.signal for s in a] == [s.signal for s in b]