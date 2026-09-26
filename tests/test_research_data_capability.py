"""Tests for DATA-001 - RESEARCH DATA CAPABILITY AUDIT.

The tests verify the dataset validation logic, the generic dataset validator,
the source-discovery inventory, the strategy coverage matrix, and the
integrity guarantees (all 15 protected artifact SHAs UNCHANGED).  They do NOT
touch or modify the protected internal algorithm (model_0 /
Iteration-009..012 / protected OOS) nor the PSB-001/PSB-002 artifacts.
"""
from __future__ import annotations

import json

import pytest

from fno_ai_paper_trading.research.research_data_capability import (
    BAR_SECONDS,
    DATA_SOURCES,
    DATASET,
    EXPECTED_COLUMNS,
    EXPECTED_ROWS,
    MARKET_CLOSE,
    MARKET_OPEN,
    META_PATH,
    OUT_JSON,
    PROTECTED_SHAS,
    RESEARCH_END,
    RESEARCH_START,
    STRATEGY_COVERAGE_MATRIX,
    TIMEFRAME,
    TZ,
    _all_trading_days,
    _bars_to_seconds,
    load_csv,
    validate_baseline,
    validate_dataset,
    verify_integrity,
)


@pytest.fixture(scope="module")
def baseline_audit():
    return validate_baseline(DATASET, META_PATH)


@pytest.fixture(scope="module")
def artifact():
    return json.loads(OUT_JSON.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------
# 1. Constants and paths
# -----------------------------------------------------------------------

def test_dataset_exists():
    assert DATASET.exists()
    assert META_PATH.exists()


def test_out_json_exists():
    assert OUT_JSON.exists()


def test_constants():
    assert EXPECTED_ROWS == 87193
    assert EXPECTED_COLUMNS == [
        "timestamp", "open", "high", "low", "close", "volume", "open_interest",
    ]
    assert TIMEFRAME == "5m"
    assert TZ == "Asia/Kolkata"
    assert MARKET_OPEN == "09:15"
    assert MARKET_CLOSE == "15:30"
    assert BAR_SECONDS == 300
    assert RESEARCH_START == "2022-01-03"
    assert RESEARCH_END == "2025-10-03"


def test_bars_to_seconds():
    assert _bars_to_seconds("1m") == 60
    assert _bars_to_seconds("5m") == 300
    assert _bars_to_seconds("15m") == 900
    assert _bars_to_seconds("1h") == 3600


# -----------------------------------------------------------------------
# 2. Baseline dataset audit through validate_baseline
# -----------------------------------------------------------------------

def test_row_count(baseline_audit):
    assert baseline_audit["stats"]["total_rows"] == EXPECTED_ROWS


def test_column_count(baseline_audit):
    assert len(baseline_audit["stats"]["columns"]) == len(EXPECTED_COLUMNS)


def test_timestamps_monotonic(baseline_audit):
    assert baseline_audit["checks"]["timestamps_monotonic"] == "PASS"


def test_ohlc_consistent(baseline_audit):
    assert baseline_audit["checks"]["ohlc_consistency"] == "PASS"
    assert baseline_audit["stats"]["ohlc_violations"] == 0


def test_volume_all_zero(baseline_audit):
    assert baseline_audit["checks"]["volume_all_zero"] == "PASS"
    assert baseline_audit["checks"]["open_interest_all_zero"] == "PASS"


def test_no_duplicates(baseline_audit):
    assert baseline_audit["checks"]["duplicate_timestamps"] == "PASS"
    assert baseline_audit["stats"]["duplicate_timestamps"] == 0


def test_research_domain(baseline_audit):
    assert baseline_audit["stats"]["research_bars"] == 69781
    assert baseline_audit["stats"]["research_days"] == 932


def test_gap_analysis_present(baseline_audit):
    gaps = baseline_audit["stats"]["gap_distribution"]
    assert gaps.get(300) == 86016
    assert baseline_audit["checks"]["gap_analysis"] == "PASS"


def test_no_errors(baseline_audit):
    assert baseline_audit["errors"] == []


def test_meta_consistency(baseline_audit):
    assert baseline_audit["checks"]["meta_instrument"] == "PASS"
    assert baseline_audit["checks"]["meta_timezone"] == "PASS"
    assert baseline_audit["checks"]["meta_interval"] == "PASS"
    assert baseline_audit["checks"]["meta_provider"] == "PASS"
    assert baseline_audit["checks"]["meta_num_bars"] == "PASS"


# -----------------------------------------------------------------------
# 3. Generic validate_dataset (any future source)
# -----------------------------------------------------------------------

def test_validate_dataset_good(tmp_path):
    csv_path = tmp_path / "test.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume,open_interest\n"
        "2022-01-03T09:15:00,1,2,0.5,1.5,100,0\n"
        "2022-01-03T09:20:00,1.5,2.5,1,2,120,0\n",
        encoding="utf-8",
    )
    res = validate_dataset(csv_path, timeframe="5m", expect_volume=True)
    assert res["checks"]["timestamps_monotonic"] == "PASS"
    assert res["checks"]["no_duplicates"] == "PASS"
    assert res["checks"]["ohlc_consistent"] == "PASS"
    assert res["checks"]["volume_present"] == "PASS"
    assert res["errors"] == []


