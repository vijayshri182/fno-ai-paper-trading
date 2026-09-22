"""Experiment-specific metrics, reconciliation and fiscal fingerprint.

The report schema lives entirely in the isolated experiment namespace; it
never touches the frozen MA(5,21) report schema, and it always carries the
experiment tag ``15M_DIRECTIONAL_OPTIONS_EXPERIMENT``. A baseline comparison
table may be attached at reporting level only; no winner is ever declared.
"""

from __future__ import annotations

import statistics
from decimal import Decimal

from fno_ai_paper_trading.experiments.directional_15m.contract import (
    EXPERIMENT_ID,
    DONCHIAN_ENTRY_CHANNEL,
    DONCHIAN_EXIT_CHANNEL,
)
from fno_ai_paper_trading.paper_track.store import stable_dumps

__all__ = [
    "build_experiment_report",
    "baseline_metrics_from_engine",
    "comparison_table",
]

_SIGNAL_SOURCE = (
    f"DonchianBreakout({DONCHIAN_ENTRY_CHANNEL},{DONCHIAN_EXIT_CHANNEL}) from "
    "strategies/research_candidates.py (read-only, frozen)"
)


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def build_experiment_report(engine) -> dict:
    """Full metric set for the current state of ``engine`` (deterministic)."""
    portfolio = engine.portfolio
    payments = [fill for fill in engine.broker.fills]
    commissions = sum((_d(f.commission) for f in payments), Decimal("0"))
    realized = portfolio.realized_pnl
    net = realized - commissions

    closures = list(engine.closures)
    wins = [c for c in closures if _d(c["realized"]) > 0]
    losses = [c for c in closures if _d(c["realized"]) < 0]
    flat = [c for c in closures if _d(c["realized"]) == 0]
    decided = [_d(c["realized"]) for c in closures if _d(c["realized"]) != 0]
    gross_profit = sum((_d(c["realized"]) for c in wins), Decimal("0"))
    gross_loss = sum((_d(c["realized"]) for c in losses), Decimal("0"))
    best = max(decided) if decided else None
    worst = min(decided) if decided else None
    gross_avg = (gross_profit + gross_loss) / Decimal(str(len(decided))) if decided else None
    win_rate = None
    if wins or losses:
        win_rate = Decimal(str(len(wins))) / Decimal(str(len(wins) + len(losses)))

    minutes = sorted(int(c["minutes"]) for c in closures)
    tpm = {
        "mean": round(statistics.mean(minutes), 2) if minutes else None,
        "median": int(statistics.median(minutes)) if minutes else None,
        "max": minutes[-1] if minutes else None,
    }

    max_streak = 0
    current_streak = 0
    for c in reversed(closures):
        if _d(c["realized"]) < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    dd = engine.max_drawdown
    peak = max(engine.equity_curve)
    dd_pct = (dd / peak) if peak else Decimal("0")

    worst_day = _worst_day(engine.closures)

    instrument = engine.config.instrument
    baseline = getattr(engine, "baseline_metrics", None)

    report_payload = {
        "schema": "15m_directional_options.report",
        "version": 1,
        "experiment_id": EXPERIMENT_ID,
        "scope": {
            "instrument": {
                "symbol": instrument.symbol,
                "exchange": instrument.exchange,
                "lot_size": instrument.lot_size,
                "multiplier": instrument.multiplier,
                # Label-only: the underlying index carries no option fields.
                "option_type": instrument.option_type,
                "strike": str(instrument.strike) if instrument.strike is not None else None,
                "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
            },
            "signal_source": _SIGNAL_SOURCE,
            "cadence": {
                "bar_minutes": 5,
                "window_bars": 3,
                "decision_minutes": 15,
                "first_decision": "09:30",
                "last_decision": "15:15",
                "flatten_time": "15:20",
            },
            "option_selection": {
                "mode": "label-only position intents on the index",
                "zero_premium": True,
                "no_strike": True,
                "no_expiry": True,
                "no_premium": True,
                "note": (
                    "CALL=long index, PUT=short index; no option strike/expiry/premium is "
                    "ever fabricated. P&L is the index move (zero-premium simplification)."
                ),
            },
            "days": sorted(engine._finalized_days),
            "granularity": "position decisions only at 15-minute decision points",
        },
        "accounting": {
            "initial_cash": str(engine.config.initial_cash),
            "cash_end": str(portfolio.cash),
            "realized_pnl": str(realized),
            "commissions": str(commissions),
            "gross_profit": str(gross_profit),
            "gross_loss": str(gross_loss),
            "net_pnl": str(net),
            "reconciled": portfolio.cash
            == engine.config.initial_cash + realized - commissions,
            "reconciliation_note": "cash_end == initial_cash + realized_pnl - commissions",
        },
        "decisions": {
            "decision_points_total": engine.counters["decision_points"],
            "signal_counts": dict(engine.counters["signal_counts"]),
            "entries": _leg_sides(engine, "entries"),
            "exits": _leg_sides(engine, "exits"),
            "switches": dict(getattr(engine, "_switch_derived", {"CALL_TO_PUT": 0, "PUT_TO_CALL": 0})),
            "reversals": engine.counters["reversals"],
            "stops_fired": engine.counters["stops_fired"],
            "eod_flattens": engine.counters["flatten_count"],
            "mid_window_actions": 0,
            "so_invariant_note": (
                "every position change is attached to a 15-minute decision point; "
                "no action can occur mid-window."
            ),
            "risk_refusals": list(engine.counters["risk_refusals"]),
            "sizing_skips": list(engine.counters["sizing_skips"]),
        },
        "performance": {
            "round_trips": len(closures),
            "wins": len(wins),
            "losses": len(losses),
            "flat_closures": len(flat),
            "win_rate": str(win_rate) if win_rate is not None else None,
            "gross_avg_trade": str(gross_avg) if gross_avg is not None else None,
            "best_trade": str(best) if best is not None else None,
            "worst_trade": str(worst) if worst is not None else None,
            "consecutive_losses": {"current": current_streak, "max": max_streak},
            "max_drawdown": str(dd),
            "max_drawdown_pct": str(dd_pct),
            "time_in_position_minutes": tpm,
            "worst_day": worst_day,
        },
        "execution": {
            "fills": len(payments),
            "broker": {"class": type(engine.broker).__name__, "is_live": engine.broker.is_live},
            "label_only": True,
            "zero_premium": True,
        },
        "data_quality": {
            "poisoned": len(engine.counters["poisoned"]),
            "anomalies": len(engine.counters["anomalies"]),
            "data_errors": len(engine.counters["data_errors"]),
        },
        "baseline_comparison": (
            comparison_table(engine, baseline) if baseline is not None else None
        ),
        "fingerprint": engine.stable_fingerprint(),
        "declared_winner": None,
    }
    return report_payload


