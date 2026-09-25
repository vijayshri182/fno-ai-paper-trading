"""Fresh-OOS data-readiness semantics.

Readiness is deterministic: ``DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION``
only when >= 20 trading days AND >= 1500 bars are accepted. The trade-count
threshold is carried but never satisfied by the collector, and readiness is
never ``VALIDATION_READY``. Reported coverage always derives from the manifest
pool (accepted dates/bars), not from guessing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fresh_oos_testkit import BOUNDARY, days_to_today, make_context
from fno_ai_paper_trading.fresh_oos.protocol import (
    MIN_BARS,
    MIN_TRADING_DAYS,
    STATUS_SUCCESS,
    compute_readiness,
)

NOW = datetime(2026, 10, 16, 12, 0)


def _run_days(ctx, count):
    """Run a catch-up pass and assert exactly ``count`` days were accepted."""
    outcome = ctx["collector"].collect_once()
    assert outcome.status in (STATUS_SUCCESS, "NO_NEW_DATA")
    accepted = ctx["manifest"].accepted_dates
    assert len(accepted) == count
    return accepted


def test_readiness_requires_both_days_and_bars(tmp_path):
    assert not compute_readiness(MIN_TRADING_DAYS, MIN_BARS - 1).data_ready
    assert not compute_readiness(MIN_TRADING_DAYS - 1, MIN_BARS).data_ready
    ready = compute_readiness(MIN_TRADING_DAYS, MIN_BARS)
    assert ready.data_ready
    assert ready.label == "DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION"
    assert ready.validation_run is False


def test_readiness_never_satisfied_by_trades_alone(tmp_path):
    # trades are reported but ignored: even a large trade count does not flip
    # readiness (trade count belongs to the separate validation job).
    ready = compute_readiness(MIN_TRADING_DAYS, MIN_BARS, min_trades=30)
    assert ready.min_trades == 30
    assert ready.data_ready
    assert "TRADE" not in ready.label
    assert ready.ready_message  # explains the separate trade-count job


def test_fully_acquired_window_is_data_ready(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    eligible = days_to_today(BOUNDARY, NOW)
    assert len(eligible) >= MIN_TRADING_DAYS
    _run_days(ctx, len(eligible))
    report = ctx["manifest"].readiness()
    assert report.data_ready
    assert report.label == "DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION"
    assert report.missing_days == 0
    assert report.missing_bars == 0
    status = ctx["collector"].describe_status(now=NOW)
    assert status["readiness"]["label"] == report.label
    assert status["accepted_days"] == len(eligible)


def test_partial_window_reports_missing_requirements(tmp_path):
    ctx = make_context(tmp_path, now=datetime(2026, 9, 28, 12, 0))
    captured = _run_days(ctx, 10)  # fewer than the minimum trading days
    report = ctx["manifest"].readiness()
    assert not report.data_ready
    assert report.label == "NOT_READY"
    assert report.missing_days == MIN_TRADING_DAYS - len(captured)
    assert any("trading days" in reason for reason in report.reasons)


def test_status_never_reports_validation_run(tmp_path):
    ctx = make_context(tmp_path, now=datetime(2026, 9, 28, 12, 0))
    ctx["collector"].collect_once()
    status = ctx["collector"].describe_status(now=datetime(2026, 9, 28, 12, 0))
    assert status["validation"] == "NOT RUN"


def test_manifest_protocol_block_locks_thresholds(tmp_path):
    ctx = make_context(tmp_path, now=datetime(2026, 9, 28, 12, 0))
    ctx["collector"].collect_once()
    manifest = ctx["manifest"].load(ctx["manifest"].path)
    protocol = manifest.to_dict()["protocol"]
    assert protocol["boundary_exclusive"] == BOUNDARY.isoformat()
    assert protocol["min_trading_days"] == MIN_TRADING_DAYS
    assert protocol["min_bars"] == MIN_BARS
    assert protocol["min_trades"] == 30
    assert protocol["single_use_validation"] is True
    assert protocol["parameter_tuning_allowed"] is False
    assert protocol["interval"] == "5m"