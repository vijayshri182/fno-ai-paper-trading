"""Market-closed agent jobs (WS 7.8).

While the NSE market is closed (or pre-open) the continuous paper-trading agent
runs offline work: replay / evaluation of logged datasets, learning-loop cycles
and experience capture. Jobs are booked through a :class:`RunPolicy`
(e.g. once per day), so the agent can batch them deterministically, and each run
returns a plain, serializable result the agent records on the heartbeat
(``jobs_run``).

Safety: jobs are evidence / bookkeeping only. Nothing here imports broker,
portfolio, risk or sizing modules — mirroring the §17f.7 learning boundary — and
a learning cycle promotes at most a registry version, never a live order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.learning.loop import LearningLoop, LearningLoopConfig
from fno_ai_paper_trading.persistence.experience_store import ExperienceStore
from fno_ai_paper_trading.promotion.registry import VersionRegistry
from fno_ai_paper_trading.strategies.base import Strategy

DEFAULT_MIN_JOB_INTERVAL = timedelta(hours=23)


@dataclass(frozen=True)
class RunPolicy:
    """When a closed-market job may run (relative to its last successful run)."""

    once_daily: bool = True
    minimum_interval: timedelta = DEFAULT_MIN_JOB_INTERVAL

    def __post_init__(self) -> None:
        if self.minimum_interval <= timedelta(0):
            raise ValueError("minimum_interval must be positive")

    def should_run(self, last_run: datetime | None, now: datetime) -> bool:
        """True when ``now`` is eligible after ``last_run`` (None => run)."""
        if last_run is None:
            return True
        return now >= last_run and (now - last_run) >= self.minimum_interval


@dataclass(frozen=True)
class ClosedJobContext:
    """What a closed-market job may touch (all optional at run time)."""

    now: datetime
    datasets_dir: str = "datasets"
    out_dir: str = "reports/agent"
    store: ExperienceStore | None = None
    registry: VersionRegistry | None = None
    factories: Mapping[str, Callable[[], Strategy]] = field(default_factory=dict)
    extra: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ClosedJob:
    """One booked offline job with its scheduling policy."""

    job_id: str
    description: str
    policy: RunPolicy
    run: Callable[[ClosedJobContext], Mapping[str, object]]

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id is required")
        if not self.description.strip():
            raise ValueError("description is required")
        if not callable(self.run):
            raise TypeError("run must be callable")


@dataclass(frozen=True)
class JobResult:
    """Outcome of one job execution window."""

    job_id: str
    ran: bool
    skipped_reason: str = ""
    result: Mapping[str, object] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "result": dict(self.result),
            "error": self.error,
        }


def run_closed_jobs(
    jobs: Sequence[ClosedJob],
    ctx: ClosedJobContext,
    jobs_last_run: Mapping[str, datetime],
) -> tuple[list[JobResult], dict[str, datetime]]:
    """Run every due job; returns ``(results, updated_last_run)``.

    A failing job never aborts the agent cycle: it is recorded with its error
    and the next window retries it. ``jobs_last_run`` is only advanced for jobs
    that actually ran.
    """
    updated = dict(jobs_last_run)
    results: list[JobResult] = []
    for job in jobs:
        last = updated.get(job.job_id)
        if not job.policy.should_run(last, ctx.now):
            results.append(
                JobResult(
                    job_id=job.job_id,
                    ran=False,
                    skipped_reason=(
                        f"not due (last run {last.isoformat() if last else 'never'})"
                    ),
                )
            )
            continue
        try:
            result = job.run(ctx)
        except Exception as exc:  # noqa: BLE001 - an offline job is never fatal
            results.append(
                JobResult(job_id=job.job_id, ran=True, error=str(exc))
            )
            continue
        updated[job.job_id] = ctx.now
        results.append(JobResult(job_id=job.job_id, ran=True, result=dict(result)))
    return results, updated


# --------------------------------------------------------------------------- #
# Concrete job builders
# --------------------------------------------------------------------------- #


def chunk_by_day(path: Path) -> list[DayBars]:
    """Group one dataset CSV into ordered :class:`DayBars` (shared helper)."""
    dataset = load_dataset(path)
    grouped: dict[str, list] = {}
    for bar in dataset.bars:
        grouped.setdefault(bar.timestamp.date().isoformat(), []).append(bar)
    return [
        DayBars(
            day=date.fromisoformat(day_key),
            bars=tuple(grouped[day_key]),
            source_hash=dataset.data_hash,
        )
        for day_key in sorted(grouped)
    ]


def _load_days(datasets_dir: str | Path) -> list[DayBars]:
    days: list[DayBars] = []
    for path in sorted(Path(datasets_dir).glob("*.csv")):
        days.extend(chunk_by_day(path))
    return days


def build_learning_cycle_job(
    *,
    job_id: str = "learning_cycle",
    description: str = "Learning-loop cycle over logged datasets (paper-only)",
    policy: RunPolicy | None = None,
    champion_resolver: Callable[[ClosedJobContext], Strategy] | None = None,
    candidates: Sequence[Strategy] = (),
    split=None,
    criteria=None,
) -> ClosedJob:
    """A learning-loop cycle job (WS 7.13 machinery, evidence only).

    ``champion_resolver`` picks the strategy (defaults to the registry's active
    champion; falls back to ``moving_average_cross``). Candidates default to
    empty so the gate can never invent a promotion without recorded challengers.
    """

    def _run(ctx: ClosedJobContext) -> Mapping[str, object]:
        days = _load_days(ctx.datasets_dir)
        if not days:
            return {"day_count": 0, "note": "no datasets to run"}
        store = ctx.store or ExperienceStore(directory="experience_store", name="experiences")
        registry = ctx.registry or VersionRegistry("model_registry")
        if champion_resolver is not None:
            champion = champion_resolver(ctx)
        elif registry.active is not None:
            factories = ctx.factories or {}
            from fno_ai_paper_trading.learning.loop import resolve_active_champion

            champion = resolve_active_champion(registry, factories)
        else:
            from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

            champion = MovingAverageCrossStrategy(fast=5, slow=21)

        config = (
            LearningLoopConfig(split=split, criteria=criteria)
            if split is not None or criteria is not None
            else LearningLoopConfig()
        )
        loop = LearningLoop(
            store=store,
            registry=registry,
            config=config,
        )
        result = loop.run_cycle(
            days,
            champion=champion,
            candidates=candidates,
            cycle=0,
            title=job_id,
        )
        return {
            "day_count": result.day_count,
            "captured_records": result.captured_records,
            "appended": result.appended,
            "duplicates": result.duplicates,
            "promotion": result.promotion,
            "cycle_result": result.to_dict(),
        }

    return ClosedJob(
        job_id=job_id,
        description=description,
        policy=policy or RunPolicy(),
        run=_run,
    )


def build_replay_capture_job(
    *,
    job_id: str = "replay_capture",
    description: str = "Replay logged datasets and capture champion experiences",
    policy: RunPolicy | None = None,
    strategy: Strategy | None = None,
) -> ClosedJob:
    """Replay-and-capture job: closed trades become experience records."""

    def _run(ctx: ClosedJobContext) -> Mapping[str, object]:
        days = _load_days(ctx.datasets_dir)
        if not days:
            return {"day_count": 0, "note": "no datasets to run"}
        from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
        from fno_ai_paper_trading.learning.loop import LearningLoop

        store = ctx.store or ExperienceStore(directory="experience_store", name="experiences")
        registry = ctx.registry or VersionRegistry("model_registry")
        champion = strategy
        if champion is None:
            factories = ctx.factories or {}
            from fno_ai_paper_trading.learning.loop import resolve_active_champion

            if registry.active is not None:
                champion = resolve_active_champion(registry, factories)
            else:
                from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy

                champion = MovingAverageCrossStrategy(fast=5, slow=21)
        loop = LearningLoop(store=store, registry=registry)
        captured = loop.capture(days, champion)
        merged = store.merge(captured.records)
        return {
            "day_count": len(days),
            "round_trips": captured.round_trips,
            "appended": merged.appended,
            "duplicates": merged.duplicates,
        }

    return ClosedJob(
        job_id=job_id,
        description=description,
        policy=policy or RunPolicy(),
        run=_run,
    )