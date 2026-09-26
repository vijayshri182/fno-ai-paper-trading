"""Tests for PSB-001 - PUBLIC STRATEGY BENCHMARK 001 (EMA 8/24/72).

This is an EXTERNAL BENCHMARK.  The tests verify the public-strategy source
audit data, the native (pandas-free) EMA implementation, causality guards,
determinism, and the classification result.  They do NOT touch the protected
internal algorithm (model_0 / Iteration-009..012 / protected OOS).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fno_ai_paper_trading.research.public_strategy_psb001 import (
    CLASSIFICATION,
    DATASET,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    HORIZONS,
    OUT_JSON,
    REPO_URL_ACTUAL,
    REPO_URL_TASK,
    SITE_URL,
    STRUCTURAL_LABEL,
    VOLUME_CONFIRM_RATIO,
    VOLUME_LOOKBACK,
    _signed,
    data_compatibility,
    ema_series,
    ema_stack_flags,
    iso,
    load_csv,
    research_mask,
    source_audit,
    structural_ema_series,
    structural_summary,
    trailing_volume_ratio,
)


def _load_artifact():
    return json.loads(OUT_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Public source constants (published values used verbatim, no tuning)
# ---------------------------------------------------------------------------

def test_published_ema_periods():
    assert (EMA_FAST, EMA_MID, EMA_SLOW) == (8, 24, 72)


def test_published_volume_parameters():
    assert VOLUME_LOOKBACK == 20
    assert VOLUME_CONFIRM_RATIO == 1.3


def test_published_horizons():
    assert HORIZONS == [3, 6, 12, 24]


def test_source_urls_present():
    assert SITE_URL.startswith("https://")
    assert REPO_URL_TASK.startswith("https://")
    assert REPO_URL_ACTUAL.startswith("https://")


# ---------------------------------------------------------------------------
# 2. EMA implementation (classic, seeded from first bar, pandas-free)
# ---------------------------------------------------------------------------

def test_ema_constant_series_converges_to_constant():
    out = ema_series([50.0] * 30, 8)
    assert all(abs(x - 50.0) < 1e-9 for x in out)


def test_ema_span8_first_value_seed():
    out = ema_series([10.0, 20.0, 20.0], 8)
    # ema[0] = 10.0; ema[1] = (2/9)*20 + (7/9)*10; ema[2] = (2/9)*20 + (7/9)*ema[1]
    alpha = 2.0 / 9.0
    e1 = alpha * 20.0 + (1 - alpha) * 10.0
    e2 = alpha * 20.0 + (1 - alpha) * e1
    assert abs(out[0] - 10.0) < 1e-9
    assert abs(out[1] - e1) < 1e-9
    assert abs(out[2] - e2) < 1e-9


def test_ema_respects_order():
    flat = [10.0, 10.0, 10.0, 10.0]
    spike = [10.0, 10.0, 10.0, 100.0]
    e_flat = ema_series(flat, 8)
    e_spike = ema_series(spike, 8)
    # last value of spike series must be higher (no look-ahead noise, causal)
    assert e_spike[-1] > e_flat[-1]


# ---------------------------------------------------------------------------
# 3. Stack flags are STACK FORMATION EVENTS (fresh), not persistent state
# ---------------------------------------------------------------------------

def test_fresh_stack_fires_once_per_contiguous_run():
    # series: bull T,T,T then F then T,T,T -> fresh only at the two run starts
    # (loop begins at index 1, matching shift-based formation semantics).
    e8 = [8.0, 9.0, 10.0, 10.0, 11.0, 12.0, 13.0]
    e24 = [7.0, 7.5, 8.0, 8.0, 9.0, 9.0, 9.0]
    e72 = [6.0, 6.0, 6.0, 8.5, 8.5, 8.5, 8.5]
    bull, bear, fresh_bull, fresh_bear = ema_stack_flags(e8, e24, e72)
    assert bull == [True, True, True, False, True, True, True]
    assert fresh_bull == [False, False, False, False, True, False, False]


def test_bear_and_bull_exclusive():
    e8 = [8.0, 6.0, 9.0, 7.0, 10.0, 9.0, 11.0]
    e24 = [7.0, 7.0, 8.0, 8.0, 9.0, 9.0, 10.0]
    e72 = [6.0, 8.0, 6.0, 9.0, 7.0, 10.0, 8.0]
    bull, bear, _, _ = ema_stack_flags(e8, e24, e72)
    for b, r in zip(bull, bear):
        assert not (b and r)


# ---------------------------------------------------------------------------
# 4. Volume helper is causal (trailing window, current bar k as last elemen)
# ---------------------------------------------------------------------------

def test_volume_ratio_trailing_20():
    # published rule: current bar is the LAST element of the trailing-20 window
    # -> ratio = current_vol / mean(current + previous 19)
    vol = [100.0] * 19 + [200.0, 100.0, 100.0]
    ratio = trailing_volume_ratio(vol)
    # window of 19x100 + 200 -> mean 105; 200/105 = 1.9047 >= 1.3 (confirm)
    mean_peak = (19 * 100.0 + 200.0) / 20.0
    assert abs(ratio[19] - (200.0 / mean_peak)) < 1e-9
    assert ratio[19] >= 1.3 - 1e-9
    # back to normal afterwards (behaviour not persistent)
    assert ratio[20] < 1.3


# ---------------------------------------------------------------------------
# 5. Dataset compatibility: volume is all-zero -> mandatory rule unreproducible
# ---------------------------------------------------------------------------

def test_dataset_volume_is_all_zero():
    data = load_csv(DATASET)
    compat = data_compatibility(data)
    assert compat["volume_all_zero"] is True
    assert compat["volume_nonzero_bars"] == 0


def test_research_domain_is_protected():
    data = load_csv(DATASET)
    mask = research_mask(data["ts"])
    assert sum(mask) == 69781
    assert sum(1 for m in mask if not m) == data_compatibility(data)["oos_bars"]


def test_classification_not_reproducible():
    assert CLASSIFICATION == "NOT_REPRODUCIBLE_WITH_CURRENT_DATA"


# ---------------------------------------------------------------------------
# 6. Structural EMA analysis (PSB-001B) has no future information
# ---------------------------------------------------------------------------

def test_structural_uses_research_domain_only():
    data = load_csv(DATASET)
    analy = structural_ema_series(data)
    assert analy["n_bars"] == 69781
    # every entry timestamp inside research window
    for ev in analy["events_bull"] + analy["events_bear"]:
        t = iso(ev["entry_ts"])
        assert iso("2022-01-03") <= t <= iso("2025-10-03T23:59:59")


def test_structural_is_labeled_not_reproduction():
    assert "NOT THE PUBLISHED STRATEGY" in STRUCTURAL_LABEL
    assert "PSB-001B" in STRUCTURAL_LABEL


def test_structural_summary_shape():
    data = load_csv(DATASET)
    s = structural_summary(structural_ema_series(data))
    assert s["bull_signals"] + s["bear_signals"] == s["total_signals"]
    for side in ("bull", "bear"):
        for h in ("h_3", "h_6", "h_12", "h_24"):
            assert h in s[side]
            assert "win_rate_pct" in s[side][h]


# ---------------------------------------------------------------------------
# 7. Causality audit: no future information in signal construction
# ---------------------------------------------------------------------------

def test_ema_uses_no_future_info():
    # recompute EMA and prove it only depends on prefix
    vals = [10.0, 11.0, 12.0, 11.5, 10.5, 10.0, 11.0, 12.5, 13.0, 12.0]
    e = ema_series(vals, 8)
    prefix = vals[:5]
    e_prefix = ema_series(prefix, 8)
    assert abs(e[4] - e_prefix[4]) < 1e-9


def test_fresh_flags_use_previous_bar_only():
    e8 = [7.0] * 10 + [10.0] * 10
    e24 = [9.0] * 10 + [9.0] * 10
    e72 = [8.0] * 10 + [8.0] * 10
    bull, bear, fresh_bull, fresh_bear = ema_stack_flags(e8, e24, e72)
    # first segment: 7 > 9 is false -> no bull stack; at index 10 the stack
    # forms for the first time -> exactly one fresh_bull event, nothing after
    assert fresh_bull[10] is True
    assert sum(fresh_bull[11:]) == 0


# ---------------------------------------------------------------------------
# 8. Determinism
# ---------------------------------------------------------------------------

def test_structural_determinism():
    data = load_csv(DATASET)
    a = json.dumps(structural_ema_series(data), sort_keys=True, default=str)
    b = json.dumps(structural_ema_series(data), sort_keys=True, default=str)
    assert hashlib.sha256(a.encode("utf-8")).hexdigest() == \
        hashlib.sha256(b.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 9. Artifact JSON completeness & safety state
# ---------------------------------------------------------------------------

def test_artifact_json_fields():
    art = _load_artifact()
    for key in ("source_url", "repository_url", "source_audit", "exact_published_rules",
                "unknown_rules", "conflicts", "timeframe", "instrument",
                "data_compatibility", "volume_availability", "reproduction_status",
                "author_reported_results", "independent_results", "causality",
                "determinism", "limitations", "existing_algorithm_integrity",
                "final_classification", "safety_state"):
        assert key in art


def test_safety_state():
    art = _load_artifact()
    s = art["safety_state"]
    assert s["live_trading"] is False
    assert s["live_gate"] == "CLOSED"
    assert s["algo_ready"] == "NO"
    assert s["algorithm_health"] == "RED"
    assert s["promotion"] == "NO"


def test_author_results_are_labeled_not_independent():
    art = _load_artifact()
    assert "not independently verified" in art["author_reported_results"]["note"].lower()


# ---------------------------------------------------------------------------
# 10. Existing-algorithm integrity (this module must not modify anything)
# ---------------------------------------------------------------------------

def test_module_does_not_overwrite_protected_artifacts():
    import hashlib as _h
    from fno_ai_paper_trading.research import public_strategy_psb001 as m

    iter9 = m.ROOT / "runs/research/day_batch/iteration_009_vol_led.json"
    iter12 = m.ROOT / "runs/research/day_batch/iteration_012_trade_forensics.json"
    assert _h.sha256(iter9.read_bytes()).hexdigest() == \
        "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2"
    assert _h.sha256(iter12.read_bytes()).hexdigest() == \
        "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb"