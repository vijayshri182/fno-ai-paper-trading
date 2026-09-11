"""Five-year historical replay capability (WS 7.5).

This module provides the *capability* to process approximately five years of
NIFTY 50 intraday data one trading day at a time, deterministically and
resumably, with honest progress accounting. It never claims five years are done
— the report records exactly how many days were actually processed.

Discipline is enforced here rather than in the strategy: every day is validated,
replayed through the frozen baseline with identical cost assumptions, and
recorded against a resumable progress checkpoint. Period labels
(training / validation / out-of-sample) are computed before any evaluation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.data.validation import validate_bars
from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import Strategy
from fno_ai_paper_trading.utils.functions import non_negative_decimal


@dataclass(frozen=True)
class DayBars:
    """One trading day of validated intraday bars (chronological)."""

    day: date
    bars: tuple[MarketPrice, ...]
    source_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise TypeError("day must be a date")
        if not isinstance(self.bars, tuple):
            object.__setattr__(self, "bars", tuple(self.bars))
        if self.bars:
            first = self.bars[0].timestamp.date()
            last = self.bars[-1].timestamp.date()
            if first != self.day or last != self.day:
                raise ValueError("DayBars bars must all belong to the given day")
            for bar in self.bars:
                if bar.timestamp.date() != self.day:
                    raise ValueError("DayBars bars must all belong to the given day")
            if not isinstance(self.source_hash, str):
                raise TypeError("source_hash must be a string")


@dataclass(frozen=True)
class PeriodSplitConfig:
    """Contiguous train/validation/out-of-sample split over the processed days."""

    training_ratio: Decimal = Decimal("0.60")
    validation_ratio: Decimal = Decimal("0.20")
    walk_forward_windows: int | None = None

    def __post_init__(self) -> None:
        training = non_negative_decimal(self.training_ratio, "training_ratio")
        validation = non_negative_decimal(self.validation_ratio, "validation_ratio")
        if training + validation > 1:
            raise ValueError("training_ratio + validation_ratio must be <= 1")
        if self.walk_forward_windows is not None and self.walk_forward_windows < 1:
            raise ValueError("walk_forward_windows must be >= 1 when set")
        object.__setattr__(self, "training_ratio", training)
        object.__setattr__(self, "validation_ratio", validation)

    @property
    def out_of_sample_ratio(self) -> Decimal:
        return Decimal("1") - self.training_ratio - self.validation_ratio


def split_period(days: Sequence[date], config: PeriodSplitConfig) -> Mapping[date, str]:
    """Label each day as ``training``, ``validation`` or ``out_of_sample``.

    The split is contiguous and based on the ratio of *days*, computed up
    front so no evaluation can influence how a day is labelled.
    """
    ordered = list(dict.fromkeys(days))
    ordered.sort()
    labels: dict[date, str] = {}
    count = len(ordered)
    if count == 0:
        return labels
    train_end = int(config.training_ratio * Decimal(count))
    val_end = train_end + int(config.validation_ratio * Decimal(count))
    for index, day in enumerate(ordered):
        if index < train_end:
            labels[day] = "training"
        elif index < val_end:
            labels[day] = "validation"
        else:
            labels[day] = "out_of_sample"
    return labels


class ProgressStore:
    """Resumable checkpoint of processed days (keyed by date + source hash).

    Persisted as JSON at an operator-supplied path. Two sources are never
    confused: the same date from a different ``source_hash`` is not treated as
    already processed. When no hash is given, any recorded source for the date
    counts as processed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        if path is not None:
            self.load(path)

    @staticmethod
    def _key(day: date, source_hash: str) -> str:
        return f"{day.isoformat()}|{source_hash}"

    def load(self, path: Path) -> None:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._data = {str(key): str(value) for key, value in payload.items()}
            self.path = path
        else:
            self._data = {}
            self.path = path

    def contains(self, day: date, source_hash: str = "") -> bool:
        if source_hash:
            return self._key(day, source_hash) in self._data
        prefix = f"{day.isoformat()}|"
        return any(key.startswith(prefix) for key in self._data)

    def mark(self, day: date, source_hash: str = "") -> None:
        key = self._key(day, source_hash)
        self._data[key] = datetime.now().isoformat(timespec="seconds")
        if self.path is not None:
            self.path.write_text(
                json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
            )

    @property
    def processed(self) -> list[str]:
        return sorted({key.split("|", 1)[0] for key in self._data})

    @property
    def count(self) -> int:
        return len(self._data)


