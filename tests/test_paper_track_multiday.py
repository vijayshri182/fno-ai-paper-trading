"""Phase 6 — multi-day persistent paper account (resume across processes).

The engine already persists one day atomically (checkpoints + reports + hashed
sidecars).  Phase 6 turns it into a *persistent account*: each new trading day
resumes the latest checkpoint, so cash, realized P&L and the trade ledger carry
forward instead of being reset to the opening capital.  Everything here runs
fully offline on deterministic synthetic feeds.

Covered contract:

* cash carries forward: next day opens on the prior closing equity;
* realized P&L and the trade ledger accumulate over days;
* the account capital is never accidentally reset (the original deposit stays
  the accounting base even after a resume);
* every day stays flat overnight and produces its own persisted report;
* restart determinism across a day boundary (identical accounts replay to
  identical fingerprints);
* a completed day is refused on resume (duplicate protection);
* atomic, hashed persistence is enforced (torn state is refused on read).
"""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.paper_track.engine import TrackConfig
from fno_ai_paper_trading.paper_track.errors import PaperTrackError, TrackCheckpointError
from fno_ai_paper_trading.paper_track.invariants import verify_day_end_invariants
from fno_ai_paper_trading.paper_track.report import assert_report_clean
from fno_ai_paper_trading.paper_track.runner import run_sessions
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from tests.paper_track_testkit import closes_feed

DAY1 = date(2026, 9, 21)  # Monday
DAY2 = date(2026, 9, 22)  # Tuesday
BASE_CAPITAL = Decimal("100000")


class PlanStrategy(Strategy):
    """Deterministic BUY at ``buy`` / SELL at ``sell`` (bar open times)."""

    name = "plan"

    def __init__(self, buy: time | None = None, sell: time | None = None) -> None:
        self.buy = buy
        self.sell = sell

    def analyze(self, bars):
        last = bars[-1]
        moment = last.timestamp.time()
        if self.buy is not None and moment == self.buy:
            return SignalResult(Signal.BUY, last.instrument, last.timestamp, "plan buy")
        if self.sell is not None and moment == self.sell:
            return SignalResult(Signal.SELL, last.instrument, last.timestamp, "plan sell")
        return SignalResult(Signal.HOLD, last.instrument, last.timestamp, "plan hold")


def _config(tmp_path, account="account") -> TrackConfig:
    return TrackConfig(account=account, store_dir=Path(tmp_path))


def _day1_feed():
    return closes_feed([25000 + 40 * i for i in range(75)], day=DAY1)


def _day2_feed():
    return closes_feed([26000 - 30 * i for i in range(75)], day=DAY2)


def _run_day(config, store, feed, day, run_id=None):
    return run_sessions(
        config=config, store=store, feed=feed, days=[day], resume=False, run_id=run_id
    )


def _two_day_account(tmp_path, account="account"):
    """Day1 (up-day) then Day2 (down-day) over one persistent account."""
    strategy = PlanStrategy(buy=time(10, 0), sell=time(12, 0))
    config = TrackConfig(account=account, store_dir=Path(tmp_path), strategy=strategy)
    store = TrackStore(Path(tmp_path), account)
    engine1 = _run_day(config, store, _day1_feed(), DAY1, run_id="run-1")
    engine2 = run_sessions(
        config=config, store=store, feed=_day2_feed(), days=[DAY2],
        resume=True, run_id="run-2",
    )
    return engine1, engine2, store


def _cumulative_fees(engine) -> Decimal:
    return sum((t.commission for t in engine.portfolio.trade_history), Decimal("0"))


def _cumulative_gross(engine) -> Decimal:
    return sum((t.realized_pnl for t in engine.portfolio.trade_history), Decimal("0"))


