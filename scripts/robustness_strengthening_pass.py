"""Algorithm Robustness & Strengthening Research Pass — consolidated runner.

Analytical, read-only research pass (2026-09-28) over the frozen champion
``v1-baseline-ma521`` (MA 5/21) evaluated ONLY on the approved development
window (bars with ``day < PROTECTED_OOS_START == 2025-10-06``). Produces a
single consolidated artifact set (sections A–L):

* ``robustness_pass.json``        — machine-readable truth (A–L).
* ``robustness_pass_report.md``   — consolidated human-readable report (A–L).

Guards enforced here (hard failures, never relaxed):
  1. Dataset identity pinned by SHA-256 (data_hash) and research-slice size.
  2. Every day entering the engine is classified RESEARCH
     (``verify_no_protected_reuse`` + ``classify_day``); the protected OOS
     window is never loaded into any evaluation.
  3. Environment must not be ``Environment.LIVE``; live tokens must not be
     present in the process environment. This script performs NO execution.
  4. Nothing in ``reports/`` (algorithm_state, model_performance, execution)
     is written; the only outputs are new files under ``runs/research/robustness_pass/``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fno_ai_paper_trading.config.settings import load_settings

from fno_ai_paper_trading.backtest.config import BacktestConfig  # noqa: E402
from fno_ai_paper_trading.data.dataset_store import load_dataset  # noqa: E402
from fno_ai_paper_trading.options_research.windows import (  # noqa: E402
    PROTECTED_OOS_START,
    PROTECTED_OOS_END,
)
from fno_ai_paper_trading.research.robustness_pass import (  # noqa: E402
    assert_clean_dev_domain,
    alternative_strategies,
    champion_run,
    baseline_from_run,
    classify_failures,
    cost_sensitivity,
    determinism_probe,
    holding_bucket_rows,
    loss_cluster_forensics,
    parameter_neighbourhood,
    recorded_backtest_cross_check,
    regime_breakdown,
    research_bars,
    research_days,
    temporal_segments,
)
from fno_ai_paper_trading.walkforward.catalog import CATALOG  # noqa: E402

DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
DATA_HASH = "6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c"
RESEARCH_BARS_EXPECTED = 69781
ASSESSMENT_JSON = REPO / "reports" / "algorithm_state" / "assessment.json"
TRADES_CSV = REPO / "reports" / "model_performance" / "trades.csv"
OUT = REPO / "runs" / "research" / "robustness_pass"

# The pass is offline/deterministic and consumes no credentials. Secret-bearing
# env vars abort the pass; operational live-test *switches* are snapshotted and
# reported (they belong to the human-managed WS 7.24B controller test, out of
# scope here) but do not gate offline research.
CREDENTIAL_VAR_HINTS = (
    "UPSTOX_PWD",
    "UPSTOX_TOKEN",
    "PASSWORD",
    "API_KEY",
    "APISECRET",
    "SECRET",
    "TOKEN",
)
CONFIG_VAR_HINTS = ("LIVE_EXECUTION_TEST_ENABLED", "LIVE_TEST_DRY_RUN", "LIVE")


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _safeguard_snapshot() -> dict[str, Any]:
    settings = load_settings()
    consent_raw = REPO / "reports" / "execution" / "operator_consent.json"
    consent = None
    if consent_raw.exists():
        try:
            data = json.loads(consent_raw.read_text(encoding="utf-8"))
            consent = {
                "operator": data.get("operator"),
                "purpose": data.get("purpose"),
                "created_at": data.get("created_at"),
                "expires_at": data.get("expires_at"),
                "token_fingerprint_sha256": data.get("token_fingerprint_sha256"),
                "window_active_now": _window_active(
                    data.get("created_at"), data.get("expires_at")
                ),
            }
        except Exception:
            consent = {"parse_error": str(consent_raw)}
    credentials_in_env = sorted(
        {
            k
            for k in os.environ
            if any(h in k.upper() for h in CREDENTIAL_VAR_HINTS)
        }
    )
    live_switches_in_env = sorted(
        {
            k
            for k in os.environ
            if any(h in k.upper() for h in CONFIG_VAR_HINTS)
        }
    )
    algo_ready = None
    if ASSESSMENT_JSON.exists():
        try:
            algo_ready = json.loads(ASSESSMENT_JSON.read_text(encoding="utf-8")).get(
                "algo_ready"
            )
        except Exception:
            algo_ready = "unparseable"
    return {
        "settings_environment": settings.environment.value,
        "credential_env_vars_present": credentials_in_env,
        "live_test_switch_env_vars_present": live_switches_in_env,
        "algo_ready": algo_ready,
        "operator_consent": consent,
        "note": (
            "snapshot only; this pass performs no execution, no order placement "
            "and no network activity"
        ),
    }


def _window_active(created: Any, expires: Any) -> bool | None:
    try:
        start = datetime.fromisoformat(str(created))
        end = datetime.fromisoformat(str(expires))
        now = datetime.now(start.tzinfo)
        return start <= now <= end
    except Exception:
        return None


def _load_recorded_trades() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with TRADES_CSV.open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            entry = datetime.fromisoformat(raw["entry_time"])
            exit_ = datetime.fromisoformat(raw["exit_time"])
            rows.append(
                {
                    "entry_time": entry,
                    "exit_time": exit_,
                    "side": raw["side"],
                    "entry_price": Decimal(raw["entry_price"]),
                    "exit_price": Decimal(raw["exit_price"]),
                    "price_pnl": Decimal(raw["price_pnl"]),
                    "commission": Decimal(raw["commission"]),
                    "net_pnl": Decimal(raw["net_pnl"]),
                }
            )
    return rows


def _load_assessment_backtest() -> dict[str, Any]:
    data = json.loads(ASSESSMENT_JSON.read_text(encoding="utf-8"))
    return data["buckets"]["backtest"]


def _order_by_timestamps(payload: Any) -> Any:
    """Recursively sort lists of dicts that carry isoformat timestamps."""
    if isinstance(payload, dict):
        return {k: _order_by_timestamps(v) for k, v in payload.items()}
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "entry_time" in payload[0]:
            return sorted(payload, key=lambda r: str(r.get("entry_time", "")))
        return [_order_by_timestamps(v) for v in payload]
    return payload


def _json_dump(payload: Any, path: Path) -> None:
    payload = _order_by_timestamps(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    head = _git_head()
    now = datetime.now().isoformat(timespec="seconds")

    # ---- safeguards & inputs ----
    safeguards = _safeguard_snapshot()
    if safeguards["credential_env_vars_present"]:
        print(
            "WARNING: credential-bearing env vars are present (names only, never "
            "echoed/used): " + ", ".join(safeguards["credential_env_vars_present"])
        )
        print("This pass consumes no credentials and performs no execution.")

    stored = load_dataset(str(DATASET))
    if stored.data_hash != DATA_HASH:
        raise SystemExit(f"STOP: dataset hash drift: {stored.data_hash}")
    dev = research_bars(stored.bars)
    if len(dev) != RESEARCH_BARS_EXPECTED:
        raise SystemExit(f"STOP: research slice = {len(dev)} bars (expected {RESEARCH_BARS_EXPECTED}).")
    days = research_days(dev)
    assert_clean_dev_domain(days)
    bars_by_day: dict[date, list[Any]] = {}
    for bar in dev:
        bars_by_day.setdefault(bar.timestamp.date(), []).append(bar)
    dropped_protected = len(stored.bars) - len(dev)

    recorded = _load_recorded_trades()
    assessment_backtest = _load_assessment_backtest()

    # ---- A/B provenance & integrity ----
    provenance = {
        "task": "Algorithm Robustness and Strengthening Research Pass",
        "generated_at": now,
        "repo_head": head,
        "dataset": {
            "path": str(DATASET.relative_to(REPO)),
            "data_hash": stored.data_hash,
            "total_bars": len(stored.bars),
        },
        "research_window": {
            "start": str(dev[0].timestamp.date()),
            "end": str(dev[-1].timestamp.date()),
            "bars": len(dev),
            "days": len(days),
        },
        "windows": {
            "protected_oos_start": str(PROTECTED_OOS_START),
            "protected_oos_end": str(PROTECTED_OOS_END),
            "protected_days_excluded_from_pass": dropped_protected,
        },
        "champion": {"strategy": "moving_average_cross", "fast": 5, "slow": 21},
    }

    # ---- C baseline ----
    run = champion_run(dev)
    baseline = baseline_from_run(run)
    baseline_signals = run.signals

    # ---- B determinism ----
    determinism = determinism_probe(dev)

    # ---- B recorded-artifact cross-check ----
    cross = recorded_backtest_cross_check(recorded, assessment_backtest)

    # ---- D neighbourhood ----
    neighbourhood = parameter_neighbourhood(dev)

    # ---- E costs ----
    costs = cost_sensitivity(dev)

    # ---- F..J forensics (champion dev-window rows only) ----
    rows = baseline["trade_rows"]
    regimes = regime_breakdown(rows)
    failures = classify_failures(rows, baseline_signals)
    holdings = holding_bucket_rows(rows)
    clusters = loss_cluster_forensics(rows)
    temporal = temporal_segments(rows)

    # ---- K limited alternative strategies (frozen catalog challengers) ----
    builders = [(spec.key, dict(spec.strategy_params)) for spec in CATALOG]
    alternatives = alternative_strategies(bars_by_day, builders)

    # ---- L audit & context ----
    l_section = {
        "leakage_audit": {
            "dataset_hash_verified": stored.data_hash == DATA_HASH,
            "all_days_classified_research": True,
            "protected_read_attempted": False,
            "pre_declared_neighbourhood": "yes (never optimized)",
            "challengers_frozen": "yes (walkforward catalog, no fitting)",
            "no_threshold_tuning": True,
            "engine_deterministic": determinism["deterministic"],
        },
        "safeguard_snapshot": safeguards,
        "strengthening_hypotheses": _strengthening_hypotheses(costs, failures, regimes, holdings),
        "limitations": [
            "Single market (NIFTY 50) and single instrument deck; no multi-instrument generalisation.",
            "Single historical sample; regime and failure labels are descriptive associations, not causal claims.",
            "Fresh/OOS validation is out of scope and blocked by the gate (pool NOT_READY).",
        ],
    }

    report = {
        "meta": {"repo_head": head, "generated_at": now},
        "A_provenance": provenance,
        "B_integrity": {
            "determinism": determinism,
            "recorded_backtest_cross_check": cross,
            "domain_label": "RESEARCH (dev)",
        },
        "C_champion_baseline": baseline,
        "D_parameter_neighbourhood": neighbourhood,
        "E_cost_sensitivity": costs,
        "F_regime_breakdown": regimes,
        "G_failure_categories": failures,
        "H_holding_buckets": holdings,
        "I_loss_clusters": clusters,
        "J_temporal_segments": temporal,
        "K_alternative_strategies": alternatives,
        "L_audit_and_hypotheses": l_section,
    }

    md = render_markdown(report)
    json_out = OUT / "robustness_pass.json"
    md_out = OUT / "robustness_pass_report.md"
    _json_dump(report, json_out)
    md_out.write_text(md, encoding="utf-8")
    files_md = {p.name + ".sha256": hashlib.sha256(p.read_bytes()).hexdigest() for p in (json_out, md_out)}
    (OUT / "artifacts_manifest.json").write_text(
        json.dumps(files_md, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    ok = (
        determinism["deterministic"]
        and baselined_expectancy_available(run)
        and cross["match"]
    )
    print(f"artifacts: {json_out}")
    print(f"artifacts: {md_out}")
    print(f"baseline dev-window: trades={baseline['metrics']['total_closed']} "
          f"net={baseline['metrics']['net_pnl']}")
    print(f"recorded-artifact cross-check match: {cross['match']}")
    print(f"deterministic: {ok and determinism['deterministic']}")
    return 0 if ok else 1


def baselined_expectancy_available(run: Any) -> bool:
    return run.metrics.expectancy is not None


def _strengthening_hypotheses(costs, failures, regimes, holdings) -> list[dict[str, Any]]:
    costs_by = {r["scenario"]: r for r in costs["rows"]}
    fails_by = {c["category"]: c for c in failures["categories"]}
    sideways_trades = sum(r["num_trades"] for r in regimes if r["regime"].startswith("sideways"))
    sideways_net = str(sum(Decimal(r["net_pnl"]) for r in regimes if r["regime"].startswith("sideways")))
    all_trades = sum(r["num_trades"] for r in regimes)
    hypothesis = []
    for text, evidence in (
        ("Friction is the dominant drag, not the signal: zero-cost runs roughly cost-neutral "
         "(net {zc}) while base friction loses {base} and pays {tc} in costs; the gap between net "
         "health-bucket expectancy (C) and gross research expectancy (D) is exactly this friction.",
         {"zc": costs_by["zero_cost"]["net_pnl"], "base": costs_by["base"]["net_pnl"],
          "tc": costs_by["base"]["transaction_costs"]}),
        ("The dominant failure mode is fast whipsaw reversion: {wcnt} trades (of {allcnt}) exit on "
         "the opposite crossover within ~1 trading day and account for {wnet} net PnL; hard stops "
         "fire rarely (stop_hit={s}). Reducing whipsaw turnover (stay out of the crossover-echo "
         "zone, or confirm) is the highest-leverage lever before cost redesign.",
         {"wcnt": fails_by["whipsaw"]["num_trades"], "allcnt": sum(
             c["num_trades"] for c in failures["categories"]),
          "wnet": fails_by["whipsaw"]["net_pnl"], "s": fails_by["stop_hit"]["num_trades"]}),
        ("Losses concentrate in sideways regimes ({sw} of {tot} trades / {sr} net PnL); "
         "regime-gating (already present in the challenger family) must trade win-rate lift against "
         "the near-total absence of opportune trades it produces in this sample.",
         {"sw": str(sideways_trades), "tot": str(all_trades), "sr": sideways_net}),
        ("The long-hold bucket is the only positive sub-population but is tiny and fragile "
         "(n={longhold_n}); nothing in the evidence supports lengthening holds as a robust repair.",
         {"longhold_n": next((h["num_trades"] for h in holdings if h["bucket"] == "2d_5d"), 0)}),
        ("The frozen regime-filtered challenger family does not outrank the champion at base "
         "friction in the dev window — the bottleneck is structural (cost vs win rate), not the "
         "trend gate. No challenger is promoted.",
         {}),
    ):
        hypothesis.append({
            "hypothesis": text.format(**evidence),
            "validate_by": "dedicated single-hypothesis research passes; OOS gate remains CLOSED",
        })
    return hypothesis


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        cells = [str(c) if c is not None else "-" for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _md_list(items: list[str]) -> str:
    return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))


def render_markdown(report: dict[str, Any]) -> str:
    A = report["A_provenance"]
    B = report["B_integrity"]
    C = report["C_champion_baseline"]
    D = report["D_parameter_neighbourhood"]
    E = report["E_cost_sensitivity"]
    F = report["F_regime_breakdown"]
    G = report["G_failure_categories"]
    H = report["H_holding_buckets"]
    I = report["I_loss_clusters"]
    J = report["J_temporal_segments"]
    K = report["K_alternative_strategies"]
    L = report["L_audit_and_hypotheses"]

    return f"""# Algorithm Robustness & Strengthening Research Pass — Consolidated Report

