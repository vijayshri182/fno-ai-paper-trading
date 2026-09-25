"""Deterministic NIFTY market-session gate: helper + scheduled-path behavior.

The gate is a scheduling-only efficiency guard: CLOSED passes never touch the
Upstox client, the store or the manifest. All tests are network-free and use a
fixed clock.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fresh_oos_testkit import make_context
from fno_ai_paper_trading.fresh_oos.scheduler import FreshOosScheduler
from fno_ai_paper_trading.fresh_oos.session_hours import (
    MARKET_TZ,
    REASON_OPEN,
    REASON_OUTSIDE_MARKET_HOURS,
    REASON_WEEKEND,
    evaluate_session,
    market_now,
)

NOW = datetime(2026, 10, 15, 12, 0)  # Thursday, IST session OPEN
D1 = date(2026, 9, 17)

# Fixed clocks: naive datetimes are interpreted as Asia/Kolkata by the gate.
OPEN_THU = datetime(2026, 10, 15, 12, 0)        # Thursday, session OPEN
CLOSED_SUNDAY = datetime(2026, 9, 20, 16, 0)    # Sunday, WEEKEND
CLOSED_MON_OFFHOURS = datetime(2026, 9, 14, 16, 0)  # Monday 16:00 IST, outside hours


class _PoisonClient:
    """Fails loudly if the machine-hours gate ever lets a fetch through."""

    def __init__(self) -> None:
        self.calls: list[date] = []

    def fetch_5m_day(self, instrument, day):  # pragma: no cover - never invoked
        self.calls.append(day)
        raise AssertionError("Upstox fetch must never be called during a CLOSED pass")

    def redact_error_text(self, text: str) -> str:
        return text


# --------------------------------------------------------------------------- #
# Pure session-calendar helper
# --------------------------------------------------------------------------- #

def test_monday_091459_closed():
    assert not evaluate_session(datetime(2026, 9, 14, 9, 14, 59)).is_open


def test_monday_091500_open():
    r = evaluate_session(datetime(2026, 9, 14, 9, 15, 0))
    assert r.is_open and r.reason == REASON_OPEN


def test_monday_120000_open():
    r = evaluate_session(datetime(2026, 9, 14, 12, 0, 0))
    assert r.is_open and r.reason == REASON_OPEN


def test_monday_152959_open():
    r = evaluate_session(datetime(2026, 9, 14, 15, 29, 59))
    assert r.is_open and r.reason == REASON_OPEN


def test_monday_153000_closed():
    r = evaluate_session(datetime(2026, 9, 14, 15, 30, 0))
    assert not r.is_open and r.reason == REASON_OUTSIDE_MARKET_HOURS


def test_monday_160000_closed():
    r = evaluate_session(datetime(2026, 9, 14, 16, 0, 0))
    assert not r.is_open and r.reason == REASON_OUTSIDE_MARKET_HOURS


def test_saturday_1200_closed():
    r = evaluate_session(datetime(2026, 9, 19, 12, 0, 0))
    assert not r.is_open and r.reason == REASON_WEEKEND


def test_sunday_1200_closed():
    r = evaluate_session(datetime(2026, 9, 20, 12, 0, 0))
    assert not r.is_open and r.reason == REASON_WEEKEND


def test_phase_label_round_trip():
    assert evaluate_session(datetime(2026, 9, 14, 12, 0)).phase == "OPEN"
    assert evaluate_session(datetime(2026, 9, 20, 12, 0)).phase == "CLOSED"
    assert str(evaluate_session(datetime(2026, 9, 14, 16, 0))) == REASON_OUTSIDE_MARKET_HOURS


def test_same_instant_is_timezone_independent():
    # 09:30 IST Monday 2026-09-14 expressed in UTC and in UTC-07: same verdict.
    utc = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
    minus7 = datetime(2026, 9, 13, 21, 0, tzinfo=timezone(timedelta(hours=-7)))
    r1, r2 = evaluate_session(utc), evaluate_session(minus7)
    assert r1.is_open and r2.is_open
    assert r1.observed_at == r2.observed_at
    assert r1.observed_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_naive_datetime_interpreted_as_ist_never_local():
    naive = evaluate_session(datetime(2026, 9, 16, 12, 0))
    aware = evaluate_session(datetime(2026, 9, 16, 12, 0, tzinfo=MARKET_TZ))
    assert naive.is_open == aware.is_open is True
    assert naive.observed_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_market_now_is_aware_ist():
    now = market_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=5, minutes=30)


# --------------------------------------------------------------------------- #
# Scheduled-path gating
# --------------------------------------------------------------------------- #

def test_scheduler_closed_is_clean_skip_with_no_side_effects(tmp_path, capsys):
    ctx = make_context(tmp_path, now=NOW)
    scheduler = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session,
        gate_now_fn=lambda: CLOSED_SUNDAY,
    )
    assert scheduler.run(once=True) == 0          # clean SKIP, not a failure
    assert ctx["client"].calls == []              # no Upstox request
    assert ctx["store"].find(D1) is None          # no data written
    assert not (ctx["root"] / "fresh_oos_manifest.json").exists()  # manifest untouched
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "WEEKEND" in out and "CLOSED" in out


def test_scheduler_closed_offhours_reports_reason(tmp_path, capsys):
    ctx = make_context(tmp_path, now=NOW)
    scheduler = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session,
        gate_now_fn=lambda: CLOSED_MON_OFFHOURS,
    )
    assert scheduler.run(once=True) == 0
    assert ctx["client"].calls == []
    assert "OUTSIDE_NIFTY_MARKET_HOURS" in capsys.readouterr().out


def test_scheduler_closed_does_not_append_to_existing_manifest(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    open_sched = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session, gate_now_fn=lambda: OPEN_THU,
    )
    assert open_sched.run(once=True) == 0
    runs_before = len(ctx["manifest"].runs)
    assert runs_before == 1
    closed_sched = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session, gate_now_fn=lambda: CLOSED_SUNDAY,
    )
    assert closed_sched.run(once=True) == 0
    assert len(ctx["manifest"].runs) == runs_before  # manifest unchanged


def test_scheduler_closed_without_credential_is_clean_skip(tmp_path):
    # The network/credential path must never be reached on a CLOSED pass, so an
    # absent token must NOT turn into AUTH_REQUIRED.
    poison = _PoisonClient()
    ctx = make_context(tmp_path, now=NOW, client=poison)
    scheduler = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session, gate_now_fn=lambda: CLOSED_SUNDAY,
    )
    assert scheduler.run(once=True) == 0
    assert poison.calls == []


def test_scheduler_open_still_runs_collector(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    scheduler = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session, gate_now_fn=lambda: OPEN_THU,
    )
    assert scheduler.run(once=True) == 0
    assert ctx["client"].calls                      # collector path preserved
    assert ctx["store"].find(D1) is not None
    assert len(ctx["manifest"].runs) == 1


def test_scheduler_resident_loop_skips_closed_cycles(tmp_path, capsys):
    ctx = make_context(tmp_path, now=NOW)
    scheduler = FreshOosScheduler(
        ctx["collector"], session_gate=evaluate_session,
        gate_now_fn=lambda: CLOSED_SUNDAY,
        sleep=lambda _seconds: scheduler.stop(),
    )
    assert scheduler.run(once=False) == 0
    assert ctx["client"].calls == []
    assert capsys.readouterr().out.count("SKIPPED") >= 2


def test_scheduler_class_default_runs_every_pass(tmp_path):
    # The bare-loop class is not gated by default (manual/direct semantics);
    # only the scheduled main() entry point enables the gate.
    ctx = make_context(tmp_path, now=NOW)
    scheduler = FreshOosScheduler(ctx["collector"], sleep=lambda s: None)
    assert scheduler.run(once=True) == 0
    assert ctx["client"].calls
    assert len(ctx["manifest"].runs) == 1


def test_scheduler_main_wires_session_gate(monkeypatch, tmp_path):
    from fno_ai_paper_trading.fresh_oos import scheduler as sched_module

    ctx = make_context(tmp_path, now=NOW)
    captured: dict[str, object] = {}
    original_cls = sched_module.FreshOosScheduler

    class _Spy(original_cls):
        def __init__(self, collector, **kwargs):
            captured.update(kwargs)
            super().__init__(collector, **kwargs)

    monkeypatch.setattr(sched_module, "FreshOosScheduler", _Spy)
    monkeypatch.setattr(
        sched_module.factory, "build_collector", lambda **kw: ctx["collector"]
    )
    assert sched_module.main(["--once"]) == 0
    assert captured.get("session_gate") is sched_module.evaluate_session
    assert captured.get("gate_now_fn") is sched_module.market_now