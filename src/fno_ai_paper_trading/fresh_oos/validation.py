"""Per-day completeness and integrity validation for fresh-OOS 5m candles.

Validation is report-only: it never repairs, never filters and never fabricates
data. A day is accepted only when every check passes; otherwise the collector
records an issue and stores nothing (atomic persistence guarantee).

Checks (all on one target trading day ``day``):

* non-empty series;
* every timestamp parses to a naive IST datetime on ``day``;
* no bar dated on/before the fresh-OOS boundary (2026-09-11 inclusive);
* timestamps inside the 09:15..15:25 session (5-minute alignment);
* structurally valid OHLC (finite numeric, non-negative, high>=max(o,c),
  low<=min(o,c), high>=low), integer non-negative volume, optional
  non-negative open interest;
* strictly increasing, duplicate-free timestamps with no gaps at the 5-minute
  cadence;
* full-session coverage: exactly ``expected_bars`` candles spanning the whole
  session -- a shorter day is ``INCOMPLETE`` (half-acquired sessions must not
  enter the immutable pool).

The full-day convention (75 candles per NIFTY 50 index day, 09:15..15:25) was
verified against the real stored datasets, not assumed; the function accepts
``expected_bars`` so tests can exercise partial-day policy at small scale.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from fno_ai_paper_trading.fresh_oos.protocol import (
    EXPECTED_BARS_PER_FULL_DAY,
    FRESH_OOS_BOUNDARY,
    SESSION_FIRST_TIME,
    SESSION_LAST_TIME,
)
from fno_ai_paper_trading.models.market import MarketPrice

# Issue codes (structured, so tests and the collector can classify precisely).
CODE_EMPTY = "EMPTY"
CODE_BAD_TIMESTAMP = "BAD_TIMESTAMP"
CODE_AWARE_TZ = "AWARE_TZ"
CODE_WRONG_DAY = "WRONG_DAY"
CODE_BOUNDARY = "BOUNDARY"
CODE_OUT_OF_SESSION = "OUT_OF_SESSION"
CODE_CADENCE = "CADENCE"
CODE_OHLC = "OHLC"
CODE_ORDER = "ORDER"
CODE_DUPLICATE = "DUPLICATE"
CODE_GAP = "GAP"
CODE_COVERAGE_INCOMPLETE = "COVERAGE_INCOMPLETE"
CODE_COVERAGE_OVERFLOW = "COVERAGE_OVERFLOW"

_ZERO = Decimal("0")


@dataclass(frozen=True)
class DayIssue:
    """One validation problem, with a stable machine-readable code."""

    code: str
    message: str
    index: int | None = None

    def __str__(self) -> str:
        where = "" if self.index is None else f" (bar {self.index})"
        return f"[{self.code}]{where} {self.message}"


@dataclass(frozen=True)
class DayValidationResult:
    """All issues found for one day plus an overall verdict and classification."""

    day: date
    issues: tuple[DayIssue, ...]
    num_bars: int
    target_bars: int

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def status(self) -> str:
        """Closed-set status: PROTOCOL_VIOLATION / INCOMPLETE / DATA_INVALID.

        Any bar on or before the fresh-OOS boundary is a protocol violation
        (never silently filtered). A day with fewer bars than the expected
        session is INCOMPLETE; every other structural failure is DATA_INVALID.
        """
        from fno_ai_paper_trading.fresh_oos.protocol import (  # local import avoids cycles
            STATUS_DATA_INVALID,
            STATUS_INCOMPLETE,
            STATUS_PROTOCOL_VIOLATION,
        )

        if self.ok:
            return "VALID"
        if CODE_BOUNDARY in self.codes:
            return STATUS_PROTOCOL_VIOLATION
        if self.num_bars < self.target_bars and any(
            code in (CODE_COVERAGE_INCOMPLETE, CODE_EMPTY) for code in self.codes
        ):
            return STATUS_INCOMPLETE
        return STATUS_DATA_INVALID


def expected_session_times(day: date, expected_bars: int = EXPECTED_BARS_PER_FULL_DAY) -> list[datetime]:
    """The exact bar timestamps of a complete session: 09:15 + 5min * i."""
    start = datetime(day.year, day.month, day.day, SESSION_FIRST_TIME.hour, SESSION_FIRST_TIME.minute)
    return [start + timedelta(minutes=5 * i) for i in range(expected_bars)]


def validate_5m_rows(
    rows: list[Mapping[str, Any] | MarketPrice],
    day: date,
    *,
    boundary: date = FRESH_OOS_BOUNDARY,
    expected_bars: int = EXPECTED_BARS_PER_FULL_DAY,
) -> DayValidationResult:
    """Validate a day of 5-minute bars given as dict-like rows or MarketPrice.

    ``rows`` may be ``MarketPrice`` objects (trivially valid structurally) or
    raw dicts (so malformed / NaN / inverted-OHLC rows can be rejected in
    tests). Returns a report; the caller stores nothing when ``ok`` is False.
    """
    issues: list[DayIssue] = []
    bars = [row for row in rows] if rows else []
    if not bars:
        return DayValidationResult(
            day=day,
            issues=(DayIssue(CODE_EMPTY, "no bars for day"),),
            num_bars=0,
            target_bars=expected_bars,
        )

    parsed: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, int, int | None]] = []
    for index, row in enumerate(bars):
        ts = _timestamp_of(row, index, issues)
        if ts is None:
            continue
        if ts.tzinfo is not None:
            issues.append(
                DayIssue(
                    CODE_AWARE_TZ,
                    f"bar {index} timestamp is timezone-aware; fresh OOS must be naive IST",
                    index=index,
                )
            )
            ts = ts.replace(tzinfo=None)
        if ts.date() != day:
            issues.append(
                DayIssue(
                    CODE_WRONG_DAY,
                    f"bar dated {ts.date().isoformat()} is not the target day {day.isoformat()}",
                    index=index,
                )
            )
        if ts.date() <= boundary:
            issues.append(
                DayIssue(
                    CODE_BOUNDARY,
                    f"bar dated {ts.date().isoformat()} is on or before the fresh-OOS "
                    f"boundary {boundary.isoformat()} (strictly-after required)",
                    index=index,
                )
            )
        clock = ts.time()
        if clock < SESSION_FIRST_TIME or clock > SESSION_LAST_TIME:
            issues.append(
                DayIssue(
                    CODE_OUT_OF_SESSION,
                    f"bar at {clock.isoformat()} is outside the {SESSION_FIRST_TIME.isoformat()}"
                    f"..{SESSION_LAST_TIME.isoformat()} session window",
                    index=index,
                )
            )
        minutes = ts.hour * 60 + ts.minute
        if minutes % 5 != 0:
            issues.append(
                DayIssue(CODE_CADENCE, f"bar at {clock.isoformat()} is not 5-minute aligned", index=index)
            )

        ohlc = _ohlc_of(row, index, issues)
        if ohlc is None:
            continue
        volume = _volume_of(row, index, issues)
        oi = _oi_of(row, index, issues)
        parsed.append((ts, ohlc[0], ohlc[1], ohlc[2], ohlc[3], volume, oi))

    if not parsed:
        return DayValidationResult(
            day=day,
            issues=tuple(issues) or (DayIssue(CODE_BAD_TIMESTAMP, "no parseable bars"),),
            num_bars=0,
            target_bars=expected_bars,
        )

    # Ordering, duplicates, cadence gaps.
    for index, (previous, current) in enumerate(zip(parsed, parsed[1:])):
        if current[0] < previous[0]:
            issues.append(
                DayIssue(CODE_ORDER, "timestamps out of order", index=index + 1)
            )
        if current[0] == previous[0]:
            issues.append(
                DayIssue(CODE_DUPLICATE, f"duplicate timestamp {current[0].isoformat()}", index=index + 1)
            )
        gap_minutes = (current[0] - previous[0]).total_seconds() / 60.0
        if 0 < gap_minutes != 5:
            issues.append(DayIssue(CODE_GAP, f"unexplained gap of {gap_minutes:g} minutes", index=index + 1))

    # Full-session coverage: exact set match, no more and no less.
    expected = expected_session_times(day, expected_bars)
    actual = [item[0] for item in parsed]
    if len(actual) < expected_bars:
        issues.append(
            DayIssue(
                CODE_COVERAGE_INCOMPLETE,
                f"incomplete day: expected {expected_bars} bars, got {len(actual)}",
            )
        )
    elif len(actual) > expected_bars:
        issues.append(
            DayIssue(CODE_COVERAGE_OVERFLOW, f"more bars than the {expected_bars} full session", )
        )
    elif set(actual) != set(expected):
        issues.append(
            DayIssue(CODE_COVERAGE_INCOMPLETE, "session coverage does not match the canonical 5m grid", )
        )

    return DayValidationResult(
        day=day,
        issues=tuple(issues),
        num_bars=len(actual),
        target_bars=expected_bars,
    )


def validate_5m_day(
    bars: list[MarketPrice],
    day: date,
    *,
    boundary: date = FRESH_OOS_BOUNDARY,
    expected_bars: int = EXPECTED_BARS_PER_FULL_DAY,
) -> DayValidationResult:
    """Validate a day of normalized :class:`MarketPrice` bars."""
    return validate_5m_rows(list(bars), day, boundary=boundary, expected_bars=expected_bars)


def _timestamp_of(row: Mapping[str, Any] | MarketPrice, index: int, issues: list[DayIssue]) -> datetime | None:
    value = row.get("timestamp") if isinstance(row, Mapping) else getattr(row, "timestamp", None)
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            issues.append(DayIssue(CODE_BAD_TIMESTAMP, f"unparseable timestamp {value!r}", index=index))
            return None
    issues.append(DayIssue(CODE_BAD_TIMESTAMP, f"missing timestamp in bar {index}", index=index))
    return None


def _ohlc_of(
    row: Mapping[str, Any] | MarketPrice, index: int, issues: list[DayIssue]
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    if isinstance(row, Mapping):
        values = [row.get(name) for name in ("open", "high", "low", "close")]
    else:
        values = [getattr(row, name, None) for name in ("open", "high", "low", "close")]
    parsed: list[Decimal] = []
    for name, value in zip(("open", "high", "low", "close"), values):
        if isinstance(value, Decimal):
            parsed.append(value)
            continue
        try:
            parsed.append(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            issues.append(DayIssue(CODE_OHLC, f"non-numeric {name} {value!r}", index=index))
            return None
    for name, value in zip(("open", "high", "low", "close"), parsed):
        if not value.is_finite():
            issues.append(DayIssue(CODE_OHLC, f"non-finite {name} {value}", index=index))
            return None
        if value < _ZERO:
            issues.append(DayIssue(CODE_OHLC, f"negative {name} {value}", index=index))
            return None
    open_, high, low, close = parsed
    if high < max(open_, close):
        issues.append(DayIssue(CODE_OHLC, "high below max(open, close)", index=index))
    if low > min(open_, close):
        issues.append(DayIssue(CODE_OHLC, "low above min(open, close)", index=index))
    if high < low:
        issues.append(DayIssue(CODE_OHLC, "high below low", index=index))
    return open_, high, low, close


def _volume_of(row: Mapping[str, Any] | MarketPrice, index: int, issues: list[DayIssue]) -> int:
    value = row.get("volume") if isinstance(row, Mapping) else getattr(row, "volume", 0)
    if value is None:
        return 0
    try:
        num = int(value)
    except (TypeError, ValueError):
        issues.append(DayIssue(CODE_OHLC, f"non-integer volume {value!r}", index=index))
        return 0
    if num < 0:
        issues.append(DayIssue(CODE_OHLC, "negative volume", index=index))
    return num


def _oi_of(row: Mapping[str, Any] | MarketPrice, index: int, issues: list[DayIssue]) -> int | None:
    value = row.get("open_interest") if isinstance(row, Mapping) else getattr(row, "open_interest", None)
    if value is None or value == "":
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        issues.append(DayIssue(CODE_OHLC, f"non-integer open_interest {value!r}", index=index))
        return None
    if num < 0:
        issues.append(DayIssue(CODE_OHLC, "negative open_interest", index=index))
    return num