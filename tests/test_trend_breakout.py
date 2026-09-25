"""Tests for the model_2 session-breakout provider and helpers.

Same decision-time contract as model_1: signal for bar ``i`` is a pure
function of ``bars[:i+1]``. Verified: determinism, no look-ahead, warm-up,
entry-window gating, BUY/SELL alternation, and wrapper equivalence.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.trend_breakout import (
    TrendBreakoutParams,
    TrendBreakoutStrategy,
    trend_breakout_signals,
)


def _instrument() -> Instrument:
    return Instrument(
        symbol="NIFTY50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY 50",
    )


def _bar(timestamp: datetime, price: float) -> MarketPrice:
    return MarketPrice(
        instrument=_instrument(),
        timestamp=timestamp,
        open=Decimal(f"{price - 0.4:.2f}"),
        high=Decimal(f"{price + 1.6:.2f}"),
        low=Decimal(f"{price - 1.6:.2f}"),
        close=Decimal(f"{price:.2f}"),
        volume=1000,
    )


def _intraday_bars(days: int = 5, start: date = date(2026, 1, 5)) -> list[MarketPrice]:
    bars: list[MarketPrice] = []
    for d in range(days):
        day = start + timedelta(days=d)
        for k in range(75):
            n = d * 75 + k
            price = 6000.0 + 400.0 * (n / 30.0 % 2.0 - 1.0) + 1.5 * n
            ts = datetime.combine(day, datetime.min.time()) + timedelta(minutes=k * 5 + 555)
            bars.append(_bar(ts, price))
    return bars


def test_provider_returns_one_signal_per_bar():
    bars = _intraday_bars(days=4)
    signals = trend_breakout_signals(bars, TrendBreakoutParams())
    assert len(signals) == len(bars)


def test_provider_is_deterministic():
    bars = _intraday_bars(days=5)
    first = trend_breakout_signals(bars, TrendBreakoutParams())
    second = trend_breakout_signals(bars, TrendBreakoutParams())
    assert [s.signal for s in first] == [s.signal for s in second]
    assert [s.meta for s in first] == [s.meta for s in second]


def test_empty_input_returns_empty():
    assert trend_breakout_signals([], TrendBreakoutParams()) == []


def test_warmup_bars_are_hold():
    bars = _intraday_bars(days=5)
    signals = trend_breakout_signals(bars, TrendBreakoutParams())
    raw = min(i for i, s in enumerate(signals) if s.meta.get("warmup") != "1")
    assert raw >= 20  # ATR/ADX/RSI + 20-bar range warm-up (~29 bars)
    for i in range(raw):
        assert signals[i].signal is Signal.HOLD
        assert signals[i].meta.get("warmup") == "1"


def test_no_look_ahead():
    bars = _intraday_bars(days=5)
    baseline = [s.signal for s in trend_breakout_signals(bars, TrendBreakoutParams())]
    for cut in (40, 240, 340):
        modified = _intraday_bars(days=5)
        future = modified[cut:]
        replacement = [_bar(b.timestamp, float(b.close) + 800.0) for b in future]
        modified[cut:] = replacement
        perturbed = [s.signal for s in trend_breakout_signals(modified, TrendBreakoutParams())]
        for i in range(0, cut):
            assert perturbed[i] == baseline[i], f"signal changed before cut {cut} at bar {i}"


def test_actionable_signals_alternate_and_gate():
    bars = _intraday_bars(days=5)
    params = TrendBreakoutParams()
    signals = trend_breakout_signals(bars, params)
    actionable = [s for s in signals if s.actionable]
    for a, b in zip(actionable, actionable[1:]):
        a_entry = a.meta.get("entry") is not None
        b_entry = b.meta.get("entry") is not None
        # entries and exits must strictly alternate
        assert a_entry != b_entry
        if not a_entry:
            # an exit must be followed by an entry before anything else
            assert b_entry
    for i, signal in enumerate(actionable):
        if signal.meta.get("entry") is None:
            continue
        minute = signal.timestamp.hour * 60 + signal.timestamp.minute
        assert params.entry_open_minute <= minute <= params.entry_cutoff_minute
        if signal.meta["entry"] == "LONG":
            assert signal.signal is Signal.BUY
            assert actionable[i + 1].signal is Signal.SELL
        else:
            assert signal.signal is Signal.SELL
            assert actionable[i + 1].signal is Signal.BUY


def test_unknown_parameter_rejected():
    bars = _intraday_bars(days=3)
    with pytest.raises(ValueError):
        trend_breakout_signals(bars, TrendBreakoutParams(), bogus_knob=1)


def test_strategy_analyze_matches_provider_on_same_prefix():
    bars = _intraday_bars(days=5)
    params = TrendBreakoutParams()
    strategy = TrendBreakoutStrategy(params)
    for i in range(0, len(bars), 47):
        prefix = bars[: i + 1]
        expected = strategy.analyze(prefix)
        actual = trend_breakout_signals(prefix, params)[-1]
        assert actual.signal == expected.signal
        assert actual.reason == expected.reason
        assert actual.meta == expected.meta
        assert actual.instrument == expected.instrument
        assert actual.timestamp == expected.timestamp