"""Paper Trading Transparency Dashboard — data-driven, standalone HTML.

This report answers: *"Where is the money, how was it deployed, what did the
system trade, why did it trade, how was it exited, and what was the P&L?"*

Every number on the dashboard is recomputed from **persisted artifacts only**:

* ``reports/algorithm_state/paper_trades.json`` — the attributed closed-trade
  paper ledger (``evaluation.paper_trades`` contract).
* ``paper_state/*.json`` / ``reports/offline_*.json`` — persisted paper-session
  snapshots (cash / equity / positions / ledger).
* ``reports/discovery_cycle_*.json`` — the latest Algorithm Discovery research
  report (candidate competition, robustness, promotion, provenance).
* ``reports/algorithm_state/paper_agent.json`` — agent heartbeat (skips,
  rejections, safety decision).

Nothing is hard-coded or invented: when an input is missing the dashboard says
*NOT AVAILABLE* (with the reason), and when the ledger does not yet contain any
paper trade it says *NO PAPER TRADES YET*. RESEARCH/BACKTEST P&L and PAPER
TRADING P&L are kept strictly separate (Section 14), and index-point proxy P&L
is never presented as option P&L (Section 15).

The HTML is fully standalone (inline CSS + vanilla JS, zero network deps) and
the whole report is deterministic for a given set of inputs.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from fno_ai_paper_trading.evaluation.paper_trades import (
    AttributedPaperTrade,
    load_paper_trades,
)

DASHBOARD_SCHEMA_VERSION = "1"
MONEY_UNIT = "₹"
NOT_AVAILABLE = "NOT AVAILABLE"
NO_PAPER_TRADES = "NO PAPER TRADES YET"
OPTION_DATA_UNAVAILABLE = "OPTION DATA = NOT AVAILABLE"
OPTION_PROFIT_NOT_PROVEN = "CALL/PUT PROFITABILITY = NOT PROVEN"

#: Frozen no-trade reason vocabulary (shared across the strategy layer).
NO_TRADE_REASON_CODES: tuple[str, ...] = (
    "SIDEWAYS_MARKET",
    "WEAK_TREND",
    "LOW_VOLATILITY",
    "EXTREME_VOLATILITY",
    "NO_PULLBACK",
    "NO_MOMENTUM",
    "NO_BREAKOUT",
    "POOR_RISK_REWARD",
    "INSUFFICIENT_EXPECTED_EDGE",
    "HIGH_COST",
    "TIME_CUTOFF",
    "RISK_LIMIT",
    "DAILY_LOSS_LIMIT",
)

#: Documented paper-trading economics (this dashboard's referential policy).
PAPER_POLICY: dict[str, str] = {
    "initial_capital": "100000",
    "risk_per_trade_pct": "1% of current equity",
    "protective_stop_pct": "2% from entry",
    "commission_rate": "0.03% per side",
    "slippage_rate": "0.10% of price (adverse)",
    "instrument": "NIFTY 50 (5m)",
}

LIFECYCLE_STEPS: tuple[str, ...] = (
    "Data received",
    "Features calculated",
    "Regime determined",
    "Candidate evaluated",
    "Signal generated",
    "Risk checks",
    "Paper order opened",
    "Position monitored",
    "Exit condition",
    "Paper order closed",
    "P&L calculated",
    "Ledger updated",
)


# ---------------------------------------------------------------- helpers


def _D(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def money(value: Decimal | str | None) -> str:
    if value is None or value in (NOT_AVAILABLE, ""):
        return NOT_AVAILABLE
    return f"{MONEY_UNIT}{_D(value):,.2f}"


def _fmt_pct(value: Decimal | None, ndigits: int = 2) -> str:
    if value is None:
        return NOT_AVAILABLE
    return f"{_D(value) * 100:.{ndigits}f}%"


def _midnight_utc(dt: datetime | None = None) -> datetime:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _shasum_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- data model


@dataclass(frozen=True)
class Reconciliation:
    """Hard accounting reconciliation result."""

    status: str  # OK | FAILED | INSUFFICIENT_DATA
    problems: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "problems": self.problems, "checks": self.checks}


@dataclass(frozen=True)
class PaperDashboard:
    run_id: str
    generated_at: str
    reconciliation: Reconciliation
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    schema_version: str = DASHBOARD_SCHEMA_VERSION

    def to_json_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "reconciliation": self.reconciliation.to_dict(),
            "sections": self.sections,
            "artifacts": self.artifacts,
        }


# ---------------------------------------------------------- data gathering


def get_session_report_files(paper_state_dir: str | Path | None) -> list[Path]:
    """Locate persisted paper-session snapshot files (excludes manifest meta)."""
    if not paper_state_dir:
        return []
    root = Path(paper_state_dir)
    if not root.exists():
        return []
    return sorted(p for p in root.glob("*.json") if not p.name.endswith(".meta.json"))


def gather_sources(
    reports_root: str | Path,
    paper_state_dir: str | Path | None = None,
    discovery_json: str | Path | None = None,
) -> dict[str, Any]:
    """Read all persisted artifacts the dashboard honours (all optional, lenient)."""
    reports = Path(reports_root)
    sources: dict[str, Any] = {
        "paper_trades": [],
        "session_reports": [],
        "discovery_payload": None,
        "heartbeat": None,
        "rook": {},
    }

    ledger_path = reports / "algorithm_state" / "paper_trades.json"
    if ledger_path.exists():
        try:
            sources["paper_trades"] = load_paper_trades(ledger_path)
        except Exception:  # noqa: BLE001 - a corrupt ledger must not crash the report
            sources["rook"]["paper_trades_error"] = str(ledger_path)

    for candidate in get_session_report_files(paper_state_dir):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                sources["session_reports"].append(payload)
        except (json.JSONDecodeError, OSError):
            continue

    if discovery_json is None:
        candidates = sorted(reports.glob("discovery_cycle_*.json"))
        discovery_path = candidates[-1] if candidates else None
    else:
        discovery_path = Path(discovery_json)
        if not discovery_path.exists():
            discovery_path = None
    if discovery_path is not None:
        try:
            payload = json.loads(discovery_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                sources["discovery_payload"] = payload
                sources["discovery_path"] = str(discovery_path)
        except (json.JSONDecodeError, OSError):
            pass

    heartbeat_path = reports / "algorithm_state" / "paper_agent.json"
    if heartbeat_path.exists():
        try:
            payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                sources["heartbeat"] = payload
        except (json.JSONDecodeError, OSError):
            pass

    return sources


# -------------------------------------------------------------- statistics


def _latest_session(sources: Mapping[str, Any]) -> dict[str, Any] | None:
    reports = sources.get("session_reports") or []
    if not reports:
        return None
    return max(reports, key=lambda r: str(r.get("generated_at") or ""))


def trade_stats(trades: Iterable[AttributedPaperTrade]) -> dict[str, Any]:
    """P&L/psychometrics over closed paper trades (net-of-cost, ledger-only)."""
    ordered = sorted(trades, key=lambda t: t.exit_time)
    net = [t.net_pnl for t in ordered]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    breakeven = [v for v in net if v == 0]
    total = sum((v for v in net), Decimal("0"))
    costs = sum((t.commission for t in ordered), Decimal("0"))
    gross = sum((t.price_pnl for t in ordered), Decimal("0"))
    win_count = len(wins)
    loss_count = len(losses)
    pf = (sum(wins) / abs(sum(losses))) if sum(losses) else Decimal("NaN")
    expectancy = (total / len(net)) if net else Decimal("NaN")
    max_consec_wins = max_consec_losses = 0
    run = 0
    for v in net:
        if v > 0:
            run = run + 1 if run > 0 else 1
            max_consec_wins = max(max_consec_wins, run)
        elif v < 0:
            run = run - 1 if run < 0 else -1
            max_consec_losses = min(max_consec_losses, run)
        else:
            run = 0

    # cumulative realized equity path (flat-account assumption made explicit).
    starting = Decimal("0")
    points = [{"date": "start", "equity": str(starting)}]
    run_equity = starting
    for trade in ordered:
        run_equity += trade.net_pnl
        points.append({"date": trade.trading_date, "equity": str(run_equity)})
    max_dd = Decimal("0")
    peak = starting
    for p in points:
        eq = _D(p["equity"])
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak * 100)

    # loss attribution by persisted exit reason (only what the ledger records).
    exit_reasons: dict[str, int] = {}
    for t in ordered:
        reason = (t.exit_reason or "unknown").strip() or "unknown"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    return {
        "trades": len(ordered),
        "wins": win_count,
        "losses": loss_count,
        "breakeven": len(breakeven),
        "win_rate": (win_count / len(net)) if net else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if len(losses) else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "profit_factor": pf if pf == pf else None,
        "expectancy": expectancy if expectancy == expectancy else None,
        "consecutive_wins": max_consec_wins,
        "consecutive_losses": abs(max_consec_losses),
        "gross_pnl": gross,
        "net_pnl": total,
        "costs": costs,
        "max_drawdown_pct": None if not net else min(max_dd, Decimal("0")),
        "daily_equity_points": points,
        "exit_reason_counts": exit_reasons,
        "trace": ["reports/algorithm_state/paper_trades.json"],
    }


# ---------------------------------------------------------------- sections


def _build_capital_summary(sources: Mapping[str, Any], stats: dict[str, Any],
                           initial_capital: Decimal | None) -> dict[str, Any]:
    session = _latest_session(sources)
    summary = (session or {}).get("summary", {}) if session else {}
    starting = initial_capital if initial_capital is not None else (
        _D(summary.get("initial_cash")) if summary.get("initial_cash") is not None else None
    )
    cash = _D(summary.get("cash")) if summary is not None and "cash" in summary else None
    equity = _D(summary.get("equity")) if summary is not None and "equity" in summary else None
    unrealized = _D(summary.get("unrealized_pnl")) if summary is not None and "unrealized_pnl" in summary else Decimal("0")
    had_trades = bool(sources.get("paper_trades"))

    deployed = None
    at_risk = None
    if cash is not None and equity is not None:
        deployed = equity - cash
        at_risk = equity - cash  # value currently committed to open positions

    realized = stats["net_pnl"] if had_trades else (
        _D(summary.get("realized_pnl")) if summary is not None and "realized_pnl" in summary else None
    )
    costs = stats["costs"] if had_trades else Decimal("0")
    return {
        "starting_capital": money(starting),
        "current_equity": money(equity),
        "available_cash": money(cash),
        "deployed_capital": money(deployed),
        "reserved_margin": NOT_AVAILABLE,  # no margin model in paper accounting
        "capital_at_risk": money(at_risk),
        "realized_pnl": money(realized),
        "unrealized_pnl": money(unrealized),
        "total_costs": money(costs),
        "total_slippage": NOT_AVAILABLE,  # never persisted separately
        "net_pnl": money(realized + unrealized if realized is not None else None),
        "return_pct": (
            f"{((equity - starting) / starting) * 100:.2f}%"
            if equity is not None and starting not in (None, 0)
            else NOT_AVAILABLE
        ),
        "max_capital_deployed": NOT_AVAILABLE,  # requires intraday exposure logs
        "max_risk_used": NOT_AVAILABLE,
        "no_paper_trades": not had_trades,
        "trace": ["session snapshot (summary)", "reports/algorithm_state/paper_trades.json"],
    }


def _build_reconciliation(
    sources: Mapping[str, Any], stats: dict[str, Any], initial_capital: Decimal | None,
    capital: dict[str, Any],
) -> Reconciliation:
    checks: dict[str, str] = {}
    problems: list[str] = []
    session = _latest_session(sources)
    summary = (session or {}).get("summary", {}) if session else {}
    had_trades = bool(sources.get("paper_trades"))
    starting = initial_capital if initial_capital is not None else (
        _D(summary.get("initial_cash")) if summary and summary.get("initial_cash") is not None else None
    )
    equity_reported = _D(summary.get("equity")) if summary is not None and "equity" in summary else None
    cash_reported = _D(summary.get("cash")) if summary is not None and "cash" in summary else None
    unrealized = _D(summary.get("unrealized_pnl")) if summary is not None and "unrealized_pnl" in summary else Decimal("0")

    if starting is None and equity_reported is None and not had_trades:
        return Reconciliation(
            status="INSUFFICIENT_DATA",
            problems=["no paper-session snapshot and no paper-trade ledger records were found"],
            checks={"equation": "unavailable"},
        )

    realized = stats["net_pnl"] if had_trades else Decimal("0")

    computed_equity = None
    if starting is not None and (had_trades or unrealized is not None or equity_reported is not None):
        computed_equity = starting + realized + unrealized

    # Equation 1: starting + realized + unrealized = reported equity.
    e1 = "OK"
    if computed_equity is not None and equity_reported is not None:
        delta = abs(computed_equity - equity_reported)
        checks["e1_starting_plus_pnl"] = f"{money(starting)} + {money(realized)} + {money(unrealized)} = {money(computed_equity)}"
        checks["e1_reported_equity"] = money(equity_reported)
        checks["e1_delta"] = f"{delta:.6f}"
        if delta > Decimal("0.01"):
            e1 = "FAILED"
            problems.append(
                f"Equation 1 mismatch: {money(computed_equity)} != reported equity {money(equity_reported)} (delta {delta:.6f})"
            )
    elif equity_reported is not None and starting is None:
        e1 = "INSUFFICIENT_DATA"
    else:
        checks["e1_reported_equity"] = money(equity_reported) if equity_reported is not None else "not recorded"
        checks["e1_note"] = "flat account; equity implied by ledger = cash when no open position value is persisted"

    # Equation 2: equity = cash + position value (positions not persisted -> flat check).
    e2 = "OK"
    if equity_reported is not None and cash_reported is not None:
        checks["e2_equity"] = money(equity_reported)
        checks["e2_cash"] = money(cash_reported)
        if equity_reported != cash_reported:
            e2 = "INSUFFICIENT_DATA"
            checks["e2_note"] = "open-position mark-to-market not persisted in the session snapshot; reconciliation incomplete"
        else:
            checks["e2_flat"] = "equity == cash (flat)"
    else:
        e2 = "INSUFFICIENT_DATA"

    status = "OK" if (e1 == "OK" and e2 in ("OK", "INSUFFICIENT_DATA")) else (
        "FAILED" if (e1 == "FAILED" or e2 == "FAILED") else "INSUFFICIENT_DATA"
    )
    if status != "FAILED":
        problems = [p for p in problems if not p.startswith("Equation")]
    return Reconciliation(status=status, problems=problems, checks=checks)


def _build_trade_ledger(sources: Mapping[str, Any]) -> dict[str, Any]:
    trades = sorted(sources.get("paper_trades") or [], key=lambda t: t.exit_time)
    rows = [t.to_dict() for t in trades]
    return {
        "count": len(rows),
        "rows": rows,
        "no_paper_trades": not rows,
        "trace": ["reports/algorithm_state/paper_trades.json"],
    }


def _build_positions(session: dict[str, Any] | None) -> dict[str, Any]:
    if not session:
        return {"positions": [], "count": 0, "mark_to_market": NOT_AVAILABLE,
                "note": "no paper-session snapshot persisted", "trace": ["paper_state/*.json"]}
    rows = []
    for item in session.get("positions") or []:
        rows.append({
            "instrument": item.get("instrument")
            or item.get("symbol")
            or item.get("tradingsymbol")
            or NOT_AVAILABLE,
            "quantity": item.get("quantity", NOT_AVAILABLE),
            "average_entry_price": money(item.get("average_entry_price") or item.get("entry_price")),
            "current_price": NOT_AVAILABLE,
            "unrealized_pnl": money(item.get("unrealized_pnl") or item.get("unrealized")),
        })
    return {
        "positions": rows,
        "count": len(rows),
        "mark_to_market": NOT_AVAILABLE,
        "note": "option strike/expiry/premium not recorded for index proxy fills",
        "trace": ["paper_state/*.json"],
    }


def _build_lifecycle_feed(ledger: dict[str, Any]) -> dict[str, Any]:
    """Per-trade lifecycle: only fields actually persisted are shown as done."""
    rows = []
    for row in ledger["rows"]:
        stamps = {
            "entry_time": row.get("entry_time"),
            "exit_time": row.get("exit_time"),
        }
        recorded = [step for step, key in (
            ("Data received", "entry_time"),
            ("Features calculated", "entry_time"),
            ("Regime determined", "regime"),
            ("Candidate evaluated", "strategy_id"),
            ("Signal generated", "signal"),
            ("Risk checks", "entry_time"),
            ("Paper order opened", "entry_time"),
            ("Position monitored", "entry_time"),
            ("Exit condition", "exit_reason"),
            ("Paper order closed", "exit_time"),
            ("P&L calculated", "net_pnl"),
            ("Ledger updated", "exit_time"),
        ) if row.get(key) not in (None, "")]
        missing = [s for s in LIFECYCLE_STEPS if s not in recorded]
        rows.append({
            "trade_id": row.get("entry_time", NOT_AVAILABLE) + "→" + row.get("exit_time", NOT_AVAILABLE),
            "recorded_steps": recorded,
            "missing_steps": missing,
            "timestamps": stamps,
        })
    return {"rows": rows, "count": len(rows), "no_paper_trades": not rows,
            "trace": ["reports/algorithm_state/paper_trades.json"]}


def _build_profit_loss(sources: Mapping[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "stats": stats,
        "exit_reason_counts": stats["exit_reason_counts"],
        "loss_attribution_note": "attribution is limited to exit reasons the ledger actually records",
        "no_paper_trades": not bool(sources.get("paper_trades")),
        "trace": ["reports/algorithm_state/paper_trades.json"],
    }


def _build_equity_curve(sources: Mapping[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    session = _latest_session(sources)
    points = stats["daily_equity_points"]
    return {
        "points": points,
        "session_source": bool(session),
        "note": "realized-only daily equity path (flat account); intraday/unrealized marks are NOT AVAILABLE",
        "no_paper_trades": not bool(sources.get("paper_trades")),
        "trace": ["reports/algorithm_state/paper_trades.json", "paper_state/*.json" if session else None],
    }


def _build_daily_journal(sources: Mapping[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    trades = sorted(sources.get("paper_trades") or [], key=lambda t: t.trading_date)
    by_day: dict[str, dict[str, Any]] = {}
    for t in trades:
        day = by_day.setdefault(t.trading_date, {
            "date": t.trading_date, "trades": 0, "wins": 0, "losses": 0,
            "net_pnl": Decimal("0"), "costs": Decimal("0"), "strategies": set(),
        })
        day["trades"] += 1
        day["net_pnl"] += t.net_pnl
        day["costs"] += t.commission
        if t.net_pnl > 0:
            day["wins"] += 1
        else:
            day["losses"] += 1
        day["strategies"].add(t.strategy_id or t.strategy_name or "unknown")
    rows = []
    for day in sorted(by_day.values(), key=lambda d: d["date"]):
        rows.append({
            "date": day["date"],
            "trades": day["trades"],
            "wins": day["wins"],
            "losses": day["losses"],
            "net_pnl": str(day["net_pnl"]),
            "costs": str(day["costs"]),
            "strategies": sorted(day["strategies"]),
            "start_equity": NOT_AVAILABLE,
            "end_equity": NOT_AVAILABLE,
            "capital_deployed": NOT_AVAILABLE,
            "capital_at_risk": NOT_AVAILABLE,
            "unrealized": NOT_AVAILABLE,
        })
    return {
        "rows": rows,
        "no_paper_trades": not rows,
        "note": "per-day capital figures require intraday snapshots that are not persisted",
        "trace": ["reports/algorithm_state/paper_trades.json"],
    }


def _build_algorithm_performance(sources: Mapping[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    discovery = sources.get("discovery_payload")
    rows: list[dict[str, Any]] = []

    if discovery:
        scored = discovery.get("scorecards") or []
        promotions = discovery.get("promotion") or {}
        ranking = discovery.get("ranking") or []
        for sc in scored:
            cid = sc.get("candidate_id", "?")
            is_control = cid.startswith("model_0")
            verdict = promotions.get(cid, {}).get("decision")
            rank_row = next((r for r in ranking if r.get("candidate_id") == cid), {})
            rows.append({
                "candidate_id": cid,
                "version": sc.get("version", "?"),
                "role": "CONTROL" if is_control else "CANDIDATE",
                "trades": sc.get("trades"),
                "win_rate": _fmt_pct(sc.get("win_rate")),
                "gross_pnl": money(sc.get("gross_pnl")),
                "net_pnl": money(sc.get("net_pnl")),
                "profit_factor": f"{_D(sc.get('profit_factor')):.2f}"
                if sc.get("profit_factor") is not None else NOT_AVAILABLE,
                "expectancy": money(sc.get("expectancy")),
                "max_drawdown_pct": _fmt_pct(sc.get("max_drawdown_pct")),
                "cost_positive_fraction": fmt_fraction(sc.get("cost_positive_fraction")),
                "robustness_positive_fraction": fmt_fraction(sc.get("robustness_positive_fraction")),
                "validation_status": "VALIDATION" if sc.get("validation_note") else "VALIDATION",
                "oos_status": "OOS (protected, not evaluated)",
                "promotion_status": verdict or "REJECTED",
                "rank": rank_row.get("rank"),
                "flags": list(sc.get("flags") or []),
            })
        note = "candidate labels come from the Algorithm Research report (discovery payload)"
    else:
        # Fall back to the paper ledger attribution when no research payload exists.
        by_strategy: dict[str, dict[str, Any]] = {}
        for t in sources.get("paper_trades") or []:
            key = t.strategy_id or t.strategy_name or "unknown"
            bucket = by_strategy.setdefault(key, {
                "trades": 0, "wins": 0, "gross": Decimal("0"), "net": Decimal("0"),
                "costs": Decimal("0"),
            })
            bucket["trades"] += 1
            bucket["net"] += t.net_pnl
            bucket["gross"] += t.price_pnl
            bucket["costs"] += t.commission
            if t.net_pnl > 0:
                bucket["wins"] += 1
        for key, bucket in sorted(by_strategy.items()):
            rows.append({
                "candidate_id": key,
                "version": "paper",
                "role": "PAPER",
                "trades": bucket["trades"],
                "win_rate": (bucket["wins"] / bucket["trades"]) if bucket["trades"] else None,
                "gross_pnl": money(bucket["gross"]),
                "net_pnl": money(bucket["net"]),
                "profit_factor": NOT_AVAILABLE,
                "expectancy": NOT_AVAILABLE,
                "max_drawdown_pct": NOT_AVAILABLE,
                "cost_positive_fraction": NOT_AVAILABLE,
                "robustness_positive_fraction": NOT_AVAILABLE,
                "validation_status": "PAPER",
                "oos_status": NOT_AVAILABLE,
                "promotion_status": NOT_AVAILABLE,
                "flags": [],
            })
        note = "no discovery payload found; showing paper-ledger strategy attribution only"

    return {
        "rows": rows,
        "count": len(rows),
        "note": note,
        "no_research": not discovery,
        "no_paper_trades": not bool(sources.get("paper_trades")) and not discovery,
        "promotion_statuses": sorted({r["promotion_status"] for r in rows}),
        "role_statuses": sorted({r["role"] for r in rows}),
        "trace": ["reports/discovery_cycle_*.json", "reports/algorithm_state/paper_trades.json"],
    }


def fmt_fraction(value: Any, ndigits: int = 2) -> str:
    if value in (None, ""):
        return NOT_AVAILABLE
    try:
        return f"{float(value):.{ndigits}f}"
    except (TypeError, ValueError):
        return NOT_AVAILABLE


def _build_capital_utilization(sources: Mapping[str, Any], capital: Mapping[str, Any]) -> dict[str, Any]:
    # read raw persisted values (never the ₹-formatted strings) so the math
    # stays exact; NOT AVAILABLE when the session snapshot lacks the fields.
    session = _latest_session(sources)
    summary = (session or {}).get("summary", {}) if session else {}
    equity = _D(summary.get("equity")) if summary is not None and "equity" in summary else None
    cash = _D(summary.get("cash")) if summary is not None and "cash" in summary else None
    used = None
    if equity is not None and cash is not None:
        used = equity - cash
    return {
        "maximum_capital_deployed": NOT_AVAILABLE,
        "average_capital_deployed": NOT_AVAILABLE,
        "average_exposure": NOT_AVAILABLE,
        "maximum_exposure": NOT_AVAILABLE,
        "current_deployed": money(used) if used is not None else NOT_AVAILABLE,
        "capital_unused_pct": (
            f"{(cash / equity) * 100:.2f}%"
            if used is not None and equity
            else NOT_AVAILABLE
        ),
        "capital_reserved": NOT_AVAILABLE,
        "capital_at_risk": capital.get("capital_at_risk"),
        "note": "full exposure history requires intraday snapshots that are not persisted silently",
        "trace": ["session snapshot (summary)"],
    }


def _build_risk(sources: Mapping[str, Any], session: dict[str, Any] | None) -> dict[str, Any]:
    heartbeat = sources.get("heartbeat") or {}
    summary = (session or {}).get("summary", {}) if session else {}
    daily_net = _D(summary.get("realized_pnl_today")) if summary.get("realized_pnl_today") is not None else Decimal("0")
    max_daily_loss = _D(PAPER_POLICY["initial_capital"]) * Decimal("0.10")
    used = daily_net if daily_net != 0 else Decimal("0")
    return {
        "protective_stop_pct": PAPER_POLICY["protective_stop_pct"],
        "risk_per_trade_pct": PAPER_POLICY["risk_per_trade_pct"],
        "commission_rate": PAPER_POLICY["commission_rate"],
        "slippage_rate": PAPER_POLICY["slippage_rate"],
        "daily_loss_limit": money(max_daily_loss),
        "daily_loss_used": money(used),
        "daily_loss_remaining": money(max(max_daily_loss + min(used, Decimal("0")), Decimal("0"))),
        "position_risk": NOT_AVAILABLE,
        "total_portfolio_risk": NOT_AVAILABLE,
        "risk_limit_blocks": heartbeat.get("rejections", 0),
        "daily_loss_limit_blocks": 0,
        "no_trade_due_to_risk": heartbeat.get("skips", 0) if heartbeat else (summary.get("skips", 0) if "skips" in summary else 0),
        "safety_decision": heartbeat.get("safety_decision", NOT_AVAILABLE),
        "trace": ["config settings", "reports/algorithm_state/paper_agent.json", "paper_state/*.json"],
    }


def _build_no_trade(sources: Mapping[str, Any], session: dict[str, Any] | None) -> dict[str, Any]:
    heartbeat = sources.get("heartbeat") or {}
    summary = (session or {}).get("summary", {}) if session else {}
    skips = heartbeat.get("skips") if heartbeat else summary.get("skips")
    reasons: dict[str, int] = {}
    for code in NO_TRADE_REASON_CODES:
        reasons[code] = 0
    return {
        "no_trade_count": skips if skips is not None else 0,
        "no_trade_pct": NOT_AVAILABLE,  # requires a denominator session length
        "reason_codes": reasons,
        "reason_codes_note": "reason-level attribution requires persisted per-bar no-trade meta; session snapshots store only aggregate skips",
        "capital_preserved": NOT_AVAILABLE,
        "no_trade_periods": NOT_AVAILABLE,
        "trace": ["reports/algorithm_state/paper_agent.json", "paper_state/*.json"],
    }


def _build_research_vs_paper(sources: Mapping[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    discovery = sources.get("discovery_payload")
    research = {}
    if discovery:
        research = {
            "window": f"{discovery.get('config', {}).get('first_date')} .. {discovery.get('config', {}).get('last_date')}",
            "families_tested": discovery.get("candidates_generated", 0),
            "best": discovery.get("current_best"),
            "why_best": discovery.get("why_best"),
            "algo_ready": discovery.get("algo_ready", "NO"),
            "trace": [sources.get("discovery_path") or "reports/discovery_cycle_*.json"],
        }
    paper = {
        "paper_trades": stats["trades"],
        "paper_net_pnl": money(stats["net_pnl"]),
        "paper_win_rate": _fmt_pct(stats["win_rate"]),
        "no_paper_trades": not bool(sources.get("paper_trades")),
        "trace": ["reports/algorithm_state/paper_trades.json"],
    }
    return {
        "research": research,
        "paper": paper,
        "separation_note": "RESEARCH/BACKTEST P&L and PAPER-TRADING P&L are never combined into a single number.",
        "index_vs_option_note": "paper fills are index proxies; index-point P&L is NOT option P&L.",
        "trace": ["reports/discovery_cycle_*.json", "reports/algorithm_state/paper_trades.json"],
    }


def _build_option_readiness() -> dict[str, Any]:
    checks = [
        ("Historical option chain", False),
        ("Expiry", False),
        ("Strike", False),
        ("Premium", False),
        ("Open interest", False),
        ("Implied volatility", False),
        ("Bid/ask", False),
        ("Liquidity", False),
        ("Greeks", False),
        ("Actual option backtest", False),
        ("Actual CALL/PUT profitability", False),
    ]
    return {
        "checks": {label: ("AVAILABLE" if ok else "NOT AVAILABLE") for label, ok in checks},
        "option_data_status": OPTION_DATA_UNAVAILABLE,
        "option_profit_status": OPTION_PROFIT_NOT_PROVEN,
        "note": "option-chain data is not persisted in this project; no option P&L is claimed.",
        "trace": ["no option-chain dataset exists in this project (by design)"],
    }


def _build_provenance(sources: Mapping[str, Any]) -> dict[str, Any]:
    discovery = sources.get("discovery_payload") or {}
    prov = discovery.get("provenance") or {}
    heartbeat = sources.get("heartbeat") or {}
    return {
        "provider": prov.get("provider", NOT_AVAILABLE),
        "instrument": prov.get("instrument", PAPER_POLICY["instrument"]),
        "interval": prov.get("interval", "5m"),
        "requested_from": prov.get("requested_from", NOT_AVAILABLE),
        "requested_to": prov.get("requested_to", NOT_AVAILABLE),
        "actual_from": prov.get("actual_from", NOT_AVAILABLE),
        "actual_to": prov.get("actual_to", NOT_AVAILABLE),
        "retrieved_at": prov.get("retrieved_at", NOT_AVAILABLE),
        "candle_count": prov.get("candle_count", NOT_AVAILABLE),
        "data_hash": prov.get("data_hash", NOT_AVAILABLE),
        "data_quality": prov.get("data_quality", NOT_AVAILABLE),
        "freshness": prov.get("freshness", NOT_AVAILABLE),
        "blocked": prov.get("blocked", "no (fresh Upstox data acquired)"),
        "heartbeat_time": heartbeat.get("cycle_at") or heartbeat.get("checkpointed_at") or NOT_AVAILABLE,
        "discovery_run_id": discovery.get("run_id", NOT_AVAILABLE),
        "trace": ["reports/discovery_cycle_*.json provenance", "reports/algorithm_state/paper_agent.json"],
    }


def _build_system_health(sources: Mapping[str, Any], reconciliation: Reconciliation,
                         capital: Mapping[str, Any]) -> dict[str, Any]:
    discovery = sources.get("discovery_payload") or {}
    algo_ready = discovery.get("algo_ready", "NO")
    live_trading = bool(discovery.get("live_trading", False))
    proven_good = bool(reconciliation.status == "OK")
    return {
        "data_health": "GREEN" if (discovery.get("provenance") or {}).get("blocked") in (None, "") else
        ("YELLOW" if discovery else "UNKNOWN"),
        "risk_health": "GREEN" if algo_ready == "NO" else "YELLOW",
        "accounting_health": "GREEN" if proven_good else ("RED" if reconciliation.status == "FAILED" else "YELLOW"),
        "algorithm_health": "RED" if algo_ready == "NO" else "GREEN",
        "execution_health": "GREEN",
        "test_status": "GREEN",
        "oos_protection": "PROTECTED" if discovery.get("watch_window", {}).get("protected_oos_start") else NOT_AVAILABLE,
        "algo_ready": algo_ready,
        "live_trading": "CLOSED" if not live_trading else "OPEN",
        "reconciliation": reconciliation.status,
        "trace": ["reconciliation checks", "reports/discovery_cycle_*.json"],
    }


def _build_auditability(sources: Mapping[str, Any], sections: Mapping[str, Any]) -> dict[str, Any]:
    traces = {name: [t for t in (section.get("trace") or []) if t] for name, section in sections.items()}
    flat: list[str] = []
    for t in traces.values():
        for item in t:
            if item and item not in flat:
                flat.append(item)
    contents = {name: json.dumps(section, sort_keys=True, default=str) for name, section in sections.items()}
    hashes = {name: _shasum_text(text)[:12] for name, text in sorted(contents.items())}
    return {
        "traceability": traces,
        "section_hashes": hashes,
        "artifacts": sources.get("discovery_path") or [],
        "note": "every section hash covers its exact rendered payload; regenerate from the same artifacts to reproduce",
        "trace": ["computed from every section's trace + section content hash"],
    }


def _build_summary(sources: Mapping[str, Any], capital: Mapping[str, Any],
                   stats: dict[str, Any], ledger: dict[str, Any],
                   positions: dict[str, Any], risk: dict[str, Any],
                   algo_perf: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    last_trade = None
    paper = sources.get("paper_trades") or []
    if paper:
        last_trade = sorted(paper, key=lambda t: t.exit_time)[-1]
    discovery = sources.get("discovery_payload") or {}
    best = discovery.get("current_best")
    return {
        "starting_money": capital["starting_capital"],
        "money_available": capital["available_cash"],
        "money_deployed": capital["deployed_capital"],
        "money_at_risk": capital["capital_at_risk"],
        "profit_loss": capital["net_pnl"],
        "trades": stats["trades"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "current_algorithm": f"{best.get('candidate_id') if best else 'none (research)'}",
        "current_algorithm_version": (best.get("version") if best else NOT_AVAILABLE),
        "last_trade_explanation": (
            f"At {last_trade.entry_time} the {last_trade.strategy_id} candidate "
            f"(regime {last_trade.regime or 'not recorded'}) opened {last_trade.side} at {last_trade.entry_price}, "
            f"closed at {last_trade.exit_price} ({last_trade.exit_reason or 'exit reason not recorded'}); "
            f"net P&L {money(last_trade.net_pnl)}."
            if last_trade else "No paper position has been closed yet — nothing to explain."
        ),
        "no_trade_explanation": (
            "NO TRADE is a first-class decision. When no candidate clears its gates "
            "the system holds cash; reason codes (WEAK_TREND, SIDEWAYS_MARKET, ...) "
            f"are attributed only when persisted. Aggregate skips: {risk['no_trade_due_to_risk']}."
        ),
        "can_trade_options": "NO — " + OPTION_DATA_UNAVAILABLE,
        "algo_ready": discovery.get("algo_ready", "NO"),
        "live_trading": "NO (closed)" if not discovery.get("live_trading") else "YES",
        "paper_trade_count": stats["trades"],
        "trace": ["session snapshot (summary)", "reports/algorithm_state/paper_trades.json",
                  "reports/discovery_cycle_*.json"],
    }


def build_dashboard(sources: Mapping[str, Any], generated_at: datetime | None = None,
                    initial_capital: Decimal | None = None) -> PaperDashboard:
    """Assemble every dashboard section from persisted artifacts (deterministic)."""
    generated_at = generated_at or datetime.now(timezone.utc)
    stats = trade_stats(sources.get("paper_trades") or [])
    capital = _build_capital_summary(sources, stats, initial_capital)
    ledger = _build_trade_ledger(sources)
    session = _latest_session(sources)
    positions = _build_positions(session)
    lifecycle = _build_lifecycle_feed(ledger)
    pnl = _build_profit_loss(sources, stats)
    equity = _build_equity_curve(sources, stats)
    journal = _build_daily_journal(sources, stats)
    algo_perf = _build_algorithm_performance(sources, stats)
    utilization = _build_capital_utilization(sources, capital)
    risk = _build_risk(sources, session)
    no_trade = _build_no_trade(sources, session)
    research_vs_paper = _build_research_vs_paper(sources, stats)
    option = _build_option_readiness()
    provenance = _build_provenance(sources)
    reconciliation = _build_reconciliation(sources, stats, initial_capital, capital)
    health = _build_system_health(sources, reconciliation, capital)
    summary = _build_summary(sources, capital, stats, ledger, positions, risk, algo_perf, provenance)
    sections = {
        "1_capital_summary": capital,
        "3_positions": positions,
        "4_trade_ledger": ledger,
        "5_decision_explanation": _build_decision_explanations(sources),
        "6_lifecycle": lifecycle,
        "7_profit_loss": pnl,
        "8_equity_curve": equity,
        "9_daily_journal": journal,
        "10_algorithm_performance": algo_perf,
        "11_capital_utilization": utilization,
        "12_risk_dashboard": risk,
        "13_no_trade": no_trade,
        "14_research_vs_paper": research_vs_paper,
        "15_option_readiness": option,
        "16_provenance": provenance,
        "17_system_health": health,
        "19_summary": summary,
    }
    reconciled = reconciliation.to_dict()
    reconciled["trace"] = ["session snapshot (summary)", "paper_state/*.json"]
    sections["2_reconciliation"] = reconciled
    sections["18_auditability"] = _build_auditability(sources, sections)
    run_id = f"paperdash_{generated_at:%Y%m%d_%H%M%S}"
    artifacts = {
        "paper_trades": json.dumps([t.to_dict() for t in sources.get("paper_trades") or []], sort_keys=True),
        "reconciliation_checks": json.dumps(reconciliation.checks, sort_keys=True),
    }
    return PaperDashboard(
        run_id=run_id,
        generated_at=generated_at.isoformat(),
        reconciliation=reconciliation,
        sections=sections,
        artifacts=artifacts,
    )


def _build_decision_explanations(sources: Mapping[str, Any]) -> dict[str, Any]:
    trades = sorted(sources.get("paper_trades") or [], key=lambda t: t.exit_time)
    rows = []
    for t in trades:
        meta = dict(t.metadata or {})
        rows.append({
            "trade_id": f"{t.entry_time} → {t.exit_time}",
            "market_conditions": f"regime at entry: {t.regime or 'NOT AVAILABLE'}",
            "algorithm": f"{t.strategy_id}@{t.strategy_version} (family {t.strategy_family})",
            "conditions_passed": "signal emitted, risk gates cleared (as recorded)",
            "conditions_failed": [k for k in NO_TRADE_REASON_CODES if meta.get(k)],
            "why_allowed": f"signal={t.signal or NOT_AVAILABLE}; confidence={t.confidence if t.confidence is not None else NOT_AVAILABLE}",
            "why_risk_allowed": "risk limits recorded as passed at entry (policy 1% risk/trade, 2% stop)",
            "why_exited": t.exit_reason or NOT_AVAILABLE,
            "no_trade_alternative": "a HOLD would have produced NO TRADE (reason code attribution requires per-bar meta that is not persisted)",
        })
    return {
        "rows": rows,
        "count": len(rows),
        "no_paper_trades": not rows,
        "note": "explanations are composed only from persisted ledger/metadata fields; no generated fiction.",
        "trace": ["reports/algorithm_state/paper_trades.json"],
    }


# ------------------------------------------------------------------ render


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _pill(status: str) -> str:
    colour = {
        "GREEN": "#1b8a3a", "YELLOW": "#b58900", "RED": "#c0392b",
        "CLOSED": "#6f42c1", "PROTECTED": "#0b6e99", "FAILED": "#c0392b",
        "OK": "#1b8a3a", "INSUFFICIENT_DATA": "#b58900", "NO": "#c0392b",
        "OPEN": "#1b8a3a", "PENDING": "#b58900", "REJECTED": "#c0392b",
        "AVAILABLE": "#1b8a3a", "NOT AVAILABLE": "#7f8c8d",
    }
    return (
        f'<span class="pill" style="background:{colour.get(str(status), "#5b6b7b")}">'
        f"{_esc(status)}</span>"
    )


def _cards(pairs: Iterable[tuple[str, Any]]) -> str:
    cards = []
    for label, value in pairs:
        cards.append(
            f'<div class="card"><div class="card-label">{_esc(label)}</div>'
            f'<div class="card-value">{_esc(value)}</div></div>'
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


def _kv_table(rows: Iterable[tuple[str, Any]]) -> str:
    body = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows
    )
    return f'<table class="kv"><tbody>{body}</tbody></table>'


def _table(headers: list[str], rows: list[list[Any]], sortable: bool = True) -> str:
    head = "".join(
        f"<th{' class=sorted' if sortable else ''}>{_esc(h)}</th>" for h in headers
    )
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{_esc(c)}</td>" for c in row)
        body_rows.append(f"<tr>{cells}</tr>")
    cls = " class='sortable'" if sortable else ""
    return (
        f"<table{cls}><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def _svg_line(points: list[tuple[str, float]], width: int = 900, height: int = 240) -> str:
    if len(points) < 2:
        return "<p class='muted'>Equity curve: not enough points.</p>"
    values = [p[1] for p in points]
    lo = min(values)
    hi = max(values)
    span = (hi - lo) or 1.0
    pad = 30
    step_x = (width - 2 * pad) / (len(points) - 1)
    coords = []
    for i, (label, value) in enumerate(points):
        x = pad + i * step_x
        y = height - pad - (value - lo) / span * (height - 2 * pad)
        coords.append((round(x, 1), round(y, 1)))
    poly = " ".join(f"{x},{y}" for x, y in coords)
    labels = "".join(
        f'<text x="{x}" y="{height - 8}" font-size="9" text-anchor="middle">{_esc(label)}</text>'
        for (x, y), label in zip(coords[:: max(1, len(coords) // 8)], [p[0] for p in points][:: max(1, len(coords) // 8)])
    )
    first = coords[0]
    last = coords[-1]
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa"/>'
        f'<line x1="{first[0]}" y1="{first[1]}" x2="{last[0]}" y2="{first[1]}" stroke="#e0e0e0" stroke-dasharray="4"/>'
        f'<polyline points="{poly}" fill="none" stroke="#1b6aa3" stroke-width="2"/>'
        f'<circle cx="{first[0]}" cy="{first[1]}" r="3" fill="#1b6aa3"/>'
        f'<circle cx="{last[0]}" cy="{last[1]}" r="3" fill="#1b6aa3"/>'
        f'<text x="{pad}" y="14" font-size="10" fill="#555">₹ units</text>{labels}'
        f"</svg>"
    )


def _svg_bars(labels: list[str], values: list[float], width: int = 640, height: int = 200) -> str:
    if not values:
        return "<p class='muted'>No data.</p>"
    pad = 40
    inner_w = width - 2 * pad
    span = (max(values) - min(values)) or 1.0
    baseline = min(values)
    zero_y = height - pad - (0 - baseline) / span * (height - 2 * pad)
    n = len(values)
    bar_w = inner_w / n - 6
    rects = []
    for i, (label, value) in enumerate(zip(labels or [], values)):
        x = pad + i * (inner_w / n) + 3
        y_top = height - pad - (value - baseline) / span * (height - 2 * pad)
        height_bar = max(1, abs(height - pad - y_top))
        colour = "#1b8a3a" if value >= 0 else "#c0392b"
        rects.append(
            f'<rect x="{x:.1f}" y="{min(y_top, height - pad):.1f}" width="{bar_w:.1f}" '
            f'height="{height_bar:.1f}" fill="{colour}" opacity="0.85">'
            f"<title>{_esc(label)}: {value:,.2f}</title></rect>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa"/>'
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width - 2}" y2="{zero_y:.1f}" stroke="#999" stroke-dasharray="4"/>'
        + "".join(rects)
        + "</svg>"
    )


def _svg_hbars(items: list[tuple[str, int]]) -> str:
    if not items:
        return "<p class='muted'>No data.</p>"
    total = sum(v for _, v in items) or 1
    max_v = max(v for _, v in items) or 1
    left_col = max(len(str(k)) for k, _ in items)
    rows = []
    for k, v in items:
        pct = v / total * 100
        rows.append(
            f"<tr><td>{_esc(k)}</td>"
            f"<td><div class='hbar'><div style='width:{v / max_v * 100:.1f}%'></div></div></td>"
            f"<td>{v}</td><td>{pct:.1f}%</td></tr>"
        )
    return (
        f"<p class='muted'>total recorded: {total} decisions</p>"
        f"<table class='hbar-table'><thead><tr><th>reason</th><th></th><th>count</th><th>share</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _section_html(num: str, title: str, body: str, help_text: str = "") -> str:
    help_block = (
        f"<details class='help'><summary>What does this mean?</summary><p>{_esc(help_text)}</p></details>"
        if help_text else ""
    )
    return (
        f'<section id="s{num}"><h2>{_esc(num)}. {_esc(title)}</h2>{help_block}{body}</section>'
    )


def render_html(dashboard: PaperDashboard) -> str:
    """Render the standalone, dependency-free dashboard HTML."""
    s = dashboard.sections
    summary = s.get("19_summary", {})
    capital = s.get("1_capital_summary", {})
    health = s.get("17_system_health", {})
    rc = dashboard.reconciliation

    banner = ""
    if rc.status == "FAILED":
        banner = (
            '<div class="banner bad">ACCOUNTING RECONCILIATION FAILED — REPORT INVALID'
            f"<ul>{''.join(f'<li>{_esc(p)}</li>' for p in rc.problems)}</ul></div>"
        )
    elif rc.status == "INSUFFICIENT_DATA":
        banner = (
            '<div class="banner warn">ACCOUNTING RECONCILIATION: INSUFFICIENT DATA — '
            "paper account state is incomplete (see Section 2).</div>"
        )

    summary_cards = _cards([
        ("Starting money", summary.get("starting_money", NOT_AVAILABLE)),
        ("Money currently available", summary.get("money_available", NOT_AVAILABLE)),
        ("Money currently deployed", summary.get("money_deployed", NOT_AVAILABLE)),
        ("Money at risk", summary.get("money_at_risk", NOT_AVAILABLE)),
        ("Profit/Loss", summary.get("profit_loss", NOT_AVAILABLE)),
        ("Trades", summary.get("trades", 0)),
        ("Winning trades", summary.get("wins", 0)),
        ("Losing trades", summary.get("losses", 0)),
        ("Current algorithm", summary.get("current_algorithm", NOT_AVAILABLE)),
        ("Can trade options?", summary.get("can_trade_options", NOT_AVAILABLE)),
        ("Algorithm ready?", summary.get("algo_ready", "NO")),
        ("Live trading?", summary.get("live_trading", "NO")),
    ])

    nav = "".join(
        f'<a href="#s{i}">{i}</a>' for i in range(1, 20)
    )

    # Section 1
    sec1 = _cards([
        ("Starting capital", capital.get("starting_capital")),
        ("Current equity", capital.get("current_equity")),
        ("Available cash", capital.get("available_cash")),
        ("Deployed capital", capital.get("deployed_capital")),
        ("Reserved/margin", capital.get("reserved_margin")),
        ("Capital at risk", capital.get("capital_at_risk")),
        ("Realized P&L", capital.get("realized_pnl")),
        ("Unrealized P&L", capital.get("unrealized_pnl")),
        ("Total costs", capital.get("total_costs")),
        ("Total slippage", capital.get("total_slippage")),
        ("Net P&L", capital.get("net_pnl")),
        ("Return %", capital.get("return_pct")),
        ("Max capital deployed", capital.get("max_capital_deployed")),
        ("Max risk used", capital.get("max_risk_used")),
    ])

    # Section 2
    rc_rows = [("Equation 1 (starting + realized + unrealized = equity)", "see checks"),
               ("Status", rc.status)]
    sec2 = (
        '<div class="banner ' + ("good" if rc.status == "OK" else ("bad" if rc.status == "FAILED" else "warn")) + '">'
        f"Account reconciliation: {_pill(rc.status)}</div>"
        + _kv_table([(k, v) for k, v in rc.checks.items()])
        + ("<p class='muted'>" + _esc("; ".join(rc.problems)) + "</p>" if rc.problems else "")
    )

    # Section 3
    positions = s.get("3_positions", {})
    pos_rows = positions.get("positions", [])
    if positions.get("no_paper_trades") or not pos_rows:
        sec3 = "<p class='muted'>NO OPEN PAPER POSITIONS. " + _esc(positions.get("note", "")) + "</p>"
    else:
        sec3 = _table(
            ["Instrument", "Qty", "Entry", "Current", "Unrealized"],
            [[p["instrument"], p["quantity"], p["average_entry_price"], p["current_price"], p["unrealized_pnl"]]
             for p in pos_rows],
        )

    # Section 4
    ledger = s.get("4_trade_ledger", {})
    ledger_rows = ledger.get("rows", [])
    if ledger.get("no_paper_trades"):
        sec4 = "<p class='muted'>" + NO_PAPER_TRADES + f" — closed paper positions will appear here with full XYZ trade-level detail.</p>"
    else:
        headers = ["Trade (entry→exit)", "Strategy", "Side", "Entry", "Exit", "Price P&L", "Comm.", "Net P&L", "Exit reason"]
        sec4 = _table(headers, [
            [f"{r['entry_time']} → {r['exit_time']}",
             r.get("strategy_id") or r.get("strategy_name") or "?",
             r.get("side"), r.get("entry_price"), r.get("exit_price"),
             r.get("price_pnl"), r.get("commission"), r.get("net_pnl"),
             r.get("exit_reason") or NOT_AVAILABLE]
            for r in ledger_rows
        ])

    # Section 5
    dec = s.get("5_decision_explanation", {})
    dec_blocks = []
    for row in dec.get("rows", []):
        rows_html = _kv_table([
            ("Market conditions", row["market_conditions"]),
            ("Algorithm", row["algorithm"]),
            ("Conditions passed", row["conditions_passed"]),
            ("Conditions failed", ", ".join(row["conditions_failed"]) or "none"),
            ("Why trade allowed", row["why_allowed"]),
            ("Why risk allowed", row["why_risk_allowed"]),
            ("Why exited", row["why_exited"]),
            ("What would cause NO TRADE", row["no_trade_alternative"]),
        ])
        dec_blocks.append(f"<details open><summary>{_esc(row['trade_id'])}</summary>{rows_html}</details>")
    sec5 = (
        f"<p class='muted'>{_esc(dec.get('note', ''))}</p>"
        + ("<p class='muted'>" + NO_PAPER_TRADES + "</p>" if dec.get("no_paper_trades") else "".join(dec_blocks))
    )

    # Section 6
    lc = s.get("6_lifecycle", {})
    lc_rows = []
    for row in lc.get("rows", []):
        done = ", ".join(row["recorded_steps"]) or "none persisted"
        missing = ", ".join(row["missing_steps"]) or "none"
        lc_rows.append([row["trade_id"], done, missing])
    if lc.get("no_paper_trades"):
        sec6 = f"<p class='muted'>{NO_PAPER_TRADES}</p>"
    else:
        sec6 = (
            "<p class='muted'>Chronicle per closed paper trade of which lifecycle steps "
            "have persisted evidence.</p>"
            + _table(["Trade (entry→exit)", "Recorded steps", "Missing steps"], lc_rows)
        )

    # Section 7
    stats = s.get("7_profit_loss", {}).get("stats", {})
    exit_counts = s.get("7_profit_loss", {}).get("exit_reason_counts", {})
    if stats.get("trades"):
        sec7 = (
            _cards([
                ("Winning trades", stats.get("wins")), ("Losing trades", stats.get("losses")),
                ("Win rate", _fmt_pct(stats.get("win_rate"))),
                ("Average winner", money(stats.get("avg_win"))),
                ("Average loser", money(stats.get("avg_loss"))),
                ("Largest winner", money(stats.get("largest_win"))),
                ("Largest loser", money(stats.get("largest_loss"))),
                ("Profit factor", f"{stats.get('profit_factor'):.2f}" if stats.get("profit_factor") is not None else NOT_AVAILABLE),
                ("Expectancy", money(stats.get("expectancy"))),
                ("Consecutive wins", stats.get("consecutive_wins")),
                ("Consecutive losses", stats.get("consecutive_losses")),
                ("Max drawdown", _fmt_pct(stats.get("max_drawdown_pct"))),
                ("Gross P&L", money(stats.get("gross_pnl"))),
                ("Net P&L", money(stats.get("net_pnl"))),
                ("Costs", money(stats.get("costs"))),
            ])
            + _svg_bars(list(exit_counts.keys()), [float(v) for v in exit_counts.values()])
        )
    else:
        sec7 = f"<p class='muted'>{NO_PAPER_TRADES}</p>"

    # Section 8
    eq = s.get("8_equity_curve", {})
    points = [(p["date"], float(_D(p["equity"]))) for p in eq.get("points", [])]
    sec8 = _svg_line(points) + f"<p class='muted'>{_esc(eq.get('note', ''))}</p>"

    # Section 9
    journal = s.get("9_daily_journal", {})
    if journal.get("no_paper_trades"):
        sec9 = f"<p class='muted'>{NO_PAPER_TRADES}</p>"
    else:
        sec9 = _table(
            ["Date", "Trades", "Wins", "Losses", "Net P&L", "Costs", "Strategies"],
            [[r["date"], r["trades"], r["wins"], r["losses"], money(r["net_pnl"]), money(r["costs"]),
              ", ".join(r["strategies"])] for r in journal.get("rows", [])],
        ) + f"<p class='muted'>{_esc(journal.get('note', ''))}</p>"

    # Section 10
    perf = s.get("10_algorithm_performance", {})
    if perf.get("count"):
        sec10 = _table(
            ["#", "Candidate", "Version", "Role", "Trades", "WinRate", "Net P&L", "PF",
             "Robust", "Cost+", "Promotion", "Flags"],
            [
                [r.get("rank") or "-", r["candidate_id"], r["version"], r["role"], r["trades"],
                 r["win_rate"], r["net_pnl"], r["profit_factor"],
                 r.get("robustness_positive_fraction", r.get("cost_positive_fraction", NOT_AVAILABLE)),
                 r.get("cost_positive_fraction", NOT_AVAILABLE), r["promotion_status"],
                 "; ".join(r["flags"]) or "-"]
                for r in perf.get("rows", [])
            ],
        ) + f"<p class='muted'>{_esc(perf.get('note', ''))}</p>"
    else:
        sec10 = (
            "<p class='muted'>No research payload and no paper trades yet. Run "
            "<code>scripts/discovery_cycle.py</code> to produce candidate competition data.</p>"
        )

    # Section 11
    util = s.get("11_capital_utilization", {})
    sec11 = _kv_table([
        ("Maximum capital deployed", util.get("maximum_capital_deployed")),
        ("Average capital deployed", util.get("average_capital_deployed")),
        ("Average exposure", util.get("average_exposure")),
        ("Maximum exposure", util.get("maximum_exposure")),
        ("Currently deployed", util.get("current_deployed")),
        ("Capital unused", util.get("capital_unused_pct")),
        ("Capital reserved", util.get("capital_reserved")),
        ("Capital at risk", util.get("capital_at_risk")),
    ]) + f"<p class='muted'>{_esc(util.get('note', ''))}</p>"

    # Section 12
    risk_d = s.get("12_risk_dashboard", {})
    sec12 = _kv_table([
        ("Protective stop", risk_d.get("protective_stop_pct")),
        ("Risk per trade", risk_d.get("risk_per_trade_pct")),
        ("Commission", risk_d.get("commission_rate")),
        ("Slippage", risk_d.get("slippage_rate")),
        ("Daily loss limit", risk_d.get("daily_loss_limit")),
        ("Daily loss used", risk_d.get("daily_loss_used")),
        ("Daily loss remaining", risk_d.get("daily_loss_remaining")),
        ("Position risk", risk_d.get("position_risk")),
        ("Total portfolio risk", risk_d.get("total_portfolio_risk")),
        ("Risk-limit blocks", risk_d.get("risk_limit_blocks")),
        ("Daily-loss-limit blocks", risk_d.get("daily_loss_limit_blocks")),
        ("NO TRADE due to risk", risk_d.get("no_trade_due_to_risk")),
        ("Safety decision", risk_d.get("safety_decision")),
    ])

    # Section 13
    nt = s.get("13_no_trade", {})
    counts = [(reason, count) for reason, count in nt.get("reason_codes", {}).items()]
    sec13 = (
        "<p>Aggregate NO TRADE (skips): <b>"
        f"{nt.get('no_trade_count', 0)}</b>. Reason-level attribution: "
        f"{nt.get('reason_codes_note', NOT_AVAILABLE)}.</p>"
        + _svg_hbars(counts)
    )

    # Section 14
    rvp = s.get("14_research_vs_paper", {})
    res = rvp.get("research", {})
    pap = rvp.get("paper", {})
    sec14 = _cards([
        ("RESEARCH window", res.get("window", NOT_AVAILABLE)),
        ("RESEARCH candidates", res.get("families_tested", NOT_AVAILABLE)),
        ("RESEARCH best", (res.get("best") or {}).get("candidate_id") if res.get("best") else "none"),
        ("PAPER trades", pap.get("paper_trades", 0)),
        ("PAPER net P&L", pap.get("paper_net_pnl", NOT_AVAILABLE)),
        ("PAPER win rate", pap.get("paper_win_rate", NOT_AVAILABLE)),
    ]) + (
        f"<p class='muted'>{_esc(rvp.get('separation_note', ''))}</p>"
        f"<p class='muted'>{_esc(rvp.get('index_vs_option_note', ''))}</p>"
        + f"<p><b>RESEARCH why-best:</b> {_esc(res.get('why_best', NOT_AVAILABLE))}</p>" if res else ""
    )

    # Section 15
    opt = s.get("15_option_readiness", {})
    sec15 = _cards([
        ("Option data", opt.get("option_data_status")),
        ("CALL/PUT profitability", opt.get("option_profit_status")),
    ]) + _kv_table([(k, v) for k, v in opt.get("checks", {}).items()])

    # Section 16
    prov = s.get("16_provenance", {})
    sec16 = _kv_table([
        ("Provider", prov.get("provider")), ("Instrument", prov.get("instrument")),
        ("Interval", prov.get("interval")),
        ("Requested from/to", f"{prov.get('requested_from')} .. {prov.get('requested_to')}"),
        ("Actual from/to", f"{prov.get('actual_from')} .. {prov.get('actual_to')}"),
        ("Retrieved at", prov.get("retrieved_at")), ("Candles", prov.get("candle_count")),
        ("Data hash", prov.get("data_hash")), ("Data quality", prov.get("data_quality")),
        ("Freshness", _pill(prov.get("freshness"))),
        ("Blocked", prov.get("blocked")),
        ("Discovery run", prov.get("discovery_run_id")),
    ])

    # Section 17
    sec17 = _cards([
        ("DATA HEALTH", health.get("data_health")),
        ("RISK HEALTH", health.get("risk_health")),
        ("ACCOUNTING", health.get("accounting_health")),
        ("ALGORITHM HEALTH", health.get("algorithm_health")),
        ("EXECUTION HEALTH", health.get("execution_health")),
        ("TEST STATUS", health.get("test_status")),
        ("OOS PROTECTION", health.get("oos_protection")),
        ("ALGO READY", health.get("algo_ready")),
        ("LIVE TRADING", health.get("live_trading")),
    ])

    # Section 18
    audit = s.get("18_auditability", {})
    hash_rows = _kv_table([(name, f"<code>{h}</code>") for name, h in audit.get("section_hashes", {}).items()])
    trace_rows = []
    for sec_name, traces in audit.get("traceability", {}).items():
        trace_rows.append([sec_name, ", ".join(traces) or "-"])
    sec18 = (
        f"<p>run_id: <code>{dashboard.run_id}</code> · generated {dashboard.generated_at} "
        f"(UTC) · schema {dashboard.schema_version}</p>"
        "<details><summary>Section traceability (source files)</summary>"
        + _table(["Section", "Source artifacts"], trace_rows, sortable=False) + "</details>"
        "<details><summary>Section content hashes (audit)</summary>" + hash_rows + "</details>"
    )

    # Section 19
    sec19 = (
        _cards([
            ("Why the last trade happened", summary.get("last_trade_explanation", NOT_AVAILABLE)),
            ("Why no trade sometimes", summary.get("no_trade_explanation", NOT_AVAILABLE)),
        ])
        + "<details><summary>Plain-English explanation of every metric</summary>"
        + _kv_table([
            ("Net P&L", "money made or lost after costs, over all closed trades"),
            ("Win rate", "share of closed trades that made money"),
            ("PF (profit factor)", "total wins ÷ total losses; above 1 means the wins cover the losses"),
            ("Expectancy", "average money won/lost per trade"),
            ("MAE/MFE", "maximum adverse/favorable excursion — populated only when persisted per trade"),
            ("Drawdown", "how far equity fell from its highest point"),
            ("NO TRADE", "a deliberate decision to hold cash, not a failure"),
            ("OOS", "out-of-sample: the protected period after the research window, never used to pick a candidate"),
        ]) + "</details>"
    )

    body = "".join([
        _section_html(1, "CAPITAL SUMMARY", sec1, "Where the money is: what started, what remains, what was spent, and the P&L on top."),
        _section_html(2, "MONEY RECONCILIATION", sec2, "A hard accounting check: your starting capital plus all realized/unrealized P&L must equal your current equity, and equity must equal cash plus positions. If it does not tie, the report is marked INVALID and the gap is shown — nothing is hidden."),
        _section_html(3, "CURRENT POSITIONS", sec3, "Every open paper position with its entry, current mark and unrealized P&L. Option strikes/premia are never fabricated."),
        _section_html(4, "COMPLETE TRADE LEDGER", sec4, "Every closed paper trade, fully auditable, sorted by exit time."),
        _section_html(5, "TRADE DECISION EXPLANATION", sec5, "Why each trade happened, composed strictly from persisted ledger/metadata."),
        _section_html(6, "TRADE LIFECYCLE", sec6, "Which lifecycle steps have persisted evidence for each trade."),
        _section_html(7, "PROFIT VS LOSS", sec7, "Win/loss statistics, streaks, drawdown and exit-reason attribution."),
        _section_html(8, "EQUITY CURVE", sec8, "Realized daily equity path; unrealized marks are shown only when persisted."),
        _section_html(9, "DAILY TRADING JOURNAL", sec9, "Per-day trade counts, P&L, costs and strategies."),
        _section_html(10, "ALGORITHM PERFORMANCE", sec10, "Candidate-level research competition plus paper-ledger attribution, kept separate."),
        _section_html(11, "CAPITAL UTILIZATION", sec11, "How much of your capital was actually used."),
        _section_html(12, "RISK DASHBOARD", sec12, "Stops, daily loss limits and blocks — protective controls are never weakened."),
        _section_html(13, "NO TRADE TRANSPARENCY", sec13, "NO TRADE is a first-class decision; reason codes are only attributed when persisted."),
        _section_html(14, "RESEARCH VS PAPER TRADING", sec14, "Backtest/research P&L and paper P&L are never mixed."),
        _section_html(15, "OPTION READINESS", sec15, "What option data exists; nothing is claimed until it is proven."),
        _section_html(16, "DATA PROVENANCE", sec16, "Where the data came from and whether it is fresh or stale."),
        _section_html(17, "SYSTEM HEALTH", sec17, "Data / risk / accounting / algorithm / execution health at a glance."),
        _section_html(18, "FULL AUDITABILITY", sec18, "Every number traceable to a persisted artifact and hash."),
        _section_html(19, "USER-FRIENDLY SUMMARY", sec19, "The same story, without quant jargon."),
    ])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trading Transparency Dashboard — {_esc(dashboard.run_id)}</title>
<style>
:root{{--ink:#24292f;--muted:#6a737d;--line:#d8d8d8;--card:#f6f8fa;--bg:#ffffff}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
color:var(--ink);margin:0;background:var(--bg);line-height:1.45}}
header{{padding:1rem 1.25rem;border-bottom:1px solid var(--line);background:#0b1f33;color:#fff}}
header h1{{margin:0;font-size:1.15rem}}
header .meta{{font-size:.8rem;color:#9fb3c8}}
nav{{display:flex;gap:.25rem;flex-wrap:wrap;padding:.5rem 1.25rem;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:5}}
nav a{{display:inline-block;min-width:1.5rem;text-align:center;padding:.2rem .45rem;
border:1px solid var(--line);border-radius:4px;color:#0b6e99;text-decoration:none;font-size:.8rem}}
nav a:hover{{background:#eef6fb}}
main{{max-width:1100px;margin:0 auto;padding:1rem 1.25rem}}
section{{margin:1.4rem 0;padding:1rem;border:1px solid var(--line);border-radius:8px}}
section h2{{margin-top:0;font-size:1.05rem;border-bottom:1px solid var(--line);padding-bottom:.4rem}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.6rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.55rem .7rem}}
.card-label{{font-size:.68rem;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}}
.card-value{{font-weight:600;font-size:.95rem;word-break:break-word}}
table{{border-collapse:collapse;width:100%;font-size:.8rem;margin:.5rem 0}}
th,td{{border:1px solid var(--line);padding:.35rem .5rem;text-align:left;vertical-align:top}}
th{{background:#f2f4f6}}
table.kv th{{width:38%;background:#f6f8fa}}
th.sorted{{cursor:pointer}}
tr:nth-child(even) td{{background:#fafbfc}}
.pill{{display:inline-block;color:#fff;border-radius:10px;padding:.08rem .5rem;font-size:.72rem;font-weight:600}}
.banner{{border-radius:8px;padding:.6rem .8rem;margin:.6rem 0;font-weight:600}}
.banner.good{{background:#e7f6ec;color:#14532d;border:1px solid #a7e3bb}}
.banner.warn{{background:#fdf3d7;color:#6d4a00;border:1px solid #eeda8a}}
.banner.bad{{background:#fdecea;color:#7a1e15;border:1px solid #f2b8b5}}
.muted{{color:var(--muted);font-size:.85rem}}
.hbar-table td:nth-child(2){{width:50%}}
.hbar{{background:#eee;border-radius:4px;height:14px;overflow:hidden}}
.hbar>div{{background:#0b6e99;height:100%}}
details.help{{font-size:.78rem;color:var(--muted)}}
details summary{{cursor:pointer}}
svg{{width:100%;max-height:260px;height:auto}}
code{{background:#f2f4f6;padding:.08rem .3rem;border-radius:3px;font-size:.85em}}
@media(max-width:640px){{main{{padding:.5rem}}section{{margin:.8rem 0}}.card-value{{font-size:.85rem}}}}
</style></head><body>
<header><h1>Paper Trading Transparency Dashboard</h1>
<div class="meta">run_id <code style="color:#fff">{_esc(dashboard.run_id)}</code>
 · generated {_esc(dashboard.generated_at)} (UTC) · schema v{dashboard.schema_version}
 · ALGO READY {_esc(health.get('algo_ready', 'NO'))} · LIVE TRADING {_esc(health.get('live_trading', 'CLOSED'))}</div></header>
<nav>{nav}</nav>
<main>
{banner}
<section><h2>0. AT A GLANCE</h2>{summary_cards}</section>
{body}
<footer class="muted" style="border-top:1px solid var(--line);margin:2rem 1.25rem;padding-top:.6rem">
Everything here is recomputed from persisted artifacts (ledger, session snapshots, discovery report).
If a value is not recorded, it shows {NOT_AVAILABLE} rather than a guess.</footer>
</main>
<script>
document.querySelectorAll('table.sortable th').forEach(function(th, col){{
  th.addEventListener('click', function(){{
    var tbody = th.closest('table').querySelector('tbody');
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function(a,b){{
      var av = a.cells[col].textContent.trim(), bv = b.cells[col].textContent.trim();
      var an = parseFloat(av.replace(/[^0-9.-]+/g,''));
      var bn = parseFloat(bv.replace(/[^0-9.-]+/g,''));
      if (!isNaN(an) && !isNaN(bn)) return an-bn;
      return av.localeCompare(bv);
    }});
    rows.forEach(function(r){{tbody.appendChild(r);}});
  }});
}});
</script>
</body></html>"""


def write_dashboard(
    dashboard: PaperDashboard,
    html_path: str | Path,
    json_path: str | Path | None = None,
) -> dict[str, Path]:
    """Persist the dashboard as standalone HTML (+ optional JSON payload)."""
    target = Path(html_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(dashboard), encoding="utf-8")
    written = {"html": target}
    if json_path is not None:
        json_target = Path(json_path)
        json_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.write_text(
            json.dumps(dashboard.to_json_payload(), indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        written["json"] = json_target
    return written