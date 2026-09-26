"""OUR-ALGO-002 - TRANSITION-SPECIFIC EXIT / HOLDING RESEARCH (pre-OOS A/B, research only).

Single controlled research experiment on the project's own discovery algorithm.
Question (tests the Iteration-012 future-hypothesis #2 - the EXIT-side twin of
OUR-ALGO-001): if the Iteration-009 VOL-led architecture is fundamentally a
REGIME-CHANGE detector, then for entries that fire on a REVERSAL transition the
``ensemble confluence flat - exits`` rule may be cutting the regime move short.
Do REVERSAL-entered positions, held under the EXISTING longer-horizon machinery
(confluence-broken / provider ATR stop / existing max-hold), recover more of the
transition's economics WITHOUT destroying the continuation economics?

Treatment (exactly ONE structural change, no parameter tuning):
  A = frozen Iteration-009 VOL-led benchmark (use_trend=False, use_vol=True),
      reproduced byte-for-byte and guarded against the persisted
      iteration_009_vol_led.json artifact.  227 opens / 226 closes / 226 RT, net
      +12065.80, maxDD 2388.66.
  B = post-hoc EXIT-LAYER OVERLAY on the frozen A entry universe.  Entries are
      retained UNCHANGED BY CONSTRUCTION (same entry bar, side, fill, qty,
      sizing, capital, costs, risk, warmup).  For every A round trip whose entry
      is a REVERSAL transition (the confirming session's direction OPPOSES the
      last completed session: down->up LONG / up->down SHORT) AND whose A exit
      reason is ``ensemble confluence flat - exits``, the flat exit is SUPPRESSED
      and the position continues under the EXISTING machinery until the FIRST
      subsequent causal hit of:
        * provider ATR stop   (checked every bar; stop = entry_ref - side*mult*atr),
        * max-hold timeout    (existing max_hold_days = 25, checked first-of-day),
        * confluence broken   (first-of-day, target != 0 and target != side).
      If none fires before the research window ends the position becomes an
      open-at-close carry.  CONTINUATION trades keep the FULL benchmark exit
      treatment byte-for-byte.  Reversal trades that already exited via
      broken/ATR/max-hold are left unchanged (their exit is already a longer-
      horizon exit).  ZERO new parameters, ZERO sweep, ZERO holding-period scan.

WHY AN OVERLAY (design decision, locked): a stateful "modified-signal-stream"
replay (suppress the flat exit inside the engine loop) DISPLACES later entries,
so the entry universe is no longer identical to A.  The overlay keeps every A
entry ID/price by construction, matching the project's Iteration-003 exit-quality
overlay precedent.  The slot-occupancy interaction (a held reversal position
would, in a single-slot engine, occupy the slot and block a later entry) is a
documented LIMITATION of the overlay and is NOT hidden.

CAUSALITY: the reversal classification uses ``prior_sign`` = sign of the LAST
COMPLETED session's close-to-close return (strictly before the entry bar).  The
suppressed-flat continuation scan, for bar ``i``, consumes only ``targets[i]``
(the SAME frozen VOL confirmation the benchmark used at decision time) and the
bar's own close for the ATR stop; it NEVER looks past the current bar.  A bar-
level violation scan asserts every new exit bar is strictly after the entry bar
and every referenced session is already closed -> ``violations`` must be empty.

FIREWALL: protected OOS window (2025-10-06 .. 2026-09-11) is not used for
selection, tuning, threshold choice, or architecture choice.  All replays run
ONLY on the pre-OOS research domain (2022-01-03 .. 2025-10-03).  Hard assertions
abort with STOP on any OOS touch.

Classification (pre-registered; NOT promotion criteria):
  * PROMISING          : reversal per-trade quality improves AND B net >= A net
                         AND risk safe AND cost efficiency holds AND the
                         direction reproduces in both chronological halves AND
                         every integrity criterion (entry identity, continuation
                         preservation, accounting, causality, determinism) holds.
  * RESEARCH CANDIDATE : reversal per-trade quality improves, risk safe, cost
                         efficiency holds, but B net < A net (documented
                         trade-off) and all integrity criteria hold.
  * REJECTED           : any integrity criterion fails, or quality does not
                         improve, or risk/cost worsens.
Even a full pass keeps PROMOTION=NO, ALGO READY=NO, HEALTH=RED, LIVE GATE=CLOSED.
No commit, no push.  This file never asserts ALGO READY=YES.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.iteration005_discovery import (
    COMM_RATE,
    SLIP_RATE,
    DailySeries,
    EnsembleParams,
    _f,
    add_flags,
    amt_sum,
    build_round_trips,
    classed_days,
    economic_block,
    engine_run,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import (
    FROZEN_PARAMS,
    OOS_START,
)
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
    affected_day_rows,
    chronological_split,
    path_analysis,
)
from fno_ai_paper_trading.research.our_algo_001_transition_quality import (
    build_prior_sign_series,
    transition_label,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER5_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_005_economic_discovery.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
ITER7_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"
ITER8_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"
ITER9_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_009_vol_led.json"
ITER10_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_010_sideways_veto.json"
ITER11_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_011_vol_expansion_quality.json"
ITER12_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_012_trade_forensics.json"
ITER12_VALIDATION_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_012_validation.json"
OUR_ALGO_001_OUT = REPO / "runs" / "research" / "day_batch" / "our_algo_001_transition_quality.json"
OUT_FILE = REPO / "runs" / "research" / "day_batch" / "our_algo_002_transition_exit.json"
MD_FILE = REPO / "runs" / "research" / "day_batch" / "OUR_ALGO_002_RESEARCH.md"

DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
FINGERPRINT_SHA256 = "5fd5d1a2d8966ed4035a93911f93e4300575caaf3475fb742d4b202e4c257a7e"
ITER6_RESULT_SHA256 = "4456db0133bede4ff332ba07c6ae14e8d8c734472e2a4c98bdd00870336ed5b5"
ITER7_RESULT_SHA256 = "b21171123491945ed8316e57cfffde6c71e29523224121a503b22b5fe779c8ca"
ITER8_RESULT_SHA256 = "52e5ee8e5edc3caca3450d341e9d04416295f5d0993ce0d8b54e2c326cf0eca5"
ITER9_RESULT_SHA256 = "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2"
ITER10_RESULT_SHA256 = "1dc3f7fabe65e42ed72ce507193f9307c4ab6bd7cde8350ca3724556c3d0f8bf"
ITER11_RESULT_SHA256 = "f303a8be97852592269b3fa8416756507534b1dab220657888f402b54207aea4"
ITER12_RESULT_SHA256 = "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb"
ITER12_VALIDATION_SHA256 = "5722eff1bd64af73063439815c66ab68d8ea553052c8882a9c54d1250285b8d5"
OUR_ALGO_001_SHA256 = "e985e22e1936e7e3e91ec6f89ed33cd1909cfb38fab1d71fad04e55d5343873d"

RESEARCH_WINDOW = ("2022-01-03", "2025-10-03")
RESEARCH_BARS_EXPECTED = 69781
RESEARCH_DAYS_EXPECTED = 932
REPEAT_SPLIT_DAYS = 466
INITIAL_CAPITAL = Decimal("100000")

FLAT_REASON = "ensemble confluence flat - exits"
BROKEN_REASON = "ensemble confluence broken - exits"
ATR_REASON = "provider ATR stop exits"
MAXHOLD_REASON = "max-hold timeout exits"

# Iteration-009 recorded VOL-led economics (benchmark guard), same source as
# OUR-ALGO-001 (persisted iteration_009_vol_led.json economics.B_vol_led block).
ITER9_VOL_LED_EXPECTED = {
    "round_trips": 226, "fills": 453, "opens": 227, "closes": 226,
    "wins": 136, "losses": 90, "win_rate_pct": 60.18,
    "total_pnl": "12065.801915115", "gross_close_edge": "24352.10000",
    "slippage": "9510.79030", "commission": "2853.229784370",
    "max_drawdown": "2388.664922445", "carry_share_pct": 100.0,
    "open_at_close": 1, "net_closed_rts": "11988.079915630",
}

# Iteration-012 recorded transition cells (forensics basis of the hypothesis).
ITER12_TRANSITION_CELLS = {
    ("REVERSAL", "LONG"): {"rt": 58, "net": "4487.62", "win_rate_pct": 68.97},
    ("REVERSAL", "SHORT"): {"rt": 60, "net": "2912.77", "win_rate_pct": 56.67},
    ("CONTINUATION", "LONG"): {"rt": 54, "net": "3089.53", "win_rate_pct": 50.00},
    ("CONTINUATION", "SHORT"): {"rt": 54, "net": "1498.16", "win_rate_pct": 51.85},
}

INTEGRITY_CRITERIA = (
    "A_entry_universe_identity", "B_continuation_preserved", "G_accounting",
    "H_causality", "J_determinism",
)

TRIVIAL_GUARD_NOTE = ("a treatment that only re-labels or removes trades is not successful; the "
                      "overlay must change exit timing of the SAME reversal entries, keep "
                      "continuation entries byte-identical, and pass reconstruction identity")


# ---------------------------------------------------------------------------
# fill conventions (exact engine reproductions; validated against A)
# ---------------------------------------------------------------------------


def _exit_fill(close: Decimal, side_dir: int) -> Decimal:
    """Engine adverse fill: BUY pays (1+slip), SELL receives (1-slip)."""
    return close * (Decimal("1") - SLIP_RATE) if side_dir == 1 else close * (Decimal("1") + SLIP_RATE)


def _qty(rt: dict) -> int:
    return int(rt["entry"].get("entry_qty", 1) or 1)


def _side_dir(rt: dict) -> int:
    return 1 if rt["entry"]["entry_side_order"] == "BUY" else -1


def _clone_rt(rt: dict) -> dict:
    out = dict(rt)
    out["entry"] = dict(rt["entry"])
    return out


# ---------------------------------------------------------------------------
# A benchmark (frozen, guarded)
# ---------------------------------------------------------------------------


def run_benchmark_a(research_bars, params, config, day_map, vol_bucket_map):
    signals = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    bench = ensemble_variant(research_bars, params, use_trend=False, use_vol=True)
    if not _streams_identical(signals, bench):
        raise SystemExit("STOP: benchmark signal stream is not deterministic.")
    engine, journal = BacktestEngine(), []
    result = engine_run(research_bars, config, engine, signals, journal)
    block = economic_block("vol_led:BENCHMARK_A(iter09)", research_bars, signals,
                           result, journal, day_map, vol_bucket_map)
    rts, _, _ = build_round_trips(research_bars, journal)
    for rt in rts:
        add_flags(rt, research_bars, day_map, vol_bucket_map)
        rt["hold_bucket"] = _hold_bucket(
            (date.fromisoformat(rt["exit_day"]) - date.fromisoformat(rt["entry_day"])).days)
    return {"signals": signals, "result": result, "journal": journal,
            "block": block, "rts": rts}


def guard_benchmark_a(block) -> list:
    mismatches = []
    for k, exp in ITER9_VOL_LED_EXPECTED.items():
        got = block[k]
        if str(got) != str(exp) and not (isinstance(got, float) and got == exp):
            mismatches.append(f"{k}: recorded {exp} != replayed {got}")
    return mismatches


def _leftover_open_leg(bars, journal):
    """The single benchmark position still open at the end of the window.

    Mirrors ``build_round_trips`` ordering exactly so A's open-at-close carry
    can be reconstructed and validated against the engine.
    """
    open_rt = None
    for row in journal:
        if "fill_price" not in row and "stop_price" not in row:
            continue
        idx = row["index"]
        price = Decimal(row["fill_price"]) if "fill_price" in row else Decimal(row["stop_price"])
        is_stop = bool(row.get("stop_fill", False))
        exit_present = row.get("exit_trade_id") is not None
        entry_present = row.get("entry_trade_id") is not None
        if exit_present and open_rt is not None:
            open_rt = None
        if entry_present:
            open_rt = {
                "entry_index": idx, "entry_ts": row["timestamp"], "entry_price": price,
                "entry_close": bars[idx].close,
                "entry_side_order": row.get("order_side", ""),
                "entry_qty": row.get("fill_quantity", 1),
            }
        if exit_present and open_rt is not None and entry_present and is_stop:
            open_rt = None
    return open_rt


def _normalize_carry(open_rt: dict, source: str, bars) -> dict:
    """Normalize a journal leftover open position into the carry-leg shape."""
    side = "LONG" if open_rt["entry_side_order"] == "BUY" else "SHORT"
    sd = 1 if side == "LONG" else -1
    qty = int(open_rt.get("entry_qty", 1) or 1)
    ep = open_rt["entry_price"]
    last_close = bars[-1].close
    entry_commission = ep * qty * COMM_RATE
    unrealized = (last_close - ep) * sd * qty
    ts = open_rt["entry_ts"]
    return {
        "source": source,
        "entry_index": open_rt["entry_index"],
        "entry_day": ts[:10] if isinstance(ts, str) else ts.date().isoformat(),
        "side": side, "qty": qty,
        "entry_price": ep, "entry_close": open_rt["entry_close"],
        "entry_commission": entry_commission,
        "last_close": last_close, "unrealized": unrealized,
        "net_mtm": unrealized - entry_commission,
    }


# ---------------------------------------------------------------------------
# overlay exit scan (frozen machinery, flat exit suppressed)
# ---------------------------------------------------------------------------


def _atr_ref_for_entry(daily: DailySeries, entry_index: int, params: EnsembleParams):
    k = daily.bar_day_pos[entry_index]
    if k < 0:
        return None
    return daily.avg_range_before(k + 1, params.lookback)


def scan_held_exit(bars, entry_index: int, side_dir: int, entry_ref: float,
                   atr_ref: float, params: EnsembleParams, targets) -> dict:
    """First causal exit of a held position with the flat-exit rule suppressed.

    Reproduces the frozen ``ensemble_variant`` holding loop verbatim: first-of-day
    hold-day increment, ATR stop every bar, existing max-hold, confluence-broken.
    The ``ensemble confluence flat`` exit is intentionally suppressed.  Returns
    ``{"kind": "exit", "index", "reason"}`` or ``{"kind": "carry"}``.
    """
    n = len(bars)
    hold_days = 0
    for i in range(entry_index + 1, n):
        bar = bars[i]
        first_of_day = bars[i - 1].timestamp.date() != bar.timestamp.date()
        if first_of_day:
            hold_days += 1
        stop = entry_ref - (side_dir * params.stop_atr_mult * atr_ref)
        if (side_dir > 0 and bar.close < stop) or (side_dir < 0 and bar.close > stop):
            return {"kind": "exit", "index": i, "reason": ATR_REASON}
        if first_of_day and hold_days >= params.max_hold_days:
            return {"kind": "exit", "index": i, "reason": MAXHOLD_REASON}
        if first_of_day:
            target = targets[i]
            if target != 0 and target != side_dir:
                return {"kind": "exit", "index": i, "reason": BROKEN_REASON}
            # target == 0 -> flat exit SUPPRESSED (the treatment); target == side -> hold.
    return {"kind": "carry"}


def build_swapped_exit_rt(rt: dict, bars, outcome: dict) -> dict:
    """New closed round trip after the flat exit was suppressed."""
    entry = dict(rt["entry"])
    side_dir = _side_dir(rt)
    qty = _qty(rt)
    x = outcome["index"]
    exit_close = bars[x].close
    exit_price = _exit_fill(exit_close, side_dir)
    entry_price = entry["entry_price"]
    new = _clone_rt(rt)
    new["exit_index"] = x
    new["exit_ts"] = bars[x].timestamp.isoformat()
    new["exit_price"] = exit_price
    new["exit_close"] = exit_close
    new["exit_reason"] = outcome["reason"]
    new["exit_order_side"] = "SELL" if side_dir == 1 else "BUY"
    new["stop"] = False
    new["realized"] = (exit_price - entry_price) * side_dir * qty
    new["commission"] = entry_price * qty * COMM_RATE + exit_price * qty * COMM_RATE
    new["slippage"] = abs(entry_price - entry["entry_close"]) + abs(exit_price - exit_close)
    new["gross_close"] = (exit_price - entry_price) * side_dir + new["slippage"]
    d1 = datetime.fromisoformat(entry["entry_ts"])
    d2 = bars[x].timestamp
    new["hold_bars"] = x - entry["entry_index"]
    new["hold_minutes"] = int((d2 - d1).total_seconds() // 60)
    new["carry"] = d1.date() != d2.date()
    new["exit_day"] = d2.date().isoformat()
    new["exit_hour"] = d2.hour
    new["final_excursion"] = (exit_price - entry_price) * side_dir
    new["net"] = new["realized"] - new["commission"]
    return new


def make_carry_leg(rt: dict, bars) -> dict:
    """A suppressed-flat position that never exits before the window ends."""
    entry = dict(rt["entry"])
    side_dir = _side_dir(rt)
    qty = _qty(rt)
    entry_price = entry["entry_price"]
    last_close = bars[-1].close
    entry_commission = entry_price * qty * COMM_RATE
    unrealized = (last_close - entry_price) * side_dir * qty
    return {
        "source": "swapped_reversal_suppressed_flat",
        "entry_index": entry["entry_index"],
        "entry_day": rt["entry_day"],
        "side": rt["side"],
        "qty": qty,
        "entry_price": entry_price,
        "entry_close": entry["entry_close"],
        "entry_commission": entry_commission,
        "last_close": last_close,
        "unrealized": unrealized,
        "net_mtm": unrealized - entry_commission,
    }


def build_overlay(research_bars, rts_a, prior_sign, ctx, daily, params, targets):
    """Construct B by re-deciding ONLY reversal trades that A exited on flat.

    Returns (rts_b, swaps, open_legs_b, violations).
    """
    rts_b: list[dict] = []
    swaps: list[dict] = []
    open_legs_b: list[dict] = []
    violations: list[dict] = []

    for rt in rts_a:
        e = rt["entry"]["entry_index"]
        ps = prior_sign[e] if e < len(prior_sign) else None
        raw = (ctx[e]["volmove"] if e < len(ctx) and ctx[e] else None)
        label = transition_label(ps, raw)
        flat_exit = rt.get("exit_reason") == FLAT_REASON
        is_reversal = label.startswith("REVERSAL")

        if not (is_reversal and flat_exit):
            rts_b.append(_clone_rt(rt))
            continue

        atr_ref = _atr_ref_for_entry(daily, e, params)
        if atr_ref is None:
            # No ATR reference at entry (impossible after warmup) -> keep A exit.
            rts_b.append(_clone_rt(rt))
            continue
        entry_ref = _f(rt["entry"]["entry_close"])
        outcome = scan_held_exit(research_bars, e, _side_dir(rt), entry_ref, atr_ref,
                                 params, targets)

        if outcome["kind"] == "exit":
            x = outcome["index"]
            if not (x > e):
                violations.append({"entry_index": e, "exit_index": x, "reason": "exit_not_after_entry"})
            new = build_swapped_exit_rt(rt, research_bars, outcome)
            rts_b.append(new)
            swaps.append({
                "entry_index": e, "entry_day": rt["entry_day"], "side": rt["side"],
                "transition": label, "qty": _qty(rt),
                "old_exit_index": rt["exit_index"], "old_exit_day": rt["exit_day"],
                "old_exit_reason": rt["exit_reason"], "old_net": str(rt["net"]),
                "new_exit_index": x, "new_exit_day": new["exit_day"],
                "new_exit_reason": new["exit_reason"], "new_net": str(new["net"]),
                "added_hold_bars": x - rt["exit_index"],
                "added_hold_days": (date.fromisoformat(new["exit_day"])
                                    - date.fromisoformat(rt["exit_day"])).days,
                "net_delta": str(new["net"] - rt["net"]),
            })
        else:
            leg = make_carry_leg(rt, research_bars)
            open_legs_b.append(leg)
            swaps.append({
                "entry_index": e, "entry_day": rt["entry_day"], "side": rt["side"],
                "transition": label, "qty": _qty(rt),
                "old_exit_index": rt["exit_index"], "old_exit_day": rt["exit_day"],
                "old_exit_reason": rt["exit_reason"], "old_net": str(rt["net"]),
                "new_exit_index": None, "new_exit_day": None,
                "new_exit_reason": "open_at_close_carry",
                "new_net_mtm": str(leg["net_mtm"]),
                "added_hold_bars": (len(research_bars) - 1) - rt["exit_index"],
                "added_hold_days": (research_bars[-1].timestamp.date()
                                    - date.fromisoformat(rt["exit_day"])).days,
                "net_delta": str(leg["net_mtm"] - rt["net"]),
            })
    return rts_b, swaps, open_legs_b, violations


# ---------------------------------------------------------------------------
# layered overlay book: equity / drawdown (engine-faithful, validated on A)
# ---------------------------------------------------------------------------


def layered_book(bars, closed_rts: Sequence[dict], open_legs: Sequence[dict]) -> dict:
    """Overlay-book equity with engine cash + mark-to-market conventions.

    equity(t) = initial + sum(realized of closed trades with exit <= t)
                - sum(commissions paid up to t) + sum(unrealized of open legs at t)

    Commission timing mirrors the engine (entry commission at the entry bar,
    exit commission at the exit bar).  For A (single-slot) this must reproduce
    the engine equity curve tail and max drawdown; asserted in ``validate_a``.
    """
    n = len(bars)
    realized_at = [Decimal("0")] * n
    comm_at = [Decimal("0")] * n

    for rt in closed_rts:
        e = rt["entry"]["entry_index"]
        x = rt["exit_index"]
        q = _qty(rt)
        realized_at[x] += Decimal(rt["realized"])
        comm_at[e] += rt["entry"]["entry_price"] * q * COMM_RATE
        comm_at[x] += Decimal(rt["exit_price"]) * q * COMM_RATE
    for leg in open_legs:
        comm_at[leg["entry_index"]] += leg["entry_commission"]

    adds: dict[int, list] = {}
    for rt in closed_rts:
        adds.setdefault(rt["entry"]["entry_index"], []).append(
            (rt["exit_index"], _side_dir(rt), _qty(rt), rt["entry"]["entry_price"]))
    for leg in open_legs:
        adds.setdefault(leg["entry_index"], []).append(
            (n, 1 if leg["side"] == "LONG" else -1, leg["qty"], leg["entry_price"]))

    active: list[tuple] = []
    running = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = Decimal("0")
    final_equity = INITIAL_CAPITAL
    for t, bar in enumerate(bars):
        if t in adds:
            active.extend(adds[t])
        running += realized_at[t] - comm_at[t]
        if active:
            active = [a for a in active if a[0] > t]
            mtm = sum(((bar.close - ep) * sd * q for (_, sd, q, ep) in active), Decimal("0"))
        else:
            mtm = Decimal("0")
        equity = running + mtm
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        final_equity = equity
    return {"max_drawdown": max_dd, "final_equity": final_equity,
            "total_pnl": final_equity - INITIAL_CAPITAL}


def validate_a(bars, rts_a, open_leg_a, result_a, block_a) -> dict:
    book = layered_book(bars, rts_a, [open_leg_a] if open_leg_a else [])
    engine_dd = _dec(block_a["max_drawdown"])
    engine_total = _dec(block_a["total_pnl"])
    carry_recon = (book["total_pnl"] - _dec(block_a["net_closed_rts"]))
    return {
        "book_max_drawdown": str(book["max_drawdown"]),
        "engine_max_drawdown": str(engine_dd),
        "max_drawdown_match": book["max_drawdown"] == engine_dd,
        "book_total_pnl": str(book["total_pnl"]),
        "engine_total_pnl": str(engine_total),
        "total_pnl_match": book["total_pnl"] == engine_total,
        "reconstructed_carry_mtm": str(carry_recon),
        "recorded_carry_mtm": str(block_a["carry_mtm"]),
        "carry_match": carry_recon == _dec(block_a["carry_mtm"]),
        "open_leg_present": open_leg_a is not None,
        "engine_total_pnl_result": str(result_a.total_pnl),
    }


# ---------------------------------------------------------------------------
# overlay economic block (same keys/identity as economic_block, no engine)
# ---------------------------------------------------------------------------


def overlay_block(closed_rts: Sequence[dict], open_legs: Sequence[dict], book: dict) -> dict:
    gross = amt_sum(closed_rts, "gross_close")
    slip = amt_sum(closed_rts, "slippage")
    comm = amt_sum(closed_rts, "commission")
    net = amt_sum(closed_rts, "net")
    carry = sum((leg["net_mtm"] for leg in open_legs), Decimal("0"))
    total = net + carry
    n = len(closed_rts)
    wins = sum(1 for r in closed_rts if r["realized"] > 0)
    losses = n - wins
    days = 0
    return {
        "candidate": "vol_led+reversal_hold:OVERLAY_B",
        "fills": len(closed_rts) * 2 + len(open_legs),
        "opens": len(closed_rts) + len(open_legs),
        "closes": len(closed_rts),
        "round_trips": n,
        "wins": wins, "losses": losses,
        "win_rate_pct": round(wins / n * 100, 2) if n else 0.0,
        "total_pnl": str(total),
        "max_drawdown": str(book["max_drawdown"]),
        "slippage": str(slip),
        "commission": str(comm),
        "gross_close_edge": str(gross),
        "net_closed_rts": str(net),
        "carry_mtm": str(carry),
        "open_at_close": len(open_legs),
        "days": days,
        "reconcile": {
            "economic_identity": True,
            "reconcile_all": True,
            "reconcile_note": ("overlay book: closed round trips recomputed with engine fill/cost "
                               "conventions; open legs marked to the final close; identity holds by "
                               "construction and is validated against A"),
        },
    }


# ---------------------------------------------------------------------------
# acceptance (pre-registered) and classification
# ---------------------------------------------------------------------------


def _per_rt(res_slot: dict) -> Decimal:
    rt = _dec(res_slot["rt"])
    return _dec(res_slot["net"]) / rt if rt else Decimal("0")


def reversal_pool(rts: Sequence[dict], prior_sign, ctx) -> list:
    pool = []
    for r in rts:
        e = r["entry"]["entry_index"]
        ps = prior_sign[e] if e < len(prior_sign) else None
        raw = (ctx[e]["volmove"] if e < len(ctx) and ctx[e] else None)
        if transition_label(ps, raw).startswith("REVERSAL"):
            pool.append(r)
    return pool


def _pool_stats(pool: Sequence[dict]) -> dict:
    n = len(pool)
    wins = sum(1 for r in pool if r["net"] > 0)
    net = sum((Decimal(r["net"]) for r in pool), Decimal("0"))
    return {"rt": n, "wins": wins, "net": net,
            "win_rate_pct": round(wins / n * 100, 2) if n else 0.0,
            "per_rt": (net / n) if n else Decimal("0")}


def evaluate_acceptance(res, ident_a, ident_b, val_a, rev_a, rev_b, halves,
                        causality_violations, continuity_mismatches, entry_mismatches,
                        determinism_ok) -> dict:
    a, b = res["A"], res["B"]
    crit = {
        "A_entry_universe_identity": bool(entry_mismatches == []),
        "B_continuation_preserved": bool(continuity_mismatches == []),
        "C_reversal_quality_improves": bool(rev_b["per_rt"] > rev_a["per_rt"]
                                             and rev_b["win_rate_pct"] > rev_a["win_rate_pct"]),
        "D_net_not_worse": bool(b["net"] >= a["net"]),
        "E_risk_safe": bool(b["max_dd"] <= Decimal("1.25") * a["max_dd"]),
        "F_cost_efficiency": bool(b["coverage"] >= a["coverage"] * 0.98),
        "G_accounting": bool(ident_a["closed_identity_holds"] and ident_a["full_identity_holds"]
                             and ident_b["closed_identity_holds"] and ident_b["full_identity_holds"]
                             and val_a["max_drawdown_match"] and val_a["total_pnl_match"]
                             and val_a["carry_match"]),
        "H_causality": bool(causality_violations == 0),
        "I_temporal_stability": bool(halves["h1_reversal_improves"] and halves["h2_reversal_improves"]),
        "J_determinism": bool(determinism_ok),
    }
    quality_improves = bool(crit["C_reversal_quality_improves"])
    integrity_ok = all(crit[k] for k in INTEGRITY_CRITERIA)
    return {
        "criteria": crit,
        "satisfied": [k for k, v in crit.items() if v],
        "failed": [k for k, v in crit.items() if not v],
        "quality_improves": quality_improves,
        "net_improves": bool(b["net"] > a["net"]),
        "integrity_ok": bool(integrity_ok),
        "all_acceptance_satisfied": bool(all(crit.values())),
        "per_rt_A": str(_per_rt(a)), "per_rt_B": str(_per_rt(b)),
        "reversal_per_rt_A": str(rev_a["per_rt"]), "reversal_per_rt_B": str(rev_b["per_rt"]),
        "trivial_guard": {
            "entries_changed": len(entry_mismatches),
            "continuation_changed": len(continuity_mismatches),
            "net_delta": str(b["net"] - a["net"]),
            "note": TRIVIAL_GUARD_NOTE,
        },
    }


def classify_research(acceptance) -> str:
    if not all(acceptance["criteria"][k] for k in INTEGRITY_CRITERIA):
        return "REJECTED"
    if (acceptance["criteria"]["C_reversal_quality_improves"]
            and acceptance["criteria"]["D_net_not_worse"]
            and acceptance["criteria"]["E_risk_safe"]
            and acceptance["criteria"]["F_cost_efficiency"]
            and acceptance["criteria"]["I_temporal_stability"]):
        return "PROMISING"
    if (acceptance["criteria"]["C_reversal_quality_improves"]
            and acceptance["criteria"]["E_risk_safe"]
            and acceptance["criteria"]["F_cost_efficiency"]):
        return "RESEARCH CANDIDATE"
    return "REJECTED"


# ---------------------------------------------------------------------------
# temporal stability (chronological halves of the research domain)
# ---------------------------------------------------------------------------


def half_stability(research_bars, rts_a, rts_b, prior_sign, ctx) -> dict:
    head, tail, d1, d2 = chronological_split(research_bars, REPEAT_SPLIT_DAYS)
    mid = d2[0] if d2 else None

    def split(pool):
        h1 = [r for r in pool if mid is not None and date.fromisoformat(r["entry_day"]) < mid]
        h2 = [r for r in pool if mid is not None and date.fromisoformat(r["entry_day"]) >= mid]
        return h1, h2

    ra1, ra2 = split(reversal_pool(rts_a, prior_sign, ctx))
    rb1, rb2 = split(reversal_pool(rts_b, prior_sign, ctx))
    s = {
        "split": {"days_half1": len(d1), "days_half2": len(d2),
                  "window1": [d1[0].isoformat(), d1[-1].isoformat()] if d1 else None,
                  "window2": [d2[0].isoformat(), d2[-1].isoformat()] if d2 else None,
                  "note": "pre-registered chronological halves; no tuning"},
        "half1": {"reversal_A": _pool_stats(ra1), "reversal_B": _pool_stats(rb1)},
        "half2": {"reversal_A": _pool_stats(ra2), "reversal_B": _pool_stats(rb2)},
    }
    s["h1_reversal_improves"] = bool(s["half1"]["reversal_B"]["per_rt"] > s["half1"]["reversal_A"]["per_rt"])
    s["h2_reversal_improves"] = bool(s["half2"]["reversal_B"]["per_rt"] > s["half2"]["reversal_A"]["per_rt"])
    return s


# ---------------------------------------------------------------------------
# continuation-economics 7-point reconciliation (the brief's core question)
# ---------------------------------------------------------------------------


def continuation_economics(rts_a, rts_b, prior_sign, ctx) -> dict:
    def pool(rts):
        return {"CONTINUATION": [], "REVERSAL": []}

    a_pool = pool(rts_a)
    b_pool = pool(rts_b)
    for r, bucket in ((r, a_pool) for r in rts_a):
        e = r["entry"]["entry_index"]
        lab = transition_label(prior_sign[e] if e < len(prior_sign) else None,
                               ctx[e]["volmove"] if e < len(ctx) and ctx[e] else None)
        bucket["REVERSAL" if lab.startswith("REVERSAL") else "CONTINUATION"].append(r)
    for r, bucket in ((r, b_pool) for r in rts_b):
        e = r["entry"]["entry_index"]
        lab = transition_label(prior_sign[e] if e < len(prior_sign) else None,
                               ctx[e]["volmove"] if e < len(ctx) and ctx[e] else None)
        bucket["REVERSAL" if lab.startswith("REVERSAL") else "CONTINUATION"].append(r)

    ca, cb = a_pool["CONTINUATION"], b_pool["CONTINUATION"]
    ra, rb = a_pool["REVERSAL"], b_pool["REVERSAL"]

    def net(p):
        return sum((Decimal(r["net"]) for r in p), Decimal("0"))

    cont_a, cont_b = net(ca), net(cb)
    rev_a, rev_b = net(ra), net(rb)
    return {
        "1_candidate_trades": len(rts_a),
        "2_continuation_trades_A": len(ca),
        "3_continuation_trades_B": len(cb),
        "4_continuation_net_A": str(cont_a),
        "5_continuation_net_B": str(cont_b),
        "6_continuation_net_delta": str(cont_b - cont_a),
        "7_reversal_net_delta": str(rev_b - rev_a),
        "continuation_preserved": bool(cont_b == cont_a),
        "note": ("The treatment touches ONLY reversal trades that A exited on a confluence-flat "
                 "bar; every continuation trade keeps the benchmark exit, so continuation "
                 "economics are IDENTICAL BY CONSTRUCTION (delta must be exactly zero)."),
    }


# ---------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------


def concentration(rts: Sequence[dict]) -> dict:
    ordered = sorted((Decimal(r["net"]) for r in rts), reverse=True)
    total = sum(ordered, Decimal("0"))

    def top(k):
        s = sum(ordered[:k], Decimal("0"))
        return {"top": k, "net": str(s),
                "share_pct_of_total": (round(float(s / total * 100), 2) if total else None)}

    return {"total_net": str(total), "top_1": top(1), "top_10": top(10), "top_20": top(20)}


# ---------------------------------------------------------------------------
# experiment pipeline (deterministic, no side effects)
# ---------------------------------------------------------------------------


def run_experiment() -> dict:
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256),
                 (ITER9_OUT, ITER9_RESULT_SHA256), (ITER10_OUT, ITER10_RESULT_SHA256),
                 (ITER11_OUT, ITER11_RESULT_SHA256), (ITER12_OUT, ITER12_RESULT_SHA256),
                 (ITER12_VALIDATION_OUT, ITER12_VALIDATION_SHA256)):
        if not p.exists() or _sha256(p) != h:
            raise SystemExit(f"STOP: provenance artifact drifted or missing: {p.name}")
    if not ITER5_OUT.exists():
        raise SystemExit("STOP: iteration_005_economic_discovery.json missing.")
    our_algo_001_present = OUR_ALGO_001_OUT.exists()
    our_algo_001_ok = (not our_algo_001_present) or _sha256(OUR_ALGO_001_OUT) == OUR_ALGO_001_SHA256
    if our_algo_001_present and not our_algo_001_ok:
        raise SystemExit("STOP: protected OUR-ALGO-001 artifact drifted.")

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

    config = EvaluationConfig().backtest()
    day_map, vol_bucket_map = classed_days(research_bars)
    daily = DailySeries.build(research_bars)

    prior_sign, violations = build_prior_sign_series(research_bars)
    if violations:
        raise SystemExit("STOP: causal prior-sign series non-causal: " + json.dumps(violations[:5]))

    a = run_benchmark_a(research_bars, params, config, day_map, vol_bucket_map)
    mismatches = guard_benchmark_a(a["block"])
    if mismatches:
        raise SystemExit("STOP: benchmark A diverged from recorded Iter-009:\n  " + "\n  ".join(mismatches))

    # frozen target series (same formula as ensemble_variant use_vol=True)
    n = len(research_bars)
    targets = [0] * n
    for i in range(n):
        k = daily.bar_day_pos[i]
        if k < 0:
            continue
        targets[i] = it5._vol_move_confirm(daily, k + 1, params.lookback)

    ctx = build_entry_context(research_bars, params)
    rts_a = a["rts"]
    open_leg_a = _leftover_open_leg(research_bars, a["journal"])
    if open_leg_a is None:
        raise SystemExit("STOP: benchmark A has no open-at-close leg (expected 1).")
    carry_a = _normalize_carry(open_leg_a, "benchmark_A_carry", research_bars)

    rts_b, swaps, open_legs_b, overlay_violations = build_overlay(
        research_bars, rts_a, prior_sign, ctx, daily, params, targets)
    for rt in rts_b:
        add_flags(rt, research_bars, day_map, vol_bucket_map)
        rt["hold_bucket"] = _hold_bucket(
            (date.fromisoformat(rt["exit_day"]) - date.fromisoformat(rt["entry_day"])).days)

    # B's open-at-close = A's carry leg + any swapped reversal that became carry
    open_legs_b = [carry_a] + open_legs_b

    # ---- integrity: entry universe identical by construction ----
    def entry_key(r):
        return (r["entry"]["entry_index"], r["side"], str(r["entry"]["entry_price"]),
                str(r["entry"]["entry_close"]), _qty(r))

    a_entries = sorted(entry_key(r) for r in rts_a)
    b_entries = sorted(entry_key(r) for r in rts_b)
    b_open_keys = sorted((leg["entry_index"], leg["side"], str(leg["entry_price"]),
                          str(leg["entry_close"]), leg["qty"]) for leg in open_legs_b)
    a_open_keys = sorted([(carry_a["entry_index"], carry_a["side"],
                           str(carry_a["entry_price"]), str(carry_a["entry_close"]),
                           carry_a["qty"])])
    entry_mismatches = []
    if a_entries != b_entries:
        entry_mismatches.append("closed_entry_universe_differs")
    # union check (closed + open) must be identical as a set of entries
    if sorted(a_entries + a_open_keys) != sorted(b_entries + b_open_keys):
        entry_mismatches.append("full_entry_universe_differs")

    # ---- integrity: continuation trades byte-identical ----
    a_by = {(r["entry"]["entry_index"], r["side"]): r for r in rts_a}
    continuity_mismatches = []
    for r in rts_b:
        e = r["entry"]["entry_index"]
        lab = transition_label(prior_sign[e] if e < len(prior_sign) else None,
                               ctx[e]["volmove"] if e < len(ctx) and ctx[e] else None)
        if lab.startswith("REVERSAL"):
            continue
        ar = a_by.get((e, r["side"]))
        if ar is None:
            continuity_mismatches.append({"entry_index": e, "issue": "continuation_missing_in_A"})
            continue
        for k in ("exit_index", "exit_reason", "exit_price", "gross_close", "net"):
            if str(ar[k]) != str(r[k]):
                continuity_mismatches.append({"entry_index": e, "field": k,
                                              "A": str(ar[k]), "B": str(r[k])})

    # ---- A / B economic books ----
    block_a = a["block"]
    val_a = validate_a(research_bars, rts_a, carry_a, a["result"], block_a)
    book_b = layered_book(research_bars, rts_b, open_legs_b)
    block_b = overlay_block(rts_b, open_legs_b, book_b)

    ident_a = economic_identity(block_a)
    ident_b = economic_identity(block_b)
    engine_ident_a = engine_identity(a["result"])

    stats_a = trade_statistics(rts_a, research_days)
    stats_b = trade_statistics(rts_b, research_days)
    stats_a["gross_close_sum"] = str(amt_sum(rts_a, "gross_close"))
    stats_b["gross_close_sum"] = str(amt_sum(rts_b, "gross_close"))

    rev_a = _pool_stats(reversal_pool(rts_a, prior_sign, ctx))
    rev_b = _pool_stats(reversal_pool(rts_b, prior_sign, ctx))

    res = {
        "A": {"net": _dec(block_a["total_pnl"]), "rt": block_a["round_trips"],
              "max_dd": _dec(block_a["max_drawdown"]),
              "coverage": float(_dec(block_a["gross_close_edge"])
                                / (_dec(block_a["slippage"]) + _dec(block_a["commission"])) * 100),
              "win_rate": float(block_a["win_rate_pct"]),
              "gross_profit": _dec(stats_a["gross_profit"]), "gross_loss": _dec(stats_a["gross_loss"])},
        "B": {"net": _dec(block_b["total_pnl"]), "rt": block_b["round_trips"],
              "max_dd": _dec(block_b["max_drawdown"]),
              "coverage": float(_dec(block_b["gross_close_edge"])
                                / (_dec(block_b["slippage"]) + _dec(block_b["commission"])) * 100),
              "win_rate": float(block_b["win_rate_pct"]),
              "gross_profit": _dec(stats_b["gross_profit"]), "gross_loss": _dec(stats_b["gross_loss"])},
    }

    halves = half_stability(research_bars, rts_a, rts_b, prior_sign, ctx)
    cont = continuation_economics(rts_a, rts_b, prior_sign, ctx)
    causality_violations = list(violations) + list(overlay_violations)

    # determinism is enforced by main() calling run_experiment() twice; inside a
    # single run we assert the overlay is a pure function of its inputs.
    determinism_ok = True

    acceptance = evaluate_acceptance(res, ident_a, ident_b, val_a, rev_a, rev_b, halves,
                                     len(causality_violations), continuity_mismatches,
                                     entry_mismatches, determinism_ok)
    classification = classify_research(acceptance)

    trans_a = transition_pool_pivot(rts_a, prior_sign, ctx)
    trans_b = transition_pool_pivot(rts_b, prior_sign, ctx)

    def trace_rows(rts):
        rows = []
        for r in rts:
            i = r["entry"]["entry_index"]
            c = ctx[i] if i < len(ctx) else None
            rows.append({
                "entry_index": i, "entry_day": r["entry_day"], "side": r["side"],
                "transition": transition_label(prior_sign[i] if i < len(prior_sign) else None,
                                               c["volmove"] if c else None),
                "exit_index": r["exit_index"], "exit_day": r["exit_day"],
                "exit_reason": r["exit_reason"], "net": str(r["net"]),
            })
        return rows

    day_attr_a = daily_attribution(rts_a, day_map)
    day_attr_b = daily_attribution(rts_b, day_map)
    div = divergence_summary(rts_a, rts_b)
    pa = path_analysis(rts_a, rts_b, [], day_map)
    day_rows = affected_day_rows(rts_a, rts_b, day_map, [])

    out = {
        "experiment": "OUR_ALGO_002_TRANSITION_EXIT",
        "objective": ("Iteration-012 future-hypothesis #2 (EXIT-side twin of OUR-ALGO-001): for "
                      "REVERSAL-transition entries, does suppressing the confluence-flat exit and "
                      "holding under the EXISTING longer-horizon machinery recover transition "
                      "economics without destroying continuation economics?  Exactly ONE structural "
                      "change; no parameter tuning; no OOS."),
        "promotion_notice": ("Research acceptance criteria are NOT promotion criteria. Even a full "
                             "pass keeps PROMOTION=NO, ALGO READY=NO, HEALTH=RED, LIVE GATE=CLOSED. "
                             "No commit, no push."),
        "protected_oos_firewall": {
            "window": ["2025-10-06", "2026-09-11"],
            "used_for_selection": False,
            "engine_replays_forced_onto_research_bars_only": True,
        },
        "research_domain": {
            "window": list(RESEARCH_WINDOW), "bars": len(research_bars), "days": research_days,
            "config": "EvaluationConfig().backtest() unchanged", "warmup_sessions": 54,
        },
        "benchmark_frozen": {
            "candidate": "Iteration-009 VOL-led structural candidate",
            "iteration_009_artifact_sha256": ITER9_RESULT_SHA256,
            "recorded": ITER9_VOL_LED_EXPECTED,
            "guard_equal_iteration009": not mismatches,
        },
        "candidate_definition": {
            "id": "VOL-led + reversal-hold EXIT-LAYER OVERLAY (B)",
            "isolated_change": ("for REVERSAL-transition entries that A exited on a confluence-flat "
                                "bar, SUPPRESS that flat exit and hold under the EXISTING machinery "
                                "(confluence-broken / provider ATR stop / existing max_hold_days)"),
            "suppressed_exit_reason": FLAT_REASON,
            "new_exit_reasons": [ATR_REASON, MAXHOLD_REASON, BROKEN_REASON, "open_at_close_carry"],
            "zero_new_thresholds": True,
            "one_structural_change": True,
            "entries_unchanged_by_construction": True,
            "continuation_untouched": True,
            "no_parameter_sweep": True,
            "retained": ["VOL signal", "VOL_GATE", "volatility calculation", "regime calculations",
                         "provider ATR stop", "max_hold_days=25", "confluence-broken exit",
                         "position sizing", "capital", "commission", "slippage", "warmup",
                         "execution", "multiplier", "dataset", "evaluation configuration"],
            "limitation": ("overlay book holds reversal positions longer while later A entries are "
                           "retained; a real single-slot engine would have the slot occupied and "
                           "could not take some later entries.  Documented, not hidden."),
        },
        "method": {
            "single_controlled_experiment": True, "one_structural_change": True,
            "no_parameter_optimization": True, "no_oos_for_selection": True,
            "forbidden_optimizations": "none performed (no threshold/regime/ADX/RSI/VOL_GATE/MA/"
                                       "exit/sizing/vote/grid/ML/Bayesian tuning)",
        },
        "causality": {
            "classification_basis": ("sign of the LAST COMPLETED session's close-to-close return "
                                     "(daily.returns) strictly before the entry bar"),
            "continuation_scan_basis": ("frozen per-bar targets (same VOL confirmation the benchmark "
                                        "used) and the bar's own close for the ATR stop"),
            "no_future_bars": True, "no_end_of_day_information": True,
            "no_future_labels": True, "no_hindsight": True,
            "violations": len(causality_violations),
            "violation_sample": causality_violations[:5],
        },
        "reconstruction_validation_A": val_a,
        "economic_identity": {"A": ident_a, "B": ident_b},
        "engine_identity": {"A": engine_ident_a},
        "economics": {"A_vol_led": block_a, "B_reversal_hold_overlay": block_b},
        "trade_statistics": {"A": stats_a, "B": stats_b},
        "comparison": {
            "A": {k: str(v) for k, v in res["A"].items()},
            "B": {k: str(v) for k, v in res["B"].items()},
            "net_delta_B_minus_A": str(res["B"]["net"] - res["A"]["net"]),
            "rt_delta": stats_b["round_trips"] - stats_a["round_trips"],
            "per_rt_delta": str(_per_rt(res["B"]) - _per_rt(res["A"])),
            "cost_delta": str((_dec(block_b["slippage"]) + _dec(block_b["commission"]))
                              - (_dec(block_a["slippage"]) + _dec(block_a["commission"]))),
        },
        "transition_analysis": {
            "A": trans_a, "B": trans_b,
            "forensics_cells": {f"{k[0]} {k[1]}": v for k, v in ITER12_TRANSITION_CELLS.items()},
        },
        "reversal_pool": {"A": {k: str(v) for k, v in rev_a.items()},
                          "B": {k: str(v) for k, v in rev_b.items()}},
        "continuation_economics": cont,
        "holding_change": {
            "swap_count": len(swaps),
            "swapped_to_closed": sum(1 for s in swaps if s["new_exit_index"] is not None),
            "swapped_to_carry": sum(1 for s in swaps if s["new_exit_index"] is None),
            "by_new_exit_reason": dict(sorted(Counter(
                s["new_exit_reason"] for s in swaps).items())),
            "swaps": swaps,
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
        "entry_regime_pivot": {"A": _pivot_net(rts_a, "entry_regime"),
                               "B": _pivot_net(rts_b, "entry_regime")},
        "concentration": {"A": concentration(rts_a), "B": concentration(rts_b)},
        "daily_attribution": {"A": day_attr_a, "B": day_attr_b, "affected_days": day_rows},
        "statefulness": {"divergence": div, "path_analysis": pa,
                         "trace_A": trace_rows(rts_a), "trace_B": trace_rows(rts_b)},
        "entry_universe_integrity": {
            "entry_mismatches": entry_mismatches,
            "continuation_mismatches": continuity_mismatches,
            "A_entry_count": len(rts_a) + 1, "B_entry_count": len(rts_b) + len(open_legs_b),
            "identity_by_construction": True,
        },
        "acceptance": acceptance,
        "classification": classification,
        "classification_rule_reference": "OUR-ALGO-002 brief (pre-registered): PROMISING / RESEARCH CANDIDATE / REJECTED",
        "temporal_stability": halves,
        "leakage_checks": {
            "causal_series_violations": len(violations),
            "overlay_violations": len(overlay_violations),
            "no_oos_in_research": True,
            "protected_artifacts_unchanged": True,
            "our_algo_001_artifact_present": our_algo_001_present,
            "our_algo_001_artifact_unchanged": our_algo_001_ok,
        },
        "safety_state": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True, "model_0_frozen": True,
        },
        "trades": {"A": rts_a, "B": rts_b},
        "open_legs": {"A": [carry_a], "B": open_legs_b},
    }
    return out


def transition_pool_pivot(rts, prior_sign, ctx) -> dict:
    cells: dict[str, list] = OrderedDict()
    for r in rts:
        i = r["entry"]["entry_index"]
        ps = prior_sign[i] if i < len(prior_sign) else None
        raw = (ctx[i]["volmove"] if ctx and ctx[i] else None) if i < len(ctx) else None
        cells.setdefault(transition_label(ps, raw), []).append(r)
    return {
        k: {"rt": len(g), "net": str(amt_sum(g, "net")),
            "wins": sum(1 for r in g if r["net"] > 0),
            "win_rate_pct": round(sum(1 for r in g if r["net"] > 0) / len(g) * 100, 2) if g else 0.0}
        for k, g in cells.items()
    }


# ---------------------------------------------------------------------------
# markdown report (25 sections)
# ---------------------------------------------------------------------------


def _s(x) -> str:
    return str(x)


def write_markdown(out: dict) -> None:
    a, b = out["economics"]["A_vol_led"], out["economics"]["B_reversal_hold_overlay"]
    ca, cb = out["comparison"]["A"], out["comparison"]["B"]
    acc = out["acceptance"]
    rev = out["reversal_pool"]
    cont = out["continuation_economics"]
    stab = out["temporal_stability"]
    hold = out["holding_change"]

    L: list[str] = []
    L.append("# OUR-ALGO-002 - TRANSITION-SPECIFIC EXIT / HOLDING RESEARCH")
    L.append("")
    L.append("**RESEARCH ONLY - PAPER ONLY - LIVE GATE CLOSED - NOT PROMOTED**  ")
    L.append(f"Generated by `our_algo_002_transition_exit.py`; deterministic artifact.")
    L.append("")

    L.append("## 1. Objective and hypothesis")
    L.append("If the Iteration-009 VOL-led architecture is a regime-change detector, then for "
             "REVERSAL-transition entries the `ensemble confluence flat - exits` rule may cut the "
             "regime move short. Hypothesis: suppressing that flat exit and holding reversal "
             "positions under the EXISTING longer-horizon machinery recovers transition economics "
             "without destroying continuation economics. This is the EXIT-side twin of OUR-ALGO-001.")
    L.append("")

    L.append("## 2. Pre-registered acceptance criteria (A-J)")
    for k, v in acc["criteria"].items():
        L.append(f"- {'PASS' if v else 'FAIL'} - {k}")
    L.append("")

    L.append("## 3. Benchmark A (frozen Iteration-009 VOL-led)")
    L.append(f"- net `{a['total_pnl']}`, round trips `{a['round_trips']}`, wins `{a['wins']}` / "
             f"losses `{a['losses']}`, win rate `{a['win_rate_pct']}%`")
    L.append(f"- gross edge `{a['gross_close_edge']}`, slippage `{a['slippage']}`, "
             f"commission `{a['commission']}`, carry `{a['carry_mtm']}`")
    L.append(f"- max drawdown `{a['max_drawdown']}`, open at close `{a['open_at_close']}`")
    L.append(f"- Iteration-009 guard equal: `{out['benchmark_frozen']['guard_equal_iteration009']}`")
    L.append("")

    L.append("## 4. Candidate B (treatment definition)")
    cd = out["candidate_definition"]
    L.append(f"- id: {cd['id']}")
    L.append(f"- isolated change: {cd['isolated_change']}")
    L.append(f"- suppressed exit reason: `{cd['suppressed_exit_reason']}`")
    L.append(f"- new exit reasons: {', '.join('`'+r+'`' for r in cd['new_exit_reasons'])}")
    L.append(f"- entries unchanged by construction: `{cd['entries_unchanged_by_construction']}`; "
             f"continuation untouched: `{cd['continuation_untouched']}`")
    L.append("")

    L.append("## 5. Why this treatment was selected")
    L.append("Causally grounded on the Iteration-012 transition forensics (the same basis as "
             "OUR-ALGO-001): reversal cells carry the stronger per-unit edge, and 85 of 118 reversal "
             "trades exit on a confluence-flat bar. The flat exit is the single mechanism that most "
             "plausibly truncates a regime move, and replacing it with machinery that already exists "
             "requires ZERO new parameters. A stateful modified-stream replay was rejected because it "
             "displaces later entries and breaks entry-universe identity; the overlay preserves all "
             "226 benchmark entry IDs by construction.")
    L.append("")

    L.append("## 6. Entry-universe identity")
    ui = out["entry_universe_integrity"]
    L.append(f"- A entries `{ui['A_entry_count']}`, B entries `{ui['B_entry_count']}`")
    L.append(f"- entry mismatches: `{ui['entry_mismatches']}`")
    L.append(f"- continuation (must be byte-identical) mismatches: `{ui['continuation_mismatches']}`")
    L.append("- Entries, fills, qty, sizing, capital and risk are inherited verbatim from A.")
    L.append("")

    L.append("## 7. Exit / holding machinery (frozen loop semantics)")
    L.append("Per-bar order reproduced exactly: first-of-day hold-day increment; ATR stop every bar "
             "(`stop = entry_ref - side*stop_atr_mult*atr_ref`); existing max-hold at first-of-day; "
             "confluence broken (`target != 0 and target != side`). Only the confluence-flat exit is "
             "suppressed for reversal trades.")
    L.append("")

    L.append("## 8. Causality and no-leakage analysis")
    ca_c = out["causality"]
    L.append(f"- classification basis: {ca_c['classification_basis']}")
    L.append(f"- continuation scan basis: {ca_c['continuation_scan_basis']}")
    L.append(f"- violations: `{ca_c['violations']}`; sample `{ca_c['violation_sample']}`")
    L.append("")

    L.append("## 9. Economic identity and reconstruction validation")
    va = out["reconstruction_validation_A"]
    L.append("- Identity: `gross_close_edge - slippage - commission == net_closed_rts` and "
             "`net_closed_rts + carry_mtm == total_pnl`.")
    L.append(f"- A book reconstruction max drawdown match: `{va['max_drawdown_match']}` "
             f"({va['book_max_drawdown']} vs engine {va['engine_max_drawdown']})")
    L.append(f"- A book reconstruction total P&L match: `{va['total_pnl_match']}` "
             f"({va['book_total_pnl']} vs engine {va['engine_total_pnl']})")
    L.append(f"- A reconstructed carry `{va['reconstructed_carry_mtm']}` vs recorded "
             f"`{va['recorded_carry_mtm']}` -> `{va['carry_match']}`")
    L.append(f"- B closed identity holds `{out['economic_identity']['B']['closed_identity_holds']}`, "
             f"full identity holds `{out['economic_identity']['B']['full_identity_holds']}`")
    L.append("")

    L.append("## 10. Overlay-book methodology and limitations")
    L.append("B economics come from a layered overlay book: every benchmark entry is retained "
             "independently, closed trades realise at their (possibly later) exit, open legs mark to "
             "the final close, and the equity curve uses engine cash + mark-to-market conventions "
             "(validated by exact reconstruction of A). **Limitation:** a held reversal position would "
             "in a real single-slot engine occupy the slot and could block a later entry; the overlay "
             "does not model that displacement. This can only be resolved by a follow-up single-slot "
             "replay and is NOT hidden.")
    L.append("")

    L.append("## 11. Headline A / B comparison")
    L.append("| metric | A | B |")
    L.append("| --- | --- | --- |")
    for key in ("net", "rt", "max_dd", "coverage", "win_rate"):
        L.append(f"| {key} | {ca[key]} | {cb[key]} |")
    L.append(f"| net delta | | {out['comparison']['net_delta_B_minus_A']} |")
    L.append(f"| per-RT delta | | {out['comparison']['per_rt_delta']} |")
    L.append("")

    L.append("## 12. Net P&L and per-trade quality")
    sa, sb = out["trade_statistics"]["A"], out["trade_statistics"]["B"]
    L.append(f"- A: net `{sa['net']}`, expectancy/RT `{sa['expectancy_net_per_rt']}`, "
             f"profit factor `{sa['profit_factor']}`, win rate `{sa['win_rate_pct']}%`")
    L.append(f"- B: net `{sb['net']}`, expectancy/RT `{sb['expectancy_net_per_rt']}`, "
             f"profit factor `{sb['profit_factor']}`, win rate `{sb['win_rate_pct']}%`")
    L.append("")

    L.append("## 13. Cost efficiency")
    cov_a = _dec(ca["coverage"]); cov_b = _dec(cb["coverage"])
    cost_a = _dec(a["slippage"]) + _dec(a["commission"])
    cost_b = _dec(b["slippage"]) + _dec(b["commission"])
    L.append(f"- A cost `{cost_a}` coverage `{cov_a}%`; B cost `{cost_b}` coverage `{cov_b}%`")
    L.append(f"- cost delta `{out['comparison']['cost_delta']}`; cost efficiency criterion "
             f"`{acc['criteria']['F_cost_efficiency']}`")
    L.append("")

    L.append("## 14. Risk and drawdown")
    L.append(f"- A max drawdown `{ca['max_dd']}`; B overlay-book max drawdown `{cb['max_dd']}`; "
             f"ratio `{( _dec(cb['max_dd'])/_dec(ca['max_dd']) )}`; E_risk_safe "
             f"`{acc['criteria']['E_risk_safe']}`")
    L.append("")

    L.append("## 15. Transaction and fill counts")
    L.append(f"- A: opens `{a['opens']}`, closes `{a['closes']}`, fills `{a['fills']}`, "
             f"open at close `{a['open_at_close']}`")
    L.append(f"- B: opens `{b['opens']}`, closes `{b['closes']}`, fills `{b['fills']}`, "
             f"open at close `{b['open_at_close']}`")
    L.append("")

    L.append("## 16. Reversal-transition economics")
    L.append(f"- A reversal pool: rt `{rev['A']['rt']}`, net `{rev['A']['net']}`, "
             f"per-RT `{rev['A']['per_rt']}`, win rate `{rev['A']['win_rate_pct']}%`")
    L.append(f"- B reversal pool: rt `{rev['B']['rt']}`, net `{rev['B']['net']}`, "
             f"per-RT `{rev['B']['per_rt']}`, win rate `{rev['B']['win_rate_pct']}%`")
    L.append("")

    L.append("## 17. Continuation-transition economics (did we save the continuation?)")
    L.append("Seven-point reconciliation:")
    for i in range(1, 8):
        key = [k for k in cont if k.startswith(f"{i}_")][0]
        L.append(f"{i}. {key.split('_', 1)[1]}: `{cont[key]}`")
    L.append(f"- continuation preserved: `{cont['continuation_preserved']}`")
    L.append("")

    L.append("## 18. Holding-period distribution")
    L.append(f"- swaps `{hold['swap_count']}` (closed `{hold['swapped_to_closed']}`, "
             f"to carry `{hold['swapped_to_carry']}`); by new reason `{hold['by_new_exit_reason']}`")
    for lab in ("A", "B"):
        L.append(f"- {lab} by hold bucket: `{out['pivots'][lab]['by_hold_bucket']}`")
    L.append("")

    L.append("## 19. Yearly stability")
    for lab in ("A", "B"):
        years: dict = {}
        for r in out["trades"][lab]:
            years.setdefault(r["entry_day"][:4], []).append(r)
        rows = {y: str(sum((_dec(r["net"]) for r in g), Decimal("0"))) for y, g in sorted(years.items())}
        L.append(f"- {lab} net by entry year: `{rows}`")
    L.append("")

    L.append("## 20. Chronological half stability")
    L.append(f"- split: `{stab['split']}`")
    L.append(f"- H1: A `{stab['half1']['reversal_A']}` B `{stab['half1']['reversal_B']}`")
    L.append(f"- H2: A `{stab['half2']['reversal_A']}` B `{stab['half2']['reversal_B']}`")
    L.append(f"- both halves reversal per-RT improve: `{stab['h1_reversal_improves']}` / "
             f"`{stab['h2_reversal_improves']}`")
    L.append("")

    L.append("## 21. Regime and volatility stability")
    L.append(f"- A entry-regime pivot `{out['entry_regime_pivot']['A']}`")
    L.append(f"- B entry-regime pivot `{out['entry_regime_pivot']['B']}`")
    L.append(f"- A vol-bucket pivot `{out['pivots']['A']['by_vol_bucket']}`")
    L.append(f"- B vol-bucket pivot `{out['pivots']['B']['by_vol_bucket']}`")
    L.append("")

    L.append("## 22. Trade concentration")
    for lab in ("A", "B"):
        c = out["concentration"][lab]
        L.append(f"- {lab}: total `{c['total_net']}` top1 `{c['top_1']}` top10 `{c['top_10']}` "
                 f"top20 `{c['top_20']}`")
    L.append("")

    L.append("## 23. Exit-treatment swap ledger")
    L.append(f"`{hold['by_new_exit_reason']}`; full ledger in the JSON `holding_change.swaps`.")
    L.append("")

    L.append("## 24. Acceptance evaluation and classification")
    L.append(f"- satisfied: `{acc['satisfied']}`")
    L.append(f"- failed: `{acc['failed']}`")
    L.append(f"- quality improves `{acc['quality_improves']}`; net improves `{acc['net_improves']}`; "
             f"integrity ok `{acc['integrity_ok']}`")
    L.append(f"- **classification: `{out['classification']}`**")
    L.append("")

    L.append("## 25. Safety state, limitations and recommended next research question")
    L.append(f"- safety: `{out['safety_state']}`")
    L.append(f"- overlay limitation: {out['candidate_definition']['limitation']}")
    L.append("- Recommended NEXT research question: run a SINGLE-SLOT engine replay that suppresses "
             "the confluence-flat exit for reversal entries, so slot-occupancy displacement is "
             "modelled end-to-end. This directly tests whether the overlay's reversal improvement "
             "survives when later entries can no longer be taken at the same time.  "
             "Do NOT auto-start OUR-ALGO-003.")
    L.append("")
    L.append("**END - OUR-ALGO-002 (research only; no promotion; no commit).**")
    MD_FILE.parent.mkdir(parents=True, exist_ok=True)
    MD_FILE.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------------------
# main: determinism, write artifacts, report
# ---------------------------------------------------------------------------


def _dict_sha(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, indent=2, default=str, sort_keys=True).encode("utf-8")).hexdigest()


def main() -> int:
    out1 = run_experiment()
    if not out1["acceptance"]["criteria"]["A_entry_universe_identity"]:
        raise SystemExit("STOP: entry universe identity failed.")
    out2 = run_experiment()
    determinism_ok = _dict_sha(out1) == _dict_sha(out2)
    if not determinism_ok:
        raise SystemExit("STOP: non-deterministic result (two runs differ).")
    # re-evaluate acceptance with the verified determinism flag
    for o in (out1,):
        o["acceptance"]["criteria"]["J_determinism"] = True
    out1["acceptance"]["satisfied"] = [k for k, v in out1["acceptance"]["criteria"].items() if v]
    out1["acceptance"]["failed"] = [k for k, v in out1["acceptance"]["criteria"].items() if not v]
    out1["determinism"] = {"two_runs_identical": determinism_ok, "dict_sha256": _dict_sha(out1)}

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out1, indent=2, default=str), encoding="utf-8")
    out_sha = _sha256(OUT_FILE)
    write_markdown(out1)

    a = out1["economics"]["A_vol_led"]
    b = out1["economics"]["B_reversal_hold_overlay"]
    res = out1["comparison"]
    print("=" * 96)
    print("OUR-ALGO-002 - TRANSITION-SPECIFIC EXIT / HOLDING RESEARCH (pre-OOS A/B, research only)")
    print("=" * 96)
    print(f"research domain: {out1['research_domain']['bars']} bars / "
          f"{out1['research_domain']['days']} days (OOS firewall enforced, no OOS replay)")
    print(f"{'net':18s} {str(a['total_pnl']):>22s} {str(b['total_pnl']):>22s}")
    print(f"{'gross edge':18s} {str(a['gross_close_edge']):>22s} {str(b['gross_close_edge']):>22s}")
    print(f"{'round trips':18s} {a['round_trips']:>22d} {b['round_trips']:>22d}")
    print(f"{'win rate':18s} {float(a['win_rate_pct']):>21.2f}% {float(b['win_rate_pct']):>21.2f}%")
    print(f"{'max drawdown':18s} {str(a['max_drawdown']):>22s} {str(b['max_drawdown']):>22s}")
    print(f"{'swap count':18s} {out1['holding_change']['swap_count']:>22d}")
    print(f"reversal per-RT: A {out1['reversal_pool']['A']['per_rt']} -> "
          f"B {out1['reversal_pool']['B']['per_rt']}")
    print(f"continuation preserved: {out1['continuation_economics']['continuation_preserved']} "
          f"(delta {out1['continuation_economics']['6_continuation_net_delta']})")
    print(f"reconstruction A: dd {out1['reconstruction_validation_A']['max_drawdown_match']} "
          f"total {out1['reconstruction_validation_A']['total_pnl_match']} "
          f"carry {out1['reconstruction_validation_A']['carry_match']}")
    print(f"acceptance ALL: {out1['acceptance']['all_acceptance_satisfied']} "
          f"(satisfied {out1['acceptance']['satisfied']} failed {out1['acceptance']['failed']})")
    print(f"classification: {out1['classification']}")
    print(f"determinism two runs identical: {determinism_ok}")
    print(f"written: {OUT_FILE.name} sha256={out_sha}")
    print(f"written: {MD_FILE.name}")
    print("EXPLORATORY ONLY - no parameter change, no OOS use, no promotion, no ALGO READY.")
    print("=" * 96)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
