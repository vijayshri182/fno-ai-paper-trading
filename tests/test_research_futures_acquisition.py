"""Tests for DATA-002 — NIFTY FUTURES HISTORICAL DATA ACQUISITION & VALIDATION.

The DATA-002 capability audit found the expired-instruments source is blocked
(Upstox Plus, UDAPI1149), so these tests verify the *offline, deterministic*
deliverables: the planned inventory derivation, the blocked outcome record,
the artifact serialization, and the integrity guarantees (15 protected SHAs
UNCHANGED).  They never touch the protected internal algorithm (model_0 /
Iteration-009..012 / protected OOS) nor the PSB artifacts, and they never
make network calls.
"""
from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from fno_ai_paper_trading.research.research_futures_acquisition import (
    DATASETS_FUTURES,
    INVENTORY_REASON,
    INVENTORY_STATUS,
    OUT_INVENTORY_CSV,
    OUT_JSON,
    OUT_MD,
    PROTECTED_SHAS,
    RESEARCH_END,
    RESEARCH_START,
    SOURCE_CAPABILITY,
    UPSTOX_BASE_URL,
    last_thursday,
    planned_contract_inventory,
    verify_integrity,
)


@pytest.fixture(scope="module")
def artifact():
    return json.loads(OUT_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def csv_rows():
    with OUT_INVENTORY_CSV.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# -----------------------------------------------------------------------
# 1. Constants and paths
# -----------------------------------------------------------------------

def test_important_constants():
    assert RESEARCH_START == "2022-01-03"
    assert RESEARCH_END == "2025-10-03"
    assert UPSTOX_BASE_URL == "https://api.upstox.com"


def test_inventory_status_is_not_acquired():
    assert INVENTORY_STATUS == "NOT_ACQUIRED"
    assert "UPSTOX_PLUS" in INVENTORY_REASON
    assert "UDAPI1149" in INVENTORY_REASON


def test_outcome_is_blocked(artifact):
    assert artifact["outcome"] == "FAIL_SOURCE_ACCESS_BLOCKED"


def test_no_acquired_contracts_claimed(artifact, csv_rows):
    assert artifact["planned_contract_inventory"]["status"] == "NOT_ACQUIRED"
    assert artifact["planned_contract_inventory"]["count"] == len(csv_rows)
    assert all(row["status"] == "NOT_ACQUIRED" for row in csv_rows)
    assert all("UPSTOX_PLUS" in row["reason"] for row in csv_rows)


# -----------------------------------------------------------------------
# 2. Source capability audit records
# -----------------------------------------------------------------------

def test_source_capability_records_v3_works_and_expired_blocked():
    ids = {s["id"]: s for s in SOURCE_CAPABILITY}
    assert "upstox_v3_historical_candle" in ids
    assert "upstox_v2_expired_instruments" in ids
    assert ids["upstox_v3_historical_candle"]["access"] == "WORKS"
    assert ids["upstox_v2_expired_instruments"]["access"] == "BLOCKED"
    assert "UDAPI1149" in ids["upstox_v2_expired_instruments"]["blocker"]


def test_source_capability_records_legacy_paths():
    ids = {s["id"]: s for s in SOURCE_CAPABILITY}
    assert ids["legacy_index_historical"]["access"] == "DEPRECATED"
    assert ids["upstox_v2_historical_candle_symbol"]["access"] == "NOT_SERVED"


# -----------------------------------------------------------------------
# 3. Last-Thursday derivation
# -----------------------------------------------------------------------

def test_last_thursday_known_values():
    assert last_thursday(2022, 1) == date(2022, 1, 27)
    assert last_thursday(2022, 3) == date(2022, 3, 31)
    assert last_thursday(2023, 12) == date(2023, 12, 28)
    assert last_thursday(2024, 2) == date(2024, 2, 29)
    assert last_thursday(2025, 9) == date(2025, 9, 25)
    assert last_thursday(2025, 10) == date(2025, 10, 30)


def test_last_thursday_always_thursday():
    for year in (2022, 2023, 2024, 2025):
        for month in (1, 4, 7, 11):
            assert last_thursday(year, month).weekday() == 3


def test_last_thursday_last_week_of_month():
    for year in (2022, 2023, 2024, 2025):
        for month in range(1, 13):
            d = last_thursday(year, month)
            following = last_thursday(year, month)  # same month, last <= 31-7
            assert following.day >= 22
            assert d.day <= 31


# -----------------------------------------------------------------------
# 4. Planned inventory
# -----------------------------------------------------------------------

def test_inventory_spans_domain():
    inv = planned_contract_inventory()
    assert inv[0]["expiry"] == "2022-01-27"
    assert inv[-1]["expiry"] == "2025-10-30"
    assert all(RESEARCH_START <= row["expiry"] for row in inv)


def test_inventory_count_and_symbol_format(csv_rows):
    # Jan-2022 .. Oct-2025 = 46 monthly expiries (no expiry < Jan 2022 needed:
    # the Dec-2021 contract data would come from Upstox expired API, blocked).
    assert len(csv_rows) == 46
    first = csv_rows[0]
    assert first["trading_symbol"] == "NIFTY FUT 27 JAN 22"
    assert first["underlying_key"] == "NSE_INDEX|Nifty 50"
    assert first["instrument_type"] == "FUT"
    assert first["segment"] == "NSE_FO"
    last = csv_rows[-1]
    assert last["trading_symbol"] == "NIFTY FUT 30 OCT 25"
    assert last["expiry"] == "2025-10-30"


def test_no_resolved_vendor_keys_present(csv_rows):
    # There is no numeric instrument_key column: keys were never resolved
    # because the vendor endpoint is blocked. Nothing is fabricated.
    assert "instrument_key" not in csv_rows[0]


def test_inventory_dates_unique_and_sorted(csv_rows):
    expiry_dates = [row["expiry"] for row in csv_rows]
    assert len(set(expiry_dates)) == len(expiry_dates) == 46
    assert expiry_dates == sorted(expiry_dates)


# -----------------------------------------------------------------------
# 5. Artifact files exist and are consistent
# -----------------------------------------------------------------------

def test_artifacts_exist():
    assert OUT_JSON.exists()
    assert OUT_MD.exists()
    assert OUT_INVENTORY_CSV.exists()
    assert DATASETS_FUTURES.is_dir()


def test_md_marks_blocked_outcome():
    assert "FAIL_SOURCE_ACCESS_BLOCKED" in OUT_MD.read_text(encoding="utf-8")
    assert "UPSTOX_PLUS_REQUIRED_UNKNOWN_UDAPI1149" in OUT_MD.read_text(encoding="utf-8")


def test_json_integrity_section(artifact):
    assert artifact["integrity_all_unchanged"] is True
    assert all(v == "UNCHANGED" for v in artifact["integrity_verification"].values())
    assert len(artifact["integrity_verification"]) == 15


def test_safety_state(artifact):
    safety = artifact["safety_state"]
    assert safety["live_trading"] is False
    assert safety["live_gate"] == "CLOSED"
    assert safety["algo_ready"] == "NO"
    assert safety["promotion"] == "NO"


# -----------------------------------------------------------------------
# 6. Integrity helper
# -----------------------------------------------------------------------

def test_verify_integrity_all_unchanged():
    result = verify_integrity()
    assert len(result) == len(PROTECTED_SHAS) == 15
    assert all(v == "UNCHANGED" for v in result.values())