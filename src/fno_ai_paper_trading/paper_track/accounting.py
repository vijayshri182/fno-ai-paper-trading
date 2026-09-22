"""Independent P&L and cash accounting for the Daily Paper Trading Track.

The engine's portfolio is the source of truth for trading; this module derives
the same numbers *independently* from the raw broker fills and the recorded
trades, and cross-checks them against the portfolio. The identity enforced at a
flat day end is::

    cash = initial_cash + gross_realized_pnl - total_commission

where ``gross_realized_pnl`` comes from trade realized P&L (slippage is already
inside the fill prices) and ``total_commission`` is the sum over every fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.models.position import Trade

__all__ = [
    "gross_pnl",
    "total_commission",
    "net_pnl",
    "AccountingSnapshot",
    "verify_accounting",
]


def gross_pnl(trades: list[Trade] | tuple[Trade, ...]) -> Decimal:
    """Sum of realized P&L recorded on the trade ledger."""
    return sum((t.realized_pnl for t in trades), Decimal("0"))


def total_commission(fills: list[Fill] | tuple[Fill, ...]) -> Decimal:
    return sum((f.commission for f in fills), Decimal("0"))


def net_pnl(trades, fills) -> Decimal:
    return gross_pnl(trades) - total_commission(fills)


def max_drawdown(curve: list[Decimal] | tuple[Decimal, ...]) -> Decimal:
    """Peak-to-trough equity drawdown (absolute rupees) over ``curve``.

    Empty or single-point curves have zero drawdown. This single helper is the
    one definition shared by the engine's live tracking and the invariant
    verifier's recomputation.
    """
    if not curve:
        return Decimal("0")
    peak = curve[0]
    worst = Decimal("0")
    for value in curve[1:]:
        if value > peak:
            peak = value
        drop = peak - value
        if drop > worst:
            worst = drop
    return worst


@dataclass(frozen=True)
class AccountingSnapshot:
    """Verified accounting numbers for one run/day."""

    trades: tuple[Trade, ...] = field(default_factory=tuple)
    fills: tuple[Fill, ...] = field(default_factory=tuple)
    gross_pnl: Decimal = Decimal("0")
    total_commission: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")  # portfolio.realized_pnl
    initial_cash: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    flat: bool = True
    cash_consistent: bool = True
    cash_delta: Decimal = Decimal("0")
    violations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def consistent(self) -> bool:
        return not self.violations


def verify_accounting(
    *,
    trades: list[Trade] | tuple[Trade, ...],
    fills: list[Fill] | tuple[Fill, ...],
    initial_cash: Decimal,
    cash: Decimal,
    realized_pnl: Decimal,
    flat: bool = True,
) -> AccountingSnapshot:
    """Cross-check portfolio-derived figures against the raw broker records."""
    violations: list[str] = []

    gross = gross_pnl(trades)
    comm = total_commission(fills)
    net = gross - comm

    if realized_pnl != gross:
        violations.append(
            f"portfolio realized_pnl {realized_pnl} != trade gross {gross}"
        )

    trade_fees = sum((t.commission for t in trades), Decimal("0"))
    if trade_fees != comm:
        violations.append(f"trade fees {trade_fees} != fill fees {comm}")

    cash_delta = cash - initial_cash
    if flat:
        expected = gross - comm
        if cash_delta != expected:
            violations.append(
                f"flat-day cash {cash} = initial {initial_cash} + delta {cash_delta}; "
                f"expected delta {expected} (gross {gross} - fees {comm})"
            )

    return AccountingSnapshot(
        trades=tuple(trades),
        fills=tuple(fills),
        gross_pnl=gross,
        total_commission=comm,
        net_pnl=net,
        realized_pnl=realized_pnl,
        initial_cash=initial_cash,
        cash=cash,
        flat=flat,
        cash_consistent=not violations,
        cash_delta=cash_delta,
        violations=tuple(violations),
    )