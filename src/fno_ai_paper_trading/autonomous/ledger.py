"""Append-only decision-ledger writer for the autonomous loop.

Writes research decisions (Ledger Entries) into
``docs/RESEARCH_DECISION_LEDGER.md`` without rewriting history. Idempotent: the
same entry header is never appended twice. PAPER ONLY.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

ENTRY_005_HEADER = "## Entry 005"


def format_entry(number: int, title: str, fields: Mapping[str, str]) -> str:
    """Render one chronologically-appendable ledger entry as markdown."""
    if not title.strip():
        raise ValueError("title is required")
    lines = [f"## Entry {number:03d} — {title}", ""]
    for key, value in fields.items():
        if value:
            lines.append(f"- **{key}**: {value}")
        else:
            lines.append(f"- **{key}**:")
    return "\n".join(lines)


def append_entry_if_missing(
    ledger_path: str | Path,
    header: str,
    entry_markdown: str,
) -> bool:
    """Append ``entry_markdown`` unless ``header`` already exists; returns appended."""
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if header in existing:
        return False
    needs_separator = bool(existing.rstrip())
    text = existing.rstrip()
    if needs_separator:
        text += "\n\n---\n\n"
    text += entry_markdown.rstrip() + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def entry_005_markdown(
    *,
    generated_at: str,
    fresh_pool_summary: str,
    data_acquisition_available: bool,
    tests_summary: str,
    pytest_summary: str,
    current_decision: str,
) -> str:
    """Canonical Ledger Entry 005 for the autonomous-loop change."""
    acquisition = (
        "credential available for automated GET-only acquisition"
        if data_acquisition_available
        else "automated acquisition is credential-gated (FNO_UPSTOX_ACCESS_TOKEN)"
    )
    fields = {
        "Recorded": generated_at,
        "Candidate": (
            "OUR-ALGO-004 (coherent single-slot reversal-flat-hold; PROMISING per Entry 004)."
        ),
        "Blocked operation": (
            "run protected-OOS validation of OUR-ALGO-004 B on 2025-10-06..2026-09-11."
        ),
        "Reason": (
            "the protected out-of-sample window 2025-10-06..2026-09-11 is already"
            " consumed exactly once for this lineage (iteration006: 17,412 bars / 233"
            " days, engine_runs=1; WS 7.16 walk-forward: 12,975 bars / 173 days with"
            " validation 9,387 bars, conclusion B). PROJECT_PLAN §17e.11 forbids a"
            " re-run and forbids relabelling the consumed interval as fresh OOS."
        ),
        "Evidence": (
            "runs/research/day_batch/iteration_006_protected_oos_n3.json;"
            " reports/model_performance/oos_confirmation.json;"
            " reports/walkforward/summary.json; docs/project_state.json."
        ),
        "Prohibited action": (
            "re-run on 2025-10-06..2026-09-11; relabel that interval as fresh OOS;"
            " weaken PROJECT_PLAN §17e.11 / WS 7.16 gates; change health to GREEN."
        ),
        "Available legitimate paths": (
            "A fresh untouched OOS data collected strictly after 2026-09-11"
            " (single-use; coverage 20 days / 1500 bars / 30 trades) then one"
            " validation run; B research of a separately permitted family over the"
            " pre-OOS domain (no OOS touched); C new pre-registered hypothesis set on"
            " the research domain. Data collection, registration, inspection,"
            " validation and promotion remain separate steps."
        ),
        "Human decision required": (
            "not at entry time — the loop waits explicitly for fresh OOS data"
            " (WAITING_FOR_FRESH_OOS_DATA) and states exactly what data remains"
            " required; a human-decision request (A/B/C) is raised only when no"
            " protocol-compliant path remains."
        ),
        "Loop behavior changed": (
            "a BLOCKED condition no longer silently halts candidate progression: the"
            " autonomous research/validation loop now returns one terminal token and"
            " either continues on a permitted path, waits with an explicit data"
            " requirement, or escalates with an A/B/C human-decision report."
        ),
        "Fresh-data pool": fresh_pool_summary,
        "Acquisition status": acquisition,
        "Tests": tests_summary,
        "Full pytest": pytest_summary,
        "Current algorithm state": (
            "PROMOTION=NO, ALGO READY=NO, algorithm health=RED, ALGORITHM SEARCH"
            " COMPLETE (Entry 004), final research candidate identified."
        ),
        "Current live gate state": "LIVE GATE CLOSED; no real order; paper-only.",
        "Decision": current_decision,
    }
    return format_entry(5, "AUTONOMOUS LOOP: BLOCKED-NO-LONGER-SILENT", fields)