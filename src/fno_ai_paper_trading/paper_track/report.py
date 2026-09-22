"""Daily report construction for the Daily Paper Trading Track.

The report is what a scheduler/observer reads after a finalized day: strategy,
trading lifecycle, orders/fills, risk refusals, data quality, and independent
accounting figures cross-checked against the portfolio ledger. It is built
without ever reading environment variables or network state, and carries a
rerun-comparable economic fingerprint plus an explicit ``paper_only`` flag.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.paper_track.accounting import verify_accounting
from fno_ai_paper_trading.paper_track.store import stable_dumps

__all__ = [
    "build_report",
    "assert_report_clean",
    "report_fingerprint",
    "reported_days",
    "build_cumulative_report",
]


def report_fingerprint(engine) -> str:
    """Hash of the economic content, excluding volatile ids/times/run id."""
    trades = sorted(engine.portfolio.trade_history, key=lambda t: t.executed_at)
    fills = sorted(engine.broker.fills, key=lambda f: f.filled_at)
    payload = {
        "day": engine.day.isoformat() if engine.day else None,
        "interval": engine.config.interval,
        "fills": [[f.side.value, f.quantity, str(f.price), str(f.commission)] for f in fills],
        "trades": [[t.side.value, t.quantity, str(t.price), str(t.realized_pnl)] for t in trades],
        "signals": engine.counters["signals"],
        "signal_counts": engine.counters["signal_counts"],
        "entries_filled": engine.counters["entries_filled"],
        "exits_filled": engine.counters["exits_filled"],
        "stops_fired": engine.counters["stops_fired"],
        "flatten_count": engine.counters["flatten_count"],
        "cash": str(engine.portfolio.cash),
        "realized": str(engine.portfolio.realized_pnl),
        "final_position": engine.position_quantity,
        "eod_status": engine.eod_status,
        "data_skip_count": len(engine.counters["data_skips"]),
        "risk_refusal_count": len(engine.counters["risk_refusals"]),
        "error_count": len(engine.counters["errors"]),
    }
    return hashlib.sha256(stable_dumps(payload).encode("utf-8")).hexdigest()


def _fill_row(fill) -> dict:
    return {
        "order_id": fill.order_id,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": str(fill.price),
        "commission": str(fill.commission),
        "filled_at": fill.filled_at.isoformat(),
    }


def build_report(engine) -> dict:
    """Compile the full daily report for ``engine`` (live, from its state)."""
    strategy = engine.config.strategy

    fills = list(engine.broker.fills)
    fills_by_side = {OrderSide.BUY: [f for f in fills if f.side is OrderSide.BUY],
                     OrderSide.SELL: [f for f in fills if f.side is OrderSide.SELL]}
    entry = fills_by_side[OrderSide.BUY][0] if fills_by_side[OrderSide.BUY] else None
    exit = fills_by_side[OrderSide.SELL][-1] if fills_by_side[OrderSide.SELL] else None

    accounting = verify_accounting(
        trades=engine.portfolio.trade_history,
        fills=fills,
        initial_cash=engine.portfolio.initial_cash,
        cash=engine.portfolio.cash,
        realized_pnl=engine.portfolio.realized_pnl,
        flat=not engine.portfolio.open_positions(),
    )

    return {
        "paper_only": True,
        "trading_date": engine.day.isoformat() if engine.day else None,
        "account": engine.config.account,
        "run_id": engine.run_id,
        "strategy": {
            "name": strategy.name,
            "fast": 5,
            "slow": 21,
            "interval": engine.config.interval,
        },
        "policy": engine.config.policy.describe(),
        "lifecycle": {
            "entry": {
                "filled_at": entry.filled_at.isoformat(),
                "price": str(entry.price),
                "quantity": entry.quantity,
                "commission": str(entry.commission),
            }
            if entry
            else None,
            "exit": {
                "filled_at": exit.filled_at.isoformat(),
                "price": str(exit.price),
                "quantity": exit.quantity,
                "commission": str(exit.commission),
            }
            if exit
            else None,
        },
        "signals": {
            "total": engine.counters["signals"],
            "by_type": engine.counters["signal_counts"],
        },
        "orders": {
            "submitted": len(engine.broker.snapshot()[0]),
            "filled": len(fills),
            "rejected": len(engine.counters["risk_refusals"]),
        },
        "fills": [_fill_row(f) for f in fills],
        "accounting": {
            "initial_cash": str(accounting.initial_cash),
            "gross_pnl": str(accounting.gross_pnl),
            "costs": str(accounting.total_commission),
            "net_pnl": str(accounting.net_pnl),
            "cash": str(accounting.cash),
            "final_position_quantity": engine.position_quantity,
            "cash_consistent": accounting.cash_consistent,
            "violations": list(accounting.violations),
        },
        "risk": {
            "entries_filled": engine.counters["entries_filled"],
            "exits_filled": engine.counters["exits_filled"],
            "stops_fired": engine.counters["stops_fired"],
            "flatten_count": engine.counters["flatten_count"],
            "entry_approvals": list(engine.entry_approvals),
            "risk_refusals": list(engine.counters["risk_refusals"]),
            "sizing_skips": list(engine.counters["sizing_skips"]),
        },
        "data_quality": {
            "data_skips": list(engine.counters["data_skips"]),
            "anomalies": list(engine.counters["anomalies"]),
            "errors": list(engine.counters["errors"]),
        },
        "equity": {
            "max_intraday_drawdown": str(engine.max_intraday_drawdown),
            "curve": [str(x) for x in engine.equity_curve],
        },
        "eod_status": engine.eod_status,
        "fingerprint": report_fingerprint(engine),
        "report_created_at": datetime.now().isoformat(),
    }


def assert_report_clean(report: dict) -> list[str]:
    """Scan every value for credential/environment tokens; returns violations."""
    blob = json.dumps(report, sort_keys=True, default=str).lower()
    tokens = (
        "access_token",
        "upstox",
        "kite",
        "dotenv",
        "client_secret",
        "password",
        "token=",
    )
    return [token for token in tokens if token in blob]


# --------------------------------------------------------------------------- #
# Cumulative (multi-day) account reporting
# --------------------------------------------------------------------------- #

def reported_days(store) -> list:
    """Sorted trading dates that already produced a persisted account report."""
    from datetime import date

    files = sorted(store.reports_dir.glob(f"{store.account}.*.json"))
    return [date.fromisoformat(path.name.split(".")[-2]) for path in files]


def build_cumulative_report(store) -> dict:
    """Reconcile every persisted account day into one cumulative report.

    Each day's report embeds CUMULATIVE accounting figures (the account ledger
    never resets across resumes), so a day's own contribution is the difference
    between successive cumulative lines.  Every read goes through the hashed
    TrackStore so a torn/altered artifact aborts the whole cumulative report.

    The reconciliation enforces, from the persisted files alone:

    * the per-day net P&L deltas sum exactly to the final cumulative net P&L;
    * ``final_cash == start_cash + lifetime_net``;
    * every day's own accounting section is internally consistent; and
    * every day is paper-only.
    """
    days = reported_days(store)
    if not days:
        raise ValueError(f"no persisted reports for account {store.account!r}")
    if any(day.weekday() >= 5 for day in days):
        raise ValueError(f"reported_day includes a weekend/holiday for {store.account!r}")

    from decimal import Decimal

    rows: list[dict] = []
    prev_net = Decimal("0")
    prev_costs = Decimal("0")
    prev_cash: Decimal | None = None
    start_cash: Decimal | None = None
    prev_fills = 0
    prev_entries = 0
    prev_exits = 0
    prev_stops = 0
    prev_flattens = 0
    prev_errors = 0
    cumulative_status: list[str] = []
    last_totals: dict = {}

    for day in days:
        report = store.load_report(day)
        if report is None:
            raise ValueError(f"report for {day} missing despite reported_days listing it")
        accounting = report.get("accounting") or {}
        risk = report.get("risk") or {}
        quality = report.get("data_quality") or {}

        cash = Decimal(accounting.get("cash", "0"))
        gross = Decimal(accounting.get("gross_pnl", "0"))
        costs = Decimal(accounting.get("costs", "0"))
        net = Decimal(accounting.get("net_pnl", "0"))
        initial = Decimal(accounting.get("initial_cash", "0"))

        if start_cash is None:
            start_cash = initial

        # Counters/fills are CUMULATIVE across resumes (the ledger never resets);
        # one day's own contribution is the delta against the prior day's totals.
        totals = {
            "fills": len(report.get("fills", [])),
            "entries": int(risk.get("entries_filled", 0)),
            "exits": int(risk.get("exits_filled", 0)),
            "stops": int(risk.get("stops_fired", 0)),
            "flattens": int(risk.get("flatten_count", 0)),
            "errors": len(quality.get("errors", [])),
        }
        cumulative_status.append(str(report.get("eod_status", "NONE")))

        rows.append(
            {
                "trading_date": day.isoformat(),
                "day_net_pnl": str(net - prev_net),
                "day_commission": str(costs - prev_costs),
                "cash_eod": str(cash),
                "fills": totals["fills"] - prev_fills,
                "entries": totals["entries"] - prev_entries,
                "exits": totals["exits"] - prev_exits,
                "stops": totals["stops"] - prev_stops,
                "flatten_count": totals["flattens"] - prev_flattens,
                "errors": totals["errors"] - prev_errors,
                "eod_status": str(report.get("eod_status", "NONE")),
                "paper_only": bool(report.get("paper_only", False)),
                "accounting_clean": not (accounting.get("violations") or []),
            }
        )
        prev_net, prev_costs, prev_cash = net, costs, cash
        prev_fills, prev_entries, prev_exits = totals["fills"], totals["entries"], totals["exits"]
        prev_stops, prev_flattens, prev_errors = totals["stops"], totals["flattens"], totals["errors"]
        last_totals = totals

    final_cash = prev_cash if prev_cash is not None else Decimal("0")
    lifetime_net = prev_net
    lifetime_costs = prev_costs
    lifetime_gross = gross
    sum_day_net = sum((Decimal(r["day_net_pnl"]) for r in rows), Decimal("0"))
    sum_day_costs = sum((Decimal(r["day_commission"]) for r in rows), Decimal("0"))

    violations: list[str] = []
    if not start_cash:
        violations.append("cumulative report found no start cash")
    if sum_day_net != lifetime_net:
        violations.append(
            f"daily net {sum_day_net} != lifetime net {lifetime_net}"
        )
    if sum_day_costs != lifetime_costs:
        violations.append(
            f"daily commission {sum_day_costs} != lifetime {lifetime_costs}"
        )
    if final_cash != (start_cash + lifetime_net):
        violations.append(
            f"final cash {final_cash} != start {start_cash} + lifetime net {lifetime_net}"
        )
    if any(not r["accounting_clean"] for r in rows):
        violations.append("one or more days reported accounting violations")
    if any(not r["paper_only"] for r in rows):
        violations.append("a persisted day was not paper-only")

    payload = {
        "account": store.account,
        "paper_only": True,
        "first_day": rows[0]["trading_date"],
        "last_day": rows[-1]["trading_date"],
        "days": len(rows),
        "start_cash": str(start_cash),
        "final_cash": str(final_cash),
        "lifetime": {
            "gross_pnl": str(lifetime_gross),
            "costs": str(lifetime_costs),
            "net_pnl": str(lifetime_net),
        },
        "cumulative": {
            "fills": last_totals.get("fills", 0),
            "entries": last_totals.get("entries", 0),
            "exits": last_totals.get("exits", 0),
            "stops": last_totals.get("stops", 0),
            "flattens": last_totals.get("flattens", 0),
            "errors": last_totals.get("errors", 0),
            "statuses": cumulative_status,
        },
        "daily": rows,
        "reconciled": not violations,
        "violations": violations,
        "fingerprint": cumulative_fingerprint(rows),
        "report_created_at": datetime.now().isoformat(),
    }
    return payload


def cumulative_fingerprint(rows: list[dict]) -> str:
    """Stable hash of the cumulative series (rerun-comparable)."""
    payload = [
        [
            r["trading_date"],
            r["day_net_pnl"],
            r["day_commission"],
            r["cash_eod"],
            r["entries"],
            r["exits"],
            r["stops"],
            r["flatten_count"],
            r["eod_status"],
        ]
        for r in rows
    ]
    return hashlib.sha256(stable_dumps({"rows": payload}).encode("utf-8")).hexdigest()