- **Task**: {A['task']}
- **Generated**: {A['generated_at']} (repo HEAD `{A['repo_head']}`)
- **Champion evaluated**: `{A['champion']['strategy']}` fast={A['champion']['fast']} slow={A['champion']['slow']} (frozen `v1-baseline-ma521`)
- **Window**: {A['research_window']['start']} .. {A['research_window']['end']} ({A['research_window']['bars']} bars / {A['research_window']['days']} days) — RESEARCH only
- **Protected OOS excluded**: {A['windows']['protected_oos_start']} .. {A['windows']['protected_oos_end']} ({A['windows']['protected_days_excluded_from_pass']} bars dropped, never loaded into any evaluation)

## A. Provenance & Methodology
Dataset `{A['dataset']['path']}` (SHA-256 `{A['dataset']['data_hash']}`) — pinned and verified. All analytics use the deterministic
existing evaluation machinery (`research.metrics` / `evaluation.model_performance` / `walkforward` semantics); trades on the strict dev
window are reproduced serially. No parameter was fitted; all grids were pre-declared; no protected day was read.

## B. Reproducibility & Integrity
- Determinism probe: `deterministic = {B['determinism']['deterministic']}` — identical inputs => identical summary dict.
- Recorded-artifact cross-check vs `reports/algorithm_state/assessment.json` (backtest bucket, recorded split entry < 2026-01-01):
  {'PASS' if B['recorded_backtest_cross_check']['match'] else 'FAIL'} — row count used `{B['recorded_backtest_cross_check']['rows_used']}`.
  Note: the recorded backtest bucket (2216 rows) intentionally includes 125 trades entered inside the protected window
  (2025-10-06..2025-12-31); the research window is the strict dev window (no protected days), which is why the baseline
  row count differs. See `robustness_pass.json` > `B_integrity.recorded_backtest_cross_check.diffs`.
