"""ITERATION 009 - VOL-LED STRUCTURAL HYPOTHESIS (pre-OOS A/B, research domain only).

Single controlled research experiment.  Question: can the Iteration-008
attribution finding (VOL_GATE positive marginal; trend/slope gates negative
marginal on the protected OOS) be converted into a meaningful structural
relationship on the PRE-OOS research domain?

EXPERIMENT (exactly one structural change, no parameter tuning):
  A = frozen n3_ensemble benchmark (Iteration-005/006, fingerprint
      5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e).
  B = VOL-LED structural candidate: underlying confirmed-direction expansion
      signal (volmove +/-1) + VOL_GATE -> entry; trend and slope gating
      REMOVED from the entry confluence.  Every other element is preserved
      byte-for-byte: signal-machine loop, daily-series primitives, exits
      (provider ATR stop, max-hold, confluence-flat), position sizing, costs,
      execution model, causal state handling, warmup treatment (54 sessions),
      capital/commission/slippage/risk.

FIREWALL: the protected OOS window (2025-10-06 .. 2026-09-11) is NOT used for
selection, tuning, threshold choice, or architecture choice.  Candidate B is
NOT a parameter or threshold change of A; it is the externally specified
structural hypothesis from the Iter-009 brief.  Engine replays run ONLY on the
pre-OOS research domain (2022-01-03 .. 2025-10-03, 69,781 bars / 932 days).
Hard assertions abort with STOP if any OOS bar would be touched.

Classification (pre-registered): PROMISING / NOT_SUPPORTED / INCONCLUSIVE.
No parameter sweep, no OOS use, no promotion, no ALGO READY, no commit/push.
"""
from __future__ import annotations

