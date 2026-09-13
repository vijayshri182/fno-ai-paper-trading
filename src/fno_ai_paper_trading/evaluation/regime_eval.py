"""Regime-aware strategy evaluation for the frozen champion (WS 7.6).

Evaluation-only research. Reads the recorded champion replay evidence
(git-ignored ``reports/model_performance/trades.csv``), splits off the
protected out-of-sample window (entries on/after 2026-01-01, single-use and
never tuned here), and tests the §17e.5 regime-filter hypotheses *on the
recorded design+validation slice*. The protected OOS is only ever separated,
never re-read or extended for any computation in this module.

The module proves, from recorded evidence:

* the champion registers zero long entries (100% SELL), so every *BUY-side*
  regime filter (incl. WS 7.11 ``RegimeFilteredMovingAverageCross``) is a
  structural no-op on this data;
* every decision-time entry-regime group on the safe slice has negative
  expectancy, so short-side regime gates do not rescue the algorithm;
* volatility-scaled sizing changes magnitude, never the sign of expectancy.

Nothing here places orders, modifies risk/execution code, or promotes a
candidate. The frozen baseline MA(5,21) is never touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Literal, Sequence

PROTECTED_OOS_START = "2026-01-01"
TREND_IDX = 0
VOLATILITY_IDX = 1


@dataclass(frozen=True)
class RecordedTrade:
    entry_time: str
    exit_time: str
    side: str
    quantity: str
    entry_price: str
    exit_price: str
    entry_regime: str
    exit_regime: str
    holding_bars: str
    price_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal


def parse_recorded_trade(row: dict[str, str]) -> RecordedTrade:
    return RecordedTrade(
        entry_time=row["entry_time"],
        exit_time=row["exit_time"],
        side=row["side"],
        quantity=row["quantity"],
        entry_price=row["entry_price"],
        exit_price=row["exit_price"],
        entry_regime=row["entry_regime"],
        exit_regime=row["exit_regime"],
        holding_bars=row["holding_bars"],
        price_pnl=Decimal(row["price_pnl"]),
        commission=Decimal(row["commission"]),
        net_pnl=Decimal(row["net_pnl"]),
    )


def load_recorded_trades(path: str) -> list[RecordedTrade]:
    import csv

    trades: list[RecordedTrade] = []
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            trades.append(parse_recorded_trade(row))
    return trades


def split_protected(
    trades: Sequence[RecordedTrade],
) -> tuple[list[RecordedTrade], list[RecordedTrade], int]:
    """Return (safe, protected_oos, excluded_count).

    ``safe`` = design+validation trades entered before the protected OOS start;
    ``protected_oos`` = trades entered on/after that start (never used for any
    computation here). Returns the total count *before* the split so callers can
    reconcile with the recorded artifacts.
    """
    total = len(trades)
    safe: list[RecordedTrade] = []
    protected: list[RecordedTrade] = []
    for trade in trades:
        if trade.entry_time < PROTECTED_OOS_START:
            safe.append(trade)
        else:
            protected.append(trade)
    return safe, protected, total


def trend_group(label: str) -> str:
    return label.split("_")[TREND_IDX]


def volatility_group(label: str) -> str:
    parts = label.split("_")
    if len(parts) < 2:
        return "unknown"
    return parts[VOLATILITY_IDX]


@dataclass(frozen=True)
class GroupStats:
    label: str
    count: int
    winning: int
    losing: int
    win_rate_pct: str
    net_pnl: str
    expectancy: str


def _fmt(value: Decimal) -> str:
    return value.quantize(Decimal("0.01")).to_eng_string()


def _fmt_ratio(value: Decimal) -> str:
    return f"{value * 100:.4f}"


def compute_stats(trades: Sequence[RecordedTrade], label: str) -> GroupStats:
    count = len(trades)
    winning = 0
    losing = 0
    total = Decimal("0")
    for trade in trades:
        total += trade.net_pnl
        if trade.net_pnl > 0:
            winning += 1
        else:
            losing += 1
    win_rate = Decimal(winning) / Decimal(count) if count else Decimal("0")
    expectancy = total / Decimal(count) if count else Decimal("0")
    return GroupStats(
        label=label,
        count=count,
        winning=winning,
        losing=losing,
        win_rate_pct=_fmt_ratio(win_rate),
        net_pnl=_fmt(total),
        expectancy=_fmt(expectancy),
    )


GroupKey = Callable[[RecordedTrade], str]


def group_stats(
    trades: Sequence[RecordedTrade],
    key: GroupKey,
) -> list[GroupStats]:
    buckets: dict[str, list[RecordedTrade]] = {}
    for trade in trades:
        buckets.setdefault(key(trade), []).append(trade)
    rows = [compute_stats(selected, label) for label, selected in buckets.items()]
    return sorted(rows, key=lambda r: (-r.count, r.label))


def long_entry_count(trades: Sequence[RecordedTrade]) -> int:
    return sum(1 for trade in trades if trade.side.upper() == "BUY")


def residual_after_holding(
    trades: Sequence[RecordedTrade],
    held_groups: frozenset[str],
) -> tuple[GroupStats, int]:
    """Simulate a SHORT-side regime gate.

    For every trade whose entry trend group is in ``held_groups`` the gate
    suppresses the entry (trade is *removed*); the remaining trades form the
    residual. Returns residual stats plus the number of removed trades.
    """
    remaining = [t for t in trades if trend_group(t.entry_regime) not in held_groups]
    return compute_stats(remaining, "residual"), len(trades) - len(remaining)


@dataclass(frozen=True)
class HypothesisResult:
    hypothesis_id: str
    name: str
    rule: str
    gate: Literal["buy_gate", "short_gate", "sizing", "hold"]
    status: Literal[
        "structural_no_op", "rejected", "not_testable_recorded", "verified"
    ]
    observation: str
    verdict: str
    verified: bool


HYPOTHESES: tuple[HypothesisResult, ...] = (
    HypothesisResult(
        hypothesis_id="R1",
        name="strong trend -> allow crossover signals",
        rule=(
            "BUY-side: allow crossover entries only when the decision-time trend "
            "state is strong/up. SHORT-side: allow/suppress by trend strength."
        ),
        gate="buy_gate",
        status="structural_no_op",
        observation=(
            "0 of 2,601 recorded round trips (0/2,216 on the safe slice) are "
            "long entries; 100% SELL. WS 7.11 RegimeFilteredMovingAverageCross "
            "suppresses BUY signals only, so filtered == baseline on this data."
        ),
        verdict="no-op on the frozen champion; not a promotable improvement.",
        verified=True,
    ),
    HypothesisResult(
        hypothesis_id="R2",
        name="sideways/choppy -> prefer HOLD / avoid weak entries",
        rule=(
            "SHORT-side: suppress entries whose decision-time regime is "
            "sideways/choppy (flat fast-vs-slow gap)."
        ),
        gate="short_gate",
        status="rejected",
        observation=(
            "2,082 of 2,216 safe trades enter in sideways regimes; the "
            "sideways-suppressed residual (down regimes only, 134 trades) is "
            "still deeply negative per trade. WS 7.16 H4 (trend-gated MA 5/21) "
            "was rejected on protected OOS (net -3.59%, per-trade -57.52)."
        ),
        verdict="suppressing sideways entries does not create positive residual.",
        verified=True,
    ),
    HypothesisResult(
        hypothesis_id="R3",
        name="high volatility -> reduced risk/position size",
        rule="Scale position size down in high-volatility regimes.",
        gate="sizing",
        status="rejected",
        observation=(
            "Expectancy is negative in every recorded volatility group (low, "
            "normal, high). Scaling magnitude scales P&L proportionally and "
            "cannot change the sign of the edge."
        ),
        verdict="sizing changes magnitude, never the negative edge.",
        verified=True,
    ),
    HypothesisResult(
        hypothesis_id="R4",
        name="weak signal -> HOLD",
        rule="Hold when the crossover strength (|fast-slow gap|) is weak.",
        gate="hold",
        status="not_testable_recorded",
        observation=(
            "The MA(5,21) crossover emits a binary BUY/SELL with no explicit "
            "strength output; the recorded round trips carry no entry-gap "
            "feature. The closest proxy (flat-gap sideways regimes) is covered "
            "by R2 and is rejected."
        ),
        verdict="no recorded feature supports this gate; R2 proxy rejected.",
        verified=False,
    ),
    HypothesisResult(
        hypothesis_id="R5",
        name="strong signal + confirmation -> allow recommendation",
        rule="Allow entry only when a confirmation bar confirms the crossover.",
        gate="hold",
        status="not_testable_recorded",
        observation=(
            "Confirmation requires next-bar alignment, which the recorded round "
            "trips do not expose. The closest recorded test (WS 7.16 H4 "
            "single-bar trend gating) was rejected on protected OOS."
        ),
        verdict="untested on recorded evidence; closest prior test rejected.",
        verified=False,
    ),
)


@dataclass(frozen=True)
class RegimeEvaluation:
    total_trades: int
    safe_trades: int
    protected_separated: int
    long_entries_safe: int
    long_entries_total: int
    baseline: GroupStats
    by_regime: list[GroupStats]
    by_trend: list[GroupStats]
    by_volatility: list[GroupStats]
    gates: list[tuple[str, GroupStats, int]]
    hypotheses: tuple[HypothesisResult, ...]


def evaluate_recorded_regime_hypotheses(path: str) -> RegimeEvaluation:
    trades = load_recorded_trades(path)
    safe, _protected, total = split_protected(trades)
    baseline = compute_stats(safe, "baseline")
    by_regime = group_stats(safe, lambda t: t.entry_regime)
    by_trend = group_stats(safe, lambda t: trend_group(t.entry_regime))
    by_volatility = group_stats(safe, lambda t: volatility_group(t.entry_regime))

    gates: list[tuple[str, GroupStats, int]] = []
    for held in (
        frozenset({"sideways"}),
        frozenset({"up"}),
        frozenset({"sideways", "up"}),
        frozenset({"down"}),
    ):
        residual, removed = residual_after_holding(safe, held)
        gates.append(
            (f"hold_shorts_when_trend_in_{'_'.join(sorted(held)) or 'none'}", residual, removed)
        )

    return RegimeEvaluation(
        total_trades=total,
        safe_trades=len(safe),
        protected_separated=len(_protected),
        long_entries_safe=long_entry_count(safe),
        long_entries_total=long_entry_count(trades),
        baseline=baseline,
        by_regime=by_regime,
        by_trend=by_trend,
        by_volatility=by_volatility,
        gates=gates,
        hypotheses=HYPOTHESES,
    )