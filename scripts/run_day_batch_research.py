"""Day-batch research driver (WS 7.18-style, controlled iterative loop).

Runs ONE small batch of trading days through the frozen champion and the three
composite presets using the *existing* backtest engine and strategy classes
(no competing framework, no parameter re-tuning on protected OOS data).

Method
------
* Domain = every 5-minute bar strictly BEFORE the protected OOS start
  (project boundary 2025-10-06). That domain is NEVER tuned on or leaked.
* Phase 1 (days): Batch 1 = 5 representative days picked deterministically by
  market condition (trending up / down / sideways / high-vol / low-vol), or the
  next chronological days when a category is missing. Batch 2 = the 5 trading
  days immediately following Batch 1 in the pre-OOS domain (repeatability check).
* Phase 2 (replay): each strategy is replayed ONCE over the full pre-OOS domain
  (continuous, realistic — signals at 09:15 use the prior trading day's
  history; matches the five-year replay methodology). The composite uses its
  latched O(n) signal path, the champion an incremental MA-cross generator.
* Phase 3 (diagnosis): every bar of each target day is journaled (raw signal !=
  final decision), and per-day P&L / costs / trades / stops / no-trade reasons
  are attributed from the deterministic replay.
* Phase 5/6: the same script can be re-run with ONE isolated variant parameter
  (``--entry-votes``/``--adx-min``/etc.) to produce the A/B comparison on the
  same batch and the repeatability comparison on the next batch.

Artifacts (immutable, never overwritten): ``runs/research/day_batch/<run_id>/``
"""  # noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, OrderedDict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.metrics import canonical
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.research.day_batch_overlays import (
    exit_attribution,
    run_exit_quality_variant,
    verify_state_machine,
)
from fno_ai_paper_trading.strategies.base import SignalResult
from fno_ai_paper_trading.strategies.composite import MultiIndicatorStrategy, bulk_signals
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy

OOS_START = datetime(2025, 10, 6).date()  # protected OOS boundary (project-frozen)
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"


# ---------------------------------------------------------------------------
# provenance helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _config_fields(config: EvaluationConfig) -> dict[str, str]:
    return {
        "initial_capital": str(config.initial_capital),
        "quantity": config.quantity,
        "commission_rate": str(config.commission_rate),
        "commission_fixed": str(config.commission_fixed),
        "slippage_rate": str(config.slippage_rate),
        "enable_risk_manager": config.enable_risk_manager,
        "max_position_quantity": config.max_position_quantity,
        "max_order_notional": str(config.max_order_notional),
        "max_daily_loss": str(config.max_daily_loss),
        "enable_stop_loss": config.enable_stop_loss,
        "stop_loss_pct": str(config.stop_loss_pct),
    }


# ---------------------------------------------------------------------------
# incremental MA(5,21) champion signals (O(n), identical to analyze)
# ---------------------------------------------------------------------------

def champion_signals(bars: list) -> list[SignalResult]:
    fast_q, slow_q = deque(), deque()
    fast_sum = slow_sum = Decimal("0")
    fast, slow = Decimal("5"), Decimal("21")
    out: list[SignalResult] = []
    for i, bar in enumerate(bars):
        c = bar.close
        fast_prev_sum, slow_prev_sum = fast_sum, slow_sum
        fast_q.append(c); slow_q.append(c)
        fast_sum += c; slow_sum += c
        if len(fast_q) > fast:
            fast_sum -= fast_q.popleft()
        if len(slow_q) > slow:
            slow_sum -= slow_q.popleft()
        n = i + 1
        if n < slow + 1:
            out.append(SignalResult(signal=Signal.HOLD, instrument=bar.instrument,
                                    timestamp=bar.timestamp,
                                    reason=f"need at least {slow + 1} bars; have {n}"))
            continue
        fast_now = fast_sum / fast
        slow_now = slow_sum / slow
        fast_prev = fast_prev_sum / fast
        slow_prev = slow_prev_sum / slow
        meta = {"fast": str(fast_now), "slow": str(slow_now),
                "fast_prev": str(fast_prev), "slow_prev": str(slow_prev),
                "last_close": str(bar.close)}
        if fast_prev <= slow_prev and fast_now > slow_now:
            s, r = Signal.BUY, f"fast MA ({fast_now:f}) crossed above slow MA ({slow_now:f})"
        elif fast_prev >= slow_prev and fast_now < slow_now:
            s, r = Signal.SELL, f"fast MA ({fast_now:f}) crossed below slow MA ({slow_now:f})"
        else:
            s, r = Signal.HOLD, "no crossover"
        out.append(SignalResult(signal=s, instrument=bar.instrument,
                                timestamp=bar.timestamp, reason=r, meta=meta))
    return out


