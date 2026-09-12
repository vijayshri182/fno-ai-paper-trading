"""Generate the champion model-performance report and its artifacts.

Steps
-----
1. Load and audit the real 5m and 1d NIFTY datasets (coverage + validation).
2. Replay the frozen champion (MA fast=5, slow=21) ONE continuous session over
   the full 5m series with the product's paper execution/risk assumptions.
3. Derive benchmark (buy-and-hold + index price return), yearly/monthly
   realized P&L, decision-time regime breakdown, train/val/OOS segments,
   walk-forward, and bounded cost scenarios.
4. Write reports/model_performance/{summary.json, trades.csv, yearly.csv,
   monthly.csv, regime.csv, equity_curve.csv, model_performance.html}.
5. Print a short console summary.

PAPER ONLY: everything flows through BacktestEngine / PaperBroker / Portfolio /
RiskManager — no live order route exists or is touched.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.coverage import build_coverage_report
from fno_ai_paper_trading.evaluation.model_performance import (
    aggregate_by_period,
    aggregate_by_regime,
    bars_per_year_for,
    benchmark_over,
    cost_sensitivity,
    evaluate_continuous,
    evaluate_splits,
    index_price_return,
    period_equities,
    walk_forward,
)
from fno_ai_paper_trading.evaluation.perf_reports import (
    build_report_html,
    write_artifacts,
)

DATASET_5M = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
DATASET_1D = ROOT / "datasets" / "upstox_Nifty_50_1d_20050103_20260911.csv"
OUTPUT_DIR = ROOT / "reports" / "model_performance"


def _bench_dict(result) -> dict[str, str]:
    return {
        field: str(getattr(result, field))
        for field in (
            "name",
            "quantity",
            "entry_price",
            "final_price",
            "final_equity",
            "total_return_pct",
            "max_drawdown",
            "max_drawdown_pct",
            "volatility_pct",
        )
    }


def _config_dict(config: BacktestConfig) -> dict[str, str]:
    return {
        "initial_capital": str(config.initial_capital),
        "quantity": str(config.quantity),
        "commission_rate": str(config.commission_rate),
        "commission_fixed": str(config.commission_fixed),
        "slippage_rate": str(config.slippage_rate),
        "enable_risk_manager": str(config.enable_risk_manager),
        "max_position_quantity": str(config.max_position_quantity),
        "max_order_notional": str(config.max_order_notional),
        "max_daily_loss": str(config.max_daily_loss),
        "enable_stop_loss": str(config.enable_stop_loss),
        "stop_loss_pct": str(config.stop_loss_pct),
    }


def _filter_since(bars, start_text: str) -> list:
    start = datetime.fromisoformat(start_text)
    return [bar for bar in bars if bar.timestamp >= start]


def main() -> None:
    if not DATASET_5M.exists() or not DATASET_1D.exists():
        raise SystemExit(
            "missing acquired datasets; run scripts/acquire_model_performance_data.py first"
        )

    config = BacktestConfig()

    dataset_5m = load_dataset(DATASET_5M)
    dataset_1d = load_dataset(DATASET_1D)
    coverage_5m = build_coverage_report(dataset_5m, interval_minutes=5, expected_bars_per_day=75)
    coverage_1d = build_coverage_report(dataset_1d, interval_minutes=1440, expected_bars_per_day=1)

    bars_5m = dataset_5m.bars
    period_start = bars_5m[0].timestamp.isoformat()
    period_end = bars_5m[-1].timestamp.isoformat()
    bars_1d_period = _filter_since(dataset_1d.bars, period_start)

    run = evaluate_continuous(bars_5m, config=config)
    years = aggregate_by_period(run.round_trips, granularity="year")
    months = aggregate_by_period(run.round_trips, granularity="month")
    regimes = aggregate_by_regime(run.round_trips)
    end_equities = {row["period"]: row["end_equity"] for row in period_equities(run.result.equity_curve, "year")}

    ben_5m = benchmark_over(bars_5m, config.initial_capital, name="nifty50_buy_and_hold_5m")
    price_5m = index_price_return(bars_5m)
    ben_1d = benchmark_over(bars_1d_period, config.initial_capital, name="nifty50_buy_and_hold_1d_same_period")
    price_1d = index_price_return(bars_1d_period)

    splits = evaluate_splits(bars_5m, config=config)
    wf = walk_forward(bars_5m, train_size=22000, test_size=4400, config=config)
    costs = cost_sensitivity(bars_5m, base_config=config)

    notes = [
        "Real 5m history for this instrument starts 2022-01-03; the replay period "
        f"({period_start} .. {period_end}) is ~4.7 years, not five full years. Earlier "
        "five-year evidence used synthetic smoke data.",
        "Neither broker (backtest or live paper) models a cash/margin floor, so buys are "
        "filled regardless of available cash. The negative equity reached during this "
        "replay (min equity below zero) is an implicit-leverage artifact of the paper "
        "model, not a claim about real broker behaviour.",
        "Candidate missing weekday days are an upper bound: the repo market calendar only "
        "encodes the current year's NSE holidays, so older official holidays count as gaps.",
        "The champion (MA(5,21)) is frozen with no data-fitted parameters; the split and "
        "walk-forward sections are stability audits across time under identical "
        "assumptions, not evidence of parameter fitting.",
        "Benchmark rows are gross (no costs); champion rows are net of the paper cost "
        "model (commission + slippage + stop-loss).",
        "The buy-and-hold benchmark invests the largest fully-funded lot "
        f"({_bench_dict(ben_5m)['quantity']} units) so it holds most of the capital; the "
        "champion trades 1 unit at a time, so exposure is fundamentally different.",
        f"Champion gross trading P&L before friction was {Decimal(run.result.total_pnl) + run.metrics.transaction_costs:.2f} "
        "over the period; friction (transaction_costs) erased it — see the cost-sensitivity table.",
    ]

    coverage_5m_dict = coverage_5m.to_dict()
    coverage_1d_dict = coverage_1d.to_dict()

    yearly_rows = []
    for row in years:
        period = str(row["period"])
        yearly_rows.append(
            [
                period,
                str(row["num_trades"]),
                str(row["winning"]),
                str(row["losing"]),
                str(row["net_pnl"]),
                str(row["avg_net_pnl"]),
                end_equities.get(period, ""),
            ]
        )
    monthly_rows = [
        [
            str(row["period"]),
            str(row["num_trades"]),
            str(row["winning"]),
            str(row["losing"]),
            str(row["net_pnl"]),
            str(row["avg_net_pnl"]),
        ]
        for row in months
    ]
    regime_rows = [
        [
            str(row["regime"]),
            str(row["num_trades"]),
            str(row["winning"]),
            str(row["losing"]),
            str(row["win_rate_pct"]),
            str(row["net_pnl"]),
            str(row["avg_net_pnl"]),
        ]
        for row in regimes
    ]
    split_rows = [
        [
            str(row["segment"]),
            f"{row.get('start', '')} .. {row.get('end', '')}",
            str(row["bars"]),
            str(row.get("net_return_pct", "n/a")),
            str(row.get("num_trades", "")),
            str(row.get("win_rate_pct", "")),
            str(row.get("max_drawdown_pct", "")),
            str(row.get("transaction_costs", "")),
        ]
        for row in splits
    ]
    walkforward_rows = [
        [
            str(row["window"]),
            str(row["train_range"]),
            str(row["test_range"]),
            str(row["test_bars"]),
            str(row["num_trades"]),
            str(row["net_return_pct"]),
            str(row["max_drawdown_pct"]),
        ]
        for row in wf["steps"]
    ]
    cost_rows = [
        [
            str(row["scenario"]),
            str(row["commission_rate"]),
            str(row["slippage_rate"]),
            str(row["num_trades"]),
            str(row["net_return_pct"]),
            str(row["transaction_costs"]),
            str(row["max_drawdown_pct"]),
        ]
        for row in costs
    ]

    trade_rows = [
        [
            str(trip.index),
            trip.entry_trade.executed_at.isoformat(),
            trip.exit_trade.executed_at.isoformat(),
            trip.entry_trade.side.value,
            str(trip.entry_trade.quantity),
            str(trip.entry_trade.price),
            str(trip.exit_trade.price),
            str(trip.entry_regime or ""),
            str(trip.exit_regime or ""),
            str(trip.holding_bars),
            str(trip.price_pnl),
            str(trip.commission),
            str(trip.net_pnl),
        ]
        for trip in run.round_trips
    ]
    equity_rows = [
        [
            point.timestamp.isoformat(),
            str(point.bar_index),
            str(point.equity),
            str(point.cash),
            str(point.unrealized_pnl),
            str(point.drawdown_from_peak),
        ]
        for point in run.result.equity_curve
    ]

    benchmark_rows = [
        ["Net return %", str(run.metrics.net_return_pct), _bench_dict(ben_5m)["total_return_pct"], _bench_dict(ben_1d)["total_return_pct"]],
        ["Final equity", str(run.metrics.final_equity), _bench_dict(ben_5m)["final_equity"], _bench_dict(ben_1d)["final_equity"]],
        ["Max drawdown %", str(run.metrics.max_drawdown_pct), _bench_dict(ben_5m)["max_drawdown_pct"], _bench_dict(ben_1d)["max_drawdown_pct"]],
        ["Index price return % (gross)", price_5m["return_pct"], price_5m["return_pct"], price_1d["return_pct"]],
    ]

    summary = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        + "Z",
        "schema_version": "1",
        "deliverable": "model_performance_report",
        "title": "Champion MA(5,21) model-performance report (real NIFTY 50 data)",
        "strategy": {"name": run.strategy_name, "fast": 5, "slow": 21},
        "config": _config_dict(config),
        "data": {
            "bars_per_year": run.bars_per_year,
            "period": {"start": period_start, "end": period_end},
            "5m_coverage": coverage_5m_dict,
            "1d_coverage": coverage_1d_dict,
        },
        "continuous": run.summary_dict(),
        "benchmark": {
            "5m_buy_and_hold": _bench_dict(ben_5m),
            "5m_index_price_return": price_5m,
            "1d_same_period_buy_and_hold": _bench_dict(ben_1d),
            "1d_same_period_index_price_return": price_1d,
        },
        "periods": {"yearly": years, "monthly": months},
        "regimes": regimes,
        "splits": splits,
        "walkforward": wf,
        "cost_sensitivity": costs,
        "notes": notes,
    }

    html = build_report_html(
        title=summary["title"],
        generated_at=summary["generated_at"],
        summary=summary,
        coverage=coverage_5m_dict,
        benchmark_rows=benchmark_rows,
        yearly_rows=yearly_rows,
        monthly_rows=monthly_rows,
        regime_rows=regime_rows,
        split_rows=split_rows,
        walkforward_rows=walkforward_rows,
        cost_rows=cost_rows,
        config_pairs=list(_config_dict(config).items()),
        equity_curve_points=run.result.equity_curve,
        notes=notes,
    )

    written = write_artifacts(
        OUTPUT_DIR,
        summary=summary,
        trade_rows=trade_rows,
        yearly_rows=yearly_rows,
        monthly_rows=monthly_rows,
        regime_rows=regime_rows,
        equity_rows=equity_rows,
        html=html,
    )

    m = run.metrics
    print(f"champion        : {run.strategy_name} (fast=5, slow=21)")
    print(f"period          : {period_start} .. {period_end}")
    print(f"bars            : {len(bars_5m)} (5m) / {bars_per_year_for(bars_5m)} bars-per-year metrics cadence")
    print(f"net return      : {m.net_return_pct}% (net of costs)  [+{ben_1d.total_return_pct}% index buy-and-hold, same period]")
    print(f"final equity    : {m.final_equity}   (min {run.min_equity})")
    print(f"max drawdown    : {m.max_drawdown_pct}%")
    print(f"trades          : {m.num_trades}  win {m.winning_trades}/{m.losing_trades}  ({m.win_rate}%)")
    print(f"friction paid   : {m.transaction_costs}  (commission {m.total_commission} + slippage {m.slippage_cost})")
    print(f"reconciliation  : {run.reconciliation_ok}")
    print(f"artifacts       : {OUTPUT_DIR}")
    for key, path in written.items():
        print(f"  - {key}: {path}")


if __name__ == "__main__":
    main()