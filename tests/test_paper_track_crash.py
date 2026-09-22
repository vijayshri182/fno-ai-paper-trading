"""Phase 4 — crash points A-J: restart determinism (no double-fill, no loss).

Every failpoint raises :class:`InjectedFailure` mid-step. Whatever was or was
not persisted, restarting the same account/day from checkpoints (or from a
clean slate when none exists) must reproduce the *exact* clean-run economics:
identical fingerprint, a single entry/exit, a written report, flat EOD.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.paper_track.errors import InjectedFailure
from tests.paper_track_testkit import DAY0, closes_feed, day_ticks, drive, make_engine

# Bar plans (ordinals follow the engine's bar-completion sequence):
# * entry on bar 40 (12:35 bar, processed 12:40): forces a mid-day entry for E/F.
# * entry on bar 70 (15:05 bar, processed 15:10): survives to the 15:20 flatten
#   window so I/J are reachable.
PLAN_ENTRY_40 = {40: Signal.BUY}
PLAN_FLATTEN = {70: Signal.BUY}


def _crash_and_restart(tmp_path, point: str, plan: dict | None = None, closes=None):
    """Run a day under failpoint ``point``; on crash, restart and finish."""
    from fno_ai_paper_trading.strategies.base import Strategy

    strat = _plan_strategy(plan)
    engine, store, feed = make_engine(tmp_path, days=[DAY0], strategy=strat, failpoints={point})
    if closes is not None:
        engine.bars_source = closes_feed(closes)
    ticks = day_ticks(DAY0)

    crashed_at = None
    for i, tick in enumerate(ticks):
        engine.clock.set(tick)
        try:
            engine.step()
        except InjectedFailure as exc:
            assert exc.point == point
            crashed_at = i
            break
    assert crashed_at is not None, f"failpoint {point} never fired"

    engine2, store2, feed2 = make_engine(tmp_path, days=[DAY0], strategy=strat, failpoints=None)
    if closes is not None:
        engine2.bars_source = closes_feed(closes)
    engine2.load(DAY0)  # loads the checkpoint if any was written before the crash
    # Resume from the *crashed tick itself*: a crashed tick never persisted its
    # work (end-state was already restored by load()), so re-running it restores
    # the exact clean schedule instead of drifting one tick behind it.
    for tick in ticks[crashed_at:]:
        engine2.clock.set(tick)
        engine2.step()
    engine2.save_report_payload()
    return engine2


def _plan_strategy(plan):
    from tests.test_paper_track_orders import CycleStrategy

    return CycleStrategy(plan=plan or {})


@pytest.mark.parametrize(
    "point,plan",
    [
        ("A", PLAN_ENTRY_40),  # crash right after the provider fetch, before any work
        ("B", PLAN_ENTRY_40),  # crash before any signal analysis on the first bar
        ("C", PLAN_ENTRY_40),  # crash after risk approval, before order placement
        ("D", PLAN_ENTRY_40),  # crash just before broker.place_order (entry)
        ("E", PLAN_ENTRY_40),  # crash before the entry/exit order hits the broker
        ("F", PLAN_ENTRY_40),  # crash after the broker fill, before applying it
        ("G", PLAN_ENTRY_40),  # crash after consuming a bar, before checkpoint
        ("H", PLAN_ENTRY_40),  # crash after checkpoint, before report finalisation
        ("I", PLAN_FLATTEN),  # crash before the EOD flatten order
        ("J", PLAN_FLATTEN),  # crash after the flatten fill, before commit
    ],
)
def test_crash_restart_reproduces_clean_run(tmp_path, point, plan):
    strat = _plan_strategy(plan)
    # Clean reference run (no crash).
    ref, _, _ = make_engine(tmp_path, days=[DAY0], strategy=strat, run_id="ref")
    drive(ref, day_ticks(DAY0))
    clean_fp = ref.stable_fingerprint()

    engine2 = _crash_and_restart(tmp_path, point, plan=plan)

    assert engine2.stable_fingerprint() == clean_fp, f"failpoint {point} lost or duplicated a fill"
    assert engine2.position_quantity == 0
    assert engine2.eod_status in ("FLAT", "FLATTENED")
    assert engine2.counters["errors"] == []
    assert engine2.counters["entries_filled"] == 1
    assert engine2.counters["exits_filled"] == 1
    assert ref.counters["entries_filled"] == 1 and ref.counters["exits_filled"] == 1
    # Report persisted after the successful restart.
    report = engine2.store.load_report(DAY0)
    assert report is not None


def test_crash_at_every_tick_still_lands_flat_and_recoverable(tmp_path):
    # Even brutal failure injection at an arbitrary tick can always restart.
    for point in "ABCDEFGH":
        engine, store, feed = make_engine(tmp_path, days=[DAY0], strategy=_plan_strategy(PLAN_ENTRY_40),
                                          failpoints={point}, run_id=f"hard-{point}")
        try:
            drive(engine, day_ticks(DAY0))
        except InjectedFailure:
            pass
        engine2, _, _ = make_engine(tmp_path, days=[DAY0], strategy=_plan_strategy(PLAN_ENTRY_40), run_id=f"retry-{point}")
        engine2.load(DAY0)
        drive(engine2, day_ticks(DAY0))
        assert engine2.position_quantity == 0
        assert engine2.counters["errors"] == []
        assert len(engine2.broker.fills) % 2 == 0  # any day is round-trips only