# ---------------------------------------------------------------------------
# day selection
# ---------------------------------------------------------------------------

def _day_stats(bars: list) -> dict:
    opens = bars[0].open
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    day_return = (closes[-1] - opens) / opens * Decimal("100")
    day_range = (max(highs) - min(lows)) / opens * Decimal("100")
    moves = [abs(b.close - p.close) / p.close * Decimal("100")
             for p, b in zip(bars, bars[1:])]
    avg_move = (sum(moves, Decimal("0")) / len(moves)) if moves else Decimal("0")
    up = sum(1 for p, b in zip(bars, bars[1:]) if b.close > p.close)
    down = len(moves) - up
    return {
        "date": bars[0].timestamp.date().isoformat(),
        "nbars": len(bars),
        "open": str(opens),
        "close": str(closes[-1]),
        "day_return_pct": str(day_return),
        "day_range_pct": str(day_range),
        "avg_5m_move_pct": str(avg_move),
        "up_bars": up,
        "down_bars": down,
    }


def classify_day(stats: dict) -> str:
    r = float(stats["day_return_pct"])
    rg = float(stats["day_range_pct"])
    if r > 0.8 and rg > 1.0 and abs(r) / rg > 0.4:
        return "trending_up"
    if r < -0.8 and rg > 1.0 and abs(r) / rg > 0.4:
        return "trending_down"
    if abs(r) < 0.25 and rg < 1.2:
        return "sideways"
    if rg >= 1.8 and abs(r) < 0.5:
        return "volatile"
    if rg <= 0.6 and abs(r) < 0.3:
        return "low_vol"
    return "mixed"


def select_batches(days: list) -> tuple[list[str], list[str], dict[str, str]]:
    """Batch 1: 5 representative days by market condition (most recent match per
    category). Batch 2: the 5 chronological trading days following batch 1.
    Returns (batch1, batch2, category_map) where category_map[date] = regime."""
    wanted = ["trending_up", "trending_down", "sideways", "volatile", "low_vol"]
    batch1_raw: list[str] = []
    seen: set[str] = set()
    cat_map: dict[str, str] = {}
    for category in wanted:
        for stats in reversed(days):  # most recent first
            date_str = stats["date"]
            if date_str in seen:
                continue
            if classify_day(stats) == category:
                seen.add(date_str)
                batch1_raw.append(date_str)
                cat_map[date_str] = category
                break
    # fall back to next chronological days when a category is missing
    fallback = 0
    while len(batch1_raw) < 5:
        stats = days[fallback]
        date_str = stats["date"]
        if date_str not in seen:
            seen.add(date_str)
            batch1_raw.append(date_str)
            cat_map[date_str] = classify_day(stats) + " (fallback)"
        fallback += 1
    batch1_raw = batch1_raw[:5]
    batch1 = sorted(batch1_raw)
    last = max(batch1)
    chrono = [s["date"] for s in days if s["date"] > last]
    batch2 = chrono[:5]
    return batch1, batch2, cat_map


# ---------------------------------------------------------------------------
# replay + attribution
# ---------------------------------------------------------------------------

def build_domain_bars(stored, max_bars: int | None = None):
    domain = [b for b in stored.bars if b.timestamp.date() < OOS_START]
    if max_bars is not None:
        domain = domain[-max_bars:]
    return domain


def replay_strategy(bars, strategy, config, engine, signals=None) -> tuple:
    """Returns (result, journal). Composite goes through its latched engine
    hook / bulk path; the champion uses the incremental crossing generator.
    ``signals`` optionally replaces the decision series (Iteration-002 ADX
    entry-confirmation arm)."""
    if signals is not None:
        pass
    elif isinstance(strategy, MovingAverageCrossStrategy):
        signals = champion_signals(bars)
    else:
        signals = bulk_signals(bars, strategy, latch=True)
    journal: list[dict] = []
    result = engine.run(bars, strategy, config, signals=signals, journal=journal)
    return result, journal


def _meta_decimal(meta, key):
    """Decimal value from a formatted meta field (''/None -> None)."""
    if not meta:
        return None
    v = meta.get(key)
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d.is_finite() else None


