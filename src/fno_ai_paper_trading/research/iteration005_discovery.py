"""ITERATION 005 - Economic Algorithm Discovery Competition (research layer).

Diagnostic-only competition over fundamentally different, LOW-CHURN candidate
architectures, judged on economic survivability (gross edge per round trip vs
the structural ~53.6/RT transaction cost) rather than raw net P&L.

Design rules
------------
* RESEARCH ONLY: no live orders, no broker access, no changes to the engine,
  risk manager, sizing, stops, daily-loss controls, commission, slippage,
  capital, model_0, historical data, or the OOS boundary. Every number here is
  produced by the UNCHANGED BacktestEngine on the frozen pre-OOS domain.
* Frozen benchmark (diagnostic): composite_trend + ADX entry >= 25 + ADX exit
  >= 28 (Iteration-004 combined variant). Reproduced exactly by
  ``day_batch_overlays.run_combined_variant`` and cross-checked against the
  recorded ``iteration_004_combined_adx.json``.
* Candidate selection: 6 genuinely distinct architectures - 3 reused from the
  discovery catalog (d1 TREND_FOLLOWING, d5 MEAN_REVERSION, d6 MARKET_STRUCTURE)
  and 3 newly implemented low-churn families:
    n1_trend_state_daily     LOW_FREQUENCY_STATE_TRANSITION (day-lagged EMA state machine)
    n2_vol_breakout_daily    VOLATILITY_EXPANSION_BREAKOUT (live range-expansion trigger)
    n3_ensemble              META_DECISION_ENSEMBLE (AND-gate of n1 + vol-move confirmation)
  No threshold sweeping, no parameter tuning, no candidate manufacturing.
* All providers are pure, decision-time, causal: signal[i] depends only on
  bars[:i+1]. Each emits exactly one SignalResult per bar.
* Evaluation: real engine journals rebuilt into TRUE round trips with the same
  conventions as the economic failure audit (slippage = adverse 0.10%, comm =
  0.03%/side, net = realized - commission, gross edge = close-to-close,
  MAE/MFE after entry candle inclusive of exit bar).
* Windows: FULL research domain 2022-01-03..2025-10-03 (pre-OOS); predefined
  TRAIN 2025-01-02..2025-06-30; VALIDATION 2025-07-01..2025-10-03. The protected
  OOS (>= 2025-10-06) is never loaded or used.
* Walk-forward consistency, cost stress (1x/3x/5x), regime robustness,
  transparent component-weighted ranking (never net-P&L alone), and the
  A..F classification system are all computed here.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.discovery.catalog import build_deck, build_params, resolve_signal_fn
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.day_batch_overlays import run_combined_variant
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.strategies.composite import MultiIndicatorStrategy

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_005_economic_discovery.json"

OOS_START = date(2025, 10, 6)
TRAIN_START = date(2025, 1, 2)
TRAIN_END = date(2025, 6, 30)
VAL_START = date(2025, 7, 1)
VAL_END = date(2025, 10, 3)

ENTRY_ADX = Decimal("25")
EXIT_ADX = Decimal("28")
COMM_RATE = Decimal("0.0003")
SLIP_RATE = Decimal("0.001")

_FIRST_BAR_MINUTE = 9 * 60 + 15
_N2_ENTRY_OPEN = 9 * 60 + 30
_N2_ENTRY_CUTOFF = 14 * 60 + 30

COMPETITION = [
    {"id": "d1_trend_ema", "family": "TREND_FOLLOWING", "kind": "reused",
     "description": "EMA 10/30/60 stack + slope + ADX (>=20) trend following; intraday, force-exit 15:15."},
    {"id": "d5_mean_reversion", "family": "MEAN_REVERSION", "kind": "reused",
     "description": "Bollinger 20/2.0 z-score fade, NORMAL-vol gated; intraday, force-exit 15:15."},
    {"id": "d6_structure", "family": "MARKET_STRUCTURE", "kind": "reused",
     "description": "Swing high/low fractal + break of structure on range expansion; intraday, force-exit 15:10."},
    {"id": "n1_trend_state_daily", "family": "LOW_FREQUENCY_STATE_TRANSITION", "kind": "new",
     "description": "Day-lagged EMA 9/26 + slope state machine; enter at next session open after a flip; multi-day holds (carry allowed)."},
    {"id": "n2_vol_breakout_daily", "family": "VOLATILITY_EXPANSION_BREAKOUT", "kind": "new",
     "description": "Live range-expansion (>=1.25x prior-20-session avg) + break of prior-day high/low; exit on completed-session contraction, timeout or ATR stop."},
    {"id": "n3_ensemble", "family": "META_DECISION_ENSEMBLE", "kind": "new",
     "description": "AND-gate of the day-lagged trend state and a volatility-move confirmation; only highest-conviction confluence trades; multi-day holds."},
]

_NEW_KEYS = ("n1_trend_state_daily", "n2_vol_breakout_daily", "n3_ensemble")
_REUSED_KEYS = ("d1_trend_ema", "d5_mean_reversion", "d6_structure")


# ---------------------------------------------------------------------------
# shared math / daily-series helpers (decision-time, causal)
# ---------------------------------------------------------------------------


def _f(value: Decimal | float) -> float:
    return float(value)


def _minute(bar: MarketPrice) -> int:
    return bar.timestamp.hour * 60 + bar.timestamp.minute


def _ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if not values:
        return out
    k = 2.0 / (period + 1)
    ema: float | None = None
    for i, value in enumerate(values):
        ema = value if ema is None else value * k + ema * (1.0 - k)
        if i >= period - 1:
            out[i] = ema
    return out


def _hold(bridge: MarketPrice, reason: str, meta: dict) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD,
        instrument=bridge.instrument,
        timestamp=bridge.timestamp,
        reason=reason,
        meta=dict(meta),
    )


def _close_signal(bridge: MarketPrice, state: int, reason: str, meta: dict) -> SignalResult:
    signal = Signal.SELL if state > 0 else Signal.BUY
    return SignalResult(
        signal=signal,
        instrument=bridge.instrument,
        timestamp=bridge.timestamp,
        reason=reason,
        meta=dict(meta),
    )


def _open_signal(bridge: MarketPrice, state: int, reason: str, meta: dict) -> SignalResult:
    signal = Signal.BUY if state > 0 else Signal.SELL
    return SignalResult(
        signal=signal,
        instrument=bridge.instrument,
        timestamp=bridge.timestamp,
        reason=reason,
        meta=dict(meta),
    )


def _base_meta(bridge: MarketPrice, i: int, n: int) -> dict:
    return {
        "i": str(i),
        "bars_total": str(n),
        "timestamp": bridge.timestamp.isoformat(),
    }


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


@dataclass(frozen=True)
class DailySeries:
    """Per-session aggregates plus the causal per-bar mapping into them.

    ``bar_day_pos[i]`` is the 0-based position of the LAST COMPLETED session
    strictly before bar ``i`` ('date' of a bar excludes the running session),
    or ``-1`` while fewer than one completed session exists. All ``*_at``
    lookups therefore use only bars[0..i] - fully decision-time.
    """

    day_dates: list[date]
    first_idx: list[int]
    last_idx: list[int]
    closes: list[float]
    highs: list[float]
    lows: list[float]
    ranges: list[float]
    returns: list[float]
    bar_day_pos: list[int]

    @classmethod
    def build(cls, bars: Sequence[MarketPrice]) -> "DailySeries":
        n = len(bars)
        day_dates: list[date] = []
        first_idx: list[int] = []
        for i, b in enumerate(bars):
            d = b.timestamp.date()
            if not day_dates or day_dates[-1] != d:
                day_dates.append(d)
                first_idx.append(i)
        last_idx: list[int] = [first_idx[k + 1] - 1 for k in range(len(first_idx) - 1)]
        last_idx.append(n - 1)
        nd = len(day_dates)
        closes: list[float] = [0.0] * nd
        highs: list[float] = [0.0] * nd
        lows: list[float] = [0.0] * nd
        for k in range(nd):
            sl = bars[first_idx[k] : last_idx[k] + 1]
            closes[k] = _f(sl[-1].close)
            highs[k] = max(_f(b.high) for b in sl)
            lows[k] = min(_f(b.low) for b in sl)
        ranges = [highs[k] - lows[k] for k in range(nd)]
        returns = [0.0] * nd
        for k in range(1, nd):
            prior = closes[k - 1]
            returns[k] = (closes[k] - prior) / prior if prior else 0.0

        pos_to_date = {d: k for k, d in enumerate(day_dates)}
        bar_day_pos: list[int] = [-1] * n
        for i, b in enumerate(bars):
            k = pos_to_date.get(b.timestamp.date())
            if k is not None and k - 1 >= 0:
                bar_day_pos[i] = k - 1
        return cls(
            day_dates=day_dates, first_idx=first_idx, last_idx=last_idx,
            closes=closes, highs=highs, lows=lows, ranges=ranges,
            returns=returns, bar_day_pos=bar_day_pos,
        )

    def avg_range_before(self, k: int, lookback: int = 20) -> float | None:
        """Mean session range of the STORED sessions before day position k."""
        if k < lookback + 1:
            return None  # need [k-1..k-lookback] all present
        window = self.ranges[k - lookback : k]
        if not window:
            return None
        return sum(window) / len(window)

    def day_ema(self, period: int) -> list[float | None]:
        return _ema(self.closes, period)


# ---------------------------------------------------------------------------
# n1 — LOW_FREQUENCY_STATE_TRANSITION (day-lagged EMA state machine)
# ---------------------------------------------------------------------------


@dataclass
class TrendStateParams:
    fast: int = 9
    slow: int = 26
    slope_window: int = 5
    stop_atr_mult: float = 4.0
    max_hold_days: int = 30

    def as_dict(self) -> dict:
        return {
            "fast": self.fast, "slow": self.slow, "slope_window": self.slope_window,
            "stop_atr_mult": self.stop_atr_mult, "max_hold_days": self.max_hold_days,
        }


def _trend_state_target(ema_fast: float | None, ema_slow: float | None,
                        ema_fast_prior: float | None) -> int:
    if ema_fast is None or ema_slow is None or ema_fast_prior is None:
        return 0
    if ema_fast > ema_slow and ema_fast > ema_fast_prior:
        return 1
    if ema_fast < ema_slow and ema_fast < ema_fast_prior:
        return -1
    return 0


def trend_state_daily_signals(
    bars: Sequence[MarketPrice], params: TrendStateParams
) -> list[SignalResult]:
    n = len(bars)
    if n == 0:
        return []
    daily = DailySeries.build(bars)
    ema_fast = daily.day_ema(params.fast)
    ema_slow = daily.day_ema(params.slow)
    warmup = params.slow + params.slope_window + 2

    targets: list[int] = [-1000] * n  # placeholder data type guard
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k < 0 or ema_fast[k] is None or ema_slow[k] is None:
            targets[i] = 0
            continue
        prior_p = k - params.slope_window
        prior = ema_fast[prior_p] if prior_p >= 0 else None
        targets[i] = _trend_state_target(ema_fast[k], ema_slow[k], prior)

    state = 0
    hold_days = 0
    entry_ref: float | None = None
    atr_ref: float | None = None
    entry_day: int | None = None
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        first_of_day = i == 0 or bars[i - 1].timestamp.date() != day
        k = daily.bar_day_pos[i]
        meta = _base_meta(bridge, i, n)
        meta.update({
            "fast": _fmt(ema_fast[k] if k >= 0 else None),
            "slow": _fmt(ema_slow[k] if k >= 0 else None),
            "target": str(targets[i]),
        })
        if k < warmup - 1 or targets[i] == -1000:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        if first_of_day and state != 0:
            hold_days += 1

        # stop check (provider-level ATR stop, both directions)
        if state != 0 and entry_ref is not None and atr_ref is not None:
            stop = entry_ref - (state * params.stop_atr_mult * atr_ref)
            if (state > 0 and bridge.close < stop) or (state < 0 and bridge.close > stop):
                was = state
                state = 0
                entry_ref = None
                atr_ref = None
                hold_days = 0
                entry_day = None
                signals[i] = _close_signal(bridge, was, "provider ATR stop exits", meta)
                continue

        if state != 0 and first_of_day and hold_days >= params.max_hold_days:
            was = state
            state = 0
            entry_ref = None
            atr_ref = None
            hold_days = 0
            entry_day = None
            signals[i] = _close_signal(bridge, was, "max-hold timeout exits", meta)
            continue

        if first_of_day:
            target = targets[i]
            if state == 0:
                if target == 1:
                    state = 1
                    hold_days = 0
                    entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1) if daily.avg_range_before(k + 1) else None
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, 1, "daily trend state up - enter long", meta)
                    continue
                if target == -1:
                    state = -1
                    hold_days = 0
                    entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1) if daily.avg_range_before(k + 1) else None
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, -1, "daily trend state down - enter short", meta)
                    continue
                signals[i] = _hold(bridge, "FLAT", meta)
                continue
            if state != target and target != 0:
                was = state
                state = 0  # close now; re-enter on the NEXT session morning
                entry_ref = None
                atr_ref = None
                hold_days = 0
                entry_day = None
                signals[i] = _close_signal(bridge, was, "daily trend state flip exits", meta)
                continue
            if state != 0 and target == 0:
                was = state
                state = 0
                entry_ref = None
                atr_ref = None
                hold_days = 0
                entry_day = None
                signals[i] = _close_signal(bridge, was, "daily trend state flat exits", meta)
                continue
            signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
            continue
        signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
    return signals


class TrendStateStrategy(Strategy):
    name = "n1_trend_state_daily"

    def __init__(self, params: TrendStateParams | None = None) -> None:
        self.params = params or TrendStateParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return trend_state_daily_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# n2 — VOLATILITY_EXPANSION_BREAKOUT (live range-expansion trigger)
# ---------------------------------------------------------------------------


@dataclass
class VolBreakoutParams:
    lookback: int = 20
    expand_mult: float = 1.25
    contract_mult: float = 0.75
    timeout_days: int = 6
    stop_atr_mult: float = 3.0
    entry_open_minute: int = _N2_ENTRY_OPEN
    entry_cutoff_minute: int = _N2_ENTRY_CUTOFF

    def as_dict(self) -> dict:
        return {
            "lookback": self.lookback, "expand_mult": self.expand_mult,
            "contract_mult": self.contract_mult, "timeout_days": self.timeout_days,
            "stop_atr_mult": self.stop_atr_mult,
            "entry_open_minute": self.entry_open_minute,
            "entry_cutoff_minute": self.entry_cutoff_minute,
        }


def vol_breakout_daily_signals(
    bars: Sequence[MarketPrice], params: VolBreakoutParams
) -> list[SignalResult]:
    n = len(bars)
    if n == 0:
        return []
    daily = DailySeries.build(bars)
    warmup = params.lookback + 2

    run_hi: float | None = None
    run_lo: float | None = None
    state = 0
    entry_ref: float | None = None
    atr_ref: float | None = None
    entry_day: int | None = None
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        first_of_day = i == 0 or bars[i - 1].timestamp.date() != day
        if first_of_day:
            run_hi = _f(bridge.high)
            run_lo = _f(bridge.low)
        else:
            run_hi = max(run_hi or _f(bridge.high), _f(bridge.high))
            run_lo = min(run_lo or _f(bridge.low), _f(bridge.low))
        k = daily.bar_day_pos[i]
        meta = _base_meta(bridge, i, n)
        avg_before = daily.avg_range_before(k + 1, params.lookback) if k >= 0 else None
        run_range = (run_hi or 0.0) - (run_lo or 0.0)
        ratio = (run_range / avg_before) if avg_before and avg_before > 0 else None
        meta.update({
            "run_range": _fmt(run_range if not first_of_day or i == 0 else run_range),
            "avg_range_before": _fmt(avg_before),
            "ratio": _fmt(ratio),
            "state": str(state),
        })
        # warmup
        if k < warmup - 1 or avg_before is None or ratio is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue

        # stop check (provider ATR stop, both directions)
        if state != 0 and entry_ref is not None and atr_ref is not None:
            stop = entry_ref - (state * params.stop_atr_mult * atr_ref)
            if (state > 0 and bridge.close < stop) or (state < 0 and bridge.close > stop):
                was = state
                state = 0
                entry_ref = None
                atr_ref = None
                entry_day = None
                signals[i] = _close_signal(bridge, was, "provider ATR stop exits", meta)
                continue

        # completed-session contraction / timeout exits (checked at first bar of session)
        if state != 0 and first_of_day:
            prev_avg = daily.avg_range_before(k, params.lookback) if k >= 1 else None
            prev_range = daily.ranges[k - 1] if k >= 1 else 0.0
            contracted = (prev_avg and prev_avg > 0 and prev_range / prev_avg < params.contract_mult)
            timed_out = entry_day is not None and (k - entry_day) >= params.timeout_days
            if contracted or timed_out:
                was = state
                why = "completed-session contraction exits" if contracted else "vol-timeout exits"
                state = 0
                entry_ref = None
                atr_ref = None
                entry_day = None
                signals[i] = _close_signal(bridge, was, why, meta)
                continue

        # entries (live expansion event inside the window)
        if state == 0 and params.entry_open_minute <= _minute(bridge) <= params.entry_cutoff_minute:
            prior_hi = daily.highs[k]
            prior_lo = daily.lows[k]
            if ratio is not None and ratio >= params.expand_mult:
                if _f(bridge.close) > prior_hi:
                    state = 1
                    entry_ref = _f(bridge.close)
                    atr_ref = avg_before
                    entry_day = k + 1
                    signals[i] = _open_signal(bridge, 1, "vol expansion + prior-high break - enter long", meta)
                    continue
                if _f(bridge.close) < prior_lo:
                    state = -1
                    entry_ref = _f(bridge.close)
                    atr_ref = avg_before
                    entry_day = k + 1
                    signals[i] = _open_signal(bridge, -1, "vol expansion + prior-low break - enter short", meta)
                    continue
        signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
    return signals


class VolBreakoutStrategy(Strategy):
    name = "n2_vol_breakout_daily"

    def __init__(self, params: VolBreakoutParams | None = None) -> None:
        self.params = params or VolBreakoutParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return vol_breakout_daily_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# n3 — META_DECISION_ENSEMBLE (AND-gate of trend state + volatility-move confirm)
# ---------------------------------------------------------------------------


@dataclass
class EnsembleParams:
    fast: int = 9
    slow: int = 26
    slope_window: int = 5
    lookback: int = 20
    stop_atr_mult: float = 4.0
    max_hold_days: int = 25

    def as_dict(self) -> dict:
        return {
            "fast": self.fast, "slow": self.slow, "slope_window": self.slope_window,
            "lookback": self.lookback, "stop_atr_mult": self.stop_atr_mult,
            "max_hold_days": self.max_hold_days,
        }


def _vol_move_confirm(daily: DailySeries, k: int, lookback: int) -> int:
    """+1 if the last completed session expanded AND closed up, -1 mirrored, else 0."""
    if k < 1:
        return 0
    avg = daily.avg_range_before(k, lookback)
    if avg is None or avg <= 0:
        return 0
    if daily.ranges[k - 1] >= avg:
        if daily.returns[k] > 0:
            return 1
        if daily.returns[k] < 0:
            return -1
    return 0


def ensemble_signals(bars: Sequence[MarketPrice], params: EnsembleParams) -> list[SignalResult]:
    n = len(bars)
    if n == 0:
        return []
    daily = DailySeries.build(bars)
    ema_fast = daily.day_ema(params.fast)
    ema_slow = daily.day_ema(params.slow)
    warmup = params.slow + params.slope_window + params.lookback + 3

    targets: list[int] = [0] * n
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k < 0:
            targets[i] = 0
            continue
        prior_p = k - params.slope_window
        prior = ema_fast[prior_p] if prior_p >= 0 else None
        trend = _trend_state_target(ema_fast[k], ema_slow[k], prior)
        volmove = _vol_move_confirm(daily, k + 1, params.lookback)
        meta_target: int = 0
        if trend == 1 and volmove == 1:
            meta_target = 1
        elif trend == -1 and volmove == -1:
            meta_target = -1
        targets[i] = meta_target

    state = 0
    hold_days = 0
    entry_ref: float | None = None
    atr_ref: float | None = None
    entry_day: int | None = None
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        first_of_day = i == 0 or bars[i - 1].timestamp.date() != day
        k = daily.bar_day_pos[i]
        meta = _base_meta(bridge, i, n)
        meta.update({"target": str(targets[i])})
        if k < warmup - 1:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        if first_of_day and state != 0:
            hold_days += 1

        # stop check
        if state != 0 and entry_ref is not None and atr_ref is not None:
            stop = entry_ref - (state * params.stop_atr_mult * atr_ref)
            if (state > 0 and bridge.close < stop) or (state < 0 and bridge.close > stop):
                was = state
                state = 0
                entry_ref = None
                atr_ref = None
                hold_days = 0
                entry_day = None
                signals[i] = _close_signal(bridge, was, "provider ATR stop exits", meta)
                continue

        if state != 0 and first_of_day and hold_days >= params.max_hold_days:
            was = state
            state = 0
            entry_ref = None
            atr_ref = None
            hold_days = 0
            entry_day = None
            signals[i] = _close_signal(bridge, was, "max-hold timeout exits", meta)
            continue

        if first_of_day:
            target = targets[i]
            if state == 0:
                if target == 1:
                    state = 1
                    hold_days = 0
                    entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, 1, "ensemble confluence up - enter long", meta)
                    continue
                if target == -1:
                    state = -1
                    hold_days = 0
                    entry_day = k + 1
                    atr_ref = daily.avg_range_before(k + 1, params.lookback)
                    entry_ref = _f(bridge.close)
                    signals[i] = _open_signal(bridge, -1, "ensemble confluence down - enter short", meta)
                    continue
                signals[i] = _hold(bridge, "FLAT", meta)
                continue
            if state != target and target != 0:
                was = state
                state = 0
                entry_ref = None
                atr_ref = None
                hold_days = 0
                entry_day = None
                signals[i] = _close_signal(bridge, was, "ensemble confluence broken - exits", meta)
                continue
            if state != 0 and target == 0:
                was = state
                state = 0
                entry_ref = None
                atr_ref = None
                hold_days = 0
                entry_day = None
                signals[i] = _close_signal(bridge, was, "ensemble confluence flat - exits", meta)
                continue
            signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
            continue
        signals[i] = _hold(bridge, "HOLDING" if state != 0 else "FLAT", meta)
    return signals


class EnsembleStrategy(Strategy):
    name = "n3_ensemble"

    def __init__(self, params: EnsembleParams | None = None) -> None:
        self.params = params or EnsembleParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return ensemble_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# registry for the new families (mirrors discovery_candidates.PROVIDERS shape)
# ---------------------------------------------------------------------------

NEW_PROVIDERS: dict[str, Callable] = {
    "n1_trend_state_daily": trend_state_daily_signals,
    "n2_vol_breakout_daily": vol_breakout_daily_signals,
    "n3_ensemble": ensemble_signals,
}

NEW_PARAMS: dict[str, Callable] = {
    "n1_trend_state_daily": TrendStateParams,
    "n2_vol_breakout_daily": VolBreakoutParams,
    "n3_ensemble": EnsembleParams,
}

NEW_STRATEGIES: dict[str, Callable] = {
    "n1_trend_state_daily": TrendStateStrategy,
    "n2_vol_breakout_daily": VolBreakoutStrategy,
    "n3_ensemble": EnsembleStrategy,
}


# ---------------------------------------------------------------------------
# evaluation: engine helpers + true-round-trip economics (audit conventions)
# ---------------------------------------------------------------------------


class _ReplayStrategy(Strategy):
    """Analyze is never called: the engine always receives precomputed signals."""

    name = "iteration005.replay"

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        raise RuntimeError("analyze() disabled; engine must receive signals=")


def engine_run(bars, config, engine, signals, journal):
    return engine.run(bars, _ReplayStrategy(), config, signals=signals, journal=journal)


def classed_days(bars: Sequence[MarketPrice]) -> dict[str, dict]:
    """Day regime + vol bucket maps (identical to the economic-failure audit)."""
    chunks = OrderedDict()
    for b in bars:
        chunks.setdefault(b.timestamp.date().isoformat(), []).append(b)
    day_map = {}
    stats_map = {}
    for d, db in chunks.items():
        ds = day_stats(db)
        stats_map[d] = ds
        day_map[d] = classify_day(ds)
    avg_moves = [stats_map[d]["avg_5m_move_pct"] for d in stats_map]
    med_move = st.median(avg_moves) if avg_moves else 0.0
    vol_bucket_map = {
        d: ("high_vol" if stats_map[d]["avg_5m_move_pct"] > med_move else "low_vol")
        for d in stats_map
    }
    return day_map, vol_bucket_map


def day_stats(dbars: Sequence[MarketPrice]) -> dict:
    opens = dbars[0].open
    closes = [b.close for b in dbars]
    highs = [b.high for b in dbars]
    lows = [b.low for b in dbars]
    day_return = (closes[-1] - opens) / opens * Decimal("100")
    day_range = (max(highs) - min(lows)) / opens * Decimal("100")
    moves = [abs(b.close - p.close) / p.close * Decimal("100")
             for p, b in zip(dbars, dbars[1:])]
    avg_move = (sum(moves, Decimal("0")) / len(moves)) if moves else Decimal("0")
    return {
        "date": dbars[0].timestamp.date().isoformat(),
        "open": str(opens), "close": str(closes[-1]),
        "day_return_pct": float(day_return), "day_range_pct": float(day_range),
        "avg_5m_move_pct": float(avg_move),
        "nbars": len(dbars),
    }


def classify_day(stats: dict) -> str:
    r = float(stats["day_return_pct"])
    rg = float(stats["day_range_pct"])
    if r > 0.8 and rg > 1.0 and abs(r) / rg > 0.4:
        return "trending_up"
    if r < -0.8 and rg > 1.0 and abs(r) / rg > 0.4:
        return "trending_down"
    if abs(r) < 0.25 and rg < 1.2:
        return "sideways"
    if rg >= 1.8 and abs(r) < 0.5:
        return "volatile"
    if rg <= 0.6 and abs(r) < 0.3:
        return "low_vol"
    return "mixed"


def build_round_trips(bars, journal):
    """TRUE round trips from the engine journal (audit convention).

    A single journal row can hold BOTH ``exit_trade_id`` and
    ``entry_trade_id`` when the engine fills a close and an open on the same
    bar (intraday flip, or a protective stop that fires on the just-opened
    entry). Chronological order is honoured:
      * an existing tracked position is closed first (exit refers to it),
      * then any new entry is opened,
      * a ``stop_fill`` row opened and stopped within the same bar closes the
        freshly opened position.
    """
    rts: list[dict] = []
    open_rt: dict | None = None
    seen_open = 0
    seen_close = 0

    def close(open_position: dict, row: dict, idx: int, price: Decimal, is_stop: bool) -> None:
        side_dir = 1 if open_position["entry_side_order"] == "BUY" else -1
        entry_price = open_position["entry_price"]
        exit_price = price
        slippage = abs(entry_price - open_position["entry_close"]) + abs(exit_price - bars[idx].close)
        commission = (
            entry_price * open_position["entry_qty"] * COMM_RATE
            + exit_price * open_position["entry_qty"] * COMM_RATE
        )
        realized = Decimal(row["realized_pnl"])
        rt = {
            "entry": open_position,
            "exit_index": idx,
            "exit_ts": row["timestamp"],
            "exit_price": exit_price,
            "exit_close": bars[idx].close,
            "exit_reason": row.get("exit_reason", ""),
            "exit_order_side": row.get("order_side", ""),
            "stop": is_stop,
            "slippage": slippage,
            "commission": commission,
            "realized": realized,
            "side": "LONG" if side_dir == 1 else "SHORT",
            "gross_close": (exit_price - entry_price) * side_dir + slippage,
            "qty": open_position["entry_qty"],
        }
        d1 = datetime.fromisoformat(open_position["entry_ts"])
        d2 = datetime.fromisoformat(row["timestamp"])
        rt["hold_bars"] = idx - open_position["entry_index"]
        rt["hold_minutes"] = int((d2 - d1).total_seconds() // 60)
        rt["carry"] = d1.date() != d2.date()
        rt["entry_day"] = d1.date().isoformat()
        rt["exit_day"] = d2.date().isoformat()
        rt["entry_hour"] = d1.hour
        rt["exit_hour"] = d2.hour
        rt["final_excursion"] = (exit_price - entry_price) * side_dir
        lag = rts[-1] if rts else None
        prev_side = lag["side"] if lag else None
        rt["entry_type"] = "first" if prev_side is None else (
            "continuation" if prev_side == rt["side"] else "flip")
        rts.append(rt)

    for row in journal:
        if "fill_price" not in row and "stop_price" not in row:
            continue
        idx = row["index"]
        price = Decimal(row["fill_price"]) if "fill_price" in row else Decimal(row["stop_price"])
        is_stop = bool(row.get("stop_fill", False))
        exit_present = row.get("exit_trade_id") is not None
        entry_present = row.get("entry_trade_id") is not None

        if exit_present and open_rt is not None:
            # exit refers to the previously tracked position (normal close,
            # protective stop, or the closing half of an intraday flip).
            seen_close += 1
            close(open_rt, row, idx, price, is_stop)
            open_rt = None
        if entry_present:
            seen_open += 1
            open_rt = {
                "entry_index": idx,
                "entry_ts": row["timestamp"],
                "entry_price": price,
                "entry_close": bars[idx].close,
                "entry_side_order": row.get("order_side", ""),
                "entry_reason": row.get("reason", ""),
                "entry_qty": row.get("fill_quantity", 1),
            }
        if exit_present and open_rt is not None and entry_present and is_stop:
            # protective stop fired on the entry filled this same bar.
            seen_close += 1
            close(open_rt, row, idx, price, is_stop)
            open_rt = None
    return rts, seen_open, seen_close


def mae_mfe(bars, rt, bucket_mins: int = 60):
    e = rt["entry"]["entry_index"]
    x = rt["exit_index"]
    if x <= e + 1:
        return {"mae": Decimal("0"), "mfe": Decimal("0"), "mfe_pct_entry": 0.0, "first_min": True}
    ep = rt["entry"]["entry_price"]
    dirn = 1 if rt["side"] == "LONG" else -1
    best_adv = Decimal("-Infinity")
    best_fav = Decimal("-Infinity")
    for i in range(e + 1, x + 1):
        bar = bars[i]
        if dirn == 1:
            adv = ep - bar.low
            fav = bar.high - ep
        else:
            adv = bar.high - ep
            fav = ep - bar.low
        if adv > best_adv:
            best_adv = adv
        if fav > best_fav:
            best_fav = fav
    mae = best_adv if best_adv > Decimal("0") else Decimal("0")
    mfe = best_fav if best_fav > Decimal("0") else Decimal("0")
    return {
        "mae": mae,
        "mfe": mfe,
        "mfe_pct_entry": float(mfe / ep * Decimal("100")) if ep else 0.0,
        "first_min": False,
    }


def add_flags(rt, bars, day_map, vol_bucket_map):
    e = rt["entry"]["entry_index"]
    x = rt["exit_index"]
    rt["entry_regime"] = day_map.get(rt["entry_day"], "?")
    rt["exit_regime"] = day_map.get(rt["exit_day"], "?")
    rt["entry_vol_bucket"] = vol_bucket_map.get(rt["entry_day"], "?")
    m = mae_mfe(bars, rt)
    rt.update(m)
    for key in ("mfe", "mae"):
        rt[key] = rt[key] if isinstance(rt[key], Decimal) else Decimal(str(rt[key]))
    rt["giveback"] = max(Decimal("0"), rt["mfe"] - rt["final_excursion"])
    rt["net"] = rt["realized"] - rt["commission"]


def amt_sum(rts, key):
    return sum((Decimal(r[key]) for r in rts), Decimal("0"))


def mean_of(rts, key):
    vals = [float(r[key]) for r in rts]
    return st.mean(vals) if vals else None


def median_of(rts, key):
    vals = [float(r[key]) for r in rts]
    return st.median(vals) if vals else None


def tstat(vals):
    if len(vals) < 2:
        return None
    m = st.mean(vals)
    sd = st.stdev(vals)
    if sd == 0:
        return None
    return m / (sd / (len(vals) ** 0.5))


# ---------------------------------------------------------------------------
# per-window economic block
# ---------------------------------------------------------------------------


def economic_block(candidate_id, bars, signals, result, journal, day_map, vol_bucket_map):
    rts, seen_open, seen_close = build_round_trips(bars, journal)
    for rt in rts:
        add_flags(rt, bars, day_map, vol_bucket_map)
    n = len(rts)
    gross = amt_sum(rts, "gross_close")
    sibling_slip = amt_sum(rts, "slippage")
    comm = amt_sum(rts, "commission")
    realized = amt_sum(rts, "realized")
    net = amt_sum(rts, "net")
    costs = sibling_slip + comm
    carry_mtm = result.total_pnl - net
    open_leg_slip = result.slippage_cost - sibling_slip
    open_leg_comm = result.total_commission - comm
    wins = sum(1 for r in rts if r["realized"] > 0)
    losses = n - wins

    gross_rt = [float(r["gross_close"]) for r in rts]
    net_rt = [float(r["net"]) for r in rts]
    days = len({b.timestamp.date() for b in bars})
    entries_day = (n / days) if days else 0.0
    flips = sum(1 for r in rts if r["entry_type"] == "flip")
    carries = [r for r in rts if r["carry"]]

    by_regime = OrderedDict()
    for r in rts:
        by_regime.setdefault(r["entry_regime"], []).append(r)
    regime_rows = []
    positive_regimes = 0
    for reg, gr in sorted(by_regime.items(), key=str):
        gn = amt_sum(gr, "net")
        if gn > 0:
            positive_regimes += 1
        regime_rows.append({
            "regime": reg, "count": len(gr),
            "wins": sum(1 for r in gr if r["net"] > 0),
            "net": str(gn), "gross": str(amt_sum(gr, "gross_close")),
        })
    by_side = OrderedDict()
    for r in rts:
        by_side.setdefault(r["side"], []).append(r)
    side_rows = [{
        "side": s, "count": len(gr), "net": str(amt_sum(gr, "net")),
    } for s, gr in by_side.items()]

    mfe_vals = [float(r["mfe"]) for r in rts]
    mae_vals = [float(r["mae"]) for r in rts]
    hold_min = [float(r["hold_minutes"]) for r in rts]

    return {
        "candidate": candidate_id,
        "bars": len(bars),
        "days": days,
        "fills": seen_open + seen_close,
        "opens": seen_open,
        "closes": seen_close,
        "round_trips": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / n * 100, 2) if n else 0.0,
        "total_pnl": str(result.total_pnl),
        "total_return_pct": round(float(result.total_return_pct), 2),
        "max_drawdown": str(result.max_drawdown),
        "transaction_costs": str(costs),
        "slippage": str(sibling_slip),
        "commission": str(comm),
        "gross_close_edge": str(gross),
        "net_closed_rts": str(net),
        "carry_mtm": str(carry_mtm),
        "gross_per_rt_mean": round(mean_of(rts, "gross_close") or 0.0, 4) if n else None,
        "gross_per_rt_median": round(median_of(rts, "gross_close") or 0.0, 4) if n else None,
        "net_per_rt_mean": round(mean_of(rts, "net") or 0.0, 4) if n else None,
        "net_per_rt_median": round(median_of(rts, "net") or 0.0, 4) if n else None,
        "t_gross_per_rt": round(tstat(gross_rt), 3) if (n >= 2 and tstat(gross_rt) is not None) else None,
        "t_net_per_rt": round(tstat(net_rt), 3) if (n >= 2 and tstat(net_rt) is not None) else None,
        "cost_per_rt_mean": round(float(costs) / n, 4) if n else None,
        "cost_over_gross_pct": round(float(costs) / abs(float(gross)) * 100, 2) if gross else None,
        "entries_per_day": round(entries_day, 4),
        "flips": flips,
        "median_mfe": round(median_of(rts, "mfe") or 0.0, 4) if n else None,
        "median_mae": round(median_of(rts, "mae") or 0.0, 4) if n else None,
        "median_mfe_pct_entry": round(median_of(rts, "mfe_pct_entry") or 0.0, 4) if n else None,
        "median_hold_minutes": round(median_of(rts, "hold_minutes") or 0.0, 4) if n else None,
        "median_hold_bars": median_of(rts, "hold_bars"),
        "carry_count": len(carries),
        "carry_share_pct": round(len(carries) / n * 100, 2) if n else 0.0,
        "open_at_close": seen_open - seen_close,
        "open_leg_slippage": str(open_leg_slip),
        "open_leg_commission": str(open_leg_comm),
        "regime_breakdown": regime_rows,
        "positive_regime_count": positive_regimes,
        "regime_count": len(regime_rows),
        "side_breakdown": side_rows,
        "reconcile": {
            "fill_count_match": (seen_open + seen_close) == result.orders_filled,
            "rt_count_match": n == result.num_trades,
            "wins_match": wins == result.winning_trades,
            "losses_match": losses == result.losing_trades,
            "cost_residuals_nonnegative": open_leg_slip >= 0 and open_leg_comm >= 0,
            "total_with_carry": round(net + carry_mtm, 6) == round(result.total_pnl, 6),
            "economic_identity": round(net + carry_mtm, 6) == round(result.total_pnl, 6),
            "reconcile_all": (
                (seen_open + seen_close) == result.orders_filled
                and n == result.num_trades
                and wins == result.winning_trades
                and losses == result.losing_trades
                and open_leg_slip >= 0 and open_leg_comm >= 0
            ),
            "reconcile_note": (
                None
                if (
                    (seen_open + seen_close) == result.orders_filled
                    and n == result.num_trades
                    and wins == result.winning_trades
                    and losses == result.losing_trades
                    and open_leg_slip >= 0 and open_leg_comm >= 0
                )
                else "provider-state/engine-position divergence and/or stop-fill cost basis differ (audit RTs use bar close; engine protective stop uses a stop reference); RT-reconstruction is partial; engine totals (total_pnl, num_trades, costs) are authoritative"
            ),
        },
    }


def summarize(result) -> dict:
    return {
        "total_pnl": str(result.total_pnl),
        "total_return_pct": round(float(result.total_return_pct), 2),
        "max_drawdown": str(result.max_drawdown),
        "orders_filled": result.orders_filled,
        "num_trades": result.num_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "total_commission": str(result.total_commission),
        "slippage_cost": str(result.slippage_cost),
    }


# ---------------------------------------------------------------------------
# signal producers (per candidate on the full domain)
# ---------------------------------------------------------------------------


def reused_signals(candidate_id, bars):
    defn = next(d for d in build_deck() if d.candidate_id == candidate_id)
    fn = resolve_signal_fn(defn)
    params = build_params(defn)
    out = fn(bars, params)
    if len(out) != len(bars):
        raise ValueError(f"{candidate_id}: provider returned {len(out)} signals for {len(bars)} bars")
    return out


def new_signals(candidate_id, bars):
    fn = NEW_PROVIDERS[candidate_id]
    params = NEW_PARAMS[candidate_id]()
    out = fn(bars, params)
    if len(out) != len(bars):
        raise ValueError(f"{candidate_id}: provider returned {len(out)} signals for {len(bars)} bars")
    return out


def candidate_signals(candidate_id, bars):
    if candidate_id in _NEW_KEYS:
        return new_signals(candidate_id, list(bars))
    if candidate_id in _REUSED_KEYS:
        return reused_signals(candidate_id, bars)
    raise KeyError(f"unknown competition candidate {candidate_id}")


def slice_by_window(bars, signals, start: date, end: date):
    """Window slices of bars and the precomputed full-domain signal stream.

    Signals are computed ONCE over the full series (decision-time = each value
    uses only bars up to itself, so warm-ups and cross-session statistics are
    respected); the engine then replays only the in-window slice starting flat.
    """
    lo = next(i for i, b in enumerate(bars) if b.timestamp.date() >= start)
    hi = next((i for i in range(len(bars) - 1, -1, -1) if bars[i].timestamp.date() <= end), len(bars) - 1) + 1
    return list(bars[lo:hi]), list(signals[lo:hi])


# ---------------------------------------------------------------------------
# ranking + classification (transparent, never net-P&L alone)
# ---------------------------------------------------------------------------


@dataclass
class RankComponents:
    gross_edge: float
    t_stat: float
    net_after_cost: float
    cost_survivability: float
    churn: float
    walkforward: float
    regime_robustness: float
    cost_stress: float

    def total(self) -> float:
        return round(self.gross_edge + self.t_stat + self.net_after_cost
                     + self.cost_survivability + self.churn + self.walkforward
                     + self.regime_robustness + self.cost_stress, 4)

    def as_dict(self) -> dict:
        return {
            "gross_edge_25": round(self.gross_edge, 4),
            "t_stat_15": round(self.t_stat, 4),
            "net_after_cost_15": round(self.net_after_cost, 4),
            "cost_survivability_15": round(self.cost_survivability, 4),
            "churn_10": round(self.churn, 4),
            "walkforward_10": round(self.walkforward, 4),
            "regime_robustness_5": round(self.regime_robustness, 4),
            "cost_stress_5": round(self.cost_stress, 4),
            "total": self.total(),
        }


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(value, hi))


def rank_candidate(full: dict, train: dict, val: dict) -> tuple[float, RankComponents]:
    """Component-weighted economic ranking (README: never net P&L alone)."""
    rt_count = int(full.get("round_trips") or 0)
    if rt_count < 10:
        # insufficient evidence mirrors classification: zero score, no free churn.
        empty = RankComponents(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return empty.total(), empty
    gross_mean = float(full["gross_per_rt_mean"] or 0.0)
    t_gross = float(full["t_gross_per_rt"] or 0.0)
    net_mean = float(full["net_per_rt_mean"] or 0.0)
    cost = float(full["cost_per_rt_mean"] or 0.0)
    cost_over = float(full["cost_over_gross_pct"] or float("inf"))

    gross_edge = 25.0 * _clip(gross_mean / 60.0)
    t_stat = 15.0 * _clip(max(t_gross, 0.0) / 3.0)
    net_after_cost = 15.0 * _clip(net_mean / 120.0)
    if gross_mean > 0 and cost_over < 100.0:
        cost_survivability = 15.0 * _clip(1.0 - cost_over / 100.0)
    else:
        cost_survivability = 0.0
    churn = 10.0 * _clip(1.0 - float(full["entries_per_day"] or 0.0) / 1.0)

    val_pos = float(val["total_pnl"] or 0.0) > 0
    train_pos = float(train["total_pnl"] or 0.0) > 0
    walk = 0.0
    if val_pos:
        walk += 5.0
    if train_pos == val_pos:
        walk += 5.0

    reg = float(full["regime_count"] or 0.0)
    regime = 5.0 * _clip((full["positive_regime_count"] / reg) if reg else 0.0)

    stress_pos = sum(1 for k in (1, 3, 5) if float(full[f"net_at_{k}x"] or 0.0) > 0)
    cost_stress = 5.0 * (stress_pos / 3)

    components = RankComponents(
        gross_edge=gross_edge, t_stat=t_stat, net_after_cost=net_after_cost,
        cost_survivability=cost_survivability, churn=churn,
        walkforward=walk, regime_robustness=regime, cost_stress=cost_stress,
    )
    return components.total(), components


def classify(full: dict, train: dict, val: dict) -> dict:
    """Iteration-005 classification system (A..F)."""
    n = full["round_trips"]
    gross = float(full["gross_close_edge"] or 0.0)
    net1 = float(full["net_closed_rts"] or 0.0) + float(full["carry_mtm"] or 0.0)
    net_at_3 = float(full["net_at_3x"] or 0.0)
    net_at_5 = float(full["net_at_5x"] or 0.0)
    val_pos = float(val["total_pnl"] or 0.0) > 0
    train_pos = float(train["total_pnl"] or 0.0) > 0
    reg_frac = (full["positive_regime_count"] / full["regime_count"]) if full["regime_count"] else 0.0

    if n < 10:
        verdict, reason = "A", "insufficient trades (< 10) - no statistically usable edge evidence"
    elif gross <= 0:
        verdict, reason = "A", "no positive gross edge over the full research domain"
    elif net1 <= 0 and float(full["cost_over_gross_pct"] or 0.0) >= 0.9:
        verdict, reason = "B", "gross edge exists but is destroyed by transaction costs (cost >= 90% of gross)"
    elif net1 <= 0:
        verdict, reason = "C", "gross edge exceeds zero but still insufficient after real costs at 1x"
    elif net_at_3 > 0 and net_at_5 > 0 and val_pos and reg_frac >= 0.5:
        verdict, reason = "D", "economically promising: net positive at 1x/3x/5x, positive validation window, majority-positive regimes - NEEDS protected-OOS proof"
    elif net1 > 0:
        verdict, reason = "E", "positive at 1x but fails stress (3x/5x) or walk-forward/regime robustness"
    else:
        verdict, reason = "F", "inconclusive evidence"
    return {
        "code": verdict,
        "reason": reason,
        "_detail": {
            "round_trips": n,
            "gross_edge": str(full["gross_close_edge"]),
            "net_at_1x": str(full["net_at_1x"]),
            "net_at_3x": str(full["net_at_3x"]),
            "net_at_5x": str(full["net_at_5x"]),
            "val_net_positive": val_pos,
            "train_net_positive": train_pos,
            "positive_regime_fraction": round(reg_frac, 3),
        },
    }


# ---------------------------------------------------------------------------
# domain loading
# ---------------------------------------------------------------------------


def load_domain() -> list[MarketPrice]:
    stored = load_dataset(DATASET)
    domain = [b for b in stored.bars if b.timestamp.date() < OOS_START]
    assert domain, "empty pre-OOS domain"
    assert max(b.timestamp for b in domain).date() < OOS_START
    return domain


def cost_stress_block(full: dict) -> dict:
    base = Decimal(full["net_closed_rts"])
    costs = Decimal(full["transaction_costs"])
    carry = Decimal(full["carry_mtm"])
    out = {}
    for k in (1, 3, 5):
        net_k = base + carry - Decimal(k - 1) * costs
        out[f"net_at_{k}x"] = str(net_k)
        out[f"net_symbol_{k}x"] = ("positive" if net_k > 0 else ("zero" if net_k == 0 else "negative"))
    return out


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_competition(bars: list[MarketPrice]) -> dict:
    day_map, vol_bucket_map = classed_days(bars)
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()

    # ---------- frozen benchmark: composite_trend + ADX >= 25 / >= 28 (Iter-004)
    strat = MultiIndicatorStrategy(mode="trend")
    bench = run_combined_variant(bars, strat, config, engine, entry_adx=ENTRY_ADX, exit_adx=EXIT_ADX)
    bench_signals = bench["series"]

    rows: dict[str, dict] = {}
    summaries: dict[str, dict] = {}
    rankings: list[dict] = []

    def evaluate(candidate_id, signals, is_benchmark=False):
        full_sig = list(signals)
        journal: list = []
        result = engine_run(bars, config, engine, full_sig, journal)
        full_block = economic_block(candidate_id, bars, full_sig, result, journal, day_map, vol_bucket_map)
        full_block.update(cost_stress_block(full_block))
        full_block["reconcile_all"] = full_block["reconcile"]["reconcile_all"]

        train_bars, train_sig = slice_by_window(bars, full_sig, TRAIN_START, TRAIN_END)
        val_bars, val_sig = slice_by_window(bars, full_sig, VAL_START, VAL_END)
        trj: list = []
        tr = engine_run(train_bars, config, engine, train_sig, trj)
        vrj: list = []
        vr = engine_run(val_bars, config, engine, val_sig, vrj)
        train_block = economic_block(candidate_id, train_bars, train_sig, tr, trj, day_map, vol_bucket_map)
        val_block = economic_block(candidate_id, val_bars, val_sig, vr, vrj, day_map, vol_bucket_map)

        score, components = rank_candidate(full_block, train_block, val_block)
        classification = classify(full_block, train_block, val_block)

        if is_benchmark:
            meta = {
                "id": candidate_id,
                "family": "TREND_FOLLOWING (frozen Iteration-004 combined)",
                "kind": "benchmark",
                "description": "composite_trend + ADX entry >= 25 + ADX exit >= 28 (frozen, diagnostic-only).",
            }
        else:
            meta = next(c for c in COMPETITION if c["id"] == candidate_id)
        rows[candidate_id] = {
            "meta": meta,
            "benchmark": is_benchmark,
            "full_domain": full_block,
            "walkforward": {
                "train_window": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
                "validation_window": [VAL_START.isoformat(), VAL_END.isoformat()],
                "train": train_block,
                "validation": val_block,
                "train_net": train_block["net_closed_rts"],
                "val_net": val_block["net_closed_rts"],
                "train_net_pnl": train_block["total_pnl"],
                "val_net_pnl": val_block["total_pnl"],
                "train_positive": float(train_block["total_pnl"]) > 0,
                "val_positive": float(val_block["total_pnl"]) > 0,
            },
            "cost_stress": {f"{k}x": full_block[f"net_at_{k}x"] for k in (1, 3, 5)},
            "ranking": {"score": score, "components": components.as_dict()},
            "classification": classification,
        }
        summaries[candidate_id] = summarize(result)
        return candidate_id

    evaluate("_benchmark_combined", bench_signals, is_benchmark=True)
    for cid in _NEW_KEYS + _REUSED_KEYS:
        evaluate(cid, candidate_signals(cid, bars))

    for cid, row in rows.items():
        rankings.append({
            "candidate": cid,
            "family": row["meta"]["family"],
            "kind": row["meta"]["kind"],
            "benchmark": row["benchmark"],
            "score": row["ranking"]["score"],
            "components": row["ranking"]["components"],
            "classification": row["classification"]["code"],
            "gross_per_rt": row["full_domain"]["gross_per_rt_mean"],
            "net_per_rt": row["full_domain"]["net_per_rt_mean"],
            "entries_per_day": row["full_domain"]["entries_per_day"],
        })
    rankings.sort(key=lambda r: r["score"], reverse=True)

    return {
        "experiment": "ITERATION_005_ECONOMIC_ALGORITHM_DISCOVERY_COMPETITION",
        "diagnostic_only": True,
        "promotion_notice": "Iteration 005 is diagnostic research and is NOT promotion evidence; no ALGO READY.",
        "domain": {
            "bars": len(bars),
            "start": bars[0].timestamp.date().isoformat(),
            "end": bars[-1].timestamp.date().isoformat(),
            "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
            "validation": [VAL_START.isoformat(), VAL_END.isoformat()],
            "protected_oos_start": OOS_START.isoformat(),
            "protected_oos_used": False,
            "dataset": DATASET.name,
        },
        "economics": {
            "config": "EvaluationConfig().backtest() - qty 1, slip 0.001, comm 0.0003/side, "
                      "2% protective stop (LONG), daily-loss 10000, capital 100000",
            "cost_per_rt_note": "structural ~53.6/RT (adverse slippage + commission) inferred from benchmark row",
        },
        "competition": rows,
        "ranking": rankings,
        "benchmark_repro": {
            "method": "day_batch_overlays.run_combined_variant entry>=25 exit>=28 on full pre-OOS domain",
            "total_pnl": str(bench["result"].total_pnl),
            "trades": bench["result"].num_trades,
            "fills": bench["result"].orders_filled,
        },
    }


def main() -> int:
    bars = load_domain()
    print(f"domain bars: {len(bars)}  ({bars[0].timestamp.date()} .. {bars[-1].timestamp.date()})")
    out = run_competition(bars)

    recap = {}
    for cid, row in out["competition"].items():
        fd = row["full_domain"]
        recap[cid] = {
            "classification": row["classification"]["code"],
            "score": row["ranking"]["score"],
            "round_trips": fd["round_trips"],
            "fills": fd["fills"],
            "entries_per_day": fd["entries_per_day"],
            "gross_per_rt": fd["gross_per_rt_mean"],
            "net_per_rt": fd["net_per_rt_mean"],
            "cost_per_rt": fd["cost_per_rt_mean"],
            "gross_edge": fd["gross_close_edge"],
            "net_at_1x": fd["net_at_1x"],
            "net_at_3x": fd["net_at_3x"],
            "net_at_5x": fd["net_at_5x"],
            "total_pnl": fd["total_pnl"],
            "carry_mtm": fd["carry_mtm"],
            "median_hold_minutes": fd["median_hold_minutes"],
            "carry_count": fd["carry_count"],
            "val_net": row["walkforward"]["val_net"],
            "reconcile_all": fd["reconcile_all"],
        }
    out["recap"] = recap

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    print("=" * 88)
    print("ITERATION 005 - ECONOMIC ALGORITHM DISCOVERY COMPETITION")
    print("=" * 88)
    hdr = f"{'candidate':22} {'cls':>3} {'score':>6} {'RTs':>4} {'fills':>5} {'ent/day':>7} "
    hdr += f"{'gross/RT':>9} {'net/RT':>8} {'cost/RT':>8} {'net1x':>11} {'net3x':>11} {'net5x':>11} {'valNet':>10}"
    print(hdr)
    print("-" * 88)
    for r in out["ranking"]:
        cid = r["candidate"]
        fdup = out["competition"][cid]["full_domain"]
        label = "BENCH" if r["benchmark"] else cid
        print(
            f"{label:22} {r['classification']:>3} {r['score']:>6.2f} "
            f"{fdup['round_trips']:>4} {fdup['fills']:>5} {fdup['entries_per_day']:>7.4f} "
            f"{str(fdup['gross_per_rt_mean']):>9} {str(fdup['net_per_rt_mean']):>8} "
            f"{str(fdup['cost_per_rt_mean']):>8} {str(fdup['net_at_1x']):>11} "
            f"{str(fdup['net_at_3x']):>11} {str(fdup['net_at_5x']):>11} "
            f"{str(out['competition'][cid]['walkforward']['val_net']):>10}"
        )
    print("-" * 88)
    print("walk-forward (train/val net):")
    for cid, row in out["competition"].items():
        wf = row["walkforward"]
        label = "BENCH" if row["benchmark"] else cid
        print(f"  {label:22} train={wf['train_net']:>12} val={wf['val_net']:>12}")
    print("benchmark repro vs Iter-004 D_combined:")
    print(f"  total_pnl={out['benchmark_repro']['total_pnl']} trades={out['benchmark_repro']['trades']} "
          f"fills={out['benchmark_repro']['fills']}")
    ok = True
    div = 0
    for cid, row in out["competition"].items():
        if not row["full_domain"]["reconcile_all"]:
            ok = False
            div += 1
            print(f"  !! partial RT-reconstruction ({row['full_domain']['reconcile']['reconcile_note']}): {cid}")
    print(f"economic_identity_all={all(row['full_domain']['reconcile']['economic_identity'] for row in out['competition'].values())}")
    print(f"reconcile_all_strict ({len(out['competition']) - div}/{len(out['competition'])} full RT reconstruction)")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())