import hashlib
import json
import statistics as st
from datetime import date as _date
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.iteration005_discovery import (
    EnsembleParams,
    build_round_trips,
    classed_days,
    economic_block,
    engine_run,
    ensemble_signals,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import (
    FROZEN_PARAMS,
    OOS_START,
    streak_stats,
)
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    build_entry_context as it8_build_entry_context,
    condition_pivot,
    ensemble_variant,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
ITER7_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"
ITER8_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"
ITER5_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_005_economic_discovery.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_009_vol_led.json"

DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"
ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
ITER7_RESULT_SHA256 = "b21171123491945ed8316e57cfffde6c71e29523224121a503b22b5fe779c8ca"
ITER8_RESULT_SHA256 = "52e5ee8e5edc3caca3450d341e9d04416295f5d0993ce0d8b54e2c326cf0eca5"

RESEARCH_WINDOW = ("2022-01-03", "2025-10-03")
RESEARCH_BARS_EXPECTED = 69781
RESEARCH_DAYS_EXPECTED = 932

# Iteration-005 recorded n3_ensemble research economics (benchmark guard).
ITER5_N3_EXPECTED = {
    "round_trips": 108, "fills": 216, "wins": 65, "losses": 43,
    "win_rate_pct": 60.19, "total_pnl": "6648.798750230",
    "gross_close_edge": "12534.10000",
    "slippage": "4527.15770", "commission": "1358.143549770",
    "max_drawdown": "978.320204825", "carry_share_pct": 100.0,
}

# Pre-registered candidate classification (documented thresholds).
CLASSIFY_RULES = {
    "promising_min_net_ratio": "candidate net >= 1.15 x benchmark net",
    "promising_coverage": "candidate coverage >= benchmark coverage",
    "promising_dd": "candidate maxDD <= 2 x benchmark maxDD",
    "promising_winrate_floor": "candidate win rate >= benchmark win rate - 15pp",
    "promising_loss_cap": "candidate gross loss <= 2 x benchmark gross loss",
    "promising_pf_floor": "candidate profit factor >= 0.8 x benchmark profit factor",
    "not_supported_net": "candidate net <= benchmark net",
    "not_supported_loss": "candidate gross loss >= 2.5 x benchmark gross loss",
    "not_supported_dd": "candidate maxDD >= 3 x benchmark maxDD",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def classify_candidate(res: dict) -> str:
    """Pre-registered classification on research-domain economics."""
    a, b = res["A"], res["B"]
    b_pf = _dec(b.get("pf") or 0)
    a_pf = _dec(a.get("pf") or 0)
    if (b["net"] <= a["net"]
            or _dec(b["gross_loss"]) >= _dec(a["gross_loss"]) * Decimal("2.5")
            or _dec(b["max_dd"]) >= _dec(a["max_dd"]) * Decimal("3")):
        return "NOT_SUPPORTED"
    if (b["net"] >= a["net"] * Decimal("1.15")
            and b["coverage"] >= a["coverage"]
            and _dec(b["max_dd"]) <= _dec(a["max_dd"]) * Decimal("2")
            and b["win_rate"] >= a["win_rate"] - 15.0
            and _dec(b["gross_loss"]) <= _dec(a["gross_loss"]) * Decimal("2")
            and b_pf >= a_pf * Decimal("0.8")):
        return "PROMISING"
    return "INCONCLUSIVE"


def engine_identity(result) -> dict:
    """Engine-authoritative identity record (mandate 10).

    Engine accounting: ``realized_pnl`` includes slippage but excludes
    commission, so ``total_pnl == gross_profit + gross_loss - commission``.
    """
    realized_sum = result.gross_profit + result.gross_loss
    resid = result.total_pnl - realized_sum
    return {
        "basis": "engine_authoritative",
        "gross_profit_incl_slip": str(result.gross_profit),
        "gross_loss_incl_slip": str(result.gross_loss),
        "slippage_cost": str(result.slippage_cost),
        "total_commission": str(result.total_commission),
        "transaction_costs": str(result.transaction_costs),
        "total_pnl": str(result.total_pnl),
        "identity_total_pnl_eq_sum_minus_commission": str(resid == -result.total_commission),
        "residual_total_pnl_minus_sum": str(resid),
        "reconstruction_must_agree": ("journal identity is authoritative when reconcile_all is True and "
                                      "journal identity holds; otherwise engine totals are authoritative"),
    }


def economic_identity(block: dict) -> dict:
    """Economic identity on the established project convention.

    closed position identity:  gross_close_edge - slippage - commission == net_closed_rts
    full-position identity:    net_closed_rts + carry_mtm == engine total_pnl
    (reconcile_all and economic_identity as recorded by economic_block)."""
    gross = _dec(block["gross_close_edge"])
    slip = _dec(block["slippage"])
    comm = _dec(block["commission"])
    net_closed = _dec(block["net_closed_rts"])
    carry = _dec(block["carry_mtm"])
    engine_net = _dec(block["total_pnl"])
    lhs = gross - slip - comm
    closed_identity = str(lhs) == str(net_closed)
    full_identity = round(lhs + carry, 6) == round(engine_net, 6)
    return {
        "gross_close_edge": str(gross),
        "minus_slippage": str(slip),
        "minus_commission": str(comm),
        "net_closed_rts": str(net_closed),
        "closed_identity_holds": closed_identity,
        "carry_mtm": str(carry),
        "open_at_close": block["open_at_close"],
        "engine_net": str(engine_net),
        "full_identity_holds": full_identity,
        "full_identity_resid6": str(round(float(lhs + carry - engine_net), 6)),
        "reconcile_all": block["reconcile"]["reconcile_all"],
        "economic_identity_block": block["reconcile"]["economic_identity"],
        "reconcile_note": block["reconcile"]["reconcile_note"],
    }


def trade_statistics(rts: Sequence[dict], research_days: int) -> dict:
    n = len(rts)
    wins = [r for r in rts if r["net"] > 0]
    losses = [r for r in rts if r["net"] <= 0]
    gross_profit = float(sum(r["gross_close"] for r in rts if r["gross_close"] > 0))
    gross_loss = float(sum(r["gross_close"] for r in rts if r["gross_close"] < 0))
    net = float(sum(r["net"] for r in rts))
    active_days = len({r["entry_day"] for r in rts})
    holds = [r["hold_minutes"] for r in rts]
    streaks = streak_stats(rts)
    return {
        "round_trips": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "expectancy_net_per_rt": round(net / n, 6) if n else None,
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "net": round(net, 6),
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss else None,
        "active_days": active_days,
        "inactive_days": research_days - active_days,
        "longest_win_streak": streaks["longest_winning_streak"],
        "longest_losing_streak": streaks["longest_losing_streak"],
        "median_hold_minutes": st.median(holds) if holds else None,
        "mean_hold_minutes": round(float(sum(holds)) / len(holds), 2) if holds else None,
    }


def serialize_position(pos) -> str:
    if pos is None:
        return "FLAT/NONE"
    try:
        return f"{pos.direction.value}({pos.quantity})"
    except Exception:
        return str(pos)


def build_entry_trace(rts: Sequence[dict], ctx: Sequence[dict | None], journal: Sequence[dict]) -> list[dict]:
    rows = []
    for r in rts:
        c = ctx[r["entry"]["entry_index"]]
        jb = journal[r["entry"]["entry_index"]].get("position_before") if r["entry"]["entry_index"] < len(journal) else None
        rows.append({
            "entry_index": r["entry"]["entry_index"],
            "entry_day": r["entry_day"],
            "side": r["side"],
            "raw_underlying_volmove": c["volmove"] if c else None,
            "raw_trend": c["trend"] if c else None,
            "vol_gate_state": "ACTIVE_RETAINED",
            "expansion_ratio": c["expansion"] if c else None,
            "position_before": serialize_position(jb),
            "exit_index": r["exit_index"],
            "exit_day": r["exit_day"],
            "exit_reason": r["exit_reason"],
            "realized_pnl_net": str(r["net"]),
        })
    return rows


def divergence_summary(rts_a: Sequence[dict], rts_b: Sequence[dict]) -> dict:
    a_entries = {(r["entry"]["entry_index"], r["side"]) for r in rts_a}
    b_entries = {(r["entry"]["entry_index"], r["side"]) for r in rts_b}
    common = a_entries & b_entries
    return {
        "a_entries": len(a_entries), "b_entries": len(b_entries),
        "common_same_bar_same_side": len(common),
        "a_only": len(a_entries - b_entries),
        "b_only": len(b_entries - a_entries),
        "a_entry_days": len({r["entry_day"] for r in rts_a}),
        "b_entry_days": len({r["entry_day"] for r in rts_b}),
    }


def daily_attribution(rts: Sequence[dict], day_map: dict) -> dict:
    by_day: dict[str, list] = {}
    for r in rts:
        by_day.setdefault(r["entry_day"], []).append(r)
    rows = []
    for d, gr in sorted(by_day.items()):
        rows.append({
            "day": d,
            "regime": day_map.get(d, "unknown"),
            "count": len(gr),
            "net": round(float(sum(r["net"] for r in gr)), 6),
        })
    pos = sum(1 for r in rows if r["net"] > 0)
    neg = sum(1 for r in rows if r["net"] <= 0)
    by_regime: dict[str, list] = {}
    for r in rows:
        by_regime.setdefault(r["regime"], []).append(r["net"])
    regime_summary = {k: {"days": len(v), "net": round(sum(v), 6)} for k, v in by_regime.items()}
    return {"days": rows, "positive_days": pos, "negative_days": neg, "by_regime": regime_summary}


def _hold_bucket(hold_days: int) -> str:
    if hold_days <= 1:
        return "intraday_or_1d"
    if hold_days <= 5:
        return "2d_5d"
    if hold_days <= 15:
        return "6d_15d"
    return "16d_25d"


def signatures_are_actionable(signals, bars):
    if len(signals) != len(bars):
        return False
    for s in signals:
        if s is not None and not isinstance(s.actionable, bool):
            return False
    return True


def main() -> int:
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256)):
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
    research_bars = [b for b in all_bars if b.timestamp.date() < OOS_START]
    if len(research_bars) != RESEARCH_BARS_EXPECTED:
        raise SystemExit(f"STOP: research slice = {len(research_bars)} bars (expected {RESEARCH_BARS_EXPECTED}).")
    research_days = len({b.timestamp.date() for b in research_bars})
    if research_days != RESEARCH_DAYS_EXPECTED:
        raise SystemExit(f"STOP: research days = {research_days} (expected {RESEARCH_DAYS_EXPECTED}).")
    if any(b.timestamp.date() >= OOS_START for b in research_bars):
        raise SystemExit("STOP: OOS bar leaked into research slice.")
    print(f"research domain: {len(research_bars)} bars / {research_days} days "
          f"({research_bars[0].timestamp.date()} .. {research_bars[-1].timestamp.date()}); OOS firewall active (handed-limit >= {OOS_START.isoformat()}).")

    config = EvaluationConfig().backtest()
    day_map, vol_bucket_map = classed_days(research_bars)

    signals_a = ensemble_signals(research_bars, params)
    signals_b = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    for name, sig in (("A", signals_a), ("B", signals_b)):
        if not signatures_are_actionable(sig, research_bars):
            raise SystemExit(f"STOP: {name} signal stream malformed.")
        if any(b.timestamp.date() >= OOS_START for b in research_bars):
            raise SystemExit(f"STOP: {name} ran over OOS bars - firewall breach.")

    engine_a, journal_a = BacktestEngine(), []
    engine_b, journal_b = BacktestEngine(), []
    result_a = engine_run(research_bars, config, engine_a, signals_a, journal_a)
    result_b = engine_run(research_bars, config, engine_b, signals_b, journal_b)

    block_a = economic_block("n3_ensemble:BENCHMARK_A", research_bars, signals_a, result_a, journal_a, day_map, vol_bucket_map)
    block_b = economic_block("vol_led:CANDIDATE_B", research_bars, signals_b, result_b, journal_b, day_map, vol_bucket_map)

    # --- benchmark guard: A must equal recorded Iteration-005 economics ---
    it5d = json.loads(ITER5_OUT.read_text(encoding="utf-8"))
    n3_rec = it5d["competition"]["n3_ensemble"]["full_domain"]
    guard_mismatches = []
    for k, exp in ITER5_N3_EXPECTED.items():
        got = block_a[k]
        if str(got) != str(exp) and not (isinstance(got, float) and got == exp):
            guard_mismatches.append(f"{k}: recorded {exp} != replayed {got}")
    if guard_mismatches:
        raise SystemExit("STOP: benchmark A diverged from recorded Iter-005:\n  " + "\n  ".join(guard_mismatches))

    rts_a, _, _ = build_round_trips(research_bars, journal_a)
    rts_b, _, _ = build_round_trips(research_bars, journal_b)
    for rt in rts_a:
        it5.add_flags(rt, research_bars, day_map, vol_bucket_map)
    for rt in rts_b:
        it5.add_flags(rt, research_bars, day_map, vol_bucket_map)
    for rt in list(rts_a) + list(rts_b):
        rt["hold_bucket"] = _hold_bucket((_date.fromisoformat(rt["exit_day"])
                                          - _date.fromisoformat(rt["entry_day"])).days)

    stats_a = trade_statistics(rts_a, research_days)
    stats_b = trade_statistics(rts_b, research_days)
    ident_a = economic_identity(block_a)
    ident_b = economic_identity(block_b)
    engine_ident_a = engine_identity(result_a)
    engine_ident_b = engine_identity(result_b)

    id_a = it5.amt_sum(rts_a, "gross_close")
    id_b = it5.amt_sum(rts_b, "gross_close")
    stats_a["gross_close_sum"] = str(id_a)
    stats_b["gross_close_sum"] = str(id_b)
    if str(id_a) != str(block_a["gross_close_edge"]):
        raise SystemExit(f"STOP: A gross reconstruction mismatch ({id_a} vs {block_a['gross_close_edge']}).")
    if str(id_b) != str(block_b["gross_close_edge"]):
        raise SystemExit(f"STOP: B gross reconstruction mismatch ({id_b} vs {block_b['gross_close_edge']}).")

    ctx = it8_build_entry_context(research_bars, params)
    trace_a = build_entry_trace(rts_a, ctx, journal_a)
    trace_b = build_entry_trace(rts_b, ctx, journal_b)
    div = divergence_summary(rts_a, rts_b)

    res = {
        "A": {
            "net": _dec(block_a["total_pnl"]),
            "max_dd": _dec(block_a["max_drawdown"]),
            "coverage": float(_dec(block_a["gross_close_edge"]) / (_dec(block_a["slippage"]) + _dec(block_a["commission"])) * 100),
            "win_rate": float(block_a["win_rate_pct"]),
            "gross_loss": _dec(stats_a["gross_loss"]),
            "pf": stats_a["profit_factor"] if stats_a["profit_factor"] is not None else 0.0,
            "gross_profit": _dec(stats_a["gross_profit"]),
        },
        "B": {
            "net": _dec(block_b["total_pnl"]),
            "max_dd": _dec(block_b["max_drawdown"]),
            "coverage": float(_dec(block_b["gross_close_edge"]) / (_dec(block_b["slippage"]) + _dec(block_b["commission"])) * 100),
            "win_rate": float(block_b["win_rate_pct"]),
            "gross_loss": _dec(stats_b["gross_loss"]),
            "pf": stats_b["profit_factor"] if stats_b["profit_factor"] is not None else 0.0,
            "gross_profit": _dec(stats_b["gross_profit"]),
        },
    }
    classification = classify_candidate(res)

    out = {
        "experiment": "ITERATION_009_VOL_LED_STRUCTURAL_HYPOTHESIS",
        "objective": ("test whether the Iter-008 VOL-led attribution converts to a structural relationship "
                      "on the pre-OOS research domain; ONE isolated structural change; no parameter tuning"),
        "promotion_notice": ("Exploratory hypothesis test. A better pre-OOS result does NOT authorize "
                             "protected-OOS testing; classification is not a promotion. No ALGO READY, "
                             "no production change, no commit/push."),
        "protected_oos_firewall": {
            "window": ["2025-10-06", "2026-09-11"],
            "used_for_selection": False,
            "oos_result_immutable": {"net": "+1710.004256155", "round_trips": 31, "wins": 20, "losses": 11},
            "engine_replays_forced_onto_research_bars_only": True,
        },
        "research_domain": {
            "window": list(RESEARCH_WINDOW),
            "bars": len(research_bars),
            "days": research_days,
            "config": "EvaluationConfig().backtest() unchanged",
            "warmup_sessions": 54,
        },
        "benchmark_frozen": {
            "candidate": "n3_ensemble", "fingerprint_sha256": FINGERPRINT_SHA256,
            "parameters": FROZEN_PARAMS,
            "benchmark_guard_equal_iteration005": not guard_mismatches,
        },
        "candidate_definition": {
            "id": "VOL-LED STRUCTURAL CANDIDATE (B)",
            "isolated_change": "remove trend-level + trend-slope gating from entry confluence; retain VOL_GATE "
                               "and the underlying confirmed-direction expansion signal (volmove +/-1)",
            "retained": ["underlying signal machinery (DailySeries/day-EMA)", "volatility gate definition",
                         "exits (provider ATR stop / max-hold / confluence-flat)", "position sizing", "costs",
                         "execution model", "causal state handling", "warmup treatment"],
            "unchanged_thresholds": ["VOL_GATE numeric threshold", "MA params (9/26)", "ATR params (4.0)",
                                     "max_hold (25)", "risk controls", "capital/commission/slippage"],
            "no_workaround": True,
        },
        "method": {
            "single_controlled_experiment": True,
            "structural_toggle_only": True,
            "no_parameter_optimization": True,
            "no_oos_for_selection": True,
            "ablations_are_not_candidates": True,
        },
        "economics": {
            "A_benchmark": block_a,
            "B_vol_led": block_b,
        },
        "economic_identity": {"A": ident_a, "B": ident_b},
        "engine_identity": {"A": engine_ident_a, "B": engine_ident_b},
        "trade_statistics": {"A": stats_a, "B": stats_b},
        "comparison": {
            "A": {k: str(v) for k, v in res["A"].items()},
            "B": {k: str(v) for k, v in res["B"].items()},
            "net_delta_B_minus_A": str(res["B"]["net"] - res["A"]["net"]),
            "rt_delta": stats_b["round_trips"] - stats_a["round_trips"],
        },
        "winning_trade_analysis": {
            "A": {
                "wins": {"count": stats_a["wins"], "net": round(float(sum(r["net"] for r in rts_a if r["net"] > 0)), 6),
                         "by_side": condition_pivot([r for r in rts_a if r["net"] > 0], "side"),
                         "by_regime": condition_pivot([r for r in rts_a if r["net"] > 0], "entry_regime"),
                         "by_vol_bucket": condition_pivot([r for r in rts_a if r["net"] > 0], "entry_vol_bucket")},
                "losses": {"count": stats_a["losses"], "net": round(float(sum(r["net"] for r in rts_a if r["net"] <= 0)), 6),
                           "by_regime": condition_pivot([r for r in rts_a if r["net"] <= 0], "entry_regime")},
            },
            "B": {
                "wins": {"count": stats_b["wins"], "net": round(float(sum(r["net"] for r in rts_b if r["net"] > 0)), 6),
                         "by_side": condition_pivot([r for r in rts_b if r["net"] > 0], "side"),
                         "by_regime": condition_pivot([r for r in rts_b if r["net"] > 0], "entry_regime"),
                         "by_vol_bucket": condition_pivot([r for r in rts_b if r["net"] > 0], "entry_vol_bucket")},
                "losses": {"count": stats_b["losses"], "net": round(float(sum(r["net"] for r in rts_b if r["net"] <= 0)), 6),
                           "by_regime": condition_pivot([r for r in rts_b if r["net"] <= 0], "entry_regime")},
            },
        },
        "pivots": {
            "A": {"by_exit_reason": condition_pivot(rts_a, "exit_reason"),
                  "by_entry_regime": condition_pivot(rts_a, "entry_regime"),
                  "by_side": condition_pivot(rts_a, "side"),
                  "by_vol_bucket": condition_pivot(rts_a, "entry_vol_bucket"),
                  "by_hold_bucket": condition_pivot(rts_a, "hold_bucket")},
            "B": {"by_exit_reason": condition_pivot(rts_b, "exit_reason"),
                  "by_entry_regime": condition_pivot(rts_b, "entry_regime"),
                  "by_side": condition_pivot(rts_b, "side"),
                  "by_vol_bucket": condition_pivot(rts_b, "entry_vol_bucket"),
                  "by_hold_bucket": condition_pivot(rts_b, "hold_bucket")},
        },
        "statefulness": {
            "trace_A": trace_a, "trace_B": trace_b,
            "divergence": div,
            "interpretation": ("A and B follow DIFFERENT stateful paths by construction; trades are not compared "
                               "trade-for-trade. The trace records per entry: raw underlying signal, VOL_GATE state, "
                               "final candidate signal (side), position before, entry bar, exit bar/reason, realized net."),
        },
        "daily_attribution": {"A": daily_attribution(rts_a, day_map), "B": daily_attribution(rts_b, day_map)},
        "classification": {
            "pre_registered_rules": CLASSIFY_RULES,
            "result": classification,
            "rationale": {
                "net_B_vs_A": f"{res['B']['net']} vs {res['A']['net']}",
                "coverage_B_vs_A": f"{res['B']['coverage']:.2f}% vs {res['A']['coverage']:.2f}%",
                "maxDD_B_vs_A": f"{res['B']['max_dd']} vs {res['A']['max_dd']}",
                "win_rate_B_vs_A": f"{res['B']['win_rate']}% vs {res['A']['win_rate']}%",
                "gross_loss_B_vs_A": f"{res['B']['gross_loss']} vs {res['A']['gross_loss']}",
            },
        },
        "safety_state": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True,
        },
        "trades": {"A": rts_a, "B": rts_b},
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"iteration_009 written: {OUT_FILE}  sha256={_sha256(OUT_FILE)}")

    print("=" * 96)
    print("ITERATION 009 - VOL-LED STRUCTURAL HYPOTHESIS (pre-OOS A/B, research domain only)")
    print("=" * 96)
    print(f"research domain: {len(research_bars)} bars / {research_days} days (OOS firewall enforced, no OOS replay)")
    print(f"{'net':18s} {str(block_a['total_pnl']):>22s} {str(block_b['total_pnl']):>22s}")
    print(f"{'gross edge':18s} {str(block_a['gross_close_edge']):>22s} {str(block_b['gross_close_edge']):>22s}")
    print(f"{'round trips':18s} {block_a['round_trips']:>22d} {block_b['round_trips']:>22d}")
    print(f"{'fills':18s} {block_a['fills']:>22d} {block_b['fills']:>22d}")
    print(f"{'win rate':18s} {float(block_a['win_rate_pct']):>21.2f}% {float(block_b['win_rate_pct']):>21.2f}%")
    print(f"{'max drawdown':18s} {str(block_a['max_drawdown']):>22s} {str(block_b['max_drawdown']):>22s}")
    ca, cb = float(_dec(block_a["gross_close_edge"]) / (_dec(block_a["slippage"]) + _dec(block_a["commission"])) * 100), \
        float(_dec(block_b["gross_close_edge"]) / (_dec(block_b["slippage"]) + _dec(block_b["commission"])) * 100)
    print(f"{'cost coverage':18s} {ca:>21.2f}% {cb:>21.2f}%")
    print(f"{'active days':18s} {stats_a['active_days']:>22d} {stats_b['active_days']:>22d}")
    print(f"{'carry share':18s} {block_a['carry_share_pct']:>21.2f}% {block_b['carry_share_pct']:>21.2f}%")
    print(f"identity A: full {ident_a['full_identity_holds']} (resid {ident_a['full_identity_resid6']}) closed {ident_a['closed_identity_holds']} rec {ident_a['reconcile_all']}")
    print(f"identity B: full {ident_b['full_identity_holds']} (resid {ident_b['full_identity_resid6']}) closed {ident_b['closed_identity_holds']} rec {ident_b['reconcile_all']} open_at_close {ident_b['open_at_close']}")
    print(f"engine residual A/B: {engine_ident_a['residual_total_pnl_minus_sum']} / {engine_ident_b['residual_total_pnl_minus_sum']} "
          f"(== -commission: {engine_ident_a['identity_total_pnl_eq_sum_minus_commission']} / {engine_ident_b['identity_total_pnl_eq_sum_minus_commission']})")
    print(f"statefulness divergence: A entries {div['a_entries']} / B entries {div['b_entries']} / "
          f"common same-bar-same-side {div['common_same_bar_same_side']}")
    print(f"classification: {classification}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())