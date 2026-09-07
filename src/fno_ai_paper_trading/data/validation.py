"""Data-quality validation for research OHLCV datasets.

Providers already validate individual bars (:class:`MarketPrice` rejects
structurally invalid OHLC, and the Upstox adapter rejects non-monotonic candle
timestamps). This module adds the cross-bar, dataset-level checks a researcher
needs before running an experiment:

* lexically broken rows (non-positive prices, inverted high/low, negative volume)
* non-chronological bars and duplicate timestamps
* timezone hygiene (a single, known timezone per series)
* cadence gaps when the expected bar interval is known

Validation is *report-only*: it never repairs or fabricates data. Each issue is
a ``ValidationIssue`` with a level (``error``/``warning``), a message, and the
bar index (where relevant) — a report with no errors means the dataset is safe
to feed into a backtest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fno_ai_paper_trading.models.market import MarketPrice

_DECIMAL_ZERO = Decimal("0")


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a dataset."""

    level: str  # "error" | "warning"
    message: str
    index: int | None = None

    def __str__(self) -> str:
        where = "" if self.index is None else f" (bar {self.index})"
        return f"[{self.level.upper()}]{where} {self.message}"


@dataclass(frozen=True)
class ValidationReport:
    """All issues found for one dataset plus an overall verdict."""

    issues: list[ValidationIssue]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def ok(self) -> bool:
        """True when there are no errors (warnings alone are acceptable)."""
        return not self.errors


def validate_bars(
    bars: list[MarketPrice],
    *,
    interval_minutes: int | None = None,
    allow_empty: bool = True,
) -> ValidationReport:
    """Validate a normalized OHLCV series and return a :class:`ValidationReport`.

    Args:
        bars: chronological :class:`MarketPrice` series (oldest first).
        interval_minutes: expected bar cadence in minutes (e.g. 1 for 1-minute
            bars, 1440 for daily). When given, irregular spacing is flagged as a
            warning (gaps are common around holidays, so they are never errors).
        allow_empty: when False, an empty series is reported as an error.
    """
    issues: list[ValidationIssue] = []
    if not bars:
        if not allow_empty:
            issues.append(ValidationIssue("error", "no bars in dataset"))
        return ValidationReport(issues=issues)

    _validate_prices(bars, issues)

    # Ordering/duplicate/spacing checks strip timezone info so a mixed
    # aware/naive series (flagged as a warning below) cannot crash the report.
    for index, (previous, current) in enumerate(zip(bars, bars[1:])):
        prev_ts = _naive(previous.timestamp)
        curr_ts = _naive(current.timestamp)
        if curr_ts < prev_ts:
            issues.append(
                ValidationIssue(
                    "error",
                    f"timestamp out of order: {previous.timestamp.isoformat()} after "
                    f"{current.timestamp.isoformat()}",
                )
            )
        if curr_ts == prev_ts:
            issues.append(
                ValidationIssue(
                    "error",
                    f"duplicate timestamp {current.timestamp.isoformat()}",
                    index=index + 1,
                )
            )
        if interval_minutes is not None:
            _check_spacing(prev_ts, curr_ts, interval_minutes, issues, index + 1)

    aware = [bar for bar in bars if bar.timestamp.tzinfo is not None]
    naive = [bar for bar in bars if bar.timestamp.tzinfo is None]
    if aware and naive:
        issues.append(
            ValidationIssue(
                "warning",
                "timestamps mix timezone-aware and naive datetimes; "
                "normalize to a single naive market timezone before research",
            )
        )

    return ValidationReport(issues=issues)


def _naive(ts: datetime) -> datetime:
    """Strip ``tzinfo`` for comparisons; the tz-mix is reported separately."""
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def _validate_prices(bars: list[MarketPrice], issues: list[ValidationIssue]) -> None:
    for index, bar in enumerate(bars):
        if bar.close <= _DECIMAL_ZERO:
            issues.append(ValidationIssue("error", "non-positive close", index=index))
        if bar.open <= _DECIMAL_ZERO:
            issues.append(ValidationIssue("error", "non-positive open", index=index))
        if bar.high <= _DECIMAL_ZERO:
            issues.append(ValidationIssue("error", "non-positive high", index=index))
        if bar.low <= _DECIMAL_ZERO:
            issues.append(ValidationIssue("error", "non-positive low", index=index))
        if bar.high < bar.low:
            issues.append(ValidationIssue("error", "high below low", index=index))
        if bar.high < max(bar.open, bar.close):
            issues.append(
                ValidationIssue("error", "high below open/close", index=index)
            )
        if bar.low > min(bar.open, bar.close):
            issues.append(ValidationIssue("error", "low above open/close", index=index))
        if bar.volume < 0:
            issues.append(ValidationIssue("error", "negative volume", index=index))
        if bar.open_interest is not None and bar.open_interest < 0:
            issues.append(ValidationIssue("error", "negative open_interest", index=index))


def _check_spacing(
    previous: datetime,
    current: datetime,
    interval_minutes: int,
    issues: list[ValidationIssue],
    index: int,
) -> None:
    gap_minutes = (current - previous).total_seconds() / 60.0
    if gap_minutes <= 0:
        return  # already reported by the ordering/duplicate checks
    tolerance = interval_minutes * 1.5
    if gap_minutes > interval_minutes * 2 and gap_minutes - interval_minutes > tolerance:
        # Flag only clearly irregular spacing (e.g. a 5x gap) to avoid noise
        # from ordinary holiday/weekend pauses.
        if gap_minutes >= interval_minutes * 5:
            issues.append(
                ValidationIssue(
                    "warning",
                    f"large gap of {gap_minutes:g} minutes since previous bar "
                    f"(expected {interval_minutes} minutes)",
                    index=index,
                )
            )


def format_report(report: ValidationReport) -> str:
    """Render a :class:`ValidationReport` as a short human-readable summary."""
    if report.ok:
        return "OK"
    lines = [f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"]
    for issue in report.issues:
        lines.append(str(issue))
    return "\n".join(lines)