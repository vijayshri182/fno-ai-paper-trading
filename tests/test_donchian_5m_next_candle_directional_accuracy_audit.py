"""Deterministic analytical tests for the recorded 5M Donchian 20/10
NEXT-CANDLE DIRECTIONAL ACCURACY AUDIT
(reports/forensics/donchian_5m_next_candle_directional_accuracy_audit.*).

The audit answers: does a recorded BULLISH/BEARISH signal predict the direction
of the immediately following recorded 5-minute candle?

These tests verify the RECORDED-EVIDENCE contract:

  * the per-signal ledger holds exactly the directional signals (BULLISH 127,
    BEARISH 121, total 248) of the 4,392 recorded decisions (NEUTRAL 4144
    excluded), and one row per directional signal,
  * every row pairs decision timestamp T with reference candle (bar timestamp
    T-5m, the candle completed at T) and next candle (bar timestamp T) from the
    recorded dataset; no manufactured candles,
  * BULLISH => next UP is CORRECT / DOWN is WRONG; BEARISH => next DOWN is
    CORRECT / UP is WRONG; equal closes are FLAT,
  * cross-checks A-E (sums, one-next-candle-per-signal, no look-ahead),
  * the frozen DonchianBreakout(20,10) replication reproduces the recorded
    signal at all 4,392 decision moments (0 mismatches),
  * DCH20/DCH10 in the rows equal the frozen replay's prior/exit channel values
    at the reference bar,
  * overall/bullish/bearish accuracy = Correct / (Correct + Wrong) recomputed
    from the rows independently,
  * the artifact hash is deterministic (recomputed over the payload minus
    generated_at/output_hash) and the CSV mirrors the JSON rows.

NEUTRAL signals are excluded from directional accuracy entirely. Nothing is
simulated or re-optimized; the strategy, parameters and recorded artifacts are
never modified.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fno_ai_paper_trading.research.donchian_5m_forensics import (
    SIGNAL_MAP,
    canonical_hash,
    replay_donchian,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"
AUDIT_JSON = OUT / "donchian_5m_next_candle_directional_accuracy_audit.json"
AUDIT_CSV = OUT / "donchian_5m_next_candle_directional_accuracy_audit.csv"
AUDIT_MD = OUT / "donchian_5m_next_candle_directional_accuracy_audit.md"

CHECKPOINT = Path(
    r"C:\Users\user\AppData\Local\Temp\opencode\fno_5m_validation\out\exp_runA"
    r"\checkpoints\5m_directional_options.2025-08-14.json"
)
MISSING = "NOT AVAILABLE FROM RECORDED ARTIFACT"
ENTRY_CHANNEL = 20
EXIT_CHANNEL = 10
FIVE = timedelta(minutes=5)

EXPECTED_DECISIONS = 4392
EXPECTED_NEUTRAL = 4144
EXPECTED_BULLISH = 127
EXPECTED_BEARISH = 121


@pytest.fixture(scope="module")
def audit():
    if not AUDIT_JSON.exists():
        pytest.skip(f"audit artifact not available: {AUDIT_JSON}")
    return json.loads(AUDIT_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def checkpoint():
    if not CHECKPOINT.exists():
        pytest.skip(f"recorded checkpoint not available: {CHECKPOINT}")
    return json.loads(CHECKPOINT.read_text(encoding="utf-8"))


def test_artifacts_exist():
    for p in (AUDIT_JSON, AUDIT_CSV, AUDIT_MD):
        assert p.exists(), f"missing audit artifact: {p}"


def test_dataset_identity(audit):
    m = audit["meta"]
    assert m["audit"] == "NEXT-CANDLE DIRECTIONAL ACCURACY AUDIT"
    assert m["checkpoint"]["experiment_id"] == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert m["checkpoint"]["version"] == 1
    assert m["dataset"]["timeframe"] == "5m"
    assert m["dataset"]["bars"] == 4575
    assert m["dataset"]["sessions"] == 61
    assert m["dataset"]["date_range"] == [
        "2025-05-22T09:15:00", "2025-08-14T15:25:00"]
    inst = m["dataset"]["instrument"]
    assert inst["symbol"] == "NIFTY 50"
    assert inst["instrument_type"] == "INDEX"
    assert inst["exchange"] == "NSE"


def test_determinism(audit):
    bodies = {
        "meta": {k: v for k, v in audit["meta"].items()
                 if k not in ("generated_at", "output_hash")},
        "summary": audit["summary"],
        "time_of_day": audit["time_of_day"],
        "cross_checks": audit["cross_checks"],
        "rows": audit["rows"],
    }
    assert canonical_hash(bodies) == audit["meta"]["output_hash"]


def test_signal_replication_matches_recorded(checkpoint):
    # a decision at moment M corresponds to the bar completed at M (ts M-5m)
    bars = checkpoint["history"]
    results = replay_donchian(bars, ENTRY_CHANNEL, EXIT_CHANNEL)
    dec_by_moment = {d["moment"]: d for d in checkpoint["decisions"]}
    compared = mism = 0
    for i, b in enumerate(bars):
        moment = (datetime.fromisoformat(b["timestamp"]) + FIVE).isoformat()
        dec = dec_by_moment.get(moment)
        if dec is None:
            continue
        compared += 1
        if SIGNAL_MAP[results[i].signal] != dec["signal"]:
            mism += 1
    assert compared == EXPECTED_DECISIONS
    assert mism == 0


def test_row_count_and_signals(audit):
    rows = audit["rows"]
    assert len(rows) == EXPECTED_BULLISH + EXPECTED_BEARISH == 248
    by_sig = {"BULLISH": 0, "BEARISH": 0}
    for r in rows:
        assert r["signal"] in by_sig
        by_sig[r["signal"]] += 1
    assert by_sig == {"BULLISH": EXPECTED_BULLISH, "BEARISH": EXPECTED_BEARISH}
    assert all(r["signal"] != "NEUTRAL" for r in rows)


def test_summary_totals(audit, checkpoint):
    s = audit["summary"]
    assert s["total_decision_candles"] == EXPECTED_DECISIONS
    assert s["neutral_signals"] == EXPECTED_NEUTRAL
    assert s["directional_signals"] == 248
    assert s["signal_counts"]["NEUTRAL"] == EXPECTED_NEUTRAL
    assert s["signal_counts"]["BULLISH"] == EXPECTED_BULLISH
    assert s["signal_counts"]["BEARISH"] == EXPECTED_BEARISH
    assert len(checkpoint["decisions"]) == EXPECTED_DECISIONS


def test_reference_and_next_semantics(audit, checkpoint):
    """decision at T -> reference candle (bar ts T-5m, closed at T) and next
    candle (bar ts T) from the recorded dataset; nothing manufactured."""
    bars = checkpoint["history"]
    bar_by_ts = {b["timestamp"]: b for b in bars}
    for r in audit["rows"]:
        T = datetime.fromisoformat(r["decision_time"])
        assert r["reference_timestamp"] == (T - FIVE).isoformat()
        assert r["next_timestamp"] == T.isoformat()
        ref = bar_by_ts[r["reference_timestamp"]]
        nxt = bar_by_ts[r["next_timestamp"]]
        assert ref["close"] == r["reference_close"]
        for k in ("open", "high", "low", "close"):
            assert ref[k] == r[f"reference_{k}"]
            assert nxt[k] == r[f"next_{k}"]


def test_dch_values_from_frozen_replay(audit, checkpoint):
    bars = checkpoint["history"]
    results = replay_donchian(bars, ENTRY_CHANNEL, EXIT_CHANNEL)
    idx = {b["timestamp"]: i for i, b in enumerate(bars)}
    for r in audit["rows"]:
        rd = results[idx[r["reference_timestamp"]]]
        assert r["dch20_upper"] == str(rd.prior_hi)
        assert r["dch20_lower"] == str(rd.prior_lo)
        assert r["dch10_upper"] == str(rd.exit_hi)
        assert r["dch10_lower"] == str(rd.exit_lo)


def test_directional_mapping(audit):
    for r in audit["rows"]:
        rc, nc = float(r["reference_close"]), float(r["next_close"])
        expect_dir = "UP" if nc > rc else ("DOWN" if nc < rc else "FLAT")
        assert r["next_direction"] == expect_dir
        if r["signal"] == "BULLISH":
            expect_res = "CORRECT" if nc > rc else ("WRONG" if nc < rc else "FLAT")
        else:
            expect_res = "CORRECT" if nc < rc else ("WRONG" if nc > rc else "FLAT")
        assert r["result"] == expect_res


def test_cross_checks_pass(audit):
    assert audit["cross_checks"] == {
        "A: BULLISH + BEARISH == directional": True,
        "B: BULLISH outcomes == UP+DOWN+FLAT+NO_NEXT_CANDLE": True,
        "C: BEARISH outcomes == DOWN+UP+FLAT+NO_NEXT_CANDLE": True,
        "D: every directional signal has exactly one next candle": True,
        "E: no look-ahead (signal windows exclude the next candle)": True,
    }


def test_independent_accuracy_recompute(audit):
    """Acc = Correct/(Correct+Wrong), recomputed from rows independently."""
    def acc_for(sig: str):
        rows = [r for r in audit["rows"] if r["signal"] == sig]
        correct = sum(1 for r in rows if r["result"] == "CORRECT")
        wrong = sum(1 for r in rows if r["result"] == "WRONG")
        flat = sum(1 for r in rows if r["result"] == "FLAT")
        assert flat == 0
        assert len(rows) == correct + wrong
        acc = correct / (correct + wrong) * 100
        return correct, wrong, acc

    bc, bw, bacc = acc_for("BULLISH")
    ec, ew, eacc = acc_for("BEARISH")
    assert audit["summary"]["bullish"]["correct"] == bc == 57
    assert audit["summary"]["bullish"]["wrong"] == bw == 70
    assert audit["summary"]["bearish"]["correct"] == ec == 62
    assert audit["summary"]["bearish"]["wrong"] == ew == 59
    assert abs(bacc - audit["summary"]["bullish"]["accuracy_pct"]) < 5e-3
    assert abs(eacc - audit["summary"]["bearish"]["accuracy_pct"]) < 5e-3
    overall = audit["summary"]["overall"]
    assert overall["correct"] == bc + ec == 119
    assert overall["wrong"] == bw + ew == 129
    assert abs(100 * (bc + ec) / (bc + ec + bw + ew)
               - overall["accuracy_pct"]) < 5e-3


def test_time_of_day_buckets(audit):
    buckets = audit["time_of_day"]
    assert [b["bucket"] for b in buckets] == [
        "09:15-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
        "13:00-14:00", "14:00-15:15"]
    assert sum(b["signals"] for b in buckets) == 248
    grouped: dict[str, list] = {}
    for r in audit["rows"]:
        T = datetime.fromisoformat(r["decision_time"]).time()
        if datetime(2025, 1, 1, 9, 15) <= datetime(2025, 1, 1, T.hour, T.minute) < datetime(2025, 1, 1, 10, 0):
            label = "09:15-10:00"
        elif datetime(2025, 1, 1, 10, 0) <= datetime.combine(datetime(2025, 1, 1), T) < datetime(2025, 1, 1, 11, 0):
            label = "10:00-11:00"
        elif datetime(2025, 1, 1, 11, 0) <= datetime.combine(datetime(2025, 1, 1), T) < datetime(2025, 1, 1, 12, 0):
            label = "11:00-12:00"
        elif datetime(2025, 1, 1, 12, 0) <= datetime.combine(datetime(2025, 1, 1), T) < datetime(2025, 1, 1, 13, 0):
            label = "12:00-13:00"
        elif datetime(2025, 1, 1, 13, 0) <= datetime.combine(datetime(2025, 1, 1), T) < datetime(2025, 1, 1, 14, 0):
            label = "13:00-14:00"
        else:
            label = "14:00-15:15"
        grouped.setdefault(label, []).append(r)
    for b in buckets:
        rs = grouped[b["bucket"]]
        correct = sum(1 for r in rs if r["result"] == "CORRECT")
        wrong = sum(1 for r in rs if r["result"] == "WRONG")
        flat = sum(1 for r in rs if r["result"] == "FLAT")
        assert b["signals"] == len(rs)
        assert b["correct"] == correct
        assert b["wrong"] == wrong
        assert b["flat"] == flat
        den = correct + wrong
        assert b["accuracy_pct"] == (round(100 * correct / den, 4) if den else None)


def test_csv_matches_json_rows(audit):
    with open(AUDIT_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(audit["rows"])
    for cr, jr in zip(rows, audit["rows"]):
        for k, v in jr.items():
            assert cr[k] == str(v), (k, cr.get(k), v)


def test_markdown_contains_required_sections():
    md = AUDIT_MD.read_text(encoding="utf-8")
    for section in (
        "## A. Dataset identification", "## B. Methodology",
        "## C. Overall summary", "## D. BULLISH analysis",
        "## E. BEARISH analysis", "## F. Time-of-day analysis",
        "## G. Complete signal-level ledger", "## H. Validation / integrity checks",
        "## I. Neutral-signal treatment", "## J. Look-ahead-bias check",
        "NEUTRAL signals were excluded from directional accuracy because",
    ):
        assert section in md, f"missing section in markdown: {section}"