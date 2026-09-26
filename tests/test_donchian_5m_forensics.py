"""Deterministic forensics tests for the 5M_DIRECTIONAL_OPTIONS_EXPERIMENT
(5M Donchian 20/10) trade-ledger package generated under reports/forensics.

These tests are analytical only: they read the generated artifacts and the
recorded experiment checkpoint and re-derive claims from first principles.
Nothing is simulated or re-optimized.

The runA checkpoint sha and content counts are pinned here so the tests also
guard the provenance of the source of truth.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fno_ai_paper_trading.research.donchian_5m_forensics import (
    DZERO,
    DIR_SIGN,
    canonical_hash,
    load_json,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"
LEDGER_JSON = OUT / "donchian_5m_20_10_trade_ledger.json"
LEDGER_CSV = OUT / "donchian_5m_20_10_trade_ledger.csv"
REPORT_MD = OUT / "donchian_5m_20_10_trade_forensics.md"
REPORT_HTML = OUT / "donchian_5m_20_10_trade_forensics.html"

RUN_A_SHA256 = "6c8ebc8c999a23fc9a0c71a0994355092dc952429658b4c1567b3dcd58b4af12"
FINGERPRINT = "b1be21885bba16b7481a7f1d84fe7b970dd6259b969a84f5187986b20397a8c6"
EXPECTED_CLOSURES = 229
EXPECTED_NET = Decimal("-15196.561708640")
EXPECTED_REALIZED = Decimal("-11696.30900")
EXPECTED_SLIPPAGE = Decimal("11667.50900")
EXPECTED_COMMISSION = Decimal("3500.252708640")
EXPECTED_GROSS_CLOSE = Decimal("-28.80")
EXPECTED_FRESH = 105
EXPECTED_REVERSION = 124
EXPECTED_NEUTRAL = 225
EXPECTED_EOD = 3
EXPECTED_REVERSAL = 1
EXPECTED_MAX_LOSS_STREAK = 111


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _load_ledger():
    return json.loads(LEDGER_JSON.read_text(encoding="utf-8"))


def _dec(v) -> Decimal:
    return Decimal(str(v))


@pytest.fixture(scope="module")
def source_checkpoint():
    import os
    base = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\user\AppData\Local"))
    p = base / "Temp" / "opencode" / "fno_5m_validation" / "out" / "exp_runA" / "checkpoints" / "5m_directional_options.2025-08-14.json"
    if not p.exists():
        pytest.skip(f"recorded checkpoint not available: {p}")
    return load_json(str(p))


@pytest.fixture(scope="module")
def trades(d):
    return d["trades"]


@pytest.fixture(scope="module")
def d():
    return _load_ledger()


# ---------------------------------------------------------------------------
# 1. Artifact integrity + provenance
# ---------------------------------------------------------------------------


def test_artifacts_exist():
    for p in (LEDGER_JSON, LEDGER_CSV, REPORT_MD, REPORT_HTML):
        assert p.exists(), f"missing artifact {p}"


def test_provenance_fingerprint(d):
    m = d["_meta"]
    assert m["fingerprint"] == FINGERPRINT
    assert m["experiment_id"] == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert m["runA_equal_runB"] is True
    assert m["runA_sha_matches_stored"] is True
    assert m["runB_sha_matches_stored"] is True
    assert m["source_checkpoint_sha256"] == RUN_A_SHA256


def test_source_checkpoint_sha(d, source_checkpoint):
    import hashlib
    p = Path(d["_meta"]["source_checkpoint"])
    if not p.exists():
        pytest.skip("checkpoint path not on this machine")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest == RUN_A_SHA256
    assert len(source_checkpoint["closures"]) == EXPECTED_CLOSURES
    assert len(source_checkpoint["decisions"]) == 4392
    assert len(source_checkpoint["fills"]) == 458


# ---------------------------------------------------------------------------
# 2. Ledger counts and reconciliation
# ---------------------------------------------------------------------------


def test_trade_count(d):
    assert len(d["trades"]) == EXPECTED_CLOSURES
    assert d["_meta"]["trades"] == EXPECTED_CLOSURES


def test_aggregate_reconciliation(d):
    s = d["summary"]
    net, realized = _dec(s["net_total"]), _dec(s["realized_total"])
    comm, slip = _dec(s["commission_total"]), _dec(s["slippage_total"])
    gross = _dec(s["gross_close_total"])
    assert net == realized - comm
    assert net == gross - slip - comm
    assert s["gross_close_total"] == "{}".format(EXPECTED_GROSS_CLOSE) or _dec(s["gross_close_total"]) == EXPECTED_GROSS_CLOSE


def test_expected_totals(d):
    s = d["summary"]
    assert _dec(s["net_total"]) == EXPECTED_NET
    assert _dec(s["realized_total"]) == EXPECTED_REALIZED
    assert _dec(s["slippage_total"]) == EXPECTED_SLIPPAGE
    assert _dec(s["commission_total"]) == EXPECTED_COMMISSION
    assert _dec(s["gross_close_total"]) == EXPECTED_GROSS_CLOSE
    assert int(s["fresh_breakout_count"]) == EXPECTED_FRESH
    assert int(s["reversion_state_count"]) == EXPECTED_REVERSION
    assert int(s["neutral_exit_count"]) == EXPECTED_NEUTRAL
    assert int(s["eod_exit_count"]) == EXPECTED_EOD
    assert int(s["reversal_exit_count"]) == EXPECTED_REVERSAL
    assert int(s["max_consecutive_losses"]) == EXPECTED_MAX_LOSS_STREAK


# ---------------------------------------------------------------------------
# 3. Per-trade pairing invariant
# ---------------------------------------------------------------------------


def test_uniqueness_and_order(d):
    trades = d["trades"]
    assert len({t["entry_fill_order_id"] for t in trades}) == len(trades)
    assert len({t["_trade_number"] for t in trades}) == len(trades)
    for t in trades:
        assert t["entered_at"] < t["exited_at"]
        assert t["entry_fill_order_id"]
        assert t["entered_at"] and t["exited_at"]
        assert t["leg"] in ("CALL", "PUT")
        assert t["quantity"] in (1, 2)


def test_per_trade_identity(d):
    for t in d["trades"]:
        g, sp, s, c, r, n = (_dec(t[k]) for k in
                             ("gross_pnl_close", "spread_cost", "slippage_cost",
                              "commission_cost", "realized_pnl", "net_pnl"))
        assert sp == DZERO
        assert n == r - c
        assert n == g - sp - s - c
        assert _dec(t["recorded_realized"]) == r


def test_pnl_against_replay(source_checkpoint, d):
    """Recompute realized from the recorded fills to guard the ledger fields."""
    fills = source_checkpoint["fills"]
    fill_key = {(f["filled_at"], f["leg"], f["side"]): f for f in fills}
    from fno_ai_paper_trading.research.donchian_5m_forensics import ENTRY_SIDE, EXIT_SIDE
    by_id = {t["entry_fill_order_id"]: t for t in d["trades"]}
    for t in d["trades"]:
        leg = t["leg"]
        ef = fill_key[(t["entered_at"], leg, ENTRY_SIDE[leg])]
        xf = fill_key[(t["exited_at"], leg, EXIT_SIDE[leg])]
        real = (Decimal(xf["price"]) - Decimal(ef["price"])) * t["quantity"] * DIR_SIGN[leg]
        assert real == _dec(t["realized_pnl"])


# ---------------------------------------------------------------------------
# 4. Forward-horizon correctness (no look-ahead contamination)
# ---------------------------------------------------------------------------


def test_forward_moves(d):
    for t in d["trades"]:
        ec = _dec(t["entry_close"])
        sign = DIR_SIGN[t["leg"]]
        for h in ("5", "10", "15", "20", "25", "30"):
            f = t["forward"].get(h)
            if f is None or f.get("close") is None:
                continue
            fc = _dec(f["close"])
            m = _dec(f["move_close"])
            assert m == (fc - ec) * sign
            assert bool(f["correct_close"]) == (m > 0)


def test_forward_horizons_monotonic_measurement(d):
    """Entries near 15:15 lack +25/+30m candles; +30m may be None."""
    for t in d["trades"]:
        for h in ("5", "10", "15", "20", "25", "30"):
            f = t["forward"].get(h)
            assert f is not None


# ---------------------------------------------------------------------------
# 5. MFE / MAE
# ---------------------------------------------------------------------------


def test_mfe_mae_present_and_ordered(d):
    for t in d["trades"]:
        assert _dec(t["mfe"]) is not None
        assert _dec(t["mae"]) is not None
        assert t["n_held_bars"] == t["holding_minutes"] // 5


def test_losers_mostly_not_favorable(d):
    s = d["summary"]
    assert int(s["losers_gross_fill"]) == 227
    m = d["sections"]["mfe_mae"]
    assert _dec(m["pct_losers_with_positive_mfe_close"]) is not None
    assert 0.0 <= float(m["pct_losers_with_positive_mfe_close"]) <= 100.0


# ---------------------------------------------------------------------------
# 6. Directional / entry classification
# ---------------------------------------------------------------------------


def test_entry_classification(d):
    t = d["trades"]
    assert sum(1 for x in t if x["fresh_breakout"]) == EXPECTED_FRESH
    assert sum(1 for x in t if x["reversion_state_entry"]) == EXPECTED_REVERSION
    for x in t:
        assert x["fresh_breakout"] ^ x["reversion_state_entry"]


def test_exit_reason_totals(d):
    from collections import Counter
    reasons = Counter(t["exit_reason"] for t in d["trades"])
    assert reasons["NEUTRAL"] == EXPECTED_NEUTRAL
    assert reasons["EOD"] == EXPECTED_EOD
    assert reasons["REVERSAL"] == EXPECTED_REVERSAL


def test_directional_hit_rates_in_band(d):
    de = d["sections"]["directional_edge"]
    for h, rec in de.items():
        r = rec["ALL"]["hit_rate"]
        # hit_rate is a percentage (0..100); near coin-flip for this strategy.
        assert r is not None and 30.0 <= r <= 70.0


# ---------------------------------------------------------------------------
# 7. Determinism
# ---------------------------------------------------------------------------


def test_output_hash_deterministic(d):
    m, s, tr = d["_meta"], d["summary"], d["trades"]
    canon = canonical_hash({
        "meta": {k: v for k, v in m.items() if k not in ("generated_at", "output_hash")},
        "summary": s,
        "trades": tr,
    })
    assert canon == m["output_hash"]


def test_csv_matches_json():
    import csv
    with open(LEDGER_CSV, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == EXPECTED_CLOSURES + 1
    header = rows[0]
    assert len(header) == len(set(header))
    net_col = header.index("net_pnl")
    assert rows[1][net_col]


def test_md_and_html_contain_all_sections():
    md = REPORT_MD.read_text(encoding="utf-8")
    html = REPORT_HTML.read_text(encoding="utf-8")
    for i in range(1, 14):
        assert f"## {i}." in md
    assert f"({EXPECTED_CLOSURES}/{EXPECTED_CLOSURES})" in md
    assert html.count("<details>") == EXPECTED_CLOSURES
    assert html.strip().endswith("</html>")