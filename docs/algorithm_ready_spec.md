# ALGO READY / ALGORITHM HEALTH — Design Spec

> Status: **IMPLEMENTED** — `evaluation/algorithm_health.py` +
> `scripts/seed_algorithm_ledger.py` + `scripts/assess_algorithm_health.py` +
> `docs/project_status.html` ("ALGORITHM READY / HEALTH" section).
> This doc is the reference for every threshold the monitor uses.

## 1. Purpose

The ALGO READY / ALGORITHM HEALTH monitor is an objective, evidence-only status
for the paper-trading platform. It answers two questions on every checkpoint:

* **ALGORITHM HEALTH** — GREEN (healthy) / YELLOW (monitor) / RED (not ready /
  investigation required).
* **ALGO READY** — YES / MONITOR / NO (a **paper-trading readiness** indicator;
  it never authorizes real-money trading or live broker execution).

The monitor never infers health from win rate alone. A profitable-looking win
rate with negative expectancy is RED by construction. No threshold here can be
loosened to make the algorithm appear healthy.

## 2. Data buckets (never mixed)

| Bucket | Source | Definition |
|---|---|---|
| `backtest` | `reports/model_performance/trades.csv` (recorded champion MA(5,21) continuous 5m replay) | round trips entered before 2026-01-01 (design+validation) |
| `protected_oos` | same csv | round trips entered on/after 2026-01-01, **excluding** trips entered before the fresh-OOS first-signal time (09:15 + 22×5m) that use pre-OOS history — this slice reconciles exactly with the recorded OOS confirmation (champion OOS net P&L −21,759.37) |
| `paper` | `reports/algorithm_state/paper_trades.json` (recorded closed paper trades) | live paper-session closed trades (empty until paper trades exist) |
| `today` | subset of `paper` | paper trades closed on the current IST date |
| `rolling_recent` | newest evidence source (paper, else protected OOS) | last 20 closed trades of that source |

The seeder cross-checks its sums/counts against the recorded champion artifacts
(`summary.json` full replay net P&L −143,210.37, 2,601 trades; OOS confirmation
−21,759.37) and fails if any mismatch exceeds ₹0.01.

## 3. Metrics (per bucket, from recorded trades only)

Win rate %; winning/losing/closed counts; profit factor; net P&L; average
winning/losing trade; expectancy per trade; one-sample t-statistic of net P&L;
maximum drawdown (amount + %); current drawdown; consecutive wins / losses;
last-10 and last-20 win rate; today's win rate (paper bucket); average signal
confidence (None for the MA(5,21) baseline, which emits no confidence);
sample-sufficiency flag.

Drawdowns are computed on the closed-trade **cumulative P&L** (equity proxy) —
per-trade records carry no intra-trade equity. Documented simplification. Any
bucket with fewer than `min_bucket_trades_for_metrics` trades is marked
insufficient, never extrapolated.

## 4. Health thresholds (objective, documented)

| Threshold | Value | Meaning |
|---|---|---|
| `min_backtest_trades_green` | 500 | GREEN needs at least this many backtest trades |
| `min_oos_trades_green` | 30 | GREEN needs at least this many protected-OOS trades |
| `min_bucket_trades_for_metrics` | 10 | below this, per-bucket metrics are "insufficient" |
| `min_segment_trades_for_trend` | 100 | trend needs this many trades per time segment |
| `min_paper_trades_for_ready` | 10 | ALGO READY == YES needs this many closed paper trades |
| `profit_factor_green` | 1.0 | GREEN requires profit factor ≥ 1.0 |
| `expectancy_zero` | 0 | expectancy must be strictly positive for GREEN |
| `trend_tolerance_ratio` | 0.10 | trend change within ±10% is STABLE |

### 4.1 Health decision rules

**RED — NOT READY / INVESTIGATION REQUIRED** (any of):
1. hard integrity failure — risk-control violation, simulation/execution anomaly,
   or recorded dataset data-quality failure;