def test_validate_dataset_bad_ohlc(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume,open_interest\n"
        "2022-01-03T09:15:00,5,2,4,3,100,0\n",
        encoding="utf-8",
    )
    res = validate_dataset(csv_path, timeframe="5m")
    assert "FAIL" in res["checks"]["ohlc_consistent"]
    assert len(res["errors"]) == 1


def test_validate_dataset_duplicates(tmp_path):
    csv_path = tmp_path / "dup.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume,open_interest\n"
        "2022-01-03T09:15:00,1,2,0.5,1.5,100,0\n"
        "2022-01-03T09:15:00,1,2,0.5,1.5,100,0\n",
        encoding="utf-8",
    )
    res = validate_dataset(csv_path, timeframe="5m")
    assert res["checks"]["no_duplicates"] == "FAIL count=1"
    assert res["stats"]["duplicate_count"] == 1


def test_validate_dataset_volume_required(tmp_path):
    csv_path = tmp_path / "vol.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume,open_interest\n"
        "2022-01-03T09:15:00,1,2,0.5,1.5,0,0\n",
        encoding="utf-8",
    )
    res = validate_dataset(csv_path, timeframe="5m", expect_volume=True)
    assert res["checks"]["volume_present"] == "FAIL all_zero"


# -----------------------------------------------------------------------
# 4. Artifact structure
# -----------------------------------------------------------------------

def test_artifact_metadata(artifact):
    assert artifact["audit_id"] == "DATA-001"
    assert artifact["research_firewall"]["research_domain"] == "2022-01-03..2025-10-03"
    assert artifact["research_firewall"]["paper_only"] is True


def test_artifact_dataset_validation(artifact):
    dv = artifact["dataset_validation"]
    assert dv["checks"]["timestamps_monotonic"] == "PASS"
    assert dv["checks"]["ohlc_consistency"] == "PASS"
    assert dv["checks"]["volume_all_zero"] == "PASS"


def test_artifact_integrity_all_unchanged(artifact):
    assert artifact["integrity_all_unchanged"] is True
    for name, _sha in PROTECTED_SHAS.items():
        assert name in artifact["integrity_verification"]
        assert artifact["integrity_verification"][name] == "UNCHANGED"


def test_artifact_safety_state(artifact):
    ss = artifact["safety_state"]
    assert ss["live_trading"] is False
    assert ss["live_gate"] == "CLOSED"
    assert ss["algo_ready"] == "NO"
    assert ss["algorithm_health"] == "RED"
    assert ss["promotion"] == "NO"


# -----------------------------------------------------------------------
# 5. Source discovery & coverage matrix
# -----------------------------------------------------------------------

def test_source_discovery_nonempty():
    assert len(DATA_SOURCES) >= 5
    ids = {s["id"] for s in DATA_SOURCES}
    assert {"upstox_api", "nse_reports", "openchart"}.issubset(ids)


def test_source_discovery_fields():
    for src in DATA_SOURCES:
        assert src["id"]
        assert src["provider"]
        assert src["timeframes"]
        assert "notes" in src


def test_coverage_matrix_core():
    m = STRATEGY_COVERAGE_MATRIX
    assert "PRICE_OHLC" in m
    assert m["PRICE_OHLC"]["available_in_baseline"] is True
    assert m["VOLUME_INDEX_5MIN"]["available_in_baseline"] is False
    assert m["VOLUME_FUTURES"]["available_in_baseline"] is False


def test_coverage_matrix_structure():
    for key, entry in STRATEGY_COVERAGE_MATRIX.items():
        assert set(["description", "available_in_baseline", "notes"]).issubset(
            set(entry.keys())
        )


# -----------------------------------------------------------------------
# 6. Live re-verification (must match baselines)
# -----------------------------------------------------------------------

def test_verify_integrity_all_unchanged():
    results = verify_integrity()
    assert len(results) == 15
    mismatches = {k: v for k, v in results.items() if v != "UNCHANGED"}
    assert mismatches == {}


def test_load_all_bars():
    bars = load_csv(DATASET)
    assert len(bars) == EXPECTED_ROWS
    assert len(_all_trading_days(bars)) >= 932