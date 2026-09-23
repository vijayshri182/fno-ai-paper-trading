"""Behavioral tests for the multi-indicator composite strategy.

The fixtures are deterministic OHLCV series that produce a *known* signal for
each preset: a zigzag uptrend is a clear long (trend), its mirror is a clear
short, a monotone slide is a mean-reversion long, and a squeeze-then-pop is a
breakout long.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.execution.signal import decide_call_put
from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.composite import (
    MultiIndicatorStrategy,
    bulk_signals,
    latched_signals,
)
from fno_ai_paper_trading.strategies.registry import discover

BASE = datetime(2026, 9, 1, 9, 15)
INDEX = get_research_instrument("NIFTY 50")


def _bar(i, close, high, low, volume):
    return MarketPrice(
        INDEX,
        BASE + timedelta(minutes=5 * i),
        open=low + Decimal("3"),
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def up_zigzag(n=80, start=Decimal("24000")):
    closes, c = [], start
    for i in range(n):
        c = c - Decimal("3") if i % 2 == 0 else c + Decimal("15")  # ends on an up bar
        closes.append(c)
    return [_bar(i, c, c + Decimal("9"), c - Decimal("9"), 1000 + i * 4) for i, c in enumerate(closes)]


def down_zigzag(n=80, start=Decimal("24800")):
    closes, c = [], start
    for i in range(n):
        c = c + Decimal("3") if i % 2 == 0 else c - Decimal("15")  # ends on a down bar
        closes.append(c)
    return [_bar(i, c, c + Decimal("9"), c - Decimal("9"), 1000 + i * 4) for i, c in enumerate(closes)]


def flat_series(n=80):
    return [_bar(i, Decimal("24000"), Decimal("24009"), Decimal("23991"), 1000 + i * 4) for i in range(n)]


def monotone(direction, n=70, start=Decimal("24000")):
    return [
        _bar(i, start + Decimal("15") * direction * i,
             start + Decimal("15") * direction * i + Decimal("9"),
             start + Decimal("15") * direction * i - Decimal("9"), 1000 + i * 4)
        for i in range(n)
    ]


def squeeze_breakout(n=70):
    bars = []
    c = Decimal("24000")
    for i in range(n):
        if i < n - 6:
            c = c + Decimal("1")
            bars.append(_bar(i, c, c + Decimal("2"), c - Decimal("2"), 800 + i * 2))
        else:
            c = c + Decimal("4") if i == n - 6 else c + Decimal("55")
            bars.append(_bar(i, c, c + Decimal("70"), c - Decimal("12"), 5000))
    return bars


class TestDecisions:
    def test_trend_up_is_buy(self):
        result = MultiIndicatorStrategy(mode="trend").analyze(up_zigzag())
        assert result.signal is Signal.BUY

    def test_trend_down_is_sell(self):
        result = MultiIndicatorStrategy(mode="trend").analyze(down_zigzag())
        assert result.signal is Signal.SELL

    def test_trend_flat_holds(self):
        result = MultiIndicatorStrategy(mode="trend").analyze(flat_series())
        assert result.signal is Signal.HOLD

    def test_mean_reversion_slide_is_buy(self):
        result = MultiIndicatorStrategy(mode="mean_reversion").analyze(monotone(-1))
        assert result.signal is Signal.BUY

    def test_mean_reversion_rise_is_sell(self):
        result = MultiIndicatorStrategy(mode="mean_reversion").analyze(monotone(1))
        assert result.signal is Signal.SELL

    def test_breakout_squeeze_pop_is_buy(self):
        result = MultiIndicatorStrategy(mode="breakout").analyze(squeeze_breakout())
        assert result.signal is Signal.BUY

    def test_insufficient_data_holds(self):
        result = MultiIndicatorStrategy(mode="trend").analyze(up_zigzag(n=30))
        assert result.signal is Signal.HOLD
        assert "warming up" in result.reason

    def test_bad_mode_rejected(self):
        with pytest.raises(ValueError):
            MultiIndicatorStrategy(mode="quantum")


class TestMetaAndGuards:
    def test_meta_carries_features_and_stop(self):
        result = MultiIndicatorStrategy(mode="trend").analyze(up_zigzag())
        for key in ("ema_fast", "macd_hist", "rsi", "adx", "atr_stop_pct", "supertrend_dir",
                    "stoch_k", "williams_r", "mfi", "percent_b", "keltner_width", "obv_slope", "vwap"):
            assert key in result.meta
        assert result.meta["long_votes"]
        assert float(result.meta["confidence"]) > 0

    def test_confidence_bounded(self):
        for mode in ("trend", "mean_reversion", "breakout"):
            result = MultiIndicatorStrategy(mode=mode).analyze(squeeze_breakout())
            assert 0 <= float(result.meta["confidence"]) <= 1

    def test_adx_filter_blocks_weak_trend(self):
        strategy = MultiIndicatorStrategy(mode="trend", adx_min=Decimal("999"))
        assert strategy.analyze(up_zigzag()).signal is Signal.HOLD

    def test_adx_filter_off_allows_trade(self):
        strategy = MultiIndicatorStrategy(mode="trend", adx_min=None)
        assert strategy.analyze(up_zigzag()).signal is Signal.BUY

    def test_rsi_guard_blocks_overbought_buy(self):
        # The fixture's tail RSI is ~84; a tighter guard must block the BUY.
        guarded = MultiIndicatorStrategy(mode="trend", rsi_overbought=Decimal("80"))
        assert guarded.analyze(up_zigzag()).signal is Signal.HOLD
        unguarded = MultiIndicatorStrategy(mode="trend", rsi_overbought=None)
        assert unguarded.analyze(up_zigzag()).signal is Signal.BUY

    def test_deterministic(self):
        strategy = MultiIndicatorStrategy(mode="trend")
        first = strategy.analyze(up_zigzag())
        second = strategy.analyze(up_zigzag())
        assert first.signal is second.signal
        assert first.meta == second.meta


class TestCallPutMapping:
    def test_buy_maps_to_call(self):
        decision = decide_call_put(up_zigzag(), strategy=MultiIndicatorStrategy(mode="trend"))
        assert decision.leg.value == "CALL"

    def test_sell_maps_to_put(self):
        decision = decide_call_put(down_zigzag(), strategy=MultiIndicatorStrategy(mode="trend"))
        assert decision.leg.value == "PUT"

    def test_insufficient_data_maps_to_none(self):
        decision = decide_call_put(up_zigzag(n=20), strategy=MultiIndicatorStrategy(mode="trend"))
        assert decision.leg.value == "NONE"


class TestRegistryIntegration:
    def test_composite_registered_with_provider(self):
        registry = discover()
        spec = registry.get("composite_multi_indicator")
        assert spec.parameters["mode"] == "trend"
        assert spec.supports_confidence is True
        assert callable(registry.provider("composite_multi_indicator"))

    def test_composite_provider_is_stateless(self):
        registry = discover()
        provider = registry.create("composite_multi_indicator")
        bars = up_zigzag()
        first = provider(bars)
        second = provider(bars)
        assert first[0].meta == second[0].meta


def _as_signal_result(signal: Signal, index: int) -> object:
    return _SignalResultStub(signal)


class _SignalResultStub:
    def __init__(self, signal: Signal):
        self.signal = signal
        self.instrument = None
        self.timestamp = None
        self.reason = signal.value
        self.meta = {}


class TestBulkSignals:
    """bulk_signals(latch=False) must equal per-prefix analyze() exactly."""

    @staticmethod
    def _assert_equal_raw(bars, strategy):
        signals = bulk_signals(bars, strategy, latch=False)
        assert len(signals) == len(bars)
        for i in (0, 1, 5, 39, 40, len(bars) // 2, len(bars) - 1):
            bulk = signals[i]
            direct = strategy.analyze(bars[: i + 1])
            assert bulk.signal is direct.signal, (strategy.mode, i, bulk.signal, direct.signal)
            assert bulk.reason == direct.reason, (strategy.mode, i)
            assert bulk.meta == direct.meta, (strategy.mode, i)
            assert bulk.timestamp == direct.timestamp, (strategy.mode, i)

    def test_matches_analyze_on_zigzag(self):
        for mode in ("trend", "mean_reversion", "breakout"):
            self._assert_equal_raw(up_zigzag(), MultiIndicatorStrategy(mode=mode))

    def test_matches_analyze_on_down_zigzag(self):
        for mode in ("trend", "mean_reversion", "breakout"):
            self._assert_equal_raw(down_zigzag(), MultiIndicatorStrategy(mode=mode))

    def test_matches_analyze_on_squeeze(self):
        for mode in ("trend", "mean_reversion", "breakout"):
            self._assert_equal_raw(squeeze_breakout(), MultiIndicatorStrategy(mode=mode))

    def test_matches_analyze_on_montone(self):
        for mode in ("trend", "mean_reversion", "breakout"):
            self._assert_equal_raw(monotone(1), MultiIndicatorStrategy(mode=mode))
            self._assert_equal_raw(monotone(-1), MultiIndicatorStrategy(mode=mode))

    def test_warmup_prefixes_agree(self):
        strategy = MultiIndicatorStrategy(mode="trend")
        bars = up_zigzag(n=35)
        signals = bulk_signals(bars, strategy, latch=False)
        for i in range(len(bars)):
            direct = strategy.analyze(bars[: i + 1])
            assert signals[i].signal is direct.signal
            assert signals[i].reason == direct.reason

    def test_empty_is_empty(self):
        assert bulk_signals([], MultiIndicatorStrategy(mode="trend")) == []
        assert latched_signals([]) == []


class TestLatchedSignals:
    def test_repeated_buy_collapses_to_single_entry(self):
        signals = [_as_signal_result(Signal.BUY, 0)] * 5
        latched = latched_signals(signals)
        assert [r.signal for r in latched] == [Signal.BUY, Signal.HOLD, Signal.HOLD, Signal.HOLD, Signal.HOLD]

    def test_opposite_flip_exits_and_allows_reenry(self):
        signals = [
            Signal.BUY, Signal.BUY, Signal.SELL, Signal.SELL, Signal.BUY, Signal.HOLD,
        ]
        raw = [_as_signal_result(s, i) for i, s in enumerate(signals)]
        latched = latched_signals(raw)
        assert [r.signal for r in latched] == [
            Signal.BUY, Signal.HOLD, Signal.SELL, Signal.HOLD, Signal.BUY, Signal.HOLD,
        ]

    def test_flat_series_passes_through(self):
        raw = [_as_signal_result(Signal.HOLD, i) for i in range(4)]
        latched = latched_signals(raw)
        assert all(r.signal is Signal.HOLD for r in latched)

    def test_persistent_trend_reduces_to_one_entry(self):
        bars = up_zigzag()
        strategy = MultiIndicatorStrategy(mode="trend")
        raw = bulk_signals(bars, strategy, latch=False)
        latched = bulk_signals(bars, strategy, latch=True)
        assert sum(r.signal is Signal.BUY for r in raw) >= 2
        assert sum(r.signal is Signal.BUY for r in latched) == 1
        assert sum(r.signal is Signal.HOLD for r in latched) == len(bars) - 1


class TestBacktestEngineIntegration:
    def test_latched_replay_orders_once(self):
        from fno_ai_paper_trading.backtest.engine import BacktestEngine

        bars = up_zigzag()
        strategy = MultiIndicatorStrategy(mode="trend")
        engine = BacktestEngine()
        latched = engine.run(bars, strategy, signals=bulk_signals(bars, strategy))
        raw = engine.run(bars, strategy, signals=bulk_signals(bars, strategy, latch=False))
        # A persistent trend must not stack one lot per bar.
        assert raw.orders_filled > latched.orders_filled
        assert latched.orders_filled == 1

    def test_engine_uses_strategy_latched_hook_when_no_signals(self):
        from fno_ai_paper_trading.backtest.engine import BacktestEngine

        bars = up_zigzag()
        strategy = MultiIndicatorStrategy(mode="trend")
        engine = BacktestEngine()
        without = engine.run(bars, strategy)  # no signals -> signals_for() hook
        with_signals = engine.run(bars, strategy, signals=bulk_signals(bars, strategy))
        assert without.orders_filled == with_signals.orders_filled
        assert without.total_pnl == with_signals.total_pnl
        assert without.orders_filled == 1

    def test_engine_hook_is_latched_causal_and_reproducible(self):
        from fno_ai_paper_trading.backtest.engine import BacktestEngine

        bars = up_zigzag()
        strategy = MultiIndicatorStrategy(mode="trend")
        prepared = strategy.signals_for(bars)
        assert len(prepared) == len(bars)
        # Deterministic: calling twice gives the same series.
        assert [r.signal for r in prepared] == [r.signal for r in strategy.signals_for(bars)]
        # Latch semantics: identical to bulk_signals(latch=True) ...
        assert [r.signal for r in prepared] == [
            r.signal for r in bulk_signals(bars, strategy, latch=True)
        ]
        # ... and the raw bulk series is the causal per-prefix analyze series,
        # so the latched hook is a pure function of bars[:i+1] (no look-ahead).
        assert [r.signal for r in bulk_signals(bars, strategy, latch=False)] == [
            strategy.analyze(bars[: i + 1]).signal for i in range(len(bars))
        ]
        # All bars before the warmup region stay HOLD (first decision at index
        # warmup-1, i.e. the 40th bar with warmup=40).
        assert all(r.signal == Signal.HOLD for r in prepared[: strategy.warmup - 1])
        # Every actionable bar flips direction versus the previous actionable bar.
        last_dir = None
        for r in prepared:
            if r.signal in (Signal.BUY, Signal.SELL):
                assert r.signal != last_dir
                last_dir = r.signal