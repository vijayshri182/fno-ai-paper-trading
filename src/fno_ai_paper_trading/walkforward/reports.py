"""Human/machine-readable walk-forward reports (WS 7.18).

Renders the append-only daily evolution ledger, the algorithm version/change
history and the promotion/rejection history into the artifacts the dashboard
and humans consume:

* ``summary.json`` — dashboard page source (Algorithm Evolution section).
* ``evolution_timeline.html`` / ``evolution_timeline.md`` — the day-by-day
  algorithm evolution ledger rendered as readable tables (one row per day).
* ``versions.json`` — algorithm version / change history.
* ``promotions.jsonl`` — promotion / rejection history (one decision per line).
* :func:`algorithm_logic_report_md` — read-only per-day ``algorithm-logic``
  report (renders where every day's algorithm/challenger logic actually lives:
  version IDs in the ledger resolved against ``versions.json``, challenger
  params persisted in ledger field 14, preregistered hypotheses from the frozen
  catalog; anything not persisted is labelled NOT PERSISTED instead of guessed).

All values come from the deterministic run result; no wall-clock time is ever
embedded, so identical runs produce byte-identical reports.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from fno_ai_paper_trading.research.report import escape, fmt

from fno_ai_paper_trading.walkforward.config import FRAMEWORK_VERSION
from fno_ai_paper_trading.walkforward.engine import WalkForwardResult
from fno_ai_paper_trading.walkforward.records import daily_field_label_order

# Compact human-readable timeline columns (ledger fields -> display label).
TIMELINE_COLUMNS = (
    ("1.day", "Day"),
    ("2.algorithm_used", "Algorithm"),
    ("3.parent_algorithm", "Parent"),
    ("4.regime", "Regime"),
    ("5.signal", "Signal"),
    ("10.outcome", "Outcome"),
    ("7.pnl", "P&L"),
    ("8.transaction_costs", "Costs"),
    ("12.problem_identified", "Problem"),
    ("14.challenger_generated", "Challenger"),
    ("15.modification_proposed", "Proposal"),
    ("18.validation_status", "Validation"),
    ("19.promotion_decision", "Gate"),
    ("20.next_day_algorithm", "Next algo"),
)


def _money(value: Decimal | str) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _short(items: list[Mapping[str, object]], key: str) -> str:
    if not items:
        return ""
    return "; ".join(str(item.get(key, item.get("challenger_id", "?"))) for item in items)


def _problem_pattern(record: Mapping[str, object]) -> str:
    problem = record.get("12.problem_identified", {})
    if isinstance(problem, Mapping):
        pattern = str(problem.get("pattern", ""))
        return "" if pattern in ("", "none") else pattern
    return ""


def _gate_text(record: Mapping[str, object]) -> str:
    decisions: list[str] = []
    for item in record.get("19.promotion_decision", []) or []:
        if not isinstance(item, Mapping):
            continue
        decision = str(item.get("decision", ""))
        cid = str(item.get("challenger_id", ""))
        if decision == "PROMOTE":
            decisions.append(f"{cid} PROMOTE")
        elif decision:
            decisions.append(f"{cid} {decision}")
    return "; ".join(decisions)


def _validated_text(record: Mapping[str, object]) -> str:
    rows: list[str] = []
    for item in record.get("18.validation_status", []) or []:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status", ""))
        done = item.get("validated_days")
        need = item.get("required_days")
        rows.append(f"{item.get('challenger_id', '?')}:{done}/{need}({status})")
    return "; ".join(rows)


def _timeline_rows(records: Sequence[Mapping[str, object]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                str(record.get("1.day", "")),
                str(record.get("2.algorithm_used", "")),
                str(record.get("3.parent_algorithm", "") or ""),
                str(record.get("4.regime", "")),
                str(record.get("5.signal", "")),
                str(record.get("10.outcome", "")),
                str(record.get("7.pnl", "")),
                str(record.get("8.transaction_costs", "")),
                _problem_pattern(record),
                _short(record.get("14.challenger_generated", []), "challenger_id"),
                _short(record.get("15.modification_proposed", []), "title"),
                _validated_text(record),
                _gate_text(record),
                str(record.get("20.next_day_algorithm", "")),
            ]
        )
    return rows


def _summary(
    result: WalkForwardResult,
) -> dict[str, object]:
    challengers = {
        str(c["challenger_id"]): {
            "challenger_id": str(c["challenger_id"]),
            "key": c.get("key"),
            "title": c.get("title"),
            "hypothesis": c.get("hypothesis"),
            "status": c.get("status"),
            "decision": c.get("decision"),
            "promoted_version": c.get("promoted_version_id"),
            "created_on": c.get("created_on"),
            "validation_start": c.get("validation_start"),
            "validation_end": c.get("validation_end"),
            "validated_days": len(c.get("day_metrics", {})),
        }
        for c in result.challengers
    }
    return {
        "run_id": result.run_id,
        "framework_version": result.framework_version,
        "config_hash": result.config_hash,
        "domain": {
            "first_day": result.first_day.isoformat() if result.first_day else None,
            "last_day": result.last_day.isoformat() if result.last_day else None,
            "days_available": result.days_available,
            "days_processed": result.days_processed,
        },
        "scope": {
            "research_only": True,
            "live_trading": False,
            "protected_oos_start": (
                result.config.protected_oos_start.isoformat()
                if result.config.protected_oos_start
                else None
            ),
            "note": (
                "paper/historical walk only; protected out-of-sample days are "
                "never loaded; champion parameters are frozen, challenger "
                "parameters are fixed catalog constants (never tuned)"
            ),
        },
        "config": result.config.to_dict(),
        "versions": list(result.versions),
        "parent_of": dict(result.parent_of),
        "champion_totals": {
            key: _money(value) for key, value in result.champion_totals.items()
        },
        "benchmark": dict(result.benchmark),
        "challengers": challengers,
        "promotions": list(result.promotions),
        "ledger": {
            "fields": "21-field day-by-day algorithm evolution ledger",
            "machine_file": "evolution ledger JSONL",
            "html_timeline_file": "evolution_timeline.html",
            "md_timeline_file": "evolution_timeline.md",
            "field_spec": daily_field_label_order(),
        },
        "algorithm_evolution": {
            "evidence_driven": True,
            "algorithm_for_day_frozen_before_day": True,
            "learning_from_day_affects_only_future_days": True,
            "history_never_rewritten": True,
            "protected_oos_untouched": True,
            "no_parameter_tuning": True,
        },
    }


def write_reports(
    out_dir: Path | str,
    result: WalkForwardResult,
) -> dict[str, Path]:
    """Write all walk-forward report artifacts into ``out_dir``."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    summary = _summary(result)
    written: dict[str, Path] = {}

    summary_path = target / "summary.json"
    summary_path.write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    written["summary.json"] = summary_path

    versions_path = target / "versions.json"
    versions_path.write_text(
        json.dumps(
            {
                "framework_version": result.framework_version,
                "active_version_id": (
                    state_active_id(result) or "model_0"
                ),
                "parent_of": dict(result.parent_of),
                "versions": list(result.versions),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written["versions.json"] = versions_path

    promotions_path = target / "promotions.jsonl"
    with open(promotions_path, "w", encoding="utf-8") as handle:
        for promotion in result.promotions:
            handle.write(json.dumps(promotion, sort_keys=True) + "\n")
    written["promotions.jsonl"] = promotions_path

    records = result.ledger_records
    timeline_html_path = target / "evolution_timeline.html"
    timeline_html_path.write_text(
        _build_timeline_html(result, records), encoding="utf-8"
    )
    written["evolution_timeline.html"] = timeline_html_path

    timeline_md_path = target / "evolution_timeline.md"
    timeline_md_path.write_text(_build_timeline_md(result, records), encoding="utf-8")
    written["evolution_timeline.md"] = timeline_md_path

    return written


def state_active_id(result: WalkForwardResult) -> str | None:
    for version in result.versions:
        if version.get("status") == "ACTIVE":
            return str(version["version_id"])
    return None


def _leading_cards(result: WalkForwardResult) -> str:
    from fno_ai_paper_trading.research.report import card

    totals = result.champion_totals
    net = _money(Decimal(str(totals.get("net_pnl", 0))))
    days = int(totals.get("days", 0))
    trades = int(totals.get("round_trips", 0))
    tone = "neutral" if net == "0" else ("ok" if Decimal(net) > 0 else "bad")
    return "".join(
        [
            card(str(result.days_processed), "Days (walk)", "ok"),
            card(net, "Champion net P&L", tone),
            card(str(trades), "Champion round trips"),
            card(str(len(result.promotions)), "Gate decisions"),
            card(str(_promote_count(result)), "Promotions"),
            card(
                _money(Decimal(str(totals.get("max_drawdown_pct", 0)))) + " %",
                "Max drawdown",
                "bad" if Decimal(str(totals.get("max_drawdown_pct", 0))) > 5 else "ok",
            ),
        ]
    )


def _promote_count(result: WalkForwardResult) -> int:
    return sum(1 for p in result.promotions if p.get("decision") == "PROMOTE")


def _build_timeline_html(
    result: WalkForwardResult, records: Sequence[Mapping[str, object]]
) -> str:
    from fno_ai_paper_trading.research.report import (
        build_research_html,
        kv_rows,
        table,
    )

    headers = [label for _key, label in TIMELINE_COLUMNS]
    rows = _timeline_rows(records)

    summary_row = kv_rows(
        [
            ("Run", result.run_id),
            ("Framework version", result.framework_version),
            ("Config hash", result.config_hash),
            ("Domain", f"{result.first_day} .. {result.last_day}"),
            ("Days processed", str(result.days_processed)),
            ("Champion net P&L", _money(Decimal(str(result.champion_totals.get("net_pnl", 0))))),
            ("Champion round trips", str(result.champion_totals.get("round_trips", 0))),
            ("Gate decisions", str(len(result.promotions))),
            ("Promotions", str(_promote_count(result))),
            ("Algorithm history", f"{len(result.versions)} version(s)"),
            (
                "Algorithms",
                "; ".join(f"{v['version_id']} ({v.get('status')})" for v in result.versions),
            ),
        ]
    )

    challenges = ""
    if result.challengers:
        challenges = table(
            ["Challenger", "Key", "Status", "Decision", "Created", "Window"],
            [
                [
                    str(c.get("challenger_id", "")),
                    str(c.get("key", "")),
                    str(c.get("status", "")),
                    str(c.get("decision", "") or ""),
                    str(c.get("created_on", "")),
                    (
                        f"{c.get('validation_start')} .. {c.get('validation_end')}"
                        if c.get("validation_start")
                        else ""
                    ),
                ]
                for c in result.challengers
            ],
        )

    blocks = [
        f"<div class='cards'>{_leading_cards(result)}</div>",
        f"<section><h2>Walk summary</h2><table><tbody>{summary_row}</tbody></table></section>",
    ]
    if result.benchmark:
        blocks.append(
            "<section><h2>Buy-and-hold reference</h2>"
            + table(
                ["Net P&L", "Return %", "Max DD %", "Note"],
                [
                    [
                        str(result.benchmark.get("net_pnl", "")),
                        str(result.benchmark.get("total_return_pct", "")),
                        str(result.benchmark.get("max_drawdown_pct", "")),
                        str(result.benchmark.get("note", "")),
                    ]
                ],
            )
            + "</section>"
        )
    blocks.append(f"<section><h2>Algorithm evolution / learning timeline</h2>{table(headers, rows)}</section>")
    if challenges:
        blocks.append(f"<section><h2>Challenger history</h2>{challenges}</section>")

    return build_research_html(
        title="Walk-forward algorithm evolution — " + result.run_id,
        blocks=blocks,
        disclaimer=(
            "Paper/historical walk-forward research only. Every algorithm was frozen "
            "before the day it ran; all learning from a day affects only later days and "
            "only after an explicit promotion gate. No live trading, no real money. The "
            "protected out-of-sample period was never loaded. Parameters were never tuned."
        ),
        generated_at=str(result.last_day or ""),
    )


def _build_timeline_md(
    result: WalkForwardResult, records: Sequence[Mapping[str, object]]
) -> str:
    headers = [label for _key, label in TIMELINE_COLUMNS]
    lines = [
        f"# Walk-forward algorithm evolution — {result.run_id}",
        "",
        f"- Framework version: `{result.framework_version}`",
        f"- Config hash: `{result.config_hash}`",
        f"- Domain: {result.first_day} .. {result.last_day} "
        f"({result.days_processed}/{result.days_available} days)",
        (
            f"- Champion net P&L: "
            f"{_money(Decimal(str(result.champion_totals.get('net_pnl', 0))))}"
        ),
        f"- Gate decisions: {len(result.promotions)} (promotions: {_promote_count(result)})",
        "",
        "_Paper/historical walk only. Algorithm for a day was frozen before the day ran; "
        "learning from a day affects only later days and only after an explicit promotion gate. "
        "Protected out-of-sample days were never loaded. No live trading. Parameters never tuned._",
        "",
        "## Algorithm evolution / learning timeline",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for row in _timeline_rows(records):
        escaped = [cell.replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Algorithm-logic test report (read-only rendering of persisted artifacts)
# ---------------------------------------------------------------------------

_PREDICATE_BLURBS = {
    "suppress_buys_not_up": "non-UP BUY trades lose while UP-regime BUYs win",
    "suppress_sells_not_down": "non-DOWN SELL trades lose while DOWN-regime SELLs win",
    "suppress_sideways_entries": "SIDEWAYS entries lose while trend-day entries win",
    "stricter_trend_gate": "a 0.10 trend threshold separates UP BUY winners from non-UP BUY losers",
}

_ALGORITHM_LOGIC_HEADERS = (
    "Day",
    "Algorithm Logic",
    "Parent Logic",
    "Challenger Logic",
    "What Changed",
    "Validation Gate",
    "Promotion Decision",
    "Next Algo",
)


def _strategy_logic_text(
    strategy_name: object, strategy_params: Mapping[str, object]
) -> str:
    """Human-readable logic for a persisted ``strategy_name`` + ``strategy_params``."""
    params = dict(strategy_params or {})
    if strategy_name == "moving_average_cross":
        return (
            f"CALL/PUT entries on fast-SMA({params.get('fast', '?')}) crossing "
            f"above/below slow-SMA({params.get('slow', '?')}); otherwise HOLD"
        )
    if strategy_name == "regime_filtered_ma_cross":
        trends = params.get("allowed_trends")
        if isinstance(trends, (list, tuple)):
            allowed = ",".join(str(t) for t in trends)
        else:
            allowed = str(trends)
        return (
            f"MA({params.get('fast', '?')},{params.get('slow', '?')}) cross "
            f"restricted to allowed_trends=[{allowed}] "
            f"(trend_threshold_pct={params.get('trend_threshold_pct', '?')}); "
            f"BUY is suppressed outside the allowed trends, SELL/HOLD pass through"
        )
    fields = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{strategy_name}({fields})" if fields else f"{strategy_name}(no params)"


def _resolve_algo_logic(
    version_id: str, by_id: Mapping[str, Mapping[str, object]]
) -> str:
    version = by_id.get(version_id)
    if version is None:
        return (
            f"{version_id}: NOT PERSISTED in versions.json "
            f"(strategy/params unknown)"
        )
    name = version.get("strategy_name", "?")
    params = version.get("strategy_params", {}) or {}
    return f"{version_id}: {_strategy_logic_text(name, params)}"


def _challenger_logic_text(items: Sequence[Mapping[str, object]]) -> str:
    if not items:
        return "— (none generated this day)"
    out: list[str] = []
    for item in items:
        cid = str(item.get("challenger_id", "?"))
        name = item.get("strategy_name", "?")
        params = item.get("strategy_params", {}) or {}
        out.append(f"{cid} [{_strategy_logic_text(name, params)}]")
    return "; ".join(out)


def _what_changed_text(
    used: str, nxt: str, gates: Sequence[Mapping[str, object]]
) -> str:
    promoted = [g for g in gates if g.get("decision") == "PROMOTE"]
    if promoted:
        bits = [
            f"{g.get('challenger_id', '?')} PROMOTED -> {g.get('promoted_version', '?')}"
            for g in promoted
        ]
        if nxt and nxt != used:
            bits.append(f"{used} -> {nxt}")
        return "; ".join(bits)
    if nxt and nxt != used:
        return f"algorithm change: {used} -> {nxt}"
    return f"No change (algorithm stays {used})"


def _gate_run_text(gates: Sequence[Mapping[str, object]]) -> str:
    if not gates:
        return "NOT RUN — no challenger validation window completed this day"
    return "RAN: " + "; ".join(
        f"{g.get('challenger_id', '?')}={g.get('decision', '?')}" for g in gates
    )


def _promotion_decision_text(gates: Sequence[Mapping[str, object]]) -> str:
    if not gates:
        return "NO DECISION (gate not triggered today)"
    return "; ".join(
        f"{g.get('challenger_id', '?')} {g.get('decision', '?')}" for g in gates
    )


def _validation_status_rows(
    records: Sequence[Mapping[str, object]],
) -> list[list[str]]:
    seen: list[tuple[str, str, str, str]] = []
    for record in records:
        for item in record.get("18.validation_status", []) or []:
            if not isinstance(item, Mapping):
                continue
            row = (
                str(item.get("challenger_id", "?")),
                str(item.get("status", "?")),
                str(item.get("window_start", "")),
                str(item.get("window_end", "")),
            )
            if row not in seen:
                seen.append(row)
    return [list(row) for row in seen]


def _logic_rows(
    records: Sequence[Mapping[str, object]],
    by_id: Mapping[str, Mapping[str, object]],
) -> list[list[str]]:
    """Rows shared by the Markdown and HTML algorithm-logic report renderers."""
    rows: list[list[str]] = []
    for record in records:
        used = str(record.get("2.algorithm_used", ""))
        parent = record.get("3.parent_algorithm")
        challenger_items = record.get("14.challenger_generated", []) or []
        gates = [
            g
            for g in (record.get("19.promotion_decision", []) or [])
            if isinstance(g, Mapping)
        ]
        nxt = str(record.get("20.next_day_algorithm", ""))
        rows.append(
            [
                str(record.get("1.day", "")),
                _resolve_algo_logic(used, by_id),
                (
                    "— (baseline; no parent algorithm)"
                    if not parent
                    else _resolve_algo_logic(str(parent), by_id)
                ),
                _challenger_logic_text(challenger_items),
                _what_changed_text(used, nxt, gates),
                _gate_run_text(gates),
                _promotion_decision_text(gates),
                nxt,
            ]
        )
    return rows


def algorithm_logic_report_md(
    records: Sequence[Mapping[str, object]],
    versions: Sequence[Mapping[str, object]],
    *,
    oos_start: str,
    title: str | None = None,
) -> str:
    """Render a per-day algorithm-logic test report as Markdown.

    Pure rendering of persisted artifacts -- nothing is computed from wall
    clock and nothing is invented:

    * ``2.algorithm_used`` / ``3.parent_algorithm`` are resolved per version ID
      against ``versions`` (``versions.json``); an ID with no record is labelled
      NOT PERSISTED.
    * ``14.challenger_generated`` carries the challenger strategy params that
      were persisted at generation time.
    * Preregistered challenger hypotheses are the frozen catalog constants.
    * ``13/15/17/18/19`` feed the What Changed / validation / promotion cells.

    An OOS boundary check compares the maximum included day against
    ``oos_start`` and is always printed.
    """
    from fno_ai_paper_trading.walkforward.catalog import CATALOG

    oos_start = str(oos_start)
    if not records:
        raise ValueError("records must contain at least one ledger row")
    by_id = {str(v.get("version_id", "?")): v for v in versions}
    days = sorted({str(r.get("1.day", "")) for r in records})
    max_day = max(str(r["1.day"]) for r in records)
    oos_ok = max_day < oos_start

    lines = [
        f"# {title or 'Walk-forward algorithm-logic test report'}",
        "",
        "- Source artifacts: `walkforward.ledger.jsonl` (21-field daily ledger) and "
        "`versions.json` (per-version strategy name + parameters).",
        f"- Days included: {len(records)} ({days[0]} .. {days[-1]})",
        "- Algorithm logic: ledger field `2.algorithm_used` / `3.parent_algorithm` "
        "(version IDs) resolved against `versions.json`; challenger params are "
        "persisted in field `14.challenger_generated` at generation time.",
        "- Preregistered challenger hypotheses: frozen constants "
        "(`walkforward/catalog.py`); evidence only decides whether one fires.",
        f"- Protected OOS start: `{oos_start}`",
        (
            f"- OOS boundary check: **PASS** (max included day {max_day} < {oos_start})"
            if oos_ok
            else f"- OOS boundary check: **FAIL** (max included day {max_day} >= {oos_start})"
        ),
        "",
    ]

    lines.append("| " + " | ".join(_ALGORITHM_LOGIC_HEADERS) + " |")
    lines.append("|" + "|".join(["---"] * len(_ALGORITHM_LOGIC_HEADERS)) + "|")
    for row in _logic_rows(records, by_id):
        lines.append(
            "| "
            + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in row)
            + " |"
        )
    lines.append("")

    lines += [
        "## Preregistered challenger catalog (frozen constants)",
        "",
        "Each challenger is a fixed-parameter, regime-filtered variant of the frozen "
        "MA(5,21) champion. Parameters are locked in `walkforward/catalog.py`; they "
        "are never derived from evidence (anti-overfitting discipline).",
        "",
        "| Key | Title | Frozen strategy params | Evidence predicate fires when |",
        "|-----|-------|------------------------|------------------------------|",
    ]
    for spec in CATALOG:
        params = ", ".join(f"{k}={v}" for k, v in spec.strategy_params.items())
        blurb = _PREDICATE_BLURBS.get(
            spec.key, spec.hypothesis[:80] + "…"
        )
        lines.append(
            f"| `{spec.key}` | {spec.title} | `{params}` | {blurb} |"
        )
    lines.append("")

    status_rows = _validation_status_rows(records)
    if status_rows:
        lines += [
            "## Challenger validation status observed on the included days "
            "(ledger field `18.validation_status`)",
            "",
            "| Challenger | Status | Window |",
            "|------------|--------|--------|",
        ]
        for cid, status, start, end in status_rows:
            window = f"{start} .. {end}" if start else "—"
            lines.append(f"| {cid} | {status} | {window} |")
        lines.append("")

    lines += [
        "## Persistence notes",
        "",
        "- Ledger field `2.algorithm_used` stores only a **version ID** (e.g. "
        "`model_0`); the full strategy name and parameters are persisted per "
        "version in `versions.json` and resolved above.",
        "- Ledger field `3.parent_algorithm` stores the parent version ID (None "
        "for the baseline champion).",
        "- Challenger strategy/params are persisted in field "
        "`14.challenger_generated` at generation time; the evidence predicate "
        "itself is a frozen code constant in `catalog.py`.",
        "- Any cell labelled `NOT PERSISTED in versions.json` means the referenced "
        "version ID has no record there; nothing was guessed.",
        f"- The OOS boundary check compares the max included day against the "
        f"protected OOS start (`{oos_start}`); a FAIL would mean out-of-sample "
        f"data was read, which the engine forbids by construction.",
        "",
    ]
    return "\n".join(lines)


def algorithm_logic_report_html(
    records: Sequence[Mapping[str, object]],
    versions: Sequence[Mapping[str, object]],
    *,
    oos_start: str,
    title: str | None = None,
) -> str:
    """Render a per-day algorithm-logic test report as a self-contained HTML page.

    Mirror of :func:`algorithm_logic_report_md` for the same persisted
    artifacts: per-day algorithm/parent/challenger logic, What Changed, the
    validation gate and promotion decisions, the frozen challenger catalog,
    observed validation status, and an always-printed OOS boundary check.
    """
    from fno_ai_paper_trading.research.report import (
        build_research_html,
        kv_rows,
        table,
    )
    from fno_ai_paper_trading.walkforward.catalog import CATALOG

    oos_start = str(oos_start)
    if not records:
        raise ValueError("records must contain at least one ledger row")
    by_id = {str(v.get("version_id", "?")): v for v in versions}
    days = sorted({str(r.get("1.day", "")) for r in records})
    max_day = max(str(r["1.day"]) for r in records)
    oos_ok = max_day < oos_start

    blocks = [
        "<section><h2>Report</h2><table><tbody>"
        + kv_rows(
            [
                (
                    "Source artifacts",
                    "walkforward.ledger.jsonl (21-field daily ledger) and "
                    "versions.json (per-version strategy name + parameters)",
                ),
                ("Days included", f"{len(records)} ({days[0]} .. {days[-1]})"),
                (
                    "Algorithm logic",
                    "ledger field 2/3 version IDs (algorithm_used / "
                    "parent_algorithm) resolved against versions.json; "
                    "challenger params persisted in field 14 at generation time",
                ),
                (
                    "Preregistered challengers",
                    "frozen constants (walkforward/catalog.py); evidence only "
                    "decides whether one fires",
                ),
                ("Protected OOS start", oos_start),
                (
                    "OOS boundary check",
                    (
                        f"PASS — max included day {max_day} < {oos_start}"
                        if oos_ok
                        else f"FAIL — max included day {max_day} >= {oos_start}"
                    ),
                ),
            ]
        )
        + "</tbody></table></section>",
        "<section><h2>Per-day algorithm logic (one row per day)</h2>"
        + table(list(_ALGORITHM_LOGIC_HEADERS), _logic_rows(records, by_id))
        + "</section>",
        "<section><h2>Preregistered challenger catalog (frozen constants)</h2>"
        "<p>Each challenger is a fixed-parameter, regime-filtered variant of the "
        "frozen MA(5,21) champion; parameters are locked in "
        "<code>walkforward/catalog.py</code>, never derived from evidence "
        "(anti-overfitting discipline).</p>"
        + table(
            ["Key", "Title", "Frozen strategy params", "Evidence predicate fires when"],
            [
                [
                    spec.key,
                    spec.title,
                    ", ".join(f"{k}={v}" for k, v in spec.strategy_params.items()),
                    _PREDICATE_BLURBS.get(spec.key, spec.hypothesis[:80] + "…"),
                ]
                for spec in CATALOG
            ],
        )
        + "</section>",
    ]

    status_rows = _validation_status_rows(records)
    if status_rows:
        blocks.append(
            "<section><h2>Challenger validation status observed on the included "
            "days (ledger field 18)</h2>"
            + table(
                ["Challenger", "Status", "Window"],
                [
                    [
                        cid,
                        status,
                        f"{start} .. {end}" if start else "—",
                    ]
                    for cid, status, start, end in status_rows
                ],
            )
            + "</section>"
        )

    blocks.append(
        "<section><h2>Persistence notes</h2><ul>"
        "<li>Ledger field <code>2.algorithm_used</code> stores only a version ID "
        "(e.g. <code>model_0</code>); the full strategy name and parameters are "
        "persisted per version in <code>versions.json</code> and resolved above.</li>"
        "<li>Ledger field <code>3.parent_algorithm</code> stores the parent version "
        "ID (None for the baseline champion).</li>"
        "<li>Challenger strategy/params are persisted in field "
        "<code>14.challenger_generated</code> at generation time; the evidence "
        "predicate itself is a frozen code constant in <code>catalog.py</code>.</li>"
        "<li>Any cell labelled <code>NOT PERSISTED in versions.json</code> means "
        "the referenced version ID has no record there; nothing was guessed.</li>"
        f"<li>The OOS boundary check compares the max included day against the "
        f"protected OOS start (<code>{oos_start}</code>); a FAIL would mean "
        f"out-of-sample data was read, which the engine forbids by construction.</li>"
        "</ul></section>"
    )

    return build_research_html(
        title=title or "Walk-forward algorithm-logic test report",
        blocks=blocks,
        disclaimer=(
            "Paper/historical walk-forward research only. Per-day algorithm logic is "
            "resolved verbatim from persisted artifacts (ledger + versions.json); "
            "anything not persisted is labelled NOT PERSISTED, never guessed. "
            "Challenger parameters are frozen catalog constants, never tuned. The "
            "protected out-of-sample period was never loaded or read here."
        ),
        generated_at=f"{days[0]} .. {days[-1]}",
    )


__all__ = ["write_reports", "algorithm_logic_report_md", "algorithm_logic_report_html"]