"""Render the live human-readable project dashboard from machine-readable state.

Reads ``docs/project_state.json`` (the canonical execution-state file) plus
live facts gathered from git (branch, HEAD, upstream sync state, remote) and
renders a single self-contained ``docs/project_status.html``.

The HTML page is the *rendered view*: ``docs/project_state.json`` remains the
source of truth for a fresh agent session. Regenerate after every meaningful
checkpoint:

    python scripts/update_project_status.py

Deterministic and stdlib-only. The dashboard always displays the
PAPER-TRADING safety banner; it can never imply live execution.
"""
from __future__ import annotations

import html
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "docs" / "project_state.json"
OUTPUT_FILE = REPO_ROOT / "docs" / "project_status.html"

CSS = """
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #f6f8fa; color: #1f2328; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; }
.banner { background: #5b0f00; color: #fff; text-align: center; font-weight: 700;
  letter-spacing: .06em; padding: 10px 12px; font-size: 14px; margin-bottom: 18px; border-radius: 8px; }
header { padding: 20px 24px; border-radius: 10px; background: linear-gradient(135deg, #1f2328, #3a4039); color: #fff; margin-bottom: 20px; }
header h1 { margin: 0; font-size: 22px; }
header p { margin: 6px 0 0; opacity: .9; font-size: 13px; }
.statusbar { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; align-items: stretch; }
.card { flex: 1 1 150px; background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: 12px 16px; }
.card .num { font-size: 19px; font-weight: 700; }
.card .lbl { font-size: 11px; color: #57606a; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 4px; }
.card.ok .num { color: #1a7f37; }
.card.bad .num { color: #cf222e; }
.card.warn .num { color: #bd9a00; }
.card.neutral .num { color: #0d1117; }
section { background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: 18px 20px; margin-bottom: 20px; }
section h2 { margin: 0 0 12px; font-size: 16px; border-bottom: 1px solid #eaeef2; padding-bottom: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #eaeef2; vertical-align: top; }
th { background: #f6f8fa; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: #57606a; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
.badge.ok { background: #dafbe1; color: #1a7f37; }
.badge.bad { background: #ffebe9; color: #cf222e; }
.badge.warn { background: #fff8c5; color: #7d5b00; }
.badge.neutral { background: #eaeef2; color: #57606a; }
.note { background: #fff8e6; border: 1px solid #e3c86a; border-radius: 8px; padding: 12px 16px; font-size: 12px; margin-bottom: 20px; }
.footer { font-size: 12px; color: #57606a; text-align: center; margin-top: 8px; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px; padding: 12px 14px; font-size: 12px; overflow-x: auto; }
"""


def _sh(*args: str) -> str:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, cwd=REPO_ROOT, timeout=30, check=False
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _esc(value: object) -> str:
    return html.escape(str(value))


def _render_leaderboard(rows: list[list[str]], head: list[str]) -> str:
    """HTML table where index-3 cell is a tier badge, numeric cells are right-aligned."""
    rendered = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            if i == 3:
                cells.append(f"<td>{_badge(cell)}</td>")
            elif i <= 2:
                cells.append(f"<td>{_esc(cell)}</td>")
            else:
                cells.append(f"<td class=num>{_esc(cell)}</td>")
        rendered.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in head)}</tr></thead>"
        f"<tbody>{''.join(rendered)}</tbody></table>"
    )


def _simple_table(rows: list[list[str]], head: list[str]) -> str:
    return (
        f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in head)}</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows)
        + "</tbody></table>"
    )


def _kv_table(rows: list[list[str]]) -> str:
    return "".join(
        f"<tr><th>{_esc(k)}</th><td>{_cell(v)}</td></tr>" for k, v in rows
    )


def _cell(value: str) -> str:
    """Render a table value; values already produced by :func:`_badge` are
    trusted HTML (from our own generator), everything else is escaped."""
    if isinstance(value, str) and value.startswith("<span class='badge"):
        return value
    return _esc(value)


def _badge(status: str) -> str:
    tone = "neutral"
    lowered = str(status).lower()
    if "run" in lowered or "pass" in lowered or "ready" in lowered or "ok" in lowered or lowered == "yes":
        tone = "ok"
    elif "block" in lowered or "fail" in lowered or "stop" in lowered or "disabled" in lowered or lowered == "no":
        tone = "bad"
    elif "wait" in lowered or "warn" in lowered or "risk" in lowered or "plan" in lowered or lowered == "monitor":
        tone = "warn"
    return f"<span class='badge {tone}'>{_esc(status)}</span>"


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"


