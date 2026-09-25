"""Model_1 — Regime-Aware Pullback + Momentum + Volatility + Cost Filter (intraday).

Single deterministic decision function over a chronological 5-minute bar series
for NIFTY 50 (Nifty 50 index). Every signal at bar ``i`` is a pure function of
``bars[:i+1]`` — no look-ahead, no future regime, no future outcome. It never
places an order; the caller is responsible for the backtest/paper execution.

Decision semantics (F&O mapping handled by callers):
    * ``Signal.BUY``  -> CALL (open long / close short)
    * ``Signal.SELL`` -> PUT  (open short / close long)
    * ``Signal.HOLD`` -> NO_TRADE / holding

Components (spec frozen in MODEL_1 version metadata):

* Indicators: EMA20/EMA50/EMA200, RSI14, ADX14, ATR14, ATR ratio
  (ATR14 / rolling 20-bar median ATR14), EMA slopes, prior-10-bar swing
  high/low, previous-bar high/low, opening-range bar, previous close, current
  day open, gap %, time of day.
* Regime: BULLISH = close>EMA200 and EMA20>EMA50>EMA200 and ADX>=20 and
  EMA200 slope>0; BEARISH = mirror with ADX>=20 and slope<0; otherwise
  SIDEWAYS/UNCLEAR -> NO_TRADE.
* Volatility buckets via ATR ratio: LOW <0.70 -> NO_TRADE; NORMAL 0.70-1.50 ->
  pullback; HIGH 1.50-2.00 -> pullback+breakout; EXTREME >=2.00 -> NO_TRADE.
* Pullback (momentum-confirmed): price dipped below EMA20 within the last
  ``pullback_max_bars`` bars and the decision bar bounces back above EMA20 with
  strength (low touched the EMA20 zone, close>EMA20, close>previous high, RSI
  through the entry level) while the bullish structure is intact. Mirrored for
  shorts.
* Breakout: close beyond the prior-10-bar high/low with ADX>=22 in the HIGH
  volatility bucket.
* Risk: stop 1.5 ATR, target 3 ATR, max holding 12 bars, forced exit 15:15,
  trend-failure (regime break) exit. Exits are signalled at the decision bar and
  filled at that bar's close with execution-config slippage (same replay
  contract as every other candidate in this codebase).

The provider is the source of truth; :class:`RegimePullbackStrategy` wraps it
behind the :class:`~fno_ai_paper_trading.strategies.base.Strategy` interface so
it is a drop-in for the existing evaluation machinery.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

# ---------------------------------------------------------------------------
# parameters (frozen initial value set; ranges documented in MODEL_1 metadata)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimePullbackParams:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_long_enter: float = 45.0
    rsi_short_enter: float = 55.0
    adx_period: int = 14
    adx_trend: float = 20.0
    adx_breakout: float = 22.0
    atr_period: int = 14
    atr_ratio_window: int = 20
    atr_ratio_low: float = 0.70
    atr_ratio_high: float = 1.50
    atr_ratio_extreme: float = 2.00
    pullback_max_bars: int = 8
    pullback_zone_atr: float = 0.50
    breakout_lookback: int = 10
    stop_atr: float = 1.5
    target_atr: float = 3.0
    max_hold_bars: int = 12
    entry_open_minute: int = 565
    entry_cutoff_minute: int = 870
    force_exit_minute: int = 915
    slope_window: int = 3
    swing_lookback: int = 10
    confidence_min: float = 60.0
    breakout_vol_buckets: tuple = ("HIGH",)
    exit_on_regime_break: bool = True

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
# indicator series (one array per bar, prefix-respecting)
# ---------------------------------------------------------------------------


def _pair_lists(
    bars: Sequence[MarketPrice],
    params: RegimePullbackParams,
) -> tuple[list, ...]:
    n = len(bars)
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]

    ema_fast = _ema(closes, params.ema_fast)
    ema_mid = _ema(closes, params.ema_mid)
    ema_slow = _ema(closes, params.ema_slow)
    rsi = _rsi(closes, params.rsi_period)
    atr = _atr(highs, lows, closes, params.atr_period)
    adx, _plus_di, _minus_di = _adx(highs, lows, closes, params.adx_period)

    ema_mid_slope = _pct_slope(ema_mid, params.slope_window)
    ema_slow_slope = _pct_slope(ema_slow, params.slope_window)

    atr_ratio: list = [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        start = i - params.atr_ratio_window + 1
        if start < 0:
            continue
        window = atr[start : i + 1]
        if all(v is not None for v in window):
            atr_ratio[i] = atr[i] / statistics.median(window)

    swing_high: list = [None] * n
    swing_low: list = [None] * n
    for i in range(n):
        lo = i - params.swing_lookback
        if lo < 0:
            continue
        swing_high[i] = max(highs[lo:i])
        swing_low[i] = min(lows[lo:i])

    prev_high: list = [None] * n
    prev_low: list = [None] * n
    prev_close: list = [None] * n
    for i in range(1, n):
        prev_high[i] = highs[i - 1]
        prev_low[i] = lows[i - 1]
        prev_close[i] = closes[i - 1]

    return (
        closes, highs, lows,
        ema_fast, ema_mid, ema_slow, rsi, atr, atr_ratio,
        ema_mid_slope, ema_slow_slope, swing_high, swing_low,
        prev_high, prev_low, prev_close, adx,
    )


def _ema(values: list[float], period: int) -> list:
    n = len(values)
    out: list = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    for i in range(period, n):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(closes: list[float], period: int) -> list:
    n = len(closes)
    out: list = [None] * n
    if n < period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains[i] = change
        else:
            losses[i] = -change
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _atr(highs: list, lows: list, closes: list, period: int) -> list:
    n = len(highs)
    out: list = [None] * n
    if n < period + 1:
        return out
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    seed = sum(tr[1 : period + 1]) / period
    out[period] = seed
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _adx(
    highs: list, lows: list, closes: list, period: int
) -> tuple[list, list, list]:
    n = len(highs)
    adx_out: list = [None] * n
    plus_out: list = [None] * n
    minus_out: list = [None] * n
    if n < period * 2 + 1:
        return adx_out, plus_out, minus_out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    tr_sm = _wildered(tr, 1, period)
    pdm_sm = _wildered(plus_dm, 1, period)
    mdm_sm = _wildered(minus_dm, 1, period)
    dx = [None] * n
    for i in range(n):
        if tr_sm[i] is None or tr_sm[i] == 0:
            continue
        pdi = 100.0 * pdm_sm[i] / tr_sm[i]
        mdi = 100.0 * mdm_sm[i] / tr_sm[i]
        plus_out[i] = pdi
        minus_out[i] = mdi
        denom = pdi + mdi
        dx[i] = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
    first_idx = next((i for i, v in enumerate(dx) if v is not None), None)
    if first_idx is None:
        return adx_out, plus_out, minus_out
    defined = sum(1 for v in dx if v is not None)
    if defined < period:
        return adx_out, plus_out, minus_out
    seed = sum(dx[first_idx : first_idx + period]) / period
    adx_out[first_idx + period - 1] = seed
    for i in range(first_idx + period, n):
        adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    return adx_out, plus_out, minus_out


def _wildered(values: list, start: int, period: int) -> list:
    out: list = [None] * len(values)
    seed = sum(values[start : start + period]) / period
    out[start + period - 1] = seed
    for i in range(start + period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _pct_slope(values: list, window: int) -> list:
    out: list = [None] * len(values)
    for i in range(window, len(values)):
        if values[i] is None or values[i - window] is None:
            continue
        base = values[i - window]
        if base == 0:
            continue
        out[i] = (values[i] - base) / base * 100.0
    return out


# ---------------------------------------------------------------------------
# day maps (session context: opening range, previous close, gap, time-of-day)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayMaps:
    day_of: list
    minute_of: list
    day_open: list
    prev_day_close: list
    or_high: list
    or_low: list
    gap_pct: list

    @classmethod
    def build(cls, bars: Sequence[MarketPrice]) -> "DayMaps":
        n = len(bars)
        day_of = [b.timestamp.date() for b in bars]
        minute_of = [b.timestamp.hour * 60 + b.timestamp.minute for b in bars]
        first_of_day: dict = {}
        for i, day in enumerate(day_of):
            first_of_day.setdefault(day, i)
        days_list = sorted(first_of_day)
        day_open = [None] * n
        or_high = [None] * n
        or_low = [None] * n
        prev_day_close = [None] * n
        gap_pct = [None] * n
        for k, day in enumerate(days_list):
            idx = first_of_day[day]
            end = first_of_day[days_list[k + 1]] if k + 1 < len(days_list) else n
            session_open = float(bars[idx].open)
            session_high = float(bars[idx].high)
            session_low = float(bars[idx].low)
            for j in range(idx, end):
                day_open[j] = session_open
                or_high[j] = session_high
                or_low[j] = session_low
            if k >= 1:
                prior_close = float(bars[first_of_day[days_list[k]] - 1].close)
                gap = ((session_open - prior_close) / prior_close * 100.0) if prior_close else None
                for j in range(idx, end):
                    prev_day_close[j] = prior_close
                    gap_pct[j] = gap
        return cls(
            day_of=day_of,
            minute_of=minute_of,
            day_open=day_open,
            prev_day_close=prev_day_close,
            or_high=or_high,
            or_low=or_low,
            gap_pct=gap_pct,
        )


# ---------------------------------------------------------------------------
# feature/regime helpers (pure, testable)
# ---------------------------------------------------------------------------

REGIME_BULLISH = "BULLISH"
REGIME_BEARISH = "BEARISH"
REGIME_SIDEWAYS = "SIDEWAYS"

VOL_LOW = "LOW"
VOL_NORMAL = "NORMAL"
VOL_HIGH = "HIGH"
VOL_EXTREME = "EXTREME"


def regime_of(
    close: float | None,
    ema_fast: float | None,
    ema_mid: float | None,
    ema_slow: float | None,
    adx: float | None,
    ema_slow_slope: float | None,
    adx_trend: float,
) -> str:
    if None in (close, ema_fast, ema_mid, ema_slow, adx, ema_slow_slope):
        return REGIME_SIDEWAYS
    if (
        close > ema_slow
        and ema_fast > ema_mid
        and ema_mid > ema_slow
        and adx >= adx_trend
        and ema_slow_slope > 0
    ):
        return REGIME_BULLISH
    if (
        close < ema_slow
        and ema_fast < ema_mid
        and ema_mid < ema_slow
        and adx >= adx_trend
        and ema_slow_slope < 0
    ):
        return REGIME_BEARISH
    return REGIME_SIDEWAYS


def volatility_bucket_of(atr_ratio: float | None, params: RegimePullbackParams) -> str:
    if atr_ratio is None:
        return VOL_LOW
    if atr_ratio < params.atr_ratio_low:
        return VOL_LOW
    if atr_ratio < params.atr_ratio_high:
        return VOL_NORMAL
    if atr_ratio < params.atr_ratio_extreme:
        return VOL_HIGH
    return VOL_EXTREME


def confidence_of(
    *,
    side: str,
    regime: str,
    adx: float | None,
    ema_fast: float | None,
    ema_mid: float | None,
    ema_slow: float | None,
    close: float,
    atr: float,
    rsi: float | None,
    mode: str,
    breakout_ref: float | None,
    vol_bucket: str,
    params: RegimePullbackParams,
    minute: int,
) -> tuple[float, dict]:
    """Explainable 0-100 confidence; components documented in MODEL_1 metadata."""
    comps: dict[str, float] = {}
    atr = atr if atr > 0 else 1.0
    ref = breakout_ref if breakout_ref is not None else close
    rsi_v = rsi if rsi is not None else 50.0

    if side == "CALL":
        comps["trend_alignment"] = max(0.0, min(20.0, 12.0 + 4.0 * max(-1.0, min(1.0, (close - (ema_slow or close)) / atr))))
        fast_gap = ((ema_fast if ema_fast is not None else close) - (ema_slow or close)) / atr
        comps["ema_alignment"] = (5.0 if regime == REGIME_BULLISH else 0.0) + (4.0 if fast_gap > 0.2 else 0.0)
        zone = max(0.0, (ema_fast or close) - close)
        comps["pullback_quality"] = _zone_score(zone, atr, params)
        comps["rsi_momentum"] = _rsi_score_long(rsi_v, params)
        comps["breakout_confirmation"] = _breakout_score((close - ref) / atr if mode == "BREAKOUT" else 0.0)
    else:
        comps["trend_alignment"] = max(0.0, min(20.0, 12.0 + 4.0 * max(-1.0, min(1.0, ((ema_slow or close) - close) / atr))))
        fast_gap = ((ema_slow or close) - (ema_fast if ema_fast is not None else close)) / atr
        comps["ema_alignment"] = (5.0 if regime == REGIME_BEARISH else 0.0) + (4.0 if fast_gap > 0.2 else 0.0)
        zone = max(0.0, close - (ema_fast or close))
        comps["pullback_quality"] = _zone_score(zone, atr, params)
        comps["rsi_momentum"] = _rsi_score_short(rsi_v, params)
        comps["breakout_confirmation"] = _breakout_score((ref - close) / atr if mode == "BREAKOUT" else 0.0)

    comps["adx_strength"] = _adx_score(adx if adx is not None else 0.0, params.adx_trend)
    comps["volatility_suitability"] = 9.0 if vol_bucket == VOL_HIGH else (6.0 if vol_bucket == VOL_NORMAL else 0.0)
    risk_cost = 8.0
    if minute <= params.entry_cutoff_minute - 60:
        risk_cost += 1.0
    if adx is not None and adx >= 25.0:
        risk_cost += 1.0
    comps["risk_cost"] = min(10.0, risk_cost)

    total = sum(comps.values())
    return min(100.0, total), comps


def _zone_score(zone: float, atr: float, params: RegimePullbackParams) -> float:
    if atr <= 0:
        return 3.0
    ratio = zone / atr
    if ratio <= 0.25:
        return 15.0
    if ratio <= params.pullback_zone_atr:
        return 11.0
    if ratio <= 1.0:
        return 7.0
    return 3.0


def _rsi_score_long(rsi: float, params: RegimePullbackParams) -> float:
    if rsi > 70:
        return 3.0
    if rsi >= 60:
        return 7.0
    if rsi >= params.rsi_long_enter:
        return 8.0
    return 2.0


def _rsi_score_short(rsi: float, params: RegimePullbackParams) -> float:
    if rsi < 30:
        return 3.0
    if rsi <= 40:
        return 7.0
    if rsi <= params.rsi_short_enter:
        return 8.0
    return 2.0


def _breakout_score(dist: float) -> float:
    if dist >= 0.5:
        return 10.0
    if dist >= 0.25:
        return 7.0
    if dist >= 0.0:
        return 4.0
    return 0.0


def _adx_score(adx: float, adx_trend: float) -> float:
    if adx >= 40:
        return 15.0
    if adx >= 32:
        return 12.0
    if adx >= adx_trend:
        return 8.0
    return 3.0


def confidence_band(confidence: float) -> str:
    if confidence >= 80:
        return "STRONG"
    if confidence >= 70:
        return "VALID"
    if confidence >= 60:
        return "WEAK"
    return "NO_TRADE"


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


def _fmt(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.6f}"


def _join_comps(comps: dict) -> str:
    return ";".join(f"{k}={v:.1f}" for k, v in comps.items())


def regime_pullback_signals(
    bars: Sequence[MarketPrice],
    params: RegimePullbackParams | None = None,
    **overrides,
) -> list[SignalResult]:
    """Decision-time signals for the whole series; one entry per bar."""
    p = _coerce_params(params, overrides)
    n = len(bars)
    if n == 0:
        return []
    (
        closes, highs, lows,
        ema_fast, ema_mid, ema_slow, rsi, atr, atr_ratio,
        ema_mid_slope, ema_slow_slope, swing_high, swing_low,
        prev_high, prev_low, prev_close, adx,
    ) = _pair_lists(bars, p)
    days = DayMaps.build(bars)

    ready = _first_ready(n, ema_slow, adx, atr_ratio)

    signals: list[SignalResult] = []
    state = 0
    entry_i = -1
    entry_stop = 0.0
    entry_target = 0.0

    for i, bridge in enumerate(bars):
        meta = _feature_meta(i, bars, p, days, ema_fast, ema_mid, ema_slow, rsi,
                             atr, atr_ratio, adx, ema_mid_slope, ema_slow_slope,
                             swing_high, swing_low, prev_high, prev_low, prev_close)
        if i < ready:
            meta["warmup"] = "1"
            signals.append(_hold(bridge, "warmup", meta))
            continue

        close = float(bridge.close)
        minute = days.minute_of[i]

        if state == 1:
            trigger = _long_exit(i, close, minute, p, ema_fast, ema_mid, ema_slow,
                                 adx, ema_slow_slope, entry_stop, entry_target, entry_i)
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
            trigger = _short_exit(i, close, minute, p, ema_fast, ema_mid, ema_slow,
                                  adx, ema_slow_slope, entry_stop, entry_target, entry_i)
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

        long_try = _long_entry(i, close, lows[i], minute, p, closes, lows, highs,
                               ema_fast, ema_mid, ema_slow, rsi, atr, atr_ratio,
                               adx, ema_slow_slope, swing_high, prev_high)
        short_try = _short_entry(i, close, highs[i], minute, p, closes, highs, lows,
                                 ema_fast, ema_mid, ema_slow, rsi, atr, atr_ratio,
                                 adx, ema_slow_slope, swing_low, prev_low)

        if long_try is not None and long_try[0]:
            ok, side, mode, confidence, comps, stop_, target_, trigger_reason = long_try
        elif short_try is not None and short_try[0]:
            ok, side, mode, confidence, comps, stop_, target_, trigger_reason = short_try
        else:
            ok = False
            side = "SHORT" if (short_try and short_try[1]) else "LONG"
            mode = ""
            confidence = 0.0
            comps = {}
            stop_ = 0.0
            target_ = 0.0
            trigger_reason = "|".join(
                [r for r in ((long_try[1] if long_try else None), (short_try[1] if short_try else None)) if r]
            )

        if ok:
            entry_i = i
            entry_stop = stop_
            entry_target = target_
            state = 1 if side == "LONG" else -1
            signal = Signal.BUY if side == "LONG" else Signal.SELL
            meta.update({
                "entry": side,
                "entry_mode": mode,
                "confidence": f"{confidence:.1f}",
                "confidence_band": confidence_band(confidence),
                "confidence_components": _join_comps(comps),
                "stop_ref": _fmt(stop_),
                "target_ref": _fmt(target_),
                "entry_atr": _fmt(atr[i]),
                "entry_ref": _fmt(close),
            })
            signals.append(SignalResult(
                signal=signal, instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"{side} {mode} conf={confidence:.1f}", meta=meta,
            ))
        else:
            meta["no_trade_reasons"] = trigger_reason or "NONE_ACTIONABLE"
            meta["no_trade_primary"] = (trigger_reason or "NONE_ACTIONABLE").split("|")[0]
            signals.append(_hold(bridge, "no trade", meta))
    return signals


def _coerce_params(params, overrides) -> RegimePullbackParams:
    base = params if params is not None else RegimePullbackParams()
    if not overrides:
        return base
    allowed = set(RegimePullbackParams.__dataclass_fields__)
    bad = sorted(set(overrides) - allowed)
    if bad:
        raise ValueError(f"unknown RegimePullbackParams knob(s): {bad}")
    merged = {f: getattr(base, f) for f in RegimePullbackParams.__dataclass_fields__}
    merged.update(overrides)
    return RegimePullbackParams(**merged)


def _first_ready(n, ema_slow, adx, atr_ratio) -> int:
    for i in range(n):
        if ema_slow[i] is not None and adx[i] is not None and atr_ratio[i] is not None:
            return i
    return n


def _hold(bridge, reason: str, meta: dict) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD, instrument=bridge.instrument,
        timestamp=bridge.timestamp, reason=reason, meta=meta,
    )


def _feature_meta(i, bars, p, days, ema_fast, ema_mid, ema_slow, rsi, atr,
                  atr_ratio, adx, ema_mid_slope, ema_slow_slope, swing_high,
                  swing_low, prev_high, prev_low, prev_close) -> dict:
    close = float(bars[i].close)
    regime = regime_of(close, ema_fast[i], ema_mid[i], ema_slow[i], adx[i],
                       ema_slow_slope[i], p.adx_trend)
    vol = volatility_bucket_of(atr_ratio[i], p)
    o_high = days.or_high[i]
    o_low = days.or_low[i]
    return {
        "time": bars[i].timestamp.isoformat(),
        "regime": regime,
        "ema_fast": _fmt(ema_fast[i]),
        "ema_mid": _fmt(ema_mid[i]),
        "ema_slow": _fmt(ema_slow[i]),
        "rsi": _fmt(rsi[i]),
        "adx": _fmt(adx[i]),
        "atr": _fmt(atr[i]),
        "atr_ratio": _fmt(atr_ratio[i]),
        "volatility_bucket": vol,
        "ema_mid_slope": _fmt(ema_mid_slope[i]),
        "ema_slow_slope": _fmt(ema_slow_slope[i]),
        "swing_high": _fmt(swing_high[i]),
        "swing_low": _fmt(swing_low[i]),
        "prev_high": _fmt(prev_high[i]),
        "prev_low": _fmt(prev_low[i]),
        "day_open": _fmt(days.day_open[i]),
        "prev_close": _fmt(days.prev_day_close[i]),
        "open_high": _fmt(o_high),
        "open_low": _fmt(o_low),
        "gap_pct": _fmt(days.gap_pct[i]),
    }


def _long_entry(i, close, low, minute, p, closes, lows, highs,
                ema_fast, ema_mid, ema_slow, rsi, atr, atr_ratio,
                adx, ema_slow_slope, swing_high, prev_high):
    if minute < p.entry_open_minute:
        return _no("TIME_NOT_OPEN")
    if minute > p.entry_cutoff_minute:
        return _no("TIME_CUTOFF")
    regime = regime_of(close, ema_fast[i], ema_mid[i], ema_slow[i], adx[i],
                       ema_slow_slope[i], p.adx_trend)
    if regime != REGIME_BULLISH:
        return _no("SIDEWAYS_MARKET")
    vol = volatility_bucket_of(atr_ratio[i], p)
    if vol == VOL_LOW:
        return _no("LOW_VOLATILITY")
    if vol == VOL_EXTREME:
        return _no("EXTREME_VOLATILITY")
    if i < 1:
        return _no("NO_PULLBACK")

    pull = _pullback_bar_count(i, closes, ema_fast, p, below=True)
    zone = _zone_touch_long(i, ema_fast[i], low, atr[i], p)
    breakout_ref = swing_high[i]
    breakout_ok = (
        breakout_ref is not None
        and close > breakout_ref
        and adx[i] is not None
        and adx[i] >= p.adx_breakout
        and vol in p.breakout_vol_buckets
    )
    pullback_ok = (
        pull is not None
        and zone
        and ema_mid[i] is not None
        and ema_slow[i] is not None
        and close > (ema_mid[i] or close)
        and (ema_fast[i] or 0.0) > (ema_slow[i] or 0.0)
        and _momentum_long(i, close, rsi, ema_fast, prev_high, p)
    )
    if not breakout_ok and not pullback_ok:
        reason = _entry_fail_long(i, close, p, ema_fast, ema_mid, ema_slow, rsi,
                                  prev_high, closes, lows, atr, breakout_ref, vol)
        return _no(reason)
    mode = "BREAKOUT" if breakout_ok else "PULLBACK_MOMENTUM"
    atr_v = float(atr[i]) if atr[i] is not None else 0.0
    if atr_v <= 0:
        return _no("POOR_RISK_REWARD")
    stop_ = close - p.stop_atr * atr_v
    target_ = close + p.target_atr * atr_v
    confidence, comps = confidence_of(
        side="CALL", regime=regime, adx=adx[i],
        ema_fast=ema_fast[i], ema_mid=ema_mid[i], ema_slow=ema_slow[i],
        close=close, atr=atr_v, rsi=rsi[i], mode=mode, breakout_ref=breakout_ref,
        vol_bucket=vol, params=p, minute=minute,
    )
    if confidence < p.confidence_min:
        return _no("INSUFFICIENT_EXPECTED_EDGE")
    return (True, "LONG", mode, confidence, comps, stop_, target_, None)


def _short_entry(i, close, high, minute, p, closes, highs, lows,
                 ema_fast, ema_mid, ema_slow, rsi, atr, atr_ratio,
                 adx, ema_slow_slope, swing_low, prev_low):
    if minute < p.entry_open_minute:
        return _no("TIME_NOT_OPEN")
    if minute > p.entry_cutoff_minute:
        return _no("TIME_CUTOFF")
    regime = regime_of(close, ema_fast[i], ema_mid[i], ema_slow[i], adx[i],
                       ema_slow_slope[i], p.adx_trend)
    if regime != REGIME_BEARISH:
        return _no("SIDEWAYS_MARKET")
    vol = volatility_bucket_of(atr_ratio[i], p)
    if vol == VOL_LOW:
        return _no("LOW_VOLATILITY")
    if vol == VOL_EXTREME:
        return _no("EXTREME_VOLATILITY")
    if i < 1:
        return _no("NO_PULLBACK")

    pull = _pullback_bar_count(i, closes, ema_fast, p, below=False)
    zone = _zone_touch_short(i, ema_fast[i], high, atr[i], p)
    breakout_ref = swing_low[i]
    breakout_ok = (
        breakout_ref is not None
        and close < breakout_ref
        and adx[i] is not None
        and adx[i] >= p.adx_breakout
        and vol in p.breakout_vol_buckets
    )
    pullback_ok = (
        pull is not None
        and zone
        and ema_mid[i] is not None
        and ema_slow[i] is not None
        and close < (ema_mid[i] or close)
        and (ema_fast[i] or 0.0) < (ema_slow[i] or 0.0)
        and _momentum_short(i, close, rsi, ema_fast, prev_low, p)
    )
    if not breakout_ok and not pullback_ok:
        reason = _entry_fail_short(i, close, p, ema_fast, ema_mid, ema_slow, rsi,
                                   prev_low, closes, highs, atr, breakout_ref, vol)
        return _no(reason)
    mode = "BREAKOUT" if breakout_ok else "PULLBACK_MOMENTUM"
    atr_v = float(atr[i]) if atr[i] is not None else 0.0
    if atr_v <= 0:
        return _no("POOR_RISK_REWARD")
    stop_ = close + p.stop_atr * atr_v
    target_ = close - p.target_atr * atr_v
    confidence, comps = confidence_of(
        side="PUT", regime=regime, adx=adx[i],
        ema_fast=ema_fast[i], ema_mid=ema_mid[i], ema_slow=ema_slow[i],
        close=close, atr=atr_v, rsi=rsi[i], mode=mode, breakout_ref=breakout_ref,
        vol_bucket=vol, params=p, minute=minute,
    )
    if confidence < p.confidence_min:
        return _no("INSUFFICIENT_EXPECTED_EDGE")
    return (True, "SHORT", mode, confidence, comps, stop_, target_, None)


def _pullback_bar_count(i, closes, ema_fast, p, *, below: bool) -> int | None:
    count = 0
    j = i - 1
    while j >= 0 and ema_fast[j] is not None:
        if below:
            if closes[j] > ema_fast[j]:
                break
        else:
            if closes[j] < ema_fast[j]:
                break
        count += 1
        j -= 1
        if count > p.pullback_max_bars:
            return None
    return count if 1 <= count <= p.pullback_max_bars else None


def _zone_touch_long(i, ema_fast_v, low, atr_v, p) -> bool:
    if ema_fast_v is None or atr_v is None:
        return False
    return ema_fast_v - low <= p.pullback_zone_atr * atr_v


def _zone_touch_short(i, ema_fast_v, high, atr_v, p) -> bool:
    if ema_fast_v is None or atr_v is None:
        return False
    return high - ema_fast_v <= p.pullback_zone_atr * atr_v


def _momentum_long(i, close, rsi, ema_fast, prev_high, p) -> bool:
    if rsi[i] is None or prev_high[i] is None or ema_fast[i] is None:
        return False
    return (
        rsi[i] >= p.rsi_long_enter
        and close > ema_fast[i]
        and close > prev_high[i]
    )


def _momentum_short(i, close, rsi, ema_fast, prev_low, p) -> bool:
    if rsi[i] is None or prev_low[i] is None or ema_fast[i] is None:
        return False
    return (
        rsi[i] <= p.rsi_short_enter
        and close < ema_fast[i]
        and close < prev_low[i]
    )


def _entry_fail_long(i, close, p, ema_fast, ema_mid, ema_slow, rsi, prev_high,
                     closes, lows, atr, breakout_ref, vol) -> str:
    reasons: list[str] = []
    if not _momentum_long(i, close, rsi, ema_fast, prev_high, p):
        reasons.append("NO_MOMENTUM")
    if (
        ema_mid[i] is None
        or ema_slow[i] is None
        or close <= (ema_mid[i] or close)
        or (ema_fast[i] or 0.0) <= (ema_slow[i] or 0.0)
    ):
        reasons.append("SIDEWAYS_MARKET")
    if _pullback_bar_count(i, closes, ema_fast, p, below=True) is None:
        reasons.append("NO_PULLBACK")
    if not _zone_touch_long(i, ema_fast[i], lows[i], atr[i], p):
        reasons.append("NO_PULLBACK")
    if breakout_ref is None or close <= breakout_ref:
        reasons.append("NO_BREAKOUT")
    if vol == VOL_HIGH:
        reasons.append("HIGH_COST")
    return "|".join(dict.fromkeys(reasons)) or "NONE_ACTIONABLE"


def _entry_fail_short(i, close, p, ema_fast, ema_mid, ema_slow, rsi, prev_low,
                      closes, highs, atr, breakout_ref, vol) -> str:
    reasons: list[str] = []
    if not _momentum_short(i, close, rsi, ema_fast, prev_low, p):
        reasons.append("NO_MOMENTUM")
    if (
        ema_mid[i] is None
        or ema_slow[i] is None
        or close >= (ema_mid[i] or close)
        or (ema_fast[i] or 0.0) >= (ema_slow[i] or 0.0)
    ):
        reasons.append("SIDEWAYS_MARKET")
    if _pullback_bar_count(i, closes, ema_fast, p, below=False) is None:
        reasons.append("NO_PULLBACK")
    if not _zone_touch_short(i, ema_fast[i], highs[i], atr[i], p):
        reasons.append("NO_PULLBACK")
    if breakout_ref is None or close >= breakout_ref:
        reasons.append("NO_BREAKOUT")
    if vol == VOL_HIGH:
        reasons.append("HIGH_COST")
    return "|".join(dict.fromkeys(reasons)) or "NONE_ACTIONABLE"


def _no(reason: str):
    return (False, reason, "", 0.0, {}, 0.0, 0.0, None)


def _long_exit(i, close, minute, p, ema_fast, ema_mid, ema_slow, adx,
               ema_slow_slope, entry_stop, entry_target, entry_i) -> str | None:
    if minute >= p.force_exit_minute:
        return "TIME"
    if close <= entry_stop:
        return "STOP"
    if close >= entry_target:
        return "TARGET"
    if i - entry_i >= p.max_hold_bars:
        return "MAXHOLD"
    if not p.exit_on_regime_break:
        return None
    regime = regime_of(close, ema_fast[i], ema_mid[i], ema_slow[i], adx[i],
                       ema_slow_slope[i], p.adx_trend)
    if regime != REGIME_BULLISH:
        return "REGIME_BREAK"
    return None


def _short_exit(i, close, minute, p, ema_fast, ema_mid, ema_slow, adx,
                ema_slow_slope, entry_stop, entry_target, entry_i) -> str | None:
    if minute >= p.force_exit_minute:
        return "TIME"
    if close >= entry_stop:
        return "STOP"
    if close <= entry_target:
        return "TARGET"
    if i - entry_i >= p.max_hold_bars:
        return "MAXHOLD"
    if not p.exit_on_regime_break:
        return None
    regime = regime_of(close, ema_fast[i], ema_mid[i], ema_slow[i], adx[i],
                       ema_slow_slope[i], p.adx_trend)
    if regime != REGIME_BEARISH:
        return "REGIME_BREAK"
    return None


# ---------------------------------------------------------------------------
# strategy-interface wrapper
# ---------------------------------------------------------------------------


class RegimePullbackStrategy(Strategy):
    """Strategy-interface wrapper for :func:`regime_pullback_signals`."""

    name = "model_1_regime_pullback"

    def __init__(
        self,
        params: RegimePullbackParams | None = None,
        **overrides,
    ) -> None:
        self.params = _coerce_params(params, overrides)

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return regime_pullback_signals(bars, params=self.params)[-1]