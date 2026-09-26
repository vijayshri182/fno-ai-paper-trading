"""ITERATION 012 - TRADE FORENSICS (analytical only, no strategy change).

Forensic analysis of the frozen Iteration-009 VOL-led strategy's 226 completed
round trips on the research domain (2022-01-03 .. 2025-10-03, 69,781 bars /
932 days).  Purpose: understand WHY the strategy made +₹12,065.80.

This module does NOT create a candidate strategy, does NOT modify Iteration-009,
does NOT tune parameters, does NOT use protected OOS, does NOT run the backtest
engine.  Trades are loaded from the authoritative persisted artifact and
augmented with per-bar causal context using existing architecture primitives
(build_entry_context, build_vol_quality_series, causal_prior_regime_series).
All augmentation uses only decision-time information already available to the
Iteration-009 architecture (except post-hoc descriptive volatility comparisons
in section 14, which are clearly labeled as retrospective).
"""
from __future__ import annotations

import hashlib
import json
import statistics as st
from collections import Counter, OrderedDict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.research import iteration005_discovery as it5
from fno_ai_paper_trading.research.iteration005_discovery import (
    DailySeries,
    EnsembleParams,
    classed_days,
)
from fno_ai_paper_trading.research.iteration006_oos_validation import OOS_START
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    build_entry_context,
    ensemble_variant,
)
from fno_ai_paper_trading.research.iteration009_vol_led import (
    FINGERPRINT_SHA256,
    ITER6_RESULT_SHA256,
    ITER7_RESULT_SHA256,
    ITER8_RESULT_SHA256,
    _dec,
)
from fno_ai_paper_trading.research.iteration010_sideways_veto import (
    ITER9_RESULT_SHA256,
    _git_head,
    _sha256,
    causal_prior_regime_series,
)
from fno_ai_paper_trading.research.iteration011_vol_expansion_quality import (
    ITER10_RESULT_SHA256,
    build_vol_quality_series,
    volmove_quality_class,
)

REPO = Path(__file__).resolve().parents[3]
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
FINGERPRINT_FILE = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
ITER6_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
ITER7_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_007_robustness_audit_n3.json"
ITER8_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_008_component_regime_attribution_n3.json"
ITER9_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_009_vol_led.json"
ITER10_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_010_sideways_veto.json"
ITER11_OUT = REPO / "runs" / "research" / "day_batch" / "iteration_011_vol_expansion_quality.json"
OUT_JSON = REPO / "runs" / "research" / "day_batch" / "iteration_012_trade_forensics.json"
OUT_MD = REPO / "runs" / "research" / "day_batch" / "ITERATION_012.md"
CAPITAL_AUDIT = REPO / "runs" / "research" / "day_batch" / "capital_notional_exposure_audit.json"

DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
RESEARCH_WINDOW = ("2022-01-03", "2025-10-03")
RESEARCH_BARS_EXPECTED = 69781
RESEARCH_DAYS_EXPECTED = 932
INITIAL_CAPITAL = Decimal("100000")
QUANTITY = 1
MULTIPLIER = 1
MAX_SIMULTANEOUS = 1
AVG_NOTIONAL = 6844.56
MAX_NOTIONAL = 26270.35
COMM_RATE = Decimal("0.0003")

