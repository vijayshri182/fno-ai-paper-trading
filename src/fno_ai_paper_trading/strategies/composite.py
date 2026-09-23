"""Multi-indicator composite strategy (educational, WS 7.18).

Implements the "use more than one indicator" rule from the indicator guide:
combine trend + momentum + volatility + volume, take trades only when several
independent families agree, and always carry an ATR-based stop suggestion.

Three deterministic, stateless presets (``mode``):

* ``trend``           — EMA(9/21) alignment + MACD histogram + Supertrend
                        direction + ADX strength filter + stochastic/OBV/VWAP
                        confirmation, with an RSI overbought/oversold guard and
                        ATR stop distance.   (trend-following)
* ``mean_reversion``  — RSI extremes + stochastic %K/%D cross + %B band touches
                        + Williams %R + MFI volume-weighted confirmation.
* ``breakout``        — Supertrend flip + Keltner squeeze + %B beyond-band
                        + OBV momentum + ATR expansion.

Every decision is a *vote*: each indicator family casts one directional vote,
and an actionable BUY/SELL is emitted only when the net vote exceeds a
threshold. The signal, the vote tally, every indicator value and the ATR stop
distance are returned in :class:`SignalResult` ``meta``.

This class is pure computation only — it never places orders, reads a broker,
or mutates any state. Same input always produces the same output.

Backtesting with the generic engine
-----------------------------------
The composite is a *directional* strategy: while a trend persists it keeps
voting BUY (or SELL) bar after bar. The signal-agnostic
:class:`~fno_ai_paper_trading.backtest.engine.BacktestEngine` places one order
per actionable signal, which is right for crossing strategies (the champion
emits only at the crossover) but would stack one lot per bar for a persistent
bias. When replaying the composite use :func:`bulk_signals` (default
``latch=True``) with the engine's ``signals=`` shortcut — signals are computed
in O(n) over the causal indicator stacks and the directional bias is reduced to
entry/exit signals (BUY on the transition into long, SELL on the transition
into short, HOLD for repeats). This matches how the paper session already
trades ("already long; BUY ignored") and how the frozen champion behaves.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice

from fno_ai_paper_trading.strategies import indicators
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.utils.functions import positive_int

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")

MODES = frozenset({"trend", "mean_reversion", "breakout"})

_DEFAULT_ADX_MIN = Decimal("20")
_DEFAULT_RSI_OVERBOUGHT = Decimal("90")
_DEFAULT_RSI_OVERSOLD = Decimal("10")
_DEFAULT_WARMUP = 40
_DEFAULT_ENTRY_VOTES = 3
_REVERSION_RSI_OVERBOUGHT = Decimal("70")
_REVERSION_RSI_OVERSOLD = Decimal("30")

# Feature labels used in reasons, in a stable order.
_TREND_LONG_LABELS = ("ema", "macd", "supertrend", "stoch", "obv", "vwap", "bb")
_TREND_SHORT_LABELS = ("ema", "macd", "supertrend", "stoch", "obv", "vwap", "bb")
_REVERSION_LONG_LABELS = ("rsi", "stoch", "bb", "williams", "mfi")
_REVERSION_SHORT_LABELS = ("rsi", "stoch", "bb", "williams", "mfi")
_BREAKOUT_LONG_LABELS = ("supertrend_flip", "keltner", "bb", "obv", "atr")
_BREAKOUT_SHORT_LABELS = ("supertrend_flip", "keltner", "bb", "obv", "atr")


@dataclass(frozen=True)
class _Features:
    """Indicator snapshot at the decision bar (last index of the series)."""

    values: dict
    long_votes: list[bool]
    short_votes: list[bool]
    confidence: Decimal
    atr_stop_pct: Decimal | None
    adx: Decimal | None
    rsi: Decimal | None


class MultiIndicatorStrategy(Strategy):
    """Composite trend/momentum/volatility/volume vote strategy."""

    name = "multi_indicator_composite"

    def __init__(
        self,
        mode: str = "trend",
        *,
        warmup: int = _DEFAULT_WARMUP,
        entry_votes: int = _DEFAULT_ENTRY_VOTES,
        adx_min: Decimal | None = _DEFAULT_ADX_MIN,
        rsi_overbought: Decimal | None = _DEFAULT_RSI_OVERBOUGHT,
        rsi_oversold: Decimal | None = _DEFAULT_RSI_OVERSOLD,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(MODES)}")
        self.mode = mode
        self.warmup = positive_int(warmup, "warmup")
        self.entry_votes = positive_int(entry_votes, "entry_votes")
        self.adx_min = _optional_positive_decimal(adx_min, "adx_min")
        self.rsi_overbought = _optional_positive_decimal(rsi_overbought, "rsi_overbought")
        self.rsi_oversold = _optional_positive_decimal(rsi_oversold, "rsi_oversold")

    # ------------------------------------------------------------------ API
    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        if not bars:
            return SignalResult(signal=Signal.HOLD, reason="no bars")
        last = bars[-1]
        if len(bars) < self.warmup:
            return SignalResult(
                signal=Signal.HOLD,
                instrument=last.instrument,
                timestamp=last.timestamp,
                reason=f"warming up: need {self.warmup} bars for the indicator stack; have {len(bars)}",
                meta={"mode": self.mode, "warmup": self.warmup, "bars": len(bars)},
            )
        stacks = _compute_stacks(bars)
        return _result_for(self, stacks, bars, len(bars) - 1)

    def signals_for(self, bars: list[MarketPrice]) -> list[SignalResult] | None:
        """O(n) precomputed decision series for the backtest engine.

        Returns the same decisions ``analyze`` would produce for every prefix
        (see :func:`bulk_signals`) with the directional bias latched — the
        composite enters once per trend regime instead of stacking one lot per
        bar. The live/paper service continues to call ``analyze`` per bar.
        """
        return bulk_signals(bars, self, latch=True)

    # ------------------------------------------------------------ decision
    def _decide(self, features: _Features) -> Signal:
        if self.mode == "trend":
            return self._decide_trend(features)
        if self.mode == "mean_reversion":
            return self._decide_reversion(features)
        return self._decide_breakout(features)

    def _decide_trend(self, features: _Features) -> Signal:
        long_votes = sum(features.long_votes)
        short_votes = sum(features.short_votes)
        if self.adx_min is not None and features.adx is not None and features.adx < self.adx_min:
            return Signal.HOLD
        if long_votes - short_votes >= self.entry_votes:
            if (
                self.rsi_overbought is not None
                and features.rsi is not None
                and features.rsi >= self.rsi_overbought
            ):
                return Signal.HOLD
            return Signal.BUY
        if short_votes - long_votes >= self.entry_votes:
            if self.rsi_oversold is not None and features.rsi is not None and features.rsi <= self.rsi_oversold:
                return Signal.HOLD
            return Signal.SELL
        return Signal.HOLD

    def _decide_reversion(self, features: _Features) -> Signal:
        long_votes = sum(features.long_votes)
        short_votes = sum(features.short_votes)
        if long_votes >= self.entry_votes and long_votes > short_votes:
            return Signal.BUY
        if short_votes >= self.entry_votes and short_votes > long_votes:
            return Signal.SELL
        return Signal.HOLD

    def _decide_breakout(self, features: _Features) -> Signal:
        long_votes = sum(features.long_votes)
        short_votes = sum(features.short_votes)
        if long_votes >= self.entry_votes and long_votes > short_votes:
            return Signal.BUY
        if short_votes >= self.entry_votes and short_votes > long_votes:
            return Signal.SELL
        return Signal.HOLD

    # ------------------------------------------------------------- reasons
    def _reason(self, signal: Signal, features: _Features) -> str:
        if signal is Signal.BUY:
            side = "long votes ahead"
        elif signal is Signal.SELL:
            side = "short votes ahead"
        elif self.mode == "trend" and self.adx_min is not None and features.adx is not None and features.adx < self.adx_min:
            side = f"trend filter (ADX {features.adx:.1f} < {self.adx_min:.1f}) holds"
        else:
            side = "no vote majority"
        return f"{self.mode} composite: {side}; confidence {features.confidence:.2f}"


# ---------------------------------------------------------------------------
# feature extraction (pure, per bar window)
# ---------------------------------------------------------------------------


def _optional_positive_decimal(value: Decimal | None, name: str) -> Decimal | None:
    if value is None:
        return None
    value = Decimal(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _labels(mode: str, side: str) -> tuple[str, ...]:
    if mode == "trend":
        return _TREND_LONG_LABELS if side == "long" else _TREND_SHORT_LABELS
    if mode == "mean_reversion":
        return _REVERSION_LONG_LABELS if side == "long" else _REVERSION_SHORT_LABELS
    return _BREAKOUT_LONG_LABELS if side == "long" else _BREAKOUT_SHORT_LABELS


def _fmt(value: Decimal | None, width: int = 1) -> str:
    return f"{value:.{width}f}" if value is not None else ""


def _compute_stacks(bars: list[MarketPrice]) -> dict[str, list]:
    """Compute every indicator series once over the full bar history.

    Every indicator in :mod:`fno_ai_paper_trading.strategies.indicators` is
    causal — its value at bar ``i`` depends only on bars ``[: i + 1]`` — so a
    stack computed over the whole series is identical to a from-scratch
    computation over any prefix. This is what lets :func:`bulk_signals` replay
    long histories in O(n) instead of the O(n**2) per-prefix slicing cost.
    """
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]

    ema_fast = indicators.ema(closes, 9)
    ema_slow = indicators.ema(closes, 21)
    macd_line, macd_signal, macd_hist = indicators.macd(closes, 12, 26, 9)
    rsi_series = indicators.rsi(closes, 14)
    adx_series, plus_di, minus_di = indicators.adx(highs, lows, closes, 14)
    atr_series = indicators.atr(highs, lows, closes, 14)
    k, d = indicators.stochastic(highs, lows, closes, 14, 3)
    willr = indicators.williams_r(highs, lows, closes, 14)
    mfi_series = indicators.mfi(highs, lows, closes, volumes, 14)
    bb_mid, bb_upper, bb_lower, bb_width, percent_b = indicators.bollinger(closes, 20, 2)
    k_mid, k_upper, k_lower, k_width = indicators.keltner(highs, lows, closes, 20, 10, 2)
    super_line, super_dir = indicators.supertrend(highs, lows, closes, 10, 3)
    obv_series = indicators.obv(closes, volumes)
    vwap_series = indicators.vwap(highs, lows, closes, volumes, 20)

    return {
        "closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
        "ema_fast": ema_fast, "ema_slow": ema_slow,
        "macd_line": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
        "rsi": rsi_series, "adx": adx_series, "plus_di": plus_di, "minus_di": minus_di,
        "atr": atr_series, "stoch_k": k, "stoch_d": d, "williams_r": willr,
        "mfi": mfi_series, "bb_mid": bb_mid, "bb_upper": bb_upper, "bb_lower": bb_lower,
        "bb_width": bb_width, "percent_b": percent_b,
        "keltner_mid": k_mid, "keltner_upper": k_upper, "keltner_lower": k_lower,
        "keltner_width": k_width, "supertrend_line": super_line, "supertrend_dir": super_dir,
        "obv": obv_series, "vwap": vwap_series,
    }


def _features_at(stacks: dict[str, list], i: int, mode: str) -> _Features:
    """``_Features`` snapshot at bar index ``i`` of the precomputed stacks."""
    ema_fast = stacks["ema_fast"]
    ema_slow = stacks["ema_slow"]
    macd_line = stacks["macd_line"]
    macd_signal = stacks["macd_signal"]
    macd_hist = stacks["macd_hist"]
    rsi_series = stacks["rsi"]
    adx_series = stacks["adx"]
    plus_di = stacks["plus_di"]
    minus_di = stacks["minus_di"]
    atr_series = stacks["atr"]
    k = stacks["stoch_k"]
    d = stacks["stoch_d"]
    willr = stacks["williams_r"]
    mfi_series = stacks["mfi"]
    bb_mid = stacks["bb_mid"]
    bb_upper = stacks["bb_upper"]
    bb_lower = stacks["bb_lower"]
    bb_width = stacks["bb_width"]
    percent_b = stacks["percent_b"]
    k_mid = stacks["keltner_mid"]
    k_upper = stacks["keltner_upper"]
    k_lower = stacks["keltner_lower"]
    k_width = stacks["keltner_width"]
    super_dir = stacks["supertrend_dir"]
    obv_series = stacks["obv"]
    vwap_series = stacks["vwap"]

    last_close = stacks["closes"][i]

    obv_slope = (
        (obv_series[i] - obv_series[i - 14]) / abs(obv_series[i - 14])
        if obv_series[i - 14] != 0 else _ZERO
    )

    atr_now = atr_series[i]
    atr_pct = (_HUNDRED * atr_now / last_close) if atr_now is not None and last_close > 0 else None

    ctx = {
        "last_close": _fmt(last_close),
        "ema_fast": _fmt(ema_fast[i]),
        "ema_slow": _fmt(ema_slow[i]),
        "macd_line": _fmt(macd_line[i], 4),
        "macd_signal": _fmt(macd_signal[i], 4),
        "macd_hist": _fmt(macd_hist[i], 4),
        "rsi": _fmt(rsi_series[i]),
        "adx": _fmt(adx_series[i]),
        "plus_di": _fmt(plus_di[i]),
        "minus_di": _fmt(minus_di[i]),
        "atr": _fmt(atr_now),
        "stoch_k": _fmt(k[i]),
        "stoch_d": _fmt(d[i]),
        "williams_r": _fmt(willr[i]),
        "mfi": _fmt(mfi_series[i]),
        "bb_mid": _fmt(bb_mid[i]),
        "bb_upper": _fmt(bb_upper[i]),
        "bb_lower": _fmt(bb_lower[i]),
        "percent_b": _fmt(percent_b[i], 3),
        "keltner_upper": _fmt(k_upper[i]),
        "keltner_lower": _fmt(k_lower[i]),
        "keltner_width": _fmt(k_width[i], 5),
        "supertrend_dir": str(super_dir[i]),
        "obv_slope": _fmt(Decimal(obv_slope), 3),
        "vwap": _fmt(vwap_series[i]),
        "vwap_delta_pct": _fmt(
            ((last_close - vwap_series[i]) / vwap_series[i]) * _HUNDRED if vwap_series[i] else None, 3
        ),
    }
    if mode == "trend":
        long_votes = [
            ema_fast[i] > ema_slow[i],
            macd_hist[i] > _ZERO if macd_hist[i] is not None else False,
            super_dir[i] == 1,
            (k[i] > d[i]) if k[i] is not None and d[i] is not None else False,
            obv_slope > _ZERO,
            (last_close > vwap_series[i]) if vwap_series[i] else False,
            (percent_b[i] > Decimal("0.5")) if percent_b[i] is not None else False,
        ]
        short_votes = [
            ema_fast[i] < ema_slow[i],
            macd_hist[i] < _ZERO if macd_hist[i] is not None else False,
            super_dir[i] == -1,
            (k[i] < d[i]) if k[i] is not None and d[i] is not None else False,
            obv_slope < _ZERO,
            (last_close < vwap_series[i]) if vwap_series[i] else False,
            (percent_b[i] < Decimal("0.5")) if percent_b[i] is not None else False,
        ]
    elif mode == "mean_reversion":
        k_prev, d_prev = k[i - 1], d[i - 1]
        kk, dd = k[i], d[i]
        stoch_up = (
            k_prev is not None and d_prev is not None and kk is not None and dd is not None
            and k_prev <= d_prev and kk > dd and kk < 50
        )
        stoch_down = (
            k_prev is not None and d_prev is not None and kk is not None and dd is not None
            and k_prev >= d_prev and kk < dd and kk > 50
        )
        long_votes = [
            (rsi_series[i] <= _REVERSION_RSI_OVERSOLD) if rsi_series[i] is not None else False,
            stoch_up,
            (percent_b[i] < _ZERO) if percent_b[i] is not None else False,
            (willr[i] <= -80) if willr[i] is not None else False,
            (mfi_series[i] <= 20) if mfi_series[i] is not None else False,
        ]
        short_votes = [
            (rsi_series[i] >= _REVERSION_RSI_OVERBOUGHT) if rsi_series[i] is not None else False,
            stoch_down,
            (percent_b[i] > _ONE) if percent_b[i] is not None else False,
            (willr[i] >= -20) if willr[i] is not None else False,
            (mfi_series[i] >= 80) if mfi_series[i] is not None else False,
        ]
    else:  # breakout
        atr_prev = atr_series[i - 1]
        atr_expanding = atr_now is not None and atr_prev is not None and atr_now > atr_prev
        super_flip_up = super_dir[i] == 1 and super_dir[i - 1] == -1
        super_flip_down = super_dir[i] == -1 and super_dir[i - 1] == 1
        long_votes = [
            super_flip_up,
            bool(super_flip_up or (super_dir[i] == 1 and k_upper[i] is not None and last_close > k_upper[i])),
            (percent_b[i] > _ONE) if percent_b[i] is not None else False,
            obv_slope > _ZERO,
            atr_expanding,
        ]
        short_votes = [
            super_flip_down,
            bool(super_flip_down or (super_dir[i] == -1 and k_lower[i] is not None and last_close < k_lower[i])),
            (percent_b[i] < _ZERO) if percent_b[i] is not None else False,
            obv_slope < _ZERO,
            atr_expanding,
        ]

    long_count = sum(long_votes)
    short_count = sum(short_votes)
    total = len(long_votes)
    confidence = Decimal(abs(long_count - short_count)) / total

    return _Features(
        values=ctx,
        long_votes=long_votes,
        short_votes=short_votes,
        confidence=confidence,
        atr_stop_pct=atr_pct,
        adx=adx_series[i],
        rsi=rsi_series[i],
    )


def _extract_features(bars: list[MarketPrice], mode: str) -> _Features:
    """Snapshot at the last bar of ``bars`` (thin wrapper over the stacks)."""
    return _features_at(_compute_stacks(bars), len(bars) - 1, mode)


def latched_signals(
    signals: list[SignalResult],
    *,
    initial: Signal | None = None,
) -> list[SignalResult]:
    """Reduce a per-bar directional bias series to entry/exit-only signals.

    A directional strategy keeps voting BUY (or SELL) while its bias persists;
    the generic backtest engine places one order per actionable signal, so a
    persistent bias would stack one lot per bar. Latching emits BUY only on the
    transition into a long bias and SELL only on the transition into a short
    bias; every repeated same-side signal becomes HOLD until the opposite
    direction arrives (entries keep their ``initial`` state when given).

    This mirrors the paper session's position-aware rule ("already long; BUY
    ignored") and the champion's crossing behaviour — enter once, hold, exit on
    the opposite signal.
    """
    state = initial
    out: list[SignalResult] = []
    for result in signals:
        if result.signal is Signal.BUY:
            if state is Signal.BUY:
                out.append(_latched_hold(result, state))
            else:
                state = Signal.BUY
                out.append(result)
        elif result.signal is Signal.SELL:
            if state is Signal.SELL:
                out.append(_latched_hold(result, state))
            else:
                state = Signal.SELL
                out.append(result)
        else:
            out.append(result)
    return out


def _latched_hold(result: SignalResult, state: Signal) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD,
        instrument=result.instrument,
        timestamp=result.timestamp,
        reason=f"latched: already {'long' if state is Signal.BUY else 'short'}",
        meta=dict(result.meta or {}),
    )


def bulk_signals(
    bars: list[MarketPrice],
    strategy: MultiIndicatorStrategy,
    *,
    latch: bool = True,
) -> list[SignalResult]:
    """Compute the full decision series in one O(n) pass.

    The indicator stacks are causal, so the value at bar ``i`` of a full-series
    computation is identical to a from-scratch ``analyze(bars[: i + 1])`` — the
    no-look-ahead contract is preserved while avoiding the O(n**2) per-bar
    prefix-slice cost. When ``latch`` is true (default) the directional bias is
    reduced to entry/exit signals via :func:`latched_signals`, matching how the
    composite should be traded through a signal-agnostic engine.
    """
    if not bars:
        return []
    stacks = _compute_stacks(bars)
    signals: list[SignalResult] = []
    for index, bar in enumerate(bars):
        if index + 1 < strategy.warmup:
            signals.append(
                SignalResult(
                    signal=Signal.HOLD,
                    instrument=bar.instrument,
                    timestamp=bar.timestamp,
                    reason=(
                        f"warming up: need {strategy.warmup} bars for the indicator stack; "
                        f"have {index + 1}"
                    ),
                    meta={"mode": strategy.mode, "warmup": strategy.warmup, "bars": index + 1},
                )
            )
        else:
            signals.append(_result_for(strategy, stacks, bars, index))
    if latch:
        return latched_signals(signals)
    return signals


def _result_for(
    strategy: MultiIndicatorStrategy,
    stacks: dict[str, list],
    bars: list[MarketPrice],
    index: int,
) -> SignalResult:
    """Replicate ``analyze(bars[: index + 1])`` from precomputed causal stacks."""
    features = _features_at(stacks, index, strategy.mode)
    signal = strategy._decide(features)
    meta = dict(features.values)
    meta.update(
        {
            "mode": strategy.mode,
            "long_votes": ", ".join(
                label for label, voted in zip(_labels(strategy.mode, "long"), features.long_votes) if voted
            ),
            "short_votes": ", ".join(
                label for label, voted in zip(_labels(strategy.mode, "short"), features.short_votes) if voted
            ),
            "confidence": str(features.confidence),
            "atr_stop_pct": str(features.atr_stop_pct) if features.atr_stop_pct is not None else "",
        }
    )
    last = bars[index]
    return SignalResult(
        signal=signal,
        instrument=last.instrument,
        timestamp=last.timestamp,
        reason=strategy._reason(signal, features),
        meta=meta,
    )