"""model_3 — low-frequency end-of-day trend capture (EOM).

Why this family
---------------
model_1 (pullback-fade) and model_2 (opening-range breakout) both failed the
10-day diagnostic with the same signature: every round trip costs roughly 60
index points on a ~24,000 index, and favorable excursion was minimal. Travelling
this hostile cost floor requires FEWER, LONGER-held, trend-aligned trades —
not more. model_3 therefore:

* trades only the LATE session (entries 13:00-14:45, exit 15:20), where the
  day's direction is established and the tail move is short and decisive;
* makes at most ONE entry per day (hard position cap) — eliminating churn;
* enters WITH the multi-day daily-EMA bias, and only when the day already shows
  conviction (realized range vs average) and the close is on the trend side;
* holds to the late-session exit with only an initial ATR stop — no take
  profit, no intra-day re-entry.

Decision-time and deterministic, like model_1/model_2: signal for bar ``i``
uses only ``bars[:i+1]``; entries/exits fire on the bar's close.
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
    confidence_band,
    volatility_bucket_of,
)
from fno_ai_paper_trading.strategies.trend_breakout import _daily_bias_arrays

_VOL_BUCKETS = RegimePullbackParams()


@dataclass
class EndOfDayParams:
    atr_period: int = 14
    adx_period: int = 14
    daily_ema_len: int = 3

    adx_trend: float = 20.0

    entry_open_minute: int = 13 * 60          # 13:00
    entry_cutoff_minute: int = 14 * 60 + 45   # 14:45
    force_exit_minute: int = 15 * 60 + 20     # 15:20

    daily_range_ratio: float = 0.8            # today's range must cover this x avg (rolling) daily range
    range_lookback: int = 5                   # completed days in the rolling range average
    stop_atr: float = 1.5
    move_ratio: float = 0.5                   # close must be on the trend side of the day's range
    confidence_threshold: float = 60.0

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


def _coerce_params(params, overrides) -> EndOfDayParams:
    base = params if params is not None else EndOfDayParams()
    if not overrides:
        return base
    allowed = set(EndOfDayParams.__dataclass_fields__)
    bad = sorted(set(overrides) - allowed)
    if bad:
        raise ValueError(f"unknown EndOfDayParams knob(s): {bad}")
    merged = {f: getattr(base, f) for f in EndOfDayParams.__dataclass_fields__}
    merged.update(overrides)
    return EndOfDayParams(**merged)


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _confidence(bias: int, adx: float, day_range_ratio: float, move_ratio: float,
                params: EndOfDayParams) -> float:
    bias_score = 35.0 if bias > 0 else 35.0 if bias < 0 else 0.0
    adx_score = _clamp(20.0 + (adx - params.adx_trend) * 6.0)
    range_score = _clamp(20.0 + (day_range_ratio - params.daily_range_ratio) * 60.0)
    move_score = _clamp(25.0 + (move_ratio - 0.5) * 80.0)
    return _clamp(0.30 * bias_score + 0.25 * adx_score + 0.25 * range_score + 0.20 * move_score)


def _fmt(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.6f}"


def _hold(bridge: MarketPrice, reason: str, meta: dict) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD, instrument=bridge.instrument,
        timestamp=bridge.timestamp, reason=reason, meta=meta,
    )


def end_of_day_signals(
    bars: Sequence[MarketPrice],
    params: EndOfDayParams | None = None,
    **overrides,
) -> list[SignalResult]:
    """Decision-time late-session trend-capture signals; one per bar."""
    p = _coerce_params(params, overrides)
    n = len(bars)
    if n == 0:
        return []

    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    atr = _atr(highs, lows, closes, p.atr_period)
    adx, _plus, _minus = _adx(highs, lows, closes, p.adx_period)
    day_maps = DayMaps.build(bars)
    minutes = day_maps.minute_of
    bias, base_ema = _daily_bias_arrays(bars, p.daily_ema_len)

    # per-day running range and realized-day average range (decision-time)
    day_range: list = [None] * n
    day_mid: list = [None] * n
    running_high: list = [None] * n
    running_low: list = [None] * n
    for i in range(n):
        hi = float(bars[i].high)
        lo = float(bars[i].low)
        prev = None
        if i > 0 and bars[i - 1].timestamp.date() == bars[i].timestamp.date():
            prev = i - 1
        rhi = hi if prev is None or running_high[prev] is None else max(running_high[prev], hi)
        rlo = lo if prev is None or running_low[prev] is None else min(running_low[prev], lo)
        running_high[i] = rhi
        running_low[i] = rlo
        day_range[i] = rhi - rlo
        day_mid[i] = (rhi + rlo) / 2.0

    # average COMPLETED day range over a rolling recent window (decision-time)
    daily_ranges: list[float] = []
    avg_daily_range: list = [None] * n
    for i in range(n):
        if i > 0 and bars[i].timestamp.date() != bars[i - 1].timestamp.date():
            daily_ranges.append(day_range[i - 1])
            if len(daily_ranges) > p.range_lookback:
                daily_ranges.pop(0)
        avg_daily_range[i] = statistics.mean(daily_ranges) if daily_ranges else None

    ready = 0
    for i in range(n):
        if atr[i] is not None and adx[i] is not None and avg_daily_range[i] is not None:
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
    entry_stop = 0.0
    entry_date = None

    for i, bridge in enumerate(bars):
        meta = {
            "time": bridge.timestamp.isoformat(),
            "day_bias": str(bias[i]),
            "regime": (REGIME_BULLISH if bias[i] > 0 else REGIME_BEARISH if bias[i] < 0 else REGIME_SIDEWAYS),
            "volatility_bucket": volatility_bucket_of(atr_ratio[i], _VOL_BUCKETS),
            "adx": _fmt(adx[i]),
            "atr": _fmt(atr[i]),
            "day_range": _fmt(day_range[i]),
            "day_mid": _fmt(day_mid[i]),
        }
        if i < ready:
            meta["warmup"] = "1"
            signals.append(_hold(bridge, "warmup", meta))
            continue

        close = float(bridge.close)
        minute = minutes[i]
        today = bars[i].timestamp.date()

        if state == 1:
            if minute >= p.force_exit_minute:
                meta["exit_reason"] = "TIME"
                signals.append(SignalResult(
                    signal=Signal.SELL, instrument=bridge.instrument,
                    timestamp=bridge.timestamp, reason="exit long: TIME", meta=meta,
                ))
                state = 0
            elif close <= entry_stop:
                meta["exit_reason"] = "STOP"
                signals.append(SignalResult(
                    signal=Signal.SELL, instrument=bridge.instrument,
                    timestamp=bridge.timestamp, reason="exit long: STOP", meta=meta,
                ))
                state = 0
            else:
                signals.append(_hold(bridge, "holding long", meta))
            continue

        if state == -1:
            if minute >= p.force_exit_minute:
                meta["exit_reason"] = "TIME"
                signals.append(SignalResult(
                    signal=Signal.BUY, instrument=bridge.instrument,
                    timestamp=bridge.timestamp, reason="exit short: TIME", meta=meta,
                ))
                state = 0
            elif close >= entry_stop:
                meta["exit_reason"] = "STOP"
                signals.append(SignalResult(
                    signal=Signal.BUY, instrument=bridge.instrument,
                    timestamp=bridge.timestamp, reason="exit short: STOP", meta=meta,
                ))
                state = 0
            else:
                signals.append(_hold(bridge, "holding short", meta))
            continue

        ok = False
        side = ""
        mode = ""
        confidence = 0.0
        comps: dict = {}
        stop_ref = 0.0
        fail = "NONE_ACTIONABLE"
        # hard cap: at most one entry per trading day
        if entry_date == today:
            fail = "RISK_LIMIT"
        elif minute < p.entry_open_minute:
            fail = "TIME_NOT_OPEN"
        elif minute > p.entry_cutoff_minute:
            fail = "TIME_CUTOFF"
        elif avg_daily_range[i] is None or not avg_daily_range[i]:
            fail = "NO_MOMENTUM"
        elif day_range[i] < p.daily_range_ratio * avg_daily_range[i]:
            fail = "LOW_VOLATILITY"
        elif adx[i] < p.adx_trend:
            fail = "WEAK_TREND"
        else:
            range_pct = day_range[i] / avg_daily_range[i]
            if bias[i] > 0:
                move = (close - day_mid[i]) / day_range[i] if day_range[i] else 0.0
                if move < p.move_ratio:
                    fail = "NO_MOMENTUM"
                else:
                    ok = True
                    side = "LONG"
                    mode = "EOM_LONG"
                    comps = {"adx": adx[i], "range": range_pct, "move": move}
                    confidence = _confidence(bias[i], adx[i], range_pct, move, p)
                    stop_ref = close - p.stop_atr * atr[i]
            elif bias[i] < 0:
                move = (day_mid[i] - close) / day_range[i] if day_range[i] else 0.0
                if move < p.move_ratio:
                    fail = "NO_MOMENTUM"
                else:
                    ok = True
                    side = "SHORT"
                    mode = "EOM_SHORT"
                    comps = {"adx": adx[i], "range": range_pct, "move": move}
                    confidence = _confidence(bias[i], adx[i], range_pct, move, p)
                    stop_ref = close + p.stop_atr * atr[i]
            else:
                fail = "SIDEWAYS_MARKET"
            if ok and confidence < p.confidence_threshold:
                ok = False
                side = ""
                fail = "POOR_RISK_REWARD"

        if ok:
            entry_date = today
            entry_stop = stop_ref
            state = 1 if side == "LONG" else -1
            signal = Signal.BUY if side == "LONG" else Signal.SELL
            meta.update({
                "entry": side,
                "entry_mode": mode,
                "confidence": f"{confidence:.1f}",
                "confidence_band": confidence_band(confidence),
                "confidence_components": ";".join(f"{k}={v:.1f}" for k, v in comps.items()),
                "stop_ref": _fmt(stop_ref),
                "entry_ref": _fmt(close),
                "entry_atr": _fmt(atr[i]),
            })
            signals.append(SignalResult(
                signal=signal, instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"{side} {mode} conf={confidence:.1f}", meta=meta,
            ))
        else:
            meta["no_trade_reasons"] = fail
            meta["no_trade_primary"] = fail
            signals.append(_hold(bridge, "no trade", meta))
    return signals


class EndOfDayStrategy(Strategy):
    """Strategy-interface wrapper for :func:`end_of_day_signals`."""

    name = "model_3_end_of_day"

    def __init__(
        self,
        params: EndOfDayParams | None = None,
        **overrides,
    ) -> None:
        self.params = _coerce_params(params, overrides)

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return end_of_day_signals(bars, params=self.params)[-1]