def confirm_entries(
    bars,
    strategy: MultiIndicatorStrategy,
    threshold: float,
) -> tuple[list[SignalResult], list[dict]]:
    """Iteration-002 ADX entry-confirmation overlay.

    The UNDERLYING composite state machine is preserved bit-for-bit: the very
    same strategy instance (same ``adx_min`` vote gate) drives the raw bias,
    and the bias is latched exactly like the baseline (enter-on-transition,
    hold while the bias persists, exit on the opposite transition). ADX is used
    only to decide WHETHER a flat->position transition may OPEN a position:

    * opposite-transition bars while positioned are EXITS and are emitted
      unconditionally (exits remain governed by the original strategy);
    * entry transitions while flat are emitted only when ADX at that bar is
      >= ``threshold``; otherwise the entry is DEFERRED (no order placed) and
      re-attempted while the same bias persists with ADX >= threshold, or
      DROPPED if the bias flips before confirmation.

    Returns ``(series, shadow)``; ``shadow`` is the per-bar (bias, pending,
    qty, adx, action, reason) audit used to verify the engine journal produced
    the intended state machine (no risk-manager block, no stop divergence).
    """
    raw = bulk_signals(bars, strategy, latch=False)
    n = len(raw)
    out: list[SignalResult] = []
    shadow: list[dict] = []
    state: Signal | None = None
    qty = 0
    pending = False
    for i, result in enumerate(raw):
        signal = result.signal
        adx = _meta_decimal(result.meta, "adx")
        action = "hold"
        reason = "latched hold (same bias, positioned)" if qty else "no actionable state"
        if signal is Signal.BUY or signal is Signal.SELL:
            if signal is not state:
                state = signal
                if qty == 0:
                    pending = True
            # same-side repeat: identical to baseline's latched hold
        if state is None:
            action, reason = "hold", "no directional bias yet"
        elif qty != 0 and (1 if state is Signal.BUY else -1) != qty:
            action, reason = "exit", "flip exit (original strategy governs)"
            qty = 0
            pending = True
        elif qty == 0 and pending:
            if adx is not None and adx >= threshold:
                action = "entry"
                reason = f"ADX entry confirmed ({adx:.2f} >= {threshold:.2f})"
                qty = 1 if state is Signal.BUY else -1
                pending = False
            else:
                action = "defer"
                reason = (
                    f"ADX {adx:.2f} < {threshold:.2f}; entry deferred"
                    if adx is not None
                    else "ADX unavailable; entry deferred"
                )
        emit_signal = Signal.BUY if state is Signal.BUY else (
            Signal.SELL if state is Signal.SELL else Signal.HOLD
        )
        if action not in ("entry", "exit"):
            emit_signal = Signal.HOLD
        out.append(
            SignalResult(
                signal=emit_signal,
                instrument=result.instrument,
                timestamp=result.timestamp,
                reason=reason,
                meta=dict(result.meta or {}),
            )
        )
        shadow.append(
            {
                "ts": result.timestamp.isoformat(),
                "bias": state.value if state else "",
                "adx": str(adx) if adx is not None else "",
                "pending": pending,
                "qty": qty,
                "position": {"side": "LONG" if qty > 0 else "SHORT" if qty < 0 else "FLAT",
                             "quantity": abs(qty)},
                "action": action,
                "reason": reason,
            }
        )
    return out, shadow