def _assessment_rows() -> list[dict[str, object]]:
    """Per-bucket detail from the assessment artifact when present."""
    assessment_file = REPO_ROOT / "reports" / "algorithm_state" / "assessment.json"
    if not assessment_file.exists():
        return []
    try:
        payload = json.loads(assessment_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(payload.get("buckets", {}).values())


def _read_json(relative: str) -> dict[str, object] | list[object]:
    path = REPO_ROOT / relative
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else payload
    except (OSError, ValueError):
        return {}


def _laboratory_section() -> str:
    """ALGORITHM LABORATORY: family competition + research scoreboard."""
    sb = _read_json("reports/algorithm_state/research_scoreboard.json")
    comp = sb.get("competition") or {}
    champion = sb.get("champion") or {}
    leaderboard = comp.get("leaderboard") or {}
    criteria = comp.get("criteria") or {}

    rows: list[list[str]] = [
        ["Current champion", f"{champion.get('strategy_id','?')} ({champion.get('strategy_family','?')} fam) v{champion.get('version','?')}"],
        ["Champion status", str(champion.get("status", "?"))],
        ["A BEST TESTED present?", "no" if not comp.get("best_tested_present") else "yes"],
        ["Best tested entry", (comp.get("best_tested") or {}).get("strategy_id") or "NONE (no credible positive-OOS candidate)"],
        ["Conclusion", f"WS 7.16 conclusion {comp.get('conclusion','?')} — no family currently promotable"],
        ["Registered families", ", ".join(sorted(sb.get("families") or {}))],
        ["Registered specs", str(len(sb.get("registry_catalog") or {}))],
        ["Research allocation", "EQUAL across families (never capital)"],
        ["Ensemble / meta-decision", str((sb.get("ensemble_status") or {}).get("status", "?"))],
    ]
    body = _kv_table(rows)

    body += "<h3>Family leaderboard (recorded evidence)</h3>"
    table_rows = []
    for family, members in sorted(leaderboard.items()):
        for member in members:
            table_rows.append([
                family,
                member.get("strategy_id", "?"),
                member.get("version", "?"),
                member.get("tier_label", "?"),
                str(member.get("oos_net_pnl") or "no OOS read"),
                str(member.get("oos_return_pct") or "-"),
                str(member.get("oos_trades") or "-"),
                str(member.get("oos_per_trade_t") or "-"),
            ])
    head = ["Family", "Strategy", "Version", "Tier", "OOS net P&L", "OOS ret %", "OOS trades", "per-trade t"]
    body += _render_leaderboard(table_rows, head)

    body += "<h3>Why no winner today</h3><p class='note'>" + _esc(comp.get("headline", "?")) + "</p>"

    body += "<h3>Diagnostic criteria (per strategy)</h3>"
    crit_rows = []
    for strategy_id in sorted(criteria):
        row = criteria[strategy_id]
        crit_rows.append([
            strategy_id,
            str(row.get("oos_net_pnl") or "no OOS"),
            str(row.get("oos_return_pct") or "-"),
            str(row.get("trade_count") or "-"),
            str(row.get("per_trade_t") or "-"),
            str(row.get("profit_factor") or "-"),
            str(row.get("win_rate_pct") or "-"),
            str(row.get("max_drawdown_pct") or "-"),
            str(row.get("perturbation_robustness") or "-"),
        ])
    crit_head = ["Strategy", "OOS net", "OOS ret%", "trades", "t", "PF", "Win%", "MaxDD%", "Robustness"]
    body += (
        f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in crit_head)}</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td class=num>{_esc(c)}</td>" for c in r) + "</tr>" for r in crit_rows)
        + "</tbody></table>"
    )

    body += (
        "<p class='note'><strong>PAPER-ONLY & OOS PROTECTION:</strong> every figure above "
        "comes from recorded artifacts. Protected OOS was read exactly once; nothing here "
        "re-runs, re-trains or tunes on OOS. A least-negative strategy is never called a winner.</p>"
    )
    return _section("ALGORITHM LABORATORY / RESEARCH COMPETITION", body)


