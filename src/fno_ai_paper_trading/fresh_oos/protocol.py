"""Canonical fresh-OOS protocol constants and deterministic outcomes.

Fresh OOS = NIFTY 50 5-minute bars recorded **strictly after** the lineage's
consumed protected window boundary (``FRESH_OOS_BOUNDARY`` = 2026-09-11). The
collector refuses any bar dated on or before that boundary and records it as a
protocol violation instead of silently filtering it.

This module is pure data definitions plus deterministic computation. It never
touches the network, never imports strategies, and never writes files, so every
consumer (collector, scheduler, status CLI, tests) shares one source of truth.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Mapping

PROTOCOL_NAME = "fresh_oos_single_use"
PROTOCOL_VERSION = 1
SCHEMA_VERSION = "1"

# The consumed protected OOS window for the OUR-ALGO-004 lineage ends on
# 2026-09-11 (inclusive): ``iteration006`` + the WS 7.16 walk-forward
# confirmation. Fresh OOS must start strictly after this boundary.
FRESH_OOS_BOUNDARY = date(2026, 9, 11)

# Coverage thresholds for a single-use fresh OOS window. Trades are NOT counted
# here -- ``MIN_TRADES`` is evaluated later by the separate single-use
# validation job, never by the collector.
MIN_TRADING_DAYS = 20
MIN_BARS = 1500
MIN_TRADES = 30

# NIFTY 50 5-minute session convention (verified against the real stored
# datasets): one complete index day = 75 candles stamped 09:15 .. 15:25 IST at
# a 5-minute cadence. The 15:25 candle covers the final 09:25..15:30 leg.
INTERVAL = "5m"
EXPECTED_BARS_PER_FULL_DAY = 75
SESSION_FIRST_TIME = time(9, 15)
SESSION_LAST_TIME = time(15, 25)
SESSION_END_EXCLUSIVE = time(15, 30)

# --------------------------------------------------------------------------- #
# Run statuses
# --------------------------------------------------------------------------- #
STATUS_SUCCESS = "SUCCESS"
STATUS_NO_NEW_DATA = "NO_NEW_DATA"
STATUS_NO_DATA = "NO_DATA"          # provider returned zero bars for a session date
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_AUTH_REQUIRED = "AUTH_REQUIRED"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_SOURCE_ERROR = "SOURCE_ERROR"
STATUS_DATA_INVALID = "DATA_INVALID"
STATUS_DATA_CONFLICT = "DATA_CONFLICT"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"
STATUS_PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
STATUS_LOCKED = "LOCKED"

ALL_STATUSES = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_NO_NEW_DATA,
        STATUS_NO_DATA,
        STATUS_INCOMPLETE,
        STATUS_AUTH_REQUIRED,
        STATUS_RATE_LIMITED,
        STATUS_SOURCE_ERROR,
        STATUS_DATA_INVALID,
        STATUS_DATA_CONFLICT,
        STATUS_NETWORK_ERROR,
        STATUS_PROTOCOL_VIOLATION,
        STATUS_LOCKED,
    }
)

# Per-day (pool) statuses: what happened for a specific trading date.
POOL_ACQUIRED = "ACQUIRED"                     # newly stored from the provider
POOL_NOOP_DEDICATED = "NOOP"                   # already stored, same hash
POOL_NOOP_ESTABLISHED = "NOOP_ESTABLISHED"     # already present in datasets/, same hash
POOL_CONFLICT = "CONFLICT"                     # present but a different hash
POOL_VERIFY_INVALID = "VERIFY_INVALID"         # stored artifacts fail verification

# Severity order (higher = worse) used to pick a run's final status
# deterministically when several dates were processed in one pass.
_SEVERITY: dict[str, int] = {
    STATUS_SUCCESS: 0,
    STATUS_NO_NEW_DATA: 1,
    STATUS_NO_DATA: 2,
    STATUS_INCOMPLETE: 3,
    STATUS_SOURCE_ERROR: 4,
    STATUS_NETWORK_ERROR: 4,
    STATUS_RATE_LIMITED: 5,
    STATUS_DATA_INVALID: 6,
    STATUS_DATA_CONFLICT: 7,
    STATUS_AUTH_REQUIRED: 8,
    STATUS_PROTOCOL_VIOLATION: 9,
    STATUS_LOCKED: 10,
}


def worst_status(statuses: list[str]) -> str:
    """Deterministic worst-of severity over a set of per-date statuses."""
    if not statuses:
        return STATUS_NO_NEW_DATA
    return max(statuses, key=lambda status: _SEVERITY.get(status, _SEVERITY[STATUS_SOURCE_ERROR]))


def new_run_id(now: datetime) -> str:
    """Deterministic-ish run id: ``<utc-ish timestamp>_<8 hex chars>``."""
    stamp = now.strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def utc_now_iso(dt: datetime) -> str:
    """ISO-8601 timestamp with seconds precision (naive clock kept naive)."""
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds") + "Z"
    return dt.isoformat(timespec="seconds")


@dataclass(frozen=True)
class RunOutcome:
    """Result of one collection pass (one scheduled/``--once`` invocation)."""

    run_id: str
    started_at: str
    ended_at: str
    status: str
    dates_attempted: tuple[str, ...]
    accepted: tuple[str, ...]
    noop: tuple[str, ...]
    conflicts: tuple[str, ...]
    errors: Mapping[str, str]
    message: str
    retries: int = 0

    @property
    def ok(self) -> bool:
        """True when the run did not hit a hard failure (success/coal/no-data)."""
        return self.status in (STATUS_SUCCESS, STATUS_NO_NEW_DATA, STATUS_NO_DATA)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "dates_attempted": list(self.dates_attempted),
            "accepted": list(self.accepted),
            "noop": list(self.noop),
            "conflicts": list(self.conflicts),
            "errors": dict(self.errors),
            "message": self.message,
            "retries": self.retries,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "RunOutcome":
        return cls(
            run_id=str(data["run_id"]),
            started_at=str(data["started_at"]),
            ended_at=str(data["ended_at"]),
            status=str(data["status"]),
            dates_attempted=tuple(str(x) for x in data.get("dates_attempted", [])),
            accepted=tuple(str(x) for x in data.get("accepted", [])),
            noop=tuple(str(x) for x in data.get("noop", [])),
            conflicts=tuple(str(x) for x in data.get("conflicts", [])),
            errors=dict(data.get("errors") or {}),
            message=str(data.get("message", "")),
            retries=int(data.get("retries", 0)),
        )


@dataclass(frozen=True)
class ReadinessReport:
    """Data readiness of the fresh-OOS pool (computed, never guessed).

    ``label`` is ``DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION`` when the
    collector has stored enough *coverage* (trading days + bars). It is never
    ``VALIDATION_READY``: trade-count sufficiency is only knowable by the
    separate single-use validation job, which the collector never runs.
    """

    data_ready: bool
    days: int
    bars: int
    missing_days: int
    missing_bars: int
    min_trades: int
    label: str
    reasons: tuple[str, ...]
    validation_run: bool = field(default=False)

    @property
    def ready_message(self) -> str:
        if self.label == "DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION":
            return (
                self.label
                + " (coverage met; the >=30-trade requirement is evaluated by the "
                "separate single-use validation job, which the collector never runs)"
            )
        return self.label

    def to_dict(self) -> dict[str, object]:
        return {
            "data_ready": self.data_ready,
            "days": self.days,
            "bars": self.bars,
            "missing_days": self.missing_days,
            "missing_bars": self.missing_bars,
            "min_trades": self.min_trades,
            "label": self.label,
            "reasons": list(self.reasons),
            "validation_run": self.validation_run,
        }


_READY_LABEL = "DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION"


def compute_readiness(
    days: int,
    bars: int,
    *,
    min_days: int = MIN_TRADING_DAYS,
    min_bars: int = MIN_BARS,
    min_trades: int = MIN_TRADES,
) -> ReadinessReport:
    """Compute data readiness from accumulated coverage.

    Only trading-day and bar thresholds matter here. The trade-count threshold
    (``min_trades``) is recorded and reported but never satisfied by the
    collector; it belongs to the later validation job.
    """
    days = max(0, int(days))
    bars = max(0, int(bars))
    data_ready = days >= min_days and bars >= min_bars
    reasons: list[str] = []
    if days < min_days:
        reasons.append(f"needs {min_days} trading days, has {days}")
    if bars < min_bars:
        reasons.append(f"needs {min_bars} bars, has {bars}")
    if not reasons:
        reasons.append(
            "coverage met; candidate is ready for single-use fresh-OOS validation "
            "(separate controlled job, trade count still to be evaluated)"
        )
    return ReadinessReport(
        data_ready=data_ready,
        days=days,
        bars=bars,
        missing_days=max(0, min_days - days),
        missing_bars=max(0, min_bars - bars),
        min_trades=int(min_trades),
        label=_READY_LABEL if data_ready else "NOT_READY",
        reasons=tuple(reasons),
    )