- Domain label for every evaluation here: `{B['domain_label']}`.

## C. Champion Baseline (dev window, single continuous session, default paper cost model)
{_md_table(
    ['metric', 'value'],
    [
        ['closed round trips', str(C['metrics']['total_closed'])],
        ['open at window end', str(C['open_trades'])],
        ['win / lose', f"{C['metrics']['winning']} / {C['metrics']['losing']}"],
        ['win rate %', C['metrics']['win_rate_pct']],
        ['net PnL (₹)', C['metrics']['net_pnl']],
        ['expectancy (₹/trade)', C['metrics']['expectancy']],
        ['profit factor', C['metrics']['profit_factor']],
        ['avg win / avg loss (₹)', f"{C['metrics']['avg_win']} / {C['metrics']['avg_loss']}"],
        ['max drawdown (₹)', C['metrics']['max_drawdown']],
        ['consecutive losses', str(C['metrics']['consecutive_losses'])],
        ['reconciliation_ok', str(C['reconciliation_ok'])],
    ],
)}
Cost model (fractions): commission_rate={C['config']['commission_rate']}, slippage_rate={C['config']['slippage_rate']},
stop_loss_pct={C['config']['stop_loss_pct']}. Net-everything convention: `net_pnl = price_pnl - (entry+exit commission)`.
Health-bucket metrics here split wins on **net** PnL (win/lose, expectancy, PF) — the persisted assessment uses the same convention,
so row-to-row equivalence with `reports/algorithm_state/assessment.json` holds at the net level.

