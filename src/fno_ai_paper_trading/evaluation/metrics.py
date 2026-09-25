"""Canonical research-reporting metrics (Iteration-003 reporting standard).

Single source of truth for the reporting taxonomy fixed by the research
quality audit (``runs/research/day_batch/RESEARCH_QUALITY_AUDIT.md`` §10).

Terminology (unambiguous, exactly one meaning):

* FILL        — one executed entry OR exit fill (portfolio ``trade_history``).
* ROUND TRIP  — a completed entry + exit lifecycle (engine ``num_trades``).
* WINS/LOSSES — closed round trips by sign of ``Trade.realized_pnl``.
* REALIZED P&L— sum of ``Trade.realized_pnl`` over closing fills; price-based
                (adverse slippage is already embedded in the fill prices).
* SLIPPAGE    — reporting-only decomposition of the adverse price gap paid;
                already inside realized P&L; must never be added again on top.
* COMMISSIONS — sum of all fill commissions.
* CARRY MTM   — mark-to-market of any position still open at the final bar
                (the "final carry" residual that previously caused the two
                reported "net" figures to differ by ~₹29.70).
* NET         — the canonical net economic result == ``BacktestResult.total_pnl``
                == realized P&L + carry MTM − total commissions.

This resolves the Iteration-002/domain-diagnostic discrepancy (net excluding
final carry MTM) by making ``total_pnl`` the canonical definition and labeling
the legacy figure "realized P&L − commissions" explicitly wherever it appears.
Historical economic values are never rewritten; only the labels and the
definition become uniform.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fno_ai_paper_trading.backtest.result import BacktestResult

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CanonicalSummary:
    """Canonical economic summary of one backtest replay."""

    fills: int                # executed entry or exit fills
    round_trips: int          # completed entry + exit lifecycles
    wins: int                 # wins / losses over closed round trips
    losses: int
    win_rate_pct: Decimal
    realized_pnl: Decimal     # Σ closing-fill realized P&L (incl. slippage in prices)
    commissions: Decimal      # Σ all fill commissions
    slippage: Decimal         # reporting-only: adverse price gap paid (inside realized)
    carry_mtm: Decimal        # final open position mark (domain-end)
    net: Decimal              # canonical net economic result == BacktestResult.total_pnl
    gross_profit: Decimal     # raw price-based (incl. slippage, excl. commission)
    gross_loss: Decimal
    max_drawdown: Decimal
    avg_round_trip: Decimal   # mean realized P&L per closed round trip
    median_round_trip: Decimal

    def as_dict(self) -> dict:
        fields = (
            "fills", "round_trips", "wins", "losses",
            "win_rate_pct", "realized_pnl", "commissions", "slippage",
            "carry_mtm", "net", "gross_profit", "gross_loss",
            "max_drawdown", "avg_round_trip", "median_round_trip",
        )
        return {name: str(getattr(self, name)) for name in fields}


def canonical(result: BacktestResult) -> CanonicalSummary:
    """Compute the canonical summary from an engine backtest result."""
    rt_pnls = [
        t.realized_pnl
        for t in result.trades
        if t.realized_pnl != _ZERO
    ]
    realized = sum(rt_pnls, _ZERO)
    net = result.total_pnl
    carry = net - realized + result.total_commission
    n_rt = len(rt_pnls)
    avg = (realized / Decimal(n_rt)) if n_rt else _ZERO
    ordered = sorted(rt_pnls)
    mid = n_rt // 2
    median = ordered[mid] if n_rt else _ZERO
    if n_rt and n_rt % 2 == 0:
        median = (ordered[mid - 1] + ordered[mid]) / Decimal("2")
    return CanonicalSummary(
        fills=len(result.trades),
        round_trips=result.num_trades,
        wins=result.winning_trades,
        losses=result.losing_trades,
        win_rate_pct=result.win_rate,
        realized_pnl=realized,
        commissions=result.total_commission,
        slippage=result.slippage_cost,
        carry_mtm=carry,
        net=net,
        gross_profit=result.gross_profit,
        gross_loss=result.gross_loss,
        max_drawdown=result.max_drawdown,
        avg_round_trip=avg,
        median_round_trip=median,
    )