def audit_confirmation(journal, shadow) -> dict:
    """Verify the engine journal reproduced the intended confirm state machine.

    Every actionable bar in the emitted shadow must correspond to exactly one
    journal fill row with the same timestamp, same side and (after the fill)
    the same signed position; any risk-manager rejection, stop-loss exit or
    fill mismatch is reported as a divergence.
    """
    emitted = [row for row in shadow if row["action"] in ("entry", "exit")]
    fills = [
        (entry["timestamp"], entry["order_side"])
        for entry in journal
        if entry.get("order_side") and entry.get("risk_approved", True) and entry.get("fill_quantity")
    ]
    pos_after = {}
    for entry in journal:
        if entry.get("fill_quantity"):
            pos_after[entry["timestamp"]] = entry["position_after"]
    divergences: list[dict] = []
    expected_qty = 0
    for row in emitted:
        ts = row["ts"]
        side = "BUY" if row["action"] == "entry" and row["bias"] == "BUY" else (
            "SELL" if row["action"] == "entry" else ("BUY" if row["bias"] == "BUY" else "SELL")
        )
        # exit side is the flip; entry side is the bias; both equal the emitted signal
        if row["action"] == "exit":
            side = "BUY" if row["bias"] == "BUY" else "SELL"
        expected_qty = 1 if (row["action"] == "entry" and row["bias"] == "BUY") else (
            -1 if row["action"] == "entry" else 0
        )
        fill = [f for f in fills if f[0] == ts]
        if not fill:
            divergences.append({"ts": ts, "kind": "no_engine_fill", "expected_side": side,
                                "expected_position_after": pos_after.get(ts)})
            continue
        f_side = fill[0][1]
        if f_side != side:
            divergences.append({"ts": ts, "kind": "side_mismatch", "expected_side": side,
                                "engine_side": f_side})
        jpos = pos_after.get(ts) or {}
        jqty = (1 if jpos.get("side") == "LONG" else -1) if jpos.get("side") != "FLAT" else 0
        if jqty != expected_qty and row["action"] != "exit":
            divergences.append({"ts": ts, "kind": "position_mismatch",
                                "expected_qty": expected_qty, "engine_qty": jqty})
    stops = sum(1 for e in journal if e.get("stop_fill"))
    rejected = [e["timestamp"] for e in journal if e.get("order_side") and e.get("risk_approved") is False]
    return {
        "emitted_bar_count": len(emitted),
        "engine_fill_count": len(fills),
        "divergences": divergences,
        "stop_fills": stops,
        "risk_rejected": rejected,
        "ok": not divergences and stops == 0 and not rejected,
    }


