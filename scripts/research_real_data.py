"""Run a reproducible real-data research study on NIFTY 50 daily bars.

Usage (requires UPSTOX_ACCESS_TOKEN in env or via --token)::

    python scripts/research_real_data.py

For an offline smoke test that validates the pipeline with deterministic
synthetic data::

    python scripts/research_real_data.py --smoke

The script is read-only: it fetches historical OHLCV candles and never places an
order. The acquired dataset is stored under ``datasets/`` (git-ignored) with a
SHA-256 hash. The HTML report is written under ``reports/`` (git-ignored).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Make ``src/`` importable when run directly.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fno_ai_paper_trading.data.dataset_store import (  # noqa: E402
    StoredDataset,
    dataset_hash,
    save_dataset,
)
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider  # noqa: E402
from fno_ai_paper_trading.data.validation import format_report, validate_bars  # noqa: E402
from fno_ai_paper_trading.models.market import MarketPrice  # noqa: E402
from fno_ai_paper_trading.research.real_data import (  # noqa: E402
    DEFAULT_END_DATE,
    DEFAULT_START_DATE,
    RealDataResearchResult,
    run_real_data_research,
)
from fno_ai_paper_trading.research.report import (  # noqa: E402
    build_research_html,
    card,
    fmt,
    kv_rows,
    metrics_cards,
    table,
)
from fno_ai_paper_trading.research.regimes import build_trend_reversal  # noqa: E402

DEFAULT_CAPITAL = Decimal("250000")
DEFAULT_QUANTITY = 5


def _error(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _build_synthetic_dataset(directory: Path) -> StoredDataset:
    """Create a deterministic synthetic stand-in dataset for offline validation.

    This is **not** real market data and the generated report is labelled as such.
    """
    instrument = get_research_instrument("NIFTY 50")
    bars = build_trend_reversal(instrument, bars=240)
    return save_dataset(
        bars,
        instrument=instrument,
        provider="in_memory_smoke",
        interval="1d",
        directory=directory,
        name="nifty50_smoke",
    )


def _fetch_real_dataset(
    token: str,
    start: date,
    end: date,
    directory: Path,
    name: str | None,
) -> StoredDataset:
    instrument = get_research_instrument("NIFTY 50")
    provider = UpstoxHistoricalDataProvider(access_token=token)
    bars = provider.get_historical_ohlcv(
        instrument,
        interval="1d",
        start=datetime(start.year, start.month, start.day),
        end=datetime(end.year, end.month, end.day),
    )
    report = validate_bars(bars, allow_empty=False)
    if not report.ok:
        raise ValueError(f"downloaded dataset failed validation: {format_report(report)}")
    return save_dataset(
        bars,
        instrument=instrument,
        provider="upstox",
        interval="1d",
        directory=directory,
        name=name,
    )


def _metric_rows(result: RealDataResearchResult) -> list[list[str]]:
    m_full = result.full_experiment.metrics
    m_is = result.is_experiment.metrics
    m_oos = result.oos_experiment.metrics
    rows = [
        ["Full period", fmt(m_full.net_return_pct, "%"), str(m_full.num_trades), fmt(m_full.max_drawdown_pct, "%")],
        ["In-sample", fmt(m_is.net_return_pct, "%"), str(m_is.num_trades), fmt(m_is.max_drawdown_pct, "%")],
        ["Out-of-sample", fmt(m_oos.net_return_pct, "%"), str(m_oos.num_trades), fmt(m_oos.max_drawdown_pct, "%")],
    ]
    if result.full_experiment.benchmark is not None:
        bh = result.full_experiment.benchmark
        rows.append([
            "Buy & hold (gross)",
            fmt(bh.total_return_pct, "%"),
            "1",
            fmt(bh.max_drawdown_pct, "%"),
        ])
    return rows


def _build_html_report(result: RealDataResearchResult, *, title: str, disclaimer: str, provider: str) -> str:
    blocks: list[str] = []

    # Dataset section
    stats = result.stats
    blocks.append(
        f"""<section><h2>Dataset</h2>
        {kv_rows([
            ("Provider", provider),
            ("Instrument", "NIFTY 50"),
            ("Interval", "1d"),
            ("Start", str(stats.start)),
            ("End", str(stats.end)),
            ("Observations", str(stats.observations)),
            ("Data hash", result.dataset_hash),
            ("Validation", "OK" if result.validation["ok"] else f"{result.validation['num_errors']} error(s)"),
            ("Duplicate timestamps", str(stats.duplicate_timestamps)),
            ("Invalid OHLC bars", str(stats.invalid_ohlc_bars)),
            ("Missing calendar dates", str(stats.missing_calendar_dates)),
            ("Close range", f"{fmt(stats.close_min)} - {fmt(stats.close_max)}"),
            ("Avg volume", fmt(stats.avg_volume)),
            ("Avg daily return", fmt(stats.avg_daily_return_pct, "%")),
            ("Daily return vol", fmt(stats.daily_return_volatility_pct, "%")),
            ("Largest abs daily move", fmt(stats.largest_daily_return_abs_pct, "%")),
        ])}</section>"""
    )

    # Strategy / assumptions
    blocks.append(
        f"""<section><h2>Strategy & assumptions</h2>
        {kv_rows([
            ("Strategy", "Moving Average Crossover"),
            ("Fast period", str(result.strategy_params["fast"])),
            ("Slow period", str(result.strategy_params["slow"])),
            ("Initial capital", fmt(result.initial_capital)),
            ("Quantity per signal", str(result.quantity)),
            ("Cost assumptions", result.cost_assumptions),
            ("Slippage assumptions", result.slippage_assumptions),
        ])}</section>"""
    )

    # Headline metrics
    blocks.append(
        f"""<section><h2>Performance</h2>
        <div class="cards">{metrics_cards(result.full_experiment.metrics)}</div>
        {table(
            ["Segment", "Net return", "Trades", "Max DD %"],
            _metric_rows(result),
        )}</section>"""
    )

    # OOS breakdown
    m_oos = result.oos_experiment.metrics
    blocks.append(
        f"""<section><h2>Out-of-sample</h2>
        {kv_rows([
            ("OOS bars", str(m_oos.total_bars)),
            ("OOS net return", fmt(m_oos.net_return_pct, "%")),
            ("OOS CAGR", fmt(m_oos.cagr_pct, "%")),
            ("OOS max drawdown", fmt(m_oos.max_drawdown_pct, "%")),
            ("OOS trades", str(m_oos.num_trades)),
            ("OOS win rate", fmt(m_oos.win_rate, "%")),
            ("OOS Sharpe", fmt(m_oos.sharpe_ratio)),
            ("OOS Sortino", fmt(m_oos.sortino_ratio)),
        ])}</section>"""
    )

    # Cost breakdown
    full = result.full_experiment.result
    blocks.append(
        f"""<section><h2>Economic realism</h2>
        {kv_rows([
            ("Gross P&L", fmt(full.gross_profit + full.gross_loss)),
            ("Total commission", fmt(full.total_commission)),
            ("Total slippage", fmt(full.slippage_cost)),
            ("Transaction costs", fmt(full.transaction_costs)),
            ("Net P&L", fmt(result.full_experiment.metrics.net_pnl)),
        ])}</section>"""
    )

    # Regime results
    if result.regimes:
        regime_rows = []
        for r in result.regimes:
            m = r.backtest
            regime_rows.append([
                r.name,
                str(len(r.bars)),
                fmt(m.total_return_pct, "%"),
                str(m.num_trades),
                fmt(m.max_drawdown_pct, "%"),
                fmt(r.benchmark.total_return_pct, "%"),
            ])
        blocks.append(
            f"""<section><h2>Regime analysis</h2>
            {table(
                ["Regime", "Bars", "MA net return", "Trades", "MA max DD %", "BH return"],
                regime_rows,
            )}</section>"""
        )

    # Sensitivity
    sens_rows = []
    for row in result.sensitivity.rows:
        sens_rows.append([
            f"{row.fast} / {row.slow}",
            fmt(row.total_return_pct, "%"),
            str(row.num_trades),
            fmt(row.win_rate, "%"),
            fmt(row.max_drawdown_pct, "%"),
            fmt(row.net_pnl),
        ])
    skipped = ", ".join(f"({s.fast},{s.slow})" for s in result.sensitivity.skipped) or "none"
    blocks.append(
        f"""<section><h2>Parameter sensitivity (in-sample, not optimized)</h2>
        {table(
            ["(fast, slow)", "Return", "Trades", "Win %", "Max DD %", "Net P&L"],
            sens_rows,
        )}
        <p style="font-size:12px;color:#57606a">Skipped (invalid): {skipped}</p></section>"""
    )

    # Walk-forward
    wf = result.walk_forward
    wf_rows = []
    for step, step_result in zip(wf.steps, wf.per_step):
        wf_rows.append([
            str(step.index + 1),
            f"[{step.train_start}:{step.train_end}]",
            f"[{step.test_start}:{step.test_end}]",
            str(step_result.num_trades),
            fmt(step_result.total_return_pct, "%"),
        ])
    blocks.append(
        f"""<section><h2>Walk-forward (OOS windows)</h2>
        {table(
            ["Window", "Train (bars)", "Test (bars)", "OOS trades", "OOS return"],
            wf_rows,
        )}
        {kv_rows([
            ("Total OOS bars", str(wf.total_oos_bars)),
            ("Total OOS trades", f"{wf.total_trades} (win {wf.total_wins})"),
            ("Combined OOS return", fmt(wf.combined_return_pct, "%")),
        ])}</section>"""
    )

    # Limitations
    blocks.append(
        """<section><h2>Limitations</h2>
        <ul style="font-size:13px;color:#57606a">
        <li>Cost schedule is illustrative (nse_fo_illustrative), not a guarantee of
        real broker/regulator fees for the NIFTY 50 index.</li>
        <li>Slippage/spread/impact are model assumptions, not measured market impact.</li>
        <li>The strategy is a simple moving-average crossover; no risk parity,
        position sizing, or stop-loss logic is applied.</li>
        <li>Execution is simulated; no live market, liquidity, or latency effects are captured.</li>
        <li>Results are historical/synthetic evidence, not a prediction of future performance.</li>
        </ul></section>"""
    )

    return build_research_html(
        title=title,
        blocks=blocks,
        disclaimer=disclaimer,
        generated_at=result.created_at,
    )


def _text_summary(result: RealDataResearchResult, report_path: Path, *, smoke: bool) -> str:
    stats = result.stats
    m = result.full_experiment.metrics
    oos = result.oos_experiment.metrics
    lines = [
        f"{'SMOKE RUN (synthetic data)' if smoke else 'REAL-DATA RESEARCH RUN'}",
        f"dataset        : NIFTY 50 1d | {stats.start} .. {stats.end} | {stats.observations} bars",
        f"data hash      : {result.dataset_hash}",
        f"validation     : {'OK' if result.validation['ok'] else 'FAILED'}",
        f"strategy       : MA({result.strategy_params['fast']},{result.strategy_params['slow']})",
        f"capital/qty    : {result.initial_capital} / {result.quantity}",
        f"full net ret   : {fmt(m.net_return_pct, '%')}",
        f"full trades    : {m.num_trades} | max DD {fmt(m.max_drawdown_pct, '%')}",
        f"OOS net ret    : {fmt(oos.net_return_pct, '%')}",
        f"OOS trades     : {oos.num_trades} | max DD {fmt(oos.max_drawdown_pct, '%')}",
    ]
    if result.full_experiment.benchmark is not None:
        lines.append(f"buy&hold(gross): {fmt(result.full_experiment.benchmark.total_return_pct, '%')}")
    lines.append(f"report         : {report_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a reproducible NIFTY 50 real-data research study (read-only).",
        epilog="Use --smoke for offline pipeline validation with deterministic synthetic data.",
    )
    parser.add_argument("--token", default="", help="Upstox access token (or set UPSTOX_ACCESS_TOKEN)")
    parser.add_argument(
        "--start",
        type=_parse_date,
        default=DEFAULT_START_DATE,
        help="start date YYYY-MM-DD (default 2015-01-01)",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=DEFAULT_END_DATE,
        help="end date YYYY-MM-DD (default 2024-12-31)",
    )
    parser.add_argument("--outdir", default="datasets", help="directory for the saved dataset")
    parser.add_argument("--report-dir", default="reports", help="directory for the HTML report")
    parser.add_argument("--name", default=None, help="dataset file name (default: auto)")
    parser.add_argument("--capital", type=Decimal, default=DEFAULT_CAPITAL, help="initial capital")
    parser.add_argument("--quantity", type=int, default=DEFAULT_QUANTITY, help="quantity per signal")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="run offline smoke test with synthetic data (no token required)",
    )
    args = parser.parse_args(argv)

    outdir = Path(args.outdir).resolve()
    report_dir = Path(args.report_dir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    if args.smoke:
        dataset = _build_synthetic_dataset(outdir)
        title = "NIFTY 50 Real-Data Research Pipeline — Offline Smoke Test"
        disclaimer = (
            "Synthetic smoke-test data only. This report validates the real-data "
            "research pipeline; it does not contain or claim to contain real market data."
        )
    else:
        load_dotenv()
        token = (args.token or os.getenv("UPSTOX_ACCESS_TOKEN", "") or "").strip()
        if not token:
            return _error(
                "Real-data research requires UPSTOX_ACCESS_TOKEN. "
                "Set it in the environment or pass --token, or run --smoke for offline validation."
            )
        try:
            dataset = _fetch_real_dataset(token, args.start, args.end, outdir, args.name)
        except Exception as exc:
            return _error(f"failed to acquire dataset: {exc}")
        title = "NIFTY 50 Daily — Baseline MA-Cross Research Report"
        disclaimer = (
            "Historical real-market evidence only. The cost schedule is illustrative, "
            "execution is simulated, and no live trading or order placement occurs. "
            "Past performance does not indicate future results."
        )

    try:
        result = run_real_data_research(
            dataset,
            initial_capital=args.capital,
            quantity=args.quantity,
        )
    except Exception as exc:
        return _error(f"research pipeline failed: {exc}")

    report_name = "real_data_smoke_report.html" if args.smoke else "real_data_research_report.html"
    report_path = report_dir / report_name
    provider = dataset.metadata.get("provider", "unknown")
    html = _build_html_report(result, title=title, disclaimer=disclaimer, provider=provider)
    report_path.write_text(html, encoding="utf-8")

    print(_text_summary(result, report_path, smoke=args.smoke))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