def _daily_performance_section() -> str:
    """STRATEGY/FAMILY DAILY ATTRIBUTION from the recorded daily report."""
    report = _read_json("reports/algorithm_state/daily_performance.json")
    rows = report.get("rows") if isinstance(report, dict) else []
    if not rows:
        return _section("DAILY STRATEGY / FAMILY ATTRIBUTION",
                        "<p class='note'>No recorded daily rows yet (champion replay appears once scripts/build_daily_performance.py runs).</p>")
    latest_row = rows[-1] if rows else {}
    buckets = sorted({str(r.get("bucket")) for r in rows})
    families = sorted({str(r.get("strategy_family")) for r in rows})
    head = ["Date", "Strategy", "Bucket", "Trades", "Wins/Loss", "Win%", "Daily P&L", "Cumulative", "MaxDD"]
    table_rows = [[
        str(r.get("trading_date", "?")),
        str(r.get("strategy_id", "?")),
        str(r.get("bucket", "?")),
        str(r.get("trades", 0)),
        f"{r.get('wins',0)}/{r.get('losses',0)}",
        str(r.get("win_rate_pct") or "-"),
        str(r.get("daily_pnl") or "0"),
        str(r.get("cumulative_pnl") or "0"),
        str(r.get("max_drawdown") or "0"),
    ] for r in rows[-25:]]
    body = _kv_table([
        ["Bucket sources", ", ".join(buckets)],
        ["Families represented", ", ".join(families)],
        ["Daily rows (total)", str(len(rows))],
        ["Latest trading day", str(latest_row.get("trading_date", "-"))],
        ["Latest day net P&L", str(latest_row.get("daily_pnl", "-"))],
    ])
    body += "<h3>Last 25 daily strategy rows</h3>"
    body += (
        f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in head)}</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td class=num>{_esc(c)}</td>" for c in r) + "</tr>" for r in table_rows)
        + "</tbody></table>"
    )
    body += (
        "<p class='note'>Champion rows are the recorded replay (bucket research_replay) - a "
        "recorded account series, not a performance claim. Paper bucket is empty until live "
        "paper trades close. This table attributes P&L; it never selects algorithms.</p>"
    )
    return _section("DAILY STRATEGY / FAMILY ATTRIBUTION", body)


def _continuous_agent_section() -> str:
    """WS 7.8: live continuous paper-trading agent status from the heartbeat."""
    hb = _read_json("reports/algorithm_state/paper_agent.json")
    if not isinstance(hb, dict) or not hb.get("run_id"):
        return _section(
            "CONTINUOUS PAPER-TRADING AGENT",
            "<p class='note'>No heartbeat yet — run <code>python scripts/run_paper_agent.py</code>.</p>",
        )
    decision = str(hb.get("safety_decision", "N/A"))
    decision_badge = _badge(decision)
    rows: list[list[str]] = [
        ["Run id", str(hb.get("run_id", "?"))],
        ["Agent state", _badge(str(hb.get("state", "?")))],
        ["Market phase", str(hb.get("market_phase", "?"))],
        ["Environment", str(hb.get("environment", "?"))],
        ["Safety decision", decision_badge],
        ["Safety instruction", str(hb.get("safety_instruction") or "-")],
        ["Polls", str(hb.get("polls", 0))],
        ["Jobs run (successful)", str(hb.get("jobs_run", 0))],
        ["Consumed bars", str(hb.get("consumed_bars", 0))],
        ["Paper orders / fills / trades", f"{hb.get('orders_submitted', 0)} / {hb.get('fills', 0)} / {hb.get('trades', 0)}"],
        ["Open quantity", str(hb.get("open_quantity", 0))],
        ["Cash / Equity", f"{hb.get('cash', '?')} / {hb.get('equity', '?')}"],
        ["Latest bar time", str(hb.get("latest_bar_time") or "-")],
        ["Checkpointed at", str(hb.get("checkpointed_at") or "-")],
        ["Last error", str(hb.get("last_error") or "-")],
    ]
    body = _kv_table(rows)
    body += (
        "<p class='note'>The continuous agent is paper-only: the only execution "
        "path is <code>PaperBroker</code>; live market data never implies live "
        "broker execution. A fail-safe STOP gates new cycle activity until "
        "investigation (§17h / §17k).</p>"
    )
    return _section("CONTINUOUS PAPER-TRADING AGENT", body)


