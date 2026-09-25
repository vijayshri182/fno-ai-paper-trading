"""Competition engine: transparent ranking + promotion decision over evidence.

Scoring is explicit and bounded (0..100) so a fragile high-profit candidate can
never auto-win. Every component is published with its value and a reason line;
the out-of-sample component is hard-wired to 0 because the protected OOS window
(>= the model_0 boundary) is never evaluated during discovery — absence of an
OOS proof is reported, not assumed.

Promotion goes through the existing :class:`PromotionGate` with validation
evidence only; because the gate demands an out-of-sample period, the verdict is
always REJECT until genuine OOS evidence exists — exactly the honest, recorded
answer the learning loop needs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from fno_ai_paper_trading.promotion.gate import DeltaView, PromotionGate, PromotionVerdict
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

from fno_ai_paper_trading.discovery.backtest import DiscoveryRun

_MIN_TRADES_EVIDENCE = 10


@dataclass(frozen=True)
class ScoreComponents:
    profitability: float
    consistency: float
    profit_factor: float
    drawdown: float
    expectancy: float
    robustness: float
    cost_sensitivity: float
    out_of_sample: float  # always 0.0 during discovery (OOS protected)

    @property
    def total(self) -> float:
        return (self.profitability + self.consistency + self.profit_factor
                + self.drawdown + self.expectancy + self.robustness
                + self.cost_sensitivity + self.out_of_sample)

    def as_dict(self) -> dict[str, float]:
        return {
            "profitability": round(self.profitability, 4),
            "consistency": round(self.consistency, 4),
            "profit_factor": round(self.profit_factor, 4),
            "drawdown": round(self.drawdown, 4),
            "expectancy": round(self.expectancy, 4),
            "robustness": round(self.robustness, 4),
            "cost_sensitivity": round(self.cost_sensitivity, 4),
            "out_of_sample": self.out_of_sample,
            "total": round(self.total, 4),
        }


@dataclass(frozen=True)
class Scorecard:
    candidate_id: str
    version: str
    window: tuple[date, date]
    score: float
    components: ScoreComponents
    trades: int
    net_pnl: Decimal
    robustness_positive_fraction: float
    cost_positive_fraction: float
    flags: tuple[str, ...]
    reasons: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "version": self.version,
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "score": round(self.score, 4),
            "components": self.components.as_dict(),
            "trades": self.trades,
            "net_pnl": format(self.net_pnl, "f"),
            "robustness_positive_fraction": round(self.robustness_positive_fraction, 4),
            "cost_positive_fraction": round(self.cost_positive_fraction, 4),
            "flags": list(self.flags),
            "reasons": self.reasons,
        }


def _cap(value: float, ceiling: float) -> float:
    return max(0.0, min(value, ceiling))


def score_candidate(
    run: DiscoveryRun,
    *,
    robustness_positive_fraction: float = 0.0,
    cost_positive_fraction: float = 0.0,
) -> Scorecard:
    costs = run.costs or Decimal("1")
    net_ratio = float(run.net_pnl / costs)
    profitability = _cap(25.0 * min(net_ratio, 2.0) / 2.0, 25.0) if run.net_pnl > 0 else 0.0

    wr_term = min(run.win_rate / 30.0, 1.0)
    seg_frac = (run.positive_segments / run.segments) if run.segments else 0.0
    consistency = 20.0 * (0.5 * wr_term + 0.5 * seg_frac)

    pf = run.profit_factor
    pf_score = 15.0 * (min(pf, 3.0) / 3.0) if math.isfinite(pf) and pf > 0 else 0.0

    dd_score = 15.0 * max(0.0, 1.0 - run.max_drawdown_pct / 12.0)

    cost_per_trade = float(costs) / run.trades if run.trades else 1.0
    exp_ratio = (float(run.expectancy) / cost_per_trade) if cost_per_trade > 0 else 0.0
    exp_score = 10.0 * (min(exp_ratio, 1.5) / 1.5) if run.expectancy > 0 else 0.0

    robust = _cap(10.0 * robustness_positive_fraction, 10.0)
    cost_sens = _cap(5.0 * cost_positive_fraction, 5.0)
    oos = 0.0

    components = ScoreComponents(
        profitability=profitability,
        consistency=consistency,
        profit_factor=pf_score,
        drawdown=dd_score,
        expectancy=exp_score,
        robustness=robust,
        cost_sensitivity=cost_sens,
        out_of_sample=oos,
    )

    score = components.total
    flags: list[str] = []
    if run.trades < _MIN_TRADES_EVIDENCE:
        flags.append("insufficient_evidence")
        score = score * 0.5
    if run.trades == 0:
        flags.append("no_trades")
        score = 0.0
    if run.open_at_close_total:
        flags.append("unclosed_positions_at_eod")
    flags.append("out_of_sample_unproven")

    reasons = {
        "profitability": f"net/costs = {net_ratio:.3f} (25 pts at net >= 2x costs)",
        "consistency": f"WR {run.win_rate:.1f}% (cap 30%) + {seg_frac:.2f} positive segments",
        "profit_factor": f"PF {pf if not math.isfinite(pf) else round(pf, 2)} (15 pts at PF >= 3)",
        "drawdown": f"maxDD {run.max_drawdown_pct:.2f}% (15 pts at 0%, 0 pts at >= 12%)",
        "expectancy": f"expectancy/cost-per-trade = {exp_ratio:.3f} (10 pts at >= 1.5)",
        "robustness": f"{robustness_positive_fraction:.2f} of perturbations remain profitable",
        "cost_sensitivity": f"{cost_positive_fraction:.2f} of cost multipliers stay profitable",
        "out_of_sample": "protected OOS window is NEVER evaluated during discovery",
    }

    return Scorecard(
        candidate_id=run.candidate_id,
        version=run.version,
        window=(run.window_start, run.window_end),
        score=score,
        components=components,
        trades=run.trades,
        net_pnl=run.net_pnl,
        robustness_positive_fraction=robustness_positive_fraction,
        cost_positive_fraction=cost_positive_fraction,
        flags=tuple(flags),
        reasons=reasons,
    )


def rank(scorecards: list[Scorecard]) -> list[Scorecard]:
    """Stable ranking: score desc, then net P&L desc, then trade count desc."""
    return sorted(
        scorecards,
        key=lambda c: (c.score, float(c.net_pnl), c.trades),
        reverse=True,
    )


def promotion_verdict(
    challenger_run: DiscoveryRun,
    control_run: DiscoveryRun,
    *,
    challenger_name: str,
    config: WalkForwardConfig | None = None,
) -> PromotionVerdict:
    """Wire challenger-vs-control validation evidence into the production gate.

    The gate requires an out-of-sample period; discovery provides none (OOS is
    protected behind the model_0 boundary), so the verdict is REJECT with the
    blocker recorded — no promotion without an OOS proof.
    """
    beats = challenger_run.net_pnl > control_run.net_pnl
    delta = DeltaView(
        challenger_name=challenger_name,
        period="validation",
        champion_net_pnl=control_run.net_pnl,
        challenger_net_pnl=challenger_run.net_pnl,
        champion_max_drawdown_pct=Decimal(str(control_run.max_drawdown_pct)),
        challenger_max_drawdown_pct=Decimal(str(challenger_run.max_drawdown_pct)),
        beats_champion=beats,
    )
    gate = PromotionGate()
    return gate.evaluate(
        {"validation": {challenger_name: delta}},
        {"out_of_sample": 0},
        challenger_name,
    )