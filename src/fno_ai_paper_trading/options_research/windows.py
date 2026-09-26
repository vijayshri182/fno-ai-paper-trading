"""Leakage and out-of-sample protection helpers (Phase 12).

Reuses the repository's canonical protected-window constants:

* the protected OOS window ``2025-10-06 .. 2026-09-11`` (consumed once by the
  OUR-ALGO-004 lineage: iteration-006 + WS 7.16 confirmation);
* the fresh boundary ``FRESH_OOS_BOUNDARY`` (= 2026-09-11) from
  :mod:`fno_ai_paper_trading.fresh_oos.protocol`.

The helpers are pure and deterministic: chronological split integrity, a
no-lookahead check (features/snapshots must be at or before the decision time),
and window classification with protected-window refusal. Nothing here reads
files, clocks or networks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping

from fno_ai_paper_trading.fresh_oos.protocol import FRESH_OOS_BOUNDARY  # noqa: F401  # re-exported

PROTECTED_OOS_START = date(2025, 10, 6)
PROTECTED_OOS_END = FRESH_OOS_BOUNDARY  # 2026-09-11 (inclusive)

# Defensive invariant: the fresh boundary must equal the protected end, so the
# two registries can never drift apart.
assert FRESH_OOS_BOUNDARY == date(2026, 9, 11)

WINDOW_RESEARCH = "RESEARCH"
WINDOW_PROTECTED = "PROTECTED"
WINDOW_FRESH = "FRESH"

CODE_SPLIT_ORDER = "SPLIT_ORDER"
CODE_SPLIT_INVERTED = "SPLIT_INVERTED"
CODE_SPLIT_OVERLAP = "SPLIT_OVERLAP"
CODE_SPLIT_PROTECTED = "SPLIT_PROTECTED"
CODE_LOOKAHEAD = "LOOKAHEAD"
CODE_PROTECTED_ACCESS = "PROTECTED_ACCESS"
CODE_CONSUMED = "CONSUMED"

_P_ISSUE_CODES = frozenset(
    {CODE_SPLIT_ORDER, CODE_SPLIT_INVERTED, CODE_SPLIT_OVERLAP, CODE_SPLIT_PROTECTED}
)


@dataclass(frozen=True)
class SplitIssue:
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class SplitPlan:
    development_start: date
    development_end: date
    validation_start: date
    validation_end: date

    def as_dict(self) -> dict[str, str]:
        return {
            "development_start": self.development_start.isoformat(),
            "development_end": self.development_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
        }


@dataclass(frozen=True)
class SplitAssessment:
    plan: SplitPlan
    issues: tuple[SplitIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.as_dict(),
            "ok": self.ok,
            "issues": [{"code": i.code, "message": i.message} for i in self.issues],
            "protected_oos": {
                "start": PROTECTED_OOS_START.isoformat(),
                "end": PROTECTED_OOS_END.isoformat(),
            },
        }


def validate_split(plan: SplitPlan) -> SplitAssessment:
    """Chronological split integrity: dev < validation < protected OOS."""
    issues: list[SplitIssue] = []
    dev_start, dev_end = plan.development_start, plan.development_end
    val_start, val_end = plan.validation_start, plan.validation_end

    if dev_start >= dev_end:
        issues.append(SplitIssue(CODE_SPLIT_INVERTED, "development_end must be after development_start"))
    if val_start >= val_end:
        issues.append(SplitIssue(CODE_SPLIT_INVERTED, "validation_end must be after validation_start"))
    if dev_end >= val_start:
        issues.append(SplitIssue(CODE_SPLIT_ORDER, "development window overlaps or is not before validation_start"))
    if val_start > val_end:
        issues.append(SplitIssue(CODE_SPLIT_INVERTED, "validation_start after validation_end"))
    # validation must never touch the protected OOS window
    if val_start < PROTECTED_OOS_START <= val_end or (val_start <= PROTECTED_OOS_START <= val_end):
        issues.append(
            SplitIssue(
                CODE_SPLIT_PROTECTED,
                f"validation window touches the protected OOS window {PROTECTED_OOS_START}..{PROTECTED_OOS_END}",
            )
        )
    if val_start >= PROTECTED_OOS_START:
        issues.append(
            SplitIssue(
                CODE_SPLIT_PROTECTED,
                f"validation_start on/inside the protected OOS window (starts {PROTECTED_OOS_START})",
            )
        )
    return SplitAssessment(plan=plan, issues=tuple(issues))


def classify_day(day: date) -> str:
    """Classify one day into RESEARCH / PROTECTED / FRESH."""
    if day < PROTECTED_OOS_START:
        return WINDOW_RESEARCH
    if day <= PROTECTED_OOS_END:
        return WINDOW_PROTECTED
    return WINDOW_FRESH


def collect_protected_days(days: tuple[date, ...]) -> tuple[date, ...]:
    """Return the subset of days lying inside the protected OOS window."""
    return tuple(day for day in days if PROTECTED_OOS_START <= day <= PROTECTED_OOS_END)


@dataclass(frozen=True)
class LookaheadAssessment:
    """Result of the no-lookahead check for one decision instant."""

    decision_ts: datetime
    inputs: Mapping[str, datetime]
    violations: tuple[tuple[str, datetime], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_ts": self.decision_ts.isoformat(),
            "inputs": {k: v.isoformat() for k, v in self.inputs.items()},
            "violations": [(k, v.isoformat()) for k, v in self.violations],
            "ok": self.ok,
        }


def check_no_lookahead(decision_ts: datetime, inputs: Mapping[str, datetime]) -> LookaheadAssessment:
    """Every input used at ``decision_ts`` must be known at or before it.

    A data timestamp strictly after the decision time is a look-ahead
    violation; same-instant data is allowed (data available at the decision).
    """
    violations = tuple(
        (label, ts) for label, ts in sorted(inputs.items()) if ts > decision_ts
    )
    return LookaheadAssessment(decision_ts=decision_ts, inputs=dict(inputs), violations=violations)


class ProtectedOosRefusal(ValueError):
    """Raised when a caller tries to consume the protected OOS window without a
    registered single-use consumption record."""


@dataclass(frozen=True)
class ConsumedWindowRegistry:
    """Read-only view of already-consumed windows (defence in depth).

    Mirrors the repository rule that the protected window was consumed once
    (iteration-006 + WS 7.16) and fresh windows are single-use: a second
    consumption attempt is refused.
    """

    consumed: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def consumption_of(self, day: date) -> str | None:
        key = self._window_key(day)
        record = self.consumed.get(key)
        return record.get("fingerprint") if record else None

    def assert_unconsumed_fresh(self, day: date) -> None:
        """Refuse re-using an already-consumed window for a *new* single use."""
        if day <= PROTECTED_OOS_END:
            if PROTECTED_OOS_START <= day:
                raise ProtectedOosRefusal(
                    f"{day.isoformat()} lies inside the consumed protected OOS window "
                    f"{PROTECTED_OOS_START}..{PROTECTED_OOS_END}; protected windows are "
                    "single-use and already consumed (iteration-006 + WS 7.16)"
                )
        key = self._window_key(day)
        if key in self.consumed:
            raise ProtectedOosRefusal(
                f"window {key} is already consumed (single-use); re-reading it would "
                "contaminate fresh out-of-sample evidence"
            )

    @staticmethod
    def _window_key(day: date) -> str:
        if PROTECTED_OOS_START <= day <= PROTECTED_OOS_END:
            return "protected_2025_10_06__2026_09_11"
        return f"day_{day.isoformat()}"


def verify_no_protected_reuse(days: tuple[date, ...]) -> tuple[date, ...]:
    """List any protected-window days in ``days`` (caller must refuse them)."""
    return collect_protected_days(days)


__all__ = [
    "PROTECTED_OOS_START",
    "PROTECTED_OOS_END",
    "FRESH_OOS_BOUNDARY",
    "WINDOW_RESEARCH",
    "WINDOW_PROTECTED",
    "WINDOW_FRESH",
    "SplitPlan",
    "SplitAssessment",
    "validate_split",
    "classify_day",
    "collect_protected_days",
    "verify_no_protected_reuse",
    "LookaheadAssessment",
    "check_no_lookahead",
    "ProtectedOosRefusal",
    "ConsumedWindowRegistry",
]