@dataclass(frozen=True)
class FiveYearReport:
    """Honest status of a five-year replay run. Never claims completion without
    having processed every available day."""

    name: str
    strategy_name: str
    start_date: date | None
    end_date: date | None
    days_available: int
    days_processed: int
    days_skipped: int
    status: str  # "COMPLETE" only when every available day was processed
    aggregate: EvaluationRun | None
    per_day: tuple[Mapping[str, Any], ...]
    period_split: Mapping[str, int]
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def complete(self) -> bool:
        return self.status == "COMPLETE"

    def summary(self) -> str:
        return (
            f"{self.strategy_name} replay: {self.days_processed}/{self.days_available} "
            f"trading days processed ({self.status})"
        )


class FiveYearEvaluation:
    """Run a deterministic, resumable, day-by-day replay over many days.

    ``run`` consumes :class:`DayBars` groups in order, validates each, replays
    one day through :class:`HistoricalEvaluator`, and records progress. By
    default already-processed days are skipped when a :class:`ProgressStore` is
    supplied, so interrupted runs can resume.
    """

    def __init__(
        self,
        *,
        config: EvaluationConfig | None = None,
        evaluator: HistoricalEvaluator | None = None,
        period_split: PeriodSplitConfig | None = None,
    ) -> None:
        self.config = config or EvaluationConfig()
        self._evaluator = evaluator or HistoricalEvaluator(config=self.config)
        self.period_split = period_split or PeriodSplitConfig()

    def run(
        self,
        days: Sequence[DayBars],
        strategy: Strategy,
        *,
        name: str = "five-year-replay",
        progress: ProgressStore | None = None,
        skip_processed: bool = True,
        strategy_params: Mapping[str, Any] | None = None,
    ) -> FiveYearReport:
        """Process every provided day, resuming from ``progress`` when given.

        Invalid days are skipped and counted; valid days are evaluated
        deterministically. ``status`` is "COMPLETE" only when processed +
        skipped == available and nothing was skipped as invalid.
        """
        ordered = list(days)
        ordered.sort(key=lambda item: item.day)
        params = dict(strategy_params or {})
        labels = split_period([item.day for item in ordered], self.period_split)

        evaluations: list[Mapping[str, Any]] = []
        skipped = 0
        processed = 0
        replays: list = []
        for day_bars in ordered:
            if progress is not None and skip_processed and progress.contains(
                day_bars.day, day_bars.source_hash
            ):
                continue
            report = validate_bars(list(day_bars.bars), allow_empty=False)
            if not report.ok:
                skipped += 1
                evaluations.append(
                    {
                        "day": day_bars.day.isoformat(),
                        "dataset_hash": day_bars.source_hash,
                        "period": labels.get(day_bars.day, "unknown"),
                        "status": "skipped_invalid",
                        "issues": [issue.message for issue in report.errors[:5]],
                    }
                )
                continue
            replay = self._evaluator.replay_bars(
                list(day_bars.bars),
                strategy,
                dataset_name=day_bars.day.isoformat(),
                dataset_hash=day_bars.source_hash,
                strategy_params=params,
            )
            processed += 1
            replays.append(replay)
            if progress is not None:
                progress.mark(day_bars.day, day_bars.source_hash)
            evaluations.append(
                {
                    "day": day_bars.day.isoformat(),
                    "dataset_hash": day_bars.source_hash,
                    "period": labels.get(day_bars.day, "unknown"),
                    "status": "processed",
                    "net_pnl": str(replay.session.net_pnl),
                    "net_return_pct": str(replay.session.net_return_pct),
                    "round_trips": replay.session.num_trades,
                    "win_rate": str(replay.session.win_rate),
                    "max_drawdown_pct": str(replay.session.max_drawdown_pct),
                }
            )

        if progress is not None:
            effective_processed = sum(
                1 for day_bars in ordered if progress.contains(day_bars.day, day_bars.source_hash)
            )
        else:
            effective_processed = processed

        aggregate = None
        if replays:
            aggregate = self._evaluator.build_run(
                replays,
                name=f"{name}:aggregate",
                strategy_name=strategy.name,
                strategy_params=params,
                baseline=True,
            )

        days_available = len(ordered)
        status = (
            "COMPLETE"
            if effective_processed == days_available and skipped == 0
            else "IN_PROGRESS"
        )
        start = ordered[0].day if ordered else None
        end = ordered[-1].day if ordered else None
        return FiveYearReport(
            name=name,
            strategy_name=strategy.name,
            start_date=start,
            end_date=end,
            days_available=days_available,
            days_processed=effective_processed,
            days_skipped=skipped,
            status=status,
            aggregate=aggregate,
            per_day=tuple(evaluations),
            period_split={
                label: sum(1 for item in ordered if labels.get(item.day) == label)
                for label in ("training", "validation", "out_of_sample")
            },
        )