2. backtest total ≥ 500 and backtest net expectancy ≤ 0;
3. protected OOS total ≥ 30 and OOS net expectancy ≤ 0.

**YELLOW — MONITOR:** neither GREEN nor RED fired (insufficient or mixed
evidence, or confidence uncalibrated).

**GREEN — HEALTHY** (all of):
* backtest ≥ 500 trades **and** net expectancy > 0;
* protected OOS ≥ 30 trades **and** net expectancy > 0;
* profit factor ≥ 1.0 on both backtest and OOS;
* no hard integrity failure and confidence not uncalibrated;
* a sufficient sample in both buckets (small samples are always clearly marked).

### 4.2 ALGO READY decision rules (paper-trading readiness)

* **YES** — health GREEN **and** ≥ 10 recorded closed paper trades **and** the
  paper evidence is not deteriorating.
* **MONITOR** — health YELLOW, or health GREEN but the live paper sample is still
  below 10;
* **NO** — health RED.

An algorithm can never be "READY" merely because its win rate exceeds a
percentage. Readiness requires positive expectancy evidence plus a live paper
sample.

## 5. Performance trend

Yearly time segments (oldest → newest) with a minimum of
`min_segment_trades_for_trend` trades each. The mean expectancy of the newer
half of the segments is compared with the older half (middle segment excluded
on an odd count):

* relative change > +10% → **IMPROVING**
* relative change < −10% → **DETERIORATING**
* otherwise → **STABLE**
* fewer than 3 sufficiently-sized segments → **INSUFFICIENT DATA**

Trends use actual recorded results only; missing results are never
extrapolated.

## 6. Trade-by-trade connection

Whenever a paper trade closes the operator/agent pipeline must: (1) record the
trade in `reports/algorithm_state/paper_trades.json`; (2) re-run
`scripts/seed_algorithm_ledger.py`; (3) re-run `scripts/assess_algorithm_health.py`
to update metrics, health, readates and the live dashboard; (4) persist into
`docs/project_state.json`; (5) commit + push. A single losing trade never
changes the core algorithm — the learning system may only diagnose it and
propose parameter/configuration changes that require evidence and validation
(per PROJECT_PLAN §17f).

## 7. Audit checklist

Every relevant audit verifies that:

* metrics are computed from recorded trades only (never fabricated);
* datasets are correctly separated (backtest / OOS / paper / today / rolling);
* no look-ahead bias and no future-data use (decision-time replay discipline);
* win rate, P&L, expectancy, drawdown, rolling windows are computed correctly
  (covered by `tests/test_algorithm_health.py`);
* version attribution is correct (bucket records carry algorithm + config
  version);
* protected-OOS results remain protected (single-use; this monitor only reads
  the already-recorded confirmation);
* insufficient sample sizes are clearly marked (`sample_sufficient`, trend
  INSUFFICIENT DATA);
* ALGO READY is never inferred from win rate alone;
* safety controls remain unchanged (monitor is read-only; real money disabled).

## 8. Current recorded status (2026-09-13)

From the recorded champion MA(5,21) replay (2,601 trades; OOS reconciled):

* **ALGORITHM HEALTH: RED** — backtest net expectancy ≈ −42.1/trade and OOS net
  expectancy ≈ −56.7/trade on 384 OOS trades, both ≤ 0 with sufficient samples;
  OOS per-trade t ≈ −6.76.
* **ALGO READY: NO** — health RED; the baseline shows no credible positive edge.
* **Performance trend: DETERIORATING** — newer-half expectancy worse than
  older-half (2022 ≈ −38.8/trade → 2025 ≈ −66.0/trade → 2026 ≈ −56.7/trade).
* Paper trades recorded: **0** (no live paper-session trades yet).

These are *paper-trading* readiness facts. They are evidence to preserve, not a
reason to manufacture a positive result. Candidate improvements are gated by the
WS 7.16 promotion discipline before any readiness change may occur.