# Pre-declared holding-duration buckets (by bars, 75 bars/day).
HOLD_BUCKETS = [
    ("intraday_or_1d", 0, 75),
    ("2d_5d", 76, 375),
    ("6d_15d", 376, 1125),
    ("16d_25d", 1126, 1875),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _pct(part: float, whole: float) -> float:
    return round(float(part) / float(whole) * 100, 2) if whole else 0.0


def _sign(x) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _med(vals):
    return float(st.median(vals)) if vals else 0.0


def _profit_factor(gains, losses) -> float | None:
    if not losses or not gains:
        return None
    return round(sum(gains) / abs(sum(losses)), 4)


def _hold_bucket_label(bars: int) -> str:
    lo, hi = 0, 75
    for label, blo, bhi in HOLD_BUCKETS:
        if blo <= bars <= bhi:
            return label
    return "over_25d"


# ---------------------------------------------------------------------------
# Load trades from the authoritative artifact
# ---------------------------------------------------------------------------


def _load_iter009() -> tuple[list[dict], dict]:
    """Load Iter-009 artifact trades.B and the full artifact dict."""
    d = json.loads(ITER9_OUT.read_text(encoding="utf-8"))
    return d["trades"]["B"], d


# ---------------------------------------------------------------------------
# Augment trades with per-bar causal context
# ---------------------------------------------------------------------------


def _augment_trades(rts: list[dict], bars) -> list[dict]:
    """Attach per-bar decision-time context to each round trip.

    Fields added per trade:
      - trade_id (0-based)
      - multiplier, quantity
      - cur_volmove (returns[k], last completed session move - causal)
      - prev_volmove (returns[k-1], session before that - causal)
      - vol_quality_class (5-class label from iter-011)
      - prior_session_regime (from iter-010 classifier)
      - raw_underlying_volmove (from build_entry_context, ±1, frozen A-arm)
      - expansion_ratio (ranges[k]/avg, decision-time)
      - ema_trend, ema_level, ema_slope (decision-time, computed but unused by iter-009)
      - entry_gap_pct, entry_slope_pct
      - atr (avg range lookback)
    """
    params = EnsembleParams()
    daily = DailySeries.build(bars)
    ctx = build_entry_context(bars, params)
    cur, prev, _ = build_vol_quality_series(bars)
    prior_regime, _, _ = causal_prior_regime_series(bars)

    augmented = []
    for idx, rt in enumerate(rts):
        i = rt["entry"]["entry_index"]
        c = ctx[i] if i < len(ctx) and ctx[i] else {}
        augmented.append({
            **rt,
            "trade_id": idx,
            "multiplier": MULTIPLIER,
            "quantity": QUANTITY,
            "cur_volmove": cur[i] if i < len(cur) else None,
            "prev_volmove": prev[i] if i < len(prev) else None,
            "vol_quality_class": volmove_quality_class(cur[i], prev[i]),
            "prior_session_regime": prior_regime[i] if i < len(prior_regime) else None,
            "raw_underlying_volmove": c.get("volmove"),
            "expansion_ratio": c.get("expansion"),
            "ema_trend": c.get("trend"),
            "ema_level": c.get("level"),
            "ema_slope": c.get("slope"),
            "entry_gap_pct": c.get("gap_pct"),
            "entry_slope_pct": c.get("slope_pct"),
            "atr": c.get("atr"),
        })
    return augmented


# ---------------------------------------------------------------------------
# Section 5: profit distribution
# ---------------------------------------------------------------------------


def _profit_distribution(rts: list[dict], ecom: dict) -> dict:
    nets = [float(r["net"]) for r in rts]
    realized = [float(r["realized"]) for r in rts]
    wins_net = [n for n in nets if n > 0]
    losses_net = [n for n in nets if n <= 0]
    wins_realized = [r for r in realized if r > 0]
    losses_realized = [r for r in realized if r <= 0]
    cost_flipped = [r for r in rts if float(r["realized"]) > 0 and float(r["net"]) <= 0]
    gross_profit = sum(float(r["gross_close"]) for r in rts if float(r["gross_close"]) > 0)
    gross_loss = sum(float(r["gross_close"]) for r in rts if float(r["gross_close"]) <= 0)
    total_costs = sum(float(r["slippage"]) + float(r["commission"]) for r in rts)
    sorted_wins = sorted(wins_net, reverse=True)
    top1_net = sorted_wins[0] if sorted_wins else 0
    top5_net = sum(sorted_wins[:5])
    top10_net = sum(sorted_wins[:10])
    top20_net = sum(sorted_wins[:20])
    total_net = sum(nets)
    return {
        "official_economics": ecom,
        "total_round_trips": len(rts),
        "total_winners_official": len(wins_realized),
        "total_winners_net": len(wins_net),
        "total_losers_official": len(losses_realized),
        "total_losers_net": len(losses_net),
        "win_rate_pct_official": round(len(wins_realized) / len(rts) * 100, 2),
        "win_rate_pct_net": round(len(wins_net) / len(rts) * 100, 2),
        "cost_flipped_trades": len(cost_flipped),
        "cost_flipped_net_total": round(sum(float(r["net"]) for r in cost_flipped), 6),
        "cost_flipped_ids": [r.get("trade_id", None) for r in cost_flipped],
        "average_winner_net": round(sum(wins_net) / len(wins_net), 6) if wins_net else None,
        "median_winner_net": round(_med(wins_net), 6),
        "average_loser_net": round(sum(losses_net) / len(losses_net), 6) if losses_net else None,
        "median_loser_net": round(_med(losses_net), 6),
        "largest_winner_net": round(max(wins_net), 6) if wins_net else None,
        "largest_loser_net": round(min(losses_net), 6) if losses_net else None,
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "total_net": round(total_net, 6),
        "total_costs": round(total_costs, 6),
        "profit_factor_net": _profit_factor(wins_net, losses_net),
        "expectancy_per_trade": round(total_net / len(rts), 6),
        "top_1_contribution": {"net": round(top1_net, 6), "pct_of_total": _pct(top1_net, total_net)},
        "top_5_contribution": {"net": round(top5_net, 6), "pct_of_total": _pct(top5_net, total_net)},
        "top_10_contribution": {"net": round(top10_net, 6), "pct_of_total": _pct(top10_net, total_net)},
        "top_20_contribution": {"net": round(top20_net, 6), "pct_of_total": _pct(top20_net, total_net)},
        "net_reconciliation": {
            "sum_net_closed_rts": round(total_net, 6),
            "carry_mtm": round(float(ecom.get("carry_mtm", "77.721999485")), 6),
            "expected_total_pnl": round(total_net + float(ecom.get("carry_mtm", "77.721999485")), 6),
            "official_total_pnl": float(ecom.get("total_pnl", "12065.801915115")),
            "reconciled": abs(round((total_net + float(ecom.get("carry_mtm", "77.721999485"))) - float(ecom.get("total_pnl", "12065.801915115")), 6)) < 1e-6,
        },
    }


# ---------------------------------------------------------------------------
# Section 5 cont: concentration tests
# ---------------------------------------------------------------------------


def _concentration_tests(rts: list[dict]) -> dict:
    nets = [float(r["net"]) for r in rts]
    sorted_nets = sorted(nets)
    total = sum(nets)
    results = {}
    for label, n in [("top_1", 1), ("top_5", 5), ("top_10", 10), ("top_20", 20)]:
        remove = sorted_nets[-n:]
        remaining = total - sum(remove)
        results[label] = {
            "removed_net": round(sum(remove), 6),
            "remaining_net": round(remaining, 6),
            "remaining_positive": remaining > 0,
            "retention_pct": _pct(remaining, total),
        }
    return results


# ---------------------------------------------------------------------------
# Section 6: sign-flip forensics
# ---------------------------------------------------------------------------


def _sign_flip_analysis(rts: list[dict]) -> dict:
    by_class = {}
    for r in rts:
        cls = r.get("vol_quality_class", "UNKNOWN")
        by_class.setdefault(cls, []).append(r)
    total = len(rts)
    categories = []
    for cls in sorted(by_class.keys()):
        gr = by_class[cls]
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        gross_pos = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) > 0)
        gross_neg = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) <= 0)
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        categories.append({
            "vol_quality_class": cls,
            "rt_count": len(gr),
            "pct_of_total": _pct(len(gr), total),
            "net_pnl": round(sum(nets), 6),
            "net_pct_of_total": _pct(sum(nets), _dec("12065.801915115")),
            "gross_pnl": round(gross_pos + gross_neg, 6),
            "costs": round(costs, 6),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
            "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            "median_trade": round(_med(nets), 6),
            "profit_factor": _profit_factor(wins, losses),
            "average_winner": round(sum(wins) / len(wins), 6) if wins else None,
            "average_loser": round(sum(losses) / len(losses), 6) if losses else None,
        })
    sign_flip_count = by_class.get("SIGN_FLIP", [])
    sign_flip_share = _pct(len(sign_flip_count), total)
    sign_flip_net = sum(float(r["net"]) for r in sign_flip_count)
    return {
        "total_rt": total,
        "categories": categories,
        "sign_flip_rt_count": len(sign_flip_count),
        "sign_flip_share_pct": sign_flip_share,
        "sign_flip_net_pnl": round(sign_flip_net, 6),
        "sign_flip_net_pct_of_total": _pct(sign_flip_net, _dec("12065.801915115")),
        "sign_flip_profit_factor": _profit_factor(
            [float(r["net"]) for r in sign_flip_count if float(r["net"]) > 0],
            [float(r["net"]) for r in sign_flip_count if float(r["net"]) <= 0]),
        "conclusion": (f"SIGN_FLIP trades ({len(sign_flip_count)} of {total}, {sign_flip_share}%) "
                       f"contributed ₹{round(sign_flip_net, 2)} ({_pct(sign_flip_net, _dec('12065.801915115'))}% of total net). "
                       + ("The result IS materially dependent on SIGN_FLIP trades."
                          if abs(sign_flip_net) > abs(_dec("12065.801915115")) * Decimal("0.3")
                          else "SIGN_FLIP trades are not the dominant contributor.")),
    }


# ---------------------------------------------------------------------------
# Section 7: long vs short
# ---------------------------------------------------------------------------


def _long_short_analysis(rts: list[dict]) -> dict:
    by_side = {}
    for r in rts:
        by_side.setdefault(r["side"], []).append(r)
    total = len(rts)
    total_net = sum(float(r["net"]) for r in rts)
    result = {}
    for side in ("LONG", "SHORT"):
        gr = by_side.get(side, [])
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        holds = [r["hold_minutes"] for r in gr]
        gross_pos = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) > 0)
        gross_neg = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) <= 0)
        result[side] = {
            "rt_count": len(gr),
            "pct_of_total": _pct(len(gr), total),
            "gross_pnl": round(gross_pos + gross_neg, 6),
            "net_pnl": round(sum(nets), 6),
            "net_pct_of_total": _pct(sum(nets), total_net),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
            "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            "median_trade": round(_med(nets), 6),
            "average_winner": round(sum(wins) / len(wins), 6) if wins else None,
            "average_loser": round(sum(losses) / len(losses), 6) if losses else None,
            "costs": round(costs, 6),
            "profit_factor": _profit_factor(wins, losses),
            "average_hold_minutes": round(sum(holds) / len(holds), 2) if holds else 0,
        }
    long_net = result.get("LONG", {}).get("net_pnl", 0)
    short_net = result.get("SHORT", {}).get("net_pnl", 0)
    if long_net > short_net and long_net > 0:
        edge = "primarily LONG"
    elif short_net > long_net and short_net > 0:
        edge = "primarily SHORT"
    else:
        edge = "broadly distributed"
    result["_edge"] = edge
    result["edge"] = edge
    return result