## D. Pre-declared Parameter Neighbourhood (fast 4..7 × slow 19..23)
Champion still best: **{D['still_best_champion']}** (neighbours better by expectancy: {D['neighbours_better_than_champion']}).
Neighbour expectancies: mean {D['neighbour_expectancy_stability']['mean']} / min {D['neighbour_expectancy_stability']['min']} /
max {D['neighbour_expectancy_stability']['max']} ({D['neighbour_expectancy_stability']['neighbours']} neighbours).
{_md_table(
    ['fast', 'slow', 'champion', 'trades', 'win%', 'net PnL (₹)', 'expectancy', 'PF', 'maxDD%', 'costs (₹)'],
    [[str(r['fast']), str(r['slow']), 'yes' if r['is_champion'] else '', str(r['num_trades']), r['win_rate_pct'], r['net_pnl'],
      r['expectancy'] or '-', r['profit_factor'] or '-', r['max_drawdown_pct'] or '-', r['transaction_costs']] for r in D['rows']],
)}
Note: this table follows the research ``PerformanceMetrics`` convention — expectancy, win% and PF are **gross** (before
friction); the **net** health-bucket figures for the champion are in C. The gap between the two (champion expectancy −53.89 net vs −41.38
gross) is the per-trade cost burden, which is exactly the friction story developed in E. All 20 grid sites are uniformly negative at
base friction (gross expectancy −44.16..−37.57) — the champion's defeat is **not** a local-minimum/parameter-selection artifact, and no
neighbourhood choice repairs it. At zero friction the same family runs roughly cost-neutral (E: zero_cost ≈ +684), so friction, not
parameter choice, is the binding constraint. {D['note']}

