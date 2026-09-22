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


def _config(tmp_path: Path, account: str):
    from fno_ai_paper_trading.paper_track.engine import TrackConfig

    return TrackConfig(account=account, store_dir=Path(tmp_path), strategy=_plan_strategy())


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


def test_empty_session_true_when_no_bar_consumed(tmp_path):
    """A bar-less run (zero data) must be detected by last_processed, not the
    tick-clock (which advances even on idle ticks)."""
    from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT, SyntheticFeed
    from fno_ai_paper_trading.paper_track.runner import empty_session, run_sessions
    from tests.paper_track_testkit import DAY0

    cfg = _config(tmp_path, account="emptyguard")
    store = TrackStore(tmp_path, "emptyguard")
    engine = run_sessions(
        config=cfg, store=store,
        feed=SyntheticFeed(TRACK_INSTRUMENT(), {}),
        days=[DAY0], resume=False,
    )
    assert empty_session(engine) is True


def test_empty_session_false_after_real_bars(tmp_path):
    from fno_ai_paper_trading.paper_track.runner import empty_session, run_sessions
    from tests.paper_track_testkit import DAY0

    cfg = _config(tmp_path, account="nonempty")
    store = TrackStore(tmp_path, "nonempty")
    feed = closes_feed([25000 + 10 * i for i in range(75)], day=DAY0)
    engine = run_sessions(config=cfg, store=store, feed=feed, days=[DAY0], resume=False)
    assert empty_session(engine) is False


def test_provider_bars_source_accepts_resolved_instrument():
    """The seam must pass the exact Instrument the provider's bars carry
    through to the engine, so real bars ('Nifty 50') are never rejected by the
    bar validator as "unexpected instrument 'Nifty 50'"."""
    from datetime import datetime

    from fno_ai_paper_trading.models.enums import InstrumentType
    from fno_ai_paper_trading.models.instruments import Instrument
    from fno_ai_paper_trading.paper_track.runner import ProviderBarsSource

    inst = Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="Nifty 50",
        exchange="NSE",
        lot_size=1,
        multiplier=1,
    )

    class _FakeProvider:
        def get_instrument(self, symbol):  # pragma: no cover - must not be called
            raise AssertionError("symbol lookup must be skipped for Instrument input")

        def get_historical_ohlcv(self, instrument, interval, start, end):
            assert instrument is inst
            return []

    source = ProviderBarsSource(_FakeProvider(), instrument=inst)
    assert source.instrument is inst
    assert source.bars_up_to(datetime(2026, 9, 21, 12, 0)) == []


def test_store_delete_report_removes_report_and_sidecar(tmp_path):
    from fno_ai_paper_trading.paper_track.runner import run_sessions
    from tests.paper_track_testkit import DAY0

    account = "delrep"
    cfg = _config(tmp_path, account=account)
    store = TrackStore(tmp_path, account)
    feed = closes_feed([25100 + 5 * i for i in range(75)], day=DAY0)
    run_sessions(config=cfg, store=store, feed=feed, days=[DAY0], resume=False)
    report = store.report_path(DAY0)
    sha = Path(str(report) + ".sha256")
    assert report.exists() and sha.exists()
    store.delete_report(DAY0)
    assert not report.exists() and not sha.exists()
    assert store.load_report(DAY0) is None