"""model_2 — session-breakout momentum with multi-day bias (ORB + ride).

Design notes
------------
model_1 (regime/pullback-fade) read a grinding bear market correctly but faded
every intraday bounce and let the bounce extend through a tight stop: 0/5 raw
wins, 0/972 positive in the robustness grid. model_2 changes the mechanism
instead of retuning the pullback family:

* TRADE WITH the multi-day trend (daily-EMA bias) instead of fading intraday
  bounces.
* ENTER only on a genuine session opening-range breakout with momentum
  (ADX + RSI + optional range expansion) and a minimum breakout-extent edge.
* RIDE the session with a trailing ATR stop and a fixed time exit — no take
  profit — because a hostile cost floor (roughly 60 index points per round
  trip on a ~24,000 index) can only be crossed by letting favorable moves run.

Everything is decision-time and deterministic: the signal for bar ``i`` depends
only on ``bars[:i+1]``. Entries and exits trigger on the bar's close (consistent
with the close-filled paper engine), never on the bar's own intra-bar extremes.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.strategies.regime_pullback import (
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_SIDEWAYS,
    DayMaps,
    RegimePullbackParams,
    _adx,
    _atr,
    _rsi,
    confidence_band,
    volatility_bucket_of,
)

_VOL_BUCKETS = RegimePullbackParams()

_SESSION_OPEN_MINUTE = 9 * 60 + 15


@dataclass
class TrendBreakoutParams:
    # indicator periods
    adx_period: int = 14
    atr_period: int = 14
    rsi_period: int = 14

    # multi-day bias
    daily_ema_len: int = 3              # EMA window over daily closes
    adx_trend: float = 20.0

    # session / opening range
    or_minutes: int = 30                # opening-range length in minutes (9:15+)
    entry_open_minute: int = 9 * 60 + 45
    entry_cutoff_minute: int = 14 * 60
    force_exit_minute: int = 15 * 60 + 10

    # entry filters
    breakout_atr: float = 1.0           # close must clear the OR by this many ATR
    expected_edge_atr: float = 1.0      # min breakout extent (cost edge guard)
    range_ratio_min: float = 0.0        # min bar-range / 20-bar avg (0 = off)
    rsi_long_enter: float = 50.0
    rsi_short_enter: float = 50.0
    confidence_threshold: float = 60.0

    # exits
    stop_atr: float = 1.5
    trail_atr: float = 2.0
    take_profit_atr: float = 0.0        # 0 = off (ride the session)
    max_hold_bars: int = 0              # 0 = off (bounded by force_exit_minute)

    def as_dict(self) -> dict:
        out = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, Decimal):
                out[name] = str(value)
            elif isinstance(value, tuple):
                out[name] = list(value)
            else:
                out[name] = value
        return out


# ---------------------------------------------------------------------------
# multi-day bias (decision-time)
# ---------------------------------------------------------------------------


def _daily_bias_arrays(bars: Sequence[MarketPrice], window: int = 3) -> tuple[list, list]:
    """Return (bias, base_ema) aligned to ``bars``.

    base_ema[k] for a bar on day k is an EMA over the daily closes of every
    COMPLETED day before k. The bias is the sign of (today's running close
    blended into the daily EMA) minus that base EMA. For bar ``i`` only
    ``bars[:i+1]`` is ever read.
    """
    n = len(bars)
    day_lookup: dict = {}
    day_first: list[int] = []
    for i in range(n):
        d = bars[i].timestamp.date()
        if d not in day_lookup:
            day_lookup[d] = len(day_lookup)
            day_first.append(i)
    day_count = len(day_lookup)

    day_close: list[float] = []
    for k in range(day_count):
        end = day_first[k + 1] if k + 1 < day_count else n
        day_close.append(float(bars[end - 1].close))

    alpha = 2.0 / (max(2, window) + 1.0)
    base: list = [None] * day_count
    ema: float | None = None
    for k in range(day_count):
        base[k] = ema                       # EMA over days 0..k-1
        c = day_close[k]
        ema = (alpha * c + (1 - alpha) * ema) if ema is not None else c

    day_of = [day_lookup[bars[i].timestamp.date()] for i in range(n)]
    bias: list[int] = [0] * n
    base_arr: list = [None] * n
    run_arr: list = [None] * n
    for i in range(n):
        k = day_of[i]
        b = base[k]
        base_arr[i] = b
        if b is None:
            continue
        run = alpha * float(bars[i].close) + (1 - alpha) * b
        run_arr[i] = run
        if run > b:
            bias[i] = 1
        elif run < b:
            bias[i] = -1
    return bias, base_arr


# ---------------------------------------------------------------------------
# confidence (deterministic, documented formula — not tuned)
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _confidence(side: str, adx: float, dist_atr: float, rsi: float, params: TrendBreakoutParams) -> float:
    adx_score = _clamp(25.0 + (adx - params.adx_trend) * 7.0)
    dist_score = _clamp(30.0 + (dist_atr - params.breakout_atr) * 25.0)
    if side == "LONG":
        momentum_score = _clamp((rsi - params.rsi_long_enter) * 10.0)
    else:
        momentum_score = _clamp((params.rsi_short_enter - rsi) * 10.0)
    return _clamp(0.35 * adx_score + 0.35 * dist_score + 0.30 * momentum_score)


# ---------------------------------------------------------------------------
# entry / exit helpers
# ---------------------------------------------------------------------------


def _no(reason: str) -> tuple[bool, str, str, float, dict, float, float]:
    return (False, reason, "", 0.0, {}, 0.0, 0.0)


def _or_levels(bars, minutes, or_minutes) -> tuple[list, list]:
    """Opening-range high/low shared by all bars of each session."""
    n = len(bars)
    or_high: list = [None] * n
    or_low: list = [None] * n
    i = 0
    while i < n:
        end = i + 1
        while end < n and bars[end].timestamp.date() == bars[i].timestamp.date():
            end += 1
        window_hi = None
        window_lo = None
        j = i
        while j < end and minutes[j] <= _SESSION_OPEN_MINUTE + or_minutes:
            window_hi = float(bars[j].high) if window_hi is None else max(window_hi, float(bars[j].high))
            window_lo = float(bars[j].low) if window_lo is None else min(window_lo, float(bars[j].low))
            j += 1
        if window_hi is not None:
            for k in range(i, end):
                or_high[k] = window_hi
                or_low[k] = window_lo
        i = end
    return or_high, or_low


def _entry_long(i, close, minute, p, or_high, bias, rsi, atr, adx, avg_range, range_i):
    if minute < p.entry_open_minute:
        return _no("TIME_NOT_OPEN")
    if minute > p.entry_cutoff_minute:
        return _no("TIME_CUTOFF")
    if or_high is None or bias <= 0:
        return _no("SIDEWAYS_MARKET")
    if adx is None or adx < p.adx_trend:
        return _no("WEAK_TREND")
    if rsi is None or rsi < p.rsi_long_enter:
        return _no("NO_MOMENTUM")
    atr_v = atr
    if atr_v is None or atr_v <= 0:
        return _no("POOR_RISK_REWARD")
    distance = close - or_high
    if distance <= max(p.breakout_atr, p.expected_edge_atr) * atr_v:
        return _no("NO_BREAKOUT")
    if p.range_ratio_min > 0 and avg_range and range_i < p.range_ratio_min * avg_range:
        return _no("NO_BREAKOUT")
    dist_atr = distance / atr_v
    confidence = _confidence("LONG", adx, dist_atr, rsi, p)
    if confidence < p.confidence_threshold:
        return _no("POOR_RISK_REWARD")
    return (
        True, "LONG", "ORB_LONG", confidence,
        {"adx": adx, "dist_atr": dist_atr, "rsi": rsi},
        close - p.stop_atr * atr_v, close - p.trail_atr * atr_v,
    )


def _entry_short(i, close, minute, p, or_low, bias, rsi, atr, adx, avg_range, range_i):
    if minute < p.entry_open_minute:
        return _no("TIME_NOT_OPEN")
    if minute > p.entry_cutoff_minute:
        return _no("TIME_CUTOFF")
    if or_low is None or bias >= 0:
        return _no("SIDEWAYS_MARKET")
    if adx is None or adx < p.adx_trend:
        return _no("WEAK_TREND")
    if rsi is None or rsi > p.rsi_short_enter:
        return _no("NO_MOMENTUM")
    atr_v = atr
    if atr_v is None or atr_v <= 0:
        return _no("POOR_RISK_REWARD")
    distance = or_low - close
    if distance <= max(p.breakout_atr, p.expected_edge_atr) * atr_v:
        return _no("NO_BREAKOUT")
    if p.range_ratio_min > 0 and avg_range and range_i < p.range_ratio_min * avg_range:
        return _no("NO_BREAKOUT")
    dist_atr = distance / atr_v
    confidence = _confidence("SHORT", adx, dist_atr, rsi, p)
    if confidence < p.confidence_threshold:
        return _no("POOR_RISK_REWARD")
    return (
        True, "SHORT", "ORB_SHORT", confidence,
        {"adx": adx, "dist_atr": dist_atr, "rsi": rsi},
        close + p.stop_atr * atr_v, close + p.trail_atr * atr_v,
    )


def _exit_long(i, close, minute, p, entry_stop, best_high, atr_entry, entry_i) -> str | None:
    if minute >= p.force_exit_minute:
        return "TIME"
    if p.max_hold_bars > 0 and i - entry_i >= p.max_hold_bars:
        return "MAXHOLD"
    stop = entry_stop
    if p.trail_atr > 0:
        stop = max(stop, best_high - p.trail_atr * atr_entry)
    if close <= stop:
        return "STOP"
    if p.take_profit_atr > 0 and close >= entry_stop + p.take_profit_atr * atr_entry:
        return "TARGET"
    return None


def _exit_short(i, close, minute, p, entry_stop, best_low, atr_entry, entry_i) -> str | None:
    if minute >= p.force_exit_minute:
        return "TIME"
    if p.max_hold_bars > 0 and i - entry_i >= p.max_hold_bars:
        return "MAXHOLD"
    stop = entry_stop
    if p.trail_atr > 0:
        stop = min(stop, best_low + p.trail_atr * atr_entry)
    if close >= stop:
        return "STOP"
    if p.take_profit_atr > 0 and close <= entry_stop - p.take_profit_atr * atr_entry:
        return "TARGET"
    return None


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


def _fmt(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.6f}"


def _coerce_params(params, overrides) -> TrendBreakoutParams:
    base = params if params is not None else TrendBreakoutParams()
    if not overrides:
        return base
    allowed = set(TrendBreakoutParams.__dataclass_fields__)
    bad = sorted(set(overrides) - allowed)
    if bad:
        raise ValueError(f"unknown TrendBreakoutParams knob(s): {bad}")
    merged = {f: getattr(base, f) for f in TrendBreakoutParams.__dataclass_fields__}
    merged.update(overrides)
    return TrendBreakoutParams(**merged)


def _hold(bridge: MarketPrice, reason: str, meta: dict) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD, instrument=bridge.instrument,
        timestamp=bridge.timestamp, reason=reason, meta=meta,
    )


def trend_breakout_signals(
    bars: Sequence[MarketPrice],
    params: TrendBreakoutParams | None = None,
    **overrides,
) -> list[SignalResult]:
    """Decision-time session-breakout signals; one entry per bar."""
    p = _coerce_params(params, overrides)
    n = len(bars)
    if n == 0:
        return []

    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    rsi = _rsi(closes, p.rsi_period)
    atr = _atr(highs, lows, closes, p.atr_period)
    adx, _plus, _minus = _adx(highs, lows, closes, p.adx_period)
    day_maps = DayMaps.build(bars)
    minutes = day_maps.minute_of
    bias, base_ema = _daily_bias_arrays(bars, p.daily_ema_len)
    or_high, or_low = _or_levels(bars, minutes, p.or_minutes)

    avg_range: list = [None] * n
    for i in range(n):
        lo = i - 19
        if lo >= 0:
            avg_range[i] = statistics.mean(highs[j] - lows[j] for j in range(lo, i + 1))

    ready = 0
    for i in range(n):
        if atr[i] is not None and adx[i] is not None and rsi[i] is not None and avg_range[i] is not None:
            ready = i
            break
    else:
        ready = n

    atr_ratio: list = [None] * n
    for i in range(ready, n):
        lo = i - 19
        if lo >= 0:
            window = [v for v in atr[lo:i + 1] if v is not None]
            if len(window) == i - lo + 1 and statistics.median(window):
                atr_ratio[i] = atr[i] / statistics.median(window)

    signals: list[SignalResult] = []
    state = 0
    entry_i = -1
    entry_stop = 0.0
    entry_atr = 0.0
    best_high = 0.0
    best_low = 0.0

    for i, bridge in enumerate(bars):
        meta = {
            "time": bridge.timestamp.isoformat(),
            "day_bias": str(bias[i]),
            "regime": (REGIME_BULLISH if bias[i] > 0 else REGIME_BEARISH if bias[i] < 0 else REGIME_SIDEWAYS),
            "volatility_bucket": volatility_bucket_of(atr_ratio[i], _VOL_BUCKETS),
            "rsi": _fmt(rsi[i]),
            "adx": _fmt(adx[i]),
            "atr": _fmt(atr[i]),
            "or_high": _fmt(or_high[i]),
            "or_low": _fmt(or_low[i]),
        }
        if i < ready:
            meta["warmup"] = "1"
            signals.append(_hold(bridge, "warmup", meta))
            continue

        close = float(bridge.close)
        minute = minutes[i]

        if state == 1:
            if i - 1 > entry_i:
                best_high = max(best_high, highs[i - 1])
            trigger = _exit_long(i, close, minute, p, entry_stop, best_high, entry_atr, entry_i)
            if trigger is not None:
                meta["exit_reason"] = trigger
                signals.append(SignalResult(
                    signal=Signal.SELL, instrument=bridge.instrument,
                    timestamp=bridge.timestamp, reason=f"exit long: {trigger}", meta=meta,
                ))
                state = 0
            else:
                signals.append(_hold(bridge, "holding long", meta))
            continue

        if state == -1:
            if i - 1 > entry_i:
                best_low = min(best_low, lows[i - 1])
            trigger = _exit_short(i, close, minute, p, entry_stop, best_low, entry_atr, entry_i)
            if trigger is not None:
                meta["exit_reason"] = trigger
                signals.append(SignalResult(
                    signal=Signal.BUY, instrument=bridge.instrument,
                    timestamp=bridge.timestamp, reason=f"exit short: {trigger}", meta=meta,
                ))
                state = 0
            else:
                signals.append(_hold(bridge, "holding short", meta))
            continue

        long_try = _entry_long(i, close, minute, p, or_high[i], bias[i], rsi[i],
                               atr[i], adx[i], avg_range[i], highs[i] - lows[i])
        short_try = _entry_short(i, close, minute, p, or_low[i], bias[i], rsi[i],
                                 atr[i], adx[i], avg_range[i], highs[i] - lows[i])
        if long_try[0] or short_try[0]:
            ok, side, mode, confidence, comps, stop_ref, trail_ref = (
                long_try if long_try[0] else short_try
            )
            entry_i = i
            entry_stop = stop_ref
            entry_atr = atr[i]
            best_high = highs[i]
            best_low = lows[i]
            state = 1 if side == "LONG" else -1
            signal = Signal.BUY if side == "LONG" else Signal.SELL
            meta.update({
                "entry": side,
                "entry_mode": mode,
                "confidence": f"{confidence:.1f}",
                "confidence_band": confidence_band(confidence),
                "confidence_components": ";".join(f"{k}={v:.1f}" for k, v in comps.items()),
                "stop_ref": _fmt(stop_ref),
                "trail_ref": _fmt(trail_ref),
                "entry_ref": _fmt(close),
                "entry_atr": _fmt(atr[i]),
            })
            signals.append(SignalResult(
                signal=signal, instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"{side} {mode} conf={confidence:.1f}", meta=meta,
            ))
        else:
            trigger_reason = long_try[1] if long_try[1] else short_try[1]
            meta["no_trade_reasons"] = trigger_reason or "NONE_ACTIONABLE"
            meta["no_trade_primary"] = (trigger_reason or "NONE_ACTIONABLE").split("|")[0]
            signals.append(_hold(bridge, "no trade", meta))
    return signals


# ---------------------------------------------------------------------------
# strategy-interface wrapper
# ---------------------------------------------------------------------------


class TrendBreakoutStrategy(Strategy):
    """Strategy-interface wrapper for :func:`trend_breakout_signals`."""

    name = "model_2_trend_breakout"

    def __init__(
        self,
        params: TrendBreakoutParams | None = None,
        **overrides,
    ) -> None:
        self.params = _coerce_params(params, overrides)

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return trend_breakout_signals(bars, params=self.params)[-1]