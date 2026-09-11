"""Serialization and HTML rendering for evaluation runs (WS 7.4)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fno_ai_paper_trading.evaluation.records import EvaluationRun
from fno_ai_paper_trading.research.report import (
    CSS,
    card,
    escape,
    fmt,
    kv_rows,
    table,
)


def _default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_default(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _default(item) for key, item in value.items()}
    return value


def evaluation_run_to_dict(run: EvaluationRun) -> dict[str, Any]:
    """Deterministic, JSON-serializable representation of an evaluation run."""
    return {
        "name": run.name,
        "strategy_name": run.strategy_name,
        "strategy_params": dict(run.strategy_params),
        "baseline": run.baseline,
        "config": {
            "initial_capital": str(run.config.initial_capital),
            "quantity": run.config.quantity,
            "commission_rate": str(run.config.commission_rate),
            "commission_fixed": str(run.config.commission_fixed),
            "slippage_rate": str(run.config.slippage_rate),
            "enable_risk_manager": run.config.enable_risk_manager,
            "max_position_quantity": run.config.max_position_quantity,
            "max_order_notional": str(run.config.max_order_notional),
            "max_daily_loss": str(run.config.max_daily_loss),
            "stop_loss_pct": str(run.config.stop_loss_pct),
        },
        "created_at": run.created_at,
        "sessions": [
            {
                "dataset_name": s.dataset_name,
                "dataset_hash": s.dataset_hash,
                "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
                "bars_processed": s.bars_processed,
                "net_pnl": str(s.net_pnl),
                "net_return_pct": str(s.net_return_pct),
                "win_rate": str(s.win_rate),
                "num_trades": s.num_trades,
                "total_commission": str(s.total_commission),
                "slippage_cost": str(s.slippage_cost),
                "max_drawdown": str(s.max_drawdown),
                "max_drawdown_pct": str(s.max_drawdown_pct),
                "exposure_pct": str(s.exposure_pct),
            }
            for s in run.sessions
        ],
        "aggregate": {
            "sessions": run.aggregate.sessions,
            "bars_processed": run.aggregate.bars_processed,
            "num_trades": run.aggregate.num_trades,
            "winning_trades": run.aggregate.winning_trades,
            "losing_trades": run.aggregate.losing_trades,
            "win_rate": str(run.aggregate.win_rate),
            "total_pnl": str(run.aggregate.total_pnl),
            "total_return_pct": str(run.aggregate.total_return_pct),
            "transaction_costs": str(run.aggregate.transaction_costs),
            "max_drawdown": str(run.aggregate.max_drawdown),
            "max_drawdown_pct": str(run.aggregate.max_drawdown_pct),
            "avg_trade": str(run.aggregate.avg_trade) if run.aggregate.avg_trade is not None else None,
            "losing_streak_bars": run.aggregate.losing_streak_bars,
            "exposure_pct": str(run.aggregate.exposure_pct),
            "profit_factor": str(run.aggregate.profit_factor),
        },
    }


def evaluation_run_to_html(run: EvaluationRun) -> str:
    """Self-contained, labelled historical-evaluation HTML page."""
    aggregate = run.aggregate
    label = "FROZEN BASELINE" if run.baseline else "RESEARCH CANDIDATE (NOT PROMOTED)"
    disclaimer = (
        "Historical evaluation of the frozen MA(5,21) baseline on validated paper "
        "data. Evidence only — not a profit guarantee; paper trading only; no live orders."
        if run.baseline
        else "Research candidate evaluation on validated paper data. NOT adopted, NOT a "
        "profit guarantee; paper trading only; no live orders."
    )
    header = (
        f"<header><h1>{escape(run.name)} — {label}</h1>"
        f"<p>Strategy {escape(run.strategy_name)} &middot; {len(run.sessions)} "
        f"session(s) &middot; generated {escape(run.created_at)}</p></header>"
    )
    cards = "".join(
        [
            card(f"{fmt(aggregate.total_pnl)} ₹", "Total P&L", "good" if aggregate.total_pnl >= 0 else "bad"),
            card(f"{fmt(aggregate.total_return_pct, '%')}", "Return %"),
            card(f"{fmt(aggregate.win_rate, '%')}", "Win rate"),
            card(f"{aggregate.num_trades}", "Round trips"),
            card(f"{fmt(aggregate.max_drawdown_pct, '%')}", "Max drawdown %", "bad"),
            card(f"{fmt(aggregate.transaction_costs)} ₹", "Transaction costs"),
            card(f"{aggregate.sessions}", "Sessions"),
            card(f"{aggregate.bars_processed}", "Bars processed"),
        ]
    )
    summary = kv_rows(
        [
            ("Strategy", run.strategy_name),
            ("Baseline", str(run.baseline)),
            ("Sessions", str(aggregate.sessions)),
            ("Bars processed", str(aggregate.bars_processed)),
            ("Total P&L", f"{fmt(aggregate.total_pnl)} ₹"),
            ("Total return", f"{fmt(aggregate.total_return_pct, '%')}"),
            ("Round trips", str(aggregate.num_trades)),
            ("Winning / losing", f"{aggregate.winning_trades} / {aggregate.losing_trades}"),
            ("Win rate", f"{fmt(aggregate.win_rate, '%')}"),
            ("Average trade", f"{fmt(aggregate.avg_trade)} ₹" if aggregate.avg_trade is not None else "n/a"),
            ("Transaction costs", f"{fmt(aggregate.transaction_costs)} ₹"),
            ("Max drawdown", f"{fmt(aggregate.max_drawdown)} ₹"),
            ("Max drawdown %", f"{fmt(aggregate.max_drawdown_pct, '%')}"),
            ("Exposure", f"{fmt(aggregate.exposure_pct, '%')}"),
            ("Losing streak (bars)", str(aggregate.losing_streak_bars)),
            ("Profit factor", fmt(aggregate.profit_factor)),
            ("Initial capital", f"{fmt(run.config.initial_capital)} ₹"),
            ("Commission rate", fmt(run.config.commission_rate)),
            ("Slippage rate", fmt(run.config.slippage_rate)),
            ("Stop-loss %", fmt(run.config.stop_loss_pct, "%")),
        ]
    )
    rows = [
        [
            escape(s.dataset_name),
            s.start_date.isoformat() if s.start_date else "n/a",
            s.end_date.isoformat() if s.end_date else "n/a",
            str(s.bars_processed),
            f"{fmt(s.net_pnl)} ₹",
            f"{fmt(s.net_return_pct, '%')}",
            f"{fmt(s.win_rate, '%')}",
            str(s.num_trades),
            f"{fmt(s.max_drawdown_pct, '%')}",
        ]
        for s in run.sessions
    ]
    sessions_table = table(
        ["Dataset", "From", "To", "Bars", "Net P&L", "Return %", "Win rate", "Trades", "MaxDD %"],
        rows,
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
        f"<body><div class='wrap'>{header}<div class='note'>{escape(disclaimer)}</div>"
        f"<div class='cards'>{cards}</div>"
        f"<section><h2>Aggregate</h2>{summary}</section>"
        f"<section><h2>Sessions ({len(run.sessions)})</h2>{sessions_table}</section>"
        f"<div class='footer'>Deterministic historical evaluation &middot; paper trading only &middot; no live orders</div>"
        f"</div></body></html>"
    )