def _live_execution_section() -> str:
    """WS 7.9: controlled live F&O execution integration test status.

    Reads the last execution summary artifact. Displays broker-plumbing
    integration results (Outcome A) ONLY; it never rates the algorithm
    (Outcome B = Algorithm Health, unchanged)."""
    summary = _read_json("reports/execution/last_run_summary.json")
    if not isinstance(summary, dict) or not summary.get("run_id"):
        return _section(
            "LIVE EXECUTION INTEGRATION TEST (WS 7.9)",
            "<p class='note'>No run artifact yet — run "
            "<code>python scripts/run_live_execution_test.py --data-source smoke</code>.</p>",
        )
    rows: list[list[str]] = [
        ["Run id", str(summary.get("run_id", "?"))],
        ["Mode", _badge(str(summary.get("mode") or "?"))],
        ["Dry run", str(summary.get("dry_run")) if summary.get("dry_run") is not None else "?"],
        ["Outcome (A = execution integration)", _badge(str(summary.get("outcome") or "?"))],
        ["Stage", str(summary.get("stage") or "?")],
        ["Symbol / side", f"{summary.get('symbol') or '?'} / {summary.get('side') or '?'}"],
        ["Entry / exit order", f"{summary.get('entry_order_id') or '-'} ({summary.get('entry_fill_price') or '-'}) / {summary.get('exit_order_id') or '-'} ({summary.get('exit_fill_price') or '-'})"],
        ["Position flat after exit", str(summary.get("position_flat")) if summary.get("position_flat") is not None else "?"],
        ["Reasons (if not PASS)", " ; ".join(str(r) for r in (summary.get("reasons") or []))],
        ["Algorithm Health (Outcome B)", "UNCHANGED — RED / ALGO READY = NO (this test rates broker plumbing only)"],
    ]
    body = _kv_table(rows)
    body += (
        "<p class='note'><strong>NO REAL ORDER:</strong> dry-run intent is enforced by "
        "the adapter <code>dry_run</code> flag (default True) plus the live-execution "
        "gate (env flag + consent-file fingerprint matching the token) plus explicit "
        "<code>--confirm-live-enablement</code>. Any refusal leaves a FAIL result and "
        "places no order. Real-money trading remains DISABLED; the single real F&O "
        "experiment runs tomorrow under explicit human-controlled enablement.</p>"
    )
    return _section("LIVE EXECUTION INTEGRATION TEST (WS 7.9)", body)


def _ws77_view_sections(state: dict[str, Any]) -> str:
    """WS 7.7 read-only dashboard views (SYSTEM / TRADING / PERFORMANCE / HISTORICAL / LEARNING)."""
    git = _git_facts()
    project = state.get("project", {})
    tests = state.get("tests", {})
    safety = state.get("safety_status", {})
    regime_eval = _read_json("reports/regime_eval/regime_eval.json")

    system_rows: list[list[str]] = [
        ["Environment", project.get("path", "?")],
        ["Runtime", "Python 3 (stdlib rendering)"],
        ["Git branch / HEAD", f"{git['branch']} / {git['head']} — {git['subject']}"],
        ["Upstream", git["upstream"]],
        ["Tests passed / failed", f"{tests.get('passed','?')} / {tests.get('failed',0)}"],
        ["Datasets", "real NIFTY 50 5m history; reports/datasets git-ignored"],
        ["Config", "config/settings.py; paper defaults v1-paper-defaults"],
    ]
    system = _section("SYSTEM", _kv_table(system_rows))

    trading_rows: list[list[str]] = [
        ["Algorithm", (state.get("algorithm") or {}).get("algorithm_version", "?")],
        ["Strategy", (state.get("algorithm_laboratory") or {}).get("current_champion", "moving_average_cross")],
        ["Paper server", (state.get("paper_trading_status") or {}).get("server_status", "not running")],
        ["Market session", (state.get("paper_trading_status") or {}).get("market_session", "?")],
        ["Orders placed", "PAPER ONLY — no broker/order execution"],
        ["Closed paper trades", str((state.get("algorithm") or {}).get("paper_trades_recorded", 0))],
    ]
    trading = _section("TRADING", _kv_table(trading_rows))

    perf = (state.get("algorithm") or {})
    perf_rows: list[list[str]] = [
        ["Health", _badge(perf.get("algorithm_health", "?"))],
        ["Net P&L (backtest bucket)", perf.get("net_pnl", "?")],
        ["Expectancy / trade", perf.get("expectancy", "?")],
        ["Profit factor", perf.get("profit_factor", "?")],
        ["Max drawdown", perf.get("max_drawdown", "?")],
        ["Trend", _badge(perf.get("performance_trend", "?"))],
    ]
    performance = _section("PERFORMANCE", _kv_table(perf_rows))

    hist = regime_eval.get("evaluation") if isinstance(regime_eval, dict) else {}
    regime_rows: list[list[str]] = []
    for item in hist.get("by_regime") or []:
        regime_rows.append([
            str(item.get("label", "?")),
            str(item.get("count", "-")),
            str(item.get("win_rate_pct") or "-"),
            str(item.get("net_pnl") or "-"),
            str(item.get("expectancy") or "-"),
        ])
    historical_head = ["Regime", "Trades (safe slice)", "Win %", "Net P&L", "Expectancy"]
    historical_body = "".join("N/A")
    if regime_rows:
        historical_body = (
            f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in historical_head)}</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td class=num>{_esc(c)}</td>" for c in r) + "</tr>" for r in regime_rows)
            + "</tbody></table>"
        )
    hypo_rows = []
    for item in hist.get("hypotheses") or []:
        hypo_rows.append([
            str(item.get("hypothesis_id", "?")),
            str(item.get("name", "?")),
            str(item.get("status", "?")),
            str(item.get("observation", ""))[:120],
        ])
    if hypo_rows:
        historical_body += "<h3>Regime hypotheses</h3>" + _simple_table(
            hypo_rows, ["ID", "Hypothesis", "Status", "Observation"]
        )
    historical = _section(
        "HISTORICAL / REGIME",
        historical_body if regime_rows or hypo_rows else
        "<p class='note'>no regime_eval artifact on disk yet.</p>",
    )

    lab = state.get("algorithm_laboratory") or {}
    learn_rows: list[list[str]] = [
        ["Best tested present", str(lab.get("best_tested_present", False))],
        ["Best tested", (lab.get("best_tested") or {}).get("strategy_id") or "none"],
        ["Families", ", ".join(lab.get("families") or [])],
        ["Research allocation", "EQUAL"],
        ["Ensemble status", str(lab.get("ensemble_status", "?"))],
        ["Scoreboard artifact", str(lab.get("scoreboard_artifact", "?"))],
    ]
    learning = _section("LEARNING / RESEARCH", _kv_table(learn_rows))

    return system + trading + performance + historical + learning


