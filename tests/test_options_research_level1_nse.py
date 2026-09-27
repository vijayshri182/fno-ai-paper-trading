"""Hermetic tests for the DATA-005 NSE F&O EOD Level-1 daily acquisition pipeline.

Covers acceptance criteria AC-1..AC-8 for `level1_nse` and the runner:

* AC-1  parse both CSV and ZIP bhavcopy layouts;
* AC-2  normalization to the frozen 12-field layout at daily granularity
        (CE/PE/futures identity, contracts->volume, open_int->oi);
* AC-3  Level-1 validation rejections (schema/required/ts/tz/date/numeric/
        negative/ohlc/identity/expiry/duplicate/order/coverage/no-data/source/
        synthetic labeling);
* AC-4  deterministic SHA-256 fingerprinting that is mutation-sensitive;
* AC-5  manifest/manifest-merge refuses silent overwrites (REFUSED on differing
        bytes, idempotent ALREADY_ACQUIRED on identical bytes);
* AC-6  manifest + human report carry full provenance (source id, URL,
        retrieval timestamp, date range, original filename, size, hash, status);
* AC-7  SYNTHETIC_FIXTURE source can never be presented as market evidence;
* AC-8  the module passes the clock-free / import-safety AST constraints.

Safety: no network, no wall-clock reads, no credentials, no ``execution``/
``upstox``/``credentials`` imports, protected OOS never touched. All market
fixtures are synthetic and explicitly labelled.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import zipfile
from datetime import date as dt_date
from pathlib import Path

import pytest

from fno_ai_paper_trading.options_research import level1_nse as m

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/fno_ai_paper_trading/options_research/level1_nse.py"
RUNNER = ROOT / "scripts/nse_level1_eod_acquire.py"

CLS = "SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"

TWELVE_FIELDS = {
    "ts", "instrument_key", "asset", "expiry", "strike",
    "open", "high", "low", "close", "volume", "oi", "source",
}


def _row(
    *,
    symbol="NIFTY",
    expiry="2022-06-30",
    strike="17000",
    opt="CE",
    open_="5.00",
    high="7.00",
    low="4.50",
    close="6.50",
    contracts="1200",
    oi="48231",
    ts="17-JUN-2022",
):
    return (
        f"{symbol},{expiry},{strike},{opt},{open_},{high},{low},{close},6.50,"
        f"{contracts},8.40,{oi},-2240,{ts}\n"
    )


def _futures_row(*, symbol="NIFTY", expiry="2022-06-30", close="15750.0", contracts="900", oi="100", ts="17-JUN-2022"):
    return f"{symbol},{expiry},,,{close},{close},{close},{close},15750.0,{contracts},11.2,{oi},-10,{ts}\n"


def _bhav_copy_text(*rows: str) -> str:
    return CLS + "".join(rows)


def _write_csv(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "bhav.csv"
    p.write_text(text, encoding="utf-8")
    return p


def _write_zip(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("fo17062022bhav.csv", text)
    return p


def _parse_normalize(tmp_path: Path, text: str, source=None):
    p = _write_csv(tmp_path, text)
    raw, issues = m.parse_bhavcopy(p)
    assert issues == []
    return m.normalize_rows(raw, source=m.SOURCE_ID if source is None else source)


def _base_row(**overrides):
    row = {
        "ts": "2022-06-17T10:00:00Z",
        "instrument_key": "NSE_FO|NIFTY|2022-06-30|17000|CE",
        "asset": "CE",
        "expiry": "2022-06-30",
        "strike": "17000",
        "open": "5.0",
        "high": "7.0",
        "low": "4.5",
        "close": "6.5",
        "volume": "1200",
        "oi": "48231",
        "source": m.SOURCE_ID,
    }
    key = overrides.pop("key", row["instrument_key"])
    row.update(overrides)
    row["instrument_key"] = key
    return row


# --------------------------------------------------------------------------- #
# AC-1  Parse CSV and ZIP
# --------------------------------------------------------------------------- #

def test_parse_csv_ascii(tmp_path):
    p = _write_csv(tmp_path, _bhav_copy_text(_row()))
    rows, issues = m.parse_bhavcopy(p)
    assert issues == []
    assert len(rows) == 1
    assert rows[0]["SYMBOL"] == "NIFTY"
    assert rows[0]["OPEN_INT"] == "48231"
    assert rows[0]["OPTION_TYP"] == "CE"


def test_parse_zip_preserves_row(tmp_path):
    p = _write_zip(tmp_path, "fo17062022bhav.csv.zip", _bhav_copy_text(_row()))
    rows, issues = m.parse_bhavcopy(p)
    assert issues == []
    assert len(rows) == 1
    assert rows[0]["CONTRACTS"] == "1200"


def test_parse_zip_rejects_multiple_csv_members(tmp_path):
    p = tmp_path / "two.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.csv", _bhav_copy_text(_row()))
        zf.writestr("b.csv", _bhav_copy_text(_row()))
    with pytest.raises(ValueError):
        m.parse_bhavcopy(p)


def test_parse_missing_required_column_is_refused(tmp_path):
    p = _write_csv(tmp_path, "SYMBOL,STRIKE_PR\nNIFTY,17000\n")
    rows, issues = m.parse_bhavcopy(p)
    assert rows == []
    assert any("required column missing" in issue for issue in issues)


def test_parse_unknown_extra_columns_ignored(tmp_path):
    text = "EXTRA," + CLS + "x," + _row().rstrip("\n") + "\n"
    p = _write_csv(tmp_path, text)
    rows, issues = m.parse_bhavcopy(p)
    assert issues == []
    assert len(rows) == 1


def test_parse_blank_lines_skipped(tmp_path):
    p = _write_csv(tmp_path, _bhav_copy_text(_row()) + "\n\n\n")
    rows, issues = m.parse_bhavcopy(p)
    assert issues == []
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# AC-2  Normalization to the frozen 12-field layout (daily only)
# --------------------------------------------------------------------------- #

def test_normalize_produces_twelve_contract_fields(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()))
    assert len(rows) == 1
    assert set(rows[0]) == TWELVE_FIELDS


def test_normalize_maps_identity_and_ohlcv(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row(), _futures_row()))
    assert rows[0]["asset"] == "CE"
    assert rows[0]["strike"] == "17000"
    assert rows[0]["volume"] == "1200"
    assert rows[0]["oi"] == "48231"
    assert rows[1]["asset"] == "FUTURES"
    assert rows[1]["volume"] == "900"
    assert rows[1]["strike"] == ""


def test_normalize_derives_stable_instrument_key(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()))
    assert rows[0]["instrument_key"] == "NSE_FO|NIFTY|2022-06-30|17000|CE"
    assert m.build_instrument_key("NIFTY", "2022-06-30", None, "") == "NSE_FO|NIFTY|2022-06-30||"
    assert m.build_instrument_key("NIFTY", "2022-06-30", None, "") == m.build_instrument_key(
        "nifty", "2022-06-30", None, ""
    )


def test_normalize_trade_ts_anchors_to_session_end_utc():
    assert m.normalize_trade_ts(dt_date(2022, 6, 17)) == "2022-06-17T10:00:00Z"


# --------------------------------------------------------------------------- #
# AC-3  Validation rejections
# --------------------------------------------------------------------------- #

def test_valid_rows_pass(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row(), _futures_row()))
    for r in rows:
        r["ts"] = m.normalize_trade_ts(dt_date(2022, 6, 17))
    v = m.validate_level1_rows(rows)
    assert v.ok
    assert v.status() == m.STATUS_VALID
    assert v.code_counts == {}


def test_unknown_schema_field_rejected():
    v = m.validate_level1_rows([dict(_base_row(), secrets="x")])
    assert v.code_counts.get(m.CODE_SCHEMA) == 1


def test_missing_required_field_rejected(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()))
    rows[0] = {k: v for k, v in rows[0].items() if k != "close"}
    v = m.validate_level1_rows(rows)
    assert m.CODE_SCHEMA in v.codes


def test_empty_source_rejected(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()), source="")
    v = m.validate_level1_rows(rows)
    assert m.CODE_SOURCE_MISSING in v.codes


def test_ts_unparseable_and_off_boundary_rejected():
    v = m.validate_level1_rows([dict(_base_row(), ts="not-a-timestamp")])
    assert m.CODE_TS_PARSE in v.codes
    off = dict(_base_row(), ts="2022-06-17T10:03:00Z")
    v = m.validate_level1_rows([off])
    assert m.CODE_TS_PARSE in v.codes


def test_non_utc_offset_rejected():
    tz = dict(_base_row(), ts="2022-06-17T15:30:00+05:30")
    v = m.validate_level1_rows([tz])
    assert m.CODE_TS_TZ in v.codes


def test_numeric_and_negative_rejected():
    v = m.validate_level1_rows([dict(_base_row(), open="abc")])
    assert m.CODE_NUMERIC in v.codes
    v = m.validate_level1_rows([dict(_base_row(), close="-1")])
    assert m.CODE_NEGATIVE in v.codes
    v = m.validate_level1_rows([dict(_base_row(), volume="-5")])
    assert m.CODE_NEGATIVE in v.codes


def test_ohlc_internal_consistency_rejected():
    v = m.validate_level1_rows([dict(_base_row(), high="3.0")])
    assert m.CODE_OHLC in v.codes
    v = m.validate_level1_rows([dict(_base_row(), low="9.0")])
    assert m.CODE_OHLC in v.codes


def test_identity_option_requires_positive_strike_and_expiry():
    v = m.validate_level1_rows([dict(_base_row(), strike="")])
    assert m.CODE_IDENTITY_OPTION in v.codes
    v = m.validate_level1_rows([dict(_base_row(), expiry="")])
    assert m.CODE_IDENTITY_OPTION in v.codes


def test_identity_futures_cannot_carry_strike():
    v = m.validate_level1_rows([dict(_base_row(asset="FUTURES"), strike="150000")])
    assert m.CODE_IDENTITY_FUTURES in v.codes


def test_expiry_before_data_date_rejected():
    v = m.validate_level1_rows([dict(_base_row(), expiry="2022-01-01")])
    assert m.CODE_EXPIRY in v.codes


def test_duplicate_identity_ts_rejected():
    v = m.validate_level1_rows([_base_row(), dict(_base_row(), low="4.0")])
    assert m.CODE_DUPLICATE in v.codes


def test_out_of_order_rejected():
    second = dict(_base_row(), ts="2022-06-16T10:00:00Z")
    v = m.validate_level1_rows([_base_row(), second])
    assert m.CODE_ORDER in v.codes


def test_coverage_gap_beyond_limit_requires_no_data_marker():
    # a single contract whose expiry is still in the future across both dates
    base = _base_row(expiry="2022-09-29")
    later = dict(base, ts="2022-07-20T10:00:00Z")
    v = m.validate_level1_rows([base, later])
    assert m.CODE_COVERAGE_GAP in v.codes
    marker = dict(base, ts="2022-07-20T10:00:00Z")
    marker = {k: v for k, v in marker.items() if k in {"ts", "instrument_key", "asset", "expiry", "strike", "source"}}
    v = m.validate_level1_rows([base, marker])
    assert m.CODE_COVERAGE_GAP not in v.codes
    assert v.ok


def test_unknown_asset_label_rejected():
    v = m.validate_level1_rows([_base_row(asset="BZOOM", key="NSE_FO|NIFTY|2022-06-30|17000|BZOOM")])
    assert m.CODE_IDENTITY_UNDERLYING in v.codes


# --------------------------------------------------------------------------- #
# AC-4  Deterministic, mutation-sensitive fingerprints
# --------------------------------------------------------------------------- #

def test_raw_sha256_deterministic_and_mutation_sensitive(tmp_path):
    p = _write_csv(tmp_path, _bhav_copy_text(_row()))
    h1 = m.raw_byte_sha256(p)
    assert m.raw_byte_sha256(p) == h1
    p.write_text(p.read_text(encoding="utf-8") + "x\n", encoding="utf-8")
    assert m.raw_byte_sha256(p) != h1


def test_normalized_csv_hash_is_lf_stable_and_mutation_sensitive(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()))
    out = tmp_path / "norm.csv"
    h = m.write_normalized_csv(rows, out)
    assert m.write_normalized_csv(rows, out) == h
    out.write_text(out.read_text(encoding="utf-8") + "0\n", encoding="utf-8")
    assert m.file_sha256_lf(out) != h


# --------------------------------------------------------------------------- #
# AC-5  Manifest refuses silent overwrites
# --------------------------------------------------------------------------- #

def test_merge_manifest_identical_is_idempotent():
    entry = {
        "filename": "a.csv",
        "file": {"sha256": "HASH_A", "rows": 1},
        "schema_version": m.SCHEMA_VERSION,
        "level": m.LEVEL_LABEL,
        "synthetic": False,
    }
    m1 = m.merge_manifest(m.build_manifest(), entry)
    m2 = m.merge_manifest(m1, entry)
    assert m2["files"]["a.csv"]["sha256"] == "HASH_A"
    assert list(m2["files"]) == ["a.csv"]


def test_merge_manifest_differing_bytes_refused():
    e1 = {"filename": "a.csv", "file": {"sha256": "HASH_A", "rows": 1}, "synthetic": False}
    e2 = {"filename": "a.csv", "file": {"sha256": "HASH_B", "rows": 2}, "synthetic": False}
    m1 = m.merge_manifest(m.build_manifest(), e1)
    with pytest.raises(ValueError, match="REFUSED"):
        m.merge_manifest(m1, e2)


# --------------------------------------------------------------------------- #
# AC-6  Full provenance in manifest and report
# --------------------------------------------------------------------------- #

def test_manifest_and_report_carry_full_provenance(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()))
    for r in rows:
        r["ts"] = m.normalize_trade_ts(dt_date(2022, 6, 17))
    v = m.validate_level1_rows(rows)
    prov = m.RawProvenance(
        source_id=m.SOURCE_ID,
        dataset_date="2022-06-17",
        original_filename="fo17062022bhav.csv",
        file_size_bytes=123,
        file_sha256="abc123",
        source_url="https://nsearchives.nseindia.com/...",
        retrieval_timestamp="2026-09-27T10:00:00Z",
        acquisition_status="MANUAL_PLACEMENT",
    )
    manifesto = m.build_manifest(
        source=m.SOURCE_ID, coverage={"start": "2022-06-17", "end": "2022-06-17"}, files={}
    )
    report = m.render_report(manifesto=manifesto, validation=v, provenance=prov)
    for token in (
        m.LEVEL_LABEL,
        m.SCHEMA_VERSION,
        m.CONTRACT_SCHEMA_VERSION,
        "NSE_FO_EOD_BHAVCOPY",
        "fo17062022bhav.csv",
        "2026-09-27T10:00:00Z",
        "abc123",
        "MANUAL_PLACEMENT",
    ):
        assert token in report


# --------------------------------------------------------------------------- #
# AC-7  Synthetic fixtures can never be observed market evidence
# --------------------------------------------------------------------------- #

def test_synthetic_source_label_is_always_prefixed(tmp_path):
    rows = _parse_normalize(tmp_path, _bhav_copy_text(_row()), source=m.SYNTHETIC_LABEL)
    rows[0]["ts"] = m.normalize_trade_ts(dt_date(2022, 6, 17))
    v = m.validate_level1_rows(rows)
    assert v.ok
    assert rows[0]["source"] == m.SYNTHETIC_LABEL
    report = m.render_report(
        manifesto=m.build_manifest(synthetic=True, source=m.SYNTHETIC_LABEL),
        validation=v,
        provenance=m.RawProvenance(m.SOURCE_ID, "2022-06-17", "x.csv", 1, "h", "", "t", ""),
    )
    assert "SYNTHETIC FIXTURE" in report


def test_unprefixed_synthetic_word_rejected():
    v = m.validate_level1_rows([dict(_base_row(), source="SYNTHETIC-ish")])
    assert m.CODE_SYNTHETIC_REQUIRED in v.codes


# --------------------------------------------------------------------------- #
# AC-8  Clock-free / no-forbidden-import safety (module-level AST scan)
# --------------------------------------------------------------------------- #

def test_level1_nse_module_no_wall_clock():
    src = MODULE.read_text(encoding="utf-8")
    assert "import time" not in src
    assert "from time import" not in src
    assert "datetime.now" not in src
    assert "datetime.utcnow" not in src
    assert "utcnow" not in src


def test_level1_nse_forbidden_root_imports_banned():
    src = MODULE.read_text(encoding="utf-8")
    for banned in ("import execution", "from execution", "import upstox", "from upstox", "from credentials"):
        assert banned not in src


def test_level1_nse_imports_are_scannable_list():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert "time" not in names
    assert not any("execution" in n or "upstox" in n or "credentials" in n for n in names)


# --------------------------------------------------------------------------- #
# Runner end-to-end smoke (local file, no network)
# --------------------------------------------------------------------------- #

def _load_runner():
    spec = importlib.util.spec_from_file_location("nse_level1_eod_acquire", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_acquires_and_reacquires(tmp_path, capsys):
    runner = _load_runner()
    p = _write_csv(tmp_path, _bhav_copy_text(_row(), _futures_row()))
    out = tmp_path / "out"
    report = tmp_path / "rpt"
    args = [
        "--source-path", str(p),
        "--trade-date", "2022-06-17",
        "--dataset-root", str(out),
        "--report-root", str(report),
        "--retrieval-timestamp", "2026-09-27T10:00:00Z",
    ]
    assert runner.main(args) == 0
    normalized = out / "normalized/nse_fo_eod_2022-06-17_level1_daily.csv"
    assert normalized.exists()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["level"] == m.LEVEL_LABEL
    assert manifest["schema_version"] == m.SCHEMA_VERSION
    assert manifest["files"]["nse_fo_eod_2022-06-17_level1_daily.csv"]["validation_status"] == "VALID"
    assert (out / "raw").exists() and any((out / "raw").iterdir())
    assert list(report.glob("*.md")) and list(report.glob("*.json"))
    assert "ALREADY_ACQUIRED" not in capsys.readouterr().out

    assert runner.main(args) == 0
    assert "ALREADY_ACQUIRED" in capsys.readouterr().out
    manifest2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest2["files"]["nse_fo_eod_2022-06-17_level1_daily.csv"]["sha256"] == manifest[
        "files"
    ]["nse_fo_eod_2022-06-17_level1_daily.csv"]["sha256"]


def test_runner_refuses_differing_raw_bytes(tmp_path):
    runner = _load_runner()
    p = _write_csv(tmp_path, _bhav_copy_text(_row()))
    out = tmp_path / "out"
    report = tmp_path / "rpt"
    args = ["--source-path", str(p), "--trade-date", "2022-06-17",
            "--dataset-root", str(out), "--report-root", str(report)]
    assert runner.main(args) == 0
    p.write_text(_bhav_copy_text(_row(close="99.0")), encoding="utf-8")
    assert runner.main(args) == 3


def test_runner_reports_access_blocked(tmp_path, monkeypatch, capsys):
    runner = _load_runner()

    def fake_download(url, dest):
        return "ACCESS_BLOCKED", 403

    monkeypatch.setattr(runner, "_download", fake_download)
    out = tmp_path / "out"
    report = tmp_path / "rpt"
    code = runner.main(
        ["--fetch", "--trade-date", "2022-06-17",
         "--dataset-root", str(out), "--report-root", str(report),
         "--retrieval-timestamp", "2026-09-27T10:00:00Z"]
    )
    assert code == 3
    assert "ACCESS_BLOCKED" in capsys.readouterr().out
    blocked = list((report / "blocked").glob("*.md"))
    assert len(blocked) == 1 and "ACCESS_BLOCKED" in blocked[0].read_text(encoding="utf-8")


def test_runner_requires_source_or_fetch(tmp_path, capsys):
    runner = _load_runner()
    with pytest.raises(SystemExit):
        runner.main([])