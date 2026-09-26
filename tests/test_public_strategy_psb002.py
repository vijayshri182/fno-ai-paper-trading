"""Tests for PSB-002 - PUBLIC STRATEGY BENCHMARK 002 (NIFTY 50 range breakout).

This is an EXTERNAL BENCHMARK.  Tests verify the source-audit constants,
range-construction and breakout-detection logic, no-lookahead behaviour,
determinism, and the classification result.  They do NOT touch the protected
internal algorithm (model_0 / Iteration-009..012 / protected OOS).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fno_ai_paper_trading.research.public_strategy_psb002 import (
    AM_WINDOWS,
    BEST4,
    DATASET,
    DEFAULT_SL_MODE,
    HOLD_BARS,
    HOLD_MINS,
    MIN_RANGE,
    OUT_JSON,
    PM_WINDOWS,
    RESEARCH_END,
    RESEARCH_START,
    REPO_URL,
    _all_trading_days,
    _day_type,
    _equity_curve,
    _research_bars,
    _run_one_window,
    calc_metrics,
    load_csv,
    run_strategy,
)


def _load_artifact():
    return json.loads(OUT_JSON.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------

def _bar(date, time, high, low, close):
    return {"date": date, "time": time, "open": close, "high": high,
            "low": low, "close": close}


# -----------------------------------------------------------------------
# 1. Published constants (verbatim from executable code inspection)
# -----------------------------------------------------------------------

def test_am_windows():
    assert AM_WINDOWS == ["09:30", "09:45", "10:00", "10:15", "10:30"]

def test_pm_windows():
    assert PM_WINDOWS == ["14:00", "14:15", "14:30", "14:45", "15:00"]

def test_best4():
    assert BEST4 == [("AM", "10:15"), ("AM", "10:30"), ("PM", "14:15"), ("PM", "14:45")]

def test_hold_and_range():
    assert HOLD_MINS == 20
    assert HOLD_BARS == 4
    assert MIN_RANGE == 5.0

def test_sl_default():
    assert DEFAULT_SL_MODE == "none"

def test_repo_url():
    assert "Raj1984/nifty-breakout-lab" in REPO_URL


# -----------------------------------------------------------------------
# 2. Data loading and research domain
# -----------------------------------------------------------------------

def test_load_returns_87193_bars():
    bars = load_csv(DATASET)
    assert len(bars) == 87193

def test_first_bar_format():
    bars = load_csv(DATASET)
    b = bars[0]
    assert b["date"] == "2022-01-03"
    assert b["time"] == "09:15"
    assert isinstance(b["open"], float)

def test_research_domain_69781():
    bars = load_csv(DATASET)
    rb = _research_bars(bars)
    assert len(rb) == 69781

def test_research_domain_dates_in_range():
    bars = load_csv(DATASET)
    rb = _research_bars(bars)
    assert rb[0]["date"] >= RESEARCH_START
    assert rb[-1]["date"] <= RESEARCH_END

def test_research_932_days():
    bars = load_csv(DATASET)
    rb = _research_bars(bars)
    assert len(_all_trading_days(rb)) == 932


# -----------------------------------------------------------------------
# 3. Range construction
# -----------------------------------------------------------------------

def test_range_inclusive_of_window_bar():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 101.0, 89.0, 95.0),
        _bar("2024-01-01", "09:25", 102.0, 88.0, 95.0),
        _bar("2024-01-01", "09:30", 99.0, 91.0, 95.0),
        _bar("2024-01-01", "09:35", 103.0, 87.0, 95.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["range_size"] == round(102.0 - 88.0, 2)

def test_range_excludes_post_window_bars():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 101.0, 91.0, 95.0),
        _bar("2024-01-01", "09:30", 99.0, 92.0, 95.0),
        _bar("2024-01-01", "09:35", 200.0, 10.0, 95.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["range_size"] == round(101.0 - 90.0, 2)

def test_min_range_filter_rejects_small_range():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 97.0, 98.0),
        _bar("2024-01-01", "09:20", 100.0, 97.0, 98.0),
        _bar("2024-01-01", "09:35", 101.0, 96.0, 98.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is None

def test_range_fixed_once_formed():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:25", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 200.0, 10.0, 95.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["range_size"] == 10.0


# -----------------------------------------------------------------------
# 4. Breakout detection: LONG priority on same bar
# -----------------------------------------------------------------------

def test_long_priority_if_both_breach():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 101.0, 89.0, 95.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["direction"] == "LONG"

def test_short_when_only_low_breaks():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 99.0, 89.0, 95.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["direction"] == "SHORT"

def test_no_trade_if_no_breakout():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 99.5, 90.5, 95.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is None


# -----------------------------------------------------------------------
# 5. Entry / exit prices and holding period
# -----------------------------------------------------------------------

def test_long_entry_at_range_high():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 105.0, 94.0, 103.0),
        _bar("2024-01-01", "09:40", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:45", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:50", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:55", 104.0, 99.0, 103.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["direction"] == "LONG"
    assert t["entry_price"] == 100.0
    assert t["exit_price"] == 103.0
    assert t["pnl"] == 3.0

def test_short_entry_at_range_low():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 95.0, 85.0, 87.0),
        _bar("2024-01-01", "09:40", 96.0, 86.0, 87.0),
        _bar("2024-01-01", "09:45", 96.0, 86.0, 87.0),
        _bar("2024-01-01", "09:50", 96.0, 86.0, 87.0),
        _bar("2024-01-01", "09:55", 96.0, 86.0, 87.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["direction"] == "SHORT"
    assert t["entry_price"] == 90.0
    assert t["exit_price"] == 87.0
    assert t["pnl"] == 3.0

def test_hold_bars_4_is_20_minutes():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 105.0, 94.0, 96.0),
        _bar("2024-01-01", "09:40", 104.0, 99.0, 97.0),
        _bar("2024-01-01", "09:45", 104.0, 99.0, 98.0),
        _bar("2024-01-01", "09:50", 104.0, 99.0, 99.0),
        _bar("2024-01-01", "09:55", 104.0, 99.0, 100.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["duration_mins"] == HOLD_BARS * 5  # 4 bars x 5 min = 20 min


# -----------------------------------------------------------------------
# 6. Stop-loss behaviour
# -----------------------------------------------------------------------

def test_sl_opposite_boundary():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 105.0, 88.0, 104.0),
        _bar("2024-01-01", "09:40", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:45", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:50", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:55", 104.0, 99.0, 103.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "range")
    assert t is not None
    assert t["sl_hit"] is True
    assert t["pnl"] == -10.0


# -----------------------------------------------------------------------
# 7. Re-entry: one trade per (date, window)
# -----------------------------------------------------------------------

def test_one_trade_per_window_per_day():
    trades = run_strategy([
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 105.0, 89.0, 95.0),
        _bar("2024-01-01", "09:40", 106.0, 88.0, 95.0),
        _bar("2024-01-01", "09:45", 107.0, 87.0, 95.0),
        _bar("2024-01-01", "09:50", 108.0, 86.0, 95.0),
        _bar("2024-01-01", "09:55", 109.0, 85.0, 95.0),
    ], 4, 5.0, "none")
    am0930 = [t for t in trades if t["window"] == "09:30"]
    assert len(am0930) == 1


# -----------------------------------------------------------------------
# 8. Determinism
# -----------------------------------------------------------------------

def test_strategy_determinism():
    bars = load_csv(DATASET)
    rb = _research_bars(bars)
    t1 = run_strategy(rb)
    t2 = run_strategy(rb)
    s1 = hashlib.sha256(json.dumps(t1, sort_keys=True).encode()).hexdigest()
    s2 = hashlib.sha256(json.dumps(t2, sort_keys=True).encode()).hexdigest()
    assert s1 == s2


# -----------------------------------------------------------------------
# 9. Causality: range uses only past bars
# -----------------------------------------------------------------------

def test_causality_no_lookahead():
    bars = [
        _bar("2024-01-01", "09:15", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:20", 100.0, 90.0, 95.0),
        _bar("2024-01-01", "09:35", 105.0, 94.0, 103.0),
        _bar("2024-01-01", "09:40", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:45", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:50", 104.0, 99.0, 103.0),
        _bar("2024-01-01", "09:55", 104.0, 99.0, 103.0),
    ]
    t = _run_one_window(bars, "AM", "09:30", 4, 5.0, "none")
    assert t is not None
    assert t["entry_time"] == "09:35"


# -----------------------------------------------------------------------
# 10. Classification and artifact JSON
# -----------------------------------------------------------------------

def test_artifact_json_fields():
    art = _load_artifact()
    for key in ("source_url", "source_audit", "exact_published_rules",
                "data_compatibility", "strategy_configuration",
                "execution_convention", "cost_convention", "overall",
                "yearly", "monthly", "am_pm", "long_short",
                "causality", "determinism", "integrity_verification",
                "classification", "safety_state"):
        assert key in art

def test_classification_not_profitable():
    art = _load_artifact()
    assert art["classification"] == "REPRODUCIBLE_BUT_NOT_PROFITABLE"

def test_safety_state():
    art = _load_artifact()
    s = art["safety_state"]
    assert s["live_trading"] is False
    assert s["live_gate"] == "CLOSED"
    assert s["algo_ready"] == "NO"
    assert s["algorithm_health"] == "RED"
    assert s["promotion"] is False or s["promotion"] == "NO"

def test_causality_pass():
    art = _load_artifact()
    assert all(art["causality"].values())

def test_determinism_pass():
    art = _load_artifact()
    assert art["determinism"]["identical"] is True

def test_integrity_unchanged():
    art = _load_artifact()
    iv = art["integrity_verification"]
    assert iv["iteration_009_vol_led.json"] == "UNCHANGED"
    assert iv["iteration_012_trade_forensics.json"] == "UNCHANGED"

def test_overall_metrics_present():
    art = _load_artifact()
    o = art["overall"]
    assert o["n"] > 0
    assert "win_rate_pct" in o
    assert "gross_pnl_pts" in o
    assert "net_pnl_pts" in o
    assert "max_drawdown_pts" in o

def test_volume_not_required():
    art = _load_artifact()
    assert art["data_compatibility"]["volume_required_by_strategy"] is False

def test_no_oos_leakage():
    art = _load_artifact()
    assert art["causality"]["no_oos_leakage"] is True