def _algorithm_section(state: dict[str, Any]) -> str:
    algo = state.get("algorithm") or {}
    headline = [
        ["Algorithm status", _badge(algo.get("algorithm_health", "n/a"))],
        ["ALGO READY (paper-trading readiness)", _badge(algo.get("algo_ready", "n/a"))],
        ["Win rate", f"{algo.get('win_rate') or 'n/a'}%"],
        ["Wins / Losses",
         f"{algo.get('winning_trades') or 0} / {algo.get('losing_trades') or 0} "
         f"(closed {algo.get('total_closed_trades') or 0})"],
        ["Profit factor", algo.get("profit_factor") or "n/a"],
        ["Net P&L", algo.get("net_pnl") or "n/a"],
        ["Expectancy / trade", algo.get("expectancy") or "n/a"],
        ["Max drawdown", algo.get("max_drawdown") or "n/a"],
        ["Current drawdown", algo.get("current_drawdown") or "n/a"],
        ["Consecutive wins / losses", f"{algo.get('consecutive_wins') or 0} / {algo.get('consecutive_losses') or 0}"],
        ["Average signal confidence", algo.get("average_confidence") or "n/a (baseline emits none)"],
        ["Last 10 win rate", f"{algo.get('last_10_win_rate') or 'n/a'}%"],
        ["Last 20 win rate", f"{algo.get('last_20_win_rate') or 'n/a'}%"],
        ["Today's win rate", f"{algo.get('today_win_rate') or 'n/a'}%"],
        ["Performance trend", _badge(algo.get("performance_trend", "INSUFFICIENT DATA"))],
        ["Algorithm version", algo.get("algorithm_version") or "n/a"],
        ["Configuration version", algo.get("configuration_version") or "n/a"],
        ["Strategy id / family", f"{algo.get('strategy_id') or 'moving_average_cross'} / {algo.get('strategy_family') or 'TREND_FOLLOWING'}"],
        ["Paper trades recorded", algo.get("paper_trades_recorded") or 0],
    ]
    body = _kv_table(headline)

    rows = _assessment_rows()
    if rows:
        bucket_head = ["Bucket", "Trades", "Win%", "Wins/Loss", "Net P&L", "Expectancy", "PF", "MaxDD", "Last10", "Last20"]
        bucket_rows = []
        for row in rows:
            bucket_rows.append([
                str(row.get("bucket", "?")),
                str(row.get("total_closed", "?")),
                str(row.get("win_rate_pct") or "n/a"),
                f"{row.get('winning') or 0}/{row.get('losing') or 0}",
                str(row.get("net_pnl") or "n/a"),
                str(row.get("expectancy") or "n/a"),
                str(row.get("profit_factor") or "n/a"),
                str(row.get("max_drawdown_pct") or "n/a"),
                str(row.get("last_10_win_rate") or "n/a"),
                str(row.get("last_20_win_rate") or "n/a"),
            ])
        body += "<h3>Per-dataset bucket detail</h3>" + (
            f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in bucket_head)}</tr></thead>"
            f"<tbody>{''.join('<tr>' + ''.join(f'<td class=num>{_esc(c)}</td>' for c in row) + '</tr>' for row in bucket_rows)}</tbody></table>"
        )

    reasons = [
        ["WHY this status?", algo.get("health_reason") or "n/a"],
        ["WHY this readiness?", algo.get("readiness_reason") or "n/a"],
        ["Heading source", algo.get("headline_note") or ""],
    ]
    body += "<h3>Decision rationale</h3>" + _kv_table(reasons)
    body += (
        "<p class='note'><strong>PAPER-ONLY:</strong> ALGO READY is a "
        "<em>paper-trading</em> readiness indicator. It never authorizes "
        "real-money trading or live broker execution. Real-money trading stays "
        "disabled.</p>"
    )
    return _section("ALGORITHM READY / HEALTH", body)