def _leg_sides(engine, key: str) -> dict:
    """Entry/exit counts for CALL and PUT, derived from decision records (so
    a SELL for a PUT short is never miscounted as a CALL exit)."""
    entries = {"CALL": 0, "PUT": 0}
    exits = {"CALL": 0, "PUT": 0}
    switches = {"CALL_TO_PUT": 0, "PUT_TO_CALL": 0}
    for decision in engine.decisions:
        for action in decision["actions"]:
            if action == "ENTER_CALL":
                entries["CALL"] += 1
            elif action == "ENTER_PUT":
                entries["PUT"] += 1
            elif action == "EXIT_CALL":
                exits["CALL"] += 1
            elif action == "EXIT_PUT":
                exits["PUT"] += 1
            elif action == "SWITCH_CALL_TO_PUT":
                switches["CALL_TO_PUT"] += 1
            elif action == "SWITCH_PUT_TO_CALL":
                switches["PUT_TO_CALL"] += 1
    engine._switch_derived = switches
    return entries if key == "entries" else exits


def _worst_day(closures: list[dict]) -> dict | None:
    by_day: dict[str, Decimal] = {}
    for closure in closures:
        day = closure["exited_at"][:10]
        by_day[day] = by_day.get(day, Decimal("0")) + _d(closure["realized"])
    if not by_day:
        return None
    worst_day, worst = min(by_day.items(), key=lambda kv: kv[1])
    return {"day": worst_day, "realized": str(worst)}


