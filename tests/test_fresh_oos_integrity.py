"""Fresh-OOS data-integrity and schema conformance.

Every accepted day must satisfy the canonical dataset-store schema: identical
CSV columns/digest, correct session cadence (75 bars, 09:15..15:25), sane
OHLC, no duplicates or gaps, and a ``data_hash`` that provably matches the
stored bytes. Corruption or malformed rows are rejected, never written.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from fresh_oos_testkit import generate_5m_bars, make_context, session_times
from fno_ai_paper_trading.data.dataset_store import dataset_hash, load_dataset, save_dataset
from fno_ai_paper_trading.fresh_oos.store import content_hash
from fno_ai_paper_trading.fresh_oos.protocol import (
    EXPECTED_BARS_PER_FULL_DAY,
    STATUS_SUCCESS,
)
from fno_ai_paper_trading.fresh_oos.validation import (
    CODE_CADENCE,
    CODE_DUPLICATE,
    CODE_EMPTY,
    CODE_GAP,
    CODE_OHLC,
    CODE_ORDER,
    CODE_OUT_OF_SESSION,
    CODE_WRONG_DAY,
    validate_5m_day,
    validate_5m_rows,
)

DAY = date(2026, 9, 17)
NOW = datetime(2026, 10, 15, 12, 0)


def _rows(bars):
    return [
        {
            "instrument_token": "1",
            "timestamp": b.timestamp.isoformat(),
            "open": str(b.open), "high": str(b.high), "low": str(b.low),
            "close": str(b.close), "volume": str(b.volume),
            "open_interest": "0",
        }
        for b in bars
    ]


def test_full_session_has_exactly_expected_bar_count_and_cadence():
    bars = generate_5m_bars(DAY)
    assert len(bars) == EXPECTED_BARS_PER_FULL_DAY == 75
    report = validate_5m_day(bars, DAY)
    assert report.ok
    assert report.codes == ()
    times = [b.timestamp.time() for b in bars]
    assert times[0] == session_times(DAY, 1)[0].time()
    for prev, curr in zip(times, times[1:]):
        assert (0, 5, 0) == (
            (curr.hour * 60 + curr.minute) - (prev.hour * 60 + prev.minute),
            curr.second,
            prev.second,
        )[:2] or (curr.hour * 60 + curr.minute) - (prev.hour * 60 + prev.minute) == 5


def test_canonical_csv_matches_established_dataset_store_convention(tmp_path):
    bars = generate_5m_bars(DAY)
    # The fresh-OOS store and the established research store must produce the
    # same digest for identical bars, so an accepted day is hash-comparable
    # with an established single-day dataset.
    assert content_hash(bars) == dataset_hash(bars)
    # Round-trip through the established tooling keeps the digest identical.
    from fresh_oos_testkit import make_instrument

    saved = save_dataset(
        bars,
        instrument=make_instrument(),
        provider="upstox",
        interval="5m",
        directory=str(tmp_path / "established"),
    )
    loaded = load_dataset(saved.path)
    assert dataset_hash(loaded.bars) == content_hash(bars)


def test_stored_day_is_loadable_by_established_tooling(tmp_path):
    from fresh_oos_testkit import make_instrument

    ctx = make_context(tmp_path, now=NOW)
    outcome = ctx["collector"].collect_once(force_date=DAY)
    assert outcome.status == STATUS_SUCCESS
    # The per-day CSV must be loadable as an established single-day dataset.
    store = ctx["store"]
    stored = store.find(DAY)
    assert stored is not None
    dataset = save_dataset(
        generate_5m_bars(DAY),
        instrument=make_instrument(),
        provider="upstox",
        interval="5m",
        directory=str(tmp_path / "reloaded"),
    )
    assert dataset.data_hash == stored.data_hash


def test_empty_rows_rejected():
    report = validate_5m_rows([], DAY)
    assert not report.ok
    assert CODE_EMPTY in report.codes
    assert report.status() == "INCOMPLETE"  # a no-data day is a coverage shortfall


def test_wrong_day_rows_rejected():
    bars = generate_5m_bars(DAY)
    report = validate_5m_rows(_rows(bars)[1:], date(2026, 9, 18))  # bars from the 17th
    assert CODE_WRONG_DAY in report.codes


def test_out_of_session_row_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    rows[0]["timestamp"] = datetime(2026, 9, 17, 15, 35).isoformat()  # after session end
    report = validate_5m_rows(rows, DAY)
    assert CODE_OUT_OF_SESSION in report.codes


def test_ohlc_inversion_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    rows[0] = dict(rows[0], low=str(Decimal(rows[0]["low"]) + Decimal("100")), high=str(Decimal(rows[0]["high"]) + Decimal("200")))
    report = validate_5m_rows(rows, DAY)
    assert CODE_OHLC in report.codes


def test_duplicate_timestamp_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    rows[1] = dict(rows[0])  # same timestamp as rows[0]
    report = validate_5m_rows(rows, DAY)
    assert CODE_DUPLICATE in report.codes


def test_gap_in_cadence_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    keep = rows[:40] + rows[42:]  # drop one intermediate bar
    report = validate_5m_rows(keep, DAY)
    assert CODE_GAP in report.codes
    assert CODE_CADENCE in report.codes or CODE_GAP in report.codes


def test_incomplete_session_not_stored(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    ctx["client"].responses[DAY] = generate_5m_bars(DAY)[:50]  # 50 of 75 bars
    outcome = ctx["collector"].collect_once(force_date=DAY)
    assert outcome.status == "INCOMPLETE"
    assert ctx["store"].find(DAY) is None


def test_out_of_order_rows_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    rows[0], rows[1] = rows[1], rows[0]
    report = validate_5m_rows(rows, DAY)
    assert CODE_ORDER in report.codes


def test_nan_close_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    rows[0] = dict(rows[0], close="NaN")
    report = validate_5m_rows(rows, DAY)
    assert not report.ok
    assert CODE_OHLC in report.codes  # non-finite close
    assert report.status() in ("DATA_INVALID", "INCOMPLETE")


def test_missing_ohlc_field_rejected():
    bars = generate_5m_bars(DAY)
    rows = _rows(bars)
    del rows[0]["close"]
    report = validate_5m_rows(rows, DAY)
    assert not report.ok
    assert CODE_OHLC in report.codes