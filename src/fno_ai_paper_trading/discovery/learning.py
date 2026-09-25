"""Learning loop: mine failure patterns across the deck and propose the next
fundamentally different hypothesis.

Rules (from the discovery mandate):
* never "keep changing parameters until profitable" — the loop proposes a
  *different mechanism*, not a tighter stop;
* a 10-day profit is diagnostic, not proof;
* if gross edge is killed by costs -> cut frequency / require larger expected
  move, do not squeeze the stop;
* if there is no gross edge -> research a different hypothesis;
* sideways carnage -> regime filter / NO_TRADE gate;
* failed breakouts -> confirmation + volatility expansion;
* afternoon failure -> time-of-day filter;
* short-side carnage -> investigate long-side (and vice versa).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from fno_ai_paper_trading.discovery.backtest import DiscoveryRun, TradeRecord

_MIN_BUCKET_TRADES = 5


@dataclass(frozen=True)
class BucketLoss:
    key: str
    dimension: str
    trades: int
    net: Decimal

    def to_dict(self) -> dict[str, Any]:
        parts = self.key.split("|")
        return {
            "dimension": self.dimension,
            "label": parts[1] if len(parts) > 1 else parts[0],
            "trades": self.trades,
            "net": format(self.net, "f"),
        }


@dataclass
class FailureAnalysis:
    worst_buckets: list[BucketLoss]
    bullet: str
    gross_edge_exists: bool  # gross > 0 yet net <= 0
    cost_kill: bool
    next_hypothesis: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "worst_buckets": [b.to_dict() for b in self.worst_buckets],
            "bullet": self.bullet,
            "gross_edge_exists": self.gross_edge_exists,
            "cost_kill": self.cost_kill,
            "next_hypothesis": self.next_hypothesis,
        }


def _buckets(records: list[TradeRecord]) -> list[BucketLoss]:
    grouped: dict[tuple[str, str], list[TradeRecord]] = {}
    for record in records:
        dims = [
            ("regime", record.entry_meta.get("regime") or "UNKNOWN"),
            ("side", record.side),
            ("tod", f"{record.entry_time[:2]}:xx"),
            ("vol", record.entry_meta.get("volatility_bucket") or "UNKNOWN"),
        ]
        for dimension, label in dims:
            grouped.setdefault((dimension, label), []).append(record)
    losses: list[BucketLoss] = []
    for (dimension, label), items in grouped.items():
        if len(items) < _MIN_BUCKET_TRADES:
            continue
        net = sum((r.net_pnl for r in items), Decimal("0"))
        losses.append(BucketLoss(f"{dimension}|{label}", dimension, len(items), net))
    losses.sort(key=lambda b: (b.net, -b.trades))
    return losses


_HYPOTHESIS_RULES: list[tuple[str, str, dict[str, Any]]] = [
    ("regime", "SIDEWAYS",
     {"family": "REGIME_FILTERING", "mechanism": "explicit sideways/no-trade gate before entry",
      "read": "aggregate loss concentrates in sideways sessions; filter the regime, do not retune indicators"}),
    ("regime", "CONTRACTION",
     {"family": "VOLATILITY_REGIME", "mechanism": "require range expansion confirmation before any trade",
      "read": "losses concentrate in contracting ranges; wait for expansion"}),
    ("side", "PUT",
     {"family": "TREND_FOLLOWING", "mechanism": "CALL-only hypothesis in bullish/sideways regimes",
      "read": "short side bleeds; test a long/CALL-only variant before trying harder short entries"}),
    ("side", "CALL",
     {"family": "TREND_FOLLOWING", "mechanism": "PUT-only hypothesis in bearish regimes",
      "read": "long side bleeds; test a short/PUT-only variant before trying harder long entries"}),
    ("tod", "12:xx",
     {"family": "TIME_OF_DAY", "mechanism": "entries restricted to the morning window only",
      "read": "afternoon entries bleed; restrict the entry window before touching exits"}),
    ("tod", "13:xx",
     {"family": "TIME_OF_DAY", "mechanism": "entries restricted to the morning window only",
      "read": "afternoon entries bleed; restrict the entry window before touching exits"}),
    ("tod", "14:xx",
     {"family": "TIME_OF_DAY", "mechanism": "entries restricted to before the close",
      "read": "late entries bleed; restrict the entry window before touching exits"}),
    ("vol", "HIGH",
     {"family": "VOLATILITY_REGIME", "mechanism": "require normal-vol regime; no trades in high vol",
      "read": "high-volatility sessions bleed; gate on the vol bucket first"}),
    ("vol", "EXTREME",
     {"family": "VOLATILITY_REGIME", "mechanism": "require normal-vol regime; no trades in extreme vol",
      "read": "extreme-volatility sessions bleed; gate on the vol bucket first"}),
]


def analyze_failures(train_runs: list[DiscoveryRun], validation_runs: list[DiscoveryRun]) -> FailureAnalysis:
    records = [r for run in train_runs + validation_runs for r in run.records
               if run.trades > 0]
    losses = _buckets(records)

    gross = sum((r.gross for r in records), Decimal("0"))
    net = sum((r.net_pnl for r in records), Decimal("0"))
    costs = sum((r.costs for r in records), Decimal("0"))
    gross_edge_exists = gross > 0
    cost_kill = gross_edge_exists and net <= 0

    hypothesis: dict[str, Any] | None = None
    for dimension, label, spec in _HYPOTHESIS_RULES:
        match = next((b for b in losses if b.dimension == dimension and b.key.endswith(f"|{label}")), None)
        if match is not None and match.net < 0:
            hypothesis = dict(spec)
            hypothesis["trigger"] = f"{dimension}={label} net {match.net} over {match.trades} trades"
            break

    if hypothesis is None:
        if cost_kill:
            hypothesis = {
                "family": "FREQUENCY_EDGE",
                "mechanism": "cut trade frequency and require a minimum expected move > 2x cost per side",
                "read": f"gross edge {gross} is eaten by costs {costs}; reduce frequency, do not tighten stops",
                "trigger": f"gross {gross} > 0 but net {net} <= 0",
            }
        else:
            hypothesis = {
                "family": "GENERATOR_ROUND_2",
                "mechanism": "newer fundamentally different families (e.g. intraday VWAP band, opening range mean reversion, options-theta aware time filters) with the same evidence discipline",
                "read": f"no gross edge surviving costs (gross {gross}, net {net}); research a different mechanism",
                "trigger": "no strong single failure bucket",
            }

    bullet = (
        f"Deck fired {len(records)} trades across {len(train_runs) + len(validation_runs)} runs: "
        f"gross {gross}, costs {costs}, net {net}. "
    )
    if cost_kill:
        bullet += "Costs destroy the gross edge — next step must cut frequency, never tighten stops."
    elif not gross_edge_exists:
        bullet += "No gross edge exists — next step must be a fundamentally different hypothesis."
    else:
        bullet += "Next iteration targets the worst loss bucket first."

    return FailureAnalysis(
        worst_buckets=losses[:6],
        bullet=bullet,
        gross_edge_exists=gross_edge_exists,
        cost_kill=cost_kill,
        next_hypothesis=hypothesis,
    )