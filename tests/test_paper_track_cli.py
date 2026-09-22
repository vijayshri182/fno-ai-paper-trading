"""Phase 9 — final operational CLI: observer commands ``status`` / ``report``.

``status`` summarises the account: last persisted day, lifetime cumulative
reconciliation, fingerprint — and returns a non-zero rc when the persisted
state is corrupt or fails to reconcile (fail-closed observability).
``report`` prints one persisted day's full paper report as JSON (defaults to
the latest day). Both commands stay strictly paper-side and token-free, and
all Phase-1/4 commands remain intact.
"""
from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path

from fno_ai_paper_trading.paper_track import runner as runner_mod
from fno_ai_paper_trading.paper_track.runner import main, run_sessions
from fno_ai_paper_trading.paper_track.store import TrackStore
from tests.paper_track_testkit import closes_feed

DAY1 = date(2026, 9, 21)
DAY2 = date(2026, 9, 22)


def _plan_strategy():
    from fno_ai_paper_trading.models.enums import Signal
    from fno_ai_paper_trading.strategies.base import SignalResult, Strategy

    class Plan(Strategy):
        name = "plan"

        def analyze(self, bars):
            last = bars[-1]
            moment = last.timestamp.time()
            if moment == time(10, 0):
                return SignalResult(Signal.BUY, last.instrument, last.timestamp, "plan buy")
            if moment == time(12, 0):
                return SignalResult(Signal.SELL, last.instrument, last.timestamp, "plan sell")
            return SignalResult(Signal.HOLD, last.instrument, last.timestamp, "plan hold")

    return Plan()


def _run(store_dir: Path, account: str, days: list[date]) -> None:
    from fno_ai_paper_trading.paper_track.engine import TrackConfig

    config = TrackConfig(account=account, store_dir=store_dir, strategy=_plan_strategy())
    store = TrackStore(store_dir, account)
    close = 25000.0
    for i, day in enumerate(days):
        feed = closes_feed([close + 40 * i for i in range(75)], day=day)
        run_sessions(config=config, store=store, feed=feed, days=[day], resume=i > 0)
        close -= 20 * 75


def _argv(*tokens: str) -> list[str]:
    return list(tokens)


def test_status_on_fresh_account_reports_no_days(tmp_path, capsys):
    rc = main(_argv("status", "--store-dir", str(tmp_path), "--account", "fresh"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no persisted days" in out


def test_status_after_multiday_run_reconciles(tmp_path, capsys):
    account = "cli"
    _run(tmp_path, account, [DAY1, DAY2])
    rc = main(_argv("status", "--store-dir", str(tmp_path), "--account", account))
    out = capsys.readouterr().out
    assert rc == 0
    assert "start_cash" in out
    assert "final_cash" in out
    assert "reconciled" in out and "True" in out
    assert "fingerprint" in out
    assert "2 day(s)" in out


def test_status_flags_corrupt_state_invalid(tmp_path, capsys):
    account = "corrupt"
    _run(tmp_path, account, [DAY1])
    store = TrackStore(tmp_path, account)
    report_path = store.report_path(DAY1)
    report_path.write_text("{broken", encoding="utf-8")
    rc = main(_argv("status", "--store-dir", str(tmp_path), "--account", account))
    out = capsys.readouterr().out
    assert rc == 2
    assert "STATE INVALID" in out


def test_report_prints_selected_day(tmp_path, capsys):
    account = "sel"
    _run(tmp_path, account, [DAY1, DAY2])
    rc = main(_argv("report", "--store-dir", str(tmp_path), "--account", account, "--day", "2026-09-21"))
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["account"] == account
    assert payload["trading_date"] == "2026-09-21"
    assert payload["paper_only"] is True


def test_report_defaults_to_latest_day(tmp_path, capsys):
    account = "latest"
    _run(tmp_path, account, [DAY1, DAY2])
    rc = main(_argv("report", "--store-dir", str(tmp_path), "--account", account))
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["trading_date"] == "2026-09-22"


def test_report_missing_day_returns_1(tmp_path, capsys):
    account = "missing"
    _run(tmp_path, account, [DAY1])
    rc = main(_argv("report", "--store-dir", str(tmp_path), "--account", account, "--day", "2026-09-30"))
    assert rc == 1
    assert "no report persisted" in capsys.readouterr().out


def test_report_no_account_returns_1(tmp_path, capsys):
    rc = main(_argv("report", "--store-dir", str(tmp_path), "--account", "ghost"))
    assert rc == 1


def test_existing_cli_commands_still_intact(tmp_path):
    account = "keep"
    _run(tmp_path, account, [DAY1])
    assert main(_argv("list", "--store-dir", str(tmp_path), "--account", account)) == 0
    assert main(_argv("checkpt", "--store-dir", str(tmp_path), "--account", account)) == 0


def test_status_module_hooks_are_token_free():
    source = Path(runner_mod.__file__).read_text(encoding="utf-8")
    # mandatory source fail-closed checks: no live-execution or scheduler wiring.
    # (FNO_UPSTOX_ACCESS_TOKEN's env-only read in cmd_upstox is the sanctioned
    # Phase-2 paper seam; the authoritative structural scan lives in the
    # isolation suites, so here we only assert absent imports/hooks.)
    assert "import execution" not in source
    assert "execution.engine" not in source
    assert "LiveExecution" not in source
    assert "import scheduler" not in source
    assert "scheduler.schedule" not in source
    assert "fresh_oos" not in source