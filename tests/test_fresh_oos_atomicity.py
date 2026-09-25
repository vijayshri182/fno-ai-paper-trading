"""Fresh-OOS atomicity and immutability guarantees.

A day is only ever visible through its three artifacts together, all fsynced
before any rename. A crashed/interrupted write must leave no half-accepted day
and no scratch litter; a failing second write must never overwrite the first,
and a re-read after any write must re-verify the content hash (corruption is an
error, not a silent accept).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from fresh_oos_testkit import generate_5m_bars, make_context
from fno_ai_paper_trading.fresh_oos.errors import DataInvalidError
from fno_ai_paper_trading.fresh_oos.protocol import STATUS_DATA_CONFLICT, STATUS_SUCCESS
from fno_ai_paper_trading.fresh_oos.store import content_hash

DAY = date(2026, 9, 17)
NOW = datetime(2026, 10, 15, 12, 0)


def test_write_leaves_no_scratch_and_three_artifacts(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    bars = generate_5m_bars(DAY)
    hash_ = content_hash(bars)
    stored = ctx["store"].write_day(DAY, bars, instrument=ctx["config"].instrument,
                                    data_hash=hash_, downloaded_at="now")
    assert stored.data_hash == hash_
    day_dir = ctx["store"].day_dir(DAY)
    artifacts = sorted(p.name for p in day_dir.iterdir())
    assert artifacts == ["data.csv", "metadata.json", "sha256.txt"]
    assert not list(ctx["root"].rglob(".tmp-*"))  # no scratch litter


def test_second_write_never_overwrites_accepted_day(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    bars = generate_5m_bars(DAY)
    ctx["store"].write_day(DAY, bars, instrument=ctx["config"].instrument,
                           data_hash=content_hash(bars), downloaded_at="now")
    other = generate_5m_bars(DAY, count=75)
    with pytest.raises(FileExistsError):
        ctx["store"].write_day(DAY, other, instrument=ctx["config"].instrument,
                               data_hash=content_hash(other), downloaded_at="later")
    stored = ctx["store"].find(DAY)
    assert stored.num_bars == 75


def test_collector_level_race_resolves_to_conflict_not_overwrite(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    first = ctx["collector"].collect_once(force_date=DAY)
    assert first.status == STATUS_SUCCESS
    original_hash = ctx["manifest"].pool[DAY.isoformat()]["data_hash"]
    ctx["client"].altered.add(DAY)  # different hash on the next fetch
    second = ctx["collector"].collect_once(force_date=DAY)
    assert second.status == STATUS_DATA_CONFLICT
    stored = ctx["store"].find(DAY)
    assert stored.data_hash == original_hash  # accepted content untouched


def test_partial_artifact_set_is_rejected_on_read(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    bars = generate_5m_bars(DAY)
    ctx["store"].write_day(DAY, bars, instrument=ctx["config"].instrument,
                           data_hash=content_hash(bars), downloaded_at="now")
    # simulate a torn write: drop sha256.txt
    (ctx["store"].day_dir(DAY) / "sha256.txt").unlink()
    with pytest.raises(DataInvalidError):
        ctx["store"].find(DAY)


def test_corrupted_csv_is_rejected_on_read(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    bars = generate_5m_bars(DAY)
    ctx["store"].write_day(DAY, bars, instrument=ctx["config"].instrument,
                           data_hash=content_hash(bars), downloaded_at="now")
    csv_path = ctx["store"].day_dir(DAY) / "data.csv"
    text = csv_path.read_text(encoding="utf-8")
    csv_path.write_text(text.replace("09:15", "09:17"), encoding="utf-8")
    with pytest.raises(DataInvalidError):
        ctx["store"].find(DAY)


def test_manifest_save_is_single_atomic_replace(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    ctx["collector"].collect_once(force_date=DAY)
    ctx["manifest"].generated_at = "regen"
    ctx["manifest"].save()
    assert not list(ctx["root"].glob(".tmp-*"))
    reloaded = ctx["manifest"].load(ctx["manifest"].path)
    assert reloaded.pool[DAY.isoformat()]["status"] == "ACQUIRED"


def test_write_computes_hash_when_unavailable(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    bars = generate_5m_bars(DAY)
    stored = ctx["store"].write_day(DAY, bars, instrument=ctx["config"].instrument,
                                    data_hash="", downloaded_at="now")
    assert stored.data_hash == content_hash(bars)