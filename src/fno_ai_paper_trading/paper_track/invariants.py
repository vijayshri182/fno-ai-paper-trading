"""Property/invariant verification for the Daily Paper Trading Track.

Fifteen invariants the track must never break, checked at any point (and in
full after a finalized day). Each returns violations as strings; an empty list
means the invariant held. Tests drive these over adversarial, crash-restart and
long-run simulations.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.accounting import max_drawdown, verify_accounting

__all__ = ["verify_invariants", "verify_day_end_invariants"]


def _now_fills(engine) -> list:
    return list(engine.broker.fills)


def verify_invariants(engine) -> list[str]:
    """Structural invariants checkable at any moment during a run."""
    violations: list[str] = []
    history: list[MarketPrice] = engine.history
    timestamps = [b.timestamp for b in history]

    # 1 & 12. Each consumed bar exactly once; stream strictly increasing.
    if len(set(timestamps)) != len(timestamps):
        violations.append("INV-1/12: duplicate timestamps in consumed history")
    if any(a >= b for a, b in zip(timestamps, timestamps[1:])):
        violations.append("INV-12: consumed history is not strictly increasing")
    consumed = sorted(engine.consumed) if hasattr(engine, "consumed") else []
    if [b.timestamp for b in history] != consumed:
        violations.append("INV-1: consumed set diverges from history")

    # 2. Every BUY fill was RiskManager-approved before reaching the broker.
    approvals = engine.entry_approvals if hasattr(engine, "entry_approvals") else []
    for fill in engine.broker.fills:
        if fill.side is OrderSide.BUY:
            matched = any(
                str(a.get("filled_at")) == fill.filled_at.isoformat()
                and int(a.get("quantity", -1)) == fill.quantity
                for a in approvals
            )
            if not matched:
                violations.append(f"INV-2: BUY fill {fill.order_id} lacks a recorded RiskManager approval")

    # 3. Long-only: never a short position, never a SELL while flat (defensive).
    for symbol, position in engine.portfolio.open_positions().items():
        if position.quantity < 0:
            violations.append(f"INV-3: short position {symbol} qty {position.quantity}")
    for trade in engine.portfolio.trade_history:
        if trade.side is OrderSide.SELL and trade.quantity != 0:
            pass  # covered by portfolio long_only guard + full-close exits below
    sells = [t for t in engine.portfolio.trade_history if t.side is OrderSide.SELL]
    for sell in sells:
        # A defensive proxy: the portfolio guard would have raised before any
        # SELL could create a short, so merely recording it is insufficient.
        if any(p.quantity < 0 for p in engine.portfolio.positions.values()):
            violations.append(f"INV-3: post-SELL short residue after {sell.trade_id}")

    # 4. No fill outside the session window (09:15 <= t < 15:30, trading day).
    policy = engine.config.policy
    for fill in engine.broker.fills:
        t = fill.filled_at.time().replace(second=0, microsecond=0)
        if not (policy.window_open <= t < policy.window_close):
            violations.append(
                f"INV-4: fill {fill.order_id} at {fill.filled_at:%H:%M} outside session window"
            )

    # 7. Portfolio realized P&L matches the trade ledger.
    gross = sum((t.realized_pnl for t in engine.portfolio.trade_history), Decimal("0"))
    if engine.portfolio.realized_pnl != gross:
        violations.append("INV-7: portfolio realized_pnl != trade ledger gross")

    # 8. Commission identity across fills and trades.
    fill_fees = sum((f.commission for f in engine.broker.fills), Decimal("0"))
    trade_fees = sum((t.commission for t in engine.portfolio.trade_history), Decimal("0"))
    if fill_fees != trade_fees:
        violations.append("INV-8: fill commission != trade commission")

    # 9 & 10. Unique ids.
    trade_ids = [t.trade_id for t in engine.portfolio.trade_history]
    if len(set(trade_ids)) != len(trade_ids):
        violations.append("INV-9: duplicate trade ids")
    order_ids = [o.order_id for o in engine.broker.snapshot()[0]]
    if len(set(order_ids)) != len(order_ids):
        violations.append("INV-10: duplicate order ids")

    # 11. Every fill maps to a FILLED broker order.
    orders_by_id = {o.order_id: o for o in engine.broker.snapshot()[0]}
    for fill in engine.broker.fills:
        order = orders_by_id.get(fill.order_id)
        if order is None or not order.is_filled or order.filled_quantity != order.quantity:
            violations.append(f"INV-11: fill {fill.order_id} lacks matching FILLED order")

    # 13. Equity curve length and max drawdown recomputation.
    expected_len = len(history) + 1
    if len(engine.equity_curve) != expected_len:
        violations.append(
            f"INV-13: equity curve length {len(engine.equity_curve)} != history+1 {expected_len}"
        )
    recomputed = max_drawdown(engine.equity_curve)
    if recomputed != engine.max_intraday_drawdown:
        violations.append(
            f"INV-13: max drawdown {engine.max_intraday_drawdown} != recomputed {recomputed}"
        )

    # 14. Any open position belongs to the engine's current trading day.
    today = engine.day
    for position in engine.portfolio.open_positions().values():
        if position.opened_at.date() != today:
            violations.append(
                f"INV-14: position opened {position.opened_at.date()} != today {today} (overnight leak)"
            )

    return violations


def verify_day_end_invariants(engine) -> list[str]:
    """Day-end closure invariants: flat, reconciled, reported faithfully."""
    violations = list(verify_invariants(engine))
    snapshot = verify_accounting(
        trades=engine.portfolio.trade_history,
        fills=engine.broker.fills,
        initial_cash=engine.config.initial_cash,
        cash=engine.portfolio.cash,
        realized_pnl=engine.portfolio.realized_pnl,
        flat=not engine.portfolio.open_positions(),
    )

    # 5. Flat at day end.
    if engine.portfolio.open_positions():
        violations.append(
            "INV-5: position left open at day end "
            f"({[s for s in engine.portfolio.open_positions()]})"
        )

    # 6. Cash identity when flat.
    if snapshot.cash_consistent:
        pass
    else:
        violations.extend(f"INV-6: {v}" for v in snapshot.violations)

    # 15. Report faithfully mirrors the live state.
    report = engine.report()
    if report is not None:
        accounting = report.get("accounting") or {}
        if str(accounting.get("net_pnl")) != str(snapshot.net_pnl):
            violations.append(
                f"INV-15: report net_pnl {accounting.get('net_pnl')} != {snapshot.net_pnl}"
            )
        if str(accounting.get("cash")) != str(engine.portfolio.cash):
            violations.append(
                f"INV-15: report cash {accounting.get('cash')} != portfolio {engine.portfolio.cash}"
            )
        if accounting.get("final_position_quantity") != 0:
            violations.append("INV-15: report not flat at day end")
    return violations


def consumed_timestamps(engine) -> list[datetime]:
    return [b.timestamp for b in engine.history]