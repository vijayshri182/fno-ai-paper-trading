"""Data coverage and quality auditing for a stored research dataset.

Answers "what exactly did we evaluate on": the provider, exact date range,
bar count, canonical hash, validation verdict, trading-day calendar for the
covered window, per-day bar count, and candidate gaps. Candidate gaps are
weekday dates within the range that carry no bars; because the repo's market
calendar only encodes the current year's NSE holidays, older official holidays
are counted as gaps too — they are labelled as *candidates*, not confirmed
missing data.

Report-only: this module never modifies or repairs data.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from fno_ai_paper_trading.data.dataset_store import StoredDataset
from fno_ai_paper_trading.data.validation import ValidationReport, validate_bars
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.utils.functions import positive_int


@dataclass(frozen=True)
class CoverageReport:
    """Coverage and quality facts for one dataset."""

    name: str
    path: str
    provider: str
    interval: str
    instrument: str
    start_date: date
    end_date: date
    num_bars: int
    data_hash: str
    validation: ValidationReport
    num_trading_days: int
    weekday_days: int
    median_bars_per_day: int
    mode_bars_per_day: int
    bars_per_day_histogram: dict[int, int] = field(default_factory=dict)
    days_without_bars: list[str] = field(default_factory=list)
    partial_days: list[str] = field(default_factory=list)
    outlier_notes: list[str] = field(default_factory=list)

    @property
    def missing_weekday_days(self) -> int:
        return len(self.days_without_bars)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "provider": self.provider,
            "interval": self.interval,
            "instrument": self.instrument,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "num_bars": self.num_bars,
            "data_hash": self.data_hash,
            "validation": {
                "ok": self.validation.ok,
                "num_errors": len(self.validation.errors),
                "num_warnings": len(self.validation.warnings),
                "issues": [
                    {"level": i.level, "message": i.message, "index": i.index}
                    for i in self.validation.issues
                ],
            },
            "num_trading_days": self.num_trading_days,
            "weekday_days_in_range": self.weekday_days,
            "median_bars_per_day": self.median_bars_per_day,
            "mode_bars_per_day": self.mode_bars_per_day,
            "bars_per_day_histogram": {
                day.isoformat() if isinstance(day, date) else str(day): count
                for day, count in self.bars_per_day_histogram.items()
            },
            "candidate_missing_weekday_days": self.days_without_bars,
            "candidate_missing_weekday_days_count": self.missing_weekday_days,
            "partial_days": self.partial_days,
            "outlier_notes": self.outlier_notes,
        }


def _weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def build_coverage_report(
    dataset: StoredDataset,
    *,
    interval_minutes: int | None = None,
    expected_bars_per_day: int | None = None,
    max_gap_samples: int = 12,
) -> CoverageReport:
    """Produce a :class:`CoverageReport` for a loaded ``StoredDataset``."""
    name = dataset.path.name
    bars: list[MarketPrice] = dataset.bars
    metadata = dataset.metadata
    validation = validate_bars(bars, interval_minutes=interval_minutes)

    start_date = bars[0].timestamp.date()
    end_date = bars[-1].timestamp.date()

    days: list[date] = []
    counts: Counter = Counter()
    for bar in bars:
        day = bar.timestamp.date()
        if not days or days[-1] != day:
            days.append(day)
        counts[day] += 1

    per_day = [counts[d] for d in days]
    hist = {day: counts[day] for day in days}
    num_bars = positive_int(len(bars), "num_bars")
    median = sorted(per_day)[len(per_day) // 2] if per_day else 0
    mode = int(counts.most_common(1)[0][1]) if counts else 0

    weekday_days = _weekdays(start_date, end_date)
    weekday_set = set(weekday_days)
    present_set = set(days)

    expected = expected_bars_per_day or (median if interval_minutes else 1)

    missing_days = sorted(weekday_set - present_set)
    missing: list[str] = []
    for day in missing_days:
        missing.append(day.isoformat())
        if len(missing) >= max_gap_samples:
            break

    partial: list[str] = []
    notes: list[str] = []
    for day in sorted(present_set):
        count = counts[day]
        if count != expected:
            partial.append(f"{day.isoformat()} ({count}/{expected})")
    if partial:
        notes.append(
            f"{len(partial)} day(s) diverge from the expected {expected} bars/day "
            f"(first {max_gap_samples} listed)"
        )
    if interval_minutes is not None and interval_minutes == 5:
        if mode not in (74, 75, 76):
            notes.append(f"modal bars/day is {mode}; a full NSE 5m session is usually 75")
    if not days:
        notes.append("dataset is empty")

    return CoverageReport(
        name=name,
        path=str(dataset.path),
        provider=str(metadata.get("provider", "")),
        interval=str(metadata.get("interval", "")),
        instrument=str(metadata.get("instrument", {}).get("symbol", metadata.get("symbol", ""))),
        start_date=start_date,
        end_date=end_date,
        num_bars=num_bars,
        data_hash=str(dataset.data_hash),
        validation=validation,
        num_trading_days=len(days),
        weekday_days=len(weekday_days),
        median_bars_per_day=median,
        mode_bars_per_day=mode,
        bars_per_day_histogram=hist,
        days_without_bars=missing,
        partial_days=partial[:max_gap_samples],
        outlier_notes=notes,
    )