def test_cash_carries_forward_across_days(tmp_path):
    engine1, engine2, _ = _two_day_account(tmp_path)
    assert engine1.position_quantity == 0
    assert engine2.position_quantity == 0
    # day2 did not reset to the opening capital
    assert engine2.portfolio.cash != BASE_CAPITAL
    # the closing cash reconciles against the ORIGINAL deposit, cumulatively
    assert engine2.portfolio.cash == BASE_CAPITAL + _cumulative_gross(engine2) - _cumulative_fees(engine2)


def test_realized_pnl_and_ledger_accumulate(tmp_path):
    engine1, engine2, _ = _two_day_account(tmp_path)
    assert len(engine2.portfolio.trade_history) > len(engine1.portfolio.trade_history)
    assert len(engine2.history) == 2 * 75
    assert engine2.portfolio.realized_pnl == _cumulative_gross(engine2)
    assert engine2.portfolio.realized_pnl != engine1.portfolio.realized_pnl


def test_original_deposit_is_never_lost_on_resume(tmp_path):
    _, engine2, _ = _two_day_account(tmp_path)
    assert engine2.portfolio.initial_cash == BASE_CAPITAL
    violations = verify_day_end_invariants(engine2)
    assert violations == [], violations
    report = engine2.report()
    assert report["accounting"]["initial_cash"] == "100000"
    assert report["accounting"]["violations"] == []
    assert assert_report_clean(report) == []


def test_flat_overnight_and_daily_results_persist(tmp_path):
    engine1, engine2, store = _two_day_account(tmp_path)
    assert engine1.eod_status in ("FLAT", "FLATTENED")
    assert engine2.eod_status in ("FLAT", "FLATTENED")
    for day in (DAY1, DAY2):
        report = store.load_report(day)
        assert report is not None
        assert report["trading_date"] == day.isoformat()
        assert report["paper_only"] is True
        assert report["accounting"]["final_position_quantity"] == 0
        assert report["accounting"]["violations"] == []
    checkpoints = list(store.checkpoints_dir.glob(f"{store.account}.*.json"))
    assert len(checkpoints) == 2
    reports = list(store.reports_dir.glob(f"{store.account}.*.json"))
    assert len(reports) == 2


def test_restart_across_day_boundary_is_deterministic(tmp_path):
    strategy = PlanStrategy(buy=time(10, 0), sell=time(12, 0))
    fingerprints = []
    for account in ("determ-a", "determ-b"):
        config = TrackConfig(account=account, store_dir=Path(tmp_path), strategy=strategy)
        store = TrackStore(Path(tmp_path), account)
        _run_day(config, store, _day1_feed(), DAY1, run_id=f"{account}-d1")
        engine2 = run_sessions(
            config=config, store=store, feed=_day2_feed(), days=[DAY2],
            resume=True, run_id=f"{account}-d2",
        )
        fingerprints.append(engine2.stable_fingerprint())
    # two independent resumed accounts (same feeds) replay to identical economics
    assert fingerprints[0] == fingerprints[1]


def test_completed_day_refused_on_resume(tmp_path):
    config = _config(tmp_path, account="dup")
    store = TrackStore(Path(tmp_path), "dup")
    _run_day(config, store, _day1_feed(), DAY1)
    assert store.load_report(DAY1) is not None
    with pytest.raises(PaperTrackError, match="already completed"):
        run_sessions(config=config, store=store, feed=_day1_feed(), days=[DAY1], resume=True)


def test_torn_report_is_refused_on_read(tmp_path):
    _, _, store = _two_day_account(tmp_path)
    report_path = store.report_path(DAY1)
    report_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(TrackCheckpointError):
        store.load_report(DAY1)


def test_missing_sha_sidecar_is_refused(tmp_path):
    _, _, store = _two_day_account(tmp_path)
    report_path = store.report_path(DAY1)
    report_path.with_name(report_path.name + ".sha256").unlink()
    with pytest.raises(TrackCheckpointError):
        store.load_report(DAY1)


def test_atomic_writes_leave_no_tmp_residue(tmp_path):
    _, _, store = _two_day_account(tmp_path)
    leftovers = [p for p in Path(tmp_path).rglob("*.tmp")]
    assert leftovers == []