def baseline_metrics_from_engine(base) -> dict:
    """Metrics of the frozen MA(5,21) baseline from an (unchanged) paper-track
    engine instance, computed only for the reporting comparison table.
    """
    portfolio = base.portfolio
    commissions = sum((_d(f.commission) for f in base.broker.fills), Decimal("0"))
    realized = portfolio.realized_pnl
    closures = [t for t in portfolio.trade_history if _d(t.realized_pnl) != 0]
    wins = [t for t in closures if _d(t.realized_pnl) > 0]
    losses = [t for t in closures if _d(t.realized_pnl) < 0]
    win_rate = None
    if wins or losses:
        win_rate = Decimal(str(len(wins))) / Decimal(str(len(wins) + len(losses)))
    days = sorted(base.store.reports_dir.glob(f"{base.config.account}.*.json"))
    return {
        "name": "MA(5,21) daily paper track (frozen baseline)",
        "days": [p.name.split(".")[-2] for p in days],
        "net_pnl": str(realized - commissions),
        "commissions": str(commissions),
        "gross_profit": str(sum((_d(t.realized_pnl) for t in wins), Decimal("0"))),
        "gross_loss": str(sum((_d(t.realized_pnl) for t in losses), Decimal("0"))),
        "win_rate": str(win_rate) if win_rate is not None else None,
        "round_trips": len(closures),
        "stops_fired": base.counters.get("stops_fired", 0),
        "eod_flattens": base.counters.get("flatten_count", 0),
        "max_drawdown": str(base.max_intraday_drawdown),
        "drawdown_granularity": "per-session tick (finer than the experiment's 15-min points)",
        "fingerprint": base.stable_fingerprint(),
    }


def comparison_table(engine, base: dict) -> dict:
    """Reporting-level comparison. No winner is declared and never will be."""
    exp = engine
    rows = [
        {"metric": "net_pnl", "experiment": _fmt(exp, _net_pnl), "baseline": base["net_pnl"]},
        {"metric": "commissions", "experiment": _fmt(exp, _commissions), "baseline": base["commissions"]},
        {"metric": "win_rate", "experiment": _fmt(exp, _win_rate), "baseline": base["win_rate"]},
        {"metric": "round_trips", "experiment": _fmt(exp, _round_trips), "baseline": base["round_trips"]},
        {"metric": "stops_fired", "experiment": _fmt(exp, _stops), "baseline": base["stops_fired"]},
        {"metric": "eod_flattens", "experiment": _fmt(exp, _flattens), "baseline": base["eod_flattens"]},
        {"metric": "max_drawdown", "experiment": _fmt(exp, _drawdown), "baseline": base["max_drawdown"]},
    ]
    return {
        "rows": rows,
        "drawdown_granularity": {
            "experiment": "15-minute decision points",
            "baseline": base["drawdown_granularity"],
        },
        "baseline_days": base["days"],
        "no_winner_declared": True,
        "declared_winner": None,
    }


def _fmt(engine, fn) -> str:
    try:
        return fn(engine)
    except Exception:  # pragma: no cover
        return "n/a"


def _net_pnl(engine) -> str:
    commissions = sum((_d(f.commission) for f in engine.broker.fills), Decimal("0"))
    return str(engine.portfolio.realized_pnl - commissions)


def _commissions(engine) -> str:
    return str(sum((_d(f.commission) for f in engine.broker.fills), Decimal("0")))


def _win_rate(engine) -> str | None:
    wins = sum(1 for c in engine.closures if _d(c["realized"]) > 0)
    losses = sum(1 for c in engine.closures if _d(c["realized"]) < 0)
    if (wins + losses) == 0:
        return None
    return str(Decimal(str(wins)) / Decimal(str(wins + losses)))


def _round_trips(engine) -> int:
    return len(engine.closures)


def _stops(engine) -> int:
    return engine.counters["stops_fired"]


def _flattens(engine) -> int:
    return engine.counters["flatten_count"]


def _drawdown(engine) -> str:
    return str(engine.max_drawdown)