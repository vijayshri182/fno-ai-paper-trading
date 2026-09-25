"""Discovery generation-0 candidates — six fundamentally different families.

Part of the Algorithm Discovery & Competition Engine. Each candidate is a
pure, decision-time, daily-reset signal provider (one :class:`SignalResult`
per bar) plus a :class:`Strategy` wrapper whose ``analyze`` returns the
provider's last signal for the given prefix (tested for equivalence).

Design rules
------------
* BUY = CALL (open long), SELL = PUT (open short), HOLD = NO_TRADE.
* No look-ahead: the signal for bar ``i`` depends only on ``bars[:i+1]``.
* Daily reset: every day starts flat, matching the fresh-portfolio daily
  replay contract; positions are force-exited at the session close so the
  engine always squares the day (end-of-day square-off is real).
* Times: entries only inside a documented window; a ``TIME_CUTOFF`` HOLD is
  emitted once the entry window closes.
* NO_TRADE is a first-class outcome; reasons reuse the frozen code set.
* Each family is fundamentally different from the others (different
  hypothesis), not a parameter variation of a single mechanism.

Families
--------
d1_trend_ema          TREND_FOLLOWING — EMA stack + slope alignment with ADX.
d2_momentum           MOMENTUM — RSI + ROC + acceleration confirmation.
d3_breakout_vol       BREAKOUT — Donchian range breakout w/ ATR-percentile
                      volatility expansion filter.
d4_pullback           PULLBACK — EMA trend + pullback into band + RSI recovery.
d5_mean_reversion     MEAN_REVERSION — Bollinger Z-score fade, volatility-gated.
d6_structure          MARKET_STRUCTURE — swing high/low + break of structure
                      on range compression/expansion.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.strategies.regime_pullback import (
    RegimePullbackParams,
    _adx,
    _atr,
    _rsi,
    volatility_bucket_of,
)

_VOL_BUCKETS = RegimePullbackParams()

_DEFAULT_ENTRY_OPEN = 9 * 60 + 25
_DEFAULT_ENTRY_CUTOFF = 14 * 60 + 30
_DEFAULT_FORCE_EXIT = 15 * 60 + 15


def _f(value: Decimal | float) -> float:
    return float(value)


def _minute(bar: MarketPrice) -> int:
    return bar.timestamp.hour * 60 + bar.timestamp.minute


def _emac(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if not closes:
        return out
    k = 2.0 / (period + 1)
    ema: float | None = None
    for i, close in enumerate(closes):
        ema = close if ema is None else close * k + ema * (1.0 - k)
        if i >= period - 1:
            out[i] = ema
    return out


def _smac(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    running = sum(closes[: period - 1])
    for i in range(period - 1, len(closes)):
        running += closes[i]
        out[i] = running / period
        running -= closes[i - (period - 1)]
    return out


def _roc(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        prior = closes[i - period]
        out[i] = (closes[i] - prior) / prior if prior else None
    return out


def _hold(bridge: MarketPrice, reason: str, meta: Mapping) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD,
        instrument=bridge.instrument,
        timestamp=bridge.timestamp,
        reason=reason,
        meta=dict(meta),
    )


def _close_signal(bridge: MarketPrice, state: int, reason: str, meta: Mapping) -> SignalResult:
    """Emit the closing signal for an open long (+1) or short (-1)."""
    signal = Signal.SELL if state > 0 else Signal.BUY
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


def _params_dict(params) -> dict:
    return asdict(params)


# ---------------------------------------------------------------------------
# shared: volatility-percentile day-range arrays (decision-time)
# ---------------------------------------------------------------------------


def _day_range_arrays(bars: Sequence[MarketPrice]) -> tuple[list[float], (list[float | None])]:
    n = len(bars)
    day_range: list[float] = [0.0] * n
    avg_range: list[float | None] = [None] * n
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    completed: list[float] = []
    last_range = 0.0
    running_hi: float | None = None
    running_lo: float | None = None
    for i in range(n):
        if i > 0 and bars[i].timestamp.date() != bars[i - 1].timestamp.date():
            completed.append(last_range)
            if len(completed) > 20:
                completed.pop(0)
            running_hi = None
            running_lo = None
        running_hi = highs[i] if running_hi is None else max(running_hi, highs[i])
        running_lo = lows[i] if running_lo is None else min(running_lo, lows[i])
        day_range[i] = running_hi - running_lo
        last_range = day_range[i]
        if completed:
            avg_range[i] = sum(completed) / len(completed)
    return day_range, avg_range


# ---------------------------------------------------------------------------
# d1 — TREND_FOLLOWING: EMA stack + slope + ADX
# ---------------------------------------------------------------------------


@dataclass
class EMATrendParams:
    fast: int = 10
    mid: int = 30
    slow: int = 60
    adx_period: int = 14
    adx_trend: float = 20.0
    atr_period: int = 14
    stop_atr: float = 2.5
    entry_open_minute: int = _DEFAULT_ENTRY_OPEN
    entry_cutoff_minute: int = _DEFAULT_ENTRY_CUTOFF
    force_exit_minute: int = _DEFAULT_FORCE_EXIT
    confidence_threshold: float = 60.0

    def as_dict(self) -> dict:
        return _params_dict(self)


def ema_trend_signals(bars: Sequence[MarketPrice], params: EMATrendParams) -> list[SignalResult]:
    if len(bars) == 0:
        return []
    n = len(bars)
    closes = [_f(b.close) for b in bars]
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    fast = _emac(closes, params.fast)
    mid = _emac(closes, params.mid)
    slow = _emac(closes, params.slow)
    slope = _pct_slope_list(mid, 5)
    adx, _, _ = _adx(highs, lows, closes, params.adx_period)
    atr = _atr(highs, lows, closes, params.atr_period)
    vol_bucket = _vol_bucket_list(atr, closes)
    warmup = params.slow + params.adx_period
    state = 0
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        if i == 0 or bars[i - 1].timestamp.date() != day:
            state = 0
        meta = _base_meta(bridge, i, n)
        meta.update({
            "ema_fast": _fmt(fast[i]),
            "ema_mid": _fmt(mid[i]),
            "ema_slow": _fmt(slow[i]),
            "adx": _fmt(adx[i]),
            "atr": _fmt(atr[i]),
            "volatility_bucket": vol_bucket[i],
            "regime": (_regime_of(fast[i], mid[i], slow[i]) or "UNKNOWN"),
        })
        if i < warmup or fast[i] is None or mid[i] is None or slow[i] is None or adx[i] is None or atr[i] is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        minute = _minute(bridge)
        if state != 0:
            if minute >= params.force_exit_minute:
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, f"force-exit EOD {day:%Y-%m-%d}", meta)
                continue
            if state > 0 and fast[i] <= mid[i]:
                state = 0
                signals[i] = _close_signal(bridge, 1, "fast<mid trend break exits long", meta)
                continue
            if state < 0 and fast[i] >= mid[i]:
                state = 0
                signals[i] = _close_signal(bridge, -1, "fast>mid trend break exits short", meta)
                continue
            if state > 0 and fast[i] > mid[i]:
                stop = Decimal(str(fast[i])) - Decimal(str(params.stop_atr)) * Decimal(str(atr[i]))
                if bridge.close < stop:
                    state = 0
                    signals[i] = _close_signal(bridge, 1, "ATR trailing stop exits long", meta)
                    continue
            if state < 0 and fast[i] < mid[i]:
                stop = Decimal(str(fast[i])) + Decimal(str(params.stop_atr)) * Decimal(str(atr[i]))
                if bridge.close > stop:
                    state = 0
                    signals[i] = _close_signal(bridge, -1, "ATR trailing stop exits short", meta)
                    continue
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if minute > params.entry_cutoff_minute:
            meta["no_trade_primary"] = "TIME_CUTOFF"
            signals[i] = _hold(bridge, "TIME_CUTOFF", meta)
            continue
        if not _trend_ok(fast, mid, slow, slope, adx, i, up=True) and not _trend_ok(fast, mid, slow, slope, adx, i, up=False):
            meta["no_trade_primary"] = "WEAK_TREND"
            signals[i] = _hold(bridge, "WEAK_TREND", meta)
            continue
        if _trend_ok(fast, mid, slow, slope, adx, i, up=True):
            state = 1
            signals[i] = SignalResult(signal=Signal.BUY, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="EMA stack aligned up + ADX", meta=meta)
        else:
            state = -1
            signals[i] = SignalResult(signal=Signal.SELL, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="EMA stack aligned down + ADX", meta=meta)
    return signals


class EMATrendStrategy(Strategy):
    name = "d1_trend_ema"

    def __init__(self, params: EMATrendParams | None = None) -> None:
        self.params = params or EMATrendParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return ema_trend_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# shared trend bits
# ---------------------------------------------------------------------------


def _pct_slope_list(values: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(window, len(values)):
        if values[i] is None or values[i - window] is None:
            continue
        prior = values[i - window]
        if prior:
            out[i] = (values[i] - prior) / prior
    return out


def _regime_of(fast, mid, slow) -> str | None:
    if fast is None or slow is None:
        return None
    if mid is None:
        if fast > slow:
            return "BULLISH"
        if fast < slow:
            return "BEARISH"
        return "SIDEWAYS"
    if fast > mid > slow:
        return "BULLISH"
    if fast < mid < slow:
        return "BEARISH"
    return "SIDEWAYS"


def _trend_ok(fast, mid, slow, slope, adx, i, *, up: bool) -> bool:
    """EMA stack + 5-bar slope + ADX confirmation for one direction."""
    if adx[i] is None or adx[i] < 18.0:
        return False
    if up:
        aligned = fast[i] > mid[i] > slow[i]
        rising = slope[i] is not None and slope[i] > 0
        strength = adx[i] >= 20.0
        return aligned and rising and strength
    aligned = fast[i] < mid[i] < slow[i]
    falling = slope[i] is not None and slope[i] < 0
    strength = adx[i] >= 20.0
    return aligned and falling and strength


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return f"{value:.4f}" if isinstance(value, float) else str(value)


# ---------------------------------------------------------------------------
# d2 — MOMENTUM: RSI + ROC + acceleration
# ---------------------------------------------------------------------------


@dataclass
class MomentumParams:
    rsi_period: int = 14
    roc_period: int = 10
    accel_period: int = 5
    rsi_long_enter: float = 50.0
    rsi_short_enter: float = 50.0
    entry_open_minute: int = _DEFAULT_ENTRY_OPEN
    entry_cutoff_minute: int = _DEFAULT_ENTRY_CUTOFF
    force_exit_minute: int = _DEFAULT_FORCE_EXIT
    confidence_threshold: float = 60.0

    def as_dict(self) -> dict:
        return _params_dict(self)


def momentum_signals(bars: Sequence[MarketPrice], params: MomentumParams) -> list[SignalResult]:
    if len(bars) == 0:
        return []
    n = len(bars)
    closes = [_f(b.close) for b in bars]
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    rsi = _rsi(closes, params.rsi_period)
    roc = _roc(closes, params.roc_period)
    roc_fast = _roc(closes, params.accel_period)
    atr = _atr(highs, lows, closes, 14)
    vol_bucket = _vol_bucket_list(atr, closes)
    warmup = max(params.rsi_period, params.roc_period, params.accel_period) + 1
    state = 0
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        if i == 0 or bars[i - 1].timestamp.date() != day:
            state = 0
        meta = _base_meta(bridge, i, n)
        meta.update({
            "rsi": _fmt(rsi[i]),
            "roc": _fmt(roc[i]),
            "acceleration": _fmt(_accel_value(roc, roc_fast, i)),
            "volatility_bucket": vol_bucket[i],
        })
        if i < warmup or rsi[i] is None or roc[i] is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        minute = _minute(bridge)
        if state != 0:
            if minute >= params.force_exit_minute:
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, f"force-exit EOD {day:%Y-%m-%d}", meta)
                continue
            if state > 0 and not _long_momentum(rsi[i], roc[i], _accel_value(roc, roc_fast, i), params):
                state = 0
                signals[i] = _close_signal(bridge, 1, "momentum faded exits long", meta)
                continue
            if state < 0 and not _short_momentum(rsi[i], roc[i], _accel_value(roc, roc_fast, i), params):
                state = 0
                signals[i] = _close_signal(bridge, -1, "momentum faded exits short", meta)
                continue
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if minute > params.entry_cutoff_minute:
            meta["no_trade_primary"] = "TIME_CUTOFF"
            signals[i] = _hold(bridge, "TIME_CUTOFF", meta)
            continue
        if _long_momentum(rsi[i], roc[i], _accel_value(roc, roc_fast, i), params):
            state = 1
            signals[i] = SignalResult(signal=Signal.BUY, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="RSI+ROC+acceleration confirm up", meta=meta)
        elif _short_momentum(rsi[i], roc[i], _accel_value(roc, roc_fast, i), params):
            state = -1
            signals[i] = SignalResult(signal=Signal.SELL, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="RSI+ROC+acceleration confirm down", meta=meta)
        else:
            meta["no_trade_primary"] = "NO_MOMENTUM"
            signals[i] = _hold(bridge, "NO_MOMENTUM", meta)
    return signals


def _accel_value(roc, roc_fast, i) -> float | None:
    if roc[i] is None or roc_fast[i] is None:
        return None
    return roc[i] - roc_fast[i]


def _long_momentum(rsi, roc, accel, params: MomentumParams) -> bool:
    if rsi is None or roc is None:
        return False
    return rsi > params.rsi_long_enter and roc > 0 and (accel is None or accel > 0)


def _short_momentum(rsi, roc, accel, params: MomentumParams) -> bool:
    if rsi is None or roc is None:
        return False
    return rsi < params.rsi_short_enter and roc < 0 and (accel is None or accel < 0)


class MomentumStrategy(Strategy):
    name = "d2_momentum"

    def __init__(self, params: MomentumParams | None = None) -> None:
        self.params = params or MomentumParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return momentum_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# d3 — BREAKOUT: Donchian + volatility expansion
# ---------------------------------------------------------------------------


@dataclass
class VolBreakoutParams:
    entry_channel: int = 20
    exit_channel: int = 10
    range_expansion: float = 1.0       # today's range / avg 20-day range at decision time
    atr_period: int = 14
    trail_atr: float = 2.0
    entry_open_minute: int = 9 * 60 + 45
    entry_cutoff_minute: int = 14 * 60
    force_exit_minute: int = 15 * 60 + 10
    confidence_threshold: float = 60.0

    def as_dict(self) -> dict:
        return _params_dict(self)


def vol_breakout_signals(bars: Sequence[MarketPrice], params: VolBreakoutParams) -> list[SignalResult]:
    if len(bars) == 0:
        return []
    n = len(bars)
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    closes = [_f(b.close) for b in bars]
    atr = _atr(highs, lows, closes, params.atr_period)
    day_range, avg_range = _day_range_arrays(bars)
    vol_bucket = _vol_bucket_list(atr, closes)
    warmup = params.entry_channel + params.atr_period
    state = 0
    best_long_close: float | None = None
    best_short_close: float | None = None
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        if i == 0 or bars[i - 1].timestamp.date() != day:
            state = 0
            best_long_close = None
            best_short_close = None
        meta = _base_meta(bridge, i, n)
        meta.update({
            "atr": _fmt(atr[i]),
            "volatility_bucket": vol_bucket[i],
            "day_range_ratio": _fmt(day_range[i] / avg_range[i]) if (avg_range[i] and avg_range[i] > 0) else "",
            "regime": _breakout_regime(day_range[i], avg_range[i]),
        })
        if i < warmup or atr[i] is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        minute = _minute(bridge)
        prior_hi = max(highs[i - params.entry_channel:i], default=bridge.high)
        prior_lo = min(lows[i - params.entry_channel:i], default=bridge.low)
        exit_lo = min(lows[i - params.exit_channel:i], default=bridge.low)
        exit_hi = max(highs[i - params.exit_channel:i], default=bridge.high)
        meta.update({"entry_hi_ref": _fmt(prior_hi), "entry_lo_ref": _fmt(prior_lo)})
        expanded = avg_range[i] is not None and avg_range[i] > 0 and (day_range[i] / avg_range[i]) >= params.range_expansion
        if state != 0:
            if minute >= params.force_exit_minute:
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, f"force-exit EOD {day:%Y-%m-%d}", meta)
                continue
            if state > 0:
                best_long_close = closes[i] if best_long_close is None else max(best_long_close, closes[i])
            else:
                best_short_close = closes[i] if best_short_close is None else min(best_short_close, closes[i])
            if state > 0 and bridge.close < exit_lo:
                state = 0
                signals[i] = _close_signal(bridge, 1, "channel retreat exits long", meta)
                continue
            if state < 0 and bridge.close > exit_hi:
                state = 0
                signals[i] = _close_signal(bridge, -1, "channel retreat exits short", meta)
                continue
            if state > 0 and atr[i] is not None and best_long_close is not None:
                stop = Decimal(str(best_long_close)) - Decimal(str(params.trail_atr)) * Decimal(str(atr[i]))
                if bridge.close < stop:
                    state = 0
                    signals[i] = _close_signal(bridge, 1, "ATR trailing stop exits long", meta)
                    continue
            if state < 0 and atr[i] is not None and best_short_close is not None:
                stop = Decimal(str(best_short_close)) + Decimal(str(params.trail_atr)) * Decimal(str(atr[i]))
                if bridge.close > stop:
                    state = 0
                    signals[i] = _close_signal(bridge, -1, "ATR trailing stop exits short", meta)
                    continue
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if minute < params.entry_open_minute:
            meta["no_trade_primary"] = "TIME_NOT_OPEN"
            signals[i] = _hold(bridge, "TIME_NOT_OPEN", meta)
            continue
        if minute > params.entry_cutoff_minute:
            meta["no_trade_primary"] = "TIME_CUTOFF"
            signals[i] = _hold(bridge, "TIME_CUTOFF", meta)
            continue
        if not expanded:
            meta["no_trade_primary"] = "LOW_VOLATILITY"
            signals[i] = _hold(bridge, "LOW_VOLATILITY", meta)
            continue
        if bridge.close > prior_hi:
            state = 1
            signals[i] = SignalResult(signal=Signal.BUY, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="breakout high + range expansion", meta=meta)
        elif bridge.close < prior_lo:
            state = -1
            signals[i] = SignalResult(signal=Signal.SELL, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="breakout low + range expansion", meta=meta)
        else:
            meta["no_trade_primary"] = "NO_BREAKOUT"
            signals[i] = _hold(bridge, "NO_BREAKOUT", meta)
    return signals


def _breakout_regime(day_range: float, avg_range: float | None) -> str:
    if avg_range is None or avg_range <= 0:
        return "UNKNOWN"
    ratio = day_range / avg_range
    if ratio >= 1.5:
        return "EXPANSION"
    if ratio >= 1.0:
        return "NORMAL"
    return "CONTRACTION"


class VolBreakoutStrategy(Strategy):
    name = "d3_breakout_vol"

    def __init__(self, params: VolBreakoutParams | None = None) -> None:
        self.params = params or VolBreakoutParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return vol_breakout_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# d4 — PULLBACK: EMA trend + band pullback + RSI recovery
# ---------------------------------------------------------------------------


@dataclass
class PullbackParams:
    fast: int = 10
    slow: int = 30
    rsi_period: int = 14
    adx_period: int = 14
    adx_trend: float = 18.0
    rsi_recover_long: float = 45.0
    rsi_recover_short: float = 55.0
    atr_period: int = 14
    stop_atr: float = 1.5
    target_atr: float = 2.5
    max_hold_bars: int = 12
    entry_open_minute: int = _DEFAULT_ENTRY_OPEN
    entry_cutoff_minute: int = _DEFAULT_ENTRY_CUTOFF
    force_exit_minute: int = _DEFAULT_FORCE_EXIT
    confidence_threshold: float = 60.0

    def as_dict(self) -> dict:
        return _params_dict(self)


def pullback_signals(bars: Sequence[MarketPrice], params: PullbackParams) -> list[SignalResult]:
    if len(bars) == 0:
        return []
    n = len(bars)
    closes = [_f(b.close) for b in bars]
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    fast = _emac(closes, params.fast)
    slow = _emac(closes, params.slow)
    rsi = _rsi(closes, params.rsi_period)
    adx, _, _ = _adx(highs, lows, closes, params.adx_period)
    atr = _atr(highs, lows, closes, params.atr_period)
    vol_bucket = _vol_bucket_list(atr, closes)
    warmup = params.slow + params.adx_period
    state = 0
    pending_long = False
    pending_short = False
    entry_bar = 0
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        if i == 0 or bars[i - 1].timestamp.date() != day:
            state = 0
            pending_long = False
            pending_short = False
        meta = _base_meta(bridge, i, n)
        meta.update({
            "ema_fast": _fmt(fast[i]),
            "ema_slow": _fmt(slow[i]),
            "rsi": _fmt(rsi[i]),
            "adx": _fmt(adx[i]),
            "atr": _fmt(atr[i]),
            "volatility_bucket": vol_bucket[i],
            "regime": (_regime_of(fast[i], None, slow[i]) or "UNKNOWN"),
        })
        if i < warmup or fast[i] is None or slow[i] is None or atr[i] is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        minute = _minute(bridge)
        if state != 0:
            if minute >= params.force_exit_minute or (params.max_hold_bars and i - entry_bar >= params.max_hold_bars):
                was = state
                state = 0
                pending_long = pending_short = False
                reason = f"force-exit EOD {day:%Y-%m-%d}" if minute >= params.force_exit_minute else "max hold bars"
                signals[i] = _close_signal(bridge, was, reason, meta)
                continue
            atr_v = atr[i]
            entry_price = _entry_price_px(bars, i, entry_bar)
            if atr_v is not None and entry_price is not None:
                if state > 0:
                    stop = Decimal(str(entry_price)) - Decimal(str(params.stop_atr)) * Decimal(str(atr_v))
                    target = Decimal(str(entry_price)) + Decimal(str(params.target_atr)) * Decimal(str(atr_v))
                    if bridge.close < stop:
                        state = 0
                        pending_long = pending_short = False
                        signals[i] = _close_signal(bridge, 1, "stop exits long", meta)
                        continue
                    if bridge.close >= target:
                        state = 0
                        pending_long = pending_short = False
                        signals[i] = _close_signal(bridge, 1, "target exits long", meta)
                        continue
                else:
                    stop = Decimal(str(entry_price)) + Decimal(str(params.stop_atr)) * Decimal(str(atr_v))
                    target = Decimal(str(entry_price)) - Decimal(str(params.target_atr)) * Decimal(str(atr_v))
                    if bridge.close > stop:
                        state = 0
                        pending_long = pending_short = False
                        signals[i] = _close_signal(bridge, -1, "stop exits short", meta)
                        continue
                    if bridge.close <= target:
                        state = 0
                        pending_long = pending_short = False
                        signals[i] = _close_signal(bridge, -1, "target exits short", meta)
                        continue
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if minute > params.entry_cutoff_minute:
            pending_long = pending_short = False
            meta["no_trade_primary"] = "TIME_CUTOFF"
            signals[i] = _hold(bridge, "TIME_CUTOFF", meta)
            continue
        long_setup = _pullback_setup(fast, slow, adx, rsi, i, up=True, params=params)
        short_setup = _pullback_setup(fast, slow, adx, rsi, i, up=False, params=params)
        if state == 0 and not pending_long and not pending_short and long_setup:
            pending_long = True
            pending_short = False
        elif state == 0 and not pending_long and not pending_short and short_setup:
            pending_long = False
            pending_short = True
        if state == 0 and pending_long:
            if _confirm_pullback(fast, slow, rsi, i, up=True, params=params):
                state = 1
                pending_long = False
                entry_bar = i
                signals[i] = SignalResult(signal=Signal.BUY, instrument=bridge.instrument,
                                          timestamp=bridge.timestamp,
                                          reason="pullback RSI recovery -> BUY", meta=meta)
                continue
            meta["no_trade_primary"] = "NO_PULLBACK"
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if state == 0 and pending_short:
            if _confirm_pullback(fast, slow, rsi, i, up=False, params=params):
                state = -1
                pending_short = False
                entry_bar = i
                signals[i] = SignalResult(signal=Signal.SELL, instrument=bridge.instrument,
                                          timestamp=bridge.timestamp,
                                          reason="pullback RSI recovery -> SELL", meta=meta)
                continue
            meta["no_trade_primary"] = "NO_PULLBACK"
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        meta["no_trade_primary"] = "NO_PULLBACK"
        signals[i] = _hold(bridge, "NO_PULLBACK", meta)
    return signals


def _pullback_setup(fast, slow, adx, rsi, i, *, up: bool, params: PullbackParams) -> bool:
    """Trend + pullback: price within the fast/slow band, opposite momentum dip."""
    if fast[i] is None or slow[i] is None or adx[i] is None:
        return False
    if adx[i] < params.adx_trend:
        return False
    if up:
        if not (fast[i] > slow[i]):
            return False
    else:
        if not (fast[i] < slow[i]):
            return False
    return True


def _confirm_pullback(fast, slow, rsi, i, *, up: bool, params: PullbackParams) -> bool:
    if rsi[i] is None:
        return False
    if up:
        return rsi[i] >= params.rsi_recover_long
    return rsi[i] <= params.rsi_recover_short


def _entry_price_px(bars: Sequence[MarketPrice], i: int, entry_bar: int) -> Decimal | None:
    if entry_bar < 0 or entry_bar >= i:
        return None
    return bars[entry_bar].close


class PullbackStrategy(Strategy):
    name = "d4_pullback"

    def __init__(self, params: PullbackParams | None = None) -> None:
        self.params = params or PullbackParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return pullback_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# d5 — MEAN REVERSION: Bollinger Z-score fade, volatility-gated
# ---------------------------------------------------------------------------


@dataclass
class MeanReversionParams:
    bb_period: int = 20
    bb_stdev: float = 2.0
    rsi_period: int = 14
    entry_z_long: float = -1.5
    entry_z_short: float = 1.5
    exit_z: float = 0.3
    atr_period: int = 14
    stop_atr: float = 2.0
    entry_open_minute: int = _DEFAULT_ENTRY_OPEN
    entry_cutoff_minute: int = _DEFAULT_ENTRY_CUTOFF
    force_exit_minute: int = _DEFAULT_FORCE_EXIT
    confidence_threshold: float = 60.0

    def as_dict(self) -> dict:
        return _params_dict(self)


def mean_reversion_signals(bars: Sequence[MarketPrice], params: MeanReversionParams) -> list[SignalResult]:
    if len(bars) == 0:
        return []
    n = len(bars)
    closes = [_f(b.close) for b in bars]
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    atr = _atr(highs, lows, closes, params.atr_period)
    rsi = _rsi(closes, params.rsi_period)
    bb_mid = _smac(closes, params.bb_period)
    bb_std = _bb_std(closes, params.bb_period)
    atr_ratio = _atr_ratio(atr, closes)
    warmup = params.bb_period + params.atr_period
    state = 0
    z_last: float | None = None
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        if i == 0 or bars[i - 1].timestamp.date() != day:
            state = 0
            z_last = None
        meta = _base_meta(bridge, i, n)
        z = _z_score(closes[i], bb_mid[i], bb_std[i])
        bucket = volatility_bucket_of(atr_ratio[i], _VOL_BUCKETS)
        meta.update({
            "z_score": _fmt(z),
            "rsi": _fmt(rsi[i]),
            "atr_ratio": _fmt(atr_ratio[i]),
            "volatility_bucket": bucket,
            "regime": ("BULLISH" if z is not None and z > 0 else "BEARISH") if z is not None else "UNKNOWN",
        })
        if i < warmup or z is None or atr[i] is None or rsi[i] is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        minute = _minute(bridge)
        if state != 0:
            if minute >= params.force_exit_minute:
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, f"force-exit EOD {day:%Y-%m-%d}", meta)
                continue
            if (z is not None and abs(z) <= params.exit_z) or (state > 0 and rsi[i] >= 55) or (state < 0 and rsi[i] <= 45):
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, "mean touch / RSI fade exits", meta)
                continue
            stop = _meanrev_stop(bridge, state, atr[i], params)
            if stop is not None and ((state > 0 and bridge.close < stop) or (state < 0 and bridge.close > stop)):
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, "stop exits mean-reversion", meta)
                continue
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if minute > params.entry_cutoff_minute:
            meta["no_trade_primary"] = "TIME_CUTOFF"
            signals[i] = _hold(bridge, "TIME_CUTOFF", meta)
            continue
        if bucket != "NORMAL":
            if bucket == "EXTREME":
                meta["no_trade_primary"] = "EXTREME_VOLATILITY"
            elif bucket == "HIGH":
                meta["no_trade_primary"] = "POOR_RISK_REWARD"
            else:
                meta["no_trade_primary"] = "LOW_VOLATILITY"
            signals[i] = _hold(bridge, meta["no_trade_primary"], meta)
            continue
        if z <= params.entry_z_long and rsi[i] <= 30:
            state = 1
            z_last = z
            signals[i] = SignalResult(signal=Signal.BUY, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="below-lower-band fade -> BUY", meta=meta)
        elif z >= params.entry_z_short and rsi[i] >= 70:
            state = -1
            z_last = z
            signals[i] = SignalResult(signal=Signal.SELL, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="above-upper-band fade -> SELL", meta=meta)
        else:
            meta["no_trade_primary"] = "SIDEWAYS_MARKET"
            signals[i] = _hold(bridge, "within band -> NO_TRADE", meta)
    return signals


def _bb_std(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        out[i] = variance ** 0.5
    return out


def _z_score(close: float, mid: float | None, std: float | None) -> float | None:
    if mid is None or std is None or std <= 0:
        return None
    return (close - mid) / std


def _vol_bucket_list(atr: list, closes: list) -> list[str]:
    out: list[str] = []
    for i, atr_v in enumerate(atr):
        ratio = atr_v / closes[i] if atr_v is not None and closes[i] else None
        out.append(volatility_bucket_of(ratio, _VOL_BUCKETS))
    return out


def _atr_ratio(atr: list, closes: list) -> list[float | None]:
    out: list[float | None] = [None] * len(atr)
    for i, atr_v in enumerate(atr):
        if atr_v is not None and closes[i]:
            out[i] = atr_v / closes[i]
    return out


def _meanrev_stop(bridge: MarketPrice, state: int, atr_v: float, params: MeanReversionParams) -> Decimal | None:
    if atr_v <= 0:
        return None
    if state > 0:
        return Decimal(str(bridge.close)) - Decimal(str(params.stop_atr)) * Decimal(str(atr_v))
    return Decimal(str(bridge.close)) + Decimal(str(params.stop_atr)) * Decimal(str(atr_v))


class MeanReversionStrategy(Strategy):
    name = "d5_mean_reversion"

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self.params = params or MeanReversionParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return mean_reversion_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# d6 — MARKET STRUCTURE: swing high/low + break of structure
# ---------------------------------------------------------------------------


@dataclass
class StructureParams:
    swing_bars: int = 2
    entry_channel: int = 30
    range_expansion: float = 1.0
    atr_period: int = 14
    stop_atr: float = 1.5
    entry_open_minute: int = 9 * 60 + 45
    entry_cutoff_minute: int = 14 * 60
    force_exit_minute: int = 15 * 60 + 10
    confidence_threshold: float = 60.0

    def as_dict(self) -> dict:
        return _params_dict(self)


def structure_break_signals(bars: Sequence[MarketPrice], params: StructureParams) -> list[SignalResult]:
    if len(bars) == 0:
        return []
    n = len(bars)
    highs = [_f(b.high) for b in bars]
    lows = [_f(b.low) for b in bars]
    closes = [_f(b.close) for b in bars]
    atr = _atr(highs, lows, closes, params.atr_period)
    day_range, avg_range = _day_range_arrays(bars)
    swing_high = _last_swing(highs, params.swing_bars, up=True)
    swing_low = _last_swing(lows, params.swing_bars, up=False)
    vol_bucket = _vol_bucket_list(atr, closes)
    warmup = params.entry_channel + params.atr_period
    state = 0
    best_long_close: float | None = None
    best_short_close: float | None = None
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    for i, bridge in enumerate(bars):
        day = bridge.timestamp.date()
        if i == 0 or bars[i - 1].timestamp.date() != day:
            state = 0
            best_long_close = None
            best_short_close = None
        meta = _base_meta(bridge, i, n)
        sh = swing_high[i]
        sl = swing_low[i]
        meta.update({
            "swing_high": _fmt(sh),
            "swing_low": _fmt(sl),
            "atr": _fmt(atr[i]),
            "volatility_bucket": vol_bucket[i],
            "regime": _structure_regime(day_range[i], avg_range[i]),
        })
        if i < warmup or atr[i] is None or sh is None or sl is None:
            meta["warmup"] = "1"
            signals[i] = _hold(bridge, "WARM_UP", meta)
            continue
        minute = _minute(bridge)
        expanded = avg_range[i] is not None and avg_range[i] > 0 and (day_range[i] / avg_range[i]) >= params.range_expansion
        if state != 0:
            if minute >= params.force_exit_minute:
                was = state
                state = 0
                signals[i] = _close_signal(bridge, was, f"force-exit EOD {day:%Y-%m-%d}", meta)
                continue
            if state > 0:
                best_long_close = closes[i] if best_long_close is None else max(best_long_close, closes[i])
            else:
                best_short_close = closes[i] if best_short_close is None else min(best_short_close, closes[i])
            if state > 0 and (sl is not None and bridge.close < sl):
                state = 0
                signals[i] = _close_signal(bridge, 1, "structure low broke -> exit long", meta)
                continue
            if state < 0 and (sh is not None and bridge.close > sh):
                state = 0
                signals[i] = _close_signal(bridge, -1, "structure high broke -> exit short", meta)
                continue
            if state > 0 and best_long_close is not None:
                stop = Decimal(str(best_long_close)) - Decimal(str(params.stop_atr)) * Decimal(str(atr[i]))
                if bridge.close < stop:
                    state = 0
                    signals[i] = _close_signal(bridge, 1, "ATR stop exits structure long", meta)
                    continue
            if state < 0 and best_short_close is not None:
                stop = Decimal(str(best_short_close)) + Decimal(str(params.stop_atr)) * Decimal(str(atr[i]))
                if bridge.close > stop:
                    state = 0
                    signals[i] = _close_signal(bridge, -1, "ATR stop exits structure short", meta)
                    continue
            signals[i] = _hold(bridge, "HOLDING", meta)
            continue
        if minute < params.entry_open_minute:
            meta["no_trade_primary"] = "TIME_NOT_OPEN"
            signals[i] = _hold(bridge, "TIME_NOT_OPEN", meta)
            continue
        if minute > params.entry_cutoff_minute:
            meta["no_trade_primary"] = "TIME_CUTOFF"
            signals[i] = _hold(bridge, "TIME_CUTOFF", meta)
            continue
        if not expanded:
            meta["no_trade_primary"] = "LOW_VOLATILITY"
            signals[i] = _hold(bridge, "LOW_VOLATILITY", meta)
            continue
        if bridge.close > sh:
            state = 1
            signals[i] = SignalResult(signal=Signal.BUY, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="break of structure (high) on expansion", meta=meta)
        elif bridge.close < sl:
            state = -1
            signals[i] = SignalResult(signal=Signal.SELL, instrument=bridge.instrument,
                                      timestamp=bridge.timestamp,
                                      reason="break of structure (low) on expansion", meta=meta)
        else:
            meta["no_trade_primary"] = "SIDEWAYS_MARKET"
            signals[i] = _hold(bridge, "NO_BREAKOUT", meta)
    return signals


def _last_swing(values: list[float], k: int, *, up: bool) -> list[float | None]:
    """Last confirmed swing extreme (fractal over +/- k bars) as of bar i."""
    n = len(values)
    out: list[float | None] = [None] * n
    last: float | None = None
    for i in range(k, n - k):
        window = values[i - k:i + k + 1]
        if up:
            if values[i] == max(window) and values[i] > values[i - 1]:
                last = values[i]
        else:
            if values[i] == min(window) and values[i] < values[i - 1]:
                last = values[i]
        out[i + k] = last
    return out


def _structure_regime(day_range: float, avg_range: float | None) -> str:
    if avg_range is None or avg_range <= 0:
        return "UNKNOWN"
    ratio = day_range / avg_range
    if ratio >= 1.5:
        return "EXPANSION"
    if ratio >= 1.0:
        return "NORMAL"
    return "CONTRACTION"


class StructureStrategy(Strategy):
    name = "d6_structure"

    def __init__(self, params: StructureParams | None = None) -> None:
        self.params = params or StructureParams()

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return structure_break_signals(bars, self.params)[-1]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

PROVIDERS = {
    "d1_trend_ema": ema_trend_signals,
    "d2_momentum": momentum_signals,
    "d3_breakout_vol": vol_breakout_signals,
    "d4_pullback": pullback_signals,
    "d5_mean_reversion": mean_reversion_signals,
    "d6_structure": structure_break_signals,
}

STRATEGIES = {
    "d1_trend_ema": EMATrendStrategy,
    "d2_momentum": MomentumStrategy,
    "d3_breakout_vol": VolBreakoutStrategy,
    "d4_pullback": PullbackStrategy,
    "d5_mean_reversion": MeanReversionStrategy,
    "d6_structure": StructureStrategy,
}

PARAMS = {
    "d1_trend_ema": EMATrendParams,
    "d2_momentum": MomentumParams,
    "d3_breakout_vol": VolBreakoutParams,
    "d4_pullback": PullbackParams,
    "d5_mean_reversion": MeanReversionParams,
    "d6_structure": StructureParams,
}