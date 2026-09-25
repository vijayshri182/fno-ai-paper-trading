"""Tests for the model_3 end-of-day trend-capture provider.

Same decision-time contract as model_1/model_2: signal for bar ``i`` is a pure
function of ``bars[:i+1]``. All entries must obey the one-entry-per-day cap and
the late-session window.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.end_of_day import (
    EndOfDayParams,
    EndOfDayStrategy,
    end_of_day_signals,
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
    signals = end_of_day_signals(bars, EndOfDayParams())
    assert len(signals) == len(bars)


def test_provider_is_deterministic():
    bars = _intraday_bars(days=5)
    first = end_of_day_signals(bars, EndOfDayParams())
    second = end_of_day_signals(bars, EndOfDayParams())
    assert [s.signal for s in first] == [s.signal for s in second]
    assert [s.meta for s in first] == [s.meta for s in second]


def test_empty_input_returns_empty():
    assert end_of_day_signals([], EndOfDayParams()) == []


def test_warmup_bars_are_hold():
    bars = _intraday_bars(days=5)
    signals = end_of_day_signals(bars, EndOfDayParams())
    raw = min(i for i, s in enumerate(signals) if s.meta.get("warmup") != "1")
    assert raw >= 20
    for i in range(raw):
        assert signals[i].signal is Signal.HOLD
        assert signals[i].meta.get("warmup") == "1"


def test_no_look_ahead():
    bars = _intraday_bars(days=5)
    baseline = [s.signal for s in end_of_day_signals(bars, EndOfDayParams())]
    for cut in (40, 240, 340):
        modified = _intraday_bars(days=5)
        future = modified[cut:]
        replacement = [_bar(b.timestamp, float(b.close) + 800.0) for b in future]
        modified[cut:] = replacement
        perturbed = [s.signal for s in end_of_day_signals(modified, EndOfDayParams())]
        for i in range(0, cut):
            assert perturbed[i] == baseline[i], f"signal changed before cut {cut} at bar {i}"


def test_entries_respect_window_and_daily_cap():
    bars = _intraday_bars(days=5)
    params = EndOfDayParams()
    signals = end_of_day_signals(bars, params)
    entries = [s for s in signals if s.meta.get("entry") is not None]
    seen_days: set[date] = set()
    for signal in entries:
        minute = signal.timestamp.hour * 60 + signal.timestamp.minute
        assert params.entry_open_minute <= minute <= params.entry_cutoff_minute
        assert signal.timestamp.date() not in seen_days, "one entry per day violated"
        seen_days.add(signal.timestamp.date())
        if signal.meta["entry"] == "LONG":
            assert signal.signal is Signal.BUY
        else:
            assert signal.signal is Signal.SELL


def test_unknown_parameter_rejected():
    bars = _intraday_bars(days=3)
    with pytest.raises(ValueError):
        end_of_day_signals(bars, EndOfDayParams(), bogus_knob=1)


def test_strategy_analyze_matches_provider_on_same_prefix():
    bars = _intraday_bars(days=5)
    params = EndOfDayParams()
    strategy = EndOfDayStrategy(params)
    for i in range(0, len(bars), 47):
        prefix = bars[: i + 1]
        expected = strategy.analyze(prefix)
        actual = end_of_day_signals(prefix, params)[-1]
        assert actual.signal == expected.signal
        assert actual.reason == expected.reason
        assert actual.meta == expected.meta
        assert actual.instrument == expected.instrument
        assert actual.timestamp == expected.timestamp