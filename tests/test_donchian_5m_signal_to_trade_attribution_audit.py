"""Deterministic analytical tests for the recorded 5M Donchian 20/10
SIGNAL->TO->TRADE ATTRIBUTION AUDIT
(reports/forensics/donchian_5m_signal_to_trade_attribution_audit.* and
reports/forensics/donchian_5m_position_episode_attribution.csv).

The audit answers: for each of the 248 recorded directional Donchian 20/10
signals, what position action followed and which recorded position episode
(trade) did the signal open or share, and what where the economics?

These tests verify the RECORDED-EVIDENCE contract:

  * the population is exactly the 248 directional signals (BULLISH 127 /
    BEARISH 121) of the 4,392 recorded decisions, taken one-for-one from the
    next-candle directional accuracy audit ledger,
  * every directional signal has exactly one attribution status from
    {NEW_TRADE, HELD_EXISTING_TRADE, SWITCHED_TRADE, EXITED_EXISTING_TRADE,
    NO_NEW_TRADE} and exactly one lifecycle class,
  * status counts: NEW_TRADE 228, HELD_EXISTING_TRADE 19, SWITCHED_TRADE 1,
    EXITED_EXISTING_TRADE 0, NO_NEW_TRADE 0,
  * the 229 position episodes (recorded round trips) are each opened by
    exactly one directional signal (228 NEW_TRADE + 1 switch-opened); held
    signals share 19 already-attributed episodes; no episode lacks an opener,
  * the single SWITCHED_TRADE signal (2025-06-18T09:25:00) closes one episode
    (REVERSAL) and opens another,
  * P&L attribution convention: episode economics are counted exactly once at
    the opening signal (p_and_l_owner=true); held signals never re-sum,
  * aggregate economics equal the recorded ledger / existing forensics totals
    (gross -28.80, slippage 11667.509, commission 3500.252708640, net
    -15196.561708640) and are invariant whether summed over episodes or over
    the by-status/by-next-candle-result groups,
  * exit reasons = NEUTRAL 225 / EOD 3 / REVERSAL 1,
  * no look-ahead: every signal's reference candle (bar at T-5m) exists and
    the mapped episode never starts after the signal,
  * the artifact hash is deterministic (recomputed over the payload minus
    generated_at/output_hash) and the CSVs mirror the JSON rows.

Nothing is simulated or re-optimized; the strategy, parameters and recorded
artifacts are never modified.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fno_ai_paper_trading.research.donchian_5m_forensics import canonical_hash

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"
AUDIT_JSON = OUT / "donchian_5m_signal_to_trade_attribution_audit.json"
AUDIT_CSV = OUT / "donchian_5m_signal_to_trade_attribution_audit.csv"
AUDIT_MD = OUT / "donchian_5m_signal_to_trade_attribution_audit.md"
EPISODE_CSV = OUT / "donchian_5m_position_episode_attribution.csv"

CHECKPOINT = Path(
    r"C:\Users\user\AppData\Local\Temp\opencode\fno_5m_validation\out\exp_runA"
    r"\checkpoints\5m_directional_options.2025-08-14.json"
)
FIVE = timedelta(minutes=5)

EXPECTED_DECISIONS = 4392
EXPECTED_BULLISH = 127
EXPECTED_BEARISH = 121
EXPECTED_TOTAL = 248
EXPECTED_EPISODES = 229

STATUSES = {
    "NEW_TRADE": 228,
    "HELD_EXISTING_TRADE": 19,
    "SWITCHED_TRADE": 1,
    "EXITED_EXISTING_TRADE": 0,
    "NO_NEW_TRADE": 0,
}

EXPECTED_SUMS = {
    "gross_pnl_close": Decimal("-28.80"),
    "spread_cost": Decimal("0"),
    "slippage_cost": Decimal("11667.509"),
    "commission_cost": Decimal("3500.252708640"),
    "total_cost": Decimal("15167.761708640"),
    "realized_pnl": Decimal("-11696.309"),
    "net_pnl": Decimal("-15196.561708640"),
}


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


def _dec(v) -> Decimal:
    return Decimal(str(v))


def test_artifacts_exist():
    for p in (AUDIT_JSON, AUDIT_CSV, AUDIT_MD, EPISODE_CSV):
        assert p.exists(), f"missing audit artifact: {p}"


def test_dataset_identity(audit):
    m = audit["meta"]
    assert m["audit"] == "SIGNAL-TO-TRADE ATTRIBUTION AUDIT"
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
    assert "does not modify, optimize, rank, approve, reject, or promote" in \
        m["statement"]


def test_determinism(audit):
    bodies = {
        "meta": {k: v for k, v in audit["meta"].items()
                 if k not in ("generated_at", "output_hash")},
        "summary": audit["summary"],
        "attribution": audit["attribution"],
        "churn": audit["churn"],
        "cross_checks": audit["cross_checks"],
        "rows": audit["rows"],
        "episodes": audit["episodes"],
    }
    assert canonical_hash(bodies) == audit["meta"]["output_hash"]


def test_population_matches_recorded(checkpoint):
    decisions = checkpoint["decisions"]
    assert len(decisions) == EXPECTED_DECISIONS
    count = {"NEUTRAL": 0, "BULLISH": 0, "BEARISH": 0}
    for d in decisions:
        count[d["signal"]] += 1
    assert count == {"NEUTRAL": 4392 - EXPECTED_TOTAL,
                     "BULLISH": EXPECTED_BULLISH,
                     "BEARISH": EXPECTED_BEARISH}


def test_row_count_and_signals(audit):
    rows = audit["rows"]
    assert len(rows) == EXPECTED_TOTAL
    by_sig = {"BULLISH": 0, "BEARISH": 0}
    for r in rows:
        assert r["signal"] in by_sig
        by_sig[r["signal"]] += 1
    assert by_sig == {"BULLISH": EXPECTED_BULLISH, "BEARISH": EXPECTED_BEARISH}
    assert all(r["attribution_status"] != "NEUTRAL" for r in rows)
    assert len({r["signal_time"] for r in rows}) == EXPECTED_TOTAL


def test_attribution_counts(audit):
    rows = audit["rows"]
    status = {k: 0 for k in STATUSES}
    classes = set()
    for r in rows:
        status[r["attribution_status"]] = status.get(
            r["attribution_status"], 0) + 1
        classes.add(r["attribution_class"])
    assert status == STATUSES
    assert classes == {"NEW_ENTRY", "HOLD_EXISTING_POSITION", "SWITCH"}
    assert all(r["attribution_class"] in {
        "NEW_ENTRY", "HOLD_EXISTING_POSITION", "SWITCH", "EXIT",
        "DIRECTIONAL_SIGNAL_WITH_NO_NEW_POSITION"} for r in rows)


def test_attribution_logic_consistent(audit, checkpoint):
    """Every row's recorded decision matches its fills/actions/classification."""
    dec_by = {d["moment"]: d for d in checkpoint["decisions"]}
    for r in audit["rows"]:
        d = dec_by[r["signal_time"]]
        assert d["signal"] == r["signal"]
        nf = len(d["fills"])
        if r["attribution_status"] == "NEW_TRADE":
            assert nf == 1 and "ENTER" in ",".join(d["actions"])
        elif r["attribution_status"] == "HELD_EXISTING_TRADE":
            assert nf == 0 and d["state_before"] == d["state_after"]
        elif r["attribution_status"] == "SWITCHED_TRADE":
            assert nf == 2 and d["actions"][0] == "SWITCH_PUT_TO_CALL"
        assert r["fills_count"] == nf


