"""Fresh-OOS boundary enforcement.

Fresh OOS is strictly after 2026-09-11. Any target date or returned bar on or
before the boundary must fail the run (``PROTOCOL_VIOLATION``) and never be
stored -- never silently filtered. Tests pin that invariant at the validator
and collector layers.
"""
from __future__ import annotations

from datetime import date, datetime

from fresh_oos_testkit import (
    BOUNDARY,
    days_to_today,
    generate_5m_bars,
    make_context,
    session_times,
)
from fno_ai_paper_trading.fresh_oos.protocol import (
    STATUS_PROTOCOL_VIOLATION,
    STATUS_SUCCESS,
)
from fno_ai_paper_trading.fresh_oos.validation import CODE_BOUNDARY, validate_5m_day

NEXT_DAY = date(2026, 9, 15)  # first real acquired fresh session after the boundary
NOW = datetime(2026, 10, 15, 12, 0)


def _swap_bar(bars, at_index, ts):
    bar = bars[at_index]
    return type(bar)(
        instrument=bar.instrument,
        timestamp=ts,
        open=bar.open, high=bar.high, low=bar.low,
        close=bar.close, volume=bar.volume, open_interest=0,
    )


def test_validator_rejects_bar_on_boundary():
    bars = generate_5m_bars(NEXT_DAY)
    bars[0] = _swap_bar(bars, 0, session_times(date(2026, 9, 11), 1)[0])
    report = validate_5m_day(bars, NEXT_DAY)
    assert not report.ok
    assert CODE_BOUNDARY in report.codes
    assert report.status() == STATUS_PROTOCOL_VIOLATION


def test_validator_rejects_bar_before_boundary():
    bars = generate_5m_bars(NEXT_DAY)
    bars[0] = _swap_bar(bars, 0, session_times(date(2026, 9, 10), 1)[0])
    report = validate_5m_day(bars, NEXT_DAY)
    assert CODE_BOUNDARY in report.codes
    assert report.status() == STATUS_PROTOCOL_VIOLATION


def test_validator_accepts_day_strictly_after_boundary():
    report = validate_5m_day(generate_5m_bars(NEXT_DAY), NEXT_DAY)
    assert report.ok


def test_collector_refuses_forced_protected_dates(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    for protected in (date(2026, 9, 11), date(2026, 1, 1), date(2025, 10, 6)):
        outcome = ctx["collector"].collect_once(force_date=protected)
        assert outcome.status == STATUS_PROTOCOL_VIOLATION
        assert protected.isoformat() in outcome.errors
        assert protected.isoformat() in outcome.dates_attempted
    assert not ctx["store"].base_dir.exists()  # nothing stored


def test_collector_fails_run_when_fetched_day_contains_boundary_bar(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    day = date(2026, 9, 17)
    bad_bars = generate_5m_bars(day)
    bad_bars[10] = _swap_bar(bad_bars, 10, session_times(date(2026, 9, 11), 2)[1])
    ctx["client"].responses[day] = bad_bars
    outcome = ctx["collector"].collect_once(force_date=day)
    assert outcome.status == STATUS_PROTOCOL_VIOLATION
    assert day.isoformat() in outcome.errors
    assert ctx["store"].find(day) is None  # never stored, never filtered


def test_eligible_dates_never_include_boundary_or_earlier(tmp_path):
    ctx = make_context(tmp_path, now=datetime(2026, 9, 13, 12, 0))
    assert ctx["collector"].eligible_dates(datetime(2026, 9, 13, 12, 0)) == []
    now_later = datetime(2026, 9, 30, 12, 0)
    eligible = ctx["collector"].eligible_dates(now_later)
    assert all(d > BOUNDARY for d in eligible)
    assert eligible == days_to_today(BOUNDARY, now_later)


def test_eligible_dates_are_chronological_and_catch_up_all_missed(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    eligible = ctx["collector"].eligible_dates(NOW)
    assert eligible == sorted(eligible)
    # catch-up: every trading day strictly after the boundary and before today.
    assert eligible == days_to_today(BOUNDARY, NOW)
    assert len(eligible) > 0


def test_force_date_on_a_strictly_valid_day_succeeds(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    day = date(2026, 9, 17)
    outcome = ctx["collector"].collect_once(force_date=day)
    assert outcome.status == STATUS_SUCCESS
    stored = ctx["store"].find(day)
    assert stored is not None
    assert stored.num_bars == 75


def test_established_scan_excludes_consumed_preboundary_days(tmp_path):
    from fresh_oos_testkit import make_instrument
    from fno_ai_paper_trading.data.dataset_store import save_dataset
    from fno_ai_paper_trading.fresh_oos.store import scan_established_datasets

    # one consumed-window day (2026-09-10) and one fresh day (2026-09-15)
    for day in (date(2026, 9, 10), NEXT_DAY):
        save_dataset(
            generate_5m_bars(day),
            instrument=make_instrument(),
            provider="upstox",
            interval="5m",
            directory=str(tmp_path / "datasets"),
        )
    found = scan_established_datasets(tmp_path / "datasets", boundary=BOUNDARY)
    assert set(found) == {NEXT_DAY}  # the consumed 2026-09-10 day is excluded
    # the collector's status/established index maps only fresh coverage
    ctx = make_context(tmp_path, now=NOW)
    assert set(ctx["collector"]._established_index()) == {NEXT_DAY}