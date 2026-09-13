"""Research challengers for the frozen MA(5,21) champion (evaluation-only, WS 7.16).

Each candidate is a small, explicit, evidence-driven hypothesis derived from the
champion failure audit (`docs/champion_failure_audit.md`). Every candidate is
decided at bar *i* from ``bars[:i+1]`` only — no look-ahead, no future regime,
no future outcome. They are pure decision functions; nothing here places an
order or touches risk/execution code.

Two layers per candidate:
    * an O(n) signal *provider* (``*_signals(bars, **params)``) that returns one
      :class:`SignalResult` per bar, used by the evaluation harness with the
      engine's ``signals=`` shortcut; and
    * a :class:`Strategy` subclass whose ``analyze`` returns ``provider(bars)[-1]``
      so the candidate is a drop-in for the existing strategy interface and tests.

The providers are the source of truth; equivalence between the two layers for a
given prefix is asserted in ``tests/test_research_candidates.py``.

Candidate list (rationale in ``CANDIDATES``):
    c1 long_only_ma_cross        MA(5,21) crossover, long only.
    c2 slow_long_only_ma_cross   MA(20,50) crossover, long only.
    c3 momentum_gated_ma_cross   MA(5,21) both sides, entries gated by prior-bar
                                 momentum sign.
    c4 trend_gated_ma_cross      MA(5,21) both sides, entries gated by MA-gap
                                 trend sign.
    c5 donchian_breakout         Donchian channel breakout (20/10), both sides.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Callable, Sequence

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.utils.functions import positive_int


# ---------------------------------------------------------------------------
# shared decision-time helpers
# ---------------------------------------------------------------------------


def _sma_deque(bars: Sequence[MarketPrice], period: int) -> Decimal | None:
    if len(bars) < period:
        return None
    return sum((b.close for b in bars[-period:]), Decimal("0")) / period


def _cross_event(
    bars: Sequence[MarketPrice], fast: int, slow: int, i: int
) -> tuple[Signal | None, Decimal | None, dict[str, str]]:
    """Crossover event at bar ``i`` plus decision-time feature meta.

    Returns ``(signal, ma_gap_pct, meta)`` where ``signal`` is BUY/SELL/None and
    everything is a function of ``bars[:i+1]``.
    """
    if i + 1 < slow + 1:
        return None, None, {"warmup": f"need {slow + 1} bars"}
    window = list(bars[i - slow : i + 1])
    closes = [b.close for b in window]
    fast_now = sum(closes[-fast:], Decimal("0")) / fast
    slow_now = sum(closes[-slow:], Decimal("0")) / slow
    prev = closes[:-1]
    fast_prev = sum(prev[-fast:], Decimal("0")) / fast
    slow_prev = sum(prev[-slow:], Decimal("0")) / slow
    gap_pct = (
        ((fast_now - slow_now) / slow_now) * Decimal("100") if slow_now != 0 else None
    )
    meta = {
        "fast": str(fast_now),
        "slow": str(slow_now),
        "fast_prev": str(fast_prev),
        "slow_prev": str(slow_prev),
        "ma_gap_pct": str(gap_pct) if gap_pct is not None else "",
        "last_close": str(bars[i].close),
    }
    if fast_prev <= slow_prev and fast_now > slow_now:
        return Signal.BUY, gap_pct, meta
    if fast_prev >= slow_prev and fast_now < slow_now:
        return Signal.SELL, gap_pct, meta
    return None, gap_pct, meta


def _make_hold(bridge: MarketPrice, reason: str, meta: dict[str, str]) -> SignalResult:
    return SignalResult(
        signal=Signal.HOLD,
        instrument=bridge.instrument,
        timestamp=bridge.timestamp,
        reason=reason,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# c1 / c2 — long-only MA crossover
# ---------------------------------------------------------------------------


def long_only_ma_cross_signals(
    bars: Sequence[MarketPrice], *, fast: int = 5, slow: int = 21
) -> list[SignalResult]:
    """MA crossover, long only: up-cross opens a long, down-cross closes it; a
    down-cross with no prior long is HOLD (never opens a short)."""
    fast = positive_int(fast, "fast")
    slow = positive_int(slow, "slow")
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")

    n = len(bars)
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    state = 0  # 0 flat, +1 long
    for i, bridge in enumerate(bars):
        event, gap_pct, meta = _cross_event(bars, fast, slow, i)
        if i + 1 < slow + 1:
            signals[i] = _make_hold(bridge, "warmup", meta)
            continue
        if event is Signal.BUY and state == 0:
            state = 1
            signals[i] = SignalResult(
                signal=Signal.BUY,
                instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason=f"long-only up-cross fast<{fast} slow<{slow}",
                meta=meta,
            )
        elif event is Signal.SELL and state == 1:
            state = 0
            signals[i] = SignalResult(
                signal=Signal.SELL,
                instrument=bridge.instrument,
                timestamp=bridge.timestamp,
                reason="long-only down-cross closes long",
                meta=meta,
            )
        else:
            signals[i] = _make_hold(bridge, "no long-only action", meta)
    return signals


class LongOnlyMovingAverageCross(Strategy):
    """Strategy-interface wrapper for :func:`long_only_ma_cross_signals`."""

    name = "long_only_ma_cross"

    def __init__(self, fast: int = 5, slow: int = 21) -> None:
        self.fast = fast
        self.slow = slow

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return long_only_ma_cross_signals(bars, fast=self.fast, slow=self.slow)[-1]


# ---------------------------------------------------------------------------
# c3 — momentum-gated MA crossover (both sides)
# ---------------------------------------------------------------------------


def momentum_gated_ma_cross_signals(
    bars: Sequence[MarketPrice],
    *,
    fast: int = 5,
    slow: int = 21,
    momentum_bars: int = 5,
) -> list[SignalResult]:
    """MA(5,21) both sides; *opening* signals are gated on the sign of the prior
    ``momentum_bars`` return. Closing signals always pass. Fights the audit's
    counter-trend churn: shorts only against downward momentum, longs only with
    upward momentum."""
    fast = positive_int(fast, "fast")
    slow = positive_int(slow, "slow")
    momentum_bars = positive_int(momentum_bars, "momentum_bars")

    def momentum_at(bars: Sequence[MarketPrice], i: int) -> Decimal | None:
        prior = i - momentum_bars
        if prior < 0 or bars[prior].close == 0:
            return None
        return (bars[i].close - bars[prior].close) / bars[prior].close

    n = len(bars)
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    state = 0  # 0 flat, +1 long, -1 short
    for i, bridge in enumerate(bars):
        event, gap_pct, meta = _cross_event(bars, fast, slow, i)
        if i + 1 < slow + 1:
            signals[i] = _make_hold(bridge, "warmup", meta)
            continue
        mom = momentum_at(bars, i)
        meta["momentum"] = str(mom) if mom is not None else ""
        if event is Signal.BUY:
            if state == -1:
                state = 0
                signals[i] = SignalResult(
                    signal=Signal.BUY,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason="momentum-gated up-cross closes short",
                    meta=meta,
                )
            elif state == 0 and mom is not None and mom > 0:
                state = 1
                signals[i] = SignalResult(
                    signal=Signal.BUY,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"momentum-gated BUY (mom={mom})",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "long entry gated or redundant", meta)
        elif event is Signal.SELL:
            if state == 1:
                state = 0
                signals[i] = SignalResult(
                    signal=Signal.SELL,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason="momentum-gated down-cross closes long",
                    meta=meta,
                )
            elif state == 0 and mom is not None and mom < 0:
                state = -1
                signals[i] = SignalResult(
                    signal=Signal.SELL,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"momentum-gated SELL (mom={mom})",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "short entry gated or redundant", meta)
        else:
            signals[i] = _make_hold(bridge, "no crossover", meta)
    return signals


class MomentumGatedMovingAverageCross(Strategy):
    """Strategy-interface wrapper for :func:`momentum_gated_ma_cross_signals`."""

    name = "momentum_gated_ma_cross"

    def __init__(self, fast: int = 5, slow: int = 21, momentum_bars: int = 5) -> None:
        self.fast = fast
        self.slow = slow
        self.momentum_bars = momentum_bars

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return momentum_gated_ma_cross_signals(
            bars, fast=self.fast, slow=self.slow, momentum_bars=self.momentum_bars
        )[-1]


# ---------------------------------------------------------------------------
# c4 — trend-gated MA crossover (both sides)
# ---------------------------------------------------------------------------


def trend_gated_ma_cross_signals(
    bars: Sequence[MarketPrice],
    *,
    fast: int = 5,
    slow: int = 21,
    trend_threshold_pct: Decimal | float | int | str = "0.05",
) -> list[SignalResult]:
    """MA(5,21) both sides; *opening* signals are gated on the decision-time
    fast/slow MA gap (trend). Longs open only when the gap is above the upper
    threshold, shorts only when below the lower threshold; closes always pass."""
    fast = positive_int(fast, "fast")
    slow = positive_int(slow, "slow")
    threshold = Decimal(str(trend_threshold_pct))
    if threshold < 0:
        raise ValueError("trend_threshold_pct must be non-negative")

    n = len(bars)
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    state = 0  # 0 flat, +1 long, -1 short
    for i, bridge in enumerate(bars):
        event, gap_pct, meta = _cross_event(bars, fast, slow, i)
        if i + 1 < slow + 1:
            signals[i] = _make_hold(bridge, "warmup", meta)
            continue
        gate_up = gap_pct is not None and gap_pct > threshold
        gate_down = gap_pct is not None and gap_pct < -threshold
        meta["trend_gate_up"] = str(gate_up)
        meta["trend_gate_down"] = str(gate_down)
        if event is Signal.BUY:
            if state == -1:
                state = 0
                signals[i] = SignalResult(
                    signal=Signal.BUY,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason="trend-gated up-cross closes short",
                    meta=meta,
                )
            elif state == 0 and gate_up:
                state = 1
                signals[i] = SignalResult(
                    signal=Signal.BUY,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"trend-gated BUY (gap={gap_pct})",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "long entry gated or redundant", meta)
        elif event is Signal.SELL:
            if state == 1:
                state = 0
                signals[i] = SignalResult(
                    signal=Signal.SELL,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason="trend-gated down-cross closes long",
                    meta=meta,
                )
            elif state == 0 and gate_down:
                state = -1
                signals[i] = SignalResult(
                    signal=Signal.SELL,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"trend-gated SELL (gap={gap_pct})",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "short entry gated or redundant", meta)
        else:
            signals[i] = _make_hold(bridge, "no crossover", meta)
    return signals


class TrendGatedMovingAverageCross(Strategy):
    """Strategy-interface wrapper for :func:`trend_gated_ma_cross_signals`."""

    name = "trend_gated_ma_cross"

    def __init__(self, fast: int = 5, slow: int = 21, trend_threshold_pct="0.05") -> None:
        self.fast = fast
        self.slow = slow
        self.trend_threshold_pct = trend_threshold_pct

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return trend_gated_ma_cross_signals(
            bars,
            fast=self.fast,
            slow=self.slow,
            trend_threshold_pct=self.trend_threshold_pct,
        )[-1]


# ---------------------------------------------------------------------------
# c5 — Donchian channel breakout (both sides)
# ---------------------------------------------------------------------------


def donchian_breakout_signals(
    bars: Sequence[MarketPrice],
    *,
    entry_channel: int = 20,
    exit_channel: int = 10,
) -> list[SignalResult]:
    """Breakout of a Donchian channel of the last ``entry_channel`` bars' extremes.

    Long when the close exceeds every high of the previous ``entry_channel`` bars
    (a new multi-bar high, no look-ahead), exit when the close falls back inside
    the low extreme of the last ``exit_channel`` bars. Shorts are symmetric.
    Produces fewer, longer holds — targeting the audit's finding that only
    multi-bar holds (51+ bars) were profitable.
    """
    entry_channel = positive_int(entry_channel, "entry_channel")
    exit_channel = positive_int(exit_channel, "exit_channel")
    if entry_channel <= exit_channel:
        raise ValueError("entry_channel must be wider than exit_channel")

    if len(bars) == 0:
        return []
    n = len(bars)
    signals: list[SignalResult] = [None] * n  # type: ignore[list-item]
    state = 0  # 0 flat, +1 long, -1 short
    warmup = max(entry_channel, exit_channel) + 1
    for i, bridge in enumerate(bars):
        if i + 1 < warmup:
            signals[i] = _make_hold(
                bridge, f"warmup need {warmup} bars", {"warmup": "1"}
            )
            continue
        prior_hi = max((b.high for b in bars[i - entry_channel : i]), default=bridge.high)
        prior_lo = min((b.low for b in bars[i - entry_channel : i]), default=bridge.low)
        exit_lo = min((b.low for b in bars[i - exit_channel : i]), default=bridge.low)
        exit_hi = max((b.high for b in bars[i - exit_channel : i]), default=bridge.high)
        meta = {
            "entry_hi_ref": str(prior_hi),
            "entry_lo_ref": str(prior_lo),
            "exit_hi_ref": str(exit_hi),
            "exit_lo_ref": str(exit_lo),
            "close": str(bridge.close),
        }
        if state == 0:
            if bridge.close > prior_hi:
                state = 1
                signals[i] = SignalResult(
                    signal=Signal.BUY,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"Donchian long breakout > {prior_hi}",
                    meta=meta,
                )
            elif bridge.close < prior_lo:
                state = -1
                signals[i] = SignalResult(
                    signal=Signal.SELL,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"Donchian short breakout < {prior_lo}",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "no breakout", meta)
        elif state == 1:
            if bridge.close < exit_lo:
                state = 0
                signals[i] = SignalResult(
                    signal=Signal.SELL,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"Donchian long exit < {exit_lo}",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "holding long", meta)
        else:
            if bridge.close > exit_hi:
                state = 0
                signals[i] = SignalResult(
                    signal=Signal.BUY,
                    instrument=bridge.instrument,
                    timestamp=bridge.timestamp,
                    reason=f"Donchian short exit > {exit_hi}",
                    meta=meta,
                )
            else:
                signals[i] = _make_hold(bridge, "holding short", meta)
    return signals


class DonchianBreakout(Strategy):
    """Strategy-interface wrapper for :func:`donchian_breakout_signals`."""

    name = "donchian_breakout"

    def __init__(self, entry_channel: int = 20, exit_channel: int = 10) -> None:
        self.entry_channel = entry_channel
        self.exit_channel = exit_channel

    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        return donchian_breakout_signals(
            bars, entry_channel=self.entry_channel, exit_channel=self.exit_channel
        )[-1]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

Provider = Callable[..., list[SignalResult]]

CANDIDATES: dict[str, dict[str, object]] = {
    "c1_long_only_ma_cross": {
        "provider": long_only_ma_cross_signals,
        "params": {"fast": 5, "slow": 21},
        "strategy": LongOnlyMovingAverageCross,
        "perturbations": {"fast": [3, 8], "slow": [18, 26]},
        "rationale": (
            "Champion lost net -143% with 0/2601 long entries; index +34% drift over the "
            "window. Long-only MA(5,21) removes counter-trend shorts."
        ),
    },
    "c2_slow_long_only_ma_cross": {
        "provider": long_only_ma_cross_signals,
        "params": {"fast": 20, "slow": 50},
        "strategy": LongOnlyMovingAverageCross,
        "perturbations": {"fast": [15, 26], "slow": [40, 60]},
        "rationale": (
            "Audit: median 13 bars between opposite signals; 70.7% of champion trades held "
            "<=20 bars were net losses. A 20/50 crossover forces 2.5x longer legs and far "
            "fewer flips; long-only."
        ),
    },
    "c3_momentum_gated_ma_cross": {
        "provider": momentum_gated_ma_cross_signals,
        "params": {"fast": 5, "slow": 21, "momentum_bars": 5},
        "strategy": MomentumGatedMovingAverageCross,
        "perturbations": {"momentum_bars": [3, 8]},
        "rationale": (
            "Audit: only the 97 champion trades with 51+ bar holds (downside "
            "continuation) were profitable. Gating each entry on the sign of the prior "
            "5-bar return suppresses counter-trend churn on both sides."
        ),
    },
    "c4_trend_gated_ma_cross": {
        "provider": trend_gated_ma_cross_signals,
        "params": {"fast": 5, "slow": 21, "trend_threshold_pct": "0.05"},
        "strategy": TrendGatedMovingAverageCross,
        "perturbations": {"trend_threshold_pct": ["0.02", "0.10"]},
        "rationale": (
            "Audit regime matrix: every entry landed in DOWN/SIDEWAYS regimes and no "
            "bucket was profitable. Requiring the MA-gap trend sign at the decision bar "
            "for each direction should cut the counter-trend subset."
        ),
    },
    "c5_donchian_breakout": {
        "provider": donchian_breakout_signals,
        "params": {"entry_channel": 20, "exit_channel": 10},
        "strategy": DonchianBreakout,
        "perturbations": {"entry_channel": [15, 30], "exit_channel": [8, 15]},
        "rationale": (
            "Audit: 51+ bar holds won at 96.9% WR; sub-20 bar holds all lost. A 20-bar "
            "(~1+ trading day) channel breakout enters only on multi-bar extremes and "
            "exits on 10-bar reversion, forcing much lower frequency."
        ),
    },
}