def test_episode_count_and_openers(audit):
    rows = audit["rows"]
    eps = audit["episodes"]
    assert len(eps) == EXPECTED_EPISODES
    owners = [r for r in rows if r["p_and_l_owner"]]
    owner_eps = {r["position_episode_id"] for r in owners}
    all_eps = {e["position_episode_id"] for e in eps}
    # every episode opened by exactly one directional signal; no orphan episodes
    assert len(owners) == len(owner_eps) == len(eps)
    assert owner_eps == all_eps
    assert audit["summary"]["episodes_opened_by_directional_signals"] == \
        EXPECTED_EPISODES
    assert audit["summary"]["episodes_without_directional_opener"] == 0
    # held signals share 19 distinct already-attributed episodes
    held_eps = {r["position_episode_id"] for r in rows
                if r["attribution_status"] == "HELD_EXISTING_TRADE"}
    assert len(held_eps) == 19
    assert held_eps <= owner_eps


def test_switch_semantics(audit):
    switches = [r for r in audit["rows"]
                if r["attribution_status"] == "SWITCHED_TRADE"]
    assert len(switches) == 1
    sw = switches[0]
    assert sw["signal_time"] == "2025-06-18T09:25:00"
    assert sw["signal"] == "BULLISH"
    assert sw["action_recorded"] == "SWITCH_PUT_TO_CALL"
    assert sw["state_before"] == "PUT" and sw["state_after"] == "CALL"
    # closes episode 68 (REVERSAL) and opens episode 69
    assert sw["closed_episode_id"] == "68"
    assert sw["position_episode_id"] == "69"
    assert sw["episode_entry_time"] == "2025-06-18T09:25:00"
    assert sw["episode_direction"] == "CALL"
    # the switch is the owner of the opened episode (economics counted there)
    assert sw["p_and_l_owner"] is True
    by_reason = audit["attribution"]["by_exit_reason"]
    assert by_reason["REVERSAL"]["episodes"] == 1


def test_held_rows_never_re_sum(audit):
    """Held rows carry episode economics but p_and_l_owner=false, so their
    episode P&L is counted once at the owner and never re-summed."""
    held = [r for r in audit["rows"]
            if r["attribution_status"] == "HELD_EXISTING_TRADE"]
    assert len(held) == 19
    assert all(r["p_and_l_owner"] is False for r in held)
    # status-group economics must sum only owner rows; HELD group is zero
    for status in ("HELD_EXISTING_TRADE", "EXITED_EXISTING_TRADE",
                   "NO_NEW_TRADE"):
        econ = audit["attribution"]["by_status"][status]["economics"]
        assert all(_dec(econ[k]) == 0 for k in econ)


