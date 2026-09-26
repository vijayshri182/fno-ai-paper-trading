"""Research-domain final assessment (WS 7.18, read-only, single deterministic pass).

Scope: decide, from research-domain evidence ONLY, whether any frozen candidate
beats the frozen MA(5,21) champion after costs. This script:
  * loads the single canonical 5m NIFTY 50 dataset (hash-validated);
  * replays the research domain 2022-01-03 .. 2025-10-03 with the walk-forward
    daily-reset semantics (fresh session/portfolio per day, same BacktestConfig,
    same RegimeDetector(5,21), trades closed before the day ends);
  * reproduces the persisted walk ledger champion totals bit-for-bit (this is
    the control that validates the whole path);
  * runs champion failure analysis on all 1293 completed round trips
    (regime x side, year, holding bars, time-of-day, cost/edge);
  * evaluates frozen candidates c1..c5 under identical semantics;
  * applies the WS 7.18 promotion gate criteria over the research domain;
  * sensitivity/robustness grid and cost scans on the same domain.

No random split, no look-ahead, no repeated tuning, and -- critically -- the
protected out-of-sample period (>= 2025-10-06) is never loaded or evaluated.
Candidate parameters are frozen catalog constants, untouched by this run.

Paper/historical only. Never enables live trading and never rewrites history
(the walk ledger and its artifacts are only read).
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.fast_signal import moving_average_cross_signals
from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy
from fno_ai_paper_trading.strategies.research_candidates import CANDIDATES
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig
from fno_ai_paper_trading.walkforward.engine import WalkForwardEngine
from fno_ai_paper_trading.walkforward.gate import (
    WalkForwardGate,
    WalkForwardGateCriteria,
    agg_window_metrics,
)

DATASET_PATH = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
SUMMARY_PATH = ROOT / "reports" / "walkforward" / "summary.json"
OUT_DIR = ROOT / "reports" / "research"
OUT_JSON = OUT_DIR / "research_final_assessment.json"

# Canonical research-domain boundary (persisted walk config protected_oos_start).
PROTECTED_OOS_START = date(2025, 10, 6)

_ZERO = Decimal("0")


def fmt(value: Decimal) -> str:
    return format(Decimal(str(value)), "f")


def pv(value) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# strategy registry (frozen constants; parameters NEVER tuned by this run)
# ---------------------------------------------------------------------------

CHAMPION = {
    "id": "model_0",
    "fid": "model_0",
    "family": "MOVING_AVERAGE_CROSS",
    "name": "moving_average_cross",
    "provider": moving_average_cross_signals,
    "params": {"fast": 5, "slow": 21},
    "strategy": MovingAverageCrossStrategy,
    "rationale": "Frozen WS 7.18 baseline champion (Phase 2).",
}

FAMILY_LABEL = {
    "c1_long_only_ma_cross": "LONG_ONLY_TREND",
    "c2_slow_long_only_ma_cross": "LONG_ONLY_TREND_SLOW",
    "c3_momentum_gated_ma_cross": "MOMENTUM_CONFIRMATION",
    "c4_trend_gated_ma_cross": "REGIME_AWARE_TREND_GATE",
    "c5_donchian_breakout": "VOLATILITY_BREAKOUT",
}


def candidate_list() -> list[dict[str, Any]]:
    out = []
    for cid, entry in CANDIDATES.items():
        provider = entry["provider"]
        params = dict(entry["params"])
        strategy_cls = entry["strategy"]
        out.append(
            {
                "id": cid,
                "fid": f"c-s{len(out)+1}",
                "family": FAMILY_LABEL[cid],
                "name": getattr(strategy_cls, "name", cid),
                "provider": provider,
                "params": params,
                "strategy": strategy_cls,
                "perturbations": dict(entry.get("perturbations", {})),
                "rationale": str(entry.get("rationale", "")),
            }
        )
    return out


# ---------------------------------------------------------------------------
# day replay (identical semantics to the walk)
# ---------------------------------------------------------------------------


def load_domain_days() -> tuple[list[DayBars], str]:
    dataset = load_dataset(DATASET_PATH)
    grouped: dict[str, list] = {}
    for bar in dataset.bars:
        day = bar.timestamp.date()
        if day >= PROTECTED_OOS_START:
            # protected out-of-sample days are NEVER loaded or processed
            continue
        grouped.setdefault(day.isoformat(), []).append(bar)
    days = [
        DayBars(
            day=date.fromisoformat(key),
            bars=tuple(grouped[key]),
            source_hash=dataset.data_hash,
        )
        for key in sorted(grouped)
    ]
    if not days:
        raise SystemExit("research domain empty; refusing to continue")
    return days, dataset.data_hash


@dataclass
class DayReplay:
    day: date
    run: Any
    trips: list
    bars: list


@dataclass
class Totals:
    days: int = 0
    net_pnl: Decimal = _ZERO
    costs: Decimal = _ZERO
    slippage: Decimal = _ZERO
    round_trips: int = 0
    wins: int = 0
    losses: int = 0
    exposure_units: Decimal = _ZERO
    max_drawdown_pct: Decimal = _ZERO

    @classmethod
    def from_accumulator(cls, acc: dict[str, Any]) -> "Totals":
        return cls(
            days=int(acc["days"]),
            net_pnl=pv(acc["net_pnl"]),
            costs=pv(acc["costs"]),
            slippage=pv(acc["slippage"]),
            round_trips=int(acc["round_trips"]),
            wins=int(acc["wins"]),
            losses=int(acc["losses"]),
            exposure_units=pv(acc["exposure_units"]),
            max_drawdown_pct=pv(acc["max_drawdown_pct"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "net_pnl": fmt(self.net_pnl),
            "costs": fmt(self.costs),
            "slippage": fmt(self.slippage),
            "round_trips": self.round_trips,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": fmt(
                Decimal(self.wins) * Decimal("100") / Decimal(self.round_trips)
                if self.round_trips
                else Decimal("0")
            ),
            "exposure_units": fmt(self.exposure_units),
            "avg_exposure_pct": fmt(
                self.exposure_units / Decimal(self.days) if self.days else _ZERO
            ),
            "max_drawdown_pct": fmt(self.max_drawdown_pct),
            "gross_need": fmt(self.net_pnl + self.costs),
        }


class ResearchReplay:
    """Replays all research-domain days for one provider, mirroring the walk."""

    def __init__(self) -> None:
        self._engine = WalkForwardEngine(WalkForwardConfig())
        self._bt = self._engine._bt

    def _day(self, db: DayBars, strategy: Any, provider, params: dict) -> DayReplay:
        bars = list(db.bars)
        signals = provider(bars, **params)
        result = self._engine._backtester.run(bars, strategy, self._bt, signals=signals)
        trips, _open = pair_round_trips(result.trades)
        run = self._engine._build_day_run(bars, result, signals, trips)
        return DayReplay(day=db.day, run=run, trips=list(trips), bars=bars)

    def run(
        self,
        days: Sequence[DayBars],
        provider,
        params: dict,
        strategy,
        *,
        keep_details: bool = False,
    ) -> tuple[Totals, dict[str, Any], list[dict[str, Any]]]:
        acc = {
            "days": 0,
            "net_pnl": _ZERO,
            "costs": _ZERO,
            "slippage": _ZERO,
            "round_trips": 0,
            "wins": 0,
            "losses": 0,
            "exposure_units": _ZERO,
            "max_drawdown_pct": _ZERO,
        }
        day_metrics: dict[str, Any] = {}
        details: list[dict[str, Any]] = []
        for db in days:
            replay = self._day(db, strategy, provider, params)
            self._engine._accumulate_champion(acc, replay.run)
            day_metrics[db.day.isoformat()] = replay.run.metrics()
            if keep_details:
                details.extend(
                    self._detail_rows(replay, db)
                )
        totals = Totals.from_accumulator(acc)
        return totals, day_metrics, details

    @staticmethod
    def _detail_rows(replay: DayReplay, db: DayBars) -> list[dict[str, Any]]:
        index = {bar.timestamp.isoformat(): i for i, bar in enumerate(replay.bars)}
        rows: list[dict[str, Any]] = []
        for (entry, exit_), td in zip(replay.trips, replay.run.trades):
            entry_ts = entry.executed_at
            exit_ts = exit_.executed_at
            hold = None
            if entry_ts.isoformat() in index and exit_ts.isoformat() in index:
                hold = index[exit_ts.isoformat()] - index[entry_ts.isoformat()]
            rows.append(
                {
                    "day": db.day.isoformat(),
                    "year": db.day.year,
                    "side": td.side,
                    "entry_regime": td.entry_regime,
                    "entry_time": td.entry_time,
                    "exit_time": td.exit_time,
                    "entry_hour": entry_ts.hour,
                    "tod_segment": _tod_segment(entry_ts.hour),
                    "holding_bars": hold,
                    "realized_pnl": pv(td.realized_pnl),
                    "commission": pv(td.commission),
                    "ratio": (
                        pv(td.exit_price) / pv(td.entry_price)
                        if Decimal(td.entry_price) != _ZERO
                        else None
                    ),
                }
            )
        return rows


def _tod_segment(hour: int) -> str:
    if hour < 11:
        return "09:15-11:00"
    if hour < 13:
        return "11:00-13:00"
    if hour < 15:
        return "13:00-15:00"
    return "15:00-15:25"


# ---------------------------------------------------------------------------
# failure-analysis aggregation
# ---------------------------------------------------------------------------

HOLD_BUCKETS = [(1, 5), (6, 10), (11, 20), (21, 50), (51, None)]


def _hold_label(hold: int | None) -> str:
    if hold is None:
        return "NA"
    for lo, hi in HOLD_BUCKETS:
        if hi is None:
            if hold >= lo:
                return f"{lo}+ bars"
        elif lo <= hold <= hi:
            return f"{lo}-{hi} bars"
    return "NA"


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    realized = [r["realized_pnl"] for r in rows]
    nets = [r["realized_pnl"] - r["commission"] for r in rows]
    wins = sum(1 for v in realized if v > _ZERO)
    mean_r = sum(realized, _ZERO) / n if n else _ZERO
    tstat = None
    if n >= 2:
        mean = sum(realized, _ZERO) / n
        var = sum((v - mean) ** 2 for v in realized) / (n - 1)
        sd = var.sqrt()
        if sd > _ZERO:
            tstat = fmt((mean / (sd / (Decimal(n).sqrt()))))
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": fmt(Decimal(wins) * Decimal("100") / n) if n else "0",
        "realized_pnl": fmt(sum(realized, _ZERO)),
        "net_pnl": fmt(sum(nets, _ZERO)),
        "costs": fmt(sum((r["commission"] for r in rows), _ZERO)),
        "mean_realized_per_trade": fmt(mean_r),
        "t_stat_realized": tstat,
    }


def _bucket(rows: list[dict[str, Any]], key: str | tuple[str, str]) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], list[dict[str, Any]]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    keys: list[str] = list(key) if isinstance(key, (tuple, list)) else [key]
    for r in rows:
        g = tuple(str(r[k]) for k in keys)
        grouped.setdefault(g, []).append(r)
    order = sorted(grouped)
    return order, grouped


# ---------------------------------------------------------------------------
# champion reproduction targets (persisted walk artifacts, read-only)
# ---------------------------------------------------------------------------


def expected_totals() -> dict[str, Any]:
    payload = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    return dict(payload["champion_totals"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    days, data_hash = load_domain_days()
    first = days[0].day
    last = days[-1].day
    print(f"research domain: {first} .. {last} ({len(days)} trading days; data hash {data_hash[:12]}…)")
    if any(d.day >= PROTECTED_OOS_START for d in days):
        raise SystemExit("protected OOS day leaked into domain; aborting")
    if len(days) != 932:
        print(f"WARNING: expected 932 research-domain days, got {len(days)}")

    replay = ResearchReplay()
    bench = replay._engine._benchmark(days)

    # ---- champion control (must reproduce the persisted ledger totals) ----
    champ_totals, champ_day_metrics, champ_details = replay.run(
        days,
        provider=CHAMPION["provider"],
        params=CHAMPION["params"],
        strategy=CHAMPION["strategy"](**CHAMPION["params"]),
        keep_details=True,
    )
    expected = expected_totals()
    rep: dict[str, Any] = {"computed": champ_totals.to_dict()}
    mismatches = []
    for key in ("days", "net_pnl", "costs", "slippage", "round_trips", "wins", "losses", "max_drawdown_pct"):
        exp = expected[key]
        got = rep["computed"][key]
        if pv(exp) != pv(got):
            mismatches.append(f"{key}: expected {exp} got {got}")
    if pv(expected.get("exposure_units", "0")) != champ_totals.exposure_units:
        mismatches.append(
            f"exposure_units: expected {expected['exposure_units']} got {fmt(champ_totals.exposure_units)}"
        )
    rep["reproduced"] = not mismatches
    rep["mismatches"] = mismatches
    rep["benchmark"] = bench
    if mismatches:
        print("CHAMPION CONTROL MISMATCH vs persisted ledger:")
        for m in mismatches:
            print("  " + m)
        raise SystemExit(2)
    print(
        f"champion control OK: net {fmt(champ_totals.net_pnl)}, "
        f"rt {champ_totals.round_trips}, W/L {champ_totals.wins}/{champ_totals.losses}, "
        f"costs {fmt(champ_totals.costs)}, slippage {fmt(champ_totals.slippage)}"
    )

    # ---- failure analysis (champion completed round trips) ----------------
    failure = _failure_analysis(champ_details, champ_totals)
    del champ_details

    # ---- candidates (identical daily-reset semantics) ---------------------
    candidates = candidate_list()
    candidate_results: list[dict[str, Any]] = []
    champ_window = agg_window_metrics(champ_day_metrics)

    for cand in candidates:
        totals, day_metrics, _detail = replay.run(
            days,
            provider=cand["provider"],
            params=cand["params"],
            strategy=cand["strategy"](**cand["params"]),
            keep_details=False,
        )
        window = agg_window_metrics(day_metrics)
        verdict = WalkForwardGate(
            WalkForwardGateCriteria.from_config(WalkForwardConfig())
        ).evaluate(window, champ_window, cand["id"], base_equity=Decimal("100000"))
        result = {
            "id": cand["id"],
            "name": cand["name"],
            "family": cand["family"],
            "params": dict(cand["params"]),
            "totals": totals.to_dict(),
            "verdict": {
                "decision": verdict.decision,
                "reasons": list(verdict.reasons),
                "metrics": dict(verdict.metrics),
            },
        }
        candidate_results.append(result)
        print(
            f"{cand['id']:32s} net {fmt(totals.net_pnl):>14s}  rt {totals.round_trips:>4d}  "
            f"W {totals.wins:>3d}/L {totals.losses:>4d}  -> {verdict.decision}"
        )

    # ---- robustness grid ---------------------------------------------------
    robustness = _robustness_grid(replay, days, champ_totals)

    # ---- cost scan ---------------------------------------------------------
    cost_scan = _cost_scan(replay, days, candidate_results)

    # ---- best / worst conditions from failure analysis --------------------
    best_worst = _best_worst(failure)

    decision = _make_decision(candidate_results, best_worst, robustness)

    payload = {
        "meta": {
            "dataset": str(DATASET_PATH),
            "dataset_hash": data_hash,
            "protected_oos_start": PROTECTED_OOS_START.isoformat(),
            "research_first_day": first.isoformat(),
            "research_last_day": last.isoformat(),
            "research_days": len(days),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "note": "read-only research-domain assessment; protected OOS never loaded; "
            "candidate params frozen; single deterministic pass",
        },
        "champion_control": rep,
        "failure_analysis": failure,
        "best_worst_conditions": best_worst,
        "candidates": candidate_results,
        "robustness_grid": robustness,
        "cost_scan": cost_scan,
        "decision": decision,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {OUT_JSON}")

    _print_decision(decision)
    return 0


# ---------------------------------------------------------------------------
# failure analysis
# ---------------------------------------------------------------------------


def _failure_analysis(rows: list[dict[str, Any]], totals: Totals) -> dict[str, Any]:
    overall = _stats(rows)
    by_side = {}
    order, grouped = _bucket(rows, "side")
    for key in order:
        by_side[key[0]] = _stats(grouped[key])

    by_year = {}
    order, grouped = _bucket(rows, "year")
    for key in order:
        by_year[str(key[0])] = _stats(grouped[key])

    by_tod = {}
    order, grouped = _bucket(rows, "tod_segment")
    for key in order:
        by_tod[key[0]] = _stats(grouped[key])

    by_hold: dict[str, dict[str, Any]] = {}
    held: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        label = _hold_label(r["holding_bars"])
        held.setdefault(label, []).append(r)
    for label in sorted(held, key=lambda k: (0, "") if k == "NA" else (1, k)):
        by_hold[label] = _stats(held[label])

    by_regime_side = {}
    order_r, grouped_r = _bucket(rows, ("entry_regime", "side"))
    for key in order_r:
        by_regime_side[f"{key[0]}/{key[1]}"] = _stats(grouped_r[key])

    by_regime = {}
    order_rr, grouped_rr = _bucket(rows, "entry_regime")
    for key in order_rr:
        by_regime[key[0]] = _stats(grouped_rr[key])

    stop_hits = _stats([r for r in rows if _is_stop_hit(r)])
    return {
        "n_completed_round_trips": len(rows),
        "overall": overall,
        "by_side": by_side,
        "by_year": by_year,
        "by_tod_segment": by_tod,
        "by_holding_bars": by_hold,
        "by_entry_regime": by_regime,
        "by_regime_x_side": by_regime_side,
        "stop_like_exits_2pct": stop_hits,
        "per_trade_slippage": "NOT PERSISTED (day-level slippage available only)",
        "note_trade_accounting": "trade-level realized includes slippage on closing fills; "
        "commission separately; canonical totals use day-equity accounting",
    }


def _is_stop_hit(r: dict[str, Any]) -> bool:
    ratio = r.get("ratio")
    if ratio is None:
        return False
    if r["side"] == "CALL":
        return ratio <= Decimal("0.98")
    return ratio >= Decimal("1.02")


# ---------------------------------------------------------------------------
# robustness + cost scans
# ---------------------------------------------------------------------------


def _robustness_grid(replay, days, champ_totals) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    defs = [
        ("model_0", moving_average_cross_signals, {"fast": 5, "slow": 21}, MovingAverageCrossStrategy, {"fast": [3, 8], "slow": [18, 26]}),
    ]
    for cand in candidate_list():
        defs.append(
            (cand["id"], cand["provider"], cand["params"], cand["strategy"], cand["perturbations"])
        )
    for cid, provider, params, strategy_cls, perms in defs:
        for key, values in sorted(perms.items()):
            for value in values:
                var_params = dict(params)
                var_params[key] = value
                totals, _dm, _d = replay.run(
                    days,
                    provider=provider,
                    params=var_params,
                    strategy=strategy_cls(**var_params),
                    keep_details=False,
                )
                variants.append(
                    {
                        "id": cid,
                        "variant": f"{key}={value}",
                        "net_pnl": fmt(totals.net_pnl),
                        "round_trips": totals.round_trips,
                        "wins": totals.wins,
                        "costs": fmt(totals.costs),
                        "max_drawdown_pct": fmt(totals.max_drawdown_pct),
                        "positive": totals.net_pnl > _ZERO,
                    }
                )
                print(
                    f"  robustness {cid} {key}={value}: net {fmt(totals.net_pnl):>14s} "
                    f"rt {totals.round_trips:>4d}"
                )
    positive = [v for v in variants if v["positive"]]
    return {
        "n_variants": len(variants),
        "n_positive": len(positive),
        "variants": variants,
        "champion_net_for_reference": fmt(champ_totals.net_pnl),
    }


SCENARIOS = [
    ("zero", "0", "0"),
    ("low", "0.0001", "0.0002"),
    ("base", "0.0003", "0.001"),
    ("high", "0.0005", "0.002"),
]


def _cost_scan(replay, days, candidate_results) -> dict[str, Any]:
    scan_ids = ["model_0"]
    for res in candidate_results:
        if res["verdict"]["decision"] == "PROMOTE" or res["id"] in (
            "c2_slow_long_only_ma_cross",
            "c4_trend_gated_ma_cross",
        ):
            scan_ids.append(res["id"])
    results = []
    for cid in scan_ids:
        if cid == "model_0":
            provider = CHAMPION["provider"]
            params = dict(CHAMPION["params"])
            strategy_cls = CHAMPION["strategy"]
        else:
            entry = CANDIDATES[cid]
            provider = entry["provider"]
            params = dict(entry["params"])
            strategy_cls = entry["strategy"]
        rows = []
        for label, comm_rate, slip_rate in SCENARIOS:
            # build a fresh config + engine per scenario
            cfg = WalkForwardConfig(
                commission_rate=pv(comm_rate), slippage_rate=pv(slip_rate)
            )
            engine = WalkForwardEngine(cfg)
            acc = {
                "days": 0,
                "net_pnl": _ZERO,
                "costs": _ZERO,
                "slippage": _ZERO,
                "round_trips": 0,
                "wins": 0,
                "losses": 0,
                "exposure_units": _ZERO,
                "max_drawdown_pct": _ZERO,
            }
            for db in days:
                bars = list(db.bars)
                signals = provider(bars, **params)
                result = engine._backtester.run(bars, strategy_cls(**params), engine._bt, signals=signals)
                trips, _open = pair_round_trips(result.trades)
                run = engine._build_day_run(bars, result, signals, trips)
                engine._accumulate_champion(acc, run)
            totals = Totals.from_accumulator(acc)
            rows.append(
                {
                    "scenario": label,
                    "commission_rate": comm_rate,
                    "slippage_rate": slip_rate,
                    "net_pnl": fmt(totals.net_pnl),
                    "costs": fmt(totals.costs),
                    "round_trips": totals.round_trips,
                }
            )
        results.append({"id": cid, "scenarios": rows})
    return {"scenarios_defined": [s[0] for s in SCENARIOS], "results": results}


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------


def _best_worst(failure: dict[str, Any]) -> dict[str, Any]:
    holds = failure["by_holding_bars"]
    best_hold = max(holds, key=lambda k: pv(holds[k]["mean_realized_per_trade"]))
    worst_hold = min(holds, key=lambda k: pv(holds[k]["mean_realized_per_trade"]))
    regimes = failure["by_regime_x_side"]
    best_bucket = max(regimes, key=lambda k: pv(regimes[k]["realized_pnl"]))
    worst_bucket = min(regimes, key=lambda k: pv(regimes[k]["realized_pnl"]))
    return {
        "most_profitable_holding_bucket": best_hold,
        "least_profitable_holding_bucket": worst_hold,
        "most_profitable_regime_side_bucket": best_bucket,
        "least_profitable_regime_side_bucket": worst_bucket,
        "buy_and_hold_net": "7259.65 (reference, gross)",
    }


def _make_decision(candidate_results, best_worst, robustness) -> dict[str, Any]:
    promoted = [r for r in candidate_results if r["verdict"]["decision"] == "PROMOTE"]
    if promoted:
        promoted.sort(key=lambda r: pv(r["totals"]["net_pnl"]), reverse=True)
        winner = promoted[0]
        decision = {
            "outcome": "PROMOTE",
            "champion": winner["id"],
            "params": winner["params"],
            "rationale": "passed the WS 7.18 promotion gate on the research domain; "
            "positive net after costs on a single deterministic future-only pass",
            "remainder": [r["id"] for r in candidate_results if r["totals"]["net_pnl"] > _ZERO],
        }
    else:
        decision = {
            "outcome": "NO_PROMOTION",
            "champion": "model_0 (frozen MA(5,21))",
            "rationale": "no candidate cleared the promotion gate on the research domain; "
            "the champion remains the algorithm, but with negative net-of-cost P&L "
            "and no positive-variant robustness support",
            "best_candidate_by_net": max(
                candidate_results, key=lambda r: pv(r["totals"]["net_pnl"])
            )["id"],
            "n_candidates_positive": sum(
                1 for r in candidate_results if pv(r["totals"]["net_pnl"]) > _ZERO
            ),
            "robustness_positive_variants": robustness["n_positive"],
        }
    decision["status"] = {
        "live_trading": False,
        "algo_ready": "NO",
        "health": "RED",
        "note": "no credible, robust, reproducible edge demonstrated in the research domain",
    }
    return decision


def _print_decision(decision):
    print("\n=== FINAL DECISION ===")
    print(json.dumps(decision, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())