"""Generate a labelled HTML research report from deterministic synthetic data.

Usage::

    python scripts/generate_research_report.py

Writes ``reports/research_report.html`` (the ``reports/`` folder is gitignored —
this file exists to be shared/emailed, not committed). Everything is computed
live from the research framework with an illustrative cost schedule; the report
is evidence of the framework's mechanics on synthetic regimes, never a claim
of market profitability.
"""
from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# Make ``src/`` importable when run directly (keeps the script runnable from any CWD).
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.data.mock_provider import build_sample_instruments
from fno_ai_paper_trading.models.enums import OrderSide
from fno_ai_paper_trading.research.benchmark import buy_and_hold
from fno_ai_paper_trading.research.costs import IndiaCostSchedule
from fno_ai_paper_trading.research.experiment import run_experiment
from fno_ai_paper_trading.research.execution import ExecutionAssumptions
from fno_ai_paper_trading.research.regimes import (
    REGIME_BUILDERS,
    build_sideways_choppy,
    build_sustained_uptrend,
    build_trend_reversal,
    build_volatile_market,
    regime_stats,
)
from fno_ai_paper_trading.research.report import (
    build_research_html,
    fmt,
    kv_rows,
    metrics_cards,
    table,
)
from fno_ai_paper_trading.research.sensitivity import run_parameter_sensitivity
from fno_ai_paper_trading.research.split import split_bars
from fno_ai_paper_trading.research.walkforward import run_walk_forward
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

COSTS = IndiaCostSchedule.nse_fo_illustrative()
EXECUTION = ExecutionAssumptions(
    slippage_rate=Decimal("0.0005"),
    half_spread_rate=Decimal("0.0002"),
    impact_rate=Decimal("0.0003"),
)
DISCLAIMER = (
    "Historical / synthetic evidence only. All datasets are deterministic "
    "synthetic price paths and the cost schedule is illustrative (see the cost "
    "section). Nothing here is investment advice and no market profitability is "
    "claimed. Replace the illustrative fees with your broker's actual schedule "
    "before drawing any conclusion."
)


def _config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=Decimal("250000"),
        quantity=5,
        cost_schedule=COSTS,
        execution=EXECUTION,
    )


def section_cost_model(blocks: list[str]) -> None:
    rows_se = COSTS.compute(OrderSide.SELL, Decimal("100000"), 5)
    rows_bu = COSTS.compute(OrderSide.BUY, Decimal("100000"), 5)
    blocks.append(
        f"""<section><h2>Cost model (illustrative)</h2>
<p style="font-size:13px;color:#57606a">Charges are computed with exact Decimal
arithmetic, per fill, without rounding. GST is charged on the taxable base
(brokerage + exchange + SEBI). STT applies on the sell side only; stamp duty on
the buy side only. Values below use the documented illustrative schedule.</p>
{table(
    ["Component", "BUY @ 1,00,000 notional", "SELL @ 1,00,000 notional"],
    [
        ["Brokerage", fmt(rows_bu.brokerage), fmt(rows_se.brokerage)],
        ["STT", fmt(rows_bu.stt), fmt(rows_se.stt)],
        ["Exchange charges", fmt(rows_bu.exchange_charges), fmt(rows_se.exchange_charges)],
        ["SEBI", fmt(rows_bu.sebi_charges), fmt(rows_se.sebi_charges)],
        ["Stamp duty", fmt(rows_bu.stamp_duty), fmt(rows_se.stamp_duty)],
        ["GST @ 18%", fmt(rows_bu.gst), fmt(rows_se.gst)],
        ["Other", "0", "0"],
        ["<strong>Total</strong>", fmt(rows_bu.total), fmt(rows_se.total)],
    ]
)}</section>"""
    )


def section_execution(blocks: list[str]) -> None:
    blocks.append(
        f"""<section><h2>Execution assumptions</h2>
<p style="font-size:13px;color:#57606a">Each fill is assumed to suffer a single
combined adverse-price cost. The default is indicative only.</p>
{kv_rows([
    ("Slippage rate", fmt(EXECUTION.slippage_rate, "%")),
    ("Half-spread assumption", fmt(EXECUTION.half_spread_rate, "%")),
    ("Market-impact assumption", fmt(EXECUTION.impact_rate, "%")),
    ("Total adverse rate applied per fill", fmt(EXECUTION.total_adverse_rate, "%")),
])}</section>"""
    )


