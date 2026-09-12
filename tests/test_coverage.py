"""Tests for data coverage / quality auditing (evaluation.coverage)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.data.dataset_store import save_dataset
from fno_ai_paper_trading.evaluation.coverage import build_coverage_report
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bars(days_skip=None) -> list[MarketPrice]:
    start = datetime(2026, 9, 1, 9, 15)
    instrument = _future()
    bars: list[MarketPrice] = []
    for i in range(40):
        ts = start + timedelta(days=i)
        if days_skip and ts.date() in days_skip:
            continue
        value = Decimal(24000 + i)
        bars.append(
            MarketPrice(
                instrument=instrument,
                timestamp=ts,
                open=value,
                high=value + Decimal("10"),
                low=value - Decimal("10"),
                close=value,
                volume=1000,
            )
        )
    return bars


def _dataset(tmp_path: Path, bars) -> tuple:
    saved = save_dataset(
        bars,
        instrument=_future(),
        provider="mock",
        interval="day",
        directory=tmp_path,
    )
    from fno_ai_paper_trading.data.dataset_store import load_dataset

    return load_dataset(saved.path), saved.path


def test_full_calendar_reports_no_missing_days(tmp_path: Path) -> None:
    dataset, _ = _dataset(tmp_path, _bars())
    report = build_coverage_report(dataset, interval_minutes=1440, expected_bars_per_day=1)
    assert report.num_trading_days == 40
    assert report.missing_weekday_days == 0
    assert report.validation.ok


def test_weekend_and_known_gap_are_detected(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, 9, 15)
    instrument = _future()
    bars: list[MarketPrice] = []
    for i in range(10):
        ts = start + timedelta(days=i)
        if ts.weekday() >= 5:
            continue
        value = Decimal(24000 + i)
        bars.append(
            MarketPrice(
                instrument=instrument,
                timestamp=ts,
                open=value,
                high=value,
                low=value,
                close=value,
                volume=1,
            )
        )
    dataset, path = _dataset(tmp_path, bars)
    report = build_coverage_report(dataset, interval_minutes=1440, expected_bars_per_day=1)
    assert report.num_trading_days == 8
    assert report.weekday_days >= report.num_trading_days
    assert report.validation.ok


def test_removed_weekday_is_missing_day(tmp_path: Path) -> None:
    bars = _bars()
    removed = bars.pop(2)
    assert removed.timestamp.weekday() < 5
    dataset, _ = _dataset(tmp_path, bars)
    report = build_coverage_report(dataset, interval_minutes=1440, expected_bars_per_day=1)
    assert report.missing_weekday_days >= 1
    assert removed.timestamp.date().isoformat() in report.days_without_bars


def test_duplicate_timestamp_is_validation_error(tmp_path: Path) -> None:
    bars = _bars()
    dataset, _ = _dataset(tmp_path, bars)
    from fno_ai_paper_trading.data.dataset_store import StoredDataset

    broken_bars = [bars[0], bars[0]] + bars[1:]
    broken = StoredDataset(bars=broken_bars, metadata=dataset.metadata, path=dataset.path)
    report = build_coverage_report(broken, interval_minutes=1440)
    assert not report.validation.ok
    assert report.validation.errors


def test_histogram_and_hash(tmp_path: Path) -> None:
    bars = _bars()
    dataset, _ = _dataset(tmp_path, bars)
    report = build_coverage_report(dataset, interval_minutes=1440)
    assert report.median_bars_per_day == 1
    payload = report.to_dict()
    assert payload["data_hash"] == dataset.data_hash
    assert payload["num_bars"] == len(bars)