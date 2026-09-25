"""Fresh-OOS idempotency and catch-up semantics.

Re-running the collector must never change already-accepted data: same-day
re-acquisition is a NOOP, hash drift is a DATA_CONFLICT, and a full pass after
a partial failure re-acquires only the still-missing days. Established
``datasets/`` single-day files are reused (NOOP_ESTABLISHED) instead of
re-collected.
"""
from __future__ import annotations

from datetime import date, datetime

from fresh_oos_testkit import FakeHistoricalDataClient, generate_5m_bars, make_context
from fno_ai_paper_trading.data.dataset_store import save_dataset
from fno_ai_paper_trading.fresh_oos.protocol import (
    POOL_NOOP_ESTABLISHED,
    STATUS_DATA_CONFLICT,
    STATUS_NO_NEW_DATA,
    STATUS_SUCCESS,
)
from fresh_oos_testkit import make_instrument

NOW = datetime(2026, 10, 15, 12, 0)
CATCHUP_NOW = datetime(2026, 9, 25, 12, 0)
D1 = date(2026, 9, 17)
D2 = date(2026, 9, 18)


def test_same_day_reacquisition_is_noop_with_identical_hash(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    first = ctx["collector"].collect_once(force_date=D1)
    assert first.status == STATUS_SUCCESS
    second = ctx["collector"].collect_once(force_date=D1)
    assert second.status == STATUS_NO_NEW_DATA
    assert D1.isoformat() in second.noop
    # the manifest never drops the accepted day across re-runs
    assert D1 in ctx["manifest"].accepted_dates
    entry = ctx["manifest"].pool[D1.isoformat()]
    assert entry["status"] in ("ACQUIRED", "NOOP")
    assert ctx["store"].find(D1) is not None


def test_hash_drift_on_reacquisition_is_conflict(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    ctx["client"].altered.add(D1)  # second fetch returns different bars
    first = ctx["collector"].collect_once(force_date=D1)
    assert first.status == STATUS_SUCCESS
    second = ctx["collector"].collect_once(force_date=D1)
    assert second.status == STATUS_DATA_CONFLICT
    assert D1.isoformat() in second.conflicts
    # the originally accepted content is untouched, and its fact remains
    stored = ctx["store"].find(D1)
    assert stored is not None
    assert stored.data_hash == ctx["manifest"].pool[D1.isoformat()]["data_hash"]


def test_catch_up_after_partial_failure_acquires_missing_days_only(tmp_path):
    ctx = make_context(tmp_path, now=CATCHUP_NOW)
    ctx["client"].failures[D2] = ConnectionError("transient")
    ctx["collector"].collect_once()
    assert ctx["store"].find(D1) is not None
    assert ctx["store"].find(D2) is None
    ctx["client"].failures.pop(D2)
    second = ctx["collector"].collect_once()
    assert second.status in (STATUS_SUCCESS, STATUS_NO_NEW_DATA)
    assert ctx["store"].find(D2) is not None
    assert D1.isoformat() in second.noop  # already-accepted days are NOOPs
    assert D2.isoformat() in second.accepted


def test_two_passes_produce_identical_accepted_pool(tmp_path):
    ctx = make_context(tmp_path, now=CATCHUP_NOW)
    ctx["collector"].collect_once()
    first_pool = {(d, e["data_hash"]) for d, e in ctx["manifest"].pool.items()}
    second = ctx["collector"].collect_once()
    second_pool = {(d, e["data_hash"]) for d, e in ctx["manifest"].pool.items()}
    assert first_pool == second_pool
    assert second.accepted == ()  # nothing new on a repeat pass
    assert second.noop  # previously acquired days are NOOPs


def test_established_datasets_file_reused_not_recollected(tmp_path):
    bars = generate_5m_bars(D1)
    save_dataset(
        bars,
        instrument=make_instrument(),
        provider="upstox",
        interval="5m",
        directory=str(tmp_path / "datasets"),
    )
    client = FakeHistoricalDataClient()
    ctx = make_context(tmp_path, now=NOW, client=client, verify_present=False)
    outcome = ctx["collector"].collect_once(force_date=D1)
    assert outcome.status == STATUS_NO_NEW_DATA
    assert D1.isoformat() in outcome.noop
    assert ctx["manifest"].pool[D1.isoformat()]["status"] == POOL_NOOP_ESTABLISHED
    assert ctx["store"].find(D1) is None  # not duplicated into the store
    assert client.calls == []  # never fetched or re-collected the established day