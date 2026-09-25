"""Deterministic technical-indicator library (educational, WS 7.18).

Every indicator here is a stateless, pure function of price history: the
vector at index ``i`` depends only on bars ``[: i + 1]`` (never the future),
all arithmetic is Decimal, and warm-up regions are ``None``. Indicators never
place orders or touch risk/execution code — they just turn OHLCV history into
feature series that strategies may consume.

Two conventions:

* Functions take parallel ``Decimal`` sequences (close, high, low, volume ...)
  in chronological order and return a same-length list. Warm-up entries are
  ``None`` (strategy code must treat ``None`` as "no information yet").
* Only timestep-less numbers are returned; timestamps and instruments are out
  of scope here.

Indicators provided:

* Trend: ``sma``, ``ema``, ``macd``, ``adx`` (with +DI/-DI), ``supertrend``.
* Momentum: ``rsi``, ``stochastic``, ``williams_r``, ``mfi``.
* Volatility: ``true_range``, ``atr``, ``bollinger``, ``keltner``.
* Volume: ``obv``, ``vwap``.

These are educational frameworks only — not trade recommendations.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from decimal import Decimal

Number = Decimal | None

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_HUNDRED = Decimal("100")


def _require_length(name: str, *series: Sequence) -> int:
    lengths = {len(values) for values in series}
    if not lengths:
        return 0
    if len(lengths) != 1:
        raise ValueError(f"{name}: all series must have the same length, got {sorted(lengths)}")
    return lengths.pop()


def _sma_of(values: list[Decimal], period: int) -> Decimal:
    return sum(values[-period:], _ZERO) / period


def _wildered(values: list[Decimal], period: int) -> list[Decimal | None]:
    """Wilder's smoothing: simple-average seed at index ``period - 1``, then
    recursing EMA(alpha=1/period). Same length as ``values``; earlier is None.

    The seed is the mean of the *first* ``period`` values (values[0:period]),
    so ``out[i]`` depends only on values ``[: i + 1]`` — the series is causal
    and identical whether computed over a prefix or a longer history.
    """
    if len(values) < period:
        return [None] * len(values)
    seed = sum(values[0:period], _ZERO) / period
    out: list[Decimal | None] = [None] * len(values)
    out[period - 1] = seed
    alpha = _ONE / period
    previous = seed
    for index in range(period, len(values)):
        previous = previous + alpha * (values[index] - previous)
        out[index] = previous
    return out


def _pos_int(period: int, name: str) -> None:
    if not isinstance(period, int) or period <= 0:
        raise ValueError(f"{name} must be a positive integer")


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------


def sma(values: Sequence[Decimal], period: int) -> list[Number]:
    """Simple moving average of ``values``; ``None`` until ``period`` closes.

    Uses a rolling deque so the function is O(n) rather than re-slicing the
    growing prefix per bar (which would be O(n²)).
    """
    _pos_int(period, "period")
    values = list(values)
    n = _require_length("sma", values)
    out: list[Number] = []
    window: deque[Decimal] = deque()
    window_sum = _ZERO
    for i in range(n):
        v = values[i]
        window.append(v)
        window_sum += v
        if len(window) > period:
            window_sum -= window.popleft()
        out.append(window_sum / period if len(window) >= period else None)
    return out


def ema(values: Sequence[Decimal], period: int) -> list[Number]:
    """Exponential moving average seeded by the SMA at index ``period - 1``."""
    _pos_int(period, "period")
    values = list(values)
    n = _require_length("ema", values)
    out: list[Number] = [None] * n
    if n < period:
        return out
    alpha = _TWO / (period + 1)
    out[period - 1] = _sma_of(values[:period], period)
    for i in range(period, n):
        out[i] = out[i - 1] + alpha * (values[i] - out[i - 1])
    return out


def macd(
    values: Sequence[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Number], list[Number], list[Number]]:
    """MACD line, signal line and histogram (MACD - signal).

    ``None`` until the slower EMA plus the signal smoothing have warmed up.
    """
    values = list(values)
    n = _require_length("macd", values)
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    macd_line: list[Number] = [
        (fast_line[i] - slow_line[i]) if fast_line[i] is not None and slow_line[i] is not None else None
        for i in range(n)
    ]
    seeded = [v for v in macd_line if v is not None]
    if not seeded:
        return macd_line, [None] * n, [None] * n
    start = n - len(seeded)
    signal_line: list[Number] = [None] * start + (ema(seeded, signal) if len(seeded) >= signal else [None] * len(seeded))
    histogram = [
        (macd_line[i] - signal_line[i]) if macd_line[i] is not None and signal_line[i] is not None else None
        for i in range(n)
    ]
    return macd_line, signal_line, histogram


def adx(
    high: Sequence[Decimal], low: Sequence[Decimal], close: Sequence[Decimal], period: int = 14
) -> tuple[list[Number], list[Number], list[Number]]:
    """Average Directional Index plus ``+DI`` / ``-DI`` (Wilder).

    Needs about ``2 * period`` bars; earlier entries and zero-TR regions are
    ``None``/0 conservatively. Returns ``(adx, plus_di, minus_di)``.
    """
    _pos_int(period, "period")
    high, low, close = list(high), list(low), list(close)
    n = _require_length("adx", high, low, close)
    if n < 2 * period:
        return [None] * n, [None] * n, [None] * n
    tr = [None] * n
    plus_dm: list[Decimal] = [_ZERO] * n
    minus_dm: list[Decimal] = [_ZERO] * n
    for p in range(1, n):
        tr[p] = max(high[p] - low[p], abs(high[p] - close[p - 1]), abs(low[p] - close[p - 1]))
        up_move = high[p] - high[p - 1]
        down_move = low[p - 1] - low[p]
        plus_dm[p] = up_move if (up_move > down_move and up_move > _ZERO) else _ZERO
        minus_dm[p] = down_move if (down_move > up_move and down_move > _ZERO) else _ZERO

    tr_smooth = [None] + _wildered(tr[1:], period)
    plus_smooth = [None] + _wildered(plus_dm[1:], period)
    minus_smooth = [None] + _wildered(minus_dm[1:], period)

    plus_di: list[Number] = [None] * n
    minus_di: list[Number] = [None] * n
    dx: list[Decimal] = [_ZERO] * n
    for p in range(n):
        if tr_smooth[p] is None or tr_smooth[p] == _ZERO:
            continue
        plus_di[p] = _HUNDRED * plus_smooth[p] / tr_smooth[p]
        minus_di[p] = _HUNDRED * minus_smooth[p] / tr_smooth[p]
        denom = plus_di[p] + minus_di[p]
        if denom != _ZERO:
            dx[p] = _HUNDRED * abs(plus_di[p] - minus_di[p]) / denom

    first = next((p for p in range(n) if dx[p] != _ZERO or tr_smooth[p] is not None), None)
    adx_out: list[Number] = [None] * n
    if first is None:
        return adx_out, plus_di, minus_di
    # dx may carry trailing zeros for non-validated bars; only smooth the valid prefix.
    valid = [dx[p] for p in range(first, n)]
    smoothed = _wildered(valid, period)
    for offset, value in enumerate(smoothed):
        if value is not None:
            adx_out[first + offset] = value
    return adx_out, plus_di, minus_di


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------


def rsi(values: Sequence[Decimal], period: int = 14) -> list[Number]:
    """Relative Strength Index with Wilder smoothing (0..100 scale)."""
    _pos_int(period, "period")
    values = list(values)
    n = _require_length("rsi", values)
    out: list[Number] = [None] * n
    if n <= period:
        return out
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, n):
        change = values[i] - values[i - 1]
        gains.append(change if change > _ZERO else _ZERO)
        losses.append(-change if change < _ZERO else _ZERO)
    avg_gain = _sma_of(gains[:period], period)
    avg_loss = _sma_of(losses[:period], period)
    alpha = _ONE / period
    out[period] = _rsi_ratio(avg_gain, avg_loss)
    for i in range(period + 1, n):
        index = i - 1
        avg_gain = avg_gain + alpha * (gains[index] - avg_gain)
        avg_loss = avg_loss + alpha * (losses[index] - avg_loss)
        out[i] = _rsi_ratio(avg_gain, avg_loss)
    return out


def _rsi_ratio(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == _ZERO and avg_gain == _ZERO:
        return Decimal("50")
    if avg_loss == _ZERO:
        return Decimal("100")
    if avg_gain == _ZERO:
        return _ZERO
    return _HUNDRED - _HUNDRED / (_ONE + avg_gain / avg_loss)


def stochastic(
    high: Sequence[Decimal],
    low: Sequence[Decimal],
    close: Sequence[Decimal],
    period: int = 14,
    smooth: int = 3,
) -> tuple[list[Number], list[Number]]:
    """Stochastic oscillator: raw ``%K`` plus its ``smooth``-period SMA ``%D``."""
    _pos_int(period, "period")
    _pos_int(smooth, "smooth")
    high, low, close = list(high), list(low), list(close)
    n = _require_length("stochastic", high, low, close)
    k: list[Number] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        window_high = max(high[i - period + 1 : i + 1])
        window_low = min(low[i - period + 1 : i + 1])
        span = window_high - window_low
        if span == _ZERO:
            k[i] = Decimal("50")
        else:
            k[i] = (close[i] - window_low) / span * _HUNDRED
    seeded = [v for v in k if v is not None]
    start = n - len(seeded)
    d: list[Number] = [None] * start
    if len(seeded) >= smooth:
        d += sma(seeded, smooth)
    else:
        d += [None] * len(seeded)
    return k, d


def williams_r(
    high: Sequence[Decimal], low: Sequence[Decimal], close: Sequence[Decimal], period: int = 14
) -> list[Number]:
    """Williams %R over the look-back window in the conventional ``-100..0`` range."""
    _pos_int(period, "period")
    high, low, close = list(high), list(low), list(close)
    n = _require_length("williams_r", high, low, close)
    out: list[Number] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        window_high = max(high[i - period + 1 : i + 1])
        window_low = min(low[i - period + 1 : i + 1])
        span = window_high - window_low
        if span == _ZERO:
            out[i] = -Decimal("50")
        else:
            out[i] = (window_high - close[i]) / span * -_HUNDRED
    return out


def mfi(
    high: Sequence[Decimal],
    low: Sequence[Decimal],
    close: Sequence[Decimal],
    volume: Sequence[int],
    period: int = 14,
) -> list[Number]:
    """Money Flow Index (0..100): volume-weighted momentum over typical price."""
    _pos_int(period, "period")
    high, low, close = list(high), list(low), list(close)
    volume = [Decimal(v) for v in volume]
    n = _require_length("mfi", high, low, close, volume)
    out: list[Number] = [None] * n
    if n <= period:
        return out
    typical = [(high[i] + low[i] + close[i]) / 3 for i in range(n)]
    positive: list[Decimal] = []
    negative: list[Decimal] = []
    for i in range(1, n):
        move = typical[i] - typical[i - 1]
        money = volume[i] * typical[i]
        if move > _ZERO:
            positive.append(money)
            negative.append(_ZERO)
        elif move < _ZERO:
            negative.append(money)
            positive.append(_ZERO)
        else:
            positive.append(_ZERO)
            negative.append(_ZERO)
    for i in range(period, n):
        index = i - 1
        pos_sum = sum(positive[index - period + 1 : index + 1], _ZERO)
        neg_sum = sum(negative[index - period + 1 : index + 1], _ZERO)
        if neg_sum == _ZERO and pos_sum == _ZERO:
            out[i] = Decimal("50")
        elif neg_sum == _ZERO:
            out[i] = Decimal("100")
        else:
            out[i] = _HUNDRED - _HUNDRED / (_ONE + pos_sum / neg_sum)
    return out


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


def true_range(
    high: Sequence[Decimal], low: Sequence[Decimal], close: Sequence[Decimal]
) -> list[Number]:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|); None at bar 0."""
    high, low, close = list(high), list(low), list(close)
    n = _require_length("true_range", high, low, close)
    out: list[Number] = [None] * n
    for i in range(1, n):
        out[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return out


def atr(
    high: Sequence[Decimal], low: Sequence[Decimal], close: Sequence[Decimal], period: int = 14
) -> list[Number]:
    """Average True Range with Wilder smoothing; ``None`` until ``period`` TRs."""
    _pos_int(period, "period")
    tr = true_range(high, low, close)
    tr_values = [v for v in tr if v is not None]
    if not tr_values:
        return [None] * len(tr)
    start = len(tr) - len(tr_values)
    return [None] * start + _wildered(tr_values, period)


def bollinger(
    values: Sequence[Decimal], period: int = 20, deviations: Number = 2
) -> tuple[list[Number], list[Number], list[Number], list[Number], list[Number]]:
    """Bollinger Bands -> ``(mid, upper, lower, bandwidth, percent_b)``.

    Population standard deviation over the window; ``%B = (close - lower) /
    (upper - lower)`` so 0.5 is exactly the mid band.
    """
    _pos_int(period, "period")
    values = list(values)
    deviations = Decimal(deviations)
    n = _require_length("bollinger", values)
    mid: list[Number] = [None] * n
    upper: list[Number] = [None] * n
    lower: list[Number] = [None] * n
    bandwidth: list[Number] = [None] * n
    percent_b: list[Number] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        window = values[i - period + 1 : i + 1]
        mean = _sma_of(window, period)
        variance = sum((v - mean) ** 2 for v in window) / period
        std = variance.sqrt()
        mid[i] = mean
        upper[i] = mean + deviations * std
        lower[i] = mean - deviations * std
        if mean != _ZERO:
            bandwidth[i] = (upper[i] - lower[i]) / mean
        span = upper[i] - lower[i]
        if span != _ZERO:
            percent_b[i] = (values[i] - lower[i]) / span
    return mid, upper, lower, bandwidth, percent_b


def keltner(
    high: Sequence[Decimal],
    low: Sequence[Decimal],
    close: Sequence[Decimal],
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: Number = 2,
    atr_kind: str = "wilder",
) -> tuple[list[Number], list[Number], list[Number], list[Number]]:
    """Keltner Channels -> ``(mid, upper, lower, width)`` from an EMA envelope."""
    _pos_int(ema_period, "ema_period")
    _pos_int(atr_period, "atr_period")
    if atr_kind not in ("wilder", "sma"):
        raise ValueError("atr_kind must be 'wilder' or 'sma'")
    high, low, close = list(high), list(low), list(close)
    multiplier = Decimal(multiplier)
    n = _require_length("keltner", high, low, close)
    mid = ema(close, ema_period)
    if atr_kind == "wilder":
        atr_series = atr(high, low, close, atr_period)
    else:
        tr = true_range(high, low, close)
        tr_values = [v for v in tr if v is not None]
        atr_series: list[Number] = [None] * n
        for i, t0 in enumerate(tr):
            if t0 is None:
                continue
            running = tr_values[: i]  # TRs available up to (and including) bar i
            if len(running) < atr_period:
                continue
            atr_series[i] = sum(running[-atr_period:], _ZERO) / atr_period
    upper: list[Number] = [None] * n
    lower: list[Number] = [None] * n
    width: list[Number] = [None] * n
    for i in range(n):
        if mid[i] is None or atr_series[i] is None:
            continue
        upper[i] = mid[i] + multiplier * atr_series[i]
        lower[i] = mid[i] - multiplier * atr_series[i]
        if mid[i] != _ZERO:
            width[i] = (upper[i] - lower[i]) / mid[i]
    return mid, upper, lower, width


def supertrend(
    high: Sequence[Decimal],
    low: Sequence[Decimal],
    close: Sequence[Decimal],
    period: int = 10,
    multiplier: Number = 3,
) -> tuple[list[Number], list[int]]:
    """Supertrend -> ``(line, direction)`` with direction ``1`` (up) or ``-1`` (down).

    The line is the final upper band while bullish, the final lower band while
    bearish. Direction flips only when the close crosses the opposite band.
    """
    _pos_int(period, "period")
    high, low, close = list(high), list(low), list(close)
    multiplier = Decimal(multiplier)
    n = _require_length("supertrend", high, low, close)
    atr_series = atr(high, low, close, period)
    basic = [(high[i] + low[i]) / 2 for i in range(n)]
    upper_band: list[Number] = [None] * n
    lower_band: list[Number] = [None] * n
    line: list[Number] = [None] * n
    direction: list[int] = [0] * n
    final_upper: Decimal | None = None
    final_lower: Decimal | None = None
    current_dir = 1
    for i in range(n):
        if atr_series[i] is None:
            continue
        upper_band[i] = basic[i] + multiplier * atr_series[i]
        lower_band[i] = basic[i] - multiplier * atr_series[i]
    for i in range(n):
        if upper_band[i] is None or lower_band[i] is None:
            continue
        if final_upper is None or final_lower is None:
            final_upper = upper_band[i]
            final_lower = lower_band[i]
            current_dir = 1
        else:
            if upper_band[i] < final_upper or close[i - 1] > final_upper:
                final_upper = upper_band[i]
            if lower_band[i] > final_lower or close[i - 1] < final_lower:
                final_lower = lower_band[i]
            if current_dir == 1 and close[i] < final_lower:
                current_dir = -1
            elif current_dir == -1 and close[i] > final_upper:
                current_dir = 1
        direction[i] = current_dir
        line[i] = final_upper if current_dir == 1 else final_lower
    return line, direction


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------


def obv(values: Sequence[Decimal], volume: Sequence[int]) -> list[Decimal]:
    """On-Balance Volume: running cumulative of signed volume; starts at 0."""
    values = list(values)
    volume = [Decimal(v) for v in volume]
    n = _require_length("obv", values, volume)
    out: list[Decimal] = [_ZERO] * n
    for i in range(1, n):
        if values[i] > values[i - 1]:
            out[i] = out[i - 1] + volume[i]
        elif values[i] < values[i - 1]:
            out[i] = out[i - 1] - volume[i]
        else:
            out[i] = out[i - 1]
    return out


def vwap(
    high: Sequence[Decimal],
    low: Sequence[Decimal],
    close: Sequence[Decimal],
    volume: Sequence[int],
    anchor: int = 20,
) -> list[Number]:
    """Anchored-window volume-weighted average price over the last ``anchor`` bars.

    When the window's total volume is zero the function falls back to the
    typical-price mean over the **same** anchor window — this is both
    semantically correct ("over the last anchor bars") and O(n), whereas
    re-summing the entire prefix would be O(n²).
    """
    _pos_int(anchor, "anchor")
    highs = list(high); lows = list(low); closes = list(close)
    n = _require_length("vwap", highs, lows, closes)
    volume_dec = [Decimal(v) for v in volume]
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    out: list[Number] = [None] * n
    typ_window: deque[Decimal] = deque()
    vol_window: deque[Decimal] = deque()
    typ_sum = _ZERO
    vol_sum = _ZERO
    for i in range(n):
        tv = typical[i]
        vol = volume_dec[i]
        typ_window.append(tv)
        vol_window.append(vol)
        typ_sum += tv
        vol_sum += vol
        if len(typ_window) > anchor:
            typ_sum -= typ_window.popleft()
            vol_sum -= vol_window.popleft()
        if vol_sum == _ZERO:
            out[i] = typ_sum / len(typ_window)
        else:
            # Weighted sum over the anchor window — kept O(anchor) per bar
            # for clarity; anchor is small (default 20).
            ws = sum(v * typ_window[j] for j, v in enumerate(vol_window))
            out[i] = ws / vol_sum
    return out