def five_year_report_to_dict(report: FiveYearReport) -> dict[str, Any]:
    aggregate: dict[str, Any] | None = None
    if report.aggregate is not None:
        from fno_ai_paper_trading.evaluation.report import evaluation_run_to_dict

        aggregate = evaluation_run_to_dict(report.aggregate)
    return {
        "name": report.name,
        "strategy_name": report.strategy_name,
        "start_date": report.start_date.isoformat() if report.start_date else None,
        "end_date": report.end_date.isoformat() if report.end_date else None,
        "days_available": report.days_available,
        "days_processed": report.days_processed,
        "days_skipped": report.days_skipped,
        "status": report.status,
        "complete": report.complete,
        "aggregate": aggregate,
        "per_day": list(report.per_day),
        "period_split": dict(report.period_split),
        "generated_at": report.generated_at,
    }


def five_year_report_to_html(report: FiveYearReport, title: str | None = None) -> str:
    """Render a five-year replay report as a standalone HTML page."""
    from fno_ai_paper_trading.research.report import CSS, escape, kv_rows, table

    headline = report.summary()
    status_class = "good" if report.complete else "warn"
    body = [f'<span class="{status_class}">{report.status}</span>']
    rows = [
        ("Name", report.name),
        ("Strategy", report.strategy_name),
        ("Date range", f"{report.start_date} to {report.end_date}"),
        ("Period split (train/val/oos days)",
         f"{report.period_split.get('training', 0)} / "
         f"{report.period_split.get('validation', 0)} / "
         f"{report.period_split.get('out_of_sample', 0)}"),
        ("Aggregate session count",
         str(report.aggregate.aggregate.sessions) if report.aggregate is not None else "—"),
        ("Aggregate net P&L",
         f"{report.aggregate.aggregate.total_pnl:.4f}"
         if report.aggregate is not None else "—"),
    ]
    body.append(kv_rows(rows))
    if report.aggregate is not None:
        body.append("<h3>Aggregate (this run)</h3>")
        aggregate = five_year_report_to_dict(report)["aggregate"]["aggregate"]
        body.append(
            kv_rows(
                [
                    ("Net P&L", str(aggregate["total_pnl"])),
                    ("Net return %", str(aggregate["total_return_pct"])),
                    ("Round trips", str(aggregate["num_trades"])),
                    ("Win rate %", str(aggregate["win_rate"])),
                    ("Max drawdown %", str(aggregate["max_drawdown_pct"])),
                    ("Exposure %", str(aggregate["exposure_pct"])),
                    ("Profit factor", str(aggregate["profit_factor"])),
                ]
            )
        )
    day_rows = [
        [
            str(row["status"]),
            str(row["day"]),
            str(row["period"]),
            str(row.get("net_pnl", "—")),
            str(row.get("round_trips", "—")),
            str(row.get("win_rate", "—")),
        ]
        for row in report.per_day
    ]
    body.append("<h3>Per trading day</h3>")
    body.append(table(["Status", "Day", "Period", "Net P&L", "Round trips", "Win rate %"], day_rows))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title or "Five-year historical replay"}</title>{CSS}</head>
<body>
<div class="wrap">
<h1>{escape(headline)}</h1>
{"".join(body)}
</div>
</body></html>"""