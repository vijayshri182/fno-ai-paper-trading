"""Deterministic 5-minute benchmark signal families (research-only).

This module defines the *candidate signal streams* that the 5M strategy
benchmark compares against the frozen Donchian 20/10 baseline.  Every stream is
a pure, causal function of ``bars[: i + 1]`` (never the future): the value at
global bar index ``i`` may use only bars ``0..i``.  All arithmetic is Decimal,
warm-up regions emit ``Signal15m.NEUTRAL``, and every threshold below is a
documented, pre-registered constant (no tuning after the protected OOS
window).

The project rule this module preserves:

* the working Donchian 20/10 signal is the **read-only reference**; it is never
  modified here (``strategies/research_candidates.py::donchian_breakout_signals``
  is imported unchanged),
* candidate families are *research candidates* only - none of them gets
  promoted, re-tuned on OOS, or wired into the live/paper execution path,
* the VWAP mean-reversion family is ``NOT TESTABLE`` (recorded volume is ``0``
  on every bar of the NIFTY 50 5m dataset; no volume is invented).

Each builder returns ``(list[Signal15m], diagnostics)`` with ``stream[i]`` the
intent that a 5m decision made on reference bar ``i`` should act on.

Timeframe primitives
--------------------
* session anchor: 09:15 (Nifty 5m session, one bar per 5 minutes),
* ``bars_per_candle`` buckets are anchored at 09:15 and never span sessions,
* a bucket only exists once **all** its bars complete (partial trailing candles
  are never emitted).

Candidate families (pre-registered)
-----------------------------------
``donchian_20_10_baseline``  frozen Donchian 20/10, mapped to intents.
``c1_ema_trend``            1H EMA(20/50) gate: trigger entries only when the
                            1-hour trend aligns with the Donchian direction;
                            non-aligned trigger legs are suppressed to NEUTRAL
                            (exit-only through the position state machine).
``c2_orb``                  Opening-range breakout of the first three 5m candles
                            of the day; first tradable reference bar is 09:30
                            (decision 09:35); mid-range closes are NEUTRAL.
``c3_vwap_mr``              NOT TESTABLE - volume is zero on every recorded bar.
``c4_atr_regime``           ATR(14) regime against a 50-ATR rolling base:
                            ``ATR > 1.15 x base`` => TREND (Donchian trigger),
                            ``ATR < 0.85 x base`` => RANGE (SMA(20) +/- 1.5xATR
                            mean reversion), otherwise TRANSITION (NEUTRAL).
``c5_mtf_hybrid``           Multi-timeframe confluence: Donchian trigger AND
                            15m EMA(20/50) alignment AND 1H EMA(20/50) alignment
                            AND previous-session close direction; all four must
                            agree for an entry leg.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Callable, Sequence

from fno_ai_paper_trading.experiments.directional_15m.contract import Signal15m
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.indicators import atr, ema, sma
from fno_ai_paper_trading.strategies.research_candidates import donchian_breakout_signals

SESSION_OPEN = time(9, 15)
BAR_MINUTES = 5
SECONDS_PER_SESSION_BAR = BAR_MINUTES * 60

NEUTRAL = Signal15m.NEUTRAL
BULLISH = Signal15m.BULLISH
BEARISH = Signal15m.BEARISH

_RAW_TO_INTENT: dict[str, Signal15m] = {
    "BUY": BULLISH,
    "SELL": BEARISH,
    "HOLD": NEUTRAL,
}

DONCHIAN_ENTRY_CHANNEL = 20
DONCHIAN_EXIT_CHANNEL = 10
DONCHIAN_WARMUP_BARS = max(DONCHIAN_ENTRY_CHANNEL, DONCHIAN_EXIT_CHANNEL) + 1

VWAP_VOLUME_ZERO_NOTE = (
    "VWAP mean-reversion requires per-bar volume; the recorded NIFTY 50 5m "
    "dataset has volume == 0 on all 87,193 bars.  No volume is fabricated, so "
    "this family is NOT TESTABLE in this benchmark and excluded from all "
    "numeric tables."
)


def session_minute_index(ts: datetime) -> int:
    """0-based 5-minute index within the 09:15 session (bars before 09:15 < 0)."""
    start = datetime.combine(ts.date(), SESSION_OPEN)
    return int((ts - start).total_seconds() // SECONDS_PER_SESSION_BAR)


@dataclass(frozen=True)
class Candle:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    count: int
    start_idx: int
    end_idx: int


def _decimal_max(values: Sequence[Decimal]) -> Decimal:
    return max(values)


def _decimal_min(values: Sequence[Decimal]) -> Decimal:
    return min(values)


def aggregate_candles(
    bars: Sequence[MarketPrice], bars_per_candle: int
) -> tuple[list[Candle], list[int | None]]:
    """Bucket completed multi-bar candles anchored at the 09:15 session open.

    Returns ``(candles, candle_at[i])`` where ``candles`` is the full-series
    list of completed candles (global index ``c`` for the ``c``-th completed
    bucket) and ``candle_at[i]`` is the index of the most recent completed
    candle at-or-before bar ``i`` (``None`` before the first complete bucket).
    Candle boundaries are session-anchored: bar ``k`` of a session belongs to
    bucket ``k // bars_per_candle`` and a bucket is complete when ``k %``
    ``bars_per_candle == bars_per_candle - 1``; partial trailing buckets are
    never emitted, so EMA/SMA over candle closes stay causal and day-safe.
    """
    candles: list[Candle] = []
    candle_at: list[int | None] = [None] * len(bars)
    for i, bar in enumerate(bars):
        s = session_minute_index(bar.timestamp)
        if s >= 0 and (s + 1) % bars_per_candle == 0:
            start = i - bars_per_candle + 1
            if start >= 0:
                segment = bars[start : i + 1]
                candles.append(
                    Candle(
                        open=segment[0].open,
                        high=_decimal_max([b.high for b in segment]),
                        low=_decimal_min([b.low for b in segment]),
                        close=segment[-1].close,
                        count=len(segment),
                        start_idx=start,
                        end_idx=i,
                    )
                )
        candle_at[i] = len(candles) - 1 if candles else None
    return candles, candle_at


def _donchian_intents(
    bars: Sequence[MarketPrice],
    *,
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL,
    exit_channel: int = DONCHIAN_EXIT_CHANNEL,
) -> list[Signal15m]:
    raw = donchian_breakout_signals(
        bars, entry_channel=entry_channel, exit_channel=exit_channel
    )
    return [_RAW_TO_INTENT[r.signal.value] for r in raw]


# ---------------------------------------------------------------------------
# param holders (all thresholds pre-registered and documented)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DonchianParams:
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL
    exit_channel: int = DONCHIAN_EXIT_CHANNEL


@dataclass(frozen=True)
class EMATrendParams:
    bars_per_candle: int = 12            # one hour of 5-minute bars
    fast: int = 20
    slow: int = 50
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL
    exit_channel: int = DONCHIAN_EXIT_CHANNEL


@dataclass(frozen=True)
class ORBParams:
    range_bars: int = 3                  # exactly the first three 5m candles
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL  # unused; kept for symmetry
    exit_channel: int = DONCHIAN_EXIT_CHANNEL     # unused; kept for symmetry


@dataclass(frozen=True)
class ATRRegimeParams:
    atr_period: int = 14
    regime_base: int = 50
    trend_mult: Decimal = Decimal("1.15")
    range_mult: Decimal = Decimal("0.85")
    mr_sma: int = 20
    mr_band: Decimal = Decimal("1.5")
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL
    exit_channel: int = DONCHIAN_EXIT_CHANNEL


@dataclass(frozen=True)
class MTFHybridParams:
    m15_bars: int = 3
    h1_bars: int = 12
    fast: int = 20
    slow: int = 50
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL
    exit_channel: int = DONCHIAN_EXIT_CHANNEL
    use_prev_day_context: bool = True


# ---------------------------------------------------------------------------
# stream builders
# ---------------------------------------------------------------------------


def build_donchian_stream(
    bars: Sequence[MarketPrice], params: DonchianParams | None = None
) -> tuple[list[Signal15m], dict]:
    if params is None:
        params = DonchianParams()
    stream = _donchian_intents(
        bars, entry_channel=params.entry_channel, exit_channel=params.exit_channel
    )
    diagnostics = {
        "params": {
            "entry_channel": params.entry_channel,
            "exit_channel": params.exit_channel,
            "warmup_bars": max(params.entry_channel, params.exit_channel) + 1,
        },
        "note": (
            "Read-only frozen Donchian 20/10 reference stream; never modified."
        ),
    }
    return stream, diagnostics


def _ema_regime(
    candles: Sequence[Candle],
    candle_at: int | None,
    fast: Sequence[Decimal | None],
    slow: Sequence[Decimal | None],
) -> str | None:
    if candle_at is None:
        return None
    f = fast[candle_at]
    s = slow[candle_at]
    if f is None or s is None:
        return None
    if f > s:
        return "BULL"
    if f < s:
        return "BEAR"
    return "FLAT"


def build_ema_trend_stream(
    bars: Sequence[MarketPrice], params: EMATrendParams
) -> tuple[list[Signal15m], dict]:
    candles, candle_at = aggregate_candles(bars, params.bars_per_candle)
    closes = [c.close for c in candles]
    fast = ema(closes, params.fast)
    slow = ema(closes, params.slow)
    raw = donchian_breakout_signals(
        bars, entry_channel=params.entry_channel, exit_channel=params.exit_channel
    )
    stream: list[Signal15m] = []
    regimes: list[str] = []
    for i in range(len(bars)):
        reg = _ema_regime(candles, candle_at[i], fast, slow)
        regimes.append(reg if reg else "WARMUP")
        intent = _RAW_TO_INTENT[raw[i].signal.value]
        if reg == "BULL":
            stream.append(BULLISH if intent == BULLISH else NEUTRAL)
        elif reg == "BEAR":
            stream.append(BEARISH if intent == BEARISH else NEUTRAL)
        else:
            stream.append(NEUTRAL)
    diagnostics = {
        "params": {
            "bars_per_candle": params.bars_per_candle,
            "fast": params.fast,
            "slow": params.slow,
            "entry_channel": params.entry_channel,
            "exit_channel": params.exit_channel,
            "warmup_candles": max(params.fast, params.slow) + 1,
        },
        "regime_bucket": _count_values(regimes),
        "note": (
            "1H EMA(20/50) gate over the trigger.  BULL regime allows only "
            "BULLISH trigger legs; BEAR regime allows only BEARISH legs; a "
            "non-aligned trigger leg is suppressed to NEUTRAL (which exits an "
            "open leg through the position state machine) and never becomes a "
            "counter-trend entry."
        ),
    }
    return stream, diagnostics


def build_orb_stream(
    bars: Sequence[MarketPrice], params: ORBParams
) -> tuple[list[Signal15m], dict]:
    day_range: dict[date, tuple[Decimal, Decimal]] = {}
    day_bars: dict[date, list[int]] = {}
    for i, b in enumerate(bars):
        day_bars.setdefault(b.timestamp.date(), []).append(i)
    range_days = 0
    for d, idxs in day_bars.items():
        if len(idxs) < params.range_bars:
            continue
        head = idxs[: params.range_bars]
        day_range[d] = (
            _decimal_max([bars[k].high for k in head]),
            _decimal_min([bars[k].low for k in head]),
        )
        range_days += 1
    stream: list[Signal15m] = []
    in_range: list[bool] = []
    for i, b in enumerate(bars):
        rng = day_range.get(b.timestamp.date())
        s = session_minute_index(b.timestamp)
        if rng is None or s < params.range_bars:
            stream.append(NEUTRAL)
            in_range.append(False)
        else:
            in_range.append(True)
            if b.close > rng[0]:
                stream.append(BULLISH)
            elif b.close < rng[1]:
                stream.append(BEARISH)
            else:
                stream.append(NEUTRAL)
    diagnostics = {
        "params": {
            "range_bars": params.range_bars,
            "first_tradable_reference_minute": "09:30",
            "first_tradable_decision_minute": "09:35",
            "bar_defined": "range never redefined after the first three candles",
        },
        "range_days": range_days,
        "in_range_share": round(sum(1 for v in in_range if v) / len(in_range), 6),
        "note": (
            "Opening range = high/low of exactly the first three 5m session "
            "candles (09:15/09:20/09:25).  First reference bar that can trade "
            "is the 09:30 candle (decision 09:35); decisions on 09:20/09:25/"
            "09:30 are NEUTRAL.  Close > range high => BULLISH, close < range "
            "low => BEARISH, inside => NEUTRAL."
        ),
    }
    return stream, diagnostics


def _atr_base_series(
    atr_series: Sequence[Decimal | None], period: int
) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(atr_series)
    valid: list[Decimal] = []
    for i, v in enumerate(atr_series):
        if v is not None:
            valid.append(v)
        if len(valid) >= period:
            out[i] = sum(valid[-period:], Decimal("0")) / period
    return out


def build_atr_regime_stream(
    bars: Sequence[MarketPrice], params: ATRRegimeParams
) -> tuple[list[Signal15m], dict]:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    atr_series = atr(highs, lows, closes, params.atr_period)
    base = _atr_base_series(atr_series, params.regime_base)
    mr_mid = sma(closes, params.mr_sma)
    raw = donchian_breakout_signals(
        bars, entry_channel=params.entry_channel, exit_channel=params.exit_channel
    )
    stream: list[Signal15m] = []
    regimes: list[str] = []
    for i in range(len(bars)):
        av = atr_series[i]
        bv = base[i]
        if av is None or bv is None:
            regimes.append("WARMUP")
            stream.append(NEUTRAL)
            continue
        if av > params.trend_mult * bv:
            reg = "TREND"
        elif av < params.range_mult * bv:
            reg = "RANGE"
        else:
            reg = "TRANSITION"
        regimes.append(reg)
        if reg == "TREND":
            stream.append(_RAW_TO_INTENT[raw[i].signal.value])
        elif reg == "RANGE":
            mid = mr_mid[i]
            lower = mid - params.mr_band * av
            upper = mid + params.mr_band * av
            if closes[i] < lower:
                stream.append(BULLISH)
            elif closes[i] > upper:
                stream.append(BEARISH)
            else:
                stream.append(NEUTRAL)
        else:
            stream.append(NEUTRAL)
    diagnostics = {
        "params": {
            "atr_period": params.atr_period,
            "regime_base": params.regime_base,
            "trend_mult": str(params.trend_mult),
            "range_mult": str(params.range_mult),
            "mr_sma": params.mr_sma,
            "mr_band": str(params.mr_band),
            "warmup_bars": (
                params.atr_period + params.regime_base + params.mr_sma
            ),
        },
        "regime_bucket": _count_values(regimes),
        "note": (
            "ATR(14) vs its 50-ATR rolling base.  TREND (ATR > 1.15x base): the "
            "Donchian trigger drives both entries and exits.  RANGE (ATR < "
            "0.85x base): mean-reversion vs SMA(20) +/- 1.5xATR.  TRANSITION: "
            "NEUTRAL."
        ),
    }
    return stream, diagnostics


def _day_open_close(bars: Sequence[MarketPrice]) -> dict[date, tuple[Decimal, Decimal]]:
    out: dict[date, tuple[Decimal, Decimal]] = {}
    for b in bars:
        d = b.timestamp.date()
        if d not in out:
            out[d] = (b.open, b.close)
        else:
            out[d] = (out[d][0], b.close)
    return out


def build_mtf_hybrid_stream(
    bars: Sequence[MarketPrice], params: MTFHybridParams
) -> tuple[list[Signal15m], dict]:
    raw = donchian_breakout_signals(
        bars, entry_channel=params.entry_channel, exit_channel=params.exit_channel
    )
    m15, m15_at = aggregate_candles(bars, params.m15_bars)
    h1, h1_at = aggregate_candles(bars, params.h1_bars)
    m15_fast = ema([c.close for c in m15], params.fast)
    m15_slow = ema([c.close for c in m15], params.slow)
    h1_fast = ema([c.close for c in h1], params.fast)
    h1_slow = ema([c.close for c in h1], params.slow)

    oc = _day_open_close(bars)
    dates = list(oc.keys())
    prev_day: dict[date, tuple[Decimal, Decimal]] = {
        dates[k]: oc[dates[k - 1]] for k in range(1, len(dates))
    }

    stream: list[Signal15m] = []
    confluence: list[str] = []
    for i in range(len(bars)):
        m15_reg = _ema_regime(m15, m15_at[i], m15_fast, m15_slow)
        h1_reg = _ema_regime(h1, h1_at[i], h1_fast, h1_slow)
        prev = prev_day.get(bars[i].timestamp.date())
        if prev is not None:
            pd_bull = prev[1] > prev[0]
            pd_bear = prev[1] < prev[0]
        else:
            pd_bull = pd_bear = False
        bull = m15_reg == "BULL" and h1_reg == "BULL" and pd_bull
        bear = m15_reg == "BEAR" and h1_reg == "BEAR" and pd_bear
        if bull:
            confluence.append("BULL")
            stream.append(BULLISH if _RAW_TO_INTENT[raw[i].signal.value] == BULLISH else NEUTRAL)
        elif bear:
            confluence.append("BEAR")
            stream.append(BEARISH if _RAW_TO_INTENT[raw[i].signal.value] == BEARISH else NEUTRAL)
        else:
            confluence.append("NONE")
            stream.append(NEUTRAL)
    diagnostics = {
        "params": {
            "m15_bars": params.m15_bars,
            "h1_bars": params.h1_bars,
            "fast": params.fast,
            "slow": params.slow,
            "entry_channel": params.entry_channel,
            "exit_channel": params.exit_channel,
            "use_prev_day_context": params.use_prev_day_context,
            "warmup_candles_15m": max(params.fast, params.slow) + 1,
            "warmup_candles_1h": max(params.fast, params.slow) + 1,
        },
        "confluence_bucket": _count_values(confluence),
        "note": (
            "Entry leg requires the Donchian trigger AND 15m EMA(20/50) AND 1H "
            "EMA(20/50) AND the previous session's close direction to agree.  "
            "The first session of the series has no previous-session context "
            "and therefore cannot signal.  Non-aligned triggers are suppressed "
            "to NEUTRAL (exit-only)."
        ),
    }
    return stream, diagnostics


def _build_not_testable(
    bars: Sequence[MarketPrice], _params: object
) -> tuple[list[Signal15m], dict]:
    return [NEUTRAL] * len(bars), {"not_testable_reason": VWAP_VOLUME_ZERO_NOTE}


def _count_values(values: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    family: str
    label: str
    params: object
    builder: Callable[
        [Sequence[MarketPrice], object], tuple[list[Signal15m], dict]
    ]
    baseline: bool = False
    not_testable_reason: str | None = None
    note: str = ""


def build_stream(
    spec: CandidateSpec, bars: Sequence[MarketPrice]
) -> tuple[list[Signal15m], dict]:
    """Dispatch ``spec`` over a full-series bar slice; causal per-bar output."""
    return spec.builder(bars, spec.params)


def apply_overrides(params: object, overrides: dict) -> object:
    """Return a frozen clone of ``params`` with pre-registered perturbations."""
    return replace(params, **overrides)


CANDIDATES: dict[str, CandidateSpec] = {
    "donchian_20_10_baseline": CandidateSpec(
        candidate_id="donchian_20_10_baseline",
        family="reference",
        label="Donchian 20/10 baseline (frozen reference)",
        params=DonchianParams(),
        builder=build_donchian_stream,
        baseline=True,
        note="Read-only frozen reference; identity-checked against the recorded fingerprint.",
    ),
    "c1_ema_trend": CandidateSpec(
        candidate_id="c1_ema_trend",
        family="trend_gate",
        label="1H EMA(20/50) trend gate over Donchian trigger",
        params=EMATrendParams(),
        builder=build_ema_trend_stream,
        note="Entries only in 1H-trend-aligned trigger legs; trigger exits preserved.",
    ),
    "c2_orb": CandidateSpec(
        candidate_id="c2_orb",
        family="opening_range",
        label="Opening range breakout (first 3 candles)",
        params=ORBParams(),
        builder=build_orb_stream,
        note="Range fixed from the first three 09:15/09:20/09:25 candles.",
    ),
    "c3_vwap_mr": CandidateSpec(
        candidate_id="c3_vwap_mr",
        family="mean_reversion",
        label="VWAP mean reversion",
        params=DonchianParams(),
        builder=_build_not_testable,
        not_testable_reason=VWAP_VOLUME_ZERO_NOTE,
        note="NOT TESTABLE: recorded volume == 0 on every bar.",
    ),
    "c4_atr_regime": CandidateSpec(
        candidate_id="c4_atr_regime",
        family="atr_regime",
        label="ATR(14) regime: trend breakout / range mean reversion",
        params=ATRRegimeParams(),
        builder=build_atr_regime_stream,
        note="TREND >1.15x ATR base => Donchian; RANGE <0.85x => SMA+/-1.5xATR.",
    ),
    "c5_mtf_hybrid": CandidateSpec(
        candidate_id="c5_mtf_hybrid",
        family="multitimeframe",
        label="Multi-timeframe confluence (5m/15m/1H + prior session)",
        params=MTFHybridParams(),
        builder=build_mtf_hybrid_stream,
        note="Donchian AND 15m EMA AND 1H EMA AND prior-session direction.",
    ),
}

# Pre-registered robustness perturbations (documented, never tuned post-OOS).
PARAM_OVERRIDES: dict[str, list[dict]] = {
    "c1_ema_trend": [
        {"fast": 16, "slow": 40},
        {"fast": 25, "slow": 60},
    ],
    "c2_orb": [
        {"range_bars": 2},
        {"range_bars": 4},
    ],
    "c4_atr_regime": [
        {"trend_mult": Decimal("1.05"), "range_mult": Decimal("0.85")},
        {"trend_mult": Decimal("1.30"), "range_mult": Decimal("0.85")},
        {"trend_mult": Decimal("1.15"), "range_mult": Decimal("0.75")},
        {"trend_mult": Decimal("1.15"), "range_mult": Decimal("0.95")},
        {"mr_band": Decimal("1.2")},
        {"mr_band": Decimal("1.8")},
    ],
    "c5_mtf_hybrid": [
        {"fast": 16, "slow": 40},
        {"fast": 25, "slow": 60},
    ],
}