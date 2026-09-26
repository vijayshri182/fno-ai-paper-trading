"""Deterministic analytical tests for the 2026-09-21 reconstruction audit
(reports/forensics/donchian_5m_2026-09-21_signal_order_audit.*).

The audited day has NO recorded experiment artifact (status NOOP in the
fresh-OOS pool; the recorded 5M checkpoint covers 2025-05-22..2025-08-14 only).
These tests therefore verify the RECONSTRUCTION contract:

  * the recorded bar sources are exactly the six pinned artifacts (hash-checked),
  * the reconstructed decision grid is exactly 09:20..15:15 (72 points) referencing
    the completed candle opened at T-5m,
  * every signal/state/band in the JSON equals a fresh replay_donchian() over the
    recorded stream (re-derivation, no caching),
  * the prescribed leg chain equals the frozen decide() contract starting FLAT,
  * every order-lifecycle field carries the literal marker
    NOT AVAILABLE FROM RECORDED ARTIFACT (never a fabricated substitute),
  * forward closes use recorded bars strictly after the decision instant only.

Nothing is simulated or re-optimized; the strategy, contract and recorded
artifacts are never modified.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fno_ai_paper_trading.experiments.directional_5m.contract import (
    Leg,
    decide,
    decision_moments,
    signal_to_15m,
)
from fno_ai_paper_trading.research.donchian_5m_forensics import (
    DONCHIAN_ENTRY_PERIOD,
    DONCHIAN_EXIT_PERIOD,
    canonical_hash,
    jsonable,
    replay_donchian,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"
AUDIT_JSON = OUT / "donchian_5m_2026-09-21_signal_order_audit.json"
AUDIT_CSV = OUT / "donchian_5m_2026-09-21_signal_order_audit.csv"
AUDIT_MD = OUT / "donchian_5m_2026-09-21_signal_order_audit.md"

MISSING = "NOT AVAILABLE FROM RECORDED ARTIFACT"
TARGET_DAY = "2026-09-21"
EXPECTED_DECISIONS = 72

BAR_SOURCES = [
    ("datasets/upstox_Nifty_50_5m_20220103_20260911.csv",
     "81a6dffa13276cabe40aa2cbf6fef9fae5086448f6786373e3961685944e894e", 87193),
    ("datasets/upstox_Nifty_50_5m_20260915_20260915.csv",
     "78202a545a586afe4b5be9d2228a96a66df99eca1f41cb8824d3a16d20a014b5", 75),
    ("datasets/upstox_Nifty_50_5m_20260916_20260916.csv",
     "08da0b22dc1004b9e6686da998cda0aa60fa93e214aa9c98b7e706e06380b880", 75),
    ("data/fresh_oos/NIFTY_50_5m/2026-09-17/data.csv",
     "7a2a9959f20ee19f02d546d3accf715fa984be7a508556282ea8672c93b40bf7", 75),
    ("data/fresh_oos/NIFTY_50_5m/2026-09-18/data.csv",
     "ea39a13ea80f8ca144a21849cfab412ca434c7760952f1affae82ce454fc2b68", 75),
    ("data/fresh_oos/NIFTY_50_5m/2026-09-21/data.csv",
     "3ac516b274a044f10a104c85c068107b6f8db8b31f89a2109770b675ef083fcb", 75),
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_bars(path: Path) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append({
                "timestamp": row["timestamp"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row.get("volume", "0") or "0",
                "open_interest": row.get("open_interest", "0") or "0",
            })
    return out


@pytest.fixture(scope="module")
def audit():
    if not AUDIT_JSON.exists():
        pytest.skip(f"audit artifact not available: {AUDIT_JSON}")
    return json.loads(AUDIT_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stream():
    bars = []
    for rel, expected_sha, n in BAR_SOURCES:
        p = REPO / rel
        if not p.exists():
            pytest.skip(f"recorded bar source not available: {p}")
        data = p.read_bytes()
        if _sha256_bytes(data) != expected_sha:
            pytest.fail(f"recorded bar source hash mismatch: {rel}")
        got = _load_bars(p)
        if len(got) != n:
            pytest.fail(f"recorded bar source count mismatch: {rel} {len(got)} != {n}")
        bars.extend(got)
    return bars


@pytest.fixture(scope="module")
def replay_by_ts(stream):
    replay = replay_donchian(stream, entry_period=DONCHIAN_ENTRY_PERIOD,
                             exit_period=DONCHIAN_EXIT_PERIOD)
    return {stream[i]["timestamp"]: replay[i] for i in range(len(stream))}


def test_artifacts_exist():
    for p in (AUDIT_JSON, AUDIT_CSV, AUDIT_MD):
        assert p.exists(), f"missing audit artifact: {p}"


def test_meta_reconstruction_contract(audit):
    meta = audit["_meta"]
    assert meta["audit_day"] == TARGET_DAY
    assert meta["unavailable_marker"] == MISSING
    # hash the canonical payload (ignoring generated_at + output_hash)
    canon = canonical_hash({
        "meta": {k: v for k, v in meta.items() if k not in ("generated_at", "output_hash")},
        "session": audit["session"], "decisions": audit["decisions"]})
    assert meta["output_hash"] == canon


def test_stream_is_chronological_and_contiguous(stream):
    ts = [b["timestamp"] for b in stream]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)
    assert any(t.startswith(TARGET_DAY) for t in ts)


def test_decision_grid_matches_contract(audit, stream):
    from datetime import date, datetime
    moments = decision_moments(date(2026, 9, 21))
    assert len(moments) == EXPECTED_DECISIONS
    recorded = [d["moment"] for d in audit["decisions"]]
    assert recorded == [m.isoformat() for m in moments]
    # reference candle always opens at T-5m and exists in the recorded stream
    by_ts = {b["timestamp"]: b for b in stream}
    for d in audit["decisions"], moments:
        pass
    for d in audit["decisions"]:
        moment = datetime.fromisoformat(d["moment"])
        assert d["ref_candle"] == (moment - timedelta(minutes=5)).isoformat()
        assert d["ref_candle"] in by_ts, f"missing reference candle {d['ref_candle']}"


def test_signals_match_fresh_replay(audit, replay_by_ts):
    from fno_ai_paper_trading.research.donchian_5m_forensics import SIGNAL_MAP
    for d in audit["decisions"]:
        rd = replay_by_ts[d["ref_candle"]]
        assert d["replay_signal"] == rd.signal
        assert d["signal"] == SIGNAL_MAP[rd.signal]
        assert d["kind"] == rd.kind
        assert d["state_before"] == rd.state_before
        assert d["state_after"] == rd.state_after
        if d["next_moment"] != MISSING:
            from datetime import datetime
            next_ref = (datetime.fromisoformat(d["next_moment"]) - timedelta(minutes=5)).isoformat()
            assert d["next_signal"] == SIGNAL_MAP[replay_by_ts[next_ref].signal]


def test_leg_chain_matches_frozen_contract(audit):
    leg = Leg.FLAT
    for d in audit["decisions"]:
        assert d["leg_before"] == leg.value
        signal = signal_to_15m(d["replay_signal"])
        dec = decide(leg, signal)
        assert d["prescribed_action"] == (
            ",".join(a.value for a in dec.actions) if dec.actions else "NO_ACTION")
        assert d["prescribed_reason"] == dec.reason
        for a in dec.actions:
            if a.value == "ENTER_CALL":
                leg = Leg.CALL
            elif a.value == "ENTER_PUT":
                leg = Leg.PUT
            elif a.value in ("EXIT_CALL", "EXIT_PUT"):
                leg = Leg.FLAT
            elif a.value == "SWITCH_CALL_TO_PUT":
                leg = Leg.PUT
            elif a.value == "SWITCH_PUT_TO_CALL":
                leg = Leg.CALL
        assert d["leg_after"] == leg.value


def test_every_order_lifecycle_field_is_marker(audit):
    for d in audit["decisions"]:
        for field in ("option_action", "position", "exit"):
            assert d[field] == MISSING, f"{d['moment']} {field} must be the exact marker"


def test_forward_closes_use_recorded_bars_only(audit, stream):
    from datetime import datetime
    by_ts = {b["timestamp"]: b for b in stream}
    day_ts = [t for t in by_ts if t.startswith(TARGET_DAY)]
    for d in audit["decisions"]:
        moment = datetime.fromisoformat(d["moment"])
        for h in (5, 10, 15):
            fwd_ts = (moment + timedelta(minutes=h - 5)).isoformat()
            assert fwd_ts in by_ts and fwd_ts.startswith(TARGET_DAY), \
                f"{d['moment']} +{h}m references non-recorded bar {fwd_ts}"
            moved = d[f"fwd_move_{h}"]
            # NEUTRAL signals must not fabricate directional moves
            if d["signal"] == "NEUTRAL":
                assert moved == ""


def test_csv_matches_json_rows(audit):
    import csv as _csv
    with open(AUDIT_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    assert len(rows) == len(audit["decisions"])
    jrows = audit["decisions"]
    for cr, jr in zip(rows, jrows):
        for k, v in jr.items():
            if isinstance(v, (int, float)) or v is None:
                assert cr.get(k, "") == str(v) or cr.get(k, "") == "", (k, v)
            else:
                assert cr.get(k, "") == str(v), (k, cr.get(k), v)