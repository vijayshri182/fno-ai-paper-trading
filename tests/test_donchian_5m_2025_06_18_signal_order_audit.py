"""Deterministic analytical tests for the recorded 2025-06-18 signal & order
audit (reports/forensics/donchian_5m_2025_06_18_signal_order_audit.*).

The audited day is a REAL recorded trading day of the 5M directional options
experiment. These tests verify the RECORDED-EVIDENCE contract:

  * the audit JSON is a faithful, hash-verified view of the recorded
    checkpoint (decisions + fills + closures + entry approvals) and ledger,
  * the 8 audited trades are exactly ledger trades #68..#75,
  * every field labeled [C] carries the literal marker
    NOT AVAILABLE FROM RECORDED ARTIFACT (never a fabricated substitute),
  * recorded fill order-sides are consistent with the frozen engine mapping
    (CALL entry=BUY/CALL exit=SELL; PUT entry=SELL/PUT exit=BUY),
  * per-trade P&L identities hold on the recorded numbers
    (net == gross_close - slip - spread(0) - commission; realized == fill
    price delta for the recorded leg),
  * direction-transition and reversal counts in the JSON match recomputing
    them from the recorded ledger rows,
  * the artifact hash is deterministic (recomputed over the payload minus
    generated_at/output_hash).

Nothing is simulated or re-optimized; the strategy, contract and recorded
artifacts are never modified.
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fno_ai_paper_trading.research.donchian_5m_forensics import canonical_hash

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"
AUDIT_JSON = OUT / "donchian_5m_2025_06_18_signal_order_audit.json"
AUDIT_CSV = OUT / "donchian_5m_2025_06_18_signal_order_audit.csv"
AUDIT_MD = OUT / "donchian_5m_2025_06_18_signal_order_audit.md"

CHECKPOINT = Path(
    r"C:\Users\user\AppData\Local\Temp\opencode\fno_5m_validation\out\exp_runA"
    r"\checkpoints\5m_directional_options.2025-08-14.json"
)

MISSING = "NOT AVAILABLE FROM RECORDED ARTIFACT"
TARGET_DAY = "2025-06-18"
EXPECTED_TRADES = list(range(68, 76))
EXPECTED_FILLS = 16
EXPECTED_DECISIONS = 72
EXPECTED_CLOSURES = 8
EXPECTED_APPROVALS = 8
EXPECTED_DIRECTION_FLIPS = 5
EXPECTED_REVERSAL_EXITS = 1
EXPECTED_GROSS_WINNERS = 2


def _dq(value) -> Decimal:
    return Decimal(str(value))


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


def test_meta_invariants(audit):
    meta = audit["meta"]
    assert meta["audit_day"] == TARGET_DAY
    assert meta["unavailable_marker"] == MISSING
    assert meta["checkpoint"]["experiment_id"] == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert meta["checkpoint"]["version"] == 1
    canon = canonical_hash({
        "meta": {k: v for k, v in meta.items()
                 if k not in ("generated_at", "output_hash")},
        "day": audit["day"], "trades": audit["trades"],
        "decisions": audit["decisions"]})
    assert meta["output_hash"] == canon


def test_day_counts(audit):
    day = audit["day"]
    assert day["trades"] == 8
    assert day["fills"] == EXPECTED_FILLS
    assert day["closures"] == EXPECTED_CLOSURES
    assert day["entry_approvals"] == EXPECTED_APPROVALS
    assert day["decision_points"] == EXPECTED_DECISIONS
    assert day["n_direction_flips"] == EXPECTED_DIRECTION_FLIPS
    assert day["reversal_exits"] == EXPECTED_REVERSAL_EXITS
    assert day["winners_count"] == EXPECTED_GROSS_WINNERS
    assert day["losers_count"] == 8 - EXPECTED_GROSS_WINNERS


def test_trades_are_the_recorded_ledger_sequence(audit):
    assert [int(t["trade_number"]) for t in audit["trades"]] == EXPECTED_TRADES
    seq = audit["day"]["direction_transitions"]
    assert [int(t["trade_number"]) for t in seq] == EXPECTED_TRADES


def test_fills_match_checkpoint_records(audit, checkpoint):
    """Every audited fill order id must exist in the checkpoint fills with the
    same side / leg / price / quantity / commission."""
    ck_fills = {f["order_id"]: f for f in checkpoint["fills"]}
    used_ids = {
        t["entry_fill_order_id"]: t for t in audit["trades"]
    }
    used_ids.update({t["exit_fill_order_id"]: t for t in audit["trades"]})
    assert len(used_ids) == EXPECTED_FILLS
    for order_id, t in used_ids.items():
        f = ck_fills.get(order_id)
        assert f is not None, f"order {order_id} missing from checkpoint fills"
        assert f["instrument"]["symbol"] == "NIFTY 50"
        assert f["instrument"]["option_type"] is None
        assert f["instrument"]["strike"] is None
        assert f["instrument"]["expiry"] is None
        # the audit copies side/leg/price/quantity/commission verbatim
        role = "entry" if order_id == t["entry_fill_order_id"] else "exit"
        assert t[f"{role}_fill_side"] == f["side"]
        assert f["leg"] == t["direction"]
        assert t[f"{role}_fill_price"] == f["price"]
        assert t[f"{role}_fill_commission"] == f["commission"]
        assert t["quantity"] == str(f["quantity"])


def test_decision_rows_match_checkpoint_trace(audit, checkpoint):
    day = [d for d in checkpoint["decisions"] if d["moment"][:10] == TARGET_DAY]
    day.sort(key=lambda d: d["moment"])
    assert len(day) == EXPECTED_DECISIONS
    for aud_d, ck_d in zip(audit["decisions"], day):
        for fld in ("moment", "signal", "state_before", "state_after",
                    "reason", "fills"):
            assert aud_d[fld] == ck_d[fld]
        assert aud_d["actions"] == ck_d["actions"]


def test_closures_match_checkpoint_records(audit, checkpoint):
    day = [c for c in checkpoint["closures"] if c["entered_at"][:10] == TARGET_DAY]
    day.sort(key=lambda c: c["entered_at"])
    assert len(day) == EXPECTED_CLOSURES
    for t, c in zip(audit["trades"], day):
        assert t["entered_at"] == c["entered_at"]
        assert t["exited_at"] == c["exited_at"]
        assert t["closure_kind"] == c["kind"]
        assert t["closure_realized"] == c["realized"]
        assert t["direction"] == c["leg"]


def test_contract_fields_are_exact_marker(audit):
    """[C] fields must be the literal marker; never a fabricated substitute."""
    for t in audit["trades"]:
        for field in ("option_contract_symbol", "option_strike",
                      "option_expiry", "option_type"):
            assert t[field] == MISSING, f"#{t['trade_number']} {field}: {t[field]!r}"


def test_recorded_fill_side_mapping(audit):
    """Recorded order sides must match the frozen engine mapping for CALL/PUT."""
    for t in audit["trades"]:
        if t["direction"] == "CALL":
            assert t["entry_fill_side"] == "BUY"
            assert t["exit_fill_side"] == "SELL"
        else:
            assert t["entry_fill_side"] == "SELL"
            assert t["exit_fill_side"] == "BUY"


def test_reversal_disposition(audit):
    revs = [t for t in audit["trades"] if t["exit_reason"] == "REVERSAL"]
    assert len(revs) == EXPECTED_REVERSAL_EXITS
    assert {int(t["trade_number"]) for t in revs} == {68}
    rev = revs[0]
    assert rev["exit_action_recorded"] == "SWITCH_PUT_TO_CALL"
    assert rev["exit_signal_recorded"] == "BULLISH"
    assert rev["entered_at"] == "2025-06-18T09:20:00"
    assert rev["exited_at"] == "2025-06-18T09:25:00"


def test_direction_transitions_and_flips(audit):
    seq = [t["direction"] for t in audit["day"]["direction_transitions"]]
    flips = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
    assert flips == EXPECTED_DIRECTION_FLIPS
    direq = seq
    assert direq[0] == "PUT"
    assert direq[-1] == "CALL"


def test_pnl_identity_net_equals_gross_minus_costs(audit):
    for t in audit["trades"]:
        assert _dq(t["net_pnl"]) == (
            _dq(t["gross_pnl_close"]) - _dq(t["spread_cost"])
            - _dq(t["slippage_cost"]) - _dq(t["commission_cost"])
        ), f"#{t['trade_number']} net identity failed"


def test_pnl_realized_matches_fill_delta(audit):
    for t in audit["trades"]:
        ent = _dq(t["entry_fill_price"])
        ext = _dq(t["exit_fill_price"])
        if t["direction"] == "PUT":
            expected = ent - ext
        else:
            expected = ext - ent
        assert _dq(t["realized_pnl"]) == expected, \
            f"#{t['trade_number']} realized fill-delta failed"


def test_gross_winner_classification(audit):
    winners = [t for t in audit["trades"] if t["B_gross_winner"]]
    assert len(winners) == EXPECTED_GROSS_WINNERS
    for t in winners:
        assert _dq(t["gross_pnl_close"]) > 0
    for t in audit["trades"]:
        if not t["B_gross_winner"]:
            assert _dq(t["gross_pnl_close"]) <= 0


def test_winners_exist_in_json_summary(audit):
    assert audit["day"]["winners_count"] == EXPECTED_GROSS_WINNERS
    assert audit["day"]["losers_count"] == 6


def test_csv_matches_json_trades(audit):
    with open(AUDIT_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(audit["trades"])
    for cr, jr in zip(rows, audit["trades"]):
        for k, v in jr.items():
            if k not in cr:
                continue
            if isinstance(v, bool):
                assert cr.get(k, "") == str(v), (k, v, cr.get(k))
            elif isinstance(v, (int, float)) or v is None:
                assert cr.get(k, "") == str(v) or cr.get(k, "") == "", \
                    (k, v, cr.get(k))
            else:
                assert cr.get(k, "") == str(v), (k, cr.get(k), v)


def test_markdown_contains_required_sections():
    md = AUDIT_MD.read_text(encoding="utf-8")
    for section in (
        "## §1", "## §2", "## §3", "## §4", "## §5", "## §6", "## §7",
        "## §8", "## §9", "## §10", "## §11", "## §12", "## §13",
        "## KEY FINDINGS",
        "NOT AVAILABLE FROM RECORDED ARTIFACT",
    ):
        assert section in md, f"missing section/text in markdown: {section}"