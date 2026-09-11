"""Tests for WS 7.5 five-year historical replay capability.

The intent here is the *capability*: a resumable, honest, deterministic,
day-by-day replay over a configurable multi-year date range. The number of
trading days is whatever the datasets actually contain -- never a fabricated
"five years complete".
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.evaluation.five_year import (
    DayBars,
    FiveYearEvaluation,
    PeriodSplitConfig,
    ProgressStore,
    five_year_report_to_dict,
    split_period,
)
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _day(
    day: date,
    seed: int,
    n: int = 40,
    *,
    reverse: bool = False,
    source_hash_override: str | None = None,
) -> DayBars:
    inst = _future()
    bars: list[MarketPrice] = []
    for i in range(n):
        close = Decimal("24000") + Decimal(seed * 10) + Decimal((i % 8) * 25)
        ts = datetime.combine(day, time(9, 15)) + timedelta(minutes=15 * i)
        bars.append(
            MarketPrice(
                instrument=inst,
                timestamp=ts,
                open=close - Decimal("10"),
                high=close + Decimal("15"),
                low=close - Decimal("15"),
                close=close,
                volume=1000 + i,
            )
        )
    ordered = list(reversed(bars)) if reverse else bars
    source = source_hash_override if source_hash_override is not None else f"hash-{seed}"
    return DayBars(day=day, bars=tuple(ordered), source_hash=source)


def _baseline() -> MovingAverageCrossStrategy:
    return MovingAverageCrossStrategy(fast=5, slow=21)


class TestDayBars:
    def test_rejects_bar_from_another_day(self) -> None:
        bars = list(_day(date(2026, 9, 1), 1).bars)
        bars = list(bars[:5]) + [
            MarketPrice(
                instrument=_future(),
                timestamp=datetime(2026, 9, 2, 9, 15),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("105"),
                volume=100,
            )
        ] + list(bars[5:])
        with pytest.raises(ValueError, match="belong to the given day"):
            DayBars(day=date(2026, 9, 1), bars=tuple(bars))

    def test_coerces_list_to_tuple(self) -> None:
        raw = list(_day(date(2026, 9, 1), 1).bars)
        wrapper = DayBars(day=date(2026, 9, 1), bars=raw)
        assert isinstance(wrapper.bars, tuple)

    def test_rejects_non_date(self) -> None:
        with pytest.raises(TypeError, match="date"):
            DayBars(day="2026-09-01", bars=())  # type: ignore[arg-type]


class TestPeriodSplitConfig:
    def test_valid_defaults(self) -> None:
        split = PeriodSplitConfig()
        assert split.training_ratio == Decimal("0.60")
        assert split.validation_ratio == Decimal("0.20")
        assert split.out_of_sample_ratio == Decimal("0.20")

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            PeriodSplitConfig(training_ratio=Decimal("-0.1"))

    def test_rejects_ratios_summing_above_one(self) -> None:
        with pytest.raises(ValueError, match="must be <= 1"):
            PeriodSplitConfig(
                training_ratio=Decimal("0.9"), validation_ratio=Decimal("0.2")
            )

    def test_rejects_small_window(self) -> None:
        with pytest.raises(ValueError, match="walk_forward_windows"):
            PeriodSplitConfig(walk_forward_windows=0)


class TestSplitPeriod:
    def test_contiguous_and_sorted(self) -> None:
        days = [date(2026, 9, d) for d in (5, 1, 3, 8, 6, 4, 2, 7, 10, 9)]
        labels = split_period(days, PeriodSplitConfig())
        assert len(labels) == 10
        assert labels[date(2026, 9, 1)] == "training"
        assert labels[date(2026, 9, 6)] == "training"
        assert labels[date(2026, 9, 7)] == "validation"
        assert labels[date(2026, 9, 9)] == "out_of_sample"
        assert list(labels.keys()) == sorted(labels.keys())

    def test_empty_input(self) -> None:
        labels = split_period([], PeriodSplitConfig())
        assert labels == {}


class TestProgressStore:
    def test_mark_and_contains(self, tmp_path) -> None:
        path = tmp_path / "progress.json"
        store = ProgressStore(path)
        store.mark(date(2026, 9, 1), "hash-a")
        store.mark(date(2026, 9, 2), "hash-a")
        assert store.contains(date(2026, 9, 1), "hash-a")
        assert store.contains(date(2026, 9, 2), "")
        assert not store.contains(date(2026, 9, 1), "hash-b")
        assert not store.contains(date(2026, 9, 3), "hash-a")

    def test_persistence_round_trip(self, tmp_path) -> None:
        path = tmp_path / "progress.json"
        first = ProgressStore(path)
        first.mark(date(2026, 9, 1), "hash-a")
        second = ProgressStore(path)
        assert second.contains(date(2026, 9, 1), "hash-a")
        assert second.count == 1

    def test_loads_without_file(self, tmp_path) -> None:
        store = ProgressStore(tmp_path / "missing.json")
        assert store.count == 0


class TestFiveYearEvaluation:
    def test_complete_run_reports_honestly(self) -> None:
        days = [
            _day(date(2026, 9, i), seed=i) for i in (1, 2, 3)
        ]
        report = FiveYearEvaluation().run(days, _baseline(), name="demo")
        assert report.complete
        assert report.status == "COMPLETE"
        assert report.days_available == 3
        assert report.days_processed == 3
        assert report.days_skipped == 0
        assert report.start_date == date(2026, 9, 1)
        assert report.end_date == date(2026, 9, 3)
        assert report.aggregate is not None
        assert report.aggregate.aggregate.sessions == 3
        assert all(row["status"] == "processed" for row in report.per_day)
        assert report.period_split["training"] + report.period_split["validation"] + report.period_split["out_of_sample"] == 3

    def test_deterministic_across_runs(self) -> None:
        days = [_day(date(2026, 9, i), seed=i) for i in (1, 2, 3)]
        first = FiveYearEvaluation().run(days, _baseline())
        second = FiveYearEvaluation().run(days, _baseline())
        assert first.aggregate.aggregate.total_pnl == second.aggregate.aggregate.total_pnl
        assert first.aggregate.aggregate.num_trades == second.aggregate.aggregate.num_trades
        assert [row["net_pnl"] for row in first.per_day] == [row["net_pnl"] for row in second.per_day]

    def test_invalid_day_is_skipped(self) -> None:
        days = [
            _day(date(2026, 9, 1), seed=1),
            _day(date(2026, 9, 2), seed=2, reverse=True),
            _day(date(2026, 9, 3), seed=3),
        ]
        report = FiveYearEvaluation().run(days, _baseline())
        assert report.status == "IN_PROGRESS"
        assert report.days_processed == 2
        assert report.days_skipped == 1
        statuses = {row["day"]: row["status"] for row in report.per_day}
        assert statuses[date(2026, 9, 2).isoformat()] == "skipped_invalid"

    def test_empty_days(self) -> None:
        report = FiveYearEvaluation().run([], _baseline())
        assert report.status == "COMPLETE"
        assert report.days_processed == 0
        assert report.aggregate is None

    def test_resume_skips_processed_days(self, tmp_path) -> None:
        progress = ProgressStore(tmp_path / "progress.json")
        days = [_day(date(2026, 9, i), seed=i) for i in (1, 2, 3)]
        FiveYearEvaluation().run(days[:2], _baseline(), progress=progress, name="part1")

        second = FiveYearEvaluation().run(days, _baseline(), progress=progress, name="part2")
        assert second.days_available == 3
        assert second.days_processed == 3
        assert second.status == "COMPLETE"
        assert second.days_skipped == 0
        # Only the day not yet processed was replayed in this run.
        assert second.aggregate is not None
        assert second.aggregate.aggregate.sessions == 1

    def test_source_hash_distinguishes_days(self, tmp_path) -> None:
        progress = ProgressStore(tmp_path / "progress.json")
        first = _day(date(2026, 9, 1), seed=1, source_hash_override="hash-A")
        second = _day(date(2026, 9, 1), seed=9, source_hash_override="hash-B")
        FiveYearEvaluation().run([first], _baseline(), progress=progress)
        rerun = FiveYearEvaluation().run([first, second], _baseline(), progress=progress)
        assert rerun.days_available == 2
        # hash-B is a different source for the same date -> not treated as done.
        assert rerun.days_processed == 2

    def test_report_to_dict(self, tmp_path) -> None:
        days = [_day(date(2026, 9, i), seed=i) for i in (1, 2)]
        report = FiveYearEvaluation().run(days, _baseline())
        payload = five_year_report_to_dict(report)
        assert payload["status"] == "COMPLETE"
        assert payload["days_processed"] == 2
        assert payload["complete"] is True
        assert payload["aggregate"]["aggregate"]["sessions"] == 2
        assert len(payload["per_day"]) == 2
        assert set(payload["period_split"]) == {"training", "validation", "out_of_sample"}

    def test_global_config_is_used(self) -> None:
        config = EvaluationConfig(initial_capital=Decimal("250000"))
        days = [_day(date(2026, 9, i), seed=i) for i in (1, 2, 3)]
        report = FiveYearEvaluation(config=config).run(days, _baseline())
        assert report.aggregate is not None
        # A 250k starting capital must be reflected in the composite curve anchors.
        assert abs(report.aggregate.aggregate.total_pnl) <= abs(Decimal("250000"))
