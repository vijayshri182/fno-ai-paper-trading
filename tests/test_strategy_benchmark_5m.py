"""Test matrix for the research-only 5M strategy benchmark.

Covers the two new research modules:

* ``strategy_benchmark_5m_candidates`` — causality (no-look-ahead prefix
  property for every pre-registered stream), the Donchian baseline identity
  (``stream[i] == DonchianBreakout.analyze(bars[:i+1])``), ORB first-three-
  candle semantics (range never redefined, first tradable reference 09:30),
  warm-ups, the VWAP NOT-TESTABLE stub, registry and frozen param overrides,
* ``strategy_benchmark_5m`` — subclass == base engine identity on synthetic
  sessions, window slicing, feed delta semantics, cost-stress economics
  (``net_at_1x == engine net``), next-candle accuracy (cross-session pairs
  excluded), the classification gate matrix, report writers, and the real
  Dev-window equivalence proof against the recorded fingerprint
  ``b1be2188...a8c6`` (recorded-artifact check).

Project rules preserved by the tests themselves: nothing is written anywhere
outside ``tmp_path``, no live/paper artefact is touched, no commit/push and no
promotion logic is exercised beyond the classification-only verdicts.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.experiments.directional_15m.contract import (
    Signal15m,
    signal_to_15m,
)
from fno_ai_paper_trading.experiments.directional_5m.executor import (
    Directional5MOptionsConfig,
)
from fno_ai_paper_trading.experiments.directional_5m.runner import replay_experiment
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT, trading_day_sequence
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.research.strategy_benchmark_5m import (
    RECORDED_DEV_FINGERPRINT,
    WindowHistoryFeed,
    build_required_table,
    classify_candidate,
    cost_stress,
    dev_window,
    equivalence_check,
    full_window,
    load_dataset_verified,
    next_candle_accuracy,
    oos_window,
    re_stamp_bars,
    replay_strategized,
    write_artifacts,
)
from fno_ai_paper_trading.research.strategy_benchmark_5m_candidates import (
    ATRRegimeParams,
    CANDIDATES,
    DONCHIAN_WARMUP_BARS,
    EMATrendParams,
    ORBParams,
    PARAM_OVERRIDES,
    VWAP_VOLUME_ZERO_NOTE,
    _ema_regime,
    aggregate_candles,
    apply_overrides,
    build_stream,
    session_minute_index,
)
from fno_ai_paper_trading.strategies.research_candidates import (
    donchian_breakout_signals,
)

NEUTRAL = Signal15m.NEUTRAL
BULLISH = Signal15m.BULLISH
BEARISH = Signal15m.BEARISH
_NON_TESTABLE = [c.candidate_id for c in CANDIDATES.values() if c.not_testable_reason]
_NON_TESTABLE_CIDS = {c.candidate_id for c in CANDIDATES.values() if c.not_testable_reason}
_TESTABLE = [c for c in CANDIDATES.values() if not c.not_testable_reason]

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# synthetic data helpers
# ---------------------------------------------------------------------------


def _bar(
    ts: datetime,
    *,
    o: object,
    h: object,
    l: object,
    c: object,
    volume: int = 1000,
) -> MarketPrice:
    return MarketPrice(
        instrument=TRACK_INSTRUMENT(),
        timestamp=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=volume,
    )


def synthetic_walk_bars(days: list[date], seed: int = 11) -> list[MarketPrice]:
    """Deterministic random-walk 5m bars on the 09:15..15:25 session grid."""
    rng = random.Random(seed)
    px = 24000.0
    bars: list[MarketPrice] = []
    for d in days:
        for k in range(75):
            ts = datetime.combine(d, time(9, 15)) + timedelta(minutes=5 * k)
            close = px + rng.uniform(-35.0, 35.0)
            o = px
            hi = max(o, close) + 5.0
            lo = min(o, close) - 5.0
            bars.append(_bar(ts, o=o, h=hi, l=lo, c=close))
            px = close
    return bars


def three_trading_days() -> list[date]:
    return trading_day_sequence(date(2026, 9, 21), 3)


# ---------------------------------------------------------------------------
# candidates module: registry / shape
# ---------------------------------------------------------------------------


def test_registry_has_exactly_six_pre_registered_specs():
    assert set(CANDIDATES) == {
        "donchian_20_10_baseline",
        "c1_ema_trend",
        "c2_orb",
        "c3_vwap_mr",
        "c4_atr_regime",
        "c5_mtf_hybrid",
    }
    assert CANDIDATES["donchian_20_10_baseline"].baseline is True
    assert len(_NON_TESTABLE) == 1
    assert "c3_vwap_mr" in _NON_TESTABLE_CIDS


def test_every_stream_is_signal15m_and_same_length():
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=5)
    for spec in CANDIDATES.values():
        stream, diagnostics = build_stream(spec, bars)
        assert len(stream) == len(bars)
        assert all(isinstance(s, Signal15m) for s in stream)

    c3 = build_stream(CANDIDATES["c3_vwap_mr"], bars)[0]
    assert c3 == [NEUTRAL] * len(bars)
    assert build_stream(CANDIDATES["c3_vwap_mr"], bars)[1]["not_testable_reason"]


def test_donchian_identity_with_breakout_analyze_prefix():
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=7)
    stream, diagnostics = build_stream(CANDIDATES["donchian_20_10_baseline"], bars)
    assert diagnostics["params"]["warmup_bars"] == DONCHIAN_WARMUP_BARS == 21
    for i in range(len(bars)):
        expected = signal_to_15m(
            donchian_breakout_signals(
                bars[: i + 1], entry_channel=20, exit_channel=10
            )[-1].signal.value
        )
        assert stream[i] is expected, f"donchian identity failed at index {i}"


def test_no_look_ahead_prefix_property_for_every_candidate():
    """``stream(i)`` must be purely a function of ``bars[:i+1]``."""
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=13)
    overrides = {
        "c1_ema_trend": {"bars_per_candle": 3, "fast": 4, "slow": 6},
        "c5_mtf_hybrid": {"m15_bars": 3, "h1_bars": 3, "fast": 4, "slow": 6},
    }
    for spec in _TESTABLE:
        params = spec.params
        if spec.candidate_id in overrides:
            params = apply_overrides(params, overrides[spec.candidate_id])
        stream_full, _ = spec.builder(bars, params)
        assert len(stream_full) == len(bars)
        for i in range(len(bars)):
            stream_prefix, _ = spec.builder(bars[: i + 1], params)
            assert stream_full[i] == stream_prefix[-1], (
                f"{spec.candidate_id} leaks the future at index {i}"
            )


def test_donchian_warmup_is_neutral():
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=3)
    stream, _ = build_stream(CANDIDATES["donchian_20_10_baseline"], bars)
    assert stream[0] is NEUTRAL


# ---------------------------------------------------------------------------
# candidates module: session / candle primitives
# ---------------------------------------------------------------------------


def test_session_minute_index_grid():
    day = date(2026, 9, 21)
    assert session_minute_index(datetime.combine(day, time(9, 15))) == 0
    assert session_minute_index(datetime.combine(day, time(9, 20))) == 1
    assert session_minute_index(datetime.combine(day, time(15, 25))) == 74
    assert session_minute_index(datetime.combine(day, time(9, 10))) == -1


def test_aggregate_candles_is_causal_and_never_partial():
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=2)
    candles, candle_at = aggregate_candles(bars, bars_per_candle=3)
    # 75 bars/day -> 25 full 3-bar candles/day, never a partial trailing bucket.
    assert len(candles) == 75
    assert all(c.count == 3 for c in candles)
    assert candle_at[0] is None
    assert candle_at[1] is None
    assert candle_at[2] == 0
    assert candle_at[3] == 0
    assert candle_at[-1] == len(candles) - 1
    # Candle boundaries never span a session.
    day2_first = datetime.combine(days[1], time(9, 15))
    for c in candles:
        assert c.start_idx <= c.end_idx
        assert bars[c.start_idx].timestamp <= bars[c.end_idx].timestamp


# ---------------------------------------------------------------------------
# candidates module: EMA-gate, ORB, ATR, MTF semantics
# ---------------------------------------------------------------------------


def test_ema_regime_bucket_semantics():
    from fno_ai_paper_trading.strategies.indicators import ema

    candles, _ = aggregate_candles(synthetic_walk_bars(three_trading_days()), 3)
    # Uptrend series: fast EMA stays above the slow EMA.
    up = [Decimal(str(i)) for i in range(1, len(candles) + 1)]
    fast_up = ema(up, 4)
    slow_up = ema(up, 6)
    assert _ema_regime(candles, None, fast_up, slow_up) is None  # no candle yet
    reg = _ema_regime(candles, len(candles) - 1, fast_up, slow_up)
    assert reg == "BULL"


def test_ema_trend_gate_only_allows_aligned_trigger_legs():
    from fno_ai_paper_trading.research.strategy_benchmark_5m_candidates import (
        build_ema_trend_stream as _ema_stream,
    )

    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=21)
    params = EMATrendParams(bars_per_candle=3, fast=4, slow=6)
    stream, diag = _ema_stream(bars, params)
    raw = donchian_breakout_signals(bars, entry_channel=20, exit_channel=10)
    for i in range(len(bars)):
        if stream[i] is BULLISH:
            assert signal_to_15m(raw[i].signal.value) is BULLISH
        if stream[i] is BEARISH:
            assert signal_to_15m(raw[i].signal.value) is BEARISH
    assert "regime_bucket" in diag
    assert sum(diag["regime_bucket"].values()) == len(bars)


def test_orb_uses_exactly_the_first_three_candles_and_is_fixed():
    day = date(2026, 9, 21)
    # First three candles: high 106, low 97  -> range.
    prices = {
        time(9, 15): (100, 100, 102, 98),
        time(9, 20): (100, 104, 106, 99),
        time(9, 25): (104, 102, 105, 97),
        time(9, 30): (102, 99, 103, 98),   # inside range -> NEUTRAL
        time(9, 35): (99, 110, 111, 95),   # > 106 -> BULLISH
        time(10, 0): (110, 102, 112, 100), # inside -> NEUTRAL
        time(9, 45): (110, 112, 130, 80),  # huge range; must NOT redefine
        time(12, 30): (112, 92, 114, 91),  # < original low 97 -> BEARISH
        time(13, 0): (92, 110, 111, 90),   # > original high 106 -> BULLISH
    }
    bars = []
    for k in range(75):
        ts = datetime.combine(day, time(9, 15)) + timedelta(minutes=5 * k)
        o, c, h, l = prices.get(ts.time(), (100, 100, 101, 99))
        bars.append(_bar(ts, o=o, h=h, l=l, c=c))
    stream, diag = build_stream(CANDIDATES["c2_orb"], bars)
    idx = {ts: i for i, ts in enumerate(b.timestamp for b in bars)}
    expect = {
        time(9, 15): NEUTRAL,
        time(9, 20): NEUTRAL,
        time(9, 25): NEUTRAL,
        time(9, 30): NEUTRAL,
        time(9, 35): BULLISH,
        time(10, 0): NEUTRAL,
        time(9, 45): BULLISH,   # 112 close > 106 (range not redrawn to 130)
        time(12, 30): BEARISH,  # 92 close < 97 (range not redrawn to 80)
        time(13, 0): BULLISH,   # 110 close > 106 again
    }
    for t, expected in expect.items():
        assert stream[idx[datetime.combine(day, t)]] is expected, t
    assert diag["range_days"] == 1


def test_orb_first_tradable_reference_is_0930():
    day = date(2026, 9, 21)
    bars = synthetic_walk_bars([day], seed=1)
    stream, diag = build_stream(CANDIDATES["c2_orb"], bars)
    assert diag["params"]["first_tradable_reference_minute"] == "09:30"
    assert diag["params"]["first_tradable_decision_minute"] == "09:35"
    assert stream[0] is NEUTRAL and stream[1] is NEUTRAL and stream[2] is NEUTRAL


def test_atr_regime_invariants():
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=31)
    stream, diag = build_stream(CANDIDATES["c4_atr_regime"], bars)
    raw = donchian_breakout_signals(bars, entry_channel=20, exit_channel=10)
    from fno_ai_paper_trading.strategies.indicators import atr

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    atrs = atr(highs, lows, closes, 14)
    bucket = diag["regime_bucket"]
    assert set(bucket) <= {"WARMUP", "TREND", "RANGE", "TRANSITION"}
    assert sum(bucket.values()) == len(bars)
    # A directional signal requires a usable ATR reading for that bar.
    for i in range(len(bars)):
        if stream[i] in (BULLISH, BEARISH):
            assert atrs[i] is not None
        if stream[i] is BULLISH:
            assert signal_to_15m(raw[i].signal.value) is BULLISH
        if stream[i] is BEARISH:
            assert signal_to_15m(raw[i].signal.value) is BEARISH


def test_mtf_hybrid_requires_full_confluence():
    days = trading_day_sequence(date(2026, 9, 21), 4)
    bars = synthetic_walk_bars(days, seed=41)
    params = apply_overrides(
        CANDIDATES["c5_mtf_hybrid"].params,
        {"m15_bars": 3, "h1_bars": 3, "fast": 4, "slow": 6},
    )
    stream = CANDIDATES["c5_mtf_hybrid"].builder(bars, params)[0]
    raw = donchian_breakout_signals(bars, entry_channel=20, exit_channel=10)

    from fno_ai_paper_trading.research.strategy_benchmark_5m_candidates import (
        _day_open_close as _oc,
    )
    from fno_ai_paper_trading.strategies.indicators import ema

    m15, m15_at = aggregate_candles(bars, params.m15_bars)
    h1, h1_at = aggregate_candles(bars, params.h1_bars)
    mf = ema([c.close for c in m15], params.fast)
    ms = ema([c.close for c in m15], params.slow)
    hf = ema([c.close for c in h1], params.fast)
    hs = ema([c.close for c in h1], params.slow)
    oc = _oc(bars)
    dates = list(oc.keys())
    prev = {dates[k]: oc[dates[k - 1]] for k in range(1, len(dates))}
    for i in range(len(bars)):
        pd = prev.get(bars[i].timestamp.date())
        pd_bull = pd is not None and pd[1] > pd[0]
        pd_bear = pd is not None and pd[1] < pd[0]
        mreg = _ema_regime(m15, m15_at[i], mf, ms)
        hreg = _ema_regime(h1, h1_at[i], hf, hs)
        if stream[i] is BULLISH:
            assert (mreg, hreg) == ("BULL", "BULL") and pd_bull
            assert signal_to_15m(raw[i].signal.value) is BULLISH
        if stream[i] is BEARISH:
            assert (mreg, hreg) == ("BEAR", "BEAR") and pd_bear
            assert signal_to_15m(raw[i].signal.value) is BEARISH


def test_first_session_of_mtf_has_no_prev_day_context():
    days = [date(2026, 9, 21)]
    bars = synthetic_walk_bars(days, seed=51)
    stream, _ = CANDIDATES["c5_mtf_hybrid"].builder(bars, CANDIDATES["c5_mtf_hybrid"].params)
    assert all(s is NEUTRAL for s in stream)


def test_apply_overrides_is_immutable_and_typed():
    for cid in ("c1_ema_trend", "c2_orb", "c4_atr_regime", "c5_mtf_hybrid"):
        assert cid in PARAM_OVERRIDES
    orig = CANDIDATES["c1_ema_trend"].params
    modified = apply_overrides(orig, {"fast": 16, "slow": 40})
    assert orig.fast == 20 and orig.slow == 50
    assert isinstance(modified, EMATrendParams)
    assert (modified.fast, modified.slow) == (16, 40)
    for cid in PARAM_OVERRIDES:
        for ov in PARAM_OVERRIDES[cid]:
            altered = apply_overrides(CANDIDATES[cid].params, ov)
            assert type(altered) is type(CANDIDATES[cid].params)
    # baseline and NOT-TESTABLE families are never perturbed.
    assert "donchian_20_10_baseline" not in PARAM_OVERRIDES
    assert "c3_vwap_mr" not in PARAM_OVERRIDES


def test_vwap_note_never_fabricates_volume():
    assert "87,193" in VWAP_VOLUME_ZERO_NOTE
    assert "NOT TESTABLE" in CANDIDATES["c3_vwap_mr"].not_testable_reason


# ---------------------------------------------------------------------------
# harness module: window slicing / feed
# ---------------------------------------------------------------------------


def test_window_slicing_partitions_the_series():
    from fno_ai_paper_trading.research.strategy_benchmark_5m import DEV_START, OOS_START

    n = lambda ts: _bar(ts, o=1, h=2, l=1, c=1.5)
    bars = [
        n(datetime.combine(DEV_START, time(9, 15))),
        n(datetime.combine(date(2025, 8, 15), time(9, 15))),
        n(datetime.combine(date(2025, 10, 3), time(9, 15))),
        n(datetime.combine(OOS_START, time(9, 15))),
        n(datetime.combine(date(2026, 9, 11), time(9, 15))),
    ]
    dev, dev_days, n_dev = dev_window(bars)
    full, full_days, n_full = full_window(bars)
    oos, oos_days, n_oos = oos_window(bars)
    assert dev_days == [DEV_START]
    assert dev == bars[:1]
    assert full_days == [DEV_START, date(2025, 8, 15), date(2025, 10, 3)]
    assert oos_days == [OOS_START, date(2026, 9, 11)]
    assert n_dev == 1 and n_full == 3 and n_oos == 2


def test_window_history_feed_delta_and_full_prefix():
    day = date(2026, 9, 21)
    inst = TRACK_INSTRUMENT()
    bars = [
        _bar(datetime.combine(day, time(9, 15)), o=1, h=2, l=1, c=1),
        _bar(datetime.combine(day, time(9, 20)), o=1, h=2, l=1, c=2),
        _bar(datetime.combine(day, time(9, 25)), o=2, h=3, l=1, c=3),
    ]
    delta = WindowHistoryFeed(inst, bars, delta=True)
    at = lambda hhmm: datetime.combine(day, time(*map(int, hhmm.split(":"))))
    assert delta.bars_up_to(at("09:20")) == bars[:1]
    assert delta.bars_up_to(at("09:30")) == bars[1:]  # only the increment
    full = WindowHistoryFeed(inst, bars, delta=False)
    assert full.bars_up_to(at("09:20")) == bars[:1]
    assert full.bars_up_to(at("09:30")) == bars  # full completed prefix


def test_next_candle_accuracy_excludes_cross_session_pairs():
    day1, day2 = date(2026, 9, 21), date(2026, 9, 22)
    b0 = _bar(datetime.combine(day1, time(9, 15)), o=1, h=2, l=1, c=1)
    b1 = _bar(datetime.combine(day1, time(9, 20)), o=1, h=3, l=1, c=2)
    b2 = _bar(datetime.combine(day2, time(9, 15)), o=2, h=3, l=1, c=1)
    bars = [b0, b1, b2]
    stream = [BULLISH, BEARISH, BEARISH]
    res = next_candle_accuracy(stream, bars)
    # Only (b0 -> b1) is same-session: BULLISH count 1, pair resolved up.
    assert res["bullish_signals"] == 1
    assert res["bullish_next_candle_up"] == 1
    assert res["bullish_accuracy_pct"] == 100.0
    # Day1-last BEARISH never pairs with day2-first BEARISH (overnight excluded).
    assert res["bearish_signals"] == 0
    assert res["bearish_next_candle_down"] == 0


# ---------------------------------------------------------------------------
# harness module: economics / classification / reports
# ---------------------------------------------------------------------------


def test_cost_stress_1x_recovers_engine_net():
    metrics = {
        "gross_realized_pnl": "-11696.30900",
        "slippage_estimate": "11667.50900",
        "commissions": "3500.252708640",
        "net_pnl": "-15196.561708640",
    }
    cs = cost_stress(metrics)
    assert Decimal(cs["net_at_1x"]) == Decimal(metrics["net_pnl"])
    assert cs["gross_close_pnl"] == "-28.80000"
    assert cs["costs_1x"] == "15167.761708640"
    known = {
        "net_at_0x": "-28.80000",
        "net_at_3x": "-45532.085125920",
        "net_at_5x": "-75867.608543200",
    }
    for key, value in known.items():
        assert Decimal(cs[key]) == Decimal(value)


def _fake_metrics(round_trips=30, gross="1000", slip="100", net="800", dd="0.05"):
    return {
        "round_trips": round_trips,
        "gross_realized_pnl": gross,
        "slippage_estimate": slip,
        "net_pnl": net,
        "max_drawdown_pct": dd,
        "win_rate": "0.3",
    }


def test_classify_candidate_gate_matrix():
    spec = CANDIDATES["c1_ema_trend"]
    c3 = CANDIDATES["c3_vwap_mr"]
    baseline = CANDIDATES["donchian_20_10_baseline"]
    rows = {"dev": _fake_metrics(), "full": _fake_metrics(), "oos": _fake_metrics()}

    assert classify_candidate(c3, rows)["verdict"] == "not testable"
    assert classify_candidate(baseline, rows)["verdict"] == "baseline reference"
    assert classify_candidate(spec, {})["verdict"] == "insufficient evidence"

    few = dict(rows)
    few["oos"] = _fake_metrics(round_trips=10)
    assert classify_candidate(spec, few)["verdict"] == "insufficient evidence"

    neg_gross = dict(rows)
    neg_gross["oos"] = _fake_metrics(gross="-1000", slip="100", net="-800")
    assert classify_candidate(spec, neg_gross)["verdict"] == "rejected by gate"

    edge_destroyed = dict(rows)
    edge_destroyed["oos"] = _fake_metrics(gross="100", slip="300", net="-200")
    assert classify_candidate(spec, edge_destroyed)["verdict"] == "rejected by gate"

    blown = dict(rows)
    blown["full"] = _fake_metrics(net="100", dd="0.5")
    assert classify_candidate(spec, blown)["verdict"] == "unstable"

    flip = dict(rows)
    flip["dev"] = _fake_metrics(net="-100")
    assert classify_candidate(spec, flip)["verdict"] == "unstable"

    assert classify_candidate(spec, rows)["verdict"] == "promising (research-only)"


def test_build_required_table_rows_for_registry():
    dev = {
        "signals_directional": 150,
        "round_trips": 30,
        "win_rate": "0.1",
        "gross_realized_pnl": "100.0",
        "slippage_estimate": "50.0",
        "commissions": "10.0",
        "net_pnl": "40.0",
        "max_drawdown": "5.0",
    }
    candidates_out = {
        cid: {"not_testable_reason": None, "note": "", "windows": {}}
        for cid in CANDIDATES
    }
    candidates_out["c2_orb"]["windows"] = {"dev": dev, "oos": dev}
    candidates_out["c3_vwap_mr"]["not_testable_reason"] = "volume == 0"
    rows = build_required_table(candidates_out)
    by_cid = {r["candidate"]: r for r in rows}
    assert len(rows) == len(CANDIDATES)
    assert by_cid["c2_orb"]["trades"] == 30
    assert by_cid["c2_orb"]["net_pnl"] == "40.0"
    assert by_cid["c3_vwap_mr"]["trades"] is None
    assert "volume" in by_cid["c3_vwap_mr"]["note"]
    assert by_cid["donchian_20_10_baseline"]["note"] == "not run"


def test_write_artifacts_three_files_and_csv(tmp_path):
    payload = {
        "report_version": "5M_STRATEGY_BENCHMARK",
        "dataset": {
            "name": "x.csv",
            "data_hash": "abc123",
            "bars_total": 0,
            "hash_verified": True,
        },
        "windows": {
            "dev": {"first": "2025-05-22", "last": "2025-08-14", "bars": 0, "sessions": 0},
            "full": {"first": "2022-01-03", "last": "2025-10-03", "bars": 0, "sessions": 0},
            "oos": {"first": "2025-10-06", "last": "2026-09-11", "bars": 0, "sessions": 0},
        },
        "equivalence": {
            "ok": True,
            "fingerprint_run_A": "a" * 64,
            "fingerprint_run_B": "a" * 64,
            "fingerprints_identical": True,
            "matches_recorded": True,
            "recorded_fingerprint": "a" * 64,
            "signals_match_recorded": True,
            "metric_diffs": {},
            "note": "x",
        },
        "candidates": {},
        "classification": {},
        "required_table": [],
        "algo_ready": "NO",
        "no_promotion": True,
        "declared_winner": None,
        "paper_only_banner": "paper",
        "store_root": "tmp",
    }
    closures = {
        "c2_orb": {
            "dev": [
                {
                    "leg": "CALL",
                    "kind": "signal",
                    "entered_at": "2025-05-22T09:35:00",
                    "exited_at": "2025-05-22T09:40:00",
                    "minutes": 5,
                    "quantity": 1,
                    "realized": "-10.000",
                }
            ],
            "full": [],
            "oos": [],
        }
    }
    paths = write_artifacts(tmp_path, payload, closures)
    assert len(paths) == 3
    assert all(p.exists() for p in paths)
    assert json.loads(paths[0].read_text(encoding="utf-8"))["algo_ready"] == "NO"
    csv = paths[2].read_text(encoding="utf-8")
    assert csv.splitlines()[0].startswith("window,candidate_id")
    assert "dev,c2_orb,CALL,signal,2025-05-22T09:35:00" in csv


# ---------------------------------------------------------------------------
# harness module: engine identity on synthetic sessions
# ---------------------------------------------------------------------------


def test_subclass_equals_base_on_synthetic_sessions(tmp_path):
    """The only overridden behaviour is the signal source; everything else,
    including the deterministic fingerprint, must match the base engine."""
    days = three_trading_days()
    bars = synthetic_walk_bars(days, seed=17)
    feed = WindowHistoryFeed(TRACK_INSTRUMENT(), bars)
    stream, _ = build_stream(CANDIDATES["donchian_20_10_baseline"], bars)

    base_config = Directional5MOptionsConfig(account="exp_base", store_dir=tmp_path / "base")
    base_store = TrackStore(base_config.store_dir, base_config.account)
    base = replay_experiment(config=base_config, store=base_store, feed=feed, days=days)

    sub_config = Directional5MOptionsConfig(account="exp_sub", store_dir=tmp_path / "sub")
    sub_store = TrackStore(sub_config.store_dir, sub_config.account)
    sub = replay_strategized(
        cfg=sub_config,
        store=sub_store,
        feed=feed,
        days=days,
        stream=stream,
        window_bars=bars,
        persist=True,
        run_id="sub-identity",
    )

    assert base.stable_fingerprint() == sub.stable_fingerprint()
    rb = base.report()
    rs = sub.report()
    assert rs["accounting"]["net_pnl"] == rb["accounting"]["net_pnl"]
    assert rs["decisions"]["decision_points_total"] == rb["decisions"]["decision_points_total"]
    assert rb["decisions"]["decision_points_total"] == 72 * len(days)
    assert rs["data_quality"]["data_errors"] == rb["data_quality"]["data_errors"] == 0
    assert rs["accounting"]["reconciled"] is True


def test_benchmark_stream_and_replay_share_the_same_depth():
    """The drive pattern must equal day_ticks exactly (78 instants, 72 decisions)."""
    from fno_ai_paper_trading.paper_track.runner import day_ticks

    days = three_trading_days()
    assert len(day_ticks(days[0])) == 78


def test_equivalence_reproduces_recorded_baseline(tmp_path):
    """Heavy integration check: the subclass + Donchian stream over the real Dev
    window must reproduce the recorded fingerprint and metrics exactly."""
    dataset = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
    if not dataset.exists():
        pytest.skip("dataset not present in this checkout")
    ds = load_dataset_verified(REPO)
    bars = re_stamp_bars(ds)
    dev_bars, dev_days, _ = dev_window(bars)
    eq = equivalence_check(sel_bars=dev_bars, days=dev_days, store_root=tmp_path / "stores")
    assert eq["fingerprints_identical"] is True
    assert eq["matches_recorded"] is True
    assert eq["fingerprint_run_A"] == RECORDED_DEV_FINGERPRINT
    assert eq["signals_match_recorded"] is True
    assert eq["metric_diffs"] == {}
    assert eq["ok"] is True