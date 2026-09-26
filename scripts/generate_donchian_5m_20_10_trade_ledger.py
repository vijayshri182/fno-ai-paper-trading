#!/usr/bin/env python3
"""Generate the canonical, auditable trade-level forensic package for the
completed 5M_DIRECTIONAL_OPTIONS_EXPERIMENT (5M Donchian 20/10, ~229 trades).

Inputs (read-only):
  * persisted replay checkpoints of the UNMODIFIED experiment (runA / runB),
    byte-identical, holding the recorded fills/closures/decisions/history
  * validation_report.json (fingerprints + baseline)

Outputs (into <repo>/reports/forensics):
  * donchian_5m_20_10_trade_ledger.csv        - one row per completed trade
  * donchian_5m_20_10_trade_ledger.json       - same data, machine readable
  * donchian_5m_20_10_trade_forensics.md      - 13-section human report
  * donchian_5m_20_10_trade_forensics.html    - self-contained HTML report

Every figure is computed from recorded checkpoint fields (fill prices,
commissions, closure realized, decision signals/actions, OHLCV history). The
only replicated logic is the frozen Donchian state machine, which is validated
against all 4392 recorded decisions before being used to classify entry origin.

Nothing in the repository is modified other than the four generated reports and
this script's own outputs. No commit is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fno_ai_paper_trading.research.donchian_5m_forensics import (  # noqa: E402
    DZERO,
    DIR_SIGN,
    aggregate,
    build_ledger,
    canonical_hash,
    jsonable,
    load_json,
    replay_donchian,
    replay_matches_decisions,
    ticks_repr,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "reports" / "forensics"
DEFAULT_SOURCE = (
    Path(os.environ.get("LOCALAPPDATA", r"C:\Users\user\AppData\Local"))
    / "Temp" / "opencode" / "fno_5m_validation" / "out"
)

EXPECTED_FINGERPRINT_PREFIX = "b1be2188"
CHECKPOINT = "5m_directional_options.2025-08-14.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pct(w: int, n: int) -> float | None:
    return round(100.0 * w / n, 3) if n else None


def fdec(x) -> str:
    return ticks_repr(x)


def avg_dec(xs):
    xs = list(xs)
    return sum(xs, DZERO) / len(xs) if xs else DZERO


def med_dec(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return DZERO
    if n % 2:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2


def group_stats(group, name, base_str, base_dec) -> dict:
    n = len(group)
    if not n:
        return {"group": name, "count": 0}
    wins_gross = sum(1 for t in group if t["realized_pnl"] > DZERO)
    wins_net = sum(1 for t in group if t["net_pnl"] > DZERO)
    return {
        "group": name,
        "count": n,
        "win_rate_gross": pct(wins_gross, n),
        "win_rate_net": pct(wins_net, n),
        "avg_" + base_str: fdec(avg_dec(base_dec(t) for t in group)),
        "median_" + base_str: fdec(med_dec(base_dec(t) for t in group)),
        "sum_" + base_str: fdec(sum((base_dec(t) for t in group), DZERO)),
    }


def direction_hit_rates(trades, bar_by_ts) -> dict:
    """Entry-signal directional edge measured at completed-candle closes."""
    out = {}
    for horizon in (5, 10, 15, 30):
        rows = {"CALL": [0, 0], "PUT": [0, 0], "ALL": [0, 0]}
        for t in trades:
            fc = t["forward"][str(horizon)]["close"]
            if fc is None:
                continue
            npts = (Decimal(fc) - t["entry_close"]) * DIR_SIGN[t["leg"]]
            hit = npts > DZERO
            rows[t["leg"]][0] += int(hit)
            rows[t["leg"]][1] += 1
            rows["ALL"][0] += int(hit)
            rows["ALL"][1] += 1
        out[str(horizon)] = {
            leg: {"hits": v[0], "n": v[1], "hit_rate": pct(v[0], v[1])} for leg, v in rows.items()
        }
    return out


def signal_edge_all_decisions(decisions, bar_by_ts) -> dict:
    """Directional edge over ALL recorded decision instants (not just trades).

    Reference price = close of the candle that completed AT the decision moment
    (i.e. bar timestamp moment - 5m). A BULLISH call is correct at horizon h if
    the close h minutes later is above that reference; BEARISH below it.
    """
    from fno_ai_paper_trading.research.donchian_5m_forensics import closes, DZERO
    out = {}
    for horizon in (5, 10, 15, 30):
        rows = {"BULLISH": [0, 0], "BEARISH": [0, 0], "combined": [0, 0]}
        for d in decisions:
            sig = d["signal"]
            if sig not in rows:
                continue
            ref = closes(bar_by_ts, d["moment"], 0)
            fwd = closes(bar_by_ts, d["moment"], horizon)
            if ref is None or fwd is None:
                continue
            if sig == "BULLISH":
                hit = fwd > ref
            else:
                hit = fwd < ref
            rows[sig][0] += int(hit)
            rows[sig][1] += 1
            rows["combined"][0] += int(hit)
            rows["combined"][1] += 1
        out[str(horizon)] = {
            leg: {"hits": v[0], "n": v[1], "hit_rate": pct(v[0], v[1])} for leg, v in rows.items()
        }
    return out


def neutral_exit_forward(trades, bar_by_ts) -> dict:
    """Post-exit movement for NEUTRAL exits, from exit close (spread-free)."""
    neu = [t for t in trades if t["exit_reason"] == "NEUTRAL"]
    agg = defaultdict(list)
    for t in neu:
        for h in (5, 10, 15, 25, 30):
            rec = t["post_exit"]["moves_close"].get(str(h))
            if rec is not None:
                agg[h].append(float(rec["move"]))
    out = {"count": len(neu)}
    for h, vals in sorted(agg.items()):
        out[str(h)] = {
            "n": len(vals),
            "avg_move": round(statistics.mean(vals), 3),
            "pct_positive": pct(sum(1 for v in vals if v > 0), len(vals)),
        }
    return out


def holding_buckets(trades) -> dict:
    buckets = defaultdict(list)
    for t in trades:
        m = t["holding_minutes"]
        if m <= 5:
            key = "5"
        elif m <= 10:
            key = "10"
        elif m <= 15:
            key = "15"
        elif m <= 20:
            key = "20"
        elif m <= 25:
            key = "25"
        elif m <= 30:
            key = "30"
        else:
            key = ">30"
        buckets[key].append(t)
    out = {}
    for key in ("5", "10", "15", "20", "25", "30", ">30"):
        g = buckets[key]
        n = len(g)
        out[key] = {
            "count": n,
            "pct": pct(n, len(trades)),
            "win_rate": pct(sum(1 for t in g if t["realized_pnl"] > DZERO), n) if n else None,
            "net": fdec(sum((t["net_pnl"] for t in g), DZERO)),
            "avg_net": fdec(avg_dec(t["net_pnl"] for t in g)),
        }
    return out


def losing_streak(trades) -> dict:
    best = 0
    best_range = None
    cur = 0
    start = None
    for i, t in enumerate(trades):
        if t["realized_pnl"] < DZERO:
            if cur == 0:
                start = i
            cur += 1
            if cur > best:
                best = cur
                best_range = (start, i)
        else:
            cur = 0
    run = trades[best_range[0]:best_range[1] + 1]
    return {
        "max_consecutive_losses": best,
        "start": run[0]["date"],
        "end": run[-1]["date"],
        "start_ts": run[0]["entered_at"],
        "end_ts": run[-1]["exited_at"],
        "num_trades": len(run),
        "legs": dict(Counter(t["leg"] for t in run)),
        "entry_signals": dict(Counter(t["entry_signal"] for t in run)),
        "gen_rules": dict(Counter(t["gen_rule"] for t in run)),
        "exit_reasons": dict(Counter(t["exit_reason"] for t in run)),
        "avg_net": fdec(avg_dec(t["net_pnl"] for t in run)),
        "avg_realized": fdec(avg_dec(t["realized_pnl"] for t in run)),
        "avg_gross_close": fdec(avg_dec(t["gross_pnl_close"] for t in run)),
        "net_total": fdec(sum((t["net_pnl"] for t in run), DZERO)),
        "holding_5m": sum(1 for t in run if t["holding_minutes"] == 5),
        "sessions": len(set(t["date"] for t in run)),
        "fwd5_hit_rate": pct(sum(1 for t in run if t["forward"]["5"].get("correct_close")), len(run)),
    }


def longest_win_streak(trades) -> int:
    best = cur = 0
    for t in trades:
        if t["realized_pnl"] > DZERO:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def per_trade_observation(t: dict) -> str:
    """A compact, criterion-driven forensic interpretation for one trade."""
    parts = []
    kind = t["gen_rule"]
    if t["fresh_breakout"]:
        side = f"close > prior 20-bar high ({fdec(t['prior_hi'])})" if t["leg"] == "CALL" else f"close < prior 20-bar low ({fdec(t['prior_lo'])})"
        parts.append(f"Fresh 20-bar breakout entry ({kind}); {side}; breakout distance {fdec(t['breakout_dist'])} pts ({t['dist_pct']:.3f}% of close).")
    else:
        parts.append(f"Reversion-state entry ({kind}): entered because the strategy's internal exit channel (min 10-bar low / max 10-bar high = {fdec(t['exit_lo'])} / {fdec(t['exit_hi'])}) was crossed; not a fresh breakout.")
    mfe_c, mae_c = Decimal(t["mfe_close"]), Decimal(t["mae_close"])
    parts.append(f"Held {t['holding_minutes']} min ({t['holding_bars']} bar). Gross (close-to-close) {fdec(t['gross_pnl_close'])} INR, realized (fill) {fdec(t['realized_pnl'])} INR, net {fdec(t['net_pnl'])} INR.")
    parts.append(f"MFE {fdec(t['mfe'])} pts (fill basis) / {fdec(mfe_c)} pts (close basis); MAE {fdec(t['mae'])} / {fdec(mae_c)} close basis. MFE occurred at candle {t['mfe_timestamp']}.")
    f5 = t["forward"]["5"]
    if f5["close"] is not None:
        dc = bool(f5["correct_close"])
        mc_f = Decimal(f5["move_fill"])
        mc_c = Decimal(f5["move_close"])
        parts.append(f"+5m candle close {fdec(f5['close'])}; move {fdec(mc_f)} pts from fill / {fdec(mc_c)} pts from entry close; direction-correct vs close: {dc}.")
    ex = t["exit_reason"]
    if ex == "NEUTRAL":
        parts.append("Exit: NEUTRAL-mode (no fresh signal, no stop, no reversal, not EOD) - position closed opposite of entry on the internal state machine's return to flat.")
    elif ex == "EOD":
        parts.append("Exit: EOD flatten (15:20).")
    elif ex == "REVERSAL":
        parts.append("Exit: close-then-open reversal into the opposite direction.")
    return " ".join(parts)


def csv_row(t: dict) -> list[str]:
    fwd = t["forward"]
    post = t["post_exit"]
    row = {
        "trade_number": t["_trade_number"],
        "trade_id": t["entry_fill_order_id"],
        "date": t["date"],
        "session": t["date"],
        "entry_timestamp": t["entered_at"],
        "exit_timestamp": t["exited_at"],
        "direction": t["leg"],
        "index_side": t["index_side"],
        "quantity": t["quantity"],
        "entry_price": ticks_repr(t["entry_price"]),
        "exit_price": ticks_repr(t["exit_price"]),
        "entry_close": ticks_repr(t["entry_close"]),
        "exit_close": ticks_repr(t["exit_close"]),
        "entry_signal": t["entry_signal"],
        "exit_signal": t["exit_signal"],
        "gen_rule": t["gen_rule"],
        "fresh_breakout": t["fresh_breakout"],
        "reversion_state_entry": t["reversion_state_entry"],
        "breakout_period": t["breakout_period"],
        "exit_period": t["exit_period"],
        "breakout_level": ticks_repr(t["breakout_level"]),
        "breakout_dist": ticks_repr(t["breakout_dist"]),
        "dist_pct": t["dist_pct"],
        "level_to_entry": ticks_repr(t["level_to_entry"]),
        "donchian_upper": ticks_repr(t["donchian_upper"]),
        "donchian_lower": ticks_repr(t["donchian_lower"]),
        "exit_channel_upper": ticks_repr(t["exit_hi"]),
        "exit_channel_lower": ticks_repr(t["exit_lo"]),
        "state_before_entry": t["state_before_entry"],
        "state_after_entry": t["state_after_entry"],
        "entry_open": ticks_repr(t["entry_candle"]["open"]),
        "entry_high": ticks_repr(t["entry_candle"]["high"]),
        "entry_low": ticks_repr(t["entry_candle"]["low"]),
        "entry_volume": ticks_repr(t["entry_volume"]),
        "entry_open_interest": ticks_repr(t["entry_open_interest"]),
        "entry_candle_size": ticks_repr(t["entry_candle_size"]),
        "exit_open": ticks_repr(t["exit_candle"]["open"]),
        "exit_high": ticks_repr(t["exit_candle"]["high"]),
        "exit_low": ticks_repr(t["exit_candle"]["low"]),
        "exit_volume": ticks_repr(t["exit_volume"]),
        "exit_open_interest": ticks_repr(t["exit_open_interest"]),
        "exit_candle_size": ticks_repr(t["exit_candle_size"]),
        "holding_bars": t["holding_bars"],
        "holding_minutes": t["holding_minutes"],
        "gross_pnl_close": ticks_repr(t["gross_pnl_close"]),
        "spread_cost": ticks_repr(t["spread_cost"]),
        "slippage_cost": ticks_repr(t["slippage_cost"]),
        "commission_cost": ticks_repr(t["commission_cost"]),
        "total_cost": ticks_repr(t["total_cost"]),
        "realized_pnl": ticks_repr(t["realized_pnl"]),
        "net_pnl": ticks_repr(t["net_pnl"]),
    }
    for h in (5, 10, 15, 20, 25, 30):
        key = str(h)
        rec = fwd[key]
        row[f"fwd_{h}m_close"] = ticks_repr(rec["close"])
        row[f"fwd_{h}m_move_fill"] = ticks_repr(rec.get("move_fill"))
        row[f"fwd_{h}m_move_close"] = ticks_repr(rec.get("move_close"))
        row[f"fwd_{h}m_correct"] = bool(rec.get("correct_close")) if rec.get("close") is not None else ""
    for k in ("mfe", "mfe_pct", "mfe_close", "mfe_close_pct", "mfe_timestamp", "mfe_at_exit_candle",
              "mae", "mae_pct", "mae_close", "mae_close_pct", "mae_timestamp", "mae_at_exit_candle",
              "n_held_bars", "n_held_favorable", "n_held_favorable_close"):
        row[k] = ticks_repr(t[k]) if k in ("mfe_timestamp", "mae_timestamp") else (t[k] if isinstance(t[k], bool) or isinstance(t[k], int) else ticks_repr(t[k]))
    for k in ("exit_reason", "neutral_exit", "reversal_exit", "stop_exit", "time_exit", "exit_kind", "other_exit_reason"):
        row[k] = t[k] if isinstance(t[k], (bool, int, str)) else ticks_repr(t[k])
    for h in (5, 10, 15, 25, 30):
        rec = post["moves"].get(str(h))
        rec_c = post["moves_close"].get(str(h))
        row[f"post_{h}m_move"] = ticks_repr(rec["move"]) if rec else ""
        row[f"post_{h}m_correct"] = bool(rec["correct"]) if rec else ""
        row[f"post_{h}m_move_close"] = ticks_repr(rec_c["move"]) if rec_c else ""
    for k in ("post_max_fav", "post_max_adv", "post_max_fav_close", "post_max_adv_close"):
        row[k] = ticks_repr(post[k])
    actual_real = t["realized_pnl"]
    for h in (5, 10, 15, 20, 25, 30):
        key = str(h)
        r = fwd[key]
        if r.get("realized_if_exit_here") is not None:
            row[f"hold_improve_{h}m"] = r["realized_if_exit_here"] > actual_real
        else:
            row[f"hold_improve_{h}m"] = ""
    row["regime_at_entry"] = t["regime_at_entry"] if t["regime_at_entry"] else ""
    row["regime_at_exit"] = t["regime_at_exit"] if t["regime_at_exit"] else ""
    return row


def build_csv_columns() -> list[str]:
    cols = [
        "trade_number", "trade_id", "date", "session", "entry_timestamp", "exit_timestamp",
        "direction", "index_side", "quantity",
        "entry_price", "exit_price", "entry_close", "exit_close",
        "entry_signal", "exit_signal", "gen_rule", "fresh_breakout", "reversion_state_entry",
        "breakout_period", "exit_period", "breakout_level", "breakout_dist", "dist_pct", "level_to_entry",
        "donchian_upper", "donchian_lower", "exit_channel_upper", "exit_channel_lower",
        "state_before_entry", "state_after_entry",
        "entry_open", "entry_high", "entry_low", "entry_volume", "entry_open_interest", "entry_candle_size",
        "exit_open", "exit_high", "exit_low", "exit_volume", "exit_open_interest", "exit_candle_size",
        "holding_bars", "holding_minutes",
        "gross_pnl_close", "spread_cost", "slippage_cost", "commission_cost", "total_cost",
        "realized_pnl", "net_pnl",
    ]
    for h in (5, 10, 15, 20, 25, 30):
        cols += [f"fwd_{h}m_close", f"fwd_{h}m_move_fill", f"fwd_{h}m_move_close", f"fwd_{h}m_correct"]
    cols += [
        "mfe", "mfe_pct", "mfe_close", "mfe_close_pct", "mfe_timestamp", "mfe_at_exit_candle",
        "mae", "mae_pct", "mae_close", "mae_close_pct", "mae_timestamp", "mae_at_exit_candle",
        "n_held_bars", "n_held_favorable", "n_held_favorable_close",
        "exit_reason", "neutral_exit", "reversal_exit", "stop_exit", "time_exit", "exit_kind", "other_exit_reason",
    ]
    for h in (5, 10, 15, 25, 30):
        cols += [f"post_{h}m_move", f"post_{h}m_correct", f"post_{h}m_move_close"]
    cols += [
        "post_max_fav", "post_max_adv", "post_max_fav_close", "post_max_adv_close",
        "hold_improve_5m", "hold_improve_10m", "hold_improve_15m",
        "hold_improve_20m", "hold_improve_25m", "hold_improve_30m",
        "regime_at_entry", "regime_at_exit",
    ]
    return cols


def cell_str(v) -> str:
    if v is None or (isinstance(v, str) and v == ""):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return format(v, ".6f")
    return str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    src = Path(args.source)
    outdir = Path(args.outdir)
    ck_path_a = Path(args.checkpoint) if args.checkpoint else src / "exp_runA" / "checkpoints" / CHECKPOINT
    ck_path_b = src / "exp_runB" / "checkpoints" / CHECKPOINT
    sha_path_a = Path(str(ck_path_a) + ".sha256")
    rep_path = Path(args.report) if args.report else src / "validation_report.json"

    # ---- step 0: unambiguous identification of the expected experiment ----
    if not ck_path_a.exists() or not ck_path_b.exists():
        sys.exit(f"[FAIL] missing runA/runB cumulative checkpoints: {ck_path_a} / {ck_path_b}")
    if not sha_path_a.exists():
        sys.exit(f"[FAIL] missing stored sha256: {sha_path_a}")
    if not rep_path.exists():
        sys.exit(f"[FAIL] missing validation report: {rep_path}")
    ha, hb = sha256_file(ck_path_a), sha256_file(ck_path_b)
    stored = sha_path_a.read_text(encoding="utf-8").strip().split()[0][:64]
    if ha != stored:
        sys.exit(f"[FAIL] runA checkpoint sha mismatch stored={stored[:16]} actual={ha[:16]}")
    report = load_json(str(rep_path))
    fp1 = report.get("fingerprint_run1")
    fp2 = report.get("fingerprint_run2")
    if not (isinstance(fp1, str) and isinstance(fp2, str) and fp1 == fp2 and fp1.startswith(EXPECTED_FINGERPRINT_PREFIX)):
        sys.exit(f"[FAIL] validation-report fingerprints not the expected 5M experiment: {fp1} / {fp2}")
    if report.get("experiment_id") != "5M_DIRECTIONAL_OPTIONS_EXPERIMENT":
        sys.exit(f"[FAIL] report experiment_id = {report.get('experiment_id')}")

    ck = load_json(str(ck_path_a))
    if ck.get("experiment_id") != "5M_DIRECTIONAL_OPTIONS_EXPERIMENT":
        sys.exit(f"[FAIL] checkpoint experiment_id = {ck.get('experiment_id')}")
    if len(ck["closures"]) != 229:
        sys.exit(f"[FAIL] expected 229 closures, found {len(ck['closures'])}")
    # runB cross-check: same experiment size; order IDs may legitimately differ
    # between runs (randomly generated), so we compare fingerprint + counts.
    ck_b = load_json(str(ck_path_b))
    sha_path_b = Path(str(ck_path_b) + ".sha256")
    if not sha_path_b.exists():
        sys.exit(f"[FAIL] missing stored sha256 for runB: {sha_path_b}")
    stored_b = sha_path_b.read_text(encoding="utf-8").strip().split()[0][:64]
    if hb != stored_b:
        sys.exit(f"[FAIL] runB checkpoint sha mismatch stored={stored_b[:16]} actual={hb[:16]}")
    if ck_b.get("experiment_id") != "5M_DIRECTIONAL_OPTIONS_EXPERIMENT":
        sys.exit(f"[FAIL] runB experiment_id = {ck_b.get('experiment_id')}")
    if len(ck_b.get("closures", [])) != len(ck["closures"]) or len(ck_b.get("decisions", [])) != len(ck["decisions"]):
        sys.exit("[FAIL] runA/runB closure or decision counts differ")

    # ---- step 1: validated state-machine replay ----
    bar_decisions = replay_donchian(ck["history"])
    compared, mismatches = replay_matches_decisions(ck["history"], ck["decisions"], bar_decisions)
    if mismatches != 0 or compared != len(ck["decisions"]):
        sys.exit(f"[FAIL] state-machine replica mismatch {mismatches}/{compared}")

    trades, _ = build_ledger(ck, bar_decisions)
    if len(trades) != 229:
        sys.exit(f"[FAIL] ledger produced {len(trades)} trades, expected 229")

    # trade numbers + ids
    for i, t in enumerate(trades, start=1):
        t["_trade_number"] = i
    ids = [t["entry_fill_order_id"] for t in trades]
    if len(set(ids)) != len(ids):
        sys.exit("[FAIL] duplicate trade ids (entry order ids)")

    agg = aggregate(trades)
    gross_close_total = agg["gross_close_total"]
    slippage_total = agg["slippage_total"]
    comm_total = agg["commission_total"]
    realized_total = agg["realized_total"]
    net_total = agg["net_total"]

    # structural reconciliation (exact Decimal identities)
    if gross_close_total - slippage_total - comm_total != net_total:
        sys.exit("[FAIL] net != gross_close - slippage - commission")
    if realized_total - comm_total != net_total:
        sys.exit("[FAIL] net != realized - commission")
    for t in trades:
        if t["realized_pnl"] - t["recorded_realized"] != DZERO:
            sys.exit(f"[FAIL] recorded realized mismatch {t['entered_at']}")
        if t["gross_pnl_close"] - t["slippage_cost"] - t["commission_cost"] != t["net_pnl"]:
            sys.exit(f"[FAIL] per-trade reconciliation {t['entered_at']}")

    # ---- step 2: sections ----
    sections = {
        "entry_origin": {
            "fresh_breakout_count": agg["fresh_breakout_count"],
            "reversion_state_count": agg["reversion_state_count"],
            "total": agg["trades"],
            "by_rule": {},
            "fresh": None,
            "reversion": None,
        },
        "exit_analysis": {},
        "directional_edge": direction_hit_rates(trades, {b["timestamp"]: b for b in ck["history"]}),
        "signal_edge_all_decisions": signal_edge_all_decisions(ck["decisions"], {b["timestamp"]: b for b in ck["history"]}),
        "mfe_mae": {},
        "holding_time": holding_buckets(trades),
        "losing_streak": losing_streak(trades),
        "neutral_exit_forward": neutral_exit_forward(trades, {b["timestamp"]: b for b in ck["history"]}),
    }
    for rule in ("long_breakout", "short_breakout", "long_exit", "short_exit"):
        g = [t for t in trades if t["gen_rule"] == rule]
        sections["entry_origin"]["by_rule"][rule] = group_stats(g, rule, "net_str",
                                                                lambda t: t["net_pnl"])
    fresh = [t for t in trades if t["fresh_breakout"]]
    reversion = [t for t in trades if t["reversion_state_entry"]]
    sections["entry_origin"]["fresh"] = group_stats(fresh, "fresh_breakout", "net_str", lambda t: t["net_pnl"])
    sections["entry_origin"]["reversion"] = group_stats(reversion, "reversion_state", "net_str", lambda t: t["net_pnl"])

    for reason in ("NEUTRAL", "EOD", "REVERSAL"):
        g = [t for t in trades if t["exit_reason"] == reason]
        base = group_stats(g, reason, "net_str", lambda t: t["net_pnl"])
        base["avg_holding"] = round(sum(t["holding_minutes"] for t in g) / len(g), 2) if g else 0
        sections["exit_analysis"][reason] = base
    sections["exit_analysis"]["STOP"] = {"count": 0}

    losers = [t for t in trades if t["realized_pnl"] < DZERO]
    mfe = [Decimal(t["mfe"]) for t in trades]
    mae = [Decimal(t["mae"]) for t in trades]
    mfe_c = [Decimal(t["mfe_close"]) for t in trades]
    mae_c = [Decimal(t["mae_close"]) for t in trades]
    sections["mfe_mae"] = {
        "avg_mfe_fill": fdec(avg_dec(mfe)),
        "avg_mfe_close": fdec(avg_dec(mfe_c)),
        "median_mfe_fill": fdec(med_dec(mfe)),
        "median_mfe_close": fdec(med_dec(mfe_c)),
        "avg_mae_fill": fdec(avg_dec(mae)),
        "avg_mae_close": fdec(avg_dec(mae_c)),
        "median_mae_fill": fdec(med_dec(mae)),
        "median_mae_close": fdec(med_dec(mae_c)),
        "pct_losers_with_positive_mfe_fill": pct(sum(1 for t in losers if Decimal(t["mfe"]) > DZERO), len(losers)),
        "pct_losers_with_positive_mfe_close": pct(sum(1 for t in losers if Decimal(t["mfe_close"]) > DZERO), len(losers)),
        "losers_count": len(losers),
        "pct_losers_never_favorable_fill": pct(sum(1 for t in losers if Decimal(t["mfe"]) <= DZERO), len(losers)),
        "pct_losers_never_favorable_close": pct(sum(1 for t in losers if Decimal(t["mfe_close"]) <= DZERO), len(losers)),
    }

    # per-trade objective observations
    for t in trades:
        t["_observation"] = per_trade_observation(t)

    # ---- step 3: serializable ledger + meta ----
    trade_payload = []
    for t in trades:
        p = {k: v for k, v in t.items() if k not in ("entry_candle", "exit_candle", "_trade_number", "_observation")}
        p["entry_candle"] = {k: ticks_repr(v) for k, v in t["entry_candle"].items()}
        p["exit_candle"] = {k: ticks_repr(v) for k, v in t["exit_candle"].items()}
        p["_trade_number"] = t["_trade_number"]
        p["_observation"] = t["_observation"]
        trade_payload.append(jsonable(p))

    meta = {
        "experiment_id": "5M_DIRECTIONAL_OPTIONS_EXPERIMENT",
        "label": "5M Donchian 20/10",
        "fingerprint": fp1,
        "source_checkpoint": str(ck_path_a.resolve()),
        "source_checkpoint_sha256": ha,
        "runA_sha_matches_stored": ha == stored,
        "runB_sha_matches_stored": hb == stored_b,
        "runA_equal_runB": fp1 == fp2,
        "validation_report": str(rep_path.resolve()),
        "window": {"start": "2025-05-22", "end": "2025-08-14", "sessions": 61,
                   "expected_bars": 4575, "bars": len(ck["history"]),
                   "decision_points": len(ck["decisions"])},
        "strategy": {"name": "DonchianBreakout",
                     "entry_period": 20, "exit_period": 10,
                     "signal_adapter": "directional_5m.signal.donchian_signal_5m (reuses directional_15m.donchian_signal_15m, frozen)",
                     "decision_grid": "every completed 5-min candle 09:20..15:15 (72/day)"},
        "cost_model": {
            "commission_rate": "0.0003 (fraction of notional, recorded per fill)",
            "slippage_rate": "0.001 (fraction of price, BUY at close*1.001 / SELL at close*0.999)",
            "spread_cost_modeled": "0 (the engine has no separate bid/ask spread; the 0.1% fill impact is the modeled slippage)",
            "identity": "net_pnl == gross_pnl_close - slippage_cost - spread_cost(0) - commission_cost",
        },
        "win_convention": "winner = closure realized (fill-based gross P&L) > 0; losers = realized < 0; flat = 0. winners_net uses net-of-commission P&L.",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "trades": len(trades),
    }

    summary_payload = {k: jsonable(v) for k, v in agg.items() if not k.endswith("_sorted")}
    summary_payload["median_net"] = fdec(agg["median_net"])
    summary_payload["median_realized"] = fdec(agg["median_realized"])
    summary_payload["avg_gross_close_per_trade"] = fdec(avg_dec(Decimal(t["gross_pnl_close"]) for t in trades))
    summary_payload["median_gross_close"] = fdec(med_dec(Decimal(t["gross_pnl_close"]) for t in trades))
    summary_payload["longest_winning_streak"] = longest_win_streak(trades)
    summary_payload["longest_losing_streak"] = agg["max_consecutive_losses"]
    summary_payload["holding_5m_pct"] = pct(agg["holding_5m_count"], agg["trades"])
    summary_payload["median_holding_minutes"] = int(med_dec([Decimal(t["holding_minutes"]) for t in trades]))

    # output hash excludes generated_at and itself -> deterministic
    canon = canonical_hash({"meta": {k: v for k, v in meta.items() if k != "generated_at"},
                            "summary": summary_payload, "trades": trade_payload})
    meta["output_hash"] = canon

    ledger = {"_meta": jsonable(meta), "summary": summary_payload,
              "sections": {k: jsonable(v) for k, v in sections.items()},
              "trades": trade_payload}

    # ---- step 4: write CSV ----
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "donchian_5m_20_10_trade_ledger.csv"
    cols = build_csv_columns()
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        import csv
        w = csv.writer(fh)
        w.writerow(cols)
        for t in trades:
            row = csv_row(t)
            w.writerow([cell_str(row.get(c, "")) for c in cols])

    json_path = outdir / "donchian_5m_20_10_trade_ledger.json"
    json_path.write_text(json.dumps(ledger, indent=1, sort_keys=False), encoding="utf-8")

    # ---- step 5: write reports ----
    md_path = outdir / "donchian_5m_20_10_trade_forensics.md"
    html_path = outdir / "donchian_5m_20_10_trade_forensics.html"
    from fno_ai_paper_trading.research._donchian_5m_report import render_markdown_and_html
    md, html = render_markdown_and_html(ledger, trades, sections, meta)
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")

    print("WROTE", csv_path)
    print("WROTE", json_path)
    print("WROTE", md_path)
    print("WROTE", html_path)
    print()
    print(json.dumps({
        "trades": len(trades),
        "net_total": str(net_total),
        "realized_total": str(realized_total),
        "slippage_total": str(slippage_total),
        "commission_total": str(comm_total),
        "gross_close_total": str(gross_close_total),
        "fresh_breakout": agg["fresh_breakout_count"],
        "reversion_state": agg["reversion_state_count"],
        "neutral_exits": agg["neutral_exit_count"],
        "eod_exits": agg["eod_exit_count"],
        "reversal_exits": agg["reversal_exit_count"],
        "winners_gross": agg["winners_gross_fill"],
        "losers_gross": agg["losers_gross_fill"],
        "holding_5m": agg["holding_5m_count"],
        "holding_10m": agg["holding_10m_count"],
        "max_consecutive_losses": agg["max_consecutive_losses"],
        "replay_compared": compared,
        "replay_mismatches": mismatches,
        "fingerprint": fp1,
        "output_hash": canon,
    }, indent=1))


if __name__ == "__main__":
    main()