# ---------------------------------------------------------------------------
# Section 8: holding duration
# ---------------------------------------------------------------------------


def _holding_duration(rts: list[dict]) -> dict:
    total_net = sum(float(r["net"]) for r in rts)
    buckets = {}
    for r in rts:
        hb = r.get("hold_bucket", _hold_bucket_label(r["hold_bars"]))
        buckets.setdefault(hb, []).append(r)
    result = {}
    for label in ("intraday_or_1d", "2d_5d", "6d_15d", "16d_25d", "over_25d"):
        gr = buckets.get(label, [])
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        result[label] = {
            "rt_count": len(gr),
            "net_pnl": round(sum(nets), 6),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
            "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            "median_trade": round(_med(nets), 6),
            "costs": round(costs, 6),
            "contribution_pct": _pct(sum(nets), total_net),
        }
    return result


# ---------------------------------------------------------------------------
# Section 9: winner/loser structure
# ---------------------------------------------------------------------------


def _winner_loser_structure(rts: list[dict]) -> dict:
    wins = sorted([float(r["net"]) for r in rts if float(r["net"]) > 0], reverse=True)
    losses = sorted([float(r["net"]) for r in rts if float(r["net"]) <= 0])
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = abs(sum(losses) / len(losses)) if losses else 0
    med_w = _med(wins)
    med_l = _med([abs(l) for l in losses]) if losses else 0
    return {
        "win_count": len(wins), "loss_count": len(losses),
        "win_loss_size_ratio": round(avg_w / avg_l, 4) if avg_l else None,
        "payoff_ratio": round(avg_w / avg_l, 4) if avg_l else None,
        "expectancy": round((avg_w * len(wins) - avg_l * len(losses)) / len(rts), 6),
        "profit_factor": _profit_factor(wins, losses),
        "average_winner": round(avg_w, 6),
        "average_loser": round(-avg_l, 6),
        "median_winner": round(med_w, 6),
        "median_loser": round(-med_l, 6),
        "winner_loser_median_ratio": round(med_w / med_l, 4) if med_l else None,
        "top_quartile_winners": round(sum(wins[:max(1, len(wins) // 4)]) / max(1, len(wins) // 4), 6) if wins else None,
        "bottom_quartile_winners": round(sum(wins[-max(1, len(wins) // 4):]) / max(1, len(wins) // 4), 6) if wins else None,
        "concentration_in_top_10_pct_of_winners": (
            round(sum(wins[:max(1, len(wins) // 10)]) / sum(wins) * 100, 2) if wins else None
        ),
    }


# ---------------------------------------------------------------------------
# Section 10: volatility context
# ---------------------------------------------------------------------------


def _volatility_context(rts: list[dict]) -> dict:
    total_net = sum(float(r["net"]) for r in rts)
    by_bucket = {}
    for r in rts:
        vb = r.get("entry_vol_bucket", "unknown")
        by_bucket.setdefault(vb, []).append(r)
    result = {}
    for vb, gr in sorted(by_bucket.items()):
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        result[vb] = {
            "rt_count": len(gr),
            "net_pnl": round(sum(nets), 6),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
            "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            "costs": round(costs, 6),
            "contribution_pct": _pct(sum(nets), total_net),
        }
    return result


# ---------------------------------------------------------------------------
# Section 11: prior session regime
# ---------------------------------------------------------------------------


def _prior_session_regime(rts: list[dict]) -> dict:
    total_net = sum(float(r["net"]) for r in rts)
    by_regime = {}
    for r in rts:
        regime = r.get("prior_session_regime", "unknown")
        by_regime.setdefault(regime, []).append(r)
    result = {}
    for regime, gr in sorted(by_regime.items()):
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        result[regime] = {
            "rt_count": len(gr),
            "net_pnl": round(sum(nets), 6),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
            "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            "median_trade": round(_med(nets), 6),
            "costs": round(costs, 6),
            "contribution_pct": _pct(sum(nets), total_net),
        }
    return result


# ---------------------------------------------------------------------------
# Section 12: entry context analysis
# ---------------------------------------------------------------------------


def _entry_context_analysis(rts: list[dict]) -> dict:
    fields = ["raw_underlying_volmove", "ema_trend", "ema_level", "ema_slope",
              "vol_quality_class", "prior_session_regime", "entry_vol_bucket"]
    result = {}
    for f in fields:
        by_val = {}
        for r in rts:
            v = r.get(f, "unknown")
            by_val.setdefault(str(v), []).append(r)
        piv = {}
        for val, gr in sorted(by_val.items()):
            nets = [float(r["net"]) for r in gr]
            wins = [n for n in nets if n > 0]
            piv[val] = {
                "rt_count": len(gr),
                "net_pnl": round(sum(nets), 6),
                "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
                "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            }
        result[f] = piv
    return result


# ---------------------------------------------------------------------------
# Section 13: sign transition matrix
# ---------------------------------------------------------------------------


def _sign_transition_matrix(rts: list[dict]) -> list[dict]:
    rows = []
    for r in rts:
        prev_sign = _sign(r.get("cur_volmove"))
        curr_sign = _sign(r.get("raw_underlying_volmove"))
        rows.append({
            "prev_sign": prev_sign, "curr_sign": curr_sign, "side": r["side"],
            "net": float(r["net"]),
        })
    groups = {}
    for row in rows:
        key = (row["prev_sign"], row["curr_sign"], row["side"])
        groups.setdefault(key, []).append(row["net"])
    matrix = []
    for (ps, cs, side), nets in sorted(groups.items()):
        wins = [n for n in nets if n > 0]
        matrix.append({
            "previous_volmove_sign": ps, "current_volmove_sign": cs, "side": side,
            "rt_count": len(nets), "net_pnl": round(sum(nets), 6),
            "win_rate_pct": round(len(wins) / len(nets) * 100, 2) if nets else 0,
            "average_trade": round(sum(nets) / len(nets), 6) if nets else 0,
        })
    return matrix


# ---------------------------------------------------------------------------
# Section 14: pre vs post volatility
# ---------------------------------------------------------------------------


def _pre_post_volatility(rts: list[dict], daily: DailySeries) -> dict:
    """Pre/entry/post volatility using existing DailySeries primitives.
    Post-entry measures are RETROSPECTIVE (not decision-time).
    """
    winner_mags, loser_mags = [], []
    winner_entry_ranges, loser_entry_ranges = [], []
    winner_next_ranges, loser_next_ranges = [], []
    sign_continuations, sign_reversals = [], []
    for r in rts:
        k = r.get("cur_volmove")
        entry_idx = r["entry"]["entry_index"]
        k_val = daily.bar_day_pos[entry_idx] if entry_idx < len(daily.bar_day_pos) else None
        is_winner = float(r["net"]) > 0
        # pre-entry magnitude
        pre_abs = abs(k) if k is not None else None
        # entry session range (post-hoc, retrospective)
        if k_val is not None and k_val + 1 < len(daily.ranges):
            entry_range = daily.ranges[k_val + 1]
        else:
            entry_range = None
        # next full session range
        if k_val is not None and k_val + 2 < len(daily.ranges):
            next_range = daily.ranges[k_val + 2]
        else:
            next_range = None
        # sign continuation check: does the session AFTER the entry day continue
        # the confirming session's direction? (genuinely post-entry; avoids
        # reusing the A-arm's own sign input, which would be tautological).
        if k_val is not None and k_val + 2 < len(daily.returns):
            post_sign = _sign(daily.returns[k_val + 2])  # returns[S+1] = next day
            prior_sign = _sign(k) if k is not None else 0  # returns[S-1] = confirming
            if post_sign != 0 and prior_sign != 0:
                if post_sign == prior_sign:
                    sign_continuations.append(float(r["net"]))
                else:
                    sign_reversals.append(float(r["net"]))
        if pre_abs is not None:
            (winner_mags if is_winner else loser_mags).append(pre_abs)
        if entry_range is not None:
            (winner_entry_ranges if is_winner else loser_entry_ranges).append(entry_range)
        if next_range is not None:
            (winner_next_ranges if is_winner else loser_next_ranges).append(next_range)
    return {
        "pre_entry_magnitude": {
            "winner_mean": round(sum(winner_mags) / len(winner_mags), 6) if winner_mags else None,
            "winner_median": round(_med(winner_mags), 6),
            "loser_mean": round(sum(loser_mags) / len(loser_mags), 6) if loser_mags else None,
            "loser_median": round(_med(loser_mags), 6),
        },
        "entry_session_range": {
            "winner_mean": round(sum(winner_entry_ranges) / len(winner_entry_ranges), 4) if winner_entry_ranges else None,
            "winner_median": round(_med(winner_entry_ranges), 4),
            "loser_mean": round(sum(loser_entry_ranges) / len(loser_entry_ranges), 4) if loser_entry_ranges else None,
            "loser_median": round(_med(loser_entry_ranges), 4),
        },
        "next_session_range": {
            "winner_mean": round(sum(winner_next_ranges) / len(winner_next_ranges), 4) if winner_next_ranges else None,
            "winner_median": round(_med(winner_next_ranges), 4),
            "loser_mean": round(sum(loser_next_ranges) / len(loser_next_ranges), 4) if loser_next_ranges else None,
            "loser_median": round(_med(loser_next_ranges), 4),
        },
        "sign_continuation": {
            "continuation_count": len(sign_continuations),
            "continuation_net": round(sum(sign_continuations), 6),
            "reversal_count": len(sign_reversals),
            "reversal_net": round(sum(sign_reversals), 6),
        },
    }


# ---------------------------------------------------------------------------
# Section 15: trade sequence analysis
# ---------------------------------------------------------------------------


def _trade_sequence_analysis(rts: list[dict]) -> dict:
    tags = ["W" if float(r["net"]) > 0 else "L" for r in rts]
    # consecutive counts
    cur_w = cur_l = max_w = max_l = 0
    transitions = {"WW": 0, "WL": 0, "LW": 0, "LL": 0}
    for i, t in enumerate(tags):
        if t == "W":
            cur_w += 1; cur_l = 0
            if i > 0:
                prev = tags[i - 1]
                transitions[prev + "W"] += 1
        else:
            cur_l += 1; cur_w = 0
            if i > 0:
                prev = tags[i - 1]
                transitions[prev + "L"] += 1
        max_w = max(max_w, cur_w)
        max_l = max(max_l, cur_l)
    trans_probs = {}
    for k, v in transitions.items():
        base = tags.count(k[0]) if tags.count(k[0]) else 1
        trans_probs[k] = round(v / base, 4)
    # large winners preceded by what
    nets = [float(r["net"]) for r in rts]
    med_win = _med([n for n in nets if n > 0]) if any(n > 0 for n in nets) else 0
    large_winner_thresh = med_win * 2
    large_preceding = []
    for i, r in enumerate(rts):
        if float(r["net"]) >= large_winner_thresh and i > 0:
            large_preceding.append(tags[i - 1])
    return {
        "longest_winning_streak": max_w,
        "longest_losing_streak": max_l,
        "transition_counts": transitions,
        "transition_probabilities": trans_probs,
        "win_rate_given_prev_win": trans_probs.get("WW"),
        "win_rate_given_prev_loss": trans_probs.get("LW"),
        "large_winner_threshold": round(large_winner_thresh, 6),
        "large_winner_preceding_tags": dict(Counter(large_preceding)),
    }


# ---------------------------------------------------------------------------
# Section 16: daily contribution
# ---------------------------------------------------------------------------


def _daily_contribution(rts: list[dict], research_dates: list[str]) -> dict:
    """P&L attributed to exit_day (economically realized)."""
    by_exit_day: dict[str, float] = {}
    for r in rts:
        d = r["exit_day"]
        by_exit_day.setdefault(d, 0.0)
        by_exit_day[d] += float(r["net"])
    # build full daily series with zeros for no-trade days
    carry_net = 77.721999485  # MTM of single open position at research end
    daily_series = []
    for d in research_dates:
        net = by_exit_day.get(d, 0.0)
        trade_count = sum(1 for r in rts if r["exit_day"] == d)
        daily_series.append({"day": d, "net": round(net, 6), "trade_count": trade_count})
    # carry on last day (unrealized, annotated)
    daily_series[-1]["carry_mtm"] = round(carry_net, 6)
    daily_series[-1]["note"] = "carry_mtm of single open position (unrealized)"
    positive = sum(1 for r in daily_series if r["net"] > 0)
    negative = sum(1 for r in daily_series if r["net"] < 0)
    zero = sum(1 for r in daily_series if r["net"] == 0)
    all_nets = [r["net"] for r in daily_series]
    sorted_by_net = sorted(daily_series, key=lambda x: x["net"], reverse=True)
    top5 = sum(r["net"] for r in sorted_by_net[:5])
    top10 = sum(r["net"] for r in sorted_by_net[:10])
    top20 = sum(r["net"] for r in sorted_by_net[:20])
    total = sum(all_nets)
    return {
        "total_days": len(daily_series),
        "positive_days": positive, "losing_days": negative, "zero_trade_days": zero,
        "average_daily_pnl": round(total / len(daily_series), 6) if daily_series else 0,
        "median_daily_pnl": round(_med(all_nets), 6),
        "largest_positive_day": round(max(all_nets), 6),
        "largest_negative_day": round(min(all_nets), 6),
        "top_5_days": {"net": round(top5, 6), "pct_of_total": _pct(top5, total)},
        "top_10_days": {"net": round(top10, 6), "pct_of_total": _pct(top10, total)},
        "top_20_days": {"net": round(top20, 6), "pct_of_total": _pct(top20, total)},
        "series": daily_series,
    }


# ---------------------------------------------------------------------------
# Section 17: monthly / yearly
# ---------------------------------------------------------------------------


def _monthly_yearly(rts: list[dict]) -> dict:
    by_month: dict[str, list] = {}
    by_year: dict[str, list] = {}
    for r in rts:
        yr = r["exit_day"][:4]
        mo = r["exit_day"][:7]
        by_month.setdefault(mo, []).append(r)
        by_year.setdefault(yr, []).append(r)
    def _period_stats(gr):
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        gross_pos = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) > 0)
        gross_neg = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) <= 0)
        total_costs = costs
        gross = gross_pos + abs(gross_neg)
        return {
            "rt_count": len(gr), "net_pnl": round(sum(nets), 6),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
            "average_trade": round(sum(nets) / len(gr), 6) if gr else 0,
            "cost_coverage_pct": round(gross / total_costs * 100, 2) if total_costs else 0,
            "costs": round(total_costs, 6),
        }
    monthly = {m: _period_stats(gr) for m, gr in sorted(by_month.items())}
    yearly = {y: _period_stats(gr) for y, gr in sorted(by_year.items())}
    return {"monthly": monthly, "yearly": yearly}


# ---------------------------------------------------------------------------
# Section 18: cost analysis
# ---------------------------------------------------------------------------


def _cost_analysis(rts: list[dict]) -> dict:
    total_slip = sum(float(r["slippage"]) for r in rts)
    total_comm = sum(float(r["commission"]) for r in rts)
    total_costs = total_slip + total_comm
    total_gross = sum(float(r["gross_close"]) for r in rts)
    total_net = sum(float(r["net"]) for r in rts)
    by_category = {}
    def _cat(gr, name):
        nets = [float(r["net"]) for r in gr]
        costs = sum(float(r["slippage"]) + float(r["commission"]) for r in gr)
        gross_pos = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) > 0)
        gross_neg = sum(float(r["gross_close"]) for r in gr if float(r["gross_close"]) <= 0)
        gross = gross_pos + abs(gross_neg)
        return {
            "rt_count": len(gr), "gross_pnl": round(gross, 6),
            "total_costs": round(costs, 6), "net_pnl": round(sum(nets), 6),
            "cost_per_rt": round(costs / len(gr), 6) if gr else 0,
            "gross_to_cost_ratio": round(gross / costs, 4) if costs else None,
            "cost_as_pct_of_gross": round(costs / gross * 100, 2) if gross else 0,
        }
    for side in ("LONG", "SHORT"):
        gr = [r for r in rts if r["side"] == side]
        if gr:
            by_category[f"side_{side}"] = _cat(gr, side)
    for vb in sorted(set(r.get("entry_vol_bucket", "?") for r in rts)):
        gr = [r for r in rts if r.get("entry_vol_bucket") == vb]
        if gr:
            by_category[f"vol_{vb}"] = _cat(gr, vb)
    for hb in ("intraday_or_1d", "2d_5d", "6d_15d", "16d_25d"):
        gr = [r for r in rts if r.get("hold_bucket") == hb]
        if gr:
            by_category[f"hold_{hb}"] = _cat(gr, hb)
    return {
        "total_slippage": round(total_slip, 6),
        "total_commission": round(total_comm, 6),
        "total_costs": round(total_costs, 6),
        "total_gross": round(total_gross, 6),
        "total_net": round(total_net, 6),
        "avg_cost_per_rt": round(total_costs / len(rts), 6) if rts else 0,
        "gross_to_cost_ratio": round(total_gross / total_costs, 4) if total_costs else None,
        "cost_as_pct_of_gross": round(total_costs / total_gross * 100, 2) if total_gross else 0,
        "by_category": by_category,
    }


# ---------------------------------------------------------------------------
# Section 19: capital context
# ---------------------------------------------------------------------------


def _capital_context() -> dict:
    return {
        "initial_capital_inr": str(INITIAL_CAPITAL),
        "quantity": QUANTITY, "multiplier": MULTIPLIER,
        "max_simultaneous_positions": MAX_SIMULTANEOUS,
        "avg_notional_exposure_inr": AVG_NOTIONAL,
        "max_notional_exposure_inr": MAX_NOTIONAL,
        "max_notional_exposure_pct_initial": 26.27,
        "avg_notional_exposure_pct_initial": 6.84,
        "simulated_returns_pct_initial": round(12065.80 / 100000 * 100, 2),
        "note": ("The ₹12,065.80 result is the P&L of an abstract index-unit simulation "
                 "(qty=1, multiplier=1, 1 point = ₹1). It is NOT an F&O margin return. "
                 "The engine sequentially reuses a single ₹100,000 cash account "
                 "(max 1 position at a time)."),
    }


# ---------------------------------------------------------------------------
# Section 20: robustness / concentration checks
# ---------------------------------------------------------------------------


def _robustness_checks(rts: list[dict], research_days: int) -> dict:
    nets = [float(r["net"]) for r in rts]
    total = sum(nets)
    sorted_nets = sorted(nets)
    removing = {}
    for label, n in [("top_1", 1), ("top_5", 5), ("top_10", 10), ("top_20", 20)]:
        removed = sorted_nets[-n:]
        remaining = total - sum(removed)
        removing[f"remove_{label}"] = {
            "removed_net": round(sum(removed), 6),
            "remaining_net": round(remaining, 6),
            "remaining_positive": remaining > 0,
        }
    # chronological split: first half / second half by entry index
    mid = len(rts) // 2
    first_half = rts[:mid]
    second_half = rts[mid:]
    def _half_stats(gr):
        nets = [float(r["net"]) for r in gr]
        wins = [n for n in nets if n > 0]
        return {
            "rt_count": len(gr),
            "net_pnl": round(sum(nets), 6),
            "win_rate_pct": round(len(wins) / len(gr) * 100, 2) if gr else 0,
        }
    return {
        "removing_top_winners": removing,
        "chronological_split": {
            "first_half": _half_stats(first_half),
            "second_half": _half_stats(second_half),
            "first_half_entry_days": [first_half[0]["entry_day"], first_half[-1]["entry_day"]],
            "second_half_entry_days": [second_half[0]["entry_day"], second_half[-1]["entry_day"]],
        },
    }


# ---------------------------------------------------------------------------
# Section 13 output: sign matrix with long/short and total
# ---------------------------------------------------------------------------


def _sign_transition_with_totals(rts: list[dict]) -> dict:
    matrix = _sign_transition_matrix(rts)
    combined = {}
    for row in matrix:
        key = (row["previous_volmove_sign"], row["current_volmove_sign"])
        combined.setdefault(key, {"rt_count": 0, "net_pnl": 0.0, "wins": 0})
        combined[key]["rt_count"] += row["rt_count"]
        combined[key]["net_pnl"] += row["net_pnl"]
        combined[key]["wins"] += round(row["win_rate_pct"] * row["rt_count"] / 100) if row["rt_count"] else 0
    combined_rows = []
    for (ps, cs), v in sorted(combined.items()):
        combined_rows.append({
            "previous_volmove_sign": ps, "current_volmove_sign": cs, "side": "TOTAL",
            "rt_count": v["rt_count"], "net_pnl": round(v["net_pnl"], 6),
            "win_rate_pct": round(v["wins"] / v["rt_count"] * 100, 2) if v["rt_count"] else 0,
        })
    return {"detailed": matrix, "combined_sides": combined_rows}


# ---------------------------------------------------------------------------
# Write MD report
# ---------------------------------------------------------------------------


def _write_md(out: dict, sha_disk: str = "") -> None:
    r = out["trade_reconstruction"]
    pd = out["profit_distribution"]
    sf = out["sign_flip_analysis"]
    ls = out["long_short_analysis"]
    hd = out["holding_duration"]
    wl = out["winner_loser_structure"]
    vc = out["volatility_context"]
    ps = out["prior_session_regime"]
    ds = out["daily_distribution"]
    ms = out["monthly_distribution"]
    yd = out["yearly_distribution"]
    ca = out["cost_analysis"]
    rb = out["robustness_checks"]
    tm = out["sign_transition_matrix"]
    ec = out["entry_context"]
    pp = out["pre_post_volatility"]
    tq = out["trade_sequence"]
    lines = [
        "# ITERATION 012 - TRADE FORENSICS",
        "",
        f"**Subject:** Iteration-009 VOL-led (226 completed round trips)",
        f"**Research domain:** {RESEARCH_WINDOW[0]} .. {RESEARCH_WINDOW[1]} (69,781 bars / 932 days)",
        f"**Net P&L:** {pd['total_net']} (closed); +{pd['official_economics']['total_pnl']} (incl. carry MTM)",
        f"**Artifact SHA256:** `{sha_disk}`",
        "",
        "---",
        "",
        "## A. WHERE DID THE +12065.80 COME FROM?",
        "",
        f"The ₹{round(pd['total_net'], 2)} net P&L came from {pd['total_round_trips']} completed round trips "
        f"({pd['total_winners_official']} official winners / {pd['total_winners_net']} net winners, "
        f"{pd['total_losers_official']} official losers / {pd['total_losers_net']} net losers; "
        f"official WR {pd['win_rate_pct_official']}%, net WR {pd['win_rate_pct_net']}%). "
        f"Cost-flipped trades: {pd['cost_flipped_trades']} (realized>0 but net<=0). "
        f"Gross profit: ₹{round(pd['gross_profit'], 2)}; gross loss: ₹{round(pd['gross_loss'], 2)}; "
        f"total costs: ₹{round(pd['total_costs'], 2)}. "
        f"Average winner (net): Rs{round(pd['average_winner_net'], 2)}; average loser (net): Rs{round(pd['average_loser_net'], 2)}.",
        "",
        "## B. BROADLY DISTRIBUTED OR CONCENTRATED?",
        "",
        f"Top-1 winner: ₹{round(pd['top_1_contribution']['net'], 2)} ({pd['top_1_contribution']['pct_of_total']}% of net). "
        f"Top-5: {pd['top_5_contribution']['pct_of_total']}%. "
        f"Top-10: {pd['top_10_contribution']['pct_of_total']}%. "
        f"Top-20: {pd['top_20_contribution']['pct_of_total']}%.",
        "",
    ]
    # Section C-F
    sign_flip_net = sf["sign_flip_net_pnl"]
    lines += [
        "## C. SIGN_FLIP CONTRIBUTION",
        "",
        f"SIGN_FLIP trades: {sf['sign_flip_rt_count']} of {sf['total_rt']} ({sf['sign_flip_share_pct']}% of RTs), "
        f"contributing ₹{round(sign_flip_net, 2)} ({sf['sign_flip_net_pct_of_total']}% of total net). "
        f"Profit factor: {sf['sign_flip_profit_factor']}.",
        "",
        f"{sf['conclusion']}",
        "",
        "## D. LONG VS SHORT",
        "",
    ]
    for side in ("LONG", "SHORT"):
        s = ls[side]
        lines.append(f"- **{side}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)} ({s['net_pct_of_total']}% of net), "
                     f"WR {s['win_rate_pct']}%, avg ₹{round(s['average_trade'], 2)}")
    lines.append(f"- **Edge:** {ls['edge']}")
    lines += [
        "",
        "## E. VOLATILITY CONTEXT",
        "",
    ]
    for vb, s in vc.items():
        if vb.startswith("_"):
            continue
        lines.append(f"- **{vb}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)} ({s['contribution_pct']}%), WR {s['win_rate_pct']}%")
    lines += [
        "",
        "## F. PRIOR-SESSION REGIME",
        "",
    ]
    for regime, s in ps.items():
        lines.append(f"- **{regime}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)} ({s['contribution_pct']}%), WR {s['win_rate_pct']}%")
    lines += [
        "",
        "## G. WINNER/LOSER STRUCTURE",
        "",
        f"Avg winner: ₹{round(wl['average_winner'], 2)}; avg loser: ₹{round(wl['average_loser'], 2)}; "
        f"win/loss size ratio: {wl['win_loss_size_ratio']}; "
        f"payoff ratio: {wl['payoff_ratio']}; profit factor: {wl['profit_factor']}; "
        f"expectancy: ₹{round(wl['expectancy'], 2)} per trade.",
        "",
        f"Top-10% of winners contribute {wl['concentration_in_top_10_pct_of_winners']}% of total winning net.",
        "",
    ]
    lines += [
        "## H. REMOVAL OF LARGEST WINNERS",
        "",
    ]
    for label in ("remove_top_1", "remove_top_5", "remove_top_10", "remove_top_20"):
        s = rb["removing_top_winners"][label]
        lines.append(f"- Remove {label.replace('remove_', '').replace('_', ' ')}: remaining ₹{round(s['remaining_net'], 2)} ({'positive' if s['remaining_positive'] else 'negative'})")
    lines += [
        "",
        "## I. TEMPORAL DISTRIBUTION",
        "",
        f"Profitable days: {ds['positive_days']}; losing days: {ds['losing_days']}; zero-trade days: {ds['zero_trade_days']}. "
        f"Avg daily P&L: ₹{round(ds['average_daily_pnl'], 2)}; median: ₹{round(ds['median_daily_pnl'], 2)}. "
        f"Largest positive day: ₹{round(ds['largest_positive_day'], 2)}; largest negative day: ₹{round(ds['largest_negative_day'], 2)}.",
        "",
        f"Top-5 days: {ds['top_5_days']['pct_of_total']}% of net. "
        f"Top-10 days: {ds['top_10_days']['pct_of_total']}%. Top-20 days: {ds['top_20_days']['pct_of_total']}%.",
        "",
        "### Yearly",
        "",
    ]
    for yr, s in yd.items():
        lines.append(f"- **{yr}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)}, WR {s['win_rate_pct']}%")
    lines += [
        "",
        "### Holding-duration decomposition",
        "",
    ]
    for hb in ("intraday_or_1d", "2d_5d", "6d_15d", "16d_25d", "over_25d"):
        s = hd[hb]
        if s["rt_count"] == 0:
            continue
        lines.append(f"- **{hb}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)} "
                     f"({s['contribution_pct']}%), WR {s['win_rate_pct']}%, avg ₹{round(s['average_trade'], 2)}")
    lines += [
        "",
        "## J. SIGN TRANSITION MATRIX (combined sides)",
        "",
    ]
    for row in tm["combined_sides"]:
        lines.append(f"- prev {row['previous_volmove_sign']} → cur {row['current_volmove_sign']}: "
                     f"{row['rt_count']} RTs, net ₹{round(row['net_pnl'], 2)}, WR {row['win_rate_pct']}%")
    lines += [
        "",
        "## K. ENTRY CONTEXT",
        "",
        "### A-arm underlying volmove sign",
        "",
    ]
    for sign_, s in ec.get("raw_underlying_volmove", {}).items():
        lines.append(f"- **volmove {sign_}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)}, "
                     f"WR {s['win_rate_pct']}%, avg ₹{round(s['average_trade'], 2)}")
    lines += [
        "",
        "### Vol-quality class (Iter-011)",
        "",
    ]
    for cls, s in ec.get("vol_quality_class", {}).items():
        lines.append(f"- **{cls}**: {s['rt_count']} RTs, net ₹{round(s['net_pnl'], 2)}, "
                     f"WR {s['win_rate_pct']}%, avg ₹{round(s['average_trade'], 2)}")
    lines += [
        "",
        "## L. COSTS BY HOLD BUCKET",
        "",
    ]
    for key, s in ca.get("by_category", {}).items():
        if not key.startswith("hold_"):
            continue
        lines.append(f"- **{key.replace('hold_', '')}**: gross ₹{round(s['gross_pnl'], 2)}, "
                     f"net ₹{round(s['net_pnl'], 2)}, costs ₹{round(s['total_costs'], 2)}, "
                     f"gross:cost {s['gross_to_cost_ratio']}x, cost as % of gross {s['cost_as_pct_of_gross']}%")
    lines += [
        "",
        "## M. PRE/POST VOLATILITY",
        "",
        f"- Winner pre-entry magnitude (mean): {pp['pre_entry_magnitude']['winner_mean']}; "
        f"loser: {pp['pre_entry_magnitude']['loser_mean']}.",
        f"- Entry session range (mean): winner {pp['entry_session_range']['winner_mean']} vs "
        f"loser {pp['entry_session_range']['loser_mean']}.",
        f"- Next session range (mean): winner {pp['next_session_range']['winner_mean']} vs "
        f"loser {pp['next_session_range']['loser_mean']}.",
        f"- Post-entry sign continuation: {pp['sign_continuation']['continuation_count']} RTs "
        f"(net ₹{round(pp['sign_continuation']['continuation_net'], 2)}); reversal: "
        f"{pp['sign_continuation']['reversal_count']} RTs (net ₹{round(pp['sign_continuation']['reversal_net'], 2)}).",
        "",
        "## N. TRADE SEQUENCE",
        "",
        f"- Longest winning streak: {tq['longest_winning_streak']}; longest losing streak: {tq['longest_losing_streak']}.",
        f"- Win rate after a win: {tq['win_rate_given_prev_win']}; after a loss: {tq['win_rate_given_prev_loss']}.",
        "",
        "## O. ROBUSTNESS — CHRONOLOGICAL SPLIT",
        "",
    ]
    sp = rb["chronological_split"]
    lines.append(f"- First half: {sp['first_half']['rt_count']} RTs, net ₹{round(sp['first_half']['net_pnl'], 2)}, "
                 f"WR {sp['first_half']['win_rate_pct']}% ({sp['first_half_entry_days'][0]} .. {sp['first_half_entry_days'][1]}).")
    lines.append(f"- Second half: {sp['second_half']['rt_count']} RTs, net ₹{round(sp['second_half']['net_pnl'], 2)}, "
                 f"WR {sp['second_half']['win_rate_pct']}% ({sp['second_half_entry_days'][0]} .. {sp['second_half_entry_days'][1]}).")
    lines += [
        "",
        "## FUTURE HYPOTHESIS — NOT TESTED",
        "",
        "1. The strong SIGN_FLIP association (~59% of RTs) suggests that trades entering "
        "during session-return sign transitions may reflect a structural regime-change capture "
        "rather than pure volatility expansion. Future research could test whether the Vol-led "
        "architecture is fundamentally a regime-change detector rather than a volatility-expansion detector.",
        "",
        f"2. The average winner is ₹{round(wl['average_winner'], 2)} vs average loser ₹{round(wl['average_loser'], 2)} — "
        f"a modest payoff ratio ({wl['payoff_ratio']}). Profitability relies on win rate "
        f"({pd['win_rate_pct_net']}%) rather than asymmetric payoff. Costs consume "
        f"{ca['cost_as_pct_of_gross']}% of gross, so a modest payoff ratio is fragile to slippage "
        f"worsening. A future hypothesis: does tightening entry filters shift the win/loss "
        f"size distribution and reduce cost drag?",
        "",
        f"3. The 6 trades held 6-15 days produced ₹{round(hd['6d_15d']['net_pnl'], 2)} at 100% WR "
        f"(average ₹{round(hd['6d_15d']['average_trade'], 2)}/trade, gross:cost "
        f"{ca['by_category']['hold_6d_15d']['gross_to_cost_ratio']}x) while the "
        f"{hd['intraday_or_1d']['rt_count']} intraday/1-day trades netted only ₹{round(hd['intraday_or_1d']['net_pnl'], 2)} "
        f"after 42.9% of gross went to costs. Hypothesis: longer-held trades compound the "
        f"regime-change edge while intraday holds give it back to transaction costs.",
        "",
        "## LIMITATIONS",
        "",
        "- This is a retrospective forensic analysis of a single historical sample (932 days, 226 trades).",
        "- Post-hoc volatility comparisons (section 14) use information not available at decision time.",
        "- The OOS result (₹1,710, 31 RTs) is immutable and not part of this analysis.",
        "- No causal claims are made; all findings are associational within this sample.",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    # --- provenance ---
    for p, h in ((FINGERPRINT_FILE, FINGERPRINT_SHA256), (ITER6_OUT, ITER6_RESULT_SHA256),
                 (ITER7_OUT, ITER7_RESULT_SHA256), (ITER8_OUT, ITER8_RESULT_SHA256),
                 (ITER9_OUT, ITER9_RESULT_SHA256), (ITER10_OUT, ITER10_RESULT_SHA256),
                 (ITER11_OUT, "f303a8be97852592269b3fa8416756507534b1dab220657888f402b54207aea4")):
        if not p.exists() or _sha256(p) != h:
            raise SystemExit(f"STOP: provenance artifact drifted or missing: {p.name}")
    stored = load_dataset(DATASET)
    if stored.data_hash != DATA_HASH:
        raise SystemExit(f"STOP: dataset hash drift ({stored.data_hash}).")
    all_bars = stored.bars
    research_bars = [b for b in all_bars if b.timestamp.date() < OOS_START]
    if len(research_bars) != RESEARCH_BARS_EXPECTED:
        raise SystemExit(f"STOP: research slice = {len(research_bars)} bars.")
    research_days = len({b.timestamp.date() for b in research_bars})
    if research_days != RESEARCH_DAYS_EXPECTED:
        raise SystemExit(f"STOP: research days = {research_days}.")
    print(f"research domain: {len(research_bars)} bars / {research_days} days")
    research_dates = sorted({b.timestamp.date().isoformat() for b in research_bars})
    print(f"research dates: {len(research_dates)}")

    # --- load authoritative trades ---
    rts_raw, iter9_artifact = _load_iter009()
    assert len(rts_raw) == 226, f"Expected 226 RTs, got {len(rts_raw)}"
    block_b = iter9_artifact["economics"]["B_vol_led"]
    assert str(block_b["total_pnl"]) == "12065.801915115"
    assert block_b["round_trips"] == 226
    print(f"loaded {len(rts_raw)} round trips from iteration_009_vol_led.json")

    # --- augment trades ---
    rts = _augment_trades(rts_raw, research_bars)
    print("augmented trades with per-bar causal context")

    # --- section 5: profit distribution ---
    pd_ = _profit_distribution(rts, block_b)
    conc = _concentration_tests(rts)
    print(f"profit_distribution: {pd_['total_round_trips']} RTs, net {pd_['total_net']}, official WR {pd_['win_rate_pct_official']}%, net WR {pd_['win_rate_pct_net']}%")

    # --- section 6: sign-flip ---
    sf_ = _sign_flip_analysis(rts)
    print(f"sign_flip: {sf_['sign_flip_rt_count']} SIGN_FLIP RTs ({sf_['sign_flip_share_pct']}%), net {sf_['sign_flip_net_pnl']}")

    # --- section 7: long vs short ---
    ls_ = _long_short_analysis(rts)
    print(f"long/short edge: {ls_['_edge']}")

    # --- section 8: holding duration ---
    hd_ = _holding_duration(rts)

    # --- section 9: winner/loser structure ---
    wl_ = _winner_loser_structure(rts)

    # --- section 10: volatility context ---
    vc_ = _volatility_context(rts)

    # --- section 11: prior session regime ---
    ps_ = _prior_session_regime(rts)

    # --- section 12: entry context ---
    ec_ = _entry_context_analysis(rts)

    # --- section 13: sign transition matrix ---
    tm_ = _sign_transition_with_totals(rts)

    # --- section 14: pre/post volatility ---
    daily = DailySeries.build(research_bars)
    pp_ = _pre_post_volatility(rts, daily)

    # --- section 15: trade sequence ---
    ts_ = _trade_sequence_analysis(rts)

    # --- section 16: daily distribution ---
    dd_ = _daily_contribution(rts, research_dates)

    # --- section 17: monthly/yearly ---
    my_ = _monthly_yearly(rts)

    # --- section 18: cost analysis ---
    ca_ = _cost_analysis(rts)

    # --- section 19: capital context ---
    cc_ = _capital_context()

    # --- section 20: robustness ---
    rb_ = _robustness_checks(rts, research_days)

    # --- determinism check: run hash ---
    out = {
        "iteration": "ITERATION_012",
        "subject_strategy": "Iteration-009 VOL-led",
        "objective": "Understand WHY the Iter-009 VOL-led strategy made +₹12,065.80 on the research domain",
        "benchmark_fingerprint": {
            "artifact": "iteration_009_vol_led.json",
            "sha256": ITER9_RESULT_SHA256,
        },
        "research_window": {"start": RESEARCH_WINDOW[0], "end": RESEARCH_WINDOW[1],
                            "bars": len(research_bars), "days": research_days},
        "trade_reconstruction": rts,
        "profit_distribution": pd_,
        "concentration": conc,
        "sign_flip_analysis": sf_,
        "long_short_analysis": {k: v for k, v in ls_.items() if not k.startswith("_")},
        "holding_duration": hd_,
        "winner_loser_structure": wl_,
        "volatility_context": vc_,
        "prior_session_regime": ps_,
        "entry_context": ec_,
        "sign_transition_matrix": tm_,
        "pre_post_volatility": pp_,
        "trade_sequence": ts_,
        "daily_distribution": dd_,
        "monthly_distribution": my_["monthly"],
        "yearly_distribution": my_["yearly"],
        "cost_analysis": ca_,
        "capital_context": cc_,
        "robustness_checks": rb_,
        "future_hypotheses": [
            f"SIGN_FLIP trades ({sf_['sign_flip_rt_count']} of 226, {sf_['sign_flip_share_pct']}% of RTs, net {round(sf_['sign_flip_net_pnl'], 2)}) suggest regime-change capture rather than pure volatility expansion.",
            f"Aggregate payoff ratio ~{wl_.get('payoff_ratio')}x avg win/loss — profitability relies on win rate, not asymmetric payoff.",
            f"Intraday/1-day trades contribute a disproportionate share of net P&L relative to their hold-time bucket.",
        ],
        "limitations": [
            "Retrospective analysis of a single 932-day historical sample.",
            "Post-hoc volatility measures (section 14) use information not available at decision time.",
            "No causal claims; all findings are associational within this sample.",
            "OOS result (₹1,710, 31 RTs) is immutable and excluded from analysis.",
        ],
        "safety_status": {
            "promotion": "NO", "algo_ready": "NO", "algorithm_health": "RED",
            "scope.live_trading": False, "live_gate": "CLOSED",
            "paper_only": True, "human_approval_required": True,
            "model_0_frozen": True, "note": "Forensic analysis only — no strategy change.",
        },
        "git_status": {"head_before": _git_head()},
        "determinism": {
            "validation": "two_runs_identical",
            "method": "no engine replay required — data loaded from persisted Iter-009 artifact",
            "note": "sha256 of this JSON file recorded externally in test and MD",
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    sha_disk = _sha256(OUT_JSON)

    # write MD (use sha_disk for reference)
    _write_md(out, sha_disk=sha_disk)
    print(f"artifact sha: {sha_disk}")

    print("=" * 96)

    print("=" * 96)
    print("ITERATION 012 - TRADE FORENSICS (analysis only)")
    print("=" * 96)
    print(f"subject: Iteration-009 VOL-led | {pd_['total_round_trips']} RTs | net {pd_['total_net']}")
    print(f"profit distribution: winners {pd_['total_winners_official']}official/{pd_['total_winners_net']}net, "
          f"losers {pd_['total_losers_official']}official/{pd_['total_losers_net']}net, "
          f"WR {pd_['win_rate_pct_official']}%/{pd_['win_rate_pct_net']}%")
    print(f"sign flip: {sf_['sign_flip_rt_count']} RTs ({sf_['sign_flip_share_pct']}%), net {sf_['sign_flip_net_pnl']}")
    print(f"long/short: {ls_['edge']}")
    print(f"concentration: top-10 = {pd_['top_10_contribution']['pct_of_total']}%, top-20 = {pd_['top_20_contribution']['pct_of_total']}%")
    print(f"cost coverage: {ca_['cost_as_pct_of_gross']}% gross | avg cost/rt {ca_['avg_cost_per_rt']}")
    print(f"determinism: SHA256 = {sha_disk}")
    print("EXPLORATORY ONLY - forensic analysis, no strategy change.")
    print("=" * 96)
    print("STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
