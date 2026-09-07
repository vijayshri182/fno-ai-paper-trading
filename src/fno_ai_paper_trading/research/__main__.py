"""Offline strategy-research demo.

``python -m fno_ai_paper_trading.research``

Runs representative research examples over deterministic synthetic regimes with
an illustrative Indian cost schedule and execution assumptions, and prints a
compact summary. Everything is simulated — no network, no credentials, no live
orders. Output is evidence of mechanics, not of market profitability.
"""
from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.research.benchmark import buy_and_hold
from fno_ai_paper_trading.research.costs import IndiaCostSchedule
from fno_ai_paper_trading.research.execution import ExecutionAssumptions
from fno_ai_paper_trading.research.experiment import run_experiment
from fno_ai_paper_trading.research.regimes import (
    build_sideways_choppy,
    build_sustained_uptrend,
    build_trend_reversal,
    build_volatile_market,
)
from fno_ai_paper_trading.research.sensitivity import run_parameter_sensitivity
from fno_ai_paper_trading.research.split import split_bars
from fno_ai_paper_trading.research.walkforward import run_walk_forward
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy
from fno_ai_paper_trading.data.mock_provider import build_sample_instruments

COSTS = IndiaCostSchedule.nse_fo_illustrative()
EXECUTION = ExecutionAssumptions(slippage_rate=Decimal("0.0005"), half_spread_rate=Decimal("0.0002"), impact_rate=Decimal("0.0003"))


def _config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=Decimal("250000"),
        quantity=5,
        cost_schedule=COSTS,
        execution=EXECUTION,
    )


def _line(label: str, value: str) -> str:
    return f"{label:<28}: {value}"


def main() -> None:
    instrument = build_sample_instruments()[0]
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)

    print("F&O AI Paper Trading — Strategy Research Demo")
    print("(synthetic deterministic data; illustrative cost schedule; paper-only)")

    reversal = build_trend_reversal(instrument, bars=240)
    uptrend = build_sustained_uptrend(instrument, bars=240)
    choppy = build_sideways_choppy(instrument, bars=120)
    volatile = build_volatile_market(instrument, bars=240)

    exp = run_experiment(
        reversal,
        strategy,
        _config(),
        name="ma_cross_trend_reversal",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="trend_reversal",
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=5,
    )
    print("")
    print("[Experiment 1] MA(5,21) on trend-reversal (240 bars, one round trip)")
    print(_line("net P&L", f"{exp.metrics.net_pnl}"))
    print(_line("net return", f"{exp.metrics.net_return_pct}%"))
    print(_line("max drawdown", f"{exp.metrics.max_drawdown} ({exp.metrics.max_drawdown_pct}%)"))
    print(_line("closed trades", f"{exp.metrics.num_trades} (win {exp.metrics.winning_trades}/{exp.metrics.losing_trades})"))
    print(_line("expectancy", f"{exp.metrics.expectancy}"))
    print(_line("Sharpe / Sortino", f"{exp.metrics.sharpe_ratio} / {exp.metrics.sortino_ratio}"))
    print(_line("total commission", f"{exp.metrics.total_commission}"))
    if exp.benchmark is not None:
        print(_line("benchmark return (gross)", f"{exp.benchmark.total_return_pct}%"))

    split = split_bars(volatile)  # 60/20/20 train/validation/out-of-sample
    oos_exp = run_experiment(
        split.test,
        strategy,
        _config(),
        name="ma_cross_volatile_oos",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="volatile_market.out_of_sample",
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=None,
    )
    print("")
    print(f"[Out-of-sample] test segment {len(split.test)} bars (clearly separated from train/valid data)")
    print(_line("OOS net P&L", f"{oos_exp.metrics.net_pnl}"))
    print(_line("OOS return", f"{oos_exp.metrics.net_return_pct}%"))
    print(_line("OOS trades", f"{oos_exp.metrics.num_trades}"))

    sens = run_parameter_sensitivity(choppy, _config(), [5, 10, 20], [21, 50])
    print("")
    print(f"[Parameter sensitivity] MA on sideways/choppy (120 bars), {sens.num_runs} combos")
    for row in sens.rows:
        print(_line(f"fast={row.fast} slow={row.slow}", f"return {row.total_return_pct}% | trades {row.num_trades} | DD {row.max_drawdown_pct}%"))
    if sens.skipped:
        print(_line("skipped", ", ".join(f"({s.fast},{s.slow})" for s in sens.skipped)))

    wf = run_walk_forward(
        volatile,
        lambda train: MovingAverageCrossStrategy(fast=5, slow=21),
        _config(),
        train_size=120,
        test_size=60,
        step=60,
    )
    print("")
    print(f"[Walk-forward] volatile market, {wf.total_oos_bars} OOS bars across {len(wf.steps)} windows")
    print(_line("OOS trades", f"{wf.total_trades} (win {wf.total_wins})"))
    print(_line("combined return", f"{wf.combined_return_pct}%"))
    print(_line("gross profit / loss", f"{wf.gross_profit} / {wf.gross_loss}"))

    bh_rev = buy_and_hold(reversal, Decimal("250000"), 5)
    bh_up = buy_and_hold(uptrend, Decimal("250000"), 5)
    print("")
    print("[Benchmark] buy-and-hold (gross of costs, same 240-bar windows)")
    print(_line("reversal buy-and-hold", f"{bh_rev.total_return_pct}% (DD {bh_rev.max_drawdown_pct}%)"))
    print(_line("uptrend buy-and-hold", f"{bh_up.total_return_pct}% (DD {bh_up.max_drawdown_pct}%)"))

    print("")
    print("Research demo complete - simulated paper execution only.")


if __name__ == "__main__":
    main()