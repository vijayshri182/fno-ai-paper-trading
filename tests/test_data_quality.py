"""Tests for research data-quality validation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import build_sample_instruments
from fno_ai_paper_trading.data.validation import (
    ValidationReport,
    format_report,
    validate_bars,
)
from fno_ai_paper_trading.models.market import MarketPrice


def _future():
    return build_sample_instruments()[0]


def _bar(ts: datetime, *, close: str = "100", high: str = "110", low: str = "90",
         volume: int = 10) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def _daily_series(n: int = 5, start: datetime | None = None) -> list[MarketPrice]:
    base = start or datetime(2026, 9, 1, 9, 15)
    return [_bar(base + timedelta(days=i)) for i in range(n)]


def _corrupt(bar: MarketPrice, **kwargs) -> MarketPrice:
    for name, value in kwargs.items():
        object.__setattr__(bar, name, value)
    return bar


class TestHealthySeries:
    def test_well_formed_series_passes(self) -> None:
        report = validate_bars(_daily_series())
        assert isinstance(report, ValidationReport)
        assert report.ok
        assert report.errors == []
        assert report.warnings == []

    def test_empty_series_ok_by_default(self) -> None:
        assert validate_bars([]).ok

    def test_empty_series_error_when_disallowed(self) -> None:
        assert not validate_bars([], allow_empty=False).ok

    def test_format_report_ok(self) -> None:
        assert format_report(validate_bars(_daily_series())) == "OK"


class TestStructuralErrors:
    def test_non_positive_close(self) -> None:
        report = validate_bars([_corrupt(_bar(datetime(2026, 9, 1)), close=Decimal("0"))])
        assert not report.ok
        assert any("non-positive close" in i.message for i in report.errors)

    def test_high_below_low(self) -> None:
        report = validate_bars([_corrupt(_bar(datetime(2026, 9, 1)), high=Decimal("50"))])
        assert not report.ok
        assert any("high below low" in i.message for i in report.errors)

    def test_high_below_close(self) -> None:
        report = validate_bars([_corrupt(_bar(datetime(2026, 9, 1)), high=Decimal("95"))])
        assert not report.ok
        assert any("high below open/close" in i.message for i in report.errors)

    def test_low_above_close(self) -> None:
        report = validate_bars([_corrupt(_bar(datetime(2026, 9, 1)), low=Decimal("105"))])
        assert not report.ok
        assert any("low above open/close" in i.message for i in report.errors)

    def test_negative_volume(self) -> None:
        report = validate_bars([_corrupt(_bar(datetime(2026, 9, 1)), volume=-5)])
        assert not report.ok
        assert any("negative volume" in i.message for i in report.errors)

    def test_negative_open_interest(self) -> None:
        bar = _switch_to_oi_negative(_bar(datetime(2026, 9, 1)))
        report = validate_bars([bar])
        assert not report.ok
        assert any("negative open_interest" in i.message for i in report.errors)


def _switch_to_oi_negative(bar: MarketPrice) -> MarketPrice:
    return _corrupt(bar, open_interest=-1)


class TestOrderingAndDuplicates:
    def test_out_of_order_flagged(self) -> None:
        bars = _daily_series(3)
        bars.reverse()
        report = validate_bars(bars)
        assert not report.ok
        assert any("out of order" in i.message for i in report.errors)

    def test_duplicate_timestamp_flagged(self) -> None:
        bars = _daily_series(2)
        dup = _bar(bars[0].timestamp, close="101")
        report = validate_bars([bars[0], dup, bars[1]])
        assert not report.ok
        assert any("duplicate timestamp" in i.message for i in report.errors)

    def test_issue_carries_index(self) -> None:
        bars = _daily_series(4)
        dup = _bar(bars[0].timestamp, close="99")  # adjacent duplicate of bar 0
        report = validate_bars([bars[0], dup, bars[1], bars[2]])
        dup_issues = [i for i in report.errors if "duplicate timestamp" in i.message]
        assert dup_issues and dup_issues[0].index == 1


class TestSpacingAndTimezone:
    def test_regular_spacing_no_warnings(self) -> None:
        report = validate_bars(_daily_series(), interval_minutes=1440)
        assert report.ok
        assert report.warnings == []

    def test_large_gap_warns(self) -> None:
        bars = _daily_series(2)
        bars[1] = _bar(bars[0].timestamp + timedelta(days=7))
        report = validate_bars(bars, interval_minutes=1440)
        assert report.ok  # gaps are warnings, never errors
        assert any("gap" in w.message for w in report.warnings)

    def test_small_gap_no_warning(self) -> None:
        bars = _daily_series(2)
        bars[1] = _bar(bars[0].timestamp + timedelta(days=2))
        report = validate_bars(bars, interval_minutes=1440)
        assert report.warnings == []

    def test_mixed_aware_and_naive_warns(self) -> None:
        bars = _daily_series(2)
        aware = _corrupt(bars[1], timestamp=bars[1].timestamp.replace(tzinfo=timezone.utc))
        report = validate_bars([bars[0], aware])
        assert report.ok
        assert any("timezone" in w.message for w in report.warnings)