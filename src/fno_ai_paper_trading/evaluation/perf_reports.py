"""Persist a model-performance evaluation to CSV / JSON / a self-contained HTML page.

All artifacts go under ``reports/model_performance/`` (git-ignored). The HTML
page is a deterministic, dependency-free snapshot: no external JS/CDN, inline
SVG charts, and a prominent "PAPER TRADING — NO LIVE ORDER" banner. Nothing here
reads or writes live trading state.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.research.report import CSS, card, escape, fmt, kv_rows, table

PAPER_BANNER = "PAPER TRADING — NO LIVE ORDER"
MAX_CHART_POINTS = 2000


def _jsnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def write_artifacts(
    output_dir: Path,
    *,
    summary: dict[str, Any],
    trade_rows: list[list[str]],
    yearly_rows: list[list[str]],
    monthly_rows: list[list[str]],
    regime_rows: list[list[str]],
    equity_rows: list[list[str]],
    html: str,
) -> dict[str, Path]:
    """Write every deliverable artifact and return the created paths."""
    output_dir = Path(output_dir)
    paths = {
        "summary": output_dir / "summary.json",
        "trades": output_dir / "trades.csv",
        "yearly": output_dir / "yearly.csv",
        "monthly": output_dir / "monthly.csv",
        "regime": output_dir / "regime.csv",
        "equity_curve": output_dir / "equity_curve.csv",
        "html": output_dir / "model_performance.html",
    }
    write_json(paths["summary"], summary)
    write_csv(
        paths["trades"],
        [
            "rt_index",
            "entry_time",
            "exit_time",
            "side",
            "quantity",
            "entry_price",
            "exit_price",
            "entry_regime",
            "exit_regime",
            "holding_bars",
            "price_pnl",
            "commission",
            "net_pnl",
        ],
        trade_rows,
    )
    write_csv(
        paths["yearly"],
        ["period", "num_trades", "winning", "losing", "net_pnl", "avg_net_pnl", "end_equity"],
        yearly_rows,
    )
    write_csv(
        paths["monthly"],
        ["period", "num_trades", "winning", "losing", "net_pnl", "avg_net_pnl"],
        monthly_rows,
    )
    write_csv(
        paths["regime"],
        ["regime", "num_trades", "winning", "losing", "win_rate_pct", "net_pnl", "avg_net_pnl"],
        regime_rows,
    )
    write_csv(
        paths["equity_curve"],
        ["timestamp", "bar_index", "equity", "cash", "unrealized_pnl", "drawdown_from_peak"],
        equity_rows,
    )
    paths["html"].write_text(html, encoding="utf-8")
    return paths


def _downsample(points: list[tuple[float, float]], max_points: int = MAX_CHART_POINTS) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    sampled: list[tuple[float, float]] = []
    for i in range(max_points):
        sampled.append(points[int(i * step)])
    sampled.append(points[-1])
    return sampled


def _svg_line(
    points: list[tuple[float, float]],
    *,
    width: int = 1000,
    height: int = 240,
    stroke: str = "#1f6feb",
) -> str:
    if not points:
        return ""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    span_x = (xmax - xmin) or 1
    span_y = (ymax - ymin) or 1
    pad = 8
    coords = []
    for x, y in points:
        px = pad + (x - xmin) / span_x * (width - 2 * pad)
        py = height - pad - (y - ymin) / span_y * (height - 2 * pad)
        coords.append(f"{px:.1f},{py:.1f}")
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="background:#fff">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" points="{" ".join(coords)}"/></svg>'
    )


def equity_svg(curve_points) -> str:
    pts = _downsample(
        [(float(point.timestamp.timestamp()), float(point.equity)) for point in curve_points]
    )
    return _svg_line(pts)


def drawdown_svg(curve_points) -> str:
    pts = _downsample(
        [
            (float(point.timestamp.timestamp()), float(point.drawdown_from_peak))
            for point in curve_points
        ]
    )
    return _svg_line(pts, stroke="#cf222e")


def build_report_html(
    *,
    title: str,
    generated_at: str,
    summary: dict[str, Any],
    coverage: dict[str, Any],
    benchmark_rows: list[list[str]],
    yearly_rows: list[list[str]],
    monthly_rows: list[list[str]],
    regime_rows: list[list[str]],
    split_rows: list[list[str]],
    walkforward_rows: list[list[str]],
    cost_rows: list[list[str]],
    config_pairs: list[tuple[str, str]],
    equity_curve_points,
    notes: list[str],
) -> str:
    """Assemble the full deterministic HTML report page."""
    m = summary["continuous"]
    def mnum(key: str, suffix: str = "") -> str:
        value = m.get(key)
        return fmt(None) if value is None else f"{value}{suffix}"

    coverage_cards = "".join([
        card(str(coverage.get("num_bars", "?")), "Bars"),
        card(str(coverage.get("num_trading_days", "?")), "Trading days"),
        card(str(coverage.get("start_date", "")), "Start"),
        card(str(coverage.get("end_date", "")), "End"),
    ])
    headline = "".join([
        card(fmt(Decimal(m["net_return_pct"]), "%"), "Net Return", "ok" if Decimal(m["net_return_pct"]) >= 0 else "bad"),
        card(fmt(Decimal(m["final_equity"])), "Final Equity", "bad" if Decimal(m["final_equity"]) < 0 else "neutral"),
        card(fmt(Decimal(m["max_drawdown_pct"]), "%"), "Max DD %", "bad"),
        card(str(m["num_trades"]), "Round Trips"),
        card(fmt(Decimal(m["win_rate"]), "%"), "Win Rate", "bad"),
        card(fmt(Decimal(m["transaction_costs"])), "Friction Paid"),
    ])

    sections: list[str] = []
    sections.append(
        "<section><h2>Data coverage &amp; quality</h2>"
        f'<div class="cards">{coverage_cards}</div>'
        + kv_rows(
            [
                ("Dataset", str(coverage.get("name", ""))),
                ("Provider", str(coverage.get("provider", ""))),
                ("Interval", str(coverage.get("interval", ""))),
                ("Instrument", str(coverage.get("instrument", ""))),
                ("Period", f"{coverage.get('start_date')} .. {coverage.get('end_date')}"),
                ("Data hash", str(coverage.get("data_hash", ""))),
                ("Validation", "OK" if coverage.get("validation", {}).get("ok") else "ISSUES"),
                ("Weekday days in range (approx)", str(coverage.get("weekday_days_in_range", ""))),
                ("Candidate missing weekday days", str(coverage.get("candidate_missing_weekday_days_count", ""))),
            ]
        )
        + f"<p class='note'>Coverage caveat: the repo market calendar only encodes the current year's NSE holidays, so older official holidays are counted in <em>candidate missing weekday days</em>; they are upper-bound estimates, not confirmed missing data.</p>"
        + "</section>"
    )
    sections.append(
        "<section><h2>Headline metrics — champion " + escape(m["strategy_name"]) + " on " + escape(m["start"]) + " .. " + escape(m["end"]) + "</h2>"
        f'<div class="cards">{headline}</div>'
        + kv_rows(
            [
                ("Net P&L", mnum("net_pnl")),
                ("Net return %", mnum("net_return_pct", "%")),
                ("CAGR % (annualized)", mnum("cagr_pct", "%")),
                ("Final equity", mnum("final_equity")),
                ("Min equity", mnum("min_equity")),
                ("Max drawdown", mnum("max_drawdown")),
                ("Max drawdown %", mnum("max_drawdown_pct", "%")),
                ("Drawdown duration (bars)", str(m.get("drawdown_duration_bars", ""))),
                ("Annualized volatility %", mnum("annualized_volatility", "%")),
                ("Sharpe (annualized)", mnum("sharpe_ratio")),
                ("Sortino (annualized)", mnum("sortino_ratio")),
                ("Exposure %", mnum("exposure_pct", "%")),
                ("Expectancy", mnum("expectancy")),
                ("Gross profit", mnum("gross_profit")),
                ("Gross loss", mnum("gross_loss")),
                ("Commission", mnum("total_commission")),
                ("Slippage", mnum("slippage_cost")),
                ("Transaction costs", mnum("transaction_costs")),
                ("Reconciliation (Σ RT net P&L == total P&L)", str(m["reconciliation_ok"])),
            ]
        )
        + "</section>"
    )
    sections.append(
        "<section><h2>Equity curve</h2>"
        + equity_svg(equity_curve_points)
        + "</section>"
    )
    sections.append(
        "<section><h2>Drawdown from peak</h2>"
        + drawdown_svg(equity_curve_points)
        + "</section>"
    )
    sections.append(
        "<section><h2>Execution &amp; cost assumptions (paper model)</h2>"
        + kv_rows(config_pairs)
        + "<p class='note'>Neither the backtest nor the live paper broker models a cash/margin floor: buys are filled regardless of available cash. The negative-"
        + "equity stretch of this replay is therefore an artifact of the paper model (implicit leverage), not a claim about real broker behaviour.</p>"
        + "</section>"
    )
    sections.append(
        "<section><h2>Benchmark (buy &amp; hold, gross of costs)</h2>"
        + table(["Metric", "Champion (5m, net)", "NIFTY BH 5m (gross)", "NIFTY BH 1d same period (gross)"], benchmark_rows)
        + "</section>"
    )
    sections.append(
        "<section><h2>Yearly breakdown (realized, by exit period)</h2>"
        + table(["Period", "Trades", "Wins", "Losses", "Net P&L", "Avg/trade", "End equity"], yearly_rows)
        + "</section>"
    )
    sections.append(
        "<section><h2>Monthly breakdown (realized, by exit period)</h2>"
        + table(["Period", "Trades", "Wins", "Losses", "Net P&L", "Avg/trade"], monthly_rows)
        + "</section>"
    )
    sections.append(
        "<section><h2>Regime breakdown (decision-time entry regime)</h2>"
        + table(["Regime", "Trades", "Wins", "Losses", "Win rate %", "Net P&L", "Avg/trade"], regime_rows)
        + "</section>"
    )
    sections.append(
        "<section><h2>In-sample / validation / out-of-sample segments</h2>"
        + table(["Segment", "Range", "Bars", "Net return %", "Trades", "Win rate %", "Max DD %", "Transaction costs"], split_rows)
        + "<p class='note'>MA(5,21) has no data-fitted parameters, so each segment is evaluated with the same frozen champion. The segments demonstrate "
        + "stability across time under identical assumptions; they are not evidence of parameter fitting. Segments may end with an open position "
        + "(open trades &gt; 0), which is why per-segment net P&L need not reconcile with its own round-trip table.</p>"
        + "</section>"
    )
    sections.append(
        "<section><h2>Walk-forward (fixed champion, non-overlapping windows)</h2>"
        + table(["Window", "Train range", "Test range", "Test bars", "Trades", "Net return %", "Max DD %"], walkforward_rows)
        + "<p class='note'>The walk-forward framework normally refits on each train window; this evaluation intentionally serves the same frozen champion "
        + "every window, so it is a stability audit across time rather than an optimization loop.</p>"
        + "</section>"
    )
    sections.append(
        "<section><h2>Cost sensitivity (bounded, explicit scenarios)</h2>"
        + table(["Scenario", "Commission rate", "Slippage rate", "Trades", "Net return %", "Transaction costs", "Max DD %"], cost_rows)
        + "<p class='note'>Strategy decisions never read costs, so entry/exit times are identical across scenarios (see the 'same trades' flag in "
        + "summary.json). The comparison isolates the pure effect of friction.</p>"
        + "</section>"
    )
    if notes:
        notes_html = "".join(f"<li>{escape(note)}</li>" for note in notes)
        sections.append(f"<section><h2>Limitations &amp; caveats</h2><ul>{notes_html}</ul></section>")

    body = "".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{CSS}</style>
<style>
.banner {{ background:#5b0f00; color:#ffd7cf; text-align:center; font-weight:700; font-size:14px; letter-spacing:.08em; padding:8px 12px; border-radius:8px; margin-bottom:16px; }}
header {{ background: linear-gradient(135deg,#0b3d6e,#1f6feb); }}
</style>
</head>
<body>
<div class="wrap">
  <div class="banner">{PAPER_BANNER}</div>
  <header>
    <h1>{escape(title)}</h1>
    <p>Generated {escape(generated_at)} &middot; evaluation evidence &middot; no live execution</p>
  </header>
  <div class="note"><strong>Evidence:</strong> this report is computed by a deterministic evaluation harness over historical data with paper execution. It is a model-performance record, not a recommendation to trade and not live investment advice.</div>
  {body}
  <div class="footer">Generated by the model-performance evaluation harness &middot; paper-trading only &middot; {PAPER_BANNER}</div>
</div>
</body>
</html>"""