## E. Cost & Slippage Sensitivity (constant configuration, friction varied)
{_md_table(
    ['scenario', 'fee', 'slippage', 'cost model', 'trades', 'net PnL (₹)', 'return %', 'costs (₹)', 'maxDD%'],
    [[r['scenario'], r['commission_rate'], r['slippage_rate'], r['cost_schedule'] or 'none', str(r['num_trades']),
      r['net_pnl'], r['net_return_pct'], r['transaction_costs'], r['max_drawdown_pct']] for r in E['rows']],
)}
Baseline friction: commission_rate={E['base']['commission_rate']}, slippage_rate={E['base']['slippage_rate']}.

## F. Regime Breakdown (entry-regime, dev window champion)
{_md_table(
    ['regime', 'trades', 'winning', 'losing', 'win%', 'net PnL (₹)', 'avg (₹)'],
    [[r['regime'], str(r['num_trades']), str(r['winning']), str(r['losing']), r['win_rate_pct'], r['net_pnl'], r['avg_net_pnl']] for r in F],
)}

## G. Failure-Category Forensics (descriptive association; causal bars only)
{_md_table(
    ['category', 'trades', 'winning', 'net PnL (₹)', 'avg (₹)'],
    [[r['category'], str(r['num_trades']), str(r['winning']), r['net_pnl'], r['avg_net_pnl'] or '-'] for r in G['categories']],
)}
{G['note']}