def test_economics_reconcile(audit):
    """Summing every episode once equals the recorded ledger / forensics."""
    totals = {k: Decimal("0") for k in EXPECTED_SUMS}
    for e in audit["episodes"]:
        for k in totals:
            totals[k] += _dec(e[k])
    assert totals == EXPECTED_SUMS
    # cross-check against the by-next-candle-result groups and guide sums
    group_sums = {k: Decimal("0") for k in EXPECTED_SUMS}
    for label, v in audit["attribution"]["by_next_candle_result"].items():
        e = v["economics"]
        for k in group_sums:
            group_sums[k] += _dec(e[k])
    assert group_sums == EXPECTED_SUMS
    status_sums = {k: Decimal("0") for k in EXPECTED_SUMS}
    for status in ("NEW_TRADE", "HELD_EXISTING_TRADE", "SWITCHED_TRADE",
                   "EXITED_EXISTING_TRADE", "NO_NEW_TRADE"):
        e = audit["attribution"]["by_status"][status]["economics"]
        for k in status_sums:
            status_sums[k] += _dec(e[k])
    assert status_sums == EXPECTED_SUMS


def test_by_next_candle_result_counts(audit):
    g = audit["attribution"]["by_next_candle_result"]
    assert [k for k in g] == [
        "BULLISH CORRECT", "BULLISH WRONG",
        "BEARISH CORRECT", "BEARISH WRONG"]
    # signal counts recomputable from the rows
    rows = audit["rows"]
    count = {}
    for r in rows:
        key = f"{r['signal']} {r['next_candle_result']}"
        count[key] = count.get(key, 0) + 1
    for k, v in g.items():
        assert v["signals"] == count[k]
    assert sum(v["signals"] for v in g.values()) == EXPECTED_TOTAL
    # no FLAT / NO_NEXT_CANDLE in the directional population
    assert all(r["next_candle_result"] in ("CORRECT", "WRONG")
               for r in rows)


def test_exit_reasons(audit):
    by_reason = audit["attribution"]["by_exit_reason"]
    assert {k: v["episodes"] for k, v in by_reason.items()} == {
        "NEUTRAL": 225, "EOD": 3, "REVERSAL": 1}
    assert sum(v["episodes"] for v in by_reason.values()) == EXPECTED_EPISODES


def test_episode_holding_summary(audit):
    s = audit["summary"]["holding_minutes"]
    rows = audit["episodes"]
    minutes = [int(e["holding_minutes"]) for e in rows]
    assert s["min"] == 5 == min(minutes)
    assert s["max"] == 10 == max(minutes)
    assert int(s["median"]) == sorted(minutes)[len(minutes) // 2]
    assert len(minutes) == EXPECTED_EPISODES and all(
        m in (5, 10) for m in minutes)


def test_no_lookahead(audit, checkpoint):
    bars = checkpoint["history"]
    bar_by_ts = {b["timestamp"]: b for b in bars}
    for r in audit["rows"]:
        T = datetime.fromisoformat(r["signal_time"])
        ref_ts = (T - FIVE).isoformat()
        # reference candle (completed at T) must exist in the recorded history
        assert ref_ts in bar_by_ts, r["signal_time"]
        entry = datetime.fromisoformat(r["episode_entry_time"])
        exit_ = datetime.fromisoformat(r["episode_exit_time"])
        assert entry <= T, r["signal_time"]
        assert exit_ >= entry


def test_cross_checks_pass(audit):
    assert all(v is True for v in audit["cross_checks"].values())
    assert audit["cross_checks"][
        "H: economics sum to the recorded ledger totals"] is True
    assert audit["cross_checks"][
        "J: no look-ahead (signal at T uses candle closed at T; episode "
        "entry is never after its opening signal)"] is True


def test_csv_matches_json_rows(audit):
    with open(AUDIT_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(audit["rows"])
    for cr, jr in zip(rows, audit["rows"]):
        for k, v in jr.items():
            assert cr[k] == str(v), (k, cr.get(k), v)


def test_episode_csv_matches_json(audit):
    with open(EPISODE_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(audit["episodes"])
    for cr, jr in zip(rows, audit["episodes"]):
        for k, v in jr.items():
            assert cr[k] == str(v), (k, cr.get(k), v)


def test_markdown_contains_required_sections():
    md = AUDIT_MD.read_text(encoding="utf-8")
    for section in (
        "## A. Dataset identification", "## B. Population and mapping inputs",
        "## C. Methodology / attribution model", "## D. Directional-signal attribution summary",
        "## E. Position episodes", "## F. Episode exit reasons",
        "## G. Signal attribution by next-candle result",
        "## H. Next-candle-result vs episode net P&L", "## I. Churn / signal-to-exit statistics",
        "## J. Economics reconciliation", "## K. Integrity checks",
        "## L. Look-ahead-bias check", "## M. Complete signal-level attribution ledger",
        "## N. Position-episode attribution ledger", "## O. Reproduction",
        "does not modify, optimize, rank, approve, reject, or promote",
    ):
        assert section in md, f"missing section in markdown: {section}"