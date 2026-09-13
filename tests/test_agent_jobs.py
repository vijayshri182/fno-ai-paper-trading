"""Tests for closed-market jobs and scheduling policy (WS 7.8)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fno_ai_paper_trading.agent.jobs import (
    ClosedJob,
    ClosedJobContext,
    RunPolicy,
    build_learning_cycle_job,
    build_replay_capture_job,
    run_closed_jobs,
)


def _job(job_id: str, value: str = "ok") -> ClosedJob:
    return ClosedJob(
        job_id=job_id,
        description=job_id,
        policy=RunPolicy(minimum_interval=timedelta(hours=1)),
        run=lambda ctx: {"value": value},
    )


def test_run_policy_first_run():
    policy = RunPolicy()
    assert policy.should_run(None, datetime(2026, 9, 8, 16, 0)) is True


def test_run_policy_within_interval():
    policy = RunPolicy(minimum_interval=timedelta(hours=1))
    last = datetime(2026, 9, 8, 16, 0)
    assert policy.should_run(last, last + timedelta(minutes=30)) is False


def test_run_policy_after_interval():
    policy = RunPolicy(minimum_interval=timedelta(hours=1))
    last = datetime(2026, 9, 8, 16, 0)
    assert policy.should_run(last, last + timedelta(hours=2)) is True


def test_run_policy_invalid_interval():
    with pytest.raises(ValueError):
        RunPolicy(minimum_interval=timedelta(0))


def test_run_closed_jobs_only_due():
    now = datetime(2026, 9, 8, 16, 0)
    ctx = ClosedJobContext(now=now)
    a = _job("a")
    b = _job("b")
    jobs, updated = run_closed_jobs([a, b], ctx, {"b": now})
    assert [r.job_id for r in jobs] == ["a", "b"]
    assert jobs[0].ran is True
    assert jobs[1].ran is False
    assert "a" in updated and "b" in updated


def test_run_closed_jobs_failing_job():
    now = datetime(2026, 9, 8, 16, 0)
    ctx = ClosedJobContext(now=now)

    def boom(_ctx):
        raise RuntimeError("exploded")

    failing = ClosedJob("fail", "failing", RunPolicy(), boom)
    jobs, updated = run_closed_jobs([failing], ctx, {})
    assert jobs[0].ran is True
    assert jobs[0].error == "exploded"
    # A failing job is never marked as last-run (it gets retried next window).
    assert "fail" not in updated


def test_run_closed_jobs_advances_last_run():
    now = datetime(2026, 9, 8, 16, 0)
    ctx = ClosedJobContext(now=now)
    jobs, updated = run_closed_jobs([_job("ok")], ctx, {})
    assert updated["ok"] == now


def test_closed_job_validation():
    with pytest.raises(ValueError):
        ClosedJob("", "desc", RunPolicy(), lambda ctx: {})
    with pytest.raises(ValueError):
        ClosedJob("id", "", RunPolicy(), lambda ctx: {})
    with pytest.raises(TypeError):
        ClosedJob("id", "desc", RunPolicy(), None)  # type: ignore[arg-type]


def test_replay_capture_job_empty_datasets(tmp_path):
    now = datetime(2026, 9, 8, 16, 0)
    job = build_replay_capture_job(policy=RunPolicy())
    ctx = ClosedJobContext(now=now, datasets_dir=str(tmp_path))
    result = job.run(ctx)
    assert result["day_count"] == 0


def test_learning_cycle_job_empty_datasets(tmp_path):
    now = datetime(2026, 9, 8, 16, 0)
    job = build_learning_cycle_job(policy=RunPolicy())
    ctx = ClosedJobContext(now=now, datasets_dir=str(tmp_path))
    result = job.run(ctx)
    assert result["day_count"] == 0