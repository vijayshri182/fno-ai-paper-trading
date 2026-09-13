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


def _kv_table(rows: list[list[str]]) -> str:
    return "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows
    )


def _badge(status: str) -> str:
    tone = "neutral"
    lowered = status.lower()
    if "run" in lowered or "pass" in lowered or "ready" in lowered or "ok" in lowered:
        tone = "ok"
    elif "block" in lowered or "fail" in lowered or "stop" in lowered or "disabled" in lowered:
        tone = "bad"
    elif "wait" in lowered or "warn" in lowered or "risk" in lowered or "plan" in lowered:
        tone = "warn"
    return f"<span class='badge {tone}'>{_esc(status)}</span>"


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"


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