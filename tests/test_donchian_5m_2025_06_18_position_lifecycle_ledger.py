"""Deterministic analytical tests for the recorded 2025-06-18 position lifecycle
ledger (reports/forensics/donchian_5m_2025_06_18_position_lifecycle_ledger.*).

The ledger is a per-5-minute-decision lifecycle: one explicit row per recorded
decision instant (09:20..15:15, 72 rows), HOLD rows explicit, switches expanded
into two separate order events with the frozen contract order semantics:

    OPEN PUT  = SELL PUT      CLOSE PUT  = BUY PUT
    OPEN CALL = BUY CALL      CLOSE CALL = SELL CALL

These tests verify the RECORDED-EVIDENCE contract:

  * 72 rows, exactly the recorded decision moments of the day,
  * every HOLD row is explicit and has no order event,
  * every recorded day fill appears exactly once across order1/order2, and the
    semantic (OPEN/CLOSE x PUT/CALL) matches the recorded side+leg verbatim,
  * the only switch (09:25 SWITCH_PUT_TO_CALL) expands to two order events
    (BUY PUT close + BUY CALL open),
  * the frozed DonchianBreakout(20,10) recomputation on recorded-history
    prefixes reproduces the recorded signal at all 72 decision instants
    (0 mismatches),
  * the DCH20/DCH10 values reconcile with the recorded ledger (donchian_upper/
    lower == DCH20 at entry; exit_channel_upper/lower == DCH10 at entry),
  * the day-boundary facts hold (warmup=21, first-decision prefix=1426, prior
    session candles are carried),
  * the artifact hash is deterministic (recomputed over the payload minus
    generated_at/output_hash) and the CSV mirrors the JSON rows.

Nothing is simulated or re-optimized; the strategy, contract and recorded
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
LEDGER_JSON = OUT / "donchian_5m_2025_06_18_position_lifecycle_ledger.json"
LEDGER_CSV = OUT / "donchian_5m_2025_06_18_position_lifecycle_ledger.csv"
LEDGER_MD = OUT / "donchian_5m_2025_06_18_position_lifecycle_ledger.md"

CHECKPOINT = Path(
    r"C:\Users\user\AppData\Local\Temp\opencode\fno_5m_validation\out\exp_runA"
    r"\checkpoints\5m_directional_options.2025-08-14.json"
)
TRADE_LEDGER_CSV = OUT / "donchian_5m_20_10_trade_ledger.csv"

MISSING = "NOT AVAILABLE FROM RECORDED ARTIFACT"
TARGET_DAY = "2025-06-18"
EXPECTED_ROWS = 72
EXPECTED_FILLS = 16
EXPECTED_CLOSURES = 8
EXPECTED_APPROVALS = 8
EXPECTED_TRADES = 8
EXPECTED_SWITCHES = 1
EXPECTED_HOLDS = 57
ENTRY_CHANNEL = 20
EXIT_CHANNEL = 10
WARMUP_BARS = max(ENTRY_CHANNEL, EXIT_CHANNEL) + 1  # 21
FIRST_PREFIX_LEN = 1426  # recorded: 1425 candles precede the day + 09:15 ref

SEMANTIC_OK = {
    ("PUT", "SELL"): "OPEN PUT",
    ("PUT", "BUY"): "CLOSE PUT",
    ("CALL", "BUY"): "OPEN CALL",
    ("CALL", "SELL"): "CLOSE CALL",
}

MOMENTS_72 = [
    (datetime(2025, 6, 18, 9, 20) + timedelta(minutes=5 * i)).isoformat()
    for i in range(72)
]


def _dq(value) -> Decimal:
    return Decimal(str(value))


@pytest.fixture(scope="module")
def ledger():
    if not LEDGER_JSON.exists():
        pytest.skip(f"ledger artifact not available: {LEDGER_JSON}")
    return json.loads(LEDGER_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def checkpoint():
    if not CHECKPOINT.exists():
        pytest.skip(f"recorded checkpoint not available: {CHECKPOINT}")
    return json.loads(CHECKPOINT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def trade_ledger():
    if not TRADE_LEDGER_CSV.exists():
        pytest.skip(f"trade ledger not available: {TRADE_LEDGER_CSV}")
    with open(TRADE_LEDGER_CSV, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_artifacts_exist():
    for p in (LEDGER_JSON, LEDGER_CSV, LEDGER_MD):
        assert p.exists(), f"missing ledger artifact: {p}"


def test_meta_invariants(ledger):
    meta = ledger["meta"]
    assert meta["audit_day"] == TARGET_DAY
    assert meta["unavailable_marker"] == MISSING
    assert meta["checkpoint"]["experiment_id"] == "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"
    assert meta["checkpoint"]["version"] == 1
    canon = canonical_hash({
        "meta": {k: v for k, v in meta.items()
                 if k not in ("generated_at", "output_hash")},
        "day_boundary": ledger["day_boundary"],
        "day": ledger["day"],
        "rows": ledger["rows"]})
    assert meta["output_hash"] == canon


def test_day_counts(ledger):
    day = ledger["day"]
    assert day["decision_points"] == EXPECTED_ROWS
    assert day["fills"] == EXPECTED_FILLS
    assert day["closures"] == EXPECTED_CLOSURES
    assert day["entry_approvals"] == EXPECTED_APPROVALS
    assert day["trades"] == EXPECTED_TRADES
    assert day["order_events_total"] == EXPECTED_FILLS
    assert day["switch_decisions"] == EXPECTED_SWITCHES
    assert day["hold_decisions"] == EXPECTED_HOLDS
    assert day["order_event_decisions"] == EXPECTED_ROWS - EXPECTED_HOLDS


def test_one_row_per_recorded_moment(ledger, checkpoint):
    day = [d for d in checkpoint["decisions"] if d["moment"][:10] == TARGET_DAY]
    day.sort(key=lambda d: d["moment"])
    assert len(day) == EXPECTED_ROWS
    ts = [r["timestamp"] for r in ledger["rows"]]
    assert ts == MOMENTS_72
    assert ts == [d["moment"] for d in day]


def test_row_signal_and_state_match_recorded_decision(ledger, checkpoint):
    day = {d["moment"]: d for d in checkpoint["decisions"]
           if d["moment"][:10] == TARGET_DAY}
    for r in ledger["rows"]:
        d = day[r["timestamp"]]
        assert r["signal"] == d["signal"]
        assert r["state_before"] == d["state_before"]
        assert r["state_after"] == d["state_after"]
        assert r["action"] == (",".join(d["actions"]) or "NO_ACTION")
        assert r["reason"] == d["reason"]


def test_hold_rows_have_no_order_events(ledger):
    for r in ledger["rows"]:
        if r["action"] == "NO_ACTION":
            assert r["action_label"] in ("HOLD", "STAY FLAT")
            assert r["order1_semantic"] == ""
            assert r["order2_semantic"] == ""
            assert r["order1_order_id"] == ""
            assert r["order2_order_id"] == ""
        else:
            assert r["order1_semantic"] != ""


def test_every_fill_appears_exactly_once_order1_or_order2(ledger, checkpoint):
    day_fills = [f for f in checkpoint["fills"] if f["filled_at"][:10] == TARGET_DAY]
    assert len(day_fills) == EXPECTED_FILLS
    seen: set[str] = set()
    for r in ledger["rows"]:
        for key in ("order1_order_id", "order2_order_id"):
            oid = r[key]
            if oid:
                assert oid not in seen, f"fill duplicated in ledger: {oid}"
                seen.add(oid)
    assert seen == {f["order_id"] for f in day_fills}


def test_order_semantics_match_recorded_side_leg(ledger, checkpoint):
    """OPEN PUT=SELL PUT, CLOSE PUT=BUY PUT, OPEN CALL=BUY CALL,
    CLOSE CALL=SELL CALL -- built from the recorded fill side+leg."""
    day_fills = {f["order_id"]: f for f in checkpoint["fills"]
                 if f["filled_at"][:10] == TARGET_DAY}
    for r in ledger["rows"]:
        o1 = r["order1_order_id"]
        if o1:
            f = day_fills[o1]
            assert r["order1_side"] == f["side"]
            assert r["order1_leg"] == f["leg"]
            assert r["order1_semantic"] == SEMANTIC_OK[(f["leg"], f["side"])]
            assert str(r["order1_qty"]) == str(f["quantity"])
            assert r["order1_price"] == f["price"]
        o2 = r["order2_order_id"]
        if o2:
            f = day_fills[o2]
            assert r["order2_side"] == f["side"]
            assert r["order2_leg"] == f["leg"]
            assert r["order2_semantic"] == SEMANTIC_OK[(f["leg"], f["side"])]
            assert str(r["order2_qty"]) == str(f["quantity"])
            assert r["order2_price"] == f["price"]


def test_switch_expands_to_two_order_events(ledger):
    sw = [r for r in ledger["rows"] if r["action"].startswith("SWITCH")]
    assert len(sw) == 1
    r = sw[0]
    assert r["timestamp"] == "2025-06-18T09:25:00"
    assert r["signal"] == "BULLISH"
    assert r["position_before"] == "PUT"
    assert r["position_after"] == "CALL"
    # close-then-open: BUY PUT closes the existing PUT; BUY CALL opens the new CALL
    assert (r["order1_side"], r["order1_leg"]) == ("BUY", "PUT")
    assert r["order1_semantic"] == "CLOSE PUT"
    assert (r["order2_side"], r["order2_leg"]) == ("BUY", "CALL")
    assert r["order2_semantic"] == "OPEN CALL"


def test_recomputed_signal_matches_recorded_at_all_72(checkpoint, ledger):
    """Frozen DonchianBreakout(20,10) on recorded-history prefixes reproduces
    every recorded decision signal."""
    from fno_ai_paper_trading.paper_track.store import market_price_from_dict
    from fno_ai_paper_trading.strategies.research_candidates import DonchianBreakout
    hist = [market_price_from_dict(b) for b in checkpoint["history"]]
    strat = DonchianBreakout(entry_channel=ENTRY_CHANNEL, exit_channel=EXIT_CHANNEL)
    mismatch = 0
    for r in ledger["rows"]:
        T = datetime.fromisoformat(r["timestamp"])
        prefix = [b for b in hist if b.timestamp + timedelta(minutes=5) <= T]
        res = strat.analyze(list(prefix))
        mapped = {"BUY": "BULLISH", "SELL": "BEARISH", "HOLD": "NEUTRAL"}[
            res.signal.value
        ]
        if mapped != r["signal"]:
            mismatch += 1
    assert mismatch == 0


def test_dch20_and_dch10_reconcile_with_trade_ledger(ledger, trade_ledger):
    """DCH20 at the entry moment equals the recorded donchian_upper/lower; DCH10
    at the entry moment equals the recorded exit_channel_upper/lower (the ledger
    attaches the replay row to the ENTRY candle)."""
    day_trades = [t for t in trade_ledger if t["entry_timestamp"][:10] == TARGET_DAY]
    assert len(day_trades) == EXPECTED_TRADES
    row_by = {r["timestamp"]: r for r in ledger["rows"]}
    for t in day_trades:
        r = row_by[t["entry_timestamp"]]
        assert r["dch20_hi"] == t["donchian_upper"], (
            f"trade {t['trade_number']} dch20 hi mismatch")
        assert r["dch20_lo"] == t["donchian_lower"], (
            f"trade {t['trade_number']} dch20 lo mismatch")
        assert r["dch10_hi"] == t["exit_channel_upper"], (
            f"trade {t['trade_number']} dch10(exit) hi mismatch")
        assert r["dch10_lo"] == t["exit_channel_lower"], (
            f"trade {t['trade_number']} dch10(exit) lo mismatch")


def test_day_boundary_facts(ledger, checkpoint):
    db = ledger["day_boundary"]
    assert db["entries"] == ENTRY_CHANNEL
    assert db["exit_channel"] == EXIT_CHANNEL
    assert db["warmup_bars"] == WARMUP_BARS
    assert db["candles_per_session"] == 75
    sessions = sorted(set(b["timestamp"][:10] for b in checkpoint["history"]))
    assert db["sessions_in_history"] == len(sessions)
    assert db["history_first"] == checkpoint["history"][0]["timestamp"]
    assert db["history_last"] == checkpoint["history"][-1]["timestamp"]
    # continuous 61-session history, no resets; day candles carried
    assert len(checkpoint["history"]) == 4575
    assert db["bars_before_day"] == 1425
    assert db["first_decision_prefix_len"] == FIRST_PREFIX_LEN


def test_first_decision_window_spans_prior_session(checkpoint):
    """Monday 09:20 DCH20 window is entirely prior-session candles (frozen
    window semantics + recorded history)."""
    from datetime import date
    from fno_ai_paper_trading.paper_track.store import market_price_from_dict
    hist = [market_price_from_dict(b) for b in checkpoint["history"]]
    T = datetime(2025, 6, 16, 9, 20)  # Monday
    prefix = [b for b in hist if b.timestamp + timedelta(minutes=5) <= T]
    i = len(prefix) - 1
    win = prefix[i - ENTRY_CHANNEL:i]
    assert win[0].timestamp.date() == date(2025, 6, 13)  # Friday
    assert win[-1].timestamp.date() == date(2025, 6, 13)
    assert prefix[-1].timestamp.date() == date(2025, 6, 16)


def test_warmup_threshold():
    """Min candles for a directional signal == max(20,10)+1 = 21 (frozen)."""
    from fno_ai_paper_trading.strategies.research_candidates import DonchianBreakout
    import datetime as _dt
    from fno_ai_paper_trading.models.instruments import Instrument
    from fno_ai_paper_trading.models.market import MarketPrice
    from fno_ai_paper_trading.models.enums import InstrumentType
    ins = Instrument(
        symbol="NIFTY 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY 50",
        exchange="NSE",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    base = _dt.datetime(2025, 6, 18, 9, 15)
    bars = []
    px = 24000
    for k in range(0, 25):
        bars.append(MarketPrice(
            instrument=ins,
            timestamp=base + _dt.timedelta(minutes=5 * k),
            open=Decimal(px), high=Decimal(px + 1), low=Decimal(px - 1),
            close=Decimal(px), volume=0, open_interest=0,
        ))
    strat = DonchianBreakout(entry_channel=20, exit_channel=10)
    # 15..20 bars => warmup HOLD; >=21 allowed
    for n in range(15, 21):
        res = strat.analyze(list(bars[:n]))
        assert res.signal.value == "HOLD", f"{n} bars not warmup"
    res = strat.analyze(list(bars[:21]))
    assert res.signal.value == "HOLD"  # flat + no breakout
    assert "warmup" not in res.reason.lower()


def test_reconciliation_counts(ledger):
    """72 decisions / 16 fills / 8 closures / 8 approvals / 8 trades."""
    assert ledger["day"]["decision_points"] == 72
    assert ledger["day"]["fills"] == 16
    assert ledger["day"]["closures"] == 8
    assert ledger["day"]["entry_approvals"] == 8
    assert ledger["day"]["trades"] == 8


def test_orders_carry_trade_numbers(ledger):
    """Each order event is tied to exactly one recorded trade (#68..#75)."""
    tns = set()
    for r in ledger["rows"]:
        for key in ("order1_trade", "order2_trade"):
            if r[key]:
                tns.add(int(r[key]))
    assert tns == set(range(68, 76))


def test_hold_count_and_labels(ledger):
    holds = [r for r in ledger["rows"] if r["action"] == "NO_ACTION"]
    labels = {r["action_label"] for r in holds}
    assert labels <= {"HOLD", "STAY FLAT"}
    assert len(holds) == EXPECTED_HOLDS


def test_prediction_over_directional_rows_only(ledger):
    d = ledger["day"]["prediction_counts"]
    assert set(d) <= {"PREDICTION_CORRECT", "PREDICTION_WRONG",
                      "FLAT/NEUTRAL", "NO_NEXT_CANDLE"}
    directional = d["PREDICTION_CORRECT"] + d["PREDICTION_WRONG"]
    sig = ledger["day"]["signal_counts"]
    assert directional == sig["BULLISH"] + sig["BEARISH"]
    assert d["FLAT/NEUTRAL"] == sig["NEUTRAL"]
    assert d.get("NO_NEXT_CANDLE", 0) == 0


def test_csv_matches_json_rows(ledger):
    with open(LEDGER_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(ledger["rows"])
    for cr, jr in zip(rows, ledger["rows"]):
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
    md = LEDGER_MD.read_text(encoding="utf-8")
    for section in (
        "## §1 Objective", "## §2 Provenance", "## §3 Exact historical candle window",
        "## §4 Day-start behavior", "## §5 Signal definition", "## §6 Complete",
        "## §7 Explicit order-by-order", "## §8 Every position switch",
        "## §9 Next-candle directional prediction test", "## §10 Signal vs position",
        "## §11 Reconciliation", "## §12 Inconsistencies", "## §13 Conclusions",
        "## FINAL KEY QUESTION",
        "NOT AVAILABLE FROM RECORDED ARTIFACT",
    ):
        assert section in md, f"missing section/text in markdown: {section}"