def section_regimes(blocks: list[str]) -> None:
    instrument = build_sample_instruments()[0]
    rows = []
    for name, builder in sorted(REGIME_BUILDERS.items()):
        bars = builder(instrument)
        stats = regime_stats(bars)
        rows.append(
            [
                name,
                str(len(bars)),
                fmt(stats["first"]),
                fmt(stats["last"]),
                fmt(stats["min"]),
                fmt(stats["max"]),
            ]
        )
    blocks.append(
        f"""<section><h2>Synthetic market regimes (deterministic)</h2>
<p style="font-size:13px;color:#57606a">Six hand-shaped, deterministic price
paths used across this report — fully reproducible, no floating point, no
randomness. Shape descriptions live in the source docstrings.</p>
{table(["Regime", "Bars", "First", "Last", "Min", "Max"], rows)}</section>"""
    )


def section_experiments(blocks: list[str]) -> None:
    instrument = build_sample_instruments()[0]
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)

    cases = [
        ("MA(5,21) on trend-reversal", build_trend_reversal(instrument, bars=240), "trend_reversal"),
        ("MA(5,21) on sustained uptrend", build_sustained_uptrend(instrument, bars=240), "sustained_uptrend"),
        ("MA(5,21) on volatile market", build_volatile_market(instrument, bars=240), "volatile_market"),
        ("MA(5,21) on sideways/choppy", build_sideways_choppy(instrument, bars=120), "sideways_choppy"),
    ]
    html = ""
    for title, bars, dataset in cases:
        exp = run_experiment(
            bars,
            strategy,
            _config(),
            name=f"ma_cross_{dataset}",
            strategy_params={"fast": 5, "slow": 21},
            dataset_name=dataset,
            cost_assumptions="nse_fo_illustrative",
            slippage_assumptions="execution total_adverse_rate=0.001",
            benchmark_quantity=5,
        )
        m = exp.metrics
        html += (
            f"""<section><h2>{title}</h2>"""
            f"""<div class="cards">{metrics_cards(m)}</div>"""
            f"""<p style="font-size:12px;color:#57606a">"""
            f"""Config hash <code>{exp.config_hash}</code> &middot; """
            f"""bars {m.total_bars} &middot; exposure {fmt(m.exposure_pct, "%")} &middot; """
            f"""win rate {fmt(m.win_rate, "%")} &middot; profit factor {fmt(m.profit_factor)}"""
            f"""</p>"""
            f"""</section>"""
        )
    blocks.append(html)


def section_in_sample_out_of_sample(blocks: list[str]) -> None:
    instrument = build_sample_instruments()[0]
    strategy = MovingAverageCrossStrategy(fast=5, slow=21)
    bars = build_volatile_market(instrument, bars=240)
    split = split_bars(bars)  # 60/20/20

    kw = dict(
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="volatile_market.train_validation",
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=None,
    )
    train = run_experiment(split.train + split.validation, strategy, _config(), name="volatile_is", **kw)
    oos = run_experiment(
        split.test,
        strategy,
        _config(),
        name="volatile_oos",
        strategy_params={"fast": 5, "slow": 21},
        dataset_name="volatile_market.out_of_sample",
        cost_assumptions="nse_fo_illustrative",
        slippage_assumptions="execution total_adverse_rate=0.001",
        benchmark_quantity=None,
    )
    blocks.append(
        f"""<section><h2>In-sample vs out-of-sample (60/20/20 split)</h2>
<p style="font-size:13px;color:#57606a">The test segment is strictly disjoint and
contiguous; the strategy is re-instantiated on the OOS bars so no training
information leaks into the reported segment.</p>
{table(
    ["Segment", "Bars", "Net P&L", "Return", "Trades", "Max DD %"],
    [
        ["In-sample (train+validation)", str(len(split.train) + len(split.validation)),
         fmt(train.metrics.net_pnl), fmt(train.metrics.net_return_pct, "%"),
         str(train.metrics.num_trades), fmt(train.metrics.max_drawdown_pct, "%")],
        ["Out-of-sample (test)", str(len(split.test)),
         fmt(oos.metrics.net_pnl), fmt(oos.metrics.net_return_pct, "%"),
         str(oos.metrics.num_trades), fmt(oos.metrics.max_drawdown_pct, "%")],
    ]
)}</section>"""
    )