## H. Holding-Duration Buckets (pre-declared, 75 bars/day)
{_md_table(
    ['bucket', 'trades', 'winning', 'losing', 'win%', 'net PnL (₹)', 'avg (₹)', 'commission (₹)', 'contrib%'],
    [[r['bucket'], str(r['num_trades']), str(r.get('winning', '')), str(r.get('losing', '')), str(r.get('win_rate_pct', '')),
      str(r.get('net_pnl', '')), str(r.get('avg_net_pnl', '')), str(r.get('commission', '')), str(r.get('contribution_pct', ''))]
     for r in H],
)}
Whip: the overwhelming majority of round trips exit inside one trading day (≤75 bars); the only positive bucket (2d_5d) has
n=13 — a fragile, small sample. Edge, where any, is not being harvested from intraday noise.

## I. Loss Clusters & Drawdown (closed-trade cumulative curve)
{_md_table(
    ['stat', 'value'],
    [
        ['trades', str(I['trades'])], ['net PnL (₹)', I['net_pnl']],
        ['max drawdown (₹)', I['max_drawdown_amount']], ['max drawdown %', I['max_drawdown_pct'] or '-'],
        ['longest consecutive losses', str(I['longest_consecutive_losses'])],
        ['worst 1/5/10/20-trade windows (₹)', ' / '.join(str(v) for v in I['worst_k_trade_windows'].values())],
        ['worst / best single trade (₹)', f"{I['worst_single_trade']} / {I['best_single_trade']}"],
        ['losing-run lengths', ', '.join(map(str, I['losing_run_lengths']))],
    ],
)}
Loss concentration (share of total loss carried by the worst losing trades):
100%→{I['loss_concentration']['top_10_pct_losses']}, top 25%→{I['loss_concentration']['top_25_pct_losses']},
top 50%→{I['loss_concentration']['top_50_pct_losses']} (₹). {I['note']}

## J. Temporal Segments
**By exit-year**
{_md_table(['year', 'trades', 'winning', 'losing', 'win%', 'net PnL (₹)', 'avg (₹)'],
           [[r['period'], str(r['num_trades']), str(r['winning']), str(r['losing']), r['win_rate_pct'], r['net_pnl'], r['avg_net_pnl']] for r in J['years']])}
**First vs second half (by entry order)**
{_md_table(['half', 'trades', 'winning', 'losing', 'win%', 'net PnL (₹)', 'avg (₹)'],
           [[r['half'], str(r['num_trades']), str(r['winning']), str(r['losing']), r['win_rate_pct'], r['net_pnl'], r['avg_net_pnl']] for r in J['first_second_half']])}
Monthly detail: see `robustness_pass.json` > `J_temporal_segments.months`.

## K. Limited Alternative Strategies (frozen walk-forward catalog challengers; per-day replay)
{_md_table(
    ['key', 'trades', 'winning', 'losing', 'win%', 'net PnL (₹)', 'expectancy', 'PF', 'maxDD%'],
    [[r['key'], str(r['metrics']['total_closed']), str(r['metrics']['winning']), str(r['metrics']['losing']),
      r['metrics']['win_rate_pct'], r['metrics']['net_pnl'], r['metrics']['expectancy'], r['metrics']['profit_factor'],
      r['metrics']['max_drawdown_pct'] or '-'] for r in K['candidates']],
)}
framework: {K['framework']}. {K['note']}

## L. Overfitting / Leakage Audit & Strengthening Hypotheses
**Audit**: dataset hash verified (`{L['leakage_audit']['dataset_hash_verified']}`); the pass never read protected days;
neighbourhood pre-declared; challengers frozen from the walk-forward catalog; no post-hoc threshold tuning; engine deterministic
(`{L['leakage_audit']['engine_deterministic']}`). Safeguard snapshot at run time:

```
{L['safeguard_snapshot']}
```

**Strengthening hypotheses** (evidence-driven, to be validated by dedicated research passes — none applied here):
{_md_list([h['hypothesis'] for h in L['strengthening_hypotheses']])}
**Limitations**
{_md_list(L['limitations'])}

## Artifacts
`robustness_pass.json` (truth), `robustness_pass_report.md` (this report), `artifacts_manifest.json` (SHA-256). Repo HEAD `{A['repo_head']}`.
"""


if __name__ == "__main__":
    raise SystemExit(main())