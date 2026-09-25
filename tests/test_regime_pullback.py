"""Tests for the model_1 regime-pullback provider and helpers.

The provider is decision-time and deterministic: a signal at bar ``i`` is a pure
function of ``bars[:i+1]``. These tests verify determinism, no look-ahead, the
warm-up contract, entry-window gating for opening signals, the equivalence
between the strategy-interface ``analyze`` and the provider on the same prefix,
parameter validation, and the day-map bookkeeping (previous close / gap per day).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.regime_pullback import (
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_SIDEWAYS,
    VOL_EXTREME,
    VOL_HIGH,
    VOL_LOW,
    VOL_NORMAL,
    DayMaps,
    RegimePullbackParams,
    RegimePullbackStrategy,
    confidence_band,
    regime_of,
    regime_pullback_signals,
    volatility_bucket_of,
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
    """Deterministic 5-minute intraday series (09:15 .. 15:25), non-monotone."""
    bars: list[MarketPrice] = []
    for d in range(days):
        day = start + timedelta(days=d)
        for k in range(75):
            n = d * 75 + k
            price = 6000.0 + 400.0 * (n / 30.0 % 2.0 - 1.0) + 1.5 * n
            ts = datetime.combine(day, datetime.min.time()) + timedelta(minutes=k * 5 + 555)
            bars.append(_bar(ts, price))
    return bars


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_regime_of_bullish():
    params = RegimePullbackParams()
    assert (
        regime_of(210.0, 205.0, 200.0, 190.0, 25.0, 0.2, params.adx_trend)
        == REGIME_BULLISH
    )


def test_regime_of_bearish():
    params = RegimePullbackParams()
    assert (
        regime_of(180.0, 185.0, 190.0, 200.0, 25.0, -0.2, params.adx_trend)
        == REGIME_BEARISH
    )


def test_regime_of_sideways_when_adx_low():
    params = RegimePullbackParams()
    assert (
        regime_of(210.0, 205.0, 200.0, 190.0, 12.0, 0.2, params.adx_trend)
        == REGIME_SIDEWAYS
    )


def test_regime_of_sideways_on_missing_values():
    params = RegimePullbackParams()
    assert regime_of(None, 205.0, 200.0, 190.0, 25.0, 0.2, params.adx_trend) == REGIME_SIDEWAYS
    assert regime_of(210.0, 205.0, 200.0, 190.0, None, 0.2, params.adx_trend) == REGIME_SIDEWAYS


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(None, VOL_LOW), (0.5, VOL_LOW), (1.0, VOL_NORMAL), (1.8, VOL_HIGH), (2.5, VOL_EXTREME)],
)
def test_volatility_bucket_of(ratio, expected):
    assert volatility_bucket_of(ratio, RegimePullbackParams()) == expected


@pytest.mark.parametrize(
    ("confidence", "band"),
    [(85.0, "STRONG"), (75.0, "VALID"), (65.0, "WEAK"), (55.0, "NO_TRADE")],
)
def test_confidence_band(confidence, band):
    assert confidence_band(confidence) == band


# ---------------------------------------------------------------------------
# provider invariants
# ---------------------------------------------------------------------------


def test_provider_returns_one_signal_per_bar():
    bars = _intraday_bars(days=4)
    signals = regime_pullback_signals(bars, RegimePullbackParams())
    assert len(signals) == len(bars)


def test_provider_is_deterministic():
    bars = _intraday_bars(days=5)
    first = regime_pullback_signals(bars, RegimePullbackParams())
    second = regime_pullback_signals(bars, RegimePullbackParams())
    assert [s.signal for s in first] == [s.signal for s in second]
    assert [s.meta for s in first] == [s.meta for s in second]


def test_empty_input_returns_empty():
    assert regime_pullback_signals([], RegimePullbackParams()) == []


def test_warmup_bars_are_hold():
    bars = _intraday_bars(days=5)
    signals = regime_pullback_signals(bars, RegimePullbackParams())
    raw = min(i for i, s in enumerate(signals) if s.meta.get("warmup") != "1")
    assert raw >= 199  # EMA200 + ADX + ATR-ratio warm-up requires ~200 bars
    for i in range(raw):
        assert signals[i].signal is Signal.HOLD
        assert signals[i].meta.get("warmup") == "1"


def test_no_look_ahead():
    bars = _intraday_bars(days=5)
    baseline = [s.signal for s in regime_pullback_signals(bars, RegimePullbackParams())]
    for cut in (40, 240, 340):
        modified = _intraday_bars(days=5)
        future = modified[cut:]
        replacement = []
        for b in future:
            replacement.append(_bar(b.timestamp, float(b.close) + 800.0))
        modified[cut:] = replacement
        perturbed = [s.signal for s in regime_pullback_signals(modified, RegimePullbackParams())]
        for i in range(0, cut):
            assert perturbed[i] == baseline[i], f"signal changed before cut {cut} at bar {i}"


def test_actionable_signals_alternate_sides():
    bars = _intraday_bars(days=5)
    signals = regime_pullback_signals(bars, RegimePullbackParams())
    actionable = [s for s in signals if s.actionable]
    for previous, current in zip(actionable, actionable[1:]):
        assert previous.signal is not current.signal
        assert current.signal in (Signal.BUY, Signal.SELL)


def test_entries_respect_time_gate():
    bars = _intraday_bars(days=5)
    params = RegimePullbackParams()
    signals = regime_pullback_signals(bars, params)
    for signal in signals:
        if signal.meta.get("entry") is None:
            continue
        minute = signal.timestamp.hour * 60 + signal.timestamp.minute
        assert params.entry_open_minute <= minute <= params.entry_cutoff_minute


def test_reason_meta_consistent():
    bars = _intraday_bars(days=5)
    for signal in regime_pullback_signals(bars, RegimePullbackParams()):
        assert signal.timestamp is not None
        assert signal.instrument is not None
        if signal.actionable:
            assert signal.meta.get("regime") in (REGIME_BULLISH, REGIME_BEARISH)


def test_unknown_parameter_rejected():
    bars = _intraday_bars(days=3)
    with pytest.raises(ValueError):
        regime_pullback_signals(bars, RegimePullbackParams(), bogus_knob=1)


# ---------------------------------------------------------------------------
# wrapper equivalence
# ---------------------------------------------------------------------------


def test_strategy_analyze_matches_provider_on_same_prefix():
    bars = _intraday_bars(days=5)
    params = RegimePullbackParams()
    strategy = RegimePullbackStrategy(params)
    for i in range(0, len(bars), 47):
        prefix = bars[: i + 1]
        expected = strategy.analyze(prefix)
        actual = regime_pullback_signals(prefix, params)[-1]
        assert actual.signal == expected.signal
        assert actual.reason == expected.reason
        assert actual.meta == expected.meta
        assert actual.instrument == expected.instrument
        assert actual.timestamp == expected.timestamp


# ---------------------------------------------------------------------------
# day maps
# ---------------------------------------------------------------------------


def test_day_maps_previous_close_and_gap_per_day():
    bars = _intraday_bars(days=3)
    maps = DayMaps.build(bars)
    day_dates = sorted({b.timestamp.date() for b in bars})
    assert len(day_dates) == 3
    # last close of day 1 == prev close for every bar of day 2
    day1_last = [b for b in bars if b.timestamp.date() == day_dates[0]][-1]
    day2_first_idx = next(
        i for i, b in enumerate(bars) if b.timestamp.date() == day_dates[1]
    )
    assert float(maps.prev_day_close[day2_first_idx]) == float(day1_last.close)
    # every bar within the same day shares the session gap
    day2_bars = [b for b in bars if b.timestamp.date() == day_dates[1]]
    gap_values = {maps.gap_pct[i] for i in range(day2_first_idx, day2_first_idx + len(day2_bars))}
    assert len(gap_values) == 1
    assert gap_values.pop() is not None
    # the first trading day has no previous close
    assert maps.prev_day_close[0] is None
    assert maps.gap_pct[0] is None