"""Human/machine-readable walk-forward reports (WS 7.18).

Renders the append-only daily evolution ledger, the algorithm version/change
history and the promotion/rejection history into the artifacts the dashboard
and humans consume:

* ``summary.json`` — dashboard page source (Algorithm Evolution section).
* ``evolution_timeline.html`` / ``evolution_timeline.md`` — the day-by-day
  algorithm evolution ledger rendered as readable tables (one row per day).
* ``versions.json`` — algorithm version / change history.
* ``promotions.jsonl`` — promotion / rejection history (one decision per line).

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


__all__ = ["write_reports"]