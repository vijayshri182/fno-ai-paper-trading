"""Deterministic, read-only forensic helpers for the completed 5M_DIRECTIONAL_OPTIONS_EXPERIMENT.

This module contains ONLY pure functions used to turn a persisted experiment
checkpoint (fills / closures / decisions / history) into a canonical per-trade
ledger, plus the aggregation helpers that produce the report sections.

Source-of-truth rule: every trade record is derived from recorded checkpoint
fields (fill prices/commissions, closure realized, decision actions/signals,
entry approvals, OHLCV history). Nothing is estimated, interpolated or
reconstructed from raw prices beyond the exact arithmetic the engine itself
performs (documented in the module docstring of each helper).

The Donchian state-machine replication is the *same* replica already validated
4392/4392 against the checkpoint decisions; it is used solely to classify which
rule produced each ENTER decision (fresh channel breakout vs. internal-state
exit-channel reversion).

No strategy, contract or data file is modified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

FIVE = timedelta(minutes=5)
ENTRY_SIDE = {"CALL": "BUY", "PUT": "SELL"}
EXIT_SIDE = {"CALL": "SELL", "PUT": "BUY"}
DIR_SIGN = {"CALL": Decimal("1"), "PUT": Decimal("-1")}
INDEX_SIDE = {"CALL": "LONG", "PUT": "SHORT"}
DZERO = Decimal("0")

# Frozen approved signal contract (unchanged from the 15M experiment).
DONCHIAN_ENTRY_PERIOD = 20
DONCHIAN_EXIT_PERIOD = 10

# Broker model constants used by the engine (paper_broker). The ledger input is
# the recorded checkpoint; these only document the provenance of the fills.
COMMISSION_RATE = Decimal("0.0003")
SLIPPAGE_RATE = Decimal("0.001")


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _bar_by_timestamp(bars: list[dict]) -> dict[str, dict]:
    return {b["timestamp"]: b for b in bars}


def closes(bar_by_ts: dict[str, dict], moment: str, kmin: int) -> Decimal | None:
    """Close of the candle that completes ``kmin`` minutes after ``moment``.

    Bar ``timestamp`` completes at ``timestamp + 5 min``; a candle completing at
    ``moment + kmin`` therefore has timestamp ``moment + kmin - 5``.
    """
    ts = (datetime.fromisoformat(moment) + timedelta(minutes=kmin - 5)).isoformat()
    b = bar_by_ts.get(ts)
    return Decimal(b["close"]) if b else None


@dataclass
class BarDecision:
    """One row of the DonchianBreakout(20, 10) replication over a bar."""

    signal: str
    kind: str
    state_before: int
    state_after: int
    prior_hi: Decimal | None
    prior_lo: Decimal | None
    exit_hi: Decimal | None
    exit_lo: Decimal | None
    closed_over: bool = False


def replay_donchian(bars: list[dict], entry_period: int = DONCHIAN_ENTRY_PERIOD,
                    exit_period: int = DONCHIAN_EXIT_PERIOD) -> list[BarDecision]:
    """Replay the frozen Donchian state machine over chronological bars.

    State 0 = flat, 1 = long, -1 = short. Entry: close > max(prior 20 highs)
    -> BUY (long_breakout); close < min(prior 20 lows) -> SELL (short_breakout).
    Exit: while long, close < min(prior 10 lows) -> SELL (long_exit); while
    short, close > max(prior 10 highs) -> BUY (short_exit).

    Returns one decision per bar, in bar order.
    """
    out: list[BarDecision] = []
    state = 0
    for i, b in enumerate(bars):
        if i + 1 < 21:
            out.append(BarDecision("HOLD", "warmup", state, state, None, None, None, None))
            continue
        prior_hi = max(Decimal(x["high"]) for x in bars[i - entry_period:i])
        prior_lo = min(Decimal(x["low"]) for x in bars[i - entry_period:i])
        exit_lo = min(Decimal(x["low"]) for x in bars[i - exit_period:i])
        exit_hi = max(Decimal(x["high"]) for x in bars[i - exit_period:i])
        close = Decimal(b["close"])
        sb = state
        if state == 0:
            if close > prior_hi:
                state = 1
                sig, kind = "BUY", "long_breakout"
            elif close < prior_lo:
                state = -1
                sig, kind = "SELL", "short_breakout"
            else:
                sig, kind = "HOLD", "no_breakout"
        elif state == 1:
            if close < exit_lo:
                state = 0
                sig, kind = "SELL", "long_exit"
            else:
                sig, kind = "HOLD", "hold_long"
        else:
            if close > exit_hi:
                state = 0
                sig, kind = "BUY", "short_exit"
            else:
                sig, kind = "HOLD", "hold_short"
        out.append(BarDecision(sig, kind, sb, state, prior_hi, prior_lo, exit_hi, exit_lo))
    return out


SIGNAL_MAP = {"BUY": "BULLISH", "SELL": "BEARISH", "HOLD": "NEUTRAL"}


def replay_matches_decisions(bars: list[dict], decisions: list[dict],
                             bar_decisions: list[BarDecision]) -> tuple[int, int]:
    """Compare the replica's mapped signals with recorded decisions.

    A decision at moment M corresponds to the bar that completed at M (bar
    timestamp M - 5m). Returns (compared, mismatches).
    """
    dec_by_moment = {d["moment"]: d for d in decisions}
    compared = mismatches = 0
    for i, b in enumerate(bars):
        moment = (datetime.fromisoformat(b["timestamp"]) + FIVE).isoformat()
        dec = dec_by_moment.get(moment)
        if dec is None:
            continue
        compared += 1
        if SIGNAL_MAP[bar_decisions[i].signal] != dec["signal"]:
            mismatches += 1
    return compared, mismatches


def pair_round_trips(closures: list[dict], fills: list[dict], entry_approvals: list[dict],
                     bars: list[dict]) -> list[dict]:
    """Pair every closure with its entry and exit fills and the market candles.

    Returns one dict per completed trade with the raw recorded fields
    (identity, legs, times, prices, quantities, candle OHLCV, approval close,
    recorded realized, commissions). P&L/cost arithmetic is applied by the
    dedicated helpers below.
    """
    bar_by_ts = _bar_by_timestamp(bars)
    fill_key = {(f["filled_at"], f["leg"], f["side"]): f for f in fills}
    app_by_filled = {a["filled_at"]: a for a in entry_approvals}
    trades: list[dict] = []
    for cl in sorted(closures, key=lambda c: c["entered_at"]):
        leg = cl["leg"]
        ent, ext = cl["entered_at"], cl["exited_at"]
        ef = fill_key[(ent, leg, ENTRY_SIDE[leg])]
        xf = fill_key[(ext, leg, EXIT_SIDE[leg])]
        ent_candle_ts = (datetime.fromisoformat(ent) - FIVE).isoformat()
        ext_candle_ts = (datetime.fromisoformat(ext) - FIVE).isoformat()
        eb = bar_by_ts[ent_candle_ts]
        xb = bar_by_ts[ext_candle_ts]
        app = app_by_filled.get(ent, eb)
        trades.append({
            "date": ent[:10],
            "entered_at": ent,
            "exited_at": ext,
            "leg": leg,
            "index_side": INDEX_SIDE[leg],
            "quantity": int(cl["quantity"]),
            "entry_fill_order_id": ef["order_id"],
            "entry_fill_side": ef["side"],
            "exit_fill_order_id": xf["order_id"],
            "entry_price": Decimal(ef["price"]),
            "exit_price": Decimal(xf["price"]),
            "entry_close": Decimal(app["close"]),
            "exit_close": Decimal(xb["close"]),
            "entry_candle": {k: Decimal(eb[k]) for k in ("open", "high", "low", "close")},
            "exit_candle": {k: Decimal(xb[k]) for k in ("open", "high", "low", "close")},
            "entry_volume": eb.get("volume"),
            "entry_open_interest": eb.get("open_interest"),
            "exit_volume": xb.get("volume"),
            "exit_open_interest": xb.get("open_interest"),
            "entry_commission": Decimal(ef["commission"]),
            "exit_commission": Decimal(xf["commission"]),
            "recorded_realized": Decimal(cl["realized"]),
            "holding_minutes": int(cl["minutes"]),
            "closure_kind": cl["kind"],
        })
    return trades


def gross_close_pnl(t: dict) -> Decimal:
    """Spread-free P&L: (exit close - entry close) * qty * direction."""
    return (t["exit_close"] - t["entry_close"]) * t["quantity"] * DIR_SIGN[t["leg"]]


def realized_pnl(t: dict) -> Decimal:
    """Fill-based gross P&L: (exit fill - entry fill) * qty * direction."""
    return (t["exit_price"] - t["entry_price"]) * t["quantity"] * DIR_SIGN[t["leg"]]


def slippage_cost(t: dict) -> Decimal:
    """Modeled 0.1%/leg slippage: qty * (|entry fill - entry close| + |exit fill - exit close|)."""
    return (abs(t["entry_price"] - t["entry_close"]) + abs(t["exit_price"] - t["exit_close"])) * t["quantity"]


def spread_cost(t: dict) -> Decimal:
    """No explicit bid/ask spread exists in the engine cost model; always 0."""
    return DZERO


def commission_cost(t: dict) -> Decimal:
    """Recorded commissions on both fills (0.03% of notional per leg)."""
    return t["entry_commission"] + t["exit_commission"]


def net_pnl(t: dict) -> Decimal:
    """Net of commission only (slippage is already embedded in fill prices)."""
    return realized_pnl(t) - commission_cost(t)


def total_cost(t: dict) -> Decimal:
    """slippage + spread + commission; equals gross_close_pnl - net_pnl."""
    return slippage_cost(t) + spread_cost(t) + commission_cost(t)


def forward_entry(t: dict, bar_by_ts: dict, horizons: Iterable[int] = (5, 10, 15, 20, 25, 30)) -> dict:
    """Forward closes/fills/close-based moves and direction correctness after entry."""
    out = {}
    sign = DIR_SIGN[t["leg"]]
    for h in horizons:
        fc = closes(bar_by_ts, t["entered_at"], h)
        rec = {"close": fc}
        if fc is not None:
            rec["move_fill"] = (fc - t["entry_price"]) * sign
            rec["move_close"] = (fc - t["entry_close"]) * sign
            rec["correct_fill"] = rec["move_fill"] > DZERO
            rec["correct_close"] = rec["move_close"] > DZERO
            rec["realized_if_exit_here"] = (fc - t["entry_price"]) * sign * t["quantity"]
        out[h] = rec
    return out


def mfe_mae(t: dict, bars: list[dict], bar_by_ts: dict) -> dict:
    """MFE/MAE over the held bars (entry candle excluded, exit candle included).

    Two bases, exactly as the engine measures P&L:
      * vs fill  -> net of modeled slippage (worse)
      * vs close -> gross / spread-free (from the entry candle close)
    Metrices are in index points (not rupees).
    """
    ent_candle_ts = (datetime.fromisoformat(t["entered_at"]) - FIVE).isoformat()
    ext_candle_ts = (datetime.fromisoformat(t["exited_at"]) - FIVE).isoformat()
    held = [b for b in bars if ent_candle_ts < b["timestamp"] <= ext_candle_ts]
    sign = DIR_SIGN[t["leg"]]
    ep, ec = t["entry_price"], t["entry_close"]
    fav, adv, fav_c, adv_c = [], [], [], []
    for b in held:
        hi, lo = Decimal(b["high"]), Decimal(b["low"])
        fav.append((hi - ep) * sign)
        adv.append((ep - lo) * sign)
        fav_c.append((hi - ec) * sign)
        adv_c.append((ec - lo) * sign)
    out = {
        "n_held_bars": len(held),
        "mfe": max(fav) if fav else DZERO,
        "mae": max(adv) if adv else DZERO,
        "mfe_close": max(fav_c) if fav_c else DZERO,
        "mae_close": max(adv_c) if adv_c else DZERO,
        "mfe_timestamp": None,
        "mae_timestamp": None,
        "mfe_at_exit_candle": False,
        "mae_at_exit_candle": False,
        "n_held_favorable": sum(1 for x in fav if x > DZERO),
        "n_held_favorable_close": sum(1 for x in fav_c if x > DZERO),
    }
    if held:
        mfe_ts = held[max(range(len(fav)), key=lambda i: fav[i])]["timestamp"]
        mae_ts = held[max(range(len(adv)), key=lambda i: adv[i])]["timestamp"]
        out["mfe_timestamp"] = mfe_ts
        out["mae_timestamp"] = mae_ts
        out["mfe_at_exit_candle"] = mfe_ts == ext_candle_ts
        out["mae_at_exit_candle"] = mae_ts == ext_candle_ts
    # pre-computed percentages need entry price/close context
    out["mfe_pct"] = out["mfe"] * 100 / ep
    out["mae_pct"] = out["mae"] * 100 / ep
    out["mfe_close_pct"] = out["mfe_close"] * 100 / ec
    out["mae_close_pct"] = out["mae_close"] * 100 / ec
    return out


def classify_exit(t: dict, dec_by_moment: dict) -> dict:
    """Exit reason from recorded closure kind and exit decision actions."""
    dec_x = dec_by_moment.get(t["exited_at"])
    exit_signal = dec_x["signal"] if dec_x else None
    reason = None
    if t["closure_kind"] == "EOD_FLATTEN":
        reason = "EOD"
    elif dec_x and any("SWITCH" in a for a in dec_x["actions"]):
        reason = "REVERSAL"
    else:
        reason = "NEUTRAL"
    return {
        "exit_signal": exit_signal,
        "exit_reason": reason,
        "neutral_exit": reason == "NEUTRAL",
        "reversal_exit": reason == "REVERSAL",
        "stop_exit": False,
        "time_exit": reason == "EOD",
        "other_exit_reason": dec_x["reason"] if dec_x else None,
    }


def post_exit(t: dict, bar_by_ts: dict, bars: list[dict],
              horizons: Iterable[int] = (5, 10, 15, 25, 30)) -> dict:
    """What happened after exit: closed moves, max fav/adv excursion.

    Moves are in index points, signed in the trade direction. Window runs from
    the candle after the exit candle to 20 bars after entry (as in the existing
    forensic analysis).
    """
    sign = DIR_SIGN[t["leg"]]
    ext_candle_ts = (datetime.fromisoformat(t["exited_at"]) - FIVE).isoformat()
    limit_ts = (datetime.fromisoformat(t["entered_at"]) + FIVE * 20).isoformat()
    post = [b for b in bars if ext_candle_ts < b["timestamp"] <= limit_ts]
    out = {"moves": {}, "moves_close": {}}
    for h in horizons:
        fc = closes(bar_by_ts, t["exited_at"], h)
        rec = rec_c = None
        if fc is not None:
            rec = {"move": (fc - t["exit_price"]) * sign, "correct": (fc - t["exit_price"]) * sign > DZERO}
            rec_c = {"move": (fc - t["exit_close"]) * sign, "correct": (fc - t["exit_close"]) * sign > DZERO}
        out["moves"][h] = rec
        out["moves_close"][h] = rec_c
    ep, ec = t["exit_price"], t["exit_close"]
    post_fav = [(Decimal(b["high"]) - ep) * sign for b in post]
    post_adv = [(ep - Decimal(b["low"])) * sign for b in post]
    post_fav_c = [(Decimal(b["high"]) - ec) * sign for b in post]
    post_adv_c = [(ec - Decimal(b["low"])) * sign for b in post]
    out.update({
        "post_max_fav": max(post_fav) if post_fav else DZERO,
        "post_max_adv": max(post_adv) if post_adv else DZERO,
        "post_max_fav_close": max(post_fav_c) if post_fav_c else DZERO,
        "post_max_adv_close": max(post_adv_c) if post_adv_c else DZERO,
    })
    return out


def build_ledger(checkpoint: dict, bar_decisions: list[BarDecision]) -> tuple[list[dict], list[int]]:
    """Assemble the full per-trade ledger from a loaded checkpoint.

    Returns (trades, replay_index_by_trade) where replay_index_by_trade[i] is
    the bar index of the i-th trade's ENTER candle (used to attach breakout
    geometry). Trades are chronological by entered_at.
    """
    bars = checkpoint["history"]
    bar_by_ts = _bar_by_timestamp(bars)
    fills = checkpoint["fills"]
    closures = checkpoint["closures"]
    approvals = checkpoint["entry_approvals"]
    decisions = checkpoint["decisions"]
    dec_by_moment = {d["moment"]: d for d in decisions}
    tslist = [b["timestamp"] for b in bars]
    raw = pair_round_trips(closures, fills, approvals, bars)
    trades = []
    replay_idx = []
    for t in raw:
        ent_candle_ts = (datetime.fromisoformat(t["entered_at"]) - FIVE).isoformat()
        i = tslist.index(ent_candle_ts)
        rd = bar_decisions[i]
        fwd = forward_entry(t, bar_by_ts)
        mm = mfe_mae(t, bars, bar_by_ts)
        ex = classify_exit(t, dec_by_moment)
        pe = post_exit(t, bar_by_ts, bars)
        d = dict(t)
        d["entry_signal"] = dec_by_moment[t["entered_at"]]["signal"]
        d["state_before_entry"] = dec_by_moment[t["entered_at"]]["state_before"]
        d["state_after_entry"] = dec_by_moment[t["entered_at"]]["state_after"]
        d["gen_rule"] = rd.kind
        d["fresh_breakout"] = rd.kind in ("long_breakout", "short_breakout")
        d["reversion_state_entry"] = rd.kind in ("long_exit", "short_exit")
        d["exit_kind"] = t["closure_kind"]
        d["breakout_period"] = DONCHIAN_ENTRY_PERIOD
        d["exit_period"] = DONCHIAN_EXIT_PERIOD
        d["prior_hi"] = rd.prior_hi
        d["prior_lo"] = rd.prior_lo
        d["exit_hi"] = rd.exit_hi
        d["exit_lo"] = rd.exit_lo
        d["donchian_upper"] = rd.prior_hi
        d["donchian_lower"] = rd.prior_lo
        d["state_after"] = rd.state_after
        close = t["entry_close"]
        if t["leg"] == "CALL":
            level = rd.prior_hi if rd.kind == "long_breakout" else rd.exit_hi
            dist = close - level if level is not None else None
        else:
            level = rd.prior_lo if rd.kind == "short_breakout" else rd.exit_lo
            dist = level - close if level is not None else None
        d["breakout_level"] = level
        d["breakout_dist"] = dist
        d["dist_pct"] = float(dist * 100 / close) if dist is not None else None
        d["level_to_entry"] = abs(t["entry_price"] - level) if level is not None else None
        d["entry_candle_size"] = Decimal(bars[i]["high"]) - Decimal(bars[i]["low"])
        d["exit_candle_size"] = t["exit_candle"]["high"] - t["exit_candle"]["low"]
        d["gross_pnl_close"] = gross_close_pnl(t)
        d["slippage_cost"] = slippage_cost(t)
        d["spread_cost"] = spread_cost(t)
        d["commission_cost"] = commission_cost(t)
        d["total_cost"] = total_cost(t)
        d["realized_pnl"] = realized_pnl(t)
        d["net_pnl"] = net_pnl(t)
        d["holding_bars"] = t["holding_minutes"] // 5
        d.update(mm)
        d.update(ex)
        d["forward"] = {str(h): rec for h, rec in fwd.items()}
        d["post_exit"] = {
            "moves": {str(h): rec for h, rec in pe["moves"].items()},
            "moves_close": {str(h): rec for h, rec in pe["moves_close"].items()},
            "post_max_fav": pe["post_max_fav"],
            "post_max_adv": pe["post_max_adv"],
            "post_max_fav_close": pe["post_max_fav_close"],
            "post_max_adv_close": pe["post_max_adv_close"],
        }
        d["regime_at_entry"] = None
        d["regime_at_exit"] = None
        trades.append(d)
        replay_idx.append(i)
    # validate recorded realized
    for t in trades:
        if abs(t["realized_pnl"] - t["recorded_realized"]) > Decimal("0.000001"):
            raise ValueError(f"realized mismatch {t['entered_at']} {t['recorded_realized']} vs {t['realized_pnl']}")
    return trades, replay_idx


def _sum_dec(xs) -> Decimal:
    return sum(xs, DZERO)


def ticks_repr(v) -> str:
    """Canonical string form of a Decimal/int/float/None (trailing zeros preserved)."""
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return "0" if v == 0 else format(v, "f")
    if isinstance(v, float):
        return format(v, ".6f")
    return str(v)


def aggregate(trades: list[dict]) -> dict:
    """Report-level aggregation used by both the generator and the validator."""
    n = len(trades)
    wins_gross = sum(1 for t in trades if t["realized_pnl"] > DZERO)
    losses_gross = sum(1 for t in trades if t["realized_pnl"] < DZERO)
    flat_gross = n - wins_gross - losses_gross
    wins_net = sum(1 for t in trades if t["net_pnl"] > DZERO)
    realized = [t["realized_pnl"] for t in trades]
    nets = [t["net_pnl"] for t in trades]
    gross_closes = [t["gross_pnl_close"] for t in trades]
    return {
        "trades": n,
        "winners_gross_fill": wins_gross,
        "losers_gross_fill": losses_gross,
        "flat_gross_fill": flat_gross,
        "winners_net": wins_net,
        "gross_close_total": _sum_dec(gross_closes),
        "slippage_total": _sum_dec(t["slippage_cost"] for t in trades),
        "spread_total": _sum_dec(t["spread_cost"] for t in trades),
        "commission_total": _sum_dec(t["commission_cost"] for t in trades),
        "total_cost_total": _sum_dec(t["total_cost"] for t in trades),
        "realized_total": _sum_dec(realized),
        "net_total": _sum_dec(nets),
        "avg_net_per_trade": _sum_dec(nets) / n if n else DZERO,
        "net_sorted": sorted(nets),
        "realized_sorted": sorted(realized),
        "median_net": sorted(nets)[n // 2] if n else DZERO,
        "median_realized": sorted(realized)[n // 2] if n else DZERO,
        "holding_minutes_mean": sum(int(t["holding_minutes"]) for t in trades) / n if n else DZERO,
        "holding_5m_count": sum(1 for t in trades if t["holding_minutes"] == 5),
        "holding_10m_count": sum(1 for t in trades if t["holding_minutes"] == 10),
        "fresh_breakout_count": sum(1 for t in trades if t["fresh_breakout"]),
        "reversion_state_count": sum(1 for t in trades if t["reversion_state_entry"]),
        "neutral_exit_count": sum(1 for t in trades if t["exit_reason"] == "NEUTRAL"),
        "eod_exit_count": sum(1 for t in trades if t["exit_reason"] == "EOD"),
        "reversal_exit_count": sum(1 for t in trades if t["exit_reason"] == "REVERSAL"),
        "stop_exit_count": sum(1 for t in trades if t["stop_exit"]),
        "max_consecutive_losses": _max_streak(t["realized_pnl"] for t in trades),
    }


def _max_streak(signs: Iterable[Decimal]) -> int:
    best = cur = 0
    for x in signs:
        if x < DZERO:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def canonical_hash(payload: dict) -> str:
    """SHA-256 of the canonical JSON serialization (used for determinism checks)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def jsonable(value: Any) -> Any:
    """Recursively convert Decimals/floats to deterministic JSON-able values.

    Decimals become full-precision strings; floats stay floats; None/bool/int/
    str pass through; dicts/lists recurse.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        s = format(value, "f")
        return s if s != "0" else "0"
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return str(value)