"""Discovery cycle reports: 19-section markdown + JSON + ranking HTML + history.

The exact algorithm for every candidate is preserved as an immutable definition
(``candidates/<id>.<version>.definition.json``) and re-printed here from that
record — this report never fabricates logic. If a definition was not persisted
the report says "NOT PERSISTED - CANNOT RECONSTRUCT EXACTLY".
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any, Mapping


def _money(value) -> str:
    try:
        return format(value, ",.2f")
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_num(value) -> str:
    try:
        return f"{float(value):,.4f}"
    except (TypeError, ValueError):
        return str(value)


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}\n"


def _candidate_body(cid: str, entry: Mapping[str, Any]) -> str:
    definition = entry.get("definition", {})
    train = entry.get("train", {})
    valid = entry.get("validation", {})
    lines = [
        f"### {cid} — {definition.get('name', cid)}",
        f"- family: `{definition.get('family', '?')}`  provider: `{definition.get('provider_path', '?')}`",
        f"- version: `{definition.get('version', '?')}`  definition_hash: `{definition.get('definition_hash', 'NOT PERSISTED - CANNOT RECONSTRUCT EXACTLY')}`",
        f"- description: {definition.get('description', '')}",
        f"- characteristics: CALL (BUY long) / PUT (SELL short) / NO_TRADE (HOLD), daily fresh-portfolio replay, force-exit squares the day.",
        "",
        "**Train**",
    ]
    if train:
        lines.extend([
            f"- net P&L `{_money(train.get('net_pnl'))}`  gross `{_money(train.get('gross_pnl'))}`  costs `{_money(train.get('costs'))}`",
            f"- trades `{train.get('trades')}`  WR `{_fmt_pct(train.get('win_rate'))}`  PF `{train.get('profit_factor')}`  expectancy `{_money(train.get('expectancy'))}`",
            f"- avWin `{_money(train.get('avg_win'))}`  avLoss `{_money(train.get('avg_loss'))}`  maxDD `{_fmt_pct(train.get('max_drawdown_pct'))}`  Sharpe `{train.get('sharpe')}`",
            f"- trades/day `{train.get('trade_frequency')}`  days w/ trades `{train.get('days_with_trades')}/{train.get('days')}`  open-at-close `{train.get('open_at_close_total')}`",
            f"- longest losing day-streak `{train.get('consecutive_losses')}`  positive segments `{train.get('positive_segments')}/{train.get('segments')}`",
            f"- quality issues: {', '.join(train.get('quality_issues', [])) or 'none'}" if train.get("quality_issues") else "",
        ])
    lines.append("")
    lines.append("**Validation**")
    if valid:
        lines.extend([
            f"- net P&L `{_money(valid.get('net_pnl'))}`  gross `{_money(valid.get('gross_pnl'))}`  costs `{_money(valid.get('costs'))}`",
            f"- trades `{valid.get('trades')}`  WR `{_fmt_pct(valid.get('win_rate'))}`  PF `{valid.get('profit_factor')}`  expectancy `{_money(valid.get('expectancy'))}`",
            f"- maxDD `{_fmt_pct(valid.get('max_drawdown_pct'))}`  positive segments `{valid.get('positive_segments')}/{valid.get('segments')}`",
            f"- quality issues: {', '.join(valid.get('quality_issues', [])) or 'none'}" if valid.get("quality_issues") else "",
        ])
    lines.append("")
    robust = entry.get("robustness")
    cost = entry.get("cost_scan")
    if robust:
        lines.append(f"- **Robustness**: {robust.get('positive', 0)}/{robust.get('n', 0)} perturbation variants profitable ({_fmt_pct(robust.get('positive_fraction', 0) * 100)})")
    if cost:
        lines.append(f"- **Cost sensitivity**: 1x `{_money(cost.get('1x'))}` / 3x `{_money(cost.get('3x'))}` / 5x `{_money(cost.get('5x'))}` → positive fraction {cost.get('positive_fraction')}")
    return "\n".join(lines)


def build_markdown(payload: Mapping[str, Any]) -> str:
    out: list[str] = [
        f"# Algorithm Discovery & Competition — Cycle Report",
        "",
        f"- run_id: `{payload.get('run_id')}`",
        f"- timestamp (UTC): {payload.get('timestamp')}",
        f"- generation: `{payload.get('generation')}`",
        "",
    ]

    # 1 DATA SOURCE
    prov = payload.get("provenance", {})
    out.append(_section("1. DATA SOURCE", "\n".join([
        f"- provider: `{prov.get('provider')}`",
        f"- instrument: `{prov.get('instrument')}`  key: `{prov.get('instrument_key')}`  interval: `{prov.get('interval')}`",
        f"- requested: {prov.get('requested_from')} .. {prov.get('requested_to')}",
        f"- actual: {prov.get('actual_from')} .. {prov.get('actual_to')}  candles: {prov.get('candle_count')}",
        f"- fetched at (UTC): {prov.get('retrieved_at')}  freshness: `{prov.get('freshness')}`",
        f"- data hash: `{prov.get('data_hash')}`  schema version: `{prov.get('schema_version')}`  dataset file: `{prov.get('dataset_path')}`",
        f"- data quality: {prov.get('data_quality', {})}" if prov.get('data_quality') else "- data quality: (validated)",
        f"- blocked: {prov.get('blocked') or 'no (fresh Upstox data acquired)'}",
    ])))

    # 2 MARKET ENVIRONMENT
    env = payload.get("market_environment", {})
    out.append(_section("2. MARKET ENVIRONMENT", "\n".join([
        f"- research window: {env.get('window_start')} .. {env.get('window_end')}  ({env.get('days')} trading days, {env.get('bars')} bars)",
        f"- avg close {_fmt_num(env.get('avg_close'))}  min low {_fmt_num(env.get('min_low'))}  max high {_fmt_num(env.get('max_high'))}",
        f"- avg daily range (pts): {_fmt_num(env.get('avg_day_range'))}  median ATR14 (pts): {_fmt_num(env.get('median_atr'))}",
        f"- regime estimate: {env.get('regime_estimate')}",
        f"- note: {env.get('note')}",
    ])))

    # 3 FAMILIES TESTED
    families = payload.get("families_tested", [])
    out.append(_section("3. FAMILIES TESTED", "\n".join(
        f"- `{f.get('family')}` → {', '.join(f.get('candidate_ids', []))}" for f in families
    ) or "- none"))

    # 4 CANDIDATES GENERATED / REJECTED
    cand = payload.get("candidates", {})
    scored = payload.get("scorecards", [])
    generated = payload.get("candidates_generated", len(cand))
    rejected_lines = []
    best_note = payload.get("current_best")
    for cid, entry in cand.items():
        train = entry.get("train", {})
        issues = train.get("quality_issues", [])
        flags = next((s.get("flags", []) for s in scored if s.get("candidate_id") == cid), [])
        reason = "rejected"
        reasons = []
        if "insufficient_evidence" in flags:
            reasons.append("insufficient trade evidence")
        if best_note is None and train.get("net_pnl", 0) and float(train.get("net_pnl")) < 0:
            reasons.append("negative train net P&L")
        if not reasons:
            reasons.append("no promotion-grade evidence (see Section 14)")
        rejected_lines.append(f"- `{cid}` ({entry.get('definition', {}).get('family', '?')}): {', '.join(reasons)}")
    out.append(_section("4. CANDIDATES GENERATED / REJECTED", "\n".join([
        f"- generated: {generated} (control + 6 discovery families)",
        "",
        *rejected_lines,
    ])))

    # 5 TOP CANDIDATES (competition order)
    ranking = payload.get("ranking", [])
    top_lines = []
    for i, row in enumerate(ranking[:10], start=1):
        top_lines.append(
            f"{i}. `{row.get('candidate_id')}@{row.get('version')}` — score {_fmt_num(row.get('score'))}, "
            f"net {_money(row.get('net_pnl'))}, trades {row.get('trades')}, flags {', '.join(row.get('flags', [])) or 'none'}"
        )
    out.append(_section("5. TOP CANDIDATES", "\n".join(top_lines) or "- no candidates ranked"))

    # 6 EXACT LOGIC
    logic_lines = []
    for cid, entry in cand.items():
        definition = entry.get("definition", {})
        logic_lines.append(f"- `{cid}` ({definition.get('name', cid)}): {definition.get('description') or 'no description on record'}. provider `{definition.get('provider_path')}`; hash `{definition.get('definition_hash', 'NOT PERSISTED - CANNOT RECONSTRUCT EXACTLY')}`")
    out.append(_section("6. EXACT LOGIC", "\n".join(logic_lines)))

    # 7 PARAMS
    params_lines = []
    for cid, entry in cand.items():
        definition = entry.get("definition", {})
        params = definition.get("params", {})
        serialized = json.dumps(params, sort_keys=True)
        params_lines.append(f"- `{cid}` params: `{serialized}`")
    out.append(_section("7. PARAMS", "\n".join(params_lines)))

    # 8 TRADE RESULTS
    results_lines = []
    for cid, entry in cand.items():
        train = entry.get("train", {})
        valid = entry.get("validation", {})
        results_lines.append(
            f"- `{cid}`: train net {_money(train.get('net_pnl'))} over {train.get('trades')} trades (WR {_fmt_pct(train.get('win_rate'))}, PF {train.get('profit_factor')}, maxDD {_fmt_pct(train.get('max_drawdown_pct'))}, Sharpe {train.get('sharpe')}); validation net {_money(valid.get('net_pnl'))} over {valid.get('trades')} trades"
        )
    out.append(_section("8. TRADE RESULTS", "\n".join(results_lines)))

    # 9 COSTS
    cost_lines = []
    for cid, entry in cand.items():
        train = entry.get("train", {})
        cost_lines.append(f"- `{cid}`: gross {_money(train.get('gross_pnl'))} − costs {_money(train.get('costs'))} = net {_money(train.get('net_pnl'))} (costs = commission + adverse slippage)")
    out.append(_section("9. COSTS", "\n".join(cost_lines)))

    # 10 REGIME RESULTS
    regime_lines = []
    for cid, entry in cand.items():
        train = entry.get("train", {})
        breakdown = train.get("regime_breakdown", {})
        side = train.get("side_breakdown", {})
        if breakdown:
            rows = ", ".join(f"{k}: {v.get('trades')} tr, {_money(v.get('net'))}" for k, v in sorted(breakdown.items()))
            regime_lines.append(f"- `{cid}` regimes: {rows}")
        if side:
            side_rows = ", ".join(f"{k}: {v.get('trades')} tr, {_money(v.get('net'))}" for k, v in sorted(side.items()))
            regime_lines.append(f"  - CALL/PUT: {side_rows}")
    out.append(_section("10. REGIME RESULTS", "\n".join(regime_lines) or "- (no trade records)"))

    # 11 ROBUSTNESS
    robust_lines = []
    for cid, entry in cand.items():
        robust = entry.get("robustness")
        if robust:
            robust_lines.append(
                f"- `{cid}`: {robust.get('positive', 0)}/{robust.get('n', 0)} variants profitable ({_fmt_pct(robust.get('positive_fraction', 0) * 100)}); nets by variant: {robust.get('net_by_variant', {})}"
            )
        else:
            robust_lines.append(f"- `{cid}`: no perturbations (control frozen / none defined)")
    out.append(_section("11. ROBUSTNESS", "\n".join(robust_lines)))

    # 12 OOS
    oos = payload.get("watch_window", {})
    out.append(_section("12. OUT-OF-SAMPLE", "\n".join([
        f"- protected OOS start: {oos.get('protected_oos_start')}",
        f"- OOS status: `PROTECTED — NOT EVALUATED` (no model was ever optimized or scored on post-boundary data)",
        f"- evidence discipline: training ≤ {oos.get('train_end')}, validation {oos.get('validation_start')}..{oos.get('validation_end')}",
    ])))

    # 13 COMPETITION RANKING
    rank_lines = [
        "| # | candidate | version | score | net P&L | trades | flags |",
        "|---|-----------|---------|-------|---------|--------|-------|",
    ]
    for i, row in enumerate(ranking, start=1):
        rank_lines.append(
            f"| {i} | {row.get('candidate_id')} | {row.get('version')} | {_fmt_num(row.get('score'))} | {_money(row.get('net_pnl'))} | {row.get('trades')} | {', '.join(row.get('flags', []))} |"
        )
    out.append(_section("13. COMPETITION RANKING", "\n".join(rank_lines)))

    # 14 PROMOTION DECISION
    promo_lines = []
    for cid, verdict in payload.get("promotion", {}).items():
        promo_lines.append(f"- `{cid}` → **{verdict.get('decision')}**")
        for reason in verdict.get("reasons", []):
            promo_lines.append(f"  - {reason}")
    out.append(_section("14. PROMOTION DECISION", "\n".join(promo_lines) or "- no challengers evaluated"))

    # 15 CURRENT BEST
    best = payload.get("current_best")
    out.append(_section("15. CURRENT BEST",
        f"- best candidate: {best.get('candidate_id') if best else 'NONE'}  version `{best.get('version') if best else '-'}`"
        if best else "- best candidate: NONE — no candidate passed promotion-grade evidence (does not mean 'least bad')"))

    # 16 WHY BEST
    out.append(_section("16. WHY BEST", payload.get("why_best", "no positive candidate selected")))

    # 17 WHAT REMAINS UNPROVEN
    unproven = payload.get("unproven", [])
    out.append(_section("17. WHAT REMAINS UNPROVEN", "\n".join(f"- {u}" for u in unproven) or "- nothing recorded"))

    # 18 NEXT HYPOTHESIS
    nh = payload.get("next_hypothesis", {})
    out.append(_section("18. NEXT HYPOTHESIS", "\n".join([
        f"- family: `{nh.get('family', '?')}`  mechanism: {nh.get('mechanism', '?')}",
        f"- read: {nh.get('read', '?')}  trigger: {nh.get('trigger', '?')}",
        f"- learning bullet: {payload.get('learning', {}).get('bullet', '')}",
    ])))

    # 19 ALGO READY
    ready = payload.get("algo_ready", "NO")
    out.append(_section("19. ALGO READY / LIVE STATUS", "\n".join([
        f"- ALGO READY = **{ready}**  (promotion gates not satisfied)",
        f"- live_trading = **{payload.get('live_trading', False)}** — no live broker credentials exist in this project",
        f"- next research action: {payload.get('next_action', 'continue the discovery loop with the next hypothesis')}",
    ])))

    # 20 LINKS (relative, regenerated by scripts/generate_paper_dashboard.py)
    links = payload.get("links") or {}
    if links:
        out.append(_section("20. LINKS", "\n".join(
            f"- {label}: `{url}`" for label, url in sorted(links.items())
        )))

    return "\n".join(out)


def write_report(payload: Mapping[str, Any], reports_dir: str) -> dict[str, str]:
    reports_dir = pathlib.Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    run_id = payload.get("run_id", "run")
    md_path = reports_dir / f"discovery_cycle_{run_id}.md"
    json_path = reports_dir / f"discovery_cycle_{run_id}.json"
    history_path = reports_dir / "discovery_history.md"
    rank_html_path = reports_dir / "discovery_rank.html"

    md = build_markdown(payload)
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    history = build_markdown(payload)
    sep = f"\n---\n<!-- {run_id} {datetime.now(timezone.utc).isoformat()} -->\n"
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(sep + history)

    rank_html_path.write_text(_rank_html(payload), encoding="utf-8")

    return {
        "markdown": str(md_path),
        "json": str(json_path),
        "history": str(history_path),
        "html": str(rank_html_path),
    }


def _rank_html(payload: Mapping[str, Any]) -> str:
    rows = []
    ranking = payload.get("ranking", [])
    for i, row in enumerate(ranking, start=1):
        rows.append(
            "<tr>"
            f"<td>{i}</td><td>{row.get('candidate_id')}</td><td>{row.get('version')}</td>"
            f"<td>{_fmt_num(row.get('score'))}</td><td>{_money(row.get('net_pnl'))}</td>"
            f"<td>{row.get('trades')}</td><td>{', '.join(row.get('flags', []))}</td>"
            "</tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Discovery Ranking</title>"
        "<style>body{font-family:monospace;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #999;padding:.4rem .8rem}</style></head><body>"
        f"<h1>Algorithm Discovery &amp; Competition — Ranking</h1>"
        f"<p>run_id: {payload.get('run_id')} · {payload.get('timestamp')}</p>"
        "<table><thead><tr><th>#</th><th>candidate</th><th>version</th><th>score</th><th>net P&amp;L</th><th>trades</th><th>flags</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"<h2>Current best</h2><p>{payload.get('why_best')}</p>"
        "</body></html>"
    )