def _algorithm_evolution_section() -> str:
    """WS 7.18: walk-forward evolution ledger headline from reports/walkforward."""
    summary = _read_json("reports/walkforward/summary.json")
    if not isinstance(summary, dict) or not summary.get("run_id"):
        return _section(
            "ALGORITHM EVOLUTION (WS 7.18 WALK-FORWARD)",
            "<p class='note'>No walk-forward artifact yet — run "
            "<code>scripts/run_walkforward.py</code>.</p>",
        )
    domain = summary.get("domain") or {}
    scope = summary.get("scope") or {}
    champ = summary.get("champion_totals") or {}
    bench = summary.get("benchmark") or {}
    versions = summary.get("versions") or []
    promotions = summary.get("promotions") or []
    challengers = summary.get("challengers") or {}
    active = [v for v in versions if v.get("status") == "ACTIVE"]
    active_id = active[0].get("version_id", "?") if active else "?"

    rows: list[list[str]] = [
        ["Framework / run", f"walkforward-{summary.get('framework_version','?')}  ·  run {summary.get('run_id','?')}"],
        ["Discipline", str(scope.get("note", "?"))],
        ["Protected OOS start", str(scope.get("protected_oos_start", "?"))],
        ["Walked research domain", f"{domain.get('first_day','?')} .. {domain.get('last_day','?')} ({domain.get('days_processed','?')}/{domain.get('days_available','?')} days)"],
        ["Active algorithm (end of walk)", str(active_id)],
        ["Champion domain net P&L", str(champ.get("net_pnl", "?"))],
        ["Champion round trips", f"{champ.get('round_trips','?')}  (wins {champ.get('wins','?')} / losses {champ.get('losses','?')})"],
        ["Champion costs / slippage", f"{champ.get('costs','?')} / {champ.get('slippage','?')}"],
        ["Champion max drawdown", f"{champ.get('max_drawdown_pct','?')}%"],
        ["Buy-and-hold reference", f"{bench.get('name','?')}: net {bench.get('net_pnl','?')} ({bench.get('total_return_pct','?')}%)"],
        ["Promotions", str(len(promotions))],
        ["Challengers spawned / gated", f"{len(challengers)} spawned / {len(promotions)} gated"],
    ]
    body = _kv_table(rows)

    body += "<h3>Challenger lifecycle (pre-registered hypotheses, future-only validation)</h3>"
    ch_rows = []
    for cid in sorted(challengers):
        c = challengers[cid]
        ch_rows.append([
            str(c.get("key", "?")),
            str(c.get("title", ""))[:38],
            c.get("status", "?"),
            f"{c.get('validation_start','?')}..{c.get('validation_end','?')}",
            str(c.get("validated_days", 0)),
            str(c.get("decision", "?")),
        ])
    if ch_rows:
        body += _simple_table(ch_rows, ["Key", "Title", "Status", "Future-only window", "Days", "Decision"])
    else:
        body += "<p class='note'>No challenger hypothesis was supported by recorded evidence during this walk.</p>"

    props = []
    discipline = summary.get("algorithm_evolution") or {}
    for k, v in discipline.items():
        props.append(f"<li><strong>{k.replace('_', ' ')}</strong>: {'yes' if v else 'no'}</li>")
    body += "<h3>Ledger discipline guarantees</h3><ul>" + "".join(props) + "</ul>"

    body += (
        "<p class='note'>Every ledger day answers: WHAT algorithm traded, WHY (problem), "
        "WHAT hypothesis was pre-registered, WHAT happened in future-only validation, WAS it "
        "promoted, and WHY the next algorithm runs. 21-field day-by-day record: "
        "reports/walkforward/walkforward.ledger.jsonl; readable timeline: "
        "reports/walkforward/evolution_timeline.html.</p>"
    )
    return _section("ALGORITHM EVOLUTION (WS 7.18 WALK-FORWARD)", body)


def _git_facts() -> dict[str, str]:
    branch = _sh("git", "rev-parse", "--abbrev-ref", "HEAD") or "?"
    head = _sh("git", "rev-parse", "--short", "HEAD") or "?"
    subject = _sh("git", "show", "-s", "--format=%s", "HEAD") or "?"
    remote_url = _sh("git", "remote", "get-url", "origin") or "?"
    upstream = _sh("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") or "no-upstream"
    sync = _sh("git", "status", "-sb")
    dirty_untracked = _sh("git", "status", "--porcelain")
    dirty_count = len([x for x in dirty_untracked.splitlines() if x]) if dirty_untracked else 0
    clean = dirty_count == 0
    behind_ahead = "clean" if clean else f"{dirty_count} changed/untracked path(s)"
    return {
        "branch": branch,
        "head": head,
        "subject": subject,
        "remote_url": remote_url,
        "upstream": upstream,
        "sync": sync,
        "behind_ahead": behind_ahead,
        "clean": "CLEAN" if clean else "DIRTY",
    }