def section_walk_forward(blocks: list[str]) -> None:
    instrument = build_sample_instruments()[0]
    bars = build_volatile_market(instrument, bars=240)
    wf = run_walk_forward(
        bars,
        lambda train: MovingAverageCrossStrategy(fast=5, slow=21),
        _config(),
        train_size=120,
        test_size=60,
        step=60,
    )
    rows = []
    for step, step_result in zip(wf.steps, wf.per_step):
        rows.append(
            [
                str(step.index + 1),
                f"[{step.train_start}:{step.train_end}]",
                f"[{step.test_start}:{step.test_end}]",
                str(step_result.num_trades),
                fmt(step_result.total_return_pct, "%"),
            ]
        )
    blocks.append(
        f"""<section><h2>Walk-forward evaluation (rolling train/test)</h2>
<p style="font-size:13px;color:#57606a">Two hours of computation are avoided
entirely here — the point is the structure: strategy fit on each train window is
evaluated only on the immediately following contiguous test window. Combined
return below compounds the per-window returns.</p>
{table(
    ["Window", "Train (bars)", "Test (bars)", "OOS trades", "OOS return"],
    rows,
)}
{kv_rows([
    ("Total OOS bars", str(wf.total_oos_bars)),
    ("Total OOS trades", f"{wf.total_trades} (win {wf.total_wins})"),
    ("Combined OOS return", fmt(wf.combined_return_pct, "%")),
])}</section>"""
    )


def section_sensitivity(blocks: list[str]) -> None:
    instrument = build_sample_instruments()[0]
    bars = build_sideways_choppy(instrument, bars=120)
    sens = run_parameter_sensitivity(bars, _config(), [5, 10, 20], [21, 50])
    rows = []
    for r in sens.rows:
        rows.append(
            [
                f"{r.fast} / {r.slow}",
                fmt(r.total_return_pct, "%"),
                str(r.num_trades),
                fmt(r.win_rate, "%"),
                fmt(r.max_drawdown_pct, "%"),
                fmt(r.net_pnl),
            ]
        )
    skipped = ", ".join(f"({s.fast},{s.slow})" for s in sens.skipped)
    blocks.append(
        f"""<section><h2>Parameter sensitivity (explicitly enumerated, not optimized)</h2>
<p style="font-size:13px;color:#57606a">These are manual combinations, never a
grid search: the question is whether the parameter set is fragile, not which
parameters maximize return. Fast/slow sets that are geometrically impossible
(fast &ge; slow) are skipped and reported.</p>
{table(
    ["(fast, slow)", "Return", "Trades", "Win %", "Max DD %", "Net P&L"],
    rows,
)}
<p style="font-size:12px;color:#57606a">Skipped (invalid): {skipped or "none"}</p></section>"""
    )


def section_benchmarks(blocks: list[str]) -> None:
    instrument = build_sample_instruments()[0]
    rows = []
    for name, bars in [
        ("Trend-reversal", build_trend_reversal(instrument, bars=240)),
        ("Uptrend", build_sustained_uptrend(instrument, bars=240)),
        ("Volatile", build_volatile_market(instrument, bars=240)),
    ]:
        bh = buy_and_hold(bars, Decimal("250000"), 5)
        exp = run_experiment(
            bars,
            MovingAverageCrossStrategy(fast=5, slow=21),
            _config(),
            name=f"ma_cross_benchmark_{name.lower()}",
            strategy_params={"fast": 5, "slow": 21},
            dataset_name=name.lower(),
            cost_assumptions="nse_fo_illustrative",
            slippage_assumptions="execution total_adverse_rate=0.001",
            benchmark_quantity=None,
        )
        rows.append(
            [
                name,
                fmt(bh.total_return_pct, "%"),
                fmt(bh.max_drawdown_pct, "%"),
                fmt(exp.metrics.net_return_pct, "%"),
                fmt(exp.metrics.max_drawdown_pct, "%"),
            ]
        )
    blocks.append(
        f"""<section><h2>Benchmark comparison (MA strategy vs buy-and-hold)</h2>
<p style="font-size:13px;color:#57606a">Buy-and-hold is gross of costs and
always 100% exposed; the MA strategy is net of the illustrative costs. Like-for-
like comparison on identical windows.</p>
{table(
    ["Regime", "BH return %", "BH max DD %", "MA net return %", "MA net max DD %"],
    rows,
)}</section>"""
    )


def main() -> None:
    blocks: list[str] = []
    section_cost_model(blocks)
    section_execution(blocks)
    section_regimes(blocks)
    section_experiments(blocks)
    section_in_sample_out_of_sample(blocks)
    section_walk_forward(blocks)
    section_sensitivity(blocks)
    section_benchmarks(blocks)

    html = build_research_html(
        title="Strategy Research & Robustness Notebook",
        blocks=blocks,
        disclaimer=DISCLAIMER,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    out = Path(__file__).resolve().parents[1] / "reports" / "research_report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out} ({len(html)} bytes)")


if __name__ == "__main__":
    main()