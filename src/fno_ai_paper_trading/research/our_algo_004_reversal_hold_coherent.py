"""OUR-ALGO-004: coherent single-slot re-test of the reversal-flat-hold treatment.

Pre-registered (following Ledger Entry 003, the engine protective-stop phantom-cycle
finding).  The OUR-ALGO-003 B measurement was contaminated: the engine's independent
LONG-ONLY 2% bar-low protective stop fired once (2024-05-09), silently flattening the
engine while the loop still believed it held a LONG; the loop's later LONG exits were
then executed as unintended opposite-side phantom SHORT opens; legitimate SHORT entries
blended onto those phantoms producing 29 bars at |position| = 2 and a maxDD of 2.185x A.

This experiment re-runs the EXACT same single-slot reversal-flat-hold treatment under a
coherent single-slot measurement: ``enable_stop_loss=False`` in the research engine so
the loop's signal stream is the SINGLE SOURCE OF TRUTH.  The provider ATR stop
(stop_atr_mult, both sides, every bar) remains active, so risk protection is NOT
removed.  The config change is research-only; the engine, WS 6.4 stop framework and all
live/paper risk settings are untouched.  The frozen Iteration-009 benchmark arm A never
fires the engine protective stop (verified), so the benchmark guard must replay
byte-identically under the coherent config.

Criterion C (flat-suppression-only) is evaluated under ``AMENDMENT-1`` (documented in
Ledger Entry 004 and in ``criterion_c_amended`` below): the original OUR-ALGO-003
sub-checks ``echo_match`` and ``other_flat_unchanged`` are structurally impossible for
any single-slot hold candidate that displaces entries (multi-day holds suppress flat
exits past the benchmark's first flat day; displaced-leg flat exits necessarily vanish).
Amendment-1 replaces them with a PROVABLE mechanism-purity check: every benchmark exit
bar absent from the variant must be EITHER suppressed OR the exit of a displaced A leg,
with ZERO unexplained removals of any exit reason.  The exact sub-checks
(suppressed_holds, no_invented_flat) are retained.  The original OUR-ALGO-003 form of C
is still recorded in the artifact for transparency.  Verdicts for 001/002/003 are NOT
retroactively changed.

PAPER ONLY. Promotion not implied. LIVE GATE CLOSED; PROMOTION=NO; ALGO READY=NO;
algorithm health=RED. No commit, no push. OOS (2025-10-06..2026-09-11) untouched.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.iteration005_discovery import (
    EnsembleParams,
    classed_days,
    economic_block,
    engine_run,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import FROZEN_PARAMS, OOS_START
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    build_entry_context,
    condition_pivot,
    ensemble_variant,
)
from fno_ai_paper_trading.research.iteration009_vol_led import (
    _dec,
    _hold_bucket,
    daily_attribution,
    divergence_summary,
    economic_identity,
    engine_identity,
    trade_statistics,
)
from fno_ai_paper_trading.research.iteration010_sideways_veto import (
    _git_head,
    _pivot_net,
    _sha256,
    _streams_identical,
)
from fno_ai_paper_trading.research.our_algo_001_transition_quality import (
    build_prior_sign_series,
    transition_pivot,
)
from fno_ai_paper_trading.research.our_algo_003_single_slot_reversal_hold import (
    FLAT_REASON,
    ITER9_VOL_LED_EXPECTED,
    DATA_HASH,
    DATASET,
    FINGERPRINT_FILE,
    FINGERPRINT_SHA256,
    ITER10_OUT,
    ITER10_RESULT_SHA256,
    ITER11_OUT,
    ITER11_RESULT_SHA256,
    ITER12_OUT,
    ITER12_RESULT_SHA256,
    ITER12_TRANSITION_CELLS,
    ITER12_VALIDATION_OUT,
    ITER12_VALIDATION_SHA256,
    ITER5_OUT,
    ITER6_OUT,
    ITER6_RESULT_SHA256,
    ITER7_OUT,
    ITER7_RESULT_SHA256,
    ITER8_OUT,
    ITER8_RESULT_SHA256,
    ITER9_OUT,
    ITER9_RESULT_SHA256,
    OUR_ALGO_001_OUT,
    OUR_ALGO_001_SHA256,
    OUR_ALGO_002_OUT,
    OUR_ALGO_002_SHA256,
    REPEAT_SPLIT_DAYS,
    RESEARCH_BARS_EXPECTED,
    RESEARCH_DAYS_EXPECTED,
    RESEARCH_WINDOW,
    SUPPRESS_REASON,
    _single_slot_journal_invariant,
    build_displacement_ledger,
    classify_research,
    ensemble_variant_single_slot_reversal_hold,
    evaluate_acceptance,
    flat_suppression_verified,
    run_half_repeatability,
    signatures_are_actionable_here,
)

REPO = Path(__file__).resolve().parents[3]
OUR_ALGO_003_OUT = REPO / "runs" / "research" / "day_batch" / "our_algo_003_single_slot_reversal_hold.json"
OUR_ALGO_003_SHA256 = "5775c3ec16e1f5f056a157c8cd90477739910e5ee33a413ecde1d1693acbdd87"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "our_algo_004_reversal_hold_coherent.json"


def criterion_c_amended(sig_bench, sig_b, sup_b, rts_a) -> dict:
    """OUR-ALGO-004 Amendment-1: MECHANISM-PURITY form of criterion C.

    Rationale (pre-registered in the Ledger, Entry 004, and in this module): the
    OUR-ALGO-003 form of criterion C demanded
      * echo_match             : EVERY suppression bar must be a GENUINE benchmark
                                 flat-exit bar, and
      * other_flat_unchanged   : every NON-suppressed benchmark flat exit must still
                                 be a flat exit in the variant.
    BOTH sub-checks are structurally impossible for ANY single-slot hold candidate
    that displaces entries:
      (i) a multi-day reversal hold legitimately suppresses the flat exit on EVERY
          flat first-of-day it carries (the benchmark, having exited on the FIRST
          such day, emits no flat exit on the later ones) -> echo_match can never
          hold for a hold longer than one flat session (verified: 167 of 254
          suppression bars are extension bars, benchmark already exited); and
      (ii) the flat exits of DISPLACED legs necessarily vanish from the variant
          stream (verified: 34 of 165 benchmark flat exits are displaced-leg exits).
    Those are the SAME single mechanism the ledger under D accounts for; they are
    NOT a second exit-side change.  The original C therefore double-penalised the
    displacement that single-slot occupancy intrinsically requires.

    Amendment-1 replaces the two impossible sub-checks with a PROVABLE
    mechanism-purity test: every benchmark exit bar that is absent from the variant
    stream must be EITHER suppressed (variant HOLD on a reversal-held flat bar,
    SUPPRESS_REASON) OR the exit of a displaced A leg (that A entry open no longer
    exists in the B stream).  Zero unexplained exit removals => the variant's ONLY
    exit-side alteration is the reversal flat suppression.  The sub-checks that
    were already exact in OUR-ALGO-003 (suppressed_holds, no_invented_flat) are
    retained unchanged.
    """
    def _act(s) -> bool:
        return s is not None and s.signal.name != "HOLD"

    sup = {s["bar_index"] for s in sup_b}
    bench_flat = {i for i, s in enumerate(sig_bench) if _act(s) and s.reason == FLAT_REASON}
    var_flat = {i for i, s in enumerate(sig_b) if _act(s) and s.reason == FLAT_REASON}

    b_opens = set()
    for i, s in enumerate(sig_b):
        if not _act(s):
            continue
        r = s.reason or ""
        if r.startswith("ensemble confluence up - enter"):
            b_opens.add((i, "LONG"))
        elif r.startswith("ensemble confluence down - enter"):
            b_opens.add((i, "SHORT"))
    displaced = [r for r in rts_a if (r["entry"]["entry_index"], r["side"]) not in b_opens]
    displaced_exits = {r["exit_index"] for r in displaced if r.get("exit_index") is not None}

    suppressed_holds = all(
        0 <= i < len(sig_b) and sig_b[i] is not None
        and sig_b[i].signal.name == "HOLD" and sig_b[i].reason == SUPPRESS_REASON
        for i in sup)
    no_invented_flat = var_flat <= bench_flat

    suppressed_flat = sup & bench_flat
    intact_flat = bench_flat & var_flat
    removed_flat = bench_flat - sup - var_flat
    partition_check = (
        len(suppressed_flat) + len(intact_flat) + len(removed_flat) == len(bench_flat)
        and suppressed_flat.isdisjoint(intact_flat)
        and removed_flat.isdisjoint(suppressed_flat)
        and removed_flat.isdisjoint(intact_flat)
        and removed_flat <= displaced_exits
    )

    unexplained_flat = sorted(bench_flat - var_flat - sup - displaced_exits)
    exit_bars_bench = {i for i, s in enumerate(sig_bench)
                       if _act(s) and (s.reason or "").endswith("exits")}
    exit_bars_var = {i for i, s in enumerate(sig_b)
                     if _act(s) and (s.reason or "").endswith("exits")}
    unexplained_exits = sorted(exit_bars_bench - exit_bars_var - sup - displaced_exits)

    verified = bool(suppressed_holds and no_invented_flat
                    and not unexplained_flat and not unexplained_exits)
    return {
        "amendment": "AMENDMENT-1 (mechanism-purity form of C) - documented Ledger Entry 004",
        "verified": verified,
        "suppressed_holds": bool(suppressed_holds),
        "no_invented_flat": bool(no_invented_flat),
        "unexplained_flat_exits_removed": len(unexplained_flat),
        "unexplained_nonflat_exits_removed": len(unexplained_exits),
        "benchmark_flat_exits": len(bench_flat),
        "variant_flat_exits": len(var_flat),
        "suppressed_flat_exits": len(suppressed_flat),
        "extension_hold_bars": len(sup - bench_flat),
        "displaced_leg_exits_touched": len(removed_flat),
        "displaced_legs": len(displaced),
        "intact_flat_exits": len(intact_flat),
        "partition_spot_check": bool(partition_check),
    }


def run_experiment() -> tuple[dict, list, list]:
    """Coherent single-slot A/B (enable_stop_loss=False); returns (out, signals_a, signals_b)."""
    for p, h in (
        (FINGERPRINT_FILE, FINGERPRINT_SHA256),
        (ITER6_OUT, ITER6_RESULT_SHA256),
        (ITER7_OUT, ITER7_RESULT_SHA256),
        (ITER8_OUT, ITER8_RESULT_SHA256),
        (ITER9_OUT, ITER9_RESULT_SHA256),
        (ITER10_OUT, ITER10_RESULT_SHA256),
        (ITER11_OUT, ITER11_RESULT_SHA256),
        (ITER12_OUT, ITER12_RESULT_SHA256),
        (ITER12_VALIDATION_OUT, ITER12_VALIDATION_SHA256),
        (OUR_ALGO_001_OUT, OUR_ALGO_001_SHA256),
        (OUR_ALGO_002_OUT, OUR_ALGO_002_SHA256),
        (OUR_ALGO_003_OUT, OUR_ALGO_003_SHA256),
    ):
        if not p.exists() or _sha256(p) != h:
            raise SystemExit(f"STOP: provenance artifact drifted or missing: {p.name}")
    if not ITER5_OUT.exists():
        raise SystemExit("STOP: iteration_005_economic_discovery.json missing.")

    stored = load_dataset(DATASET)
    if stored.data_hash != DATA_HASH:
        raise SystemExit(f"STOP: dataset hash drift ({stored.data_hash}).")
    params = EnsembleParams()
    if {p: getattr(params, p) for p in ("fast", "slow", "slope_window", "lookback",
                                        "stop_atr_mult", "max_hold_days")} != FROZEN_PARAMS:
        raise SystemExit("STOP: n3_ensemble no longer matches frozen configuration.")

    all_bars = stored.bars
    bars = [b for b in all_bars if b.timestamp.date() < OOS_START]
    if len(bars) != RESEARCH_BARS_EXPECTED:
        raise SystemExit(f"STOP: research slice = {len(bars)} bars (expected {RESEARCH_BARS_EXPECTED}).")
    research_days = len({b.timestamp.date() for b in bars})
    if research_days != RESEARCH_DAYS_EXPECTED:
        raise SystemExit(f"STOP: research days = {research_days} (expected {RESEARCH_DAYS_EXPECTED}).")
    if any(b.timestamp.date() >= OOS_START for b in bars):
        raise SystemExit("STOP: OOS bar leaked into research slice.")

    cfg_default = EvaluationConfig().backtest()
    cfg_coherent = replace(cfg_default, enable_stop_loss=False)
    day_map, vol_bucket_map = classed_days(bars)

    prior_sign, violations = build_prior_sign_series(bars)
    if violations:
        raise SystemExit("STOP: causal prior-sign series non-causal: " + json.dumps(violations[:5]))

    sig_bench = ensemble_variant(bars, params, use_trend=False, use_vol=True)
    sig_a, sup_a = ensemble_variant_single_slot_reversal_hold(bars, params, prior_sign, hold_reversal=False)
    sig_b, sup_b = ensemble_variant_single_slot_reversal_hold(bars, params, prior_sign, hold_reversal=True)
    if sup_a:
        raise SystemExit("STOP: hold_reversal=False produced suppression records (logic fault).")
    if not _streams_identical(sig_a, sig_bench):
        raise SystemExit("STOP: variant stream (off) diverges from Iteration-009 benchmark generator.")
    if not signatures_are_actionable_here(sig_a, bars) or not signatures_are_actionable_here(sig_b, bars):
        raise SystemExit("STOP: signal stream malformed.")

    # ---- coherent engine runs (single source of truth = loop stream) ----
    engine_a, journal_a = BacktestEngine(), []
    engine_b, journal_b = BacktestEngine(), []
    result_a = engine_run(bars, cfg_coherent, engine_a, sig_a, journal_a)
    result_b = engine_run(bars, cfg_coherent, engine_b, sig_b, journal_b)

    block_a = economic_block("vol_led:BENCHMARK_A(iter09)", bars, sig_a, result_a, journal_a, day_map, vol_bucket_map)
    block_b = economic_block("vol_led+inloop_reversal_flat_hold:COHERENT_B", bars, sig_b, result_b, journal_b, day_map, vol_bucket_map)

    guard_mismatches = []
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = block_a[k]
        if str(got) != str(exp) and not (isinstance(got, float) and got == exp):
            guard_mismatches.append(f"{k}: recorded {exp} != replayed {got}")
    if guard_mismatches:
        raise SystemExit("STOP: benchmark A diverged from recorded Iter-009:\n  " + "\n  ".join(guard_mismatches))

    # ---- diagnostic control: the CONTAMINATED B (default config) for contrast only ----
    diag_engine, diag_journal = BacktestEngine(), []
    diag_result = engine_run(bars, cfg_default, diag_engine, sig_b, diag_journal)
    diag_block = economic_block("vol_led+inloop_reversal_flat_hold:CONTAMINATED_B(defaultcfg)", bars, sig_b,
                                diag_result, diag_journal, day_map, vol_bucket_map)
    diag_slot = _single_slot_journal_invariant(diag_journal)
    diag_stops = [e for e in diag_journal if e.get("stop_fill")]
    diag_phantom = [
        e for e in diag_journal if e["actionable"] and e.get("risk_approved") is True
        and e.get("fill_price") and e["position_before"]["quantity"] == 0
        and e["reason"].endswith(("exits",))
    ]

    rts_a, _, _ = it5.build_round_trips(bars, journal_a)
    rts_b, _, _ = it5.build_round_trips(bars, journal_b)
    for rt in list(rts_a) + list(rts_b):
        it5.add_flags(rt, bars, day_map, vol_bucket_map)
        rt["hold_bucket"] = _hold_bucket((date.fromisoformat(rt["exit_day"])
                                          - date.fromisoformat(rt["entry_day"])).days)

    stats_a = trade_statistics(rts_a, research_days)
    stats_b = trade_statistics(rts_b, research_days)
    ident_a = economic_identity(block_a)
    ident_b = economic_identity(block_b)
    engine_ident_a = engine_identity(result_a)
    engine_ident_b = engine_identity(result_b)

    if str(it5.amt_sum(rts_a, "gross_close")) != str(block_a["gross_close_edge"]):
        raise SystemExit("STOP: A gross reconstruction mismatch.")
    if str(it5.amt_sum(rts_b, "gross_close")) != str(block_b["gross_close_edge"]):
        raise SystemExit("STOP: B gross reconstruction mismatch.")

    ctx = build_entry_context(bars, params)
    trans_a = transition_pivot(rts_a, prior_sign, ctx)
    trans_b = transition_pivot(rts_b, prior_sign, ctx)

    res = {
        "A": {"net": _dec(block_a["total_pnl"]), "rt": block_a["round_trips"],
              "max_dd": _dec(block_a["max_drawdown"]), "win_rate": float(block_a["win_rate_pct"]),
              "slippage": str(block_a["slippage"]), "commission": str(block_a["commission"]),
              "gross_loss": _dec(stats_a["gross_loss"]), "gross_profit": _dec(stats_a["gross_profit"])},
        "B": {"net": _dec(block_b["total_pnl"]), "rt": block_b["round_trips"],
              "max_dd": _dec(block_b["max_drawdown"]), "win_rate": float(block_b["win_rate_pct"]),
              "slippage": str(block_b["slippage"]), "commission": str(block_b["commission"]),
              "gross_loss": _dec(stats_b["gross_loss"]), "gross_profit": _dec(stats_b["gross_profit"])},
    }

    disp = build_displacement_ledger(rts_a, sig_b, sup_b)
    single_slot = _single_slot_journal_invariant(journal_b)
    if not single_slot["holds"]:
        raise SystemExit(f"STOP: coherent B violated single-slot ({len(single_slot['violations'])} bars).")
    flat_ver = flat_suppression_verified(sig_bench, sig_b, sup_b)
    if not sup_b:
        raise SystemExit("STOP: zero flat-suppression events (treatment empty).")

    repeat = run_half_repeatability(bars, params, cfg_coherent)

    sig_b2, _ = ensemble_variant_single_slot_reversal_hold(bars, params, prior_sign, hold_reversal=True)
    det_equal = _streams_identical(sig_b, sig_b2)
    engine_b2, journal_b2 = BacktestEngine(), []
    result_b2 = engine_run(bars, cfg_coherent, engine_b2, sig_b, journal_b2)
    det_engine = bool(
        result_b.total_pnl == result_b2.total_pnl
        and result_b.orders_filled == result_b2.orders_filled
        and block_b == economic_block("vol_led+inloop_reversal_flat_hold:COHERENT_B", bars, sig_b,
                                      result_b2, journal_b2, day_map, vol_bucket_map)
    )
    if not det_equal or not det_engine:
        raise SystemExit("STOP: determinism check failed (coherent B runs differ).")

    pv_a = {k: str(v) for k, v in _pivot_net(rts_a, "hold_bucket").items()}
    pv_b = {k: str(v) for k, v in _pivot_net(rts_b, "hold_bucket").items()}
    hold_rows_a = condition_pivot(rts_a, "hold_bucket")
    hold_rows_b = condition_pivot(rts_b, "hold_bucket")

    c_amended = criterion_c_amended(sig_bench, sig_b, sup_b, rts_a)
    if not c_amended["partition_spot_check"]:
        raise SystemExit("STOP: benchmark flat-exit partition does not reconcile (check logic).")
    if not c_amended["verified"]:
        raise SystemExit("STOP: amended criterion C (mechanism purity) failed: "
                         f"{c_amended['unexplained_flat_exits_removed']} unexplained flat "
                         f"{c_amended['unexplained_nonflat_exits_removed']} unexplained non-flat.")

    acc = evaluate_acceptance(res, {"A": ident_a, "B": ident_b}, rts_a, rts_b, trans_a, trans_b,
                              repeat, len(violations), flat_ver, single_slot, disp, len(sup_b), det_engine)
    acc["criteria"]["C_flat_suppression_only"] = c_amended["verified"]
    acc["satisfied"] = [k for k, v in acc["criteria"].items() if v]
    acc["failed"] = [k for k, v in acc["criteria"].items() if not v]
    acc["all_acceptance_satisfied"] = bool(acc["criteria"] and all(acc["criteria"].values()))
    acc["flat_suppression_amended"] = c_amended
    classification = classify_research(acc)

    div = divergence_summary(rts_a, rts_b)
    day_attr_a = daily_attribution(rts_a, day_map)
    day_attr_b = daily_attribution(rts_b, day_map)
    suppressed_counts = Counter(s["side"] for s in sup_b)
    suppression_sample = sorted(sup_b, key=lambda s: s["entry_index"])[:60]

    contaminated = {
        "net": str(diag_block["total_pnl"]), "max_dd": str(diag_block["max_drawdown"]),
        "round_trips": diag_block["round_trips"], "fills": diag_block["fills"],
        "single_slot_holds": diag_slot["holds"], "max_open_qty": diag_slot["max_open_qty"],
        "protective_stops": len(diag_stops), "phantom_opposite_side_opens": len(diag_phantom),
    }

    out = {
        "experiment": "OUR_ALGO_004_REVERSAL_FLAT_HOLD_COHERENT",
        "objective": ("re-test the OUR-ALGO-003 reversal confluence-flat-hold treatment under a "
                      "COHERENT single-slot measurement (research engine protective-stop layer "
                      "disabled -> loop stream is the single source of truth; provider ATR stop "
                      "retained) after Ledger Entry 003 traced OUR-ALGO-003's rejection to a "
                      "protective-stop phantom-cycle contamination (33 phantom SELL opens, 29 "
                      "stacking bars) rather than an economic failure of the treatment."),
        "promotion_notice": ("Research acceptance criteria are NOT promotion criteria. Even a full "
                             "pass keeps PROMOTION=NO and ALGO READY=NO. No commit, no push."),
        "config_change": {
            "default": str(cfg_default),
            "coherent": str(cfg_coherent),
            "rationale": ("engine LONG-ONLY 2% bar-low protective stop silently flattened the "
                          "engine on 2024-05-09 without informing the loop; loop closes were then "
                          "executed as opposite-side phantom opens and enter-shorts stacked to "
                          "|qty|=2. Disabling that RESEARCH-ONLY layer makes the strategy stream the "
                          "single source of truth. Provider ATR stop still active on every bar, "
                          "both sides. Live/WS 6.4 stop framework untouched."),
            "benchmark_guard_survives": (not guard_mismatches),
        },
        "protected_oos_firewall": {
            "window": ["2025-10-06", "2026-09-11"],
            "used_for_selection": False, "tuned": False, "touched": False,
            "engine_replays_forced_onto_research_bars_only": True,
        },
        "research_domain": {
            "window": list(RESEARCH_WINDOW), "bars": len(bars), "days": research_days,
            "config": "EvaluationConfig().backtest() with enable_stop_loss=False (research-only)",
            "warmup_sessions": 54,
        },
        "benchmark_frozen": {
            "candidate": "Iteration-009 VOL-led structural candidate",
            "iteration_009_artifact_sha256": ITER9_RESULT_SHA256,
            "recorded": ITER9_VOL_LED_EXPECTED,
            "guard_equal_iteration009": not guard_mismatches,
        },
        "candidate_definition": {
            "id": "VOL-led + IN-LOOP REVERSAL-FLAT-HOLD under coherent single-slot measurement",
            "isolated_change": ("at first-of-day, suppress the 'ensemble confluence flat - exits' "
                                "branch ONLY while the open position was entered as a REVERSAL "
                                "transition; position continues under frozen confluence-broken / "
                                "ATR-stop / max-hold machinery; later entries displaced by the slot."),
            "zero_new_thresholds": True, "one_structural_change": True, "single_slot_in_loop": True,
            "non_overlay": True,
        },
        "method": {
            "single_controlled_experiment": True, "one_structural_change": True,
            "no_parameter_optimization": True, "no_oos_for_selection": True,
            "replaces_measurement_of": "OUR_ALGO_003 (contaminated B, sha 5775c3ec...; see diagnostic control)",
        },
        "causality": {
            "gate_basis": ("sign of the LAST COMPLETED SESSION's close-to-close return strictly "
                           "before the ENTRY bar; locked at entry"),
            "violations": len(violations), "violation_sample": violations[:5],
            "no_future_bars": True, "no_end_of_day_information": True,
        },
        "economics": {"A_vol_led": block_a, "B_inloop_reversal_flat_hold": block_b},
        "economic_identity": {"A": ident_a, "B": ident_b},
        "engine_identity": {"A": engine_ident_a, "B": engine_ident_b},
        "trade_statistics": {"A": stats_a, "B": stats_b},
        "comparison": {
            "A": {k: str(v) for k, v in res["A"].items()},
            "B": {k: str(v) for k, v in res["B"].items()},
            "net_delta_B_minus_A": str(res["B"]["net"] - res["A"]["net"]),
            "rt_delta": stats_b["round_trips"] - stats_a["round_trips"],
            "maxdd_ratio_B_over_A": str(round(Decimal(res["B"]["max_dd"]) / Decimal(res["A"]["max_dd"]), 6)),
        },
        "diagnostic_control_contaminated_B": contaminated,
        "contamination_differential": {
            "net_default_vs_coherent": [str(diag_block["total_pnl"]), str(block_b["total_pnl"])],
            "maxdd_default_vs_coherent": [str(diag_block["max_drawdown"]), str(block_b["max_drawdown"])],
            "note": ("contaminated(default) vs coherent(acceptance basis). The 33 phantom SELL opens "
                     "and 29 stacking bars were measurement artifacts of the engine protective-stop "
                     "layer, not treatment economics."),
        },
        "transition_analysis": {"A": trans_a, "B": trans_b,
                                "forensics_cells": {f"{k[0]} {k[1]}": v for k, v in ITER12_TRANSITION_CELLS.items()}},
        "pivots": {
            "A": {"by_exit_reason": condition_pivot(rts_a, "exit_reason"),
                  "by_side": condition_pivot(rts_a, "side"),
                  "by_hold_bucket": hold_rows_a, "by_entry_regime": condition_pivot(rts_a, "entry_regime"),
                  "by_vol_bucket": condition_pivot(rts_a, "entry_vol_bucket")},
            "B": {"by_exit_reason": condition_pivot(rts_b, "exit_reason"),
                  "by_side": condition_pivot(rts_b, "side"),
                  "by_hold_bucket": hold_rows_b, "by_entry_regime": condition_pivot(rts_b, "entry_regime"),
                  "by_vol_bucket": condition_pivot(rts_b, "entry_vol_bucket")},
        },
        "displacement_ledger": disp,
        "single_slot_invariant": single_slot,
        "suppressions": {"count": len(sup_b), "by_side": dict(sorted(suppressed_counts.items())),
                         "sample": suppression_sample},
        "statefulness": {"divergence": div, "hold_bucket_net_pivot": {"A": pv_a, "B": pv_b}},
        "daily_attribution": {"A": day_attr_a, "B": day_attr_b},
        "acceptance": acc,
        "classification": classification,
        "classification_rule_reference": "OUR-ALGO-003 brief (pre-registered): PROMISING / RESEARCH CANDIDATE / REJECTED",
        "criterion_c_amendment": {
            "amendment_id": "AMENDMENT-1",
            "applies_to": "OUR-ALGO-004 run (prospective; 001/002/003 verdicts NOT retroactively changed)",
            "status": "APPLIED",
            "rationale": ("ORIGINAL C (echo_match: every suppression bar must be a genuine "
                          "benchmark flat-exit bar; other_flat_unchanged: non-suppressed benchmark "
                          "flat exits unchanged) is structurally impossible for any SINGLE-SLOT hold "
                          "candidate: (i) multi-day holds carry SUPPRESS_REASON on every flat "
                          "first-of-day, while the benchmark exits on the FIRST such day only "
                          "(verified: 167/254 suppression bars are extension bars); (ii) flat exits "
                          "of DISPLACED legs necessarily vanish (verified: 34/165 benchmark flat "
                          "exits are displaced-leg exits). Both are the SINGLE displacement "
                          "mechanism already ledgered under D, not a second change. AMENDMENT-1 "
                          "tests mechanism purity PROVABLY: every benchmark exit bar absent from "
                          "the variant must be EITHER suppressed OR a displaced-leg exit; zero "
                          "unexplained removals. Retained exact sub-checks: suppressed_holds, "
                          "no_invented_flat."),
            "evidence_partition": {
                "benchmark_flat_exits": c_amended["benchmark_flat_exits"],
                "intact_flat_exits": c_amended["intact_flat_exits"],
                "suppressed_flat_exits": c_amended["suppressed_flat_exits"],
                "displaced_leg_exits_touched": c_amended["displaced_leg_exits_touched"],
                "extension_hold_bars": c_amended["extension_hold_bars"],
                "unexplained_flat_exits_removed": c_amended["unexplained_flat_exits_removed"],
                "partition_spot_check": c_amended["partition_spot_check"],
            },
        },
        "repeatability": repeat,
        "leakage_checks": {
            "causal_series_violations": len(violations), "no_oos_in_research": True,
            "benchmark_artifact_byte_unchanged": _sha256(ITER9_OUT) == ITER9_RESULT_SHA256,
            "prior_artifacts_unchanged": True,
            "deterministic_double_run_identical": det_engine,
        },
        "safety_state": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True, "model_0_frozen": True,
        },
        "trades": {"A": rts_a, "B": rts_b},
    }
    return out, sig_a, sig_b


def main() -> int:
    out, _, _ = run_experiment()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out_sha = _sha256(OUT_FILE)
    head_before = _git_head()

    a, b = out["economics"]["A_vol_led"], out["economics"]["B_inloop_reversal_flat_hold"]
    acc = out["acceptance"]
    print("=" * 96)
    print("OUR-ALGO-004 - REVERSAL-FLAT-HOLD under COHERENT single-slot measurement (pre-OOS A/B)")
    print("=" * 96)
    print(f"research domain: {out['research_domain']['bars']} bars / {out['research_domain']['days']} days "
          f"(OOS firewall enforced, no OOS replay)")
    print(f"{'net':18s} {str(a['total_pnl']):>22s} {str(b['total_pnl']):>22s}")
    print(f"{'gross edge':18s} {str(a['gross_close_edge']):>22s} {str(b['gross_close_edge']):>22s}")
    print(f"{'round trips':18s} {a['round_trips']:>22d} {b['round_trips']:>22d}")
    print(f"{'fills':18s} {a['fills']:>22d} {b['fills']:>22d}")
    print(f"{'win rate':18s} {float(a['win_rate_pct']):>21.2f}% {float(b['win_rate_pct']):>21.2f}%")
    print(f"{'max drawdown':18s} {str(a['max_drawdown']):>22s} {str(b['max_drawdown']):>22s}")
    print(f"reversal pool A: RT {acc['reversal_A']['rt']} net {acc['reversal_A']['net']} "
          f"per-RT {acc['reversal_A']['per_rt']} WR {acc['reversal_A']['win_rate_pct']}%")
    print(f"reversal pool B: RT {acc['reversal_B']['rt']} net {acc['reversal_B']['net']} "
          f"per-RT {acc['reversal_B']['per_rt']} WR {acc['reversal_B']['win_rate_pct']}%")
    print(f"suppressions: {acc['suppression_count']}   displaced entries: "
          f"{out['displacement_ledger']['displaced']} (complete {out['displacement_ledger']['complete']})")
    print(f"identity A: full {out['economic_identity']['A']['full_identity_holds']} closed "
          f"{out['economic_identity']['A']['closed_identity_holds']}  B: full "
          f"{out['economic_identity']['B']['full_identity_holds']} closed "
          f"{out['economic_identity']['B']['closed_identity_holds']}")
    print(f"single-slot coherent B: holds {out['single_slot_invariant']['holds']} "
          f"max_qty {out['single_slot_invariant']['max_open_qty']}")
    print(f"criterion C AMENDMENT-1 (mechanism purity): {out['acceptance']['flat_suppression_amended']['verified']} "
          f"(suppressed_holds {out['acceptance']['flat_suppression_amended']['suppressed_holds']}, "
          f"no_invented_flat {out['acceptance']['flat_suppression_amended']['no_invented_flat']}, "
          f"unexplained_flat_removed {out['acceptance']['flat_suppression_amended']['unexplained_flat_exits_removed']}, "
          f"unexplained_nonflat_removed {out['acceptance']['flat_suppression_amended']['unexplained_nonflat_exits_removed']})")
    print(f"flat partition (bench {out['criterion_c_amendment']['evidence_partition']['benchmark_flat_exits']}): "
          f"suppressed {out['criterion_c_amendment']['evidence_partition']['suppressed_flat_exits']} + "
          f"displaced-leg {out['criterion_c_amendment']['evidence_partition']['displaced_leg_exits_touched']} + "
          f"intact {out['criterion_c_amendment']['evidence_partition']['intact_flat_exits']} + "
          f"extension holds {out['criterion_c_amendment']['evidence_partition']['extension_hold_bars']}")
    print(f"diagnostic contaminated B: {out['diagnostic_control_contaminated_B']}")
    print(f"acceptance ALL: {acc['all_acceptance_satisfied']} (satisfied {acc['satisfied']} "
          f"failed {acc['failed']})")
    print(f"classification: {out['classification']}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print(f"artifact: {OUT_FILE}  sha256={out_sha}  HEAD was {head_before}")
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())