def _build_hierarchy(state: dict[str, Any]) -> str:
    completed = state.get("completed_phases", [])
    pending = state.get("pending_phases", [])
    rows: list[list[str]] = []
    for item in completed:
        rows.append([item, _badge("DONE")])
    current = state.get("current_phase", "")
    current_version = state.get("current_phase_version", "")
    rows.append([f"{current} — {current_version}", _badge(state.get("phase_status", "RUNNING"))])
    for item in pending:
        rows.append([item, _badge("PENDING")])
    head = ["Phase / work stream", "Status"]
    return (
        f"<table><thead><tr>{''.join(f'<th>{_esc(h)}</th>' for h in head)}</tr></thead>"
        f"<tbody>{''.join('<tr>' + ''.join(f'<td>{_esc(c)}</td>' for c in row) + '</tr>' for row in rows)}</tbody></table>"
    )


def _build_nested_table(title: str, mapping: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                rows.append([f"{key}.{sub_key}", str(sub_value)])
        else:
            rows.append([key, str(value)])
    return _section(title, _kv_table(rows))


def render() -> str:
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    git = _git_facts()
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    project = state.get("project", {})
    tests = state.get("tests", {})
    safety = state.get("safety_status", {})
    champion = state.get("champion", {})
    challenger = state.get("challenger", {})
    paper = state.get("paper_trading_status", {})

    # ----- PROJECT ----------------------------------------------------
    progress = state.get("overall_progress", 0)
    project_body = _kv_table([
        ["Project name", project.get("name", "?")],
        ["Project path", project.get("path", "?")],
        ["Overall project completion %", f"{progress}%"],
        ["Current phase", state.get("current_phase", "?")],
        ["Current phase version", state.get("current_phase_version", "?")],
        ["Current phase status", state.get("phase_status", "?")],
        ["Project version", state.get("project_version", "?")],
        ["Package version", project.get("package_version", "?")],
        ["Timestamp (rendered)", now],
        ["Agent status / heartbeat", f"{state.get('agent_status', '?')} — {state.get('last_activity', '')}"],
    ])

    # ----- ENGINEERING -------------------------------------------------
    eng_rows: list[list[str]] = [
        ["Current task", state.get("active_task", "?")],
        ["Tests passed", f"{tests.get('passed', '?')}  (failed {tests.get('failed', 0)})"],
        ["Tests failed", tests.get("failed", 0)],
        ["Validation status", state.get("validation", {}).get("status", "?")],
        ["Latest Git commit", f"{git['head']} — {git['subject']}"],
        ["Latest pushed commit", state.get("last_pushed_commit", "?")],
        ["Upstream sync", git["behind_ahead"]],
        ["Branch / upstream", f"{git['branch']} → {git['upstream']}"],
        ["Last successful checkpoint", state.get("last_completed_checkpoint", "?")],
        ["Next action", state.get("next_action", "?")],
    ]
    eng_body = _kv_table(eng_rows)

    # ----- MODEL / STRATEGY --------------------------------------------
    model_rows: list[list[str]] = [
        ["Current champion", champion.get("name", "?")],
        ["Champion status", champion.get("status", "?")],
        ["Champion full-window net return %", champion.get("full_window_net_return_pct", "?")],
        ["Champion OOS net return %", champion.get("oos_net_return_pct", "?")],
        ["Champion OOS per-trade t", champion.get("per_trade_oos_t", "?")],
        ["Champion note", champion.get("note", "")],
        ["Challenger status", challenger.get("status", "?")],
        ["Promotion/rejection decision", challenger.get("decision", "?")],
        ["Reason for decision", challenger.get("reason", "?")],
        ["Research status", "Challenger research cycle WS 7.16 concluded B (no credible edge)"],
        ["OOS status", "Protected OOS consumed once for shortlist confirmation"],
        ["Validation status", "All candidate evidence recorded; none promoted"],
    ]
    model_body = _kv_table(model_rows)

    # ----- PAPER TRADING -----------------------------------------------
    pt_rows: list[list[str]] = [
        ["Readiness", paper.get("readiness", "?")],
        ["Server status", paper.get("server_status", "?")],
        ["Market-session state", paper.get("market_session", "?")],
        ["Paper-trading version", paper.get("version", "?")],
        ["Configuration version", paper.get("config_version", "?")],
        ["Latest paper-trading observation", paper.get("latest_paper_trade_note", "?")],
    ]
    pt_body = _kv_table(pt_rows)

    # ----- SAFETY -------------------------------------------------------
    safety_rows: list[list[str]] = [
        ["Real-money trading", safety.get("real_money_trading", "?"), "bad" if str(safety.get("real_money_trading", "")).upper() not in ("DISABLED", "OFF", "NO") else "ok"],
        ["Broker order execution", safety.get("broker_order_execution", "?"), "bad" if str(safety.get("broker_order_execution", "")).upper() not in ("DISABLED", "OFF", "NO") else "ok"],
        ["Credentials / secrets", safety.get("credentials_secrets", "?"), "bad" if str(safety.get("credentials_secrets", "")).upper() not in ("NOT INTRODUCED", "NONE", "NO") else "ok"],
        ["Risk controls", safety.get("risk_controls", "?"), "ok"],
        ["Stop-loss controls", safety.get("stop_loss_controls", "?"), "ok"],
        ["Position sizing controls", safety.get("position_sizing_controls", "?"), "ok"],
        ["Max-loss controls", safety.get("max_loss_controls", "?"), "ok"],
    ]
    safety_body = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_badge(v)}</td></tr>" for k, v, tone in safety_rows
    )
    safety_body = f"<table><tbody>{safety_body}</tbody></table>"

    # ----- BLOCKERS / RISKS ----------------------------------------------
    blockers = state.get("blockers", []) or []
    risks = state.get("risks", []) or []
    blocker_cell = "None" if not blockers else "<br>".join(_esc(b) for b in blockers)
    risk_cell = "None" if not risks else "<br>".join(_esc(r) for r in risks)
    br_body = _kv_table([["Active blockers", blocker_cell], ["Warnings / risks", risk_cell]])

    # ----- AGENT -----------------------------------------------------------
    agent_rows: list[list[str]] = [
        ["Agent status", _badge(state.get("agent_status", "?")).replace("class='badge", "style='font-size:13px' class='badge")],
        ["Current action", state.get("active_task", "?")],
        ["Last activity", state.get("last_activity", "?")],
        ["Next action", state.get("next_action", "?")],
        ["Agent heartbeat", now],
    ]
    agent_body = _kv_table(agent_rows)

    status_cards = "".join([
        f"<div class='card ok'><div class='lbl'>Agent status</div><div class='num'>{_esc(state.get('agent_status', '?'))}</div></div>",
        f"<div class='card neutral'><div class='lbl'>Overall progress</div><div class='num'>{progress}%</div></div>",
        f"<div class='card neutral'><div class='lbl'>Phase</div><div class='num'>{_esc(state.get('current_phase_version', '?'))}</div></div>",
        f"<div class='card ok'><div class='lbl'>Tests passed</div><div class='num'>{tests.get('passed', '?')}</div></div>",
        f"<div class='card ok'><div class='lbl'>Git</div><div class='num'>{_esc(git['head'])}</div></div>",
        f"<div class='card ok'><div class='lbl'>Champion</div><div class='num'>MA(5,21)</div></div>",
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fno-ai-paper-trading — Live Project Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="banner">PAPER TRADING — NO LIVE ORDERS</div>
  <header>
    <h1>fno-ai-paper-trading — Live Project Dashboard</h1>
    <p>{_esc(project.get('path', '?'))} &middot; rendered {_esc(now)} &middot; paper-trading only &middot; no live execution</p>
  </header>
  <div class="statusbar">{status_cards}</div>
  {_section("PROJECT", project_body)}
  {_section("PHASES — Progress", _build_hierarchy(state) + "<p>After each checkpoint: rerun <code>python scripts/update_project_status.py</code>, commit, push.</p>")}
  {_section("ENGINEERING", eng_body)}
  {_section("MODEL / STRATEGY", model_body)}
  {_algorithm_section(state)}
  {_laboratory_section()}
  {_algorithm_evolution_section()}
  {_daily_performance_section()}
  {_continuous_agent_section()}
  {_live_execution_section()}
  {_ws77_view_sections(state)}
  {_section("PAPER TRADING", pt_body)}
  {_section("SAFETY", safety_body)}
  {_section("BLOCKERS / RISKS", br_body)}
  {_section("AGENT", agent_body)}
  <div class="footer">machine-readable state: docs/project_state.json &middot; rendered by scripts/update_project_status.py &middot; paper-trading only</div>
</div>
</body>
</html>"""


def main() -> None:
    output = render()
    OUTPUT_FILE.write_text(output, encoding="utf-8")
    print(f"wrote {OUTPUT_FILE} ({len(output)} bytes)")


if __name__ == "__main__":
    main()