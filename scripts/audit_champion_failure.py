"""Audit the frozen champion MA(5,21) negative result (research finding).

Reproduces the champion's continuous paper replay over real NIFTY 5m bars and
breaks the -143.21% net result down from the *actual round-trip evidence*:
trade frequency, gross edge vs friction (cost-to-edge), win/loss shapes,
holding-duration/whipsaw behaviour, stop-loss exits, long-vs-short split,
regime/period contribution, drawdown and cost scenarios.

Nothing here places orders or changes strategies. PAPER ONLY.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.model_performance import (
    aggregate_by_period,
    evaluate_continuous,
)
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.research.metrics import compute_metrics

DATASET_5M = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
OUTPUT = ROOT / "reports" / "model_performance" / "audit_champion_failure.json"


def _pct(x: Decimal, y: Decimal) -> str:
    return f"{x / y * Decimal('100'):.4f}" if y else "n/a"


def _f(x: Decimal) -> str:
    return f"{x:f}"


def percentile(values: list[Decimal], fraction: float) -> Decimal:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * len(ordered)))
    return ordered[index]


def _stop_detected(trip) -> bool:
    entry = trip.entry_trade
    exit_ = trip.exit_trade
    ratio = exit_.price / entry.price
    if entry.side.value == "BUY":
        return ratio <= Decimal("0.98")
    return ratio >= Decimal("1.02")


def main() -> None:
    if not DATASET_5M.exists():
        raise SystemExit("missing 5m dataset; run acquire_model_performance_data.py first")

    config = BacktestConfig()
    bars = load_dataset(DATASET_5M).bars
    run = evaluate_continuous(bars, config=config)
    metrics = run.metrics
    trips = run.round_trips
    signals = run.signals

    detector = RegimeDetector(fast=5, slow=21)

    # --- trade frequency ---
    days: dict[str, int] = {}
    for trip in trips:
        days.setdefault(trip.exit_trade.executed_at.date().isoformat(), 0)
        days[trip.exit_trade.executed_at.date().isoformat()] += 1
    trading_days = len(days)
    trades_per_day = Decimal(len(trips)) / Decimal(trading_days)

    # --- gross edge vs friction ---
    total_friction = metrics.transaction_costs
    gross_pnl = metrics.net_pnl + metrics.transaction_costs
    per_trade_cost = total_friction / len(trips)
    per_trade_gross = gross_pnl / len(trips)

    price_pnls = [trip.price_pnl for trip in trips]
    mean_price = sum(price_pnls, Decimal("0")) / len(price_pnls)
    variance = sum(((p - mean_price) ** 2 for p in price_pnls), Decimal("0")) / len(price_pnls)
    std_price = variance.sqrt()
    n_sqrt = Decimal(len(price_pnls)).sqrt()
    t_stat_price = mean_price / (std_price / n_sqrt) if std_price else Decimal("0")

    # friction-free gross edge per trade at bar-close fills (no commission, no
    # slippage): equals gross_pnl / trades. Per-trip spread is ~2*slippage (small
    # variance vs price noise), so the per-trade std is approximated by std_price.
    per_trade_gross = gross_pnl / Decimal(len(trips))
    t_stat_gross = per_trade_gross / (std_price / n_sqrt) if std_price else Decimal("0")
    per_trade_commission = metrics.total_commission / Decimal(len(trips))
    per_trade_slippage = metrics.slippage_cost / Decimal(len(trips))

    # --- win/loss shapes ---
    wins = [t.net_pnl for t in trips if t.net_pnl > 0]
    losses = [t.net_pnl for t in trips if t.net_pnl < 0]
    net_pnls = [t.net_pnl for t in trips]

    # --- holding duration / whipsaw ---
    holdings = [trip.holding_bars for trip in trips]
    held_le_5 = sum(1 for h in holdings if h <= 5)
    held_le_10 = sum(1 for h in holdings if h <= 9)
    held_le_20 = sum(1 for h in holdings if h <= 19)
    stops = [trip for trip in trips if _stop_detected(trip)]

    # bucket by holding bars
    buckets = {"1-5": [], "6-10": [], "11-20": [], "21-50": [], "51+": []}
    for trip in trips:
        key = (
            "1-5" if trip.holding_bars <= 5
            else "6-10" if trip.holding_bars <= 9
            else "11-20" if trip.holding_bars <= 19
            else "21-50" if trip.holding_bars <= 49
            else "51+"
        )
        buckets[key].append(trip)
    holding_rows = []
    for key, group in buckets.items():
        if not group:
            continue
        holding_rows.append(
            {
                "holding_bars": key,
                "num_trades": len(group),
                "win_rate_pct": _pct(
                    Decimal(sum(1 for t in group if t.net_pnl > 0)), Decimal(len(group))
                ),
                "avg_price_pnl": _f(sum((t.price_pnl for t in group), Decimal("0")) / len(group)),
                "avg_net_pnl": _f(sum((t.net_pnl for t in group), Decimal("0")) / len(group)),
                "net_pnl": _f(sum((t.net_pnl for t in group), Decimal("0"))),
            }
        )

    # --- signal whipsaw: bars between opposite actionable signals ---
    flips: list[int] = []
    last_side: str | None = None
    last_index = -1
    for i, signal in enumerate(signals):
        if not signal.actionable:
            continue
        side = "BUY" if signal.signal.value == "BUY" else "SELL"
        if last_side is not None and side != last_side:
            flips.append(i - last_index)
        last_side = side
        last_index = i
    flip_counts = Counter()
    for span in flips:
        flip_counts[(span - 1) // 5 * 5] += 1

    # --- long vs short ---
    longs = [trip for trip in trips if trip.entry_trade.side.value == "BUY"]
    shorts = [trip for trip in trips if trip.entry_trade.side.value == "SELL"]

    # --- per-year ---
    years = aggregate_by_period(trips, granularity="year")
    yearly = []
    for row in years:
        period = str(row["period"])
        group = [t for t in trips if t.exit_trade.executed_at.year == int(period)]
        yearly.append(
            {
                "period": period,
                "num_trades": row["num_trades"],
                "avg_holding_bars": _f(
                    sum((t.holding_bars for t in group), Decimal("0")) / Decimal(len(group) or 1)
                ),
                "net_pnl": row["net_pnl"],
                "price_pnl": row["price_pnl"],
                "commission": row["commission"],
                "transactions_costs_share": row["commission"],
            }
        )

    # --- decision-time regime entries with time-in-regime share ---
    regime_time = Counter()
    prev_regime: str | None = None
    prev_index = -1
    for i, bar in enumerate(bars):
        index = i
        if index < 21:
            continue
        regime = detector.detect_prefix(bars, index)
        label = regime.label if regime is not None else "unknown"
        if prev_regime is not None:
            regime_time[prev_regime] += index - prev_index
        prev_regime = label
        prev_index = index

    entry_regimes: dict[str, list] = {}
    for trip in trips:
        entry_regimes.setdefault(trip.entry_regime or "unknown", []).append(trip)
    total_time = sum(regime_time.values())
    regime_rows = []
    for label in sorted(set(list(regime_time.keys()) + list(entry_regimes.keys()))):
        group = entry_regimes.get(label, [])
        longs_g = [t for t in group if t.entry_trade.side.value == "BUY"]
        shorts_g = [t for t in group if t.entry_trade.side.value == "SELL"]
        regime_rows.append(
            {
                "regime": label,
                "time_share_pct": _pct(Decimal(regime_time.get(label, 0)), Decimal(total_time)),
                "num_trades": len(group),
                "long_shorts": f"{len(longs_g)}/{len(shorts_g)}",
                "win_rate_pct": _pct(
                    Decimal(sum(1 for t in group if t.net_pnl > 0)), Decimal(len(group) or 1)
                ),
                "avg_price_pnl": _f(
                    sum((t.price_pnl for t in group), Decimal("0")) / Decimal(len(group) or 1)
                ),
                "net_pnl": _f(sum((t.net_pnl for t in group), Decimal("0"))),
                "avg_holding_bars": _f(
                    sum((t.holding_bars for t in group), Decimal("0")) / Decimal(len(group) or 1)
                ),
            }
        )

    # --- drawdown ---
    max_dd = metrics.max_drawdown
    max_dd_pct = metrics.max_drawdown_pct
    drawdown_bars = metrics.drawdown_duration_bars
    in_drawdown = sum(1 for p in run.result.equity_curve if p.drawdown_from_peak > 0)

    # --- cost scenarios ---
    cost_need = per_trade_cost
    edge_ratio = per_trade_gross / per_trade_cost if per_trade_cost else Decimal("0")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "schema_version": "1",
        "deliverable": "audit_champion_failure",
        "strategy": {"name": "moving_average_cross", "fast": 5, "slow": 21},
        "config": {
            "initial_capital": str(config.initial_capital),
            "quantity": str(config.quantity),
            "commission_rate": str(config.commission_rate),
            "slippage_rate": str(config.slippage_rate),
            "stop_loss_pct": str(config.stop_loss_pct),
        },
        "headline": run.summary_dict(),
        "trade_frequency": {
            "num_trades": len(trips),
            "trading_days": trading_days,
            "trades_per_trading_day": _f(trades_per_day),
            "avg_holding_bars": _f(
                sum((Decimal(h) for h in holdings), Decimal("0")) / Decimal(len(holdings) or 1)
            ),
            "median_holding_bars": int(percentile([Decimal(h) for h in holdings], 0.5)),
            "p75_holding_bars": int(percentile([Decimal(h) for h in holdings], 0.75)),
        },
        "gross_edge_vs_friction": {
            "gross_pnl_before_friction": _f(gross_pnl),
            "friction": _f(total_friction),
            "commission": _f(metrics.total_commission),
            "slippage": _f(metrics.slippage_cost),
            "per_trade_gross_edge_at_bar_close": _f(per_trade_gross),
            "per_trade_gross_t_stat": _f(t_stat_gross),
            "per_trade_slippage": _f(per_trade_slippage),
            "per_trade_commission": _f(per_trade_commission),
            "per_trade_cost": _f(per_trade_cost),
            "edge_to_cost_ratio": _f(edge_ratio),
            "required_gross_ppl_per_trade_to_break_even": _f(cost_need - per_trade_gross),
            "realized_round_trip_ppl_mean": _f(mean_price),
            "realized_round_trip_ppl_t_stat_including_slippage": _f(t_stat_price),
            "realized_round_trip_ppl_std": _f(std_price),
        },
        "win_loss_shapes": {
            "winning": metrics.winning_trades,
            "losing": metrics.losing_trades,
            "win_rate_pct": _f(metrics.win_rate),
            "gross_profit": _f(metrics.gross_profit),
            "gross_loss": _f(metrics.gross_loss),
            "profit_factor": _f(metrics.profit_factor),
            "avg_win": _f(sum(wins, Decimal("0")) / Decimal(len(wins))) if wins else "n/a",
            "avg_loss": _f(sum(losses, Decimal("0")) / Decimal(len(losses))) if losses else "n/a",
            "best_trade": _f(max(net_pnls)),
            "worst_trade": _f(min(net_pnls)),
            "avg_trade_net": _f(metrics.expectancy if metrics.expectancy is not None else Decimal("0")),
            "std_net_per_trade": _f(
                (sum(((n - (sum(net_pnls, Decimal('0')) / len(net_pnls))) ** 2 for n in net_pnls), Decimal('0')) / len(net_pnls)).sqrt()
            ),
        },
        "whipsaw": {
            "signal_flips_total": len(flips),
            "flip_spans_by_5buckets": {str(k): v for k, v in sorted(flip_counts.items())},
            "median_bars_opposite_signal": int(percentile([Decimal(s) for s in flips], 0.5)),
            "held_1_5_bars": held_le_5,
            "held_6_10_bars": held_le_10 - held_le_5,
            "held_11_20_bars": held_le_20 - held_le_10,
            "stop_like_exits_detected": len(stops),
            "stop_like_exit_net_pnl": _f(sum((t.net_pnl for t in stops), Decimal("0"))),
        },
        "holding_buckets": holding_rows,
        "long_vs_short": {
            "longs": {"num": len(longs),
                      "win_rate_pct": _pct(Decimal(sum(1 for t in longs if t.net_pnl > 0)), Decimal(len(longs) or 1)),
                      "net_pnl": _f(sum((t.net_pnl for t in longs), Decimal("0"))),
                      "avg_holding": _f(sum((Decimal(t.holding_bars) for t in longs), Decimal("0")) / Decimal(len(longs) or 1))},
            "shorts": {"num": len(shorts),
                       "win_rate_pct": _pct(Decimal(sum(1 for t in shorts if t.net_pnl > 0)), Decimal(len(shorts) or 1)),
                       "net_pnl": _f(sum((t.net_pnl for t in shorts), Decimal("0"))),
                       "avg_holding": _f(sum((Decimal(t.holding_bars) for t in shorts), Decimal("0")) / Decimal(len(shorts) or 1))},
        },
        "yearly": yearly,
        "regime_matrix": regime_rows,
        "drawdown": {
            "max_drawdown": _f(max_dd),
            "max_drawdown_pct": _f(max_dd_pct),
            "drawdown_duration_bars": drawdown_bars,
            "bars_in_drawdown": in_drawdown,
            "fraction_in_drawdown_pct": _f(Decimal(in_drawdown) / Decimal(len(run.result.equity_curve)) * Decimal("100")),
            "min_equity": _f(run.min_equity),
        },
        "reconciliation_ok": run.reconciliation_ok,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(f"champion net           : {metrics.net_return_pct}%  (final {metrics.final_equity})")
    print(f"friction               : {_f(total_friction)} (commission {_f(metrics.total_commission)} + slippage {_f(metrics.slippage_cost)})")
    print(f"gross P&L pre-friction : {_f(gross_pnl)}  -> {_f(per_trade_gross)}/trade, t={float(t_stat_gross):.2f}")
    print(f"realized P&L at fills  : mean {_f(mean_price)}/trade, t={float(t_stat_price):.2f}, std {_f(std_price)}/trade")
    print(f"per-trade cost         : {_f(per_trade_cost)}  (slip {_f(per_trade_slippage)} + comm {_f(per_trade_commission)}) edge/cost {float(edge_ratio):.4f}")
    print(f"trades / day           : {float(trades_per_day):.3f}  ({len(trips)} over {trading_days} days)")
    print(f"hold bars med/p75      : {report['trade_frequency']['median_holding_bars']}/{report['trade_frequency']['p75_holding_bars']}")
    print(f"held <=5/<=10 bars     : {held_le_5}/{held_le_10}")
    print(f"stop-like exits        : {len(stops)} net {_f(sum((t.net_pnl for t in stops), Decimal('0')))}")
    print(f"longs vs shorts        : {len(longs)} vs {len(shorts)} (net {_f(sum((t.net_pnl for t in longs), Decimal('0'))) } vs {_f(sum((t.net_pnl for t in shorts), Decimal('0')))})")
    print(f"max DD                 : {_f(max_dd)} ({_f(max_dd_pct)}%), in-drawdown {float(report['drawdown']['fraction_in_drawdown_pct']):.1f}% of bars")
    print(f"reconciliation_ok      : {run.reconciliation_ok}")
    print(f"wrote                  : {OUTPUT}")


if __name__ == "__main__":
    main()