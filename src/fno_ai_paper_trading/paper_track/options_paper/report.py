"""Report builders for the Phase 11 options paper simulation.

Mirrors the ``paper_track.report`` conventions (``paper_only`` flag plus a
rerun-comparable economic fingerprint via SHA-256 over stable JSON). Reports
are built purely from records — no environment, no network, no credentials.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal

from fno_ai_paper_trading.paper_track.options_paper.fill_model import FILL_MODEL_VERSION
from fno_ai_paper_trading.paper_track.options_paper.models import (
    LifecyclePhase,
    LifecycleRecord,
)
from fno_ai_paper_trading.paper_track.store import stable_dumps

__all__ = [
    "record_report",
    "build_daily_report",
    "build_cumulative_report",
    "report_fingerprint",
]


def report_fingerprint(payload: dict) -> str:
    """SHA-256 over the stable-JSON form of the economic content."""
    return hashlib.sha256(stable_dumps(payload).encode("utf-8")).hexdigest()


def record_report(record: LifecycleRecord) -> dict:
    """One lifecycle record rendered as an audit report (always paper-only)."""
    return {
        "paper_only": True,
        "fill_model_version": FILL_MODEL_VERSION,
        "record": record.to_dict(),
    }


def build_daily_report(records: tuple[LifecycleRecord, ...], day: date) -> dict:
    """Aggregate the paper lifecycle for one trading day."""
    closed = [
        r for r in records
        if r.phase in (LifecyclePhase.POSITION_CLOSED, LifecyclePhase.RECONCILED)
        and r.financials is not None
        and r.financials.close_day == day.isoformat()
    ]
    open_on_day = [
        r for r in records
        if r.open_position and r.decision_timestamp.date() == day
    ]
    rejected_on_day = [
        r for r in records if r.phase is LifecyclePhase.REJECTED and r.decision_timestamp.date() == day
    ]

    net_pnl = sum((r.financials.net_realized_pnl or Decimal("0")) for r in closed)
    gross_pnl = sum((r.financials.gross_realized_pnl or Decimal("0")) for r in closed)
    commissions = sum(
        (r.financials.entry_commission + (r.financials.exit_commission or Decimal("0")))
        for r in closed
    )
    held_seconds = [r.financials.holding_duration_seconds for r in closed if r.financials.holding_duration_seconds is not None]

    ledger = []
    for r in closed:
        ledger.append(
            {
                "record_id": r.record_id,
                "contract_key": r.contract_key,
                "quantity": r.quantity,
                "entry_fill": str(r.entry.fill_price),
                "exit_fill": str(r.exit.fill_price),
                "gross_realized_pnl": str(r.financials.gross_realized_pnl),
                "net_realized_pnl": str(r.financials.net_realized_pnl),
                "close_day": r.financials.close_day,
            }
        )

    payload = {
        "paper_only": True,
        "fill_model_version": FILL_MODEL_VERSION,
        "trading_date": day.isoformat(),
        "counts": {
            "rejected": len(rejected_on_day),
            "open": len(open_on_day),
            "closed": len(closed),
            "reconciled": sum(
                1 for r in closed if r.phase is LifecyclePhase.RECONCILED
            ),
        },
        "pnl": {
            "gross_realized_pnl": str(gross_pnl),
            "net_realized_pnl": str(net_pnl),
            "total_commissions": str(commissions),
        },
        "open_on_day": [r.contract_key for r in open_on_day],
        "limitations": sorted(
            {lim for r in records if r.decision_timestamp.date() == day for lim in r.limitations}
        ),
    }
    if held_seconds:
        payload["holding_duration_seconds"] = {
            "min": int(min(held_seconds)),
            "max": int(max(held_seconds)),
        }
    if ledger:
        payload["ledger"] = ledger
    envelope = {"aggregate": payload, "fingerprint": report_fingerprint(payload)}
    return envelope


def build_cumulative_report(records: tuple[LifecycleRecord, ...]) -> dict:
    """A full-account cumulative report across all recorded lifecycles."""
    closed = [r for r in records if r.phase in (LifecyclePhase.POSITION_CLOSED, LifecyclePhase.RECONCILED)]
    net = sum((r.financials.net_realized_pnl or Decimal("0")) for r in closed if r.financials)
    payload = {
        "paper_only": True,
        "fill_model_version": FILL_MODEL_VERSION,
        "built_at": datetime.min.isoformat(),
        "total_records": len(records),
        "closed": len(closed),
        "open": sum(1 for r in records if r.open_position),
        "rejected": sum(1 for r in records if r.phase is LifecyclePhase.REJECTED),
        "net_realized_pnl": str(net),
        "exposure_lines": [
            {
                "record_id": r.record_id,
                "contract_key": r.contract_key,
                "premium_exposure": str(r.entry.premium_exposure) if r.entry is not None else None,
                "phase": r.phase.value,
            }
            for r in sorted(records, key=lambda r: r.decision_timestamp)
        ],
    }
    envelope = {"aggregate": payload, "fingerprint": report_fingerprint(payload)}
    return envelope