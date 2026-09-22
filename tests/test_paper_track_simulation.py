"""Phases 10-11, 13 — the long deterministic simulation.

Thirty NSE sessions over one cumulative account engine. Properties:
* every day ends flat (per-day checkpoint eod_status) with all 15 invariants
* the day-end ledger reconciles (cash identity, commission identity)
* reruns are byte-identical; a mid-run crash-restart reproduces the full run
* resource bounds hold (equity curve length, ledger size, store size)

Most tests share one 30-day reference run (module scope). Marked ``slow``:
each full run writes a checkpoint per bar, so these take a few minutes.
"""

from __future__ import annotations

import pytest

from fno_ai_paper_trading.paper_track.engine import TrackEngine, TrackConfig
from fno_ai_paper_trading.paper_track.feed import trading_day_sequence
from fno_ai_paper_trading.paper_track.invariants import verify_day_end_invariants
from fno_ai_paper_trading.paper_track.store import TrackStore
from tests.paper_track_testkit import DAY0, full_ticks, make_engine

DAYS = trading_day_sequence(DAY0, 30)
SEED = 555


def _run_all(store_dir, *, account="sim", seed=SEED) -> TrackEngine:
    engine, store, feed = make_engine(store_dir, account=account, days=DAYS, seed=seed)
    for tick in full_ticks(DAYS):
        engine.clock.set(tick)
        engine.step()
    engine.save_report_payload()
    return engine


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    store_dir = tmp_path_factory.mktemp("ptsim")
    engine = _run_all(store_dir, account="sim", seed=SEED)
    return engine, store_dir


@pytest.mark.slow
def test_30_day_run_ends_flat_and_reconciled(sim):
    engine, _ = sim
    assert engine.position_quantity == 0
    assert engine.eod_status in ("FLAT", "FLATTENED")
    assert engine.counters["data_skips"] == []
    assert engine.counters["anomalies"] == []
    assert engine.counters["errors"] == []
    assert len(engine.consumed) == 30 * 75
    assert len(engine.history) == 30 * 75
    assert len(engine.equity_curve) == 30 * 75 + 1
    assert len(engine.broker.fills) % 2 == 0
    orders, fills = engine.broker.snapshot()
    assert len(orders) == len(fills)
    assert len(engine.portfolio.trade_history) == len(fills)


@pytest.mark.slow
def test_per_day_reports_and_checkpoints_written(sim):
    engine, store_dir = sim
    reports = list(engine.store.reports_dir.glob(f"{engine.config.account}.*.json"))
    assert len(reports) == 30
    checkpoints = list(engine.store.checkpoints_dir.glob(f"{engine.config.account}.*.json"))
    assert len(checkpoints) == 30


@pytest.mark.slow
def test_every_day_invariants_hold(sim):
    _, store_dir = sim
    for day in DAYS:
        engine2, _, _ = make_engine(store_dir, days=DAYS, account="sim")
        engine2.config = TrackConfig(account="sim", store_dir=store_dir)
        engine2.store = TrackStore(store_dir, "sim")
        assert engine2.load(day), f"missing checkpoint for {day}"
        violations = verify_day_end_invariants(engine2)
        assert violations == [], (day, violations)


@pytest.mark.slow
def test_rerun_is_byte_identical(tmp_path, sim):
    ref_engine, _ = sim
    rerun = _run_all(tmp_path, account="rerun", seed=SEED)
    assert rerun.stable_fingerprint() == ref_engine.stable_fingerprint()
    assert rerun.portfolio.realized_pnl == ref_engine.portfolio.realized_pnl


@pytest.mark.slow
def test_mid_run_restart_reproduces_full_run(tmp_path, sim):
    ref_engine, _ = sim
    first = DAYS[:15]
    rest = DAYS[15:]
    engine, store, feed = make_engine(tmp_path, account="split", days=DAYS, seed=SEED)
    for tick in full_ticks(first):
        engine.clock.set(tick)
        engine.step()

    engine2, _, _ = make_engine(tmp_path, account="split", days=DAYS, seed=SEED)
    assert engine2.load(DAYS[14])
    for tick in full_ticks(rest):
        engine2.clock.set(tick)
        engine2.step()
    engine2.save_report_payload()
    assert engine2.stable_fingerprint() == ref_engine.stable_fingerprint()


@pytest.mark.slow
def test_store_size_stays_bounded(sim):
    engine, store_dir = sim
    total = 0
    files = 0
    for path in store_dir.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    # Checkpoints embed the cumulative history needed for byte-exact resumption,
    # so growth is O(sessions^2 * bars); assert it stays far below any "leak"
    # multiple and that nothing extra is being created.
    assert files >= 60, files
    assert total < 20 * 1024 * 1024, f"store inflated to {total} bytes"


def test_session_count_matches_requested_days():
    assert len(DAYS) == 30
    assert all(d.weekday() < 5 for d in DAYS)  # NSE weekdays only