def day_metrics_from(journal, trades, close_by_ts: dict, days: set[str], multiplier=1) -> dict:
    """Attribute the continuous replay to target dates.

    day P&L = equity at the day's last bar minus equity just before the day's
    first bar (mark-to-market of any carry + realized + costs all included).
    Raw signal != final decision: the journal separates ``raw_signal`` /
    ``actionable`` / ``reason`` from the actual fills and positions.
    """
    by_day: dict[str, dict] = {}
    baseline_equity = Decimal("100000")
    day_entries: dict[str, list[dict]] = {}
    for entry in journal:
        day = entry["timestamp"][:10]
        if day not in days:
            baseline_equity = Decimal(entry["equity"])
            continue
        day_entries.setdefault(day, []).append(entry)

    trades_by_day: dict[str, list] = {}
    for t in trades:
        trades_by_day.setdefault(t.executed_at.date().isoformat(), []).append(t)

    for day, entries in day_entries.items():
        day_pnl = Decimal(entries[-1]["equity"]) - baseline_equity
        realized = sum((t.realized_pnl for t in trades_by_day.get(day, [])), Decimal("0"))
        fees = sum((t.commission for t in trades_by_day.get(day, [])), Decimal("0"))
        wins = sum(1 for t in trades_by_day.get(day, []) if t.realized_pnl > 0)
        losses = sum(1 for t in trades_by_day.get(day, []) if t.realized_pnl < 0)
        slip = Decimal("0")
        fills = stop_outs = 0
        signal_reasons: Counter = Counter()
        for e in entries:
            if not e.get("actionable") and e.get("reason"):
                signal_reasons[str(e["reason"])[:120]] += 1
            if e.get("fill_price") is not None:
                fills += 1
                ts = e["timestamp"]
                close = close_by_ts.get(ts)
                if close is not None:
                    slip += abs(Decimal(e["fill_price"]) - close) * int(e.get("fill_quantity") or 1) * multiplier
            if e.get("stop_fill"):
                stop_outs += 1
        by_day[day] = {
            "open_equity": str(baseline_equity),
            "close_equity": str(entries[-1]["equity"]),
            "day_pnl": str(day_pnl),
            "realized_pnl": str(realized),
            "fees": str(fees),
            "slippage": str(slip),
            "wins": wins,
            "losses": losses,
            "round_trips": wins + losses,
            "fills": fills,
            "stop_outs": stop_outs,
            "no_trade_reasons_top": dict(signal_reasons.most_common(1) or {"-": 0}),
            "position_eod": entries[-1].get("position_after"),
        }
        baseline_equity = Decimal(entries[-1]["equity"])
    return by_day


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=1, choices=(1, 2))
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--mode", default="trend", choices=("trend", "mean_reversion", "breakout"))
    parser.add_argument("--entry-votes", type=int, default=None)
    parser.add_argument("--adx-min", type=str, default=None)
    parser.add_argument("--rsi-ob", type=str, default=None)
    parser.add_argument("--rsi-os", type=str, default=None)
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--confirm-adx", type=str, default=None,
                        help="Iteration-002 ADX entry-confirmation mechanism applied on the "
                             "--mode composite only: underlying vote/latch sequence unchanged; "
                             "entries are taken only when ADX >= confirm-adx (causal, per-bar); "
                             "flip-exits always execute. Threshold is a fixed constant, not tuned.")
    parser.add_argument("--confirm-exits", type=str, default=None,
                        help="Iteration-003 EXIT-quality overlay applied on the --mode composite "
                             "only: an original exit bar is ALLOWED when exit-bar ADX < "
                             "confirm-exits, else DEFERRED (the position is held and re-evaluated "
                             "only on the original's own subsequent decisions — never re-injected, "
                             "never reversed). Engine, risk, costs, protective stop untouched. "
                             "Threshold is a fixed constant, not tuned. Mutually exclusive with "
                             "--confirm-adx.")
    parser.add_argument("--out-dir", default=str(REPO / "runs" / "research" / "day_batch"))
    parser.add_argument("--max-domain-bars", type=int, default=None)
    args = parser.parse_args()

    config = EvaluationConfig()
    stored = load_dataset(DATASET)
    domain = build_domain_bars(stored, args.max_domain_bars)
    if not domain:
        print("no pre-OOS domain bars (all data on/after protected OOS start); abort")
        return 2

    # per-day stats over the whole pre-OOS domain
    all_days: list[dict] = []
    chunks: dict[str, list] = OrderedDict()
    for b in domain:
        chunks.setdefault(b.timestamp.date().isoformat(), []).append(b)
    for d, dbars in chunks.items():
        all_days.append(_day_stats(dbars))
    batch1, batch2, cat_map = select_batches(all_days)
    target = batch1 if args.batch == 1 else batch2
    if not target:
        print("no target days for this batch; abort")
        return 2

    # The four strategies; the A/B variant (ONE isolated change) may alter a
    # single parameter of one composite preset.
    spec_overrides: dict = {}
    if args.entry_votes is not None:
        spec_overrides["entry_votes"] = args.entry_votes
    if args.adx_min is not None:
        spec_overrides["adx_min"] = Decimal(args.adx_min)
    if args.rsi_ob is not None:
        spec_overrides["rsi_overbought"] = Decimal(args.rsi_ob)
    if args.rsi_os is not None:
        spec_overrides["rsi_oversold"] = Decimal(args.rsi_os)
    if args.warmup is not None:
        spec_overrides["warmup"] = args.warmup

    strategies = {
        "champion_ma521": (MovingAverageCrossStrategy(), {}),
        "composite_trend": (MultiIndicatorStrategy(mode="trend"), {}),
        "composite_mean_reversion": (MultiIndicatorStrategy(mode="mean_reversion"), {}),
        "composite_breakout": (MultiIndicatorStrategy(mode="breakout"), {}),
    }
    if spec_overrides:
        mode = args.mode
        key = {"trend": "composite_trend",
               "mean_reversion": "composite_mean_reversion",
               "breakout": "composite_breakout"}[mode]
        strategies[key] = (MultiIndicatorStrategy(mode=mode, **spec_overrides), spec_overrides)

    engine = BacktestEngine()
    close_by_ts = {b.timestamp.isoformat(): b.close for b in domain}

    # Iteration-002: ONE isolated architectural change (ADX entry-confirmation)
    # applied to the selected composite preset only. Everything else identical.
    confirm_threshold = Decimal(args.confirm_adx) if args.confirm_adx is not None else None
    confirm_series: dict[str, list[SignalResult]] = {}
    confirm_shadow: dict[str, list[dict]] = {}
    if confirm_threshold is not None:
        key = {"trend": "composite_trend",
               "mean_reversion": "composite_mean_reversion",
               "breakout": "composite_breakout"}[args.mode]
        series, shadow = confirm_entries(domain, strategies[key][0], confirm_threshold)
        confirm_series[key] = series
        confirm_shadow[key] = shadow

    # Iteration-003: ONE isolated EXIT-quality change on the selected composite
    # preset, applied through the shadow-replica overlay (engine untouched).
    exit_adx = Decimal(args.confirm_exits) if args.confirm_exits is not None else None
    if exit_adx is not None and confirm_threshold is not None:
        print("--confirm-adx and --confirm-exits are mutually exclusive; abort")
        return 2
    overlay_key = (
        {"trend": "composite_trend",
         "mean_reversion": "composite_mean_reversion",
         "breakout": "composite_breakout"}[args.mode]
        if exit_adx is not None else None
    )
    overlay: dict | None = None

    results = {}
    for name, (strategy, params) in strategies.items():
        signals = confirm_series.get(name)
        result, journal = replay_strategy(domain, strategy, config, engine, signals=signals)
        metrics = day_metrics_from(journal, result.trades, close_by_ts, set(target))
        results[name] = {
            "params": params,
            "result": result,
            "journal": journal,
            "day_metrics": metrics,
        }
        if name in confirm_shadow:
            results[name]["confirm_shadow"] = confirm_shadow[name]
            results[name]["confirm_audit"] = audit_confirmation(journal, confirm_shadow[name])

    # Iteration-003 exit-quality arm: overlay -> unchanged engine -> sync/proof/attribution.
    if overlay_key is not None:
        var = run_exit_quality_variant(domain, strategies[overlay_key][0], config, engine,
                                       exit_adx=exit_adx)
        base_signals = confirm_series.get(overlay_key) or bulk_signals(
            domain, strategies[overlay_key][0], latch=True
        )
        base_result = results[overlay_key]["result"]
        var_result = var["result"]
        proof = verify_state_machine(
            domain,
            strategies[overlay_key][0],
            base_signals,
            var["series"],
            results[overlay_key]["journal"],
            var["journal"],
            var["shadow"],
        )
        attr = exit_attribution(results[overlay_key]["journal"], var["journal"], var["shadow"])
        overlay = {
            "strategy": overlay_key,
            "exit_adx": str(exit_adx),
            "baseline_domain": canonical(base_result).as_dict(),
            "variant_domain": canonical(var_result).as_dict(),
            "sync": var["sync"],
            "proof": proof,
            "attribution": attr,
            "variant_day_metrics": day_metrics_from(
                var["journal"], var_result.trades, close_by_ts, set(target)
            ),
        }

    # summarize
    summary = {
        "run_id": f"day_batch_{args.batch}_{args.label}_{_now()}",
        "created_at": datetime.now(timezone.utc).isoformat() if False else datetime.now().isoformat(),
        "batch": args.batch,
        "label": args.label,
        "target_days": target,
        "day_categories": {d: cat_map.get(d, "?") for d in target},
        "domain": [domain[0].timestamp.date().isoformat(), domain[-1].timestamp.date().isoformat()],
        "domain_bars": len(domain),
        "dataset_name": stored.path.name,
        "dataset_hash": stored.data_hash,
        "config_fields": _config_fields(config),
        "config_hash": hashlib.sha256(
            json.dumps(_config_fields(config), sort_keys=True, default=str).encode()
        ).hexdigest()[:16],
        "protected_oos_start": OOS_START.isoformat(),
        "variant": spec_overrides if spec_overrides else None,
        "confirm_adx": str(confirm_threshold) if confirm_threshold is not None else None,
        "confirm_exits_adx": str(exit_adx) if exit_adx is not None else None,
    }
    if overlay is not None:
        summary["overlay"] = {
            "strategy": overlay["strategy"],
            "exit_adx": overlay["exit_adx"],
            "baseline_domain": overlay["baseline_domain"],
            "variant_domain": overlay["variant_domain"],
            "variant_day_metrics": overlay["variant_day_metrics"],
        }
    for name, r in results.items():
        summary[name] = {
            "params": r["params"],
            "day_metrics": r["day_metrics"],
        }
        if "confirm_audit" in r:
            summary[name]["confirm_audit"] = r["confirm_audit"]

    # ---- per-day bar-level decision journals (raw signal != final decision) ----
    journals_text: dict[str, str] = {}
    for name, r in results.items():
        rows = ["timestamp,close,raw_signal,actionable,reason,pos_before,qty_before,order_side,"
                "fill_price,fill_qty,entry_trade,exit_trade,exit_reason,realized_pnl,stop_fill,"
                "pos_after,equity,unrealized"]
        for entry in r["journal"]:
            if entry["timestamp"][:10] not in target:
                continue
            pos_b = entry.get("position_before") or {}
            pos_a = entry.get("position_after") or {}
            rows.append(
                ",".join([
                    entry["timestamp"], str(close_by_ts.get(entry["timestamp"], "")),
                    entry.get("raw_signal", "HOLD"), "1" if entry.get("actionable") else "0",
                    (entry.get("reason") or "").replace(",", ";"),
                    pos_b.get("side", "FLAT"), str(pos_b.get("quantity", 0)),
                    entry.get("order_side", ""), entry.get("fill_price", ""),
                    str(entry.get("fill_quantity", "")),
                    entry.get("entry_trade_id", ""), entry.get("exit_trade_id", ""),
                    (entry.get("exit_reason") or "").replace(",", ";"),
                    entry.get("realized_pnl", ""), "1" if entry.get("stop_fill") else "0",
                    pos_a.get("side", "FLAT"), entry.get("equity", ""),
                    entry.get("unrealized_pnl", ""),
                ])
            )
        journals_text[name] = "\n".join(rows)

    # ---- round trips closed on the target days ----
    trades_rows = [
        "strategy,day,closed_at,side,quantity,exit_price,realized_pnl,commission"
    ]
    for name, r in results.items():
        for t in r["result"].trades:
            if t.realized_pnl == Decimal("0"):
                continue
            day = t.executed_at.date().isoformat()
            if day not in target:
                continue
            trades_rows.append(
                f"{name},{day},{t.executed_at.isoformat()},{t.side.value},{t.quantity},"
                f"{t.price},{t.realized_pnl},{t.commission}"
            )
    trades_text = "\n".join(trades_rows)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = out_dir / f"{args.batch}_{args.label}_{_now()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "summary.json").write_text(
        json.dumps(_jsonable(summary), indent=2, sort_keys=True), encoding="utf-8"
    )
    (run_dir / "day_stats.json").write_text(
        json.dumps(all_days, indent=2), encoding="utf-8"
    )
    # per-day metrics csv-like table for quick reading
    lines = ["strategy,day,day_pnl,realized_pnl,fees,wins,losses,round_trips,fills,stop_outs,eod_position"]
    for name, r in results.items():
        for day in target:
            m = r["day_metrics"].get(day)
            if m is None:
                lines.append(f"{name},{day},,, ,,,,,,")
                continue
            lines.append(
                f"{name},{day},{m['day_pnl']},{m['realized_pnl']},{m['fees']},"
                f"{m['wins']},{m['losses']},{m['round_trips']},{m['fills']},"
                f"{m['stop_outs']},{json.dumps(m['position_eod'])}"
            )
    (run_dir / "metrics_table.csv").write_text("\n".join(lines), encoding="utf-8")
    for name, text in journals_text.items():
        (run_dir / f"journal_{name}.csv").write_text(text, encoding="utf-8")
    for name, shadow in confirm_shadow.items():
        rows = ["timestamp,bias,adx,pending,qty,action,reason"]
        for row in shadow:
            if row["ts"][:10] not in target:
                continue
            rows.append(
                ",".join([row["ts"], row["bias"], row["adx"], str(row["pending"]),
                          str(row["qty"]), row["action"], row["reason"].replace(",", ";")])
            )
        (run_dir / f"journal_{name}_confirm.csv").write_text("\n".join(rows), encoding="utf-8")
    for name, r in results.items():
        if "confirm_audit" in r:
            audit = r["confirm_audit"]
            audit["strategy"] = name
            (run_dir / f"confirm_audit_{name}.json").write_text(
                json.dumps(audit, indent=2), encoding="utf-8"
            )
    (run_dir / "round_trips.csv").write_text(trades_text, encoding="utf-8")
    if overlay is not None:
        name = overlay["strategy"]
        (run_dir / f"overlay_sync_{name}.json").write_text(
            json.dumps(_jsonable(overlay["sync"]), indent=2), encoding="utf-8"
        )
        (run_dir / f"overlay_proof_{name}.json").write_text(
            json.dumps(_jsonable(overlay["proof"]), indent=2), encoding="utf-8"
        )
        (run_dir / f"overlay_attribution_{name}.json").write_text(
            json.dumps(_jsonable(overlay["attribution"]), indent=2), encoding="utf-8"
        )

    print(f"run_dir : {run_dir}")
    print(f"batch   : {args.batch} ({args.label})  days={target}")
    for d in target:
        print(f"           {d}  regime={cat_map.get(d, '?')}")
    print(f"domain  : {domain[0].timestamp.date()}..{domain[-1].timestamp.date()} "
          f"({len(domain)} bars)  hash={stored.data_hash[:12]}..")
    print(f"variant : {spec_overrides or 'none'}")
    for name, r in results.items():
        if "confirm_audit" in r:
            a = r["confirm_audit"]
            print(f"confirm : {name}  threshold={str(confirm_threshold)} "
                  f"emitted={a['emitted_bar_count']} engine_fills={a['engine_fill_count']} "
                  f"divergences={len(a['divergences'])} stops={a['stop_fills']} "
                  f"risk_rejected={len(a['risk_rejected'])} ok={a['ok']}")
    print()
    header = f"{'strategy':<24}{'day':<12}{'day_pnl':>12}{'real':>10}{'fees':>8}{'W':>3}{'L':>3}{'RT':>4}{'fills':>6}{'stop':>5}"
    print(header)
    for name, r in results.items():
        for day in target:
            m = r["day_metrics"].get(day)
            if m is None:
                print(f"{name:<24}{day:<12}{'n/a':>12}")
                continue
            print(
                f"{name:<24}{day:<12}{float(m['day_pnl']):>12.2f}{float(m['realized_pnl']):>10.2f}"
                f"{float(m['fees']):>8.2f}{m['wins']:>3}{m['losses']:>3}{m['round_trips']:>4}"
                f"{m['fills']:>6}{m['stop_outs']:>5}"
            )
    print()
    for name, r in results.items():
        totals_pnl = sum((Decimal(m["day_pnl"]) for m in r["day_metrics"].values()), Decimal("0"))
        print(f"{name:<24} batch net P&L over {len(target)} days = {totals_pnl:.2f}")
    if overlay is not None:
        print()
        print(f"--- Iteration-003 EXIT-QUALITY A/B ({overlay['strategy']}, exit-adx {overlay['exit_adx']}) ---")
        sync, proof = overlay["sync"], overlay["proof"]
        print(f"sync ok={sync['ok']} mismatches={len(sync['mismatches'])} "
              f"fills {sync['fills_shadow']}/{sync['fills_engine']} "
              f"stops {sync['stops_shadow']}/{sync['stops_engine']} "
              f"risk {sync['risk_shadow']}/{sync['risk_engine']} | proof ok={proof['ok']} "
              f"raw={proof['raw_divergences']} added={proof['entry_divergences_added']} "
              f"omitted={proof['entry_divergences_omitted']} "
              f"deferred={proof['exit_divergences_deferred']} "
              f"allowed_later={proof['exit_divergences_allowed_later']}")
        header = f"{'strategy':<12}{'day':<12}{'day_pnl':>12}{'real':>10}{'W':>3}{'L':>3}{'RT':>4}{'fills':>6}{'stop':>5}"
        print(header)
        for day in target:
            m = results[overlay["strategy"]]["day_metrics"].get(day)
            v = overlay["variant_day_metrics"].get(day)
            if m is None:
                continue
            print(
                f"{'baseline':<12}{day:<12}{float(m['day_pnl']):>12.2f}{float(m['realized_pnl']):>10.2f}"
                f"{m['wins']:>3}{m['losses']:>3}{m['round_trips']:>4}{m['fills']:>6}{m['stop_outs']:>5}"
            )
            if v is not None:
                print(
                    f"{'variant':<12}{day:<12}{float(v['day_pnl']):>12.2f}{float(v['realized_pnl']):>10.2f}"
                    f"{v['wins']:>3}{v['losses']:>3}{v['round_trips']:>4}{v['fills']:>6}{v['stop_outs']:>5}"
                )
        b_tot = sum((Decimal(m["day_pnl"]) for m in results[overlay["strategy"]]["day_metrics"].values()), Decimal("0"))
        v_tot = sum((Decimal(v["day_pnl"]) for v in overlay["variant_day_metrics"].values()), Decimal("0"))
        print(f"{'baseline':<12} batch net P&L over {len(target)} days = {b_tot:.2f}")
        print(f"{'variant':<12} batch net P&L over {len(target)} days = {v_tot:.2f} (delta {v_tot - b_tot:.2f})")
        print(f"changed_exits={overlay['attribution']['changed_exits']} "
              f"entries_changed_downstream={overlay['attribution']['entries_changed_downstream']} "
              f"categories={json.dumps(overlay['attribution']['categories'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())