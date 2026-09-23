# Day-by-Day Research — Iteration 001 (ADX floor for composite_trend)

- run_id: `day_batch_1_baseline_20260915_074922` / `day_batch_1_ab_adx25_20260915_080130`
  / `day_batch_2_baseline_20260915_080449` / `day_batch_2_ab_adx25_20260915_080752`
- session: 2026-09-15 (UTC)
- dataset: `datasets/upstox_Nifty_50_5m_20220103_20260911.csv`, hash `6c400b016c1c…`,
  pre-OOS domain 2022-01-03..2025-10-03 = 69,781 bars
- protected OOS boundary: **2025-10-06** (never loaded / never tuned on)

## 1. Protected OOS status
No bar, statistic, or parameter from 2025-10-06+ was used anywhere in this iteration.
All replays truncated the domain at 2025-10-03 (last pre-OOS bar). OOS stays untouched.

## 2. What was replayed (baseline, batch 1)
Continuous pre-OOS domain replay (69,781 bars) through the existing BacktestEngine +
EvaluationConfig().backtest() costs (capital 100k, qty 1, commission 0.0003, slippage 0.001,
2% protective stop, daily-loss risk controls). Champion = frozen MA(5,21) (decision stream
verified exact vs `MovingAverageCrossStrategy.analyze()` stepwise); challengers = the three
educational composite presets (MultiIndicatorStrategy mode=trend/mean_reversion/breakout),
replayed through the latched `signals_for` engine hook (O(n), no signal stacking).

Batch 1 (5 representative pre-OOS days, one per market condition):
2025-04-08 (volatile), 2025-09-04 (trending_down), 2025-09-11 (low_vol),
2025-09-30 (sideways), 2025-10-01 (trending_up).

Baseline day results (day P&P = equity delta over the day incl. carry MTM; realized separate):

| strategy | 04-08 | 09-04 | 09-11 | 09-30 | 10-01 | batch realized |
|---|---|---|---|---|---|---|
| champion_ma521 | +17687.33 / -421.19 | -14654.14 / -53.54 | -1125.62 / -152.76 | -1335.23 / -160.42 | -149.31 / -143.93 | -931.84 |
| composite_trend | +6222.93 / +286.06 | -5097.45 / +109.00 | -183.56 / -77.90 | -879.35 / -144.07 | +75.17 / 0 | +173.09 |
| composite_mean_reversion | +4149.91 / 0 | -3434.92 / -214.33 | -297.41 / 0 | -172.95 / +64.93 | -203.03 / 0 | -149.40 |
| composite_breakout | +6388.85 / +286.06 | -5165.74 / -54.85 | -207.98 / 0 | -1061.70 / -138.99 | +151.52 / 0 | +92.22 |

Every strategy is net-negative per ~95% of days historically; the champion's account equity
is ~5.5k by 2025-04 (consistent with recorded 2024–25 window -74,190, costs-dominated).

## 3. Day-by-day diagnosis (composite_trend focus)
Full per-bar journals + round-trip tables in the baseline run_dir
(journal_composite_trend.csv, round_trips.csv).

- 04-08 (volatile, up): SELL 10:35 (+275), BUY 11:05→SELL 13:45 (+10). Won both. ADX at
  entries 44.0 / 35.3 / 33.7.
- 09-04 (trending_down): covered short +109 (ADX 28.0). Carried LONG bled -5,097 day-PL
  before the flip — a regime-lag carry, not an intraday mis-execution.
- 09-11 (low_vol): BUY 09:35 conf 0.86, ADX **20.3** → -77.9; then flip-chop SELL→BUY
  11:35→12:05 (ADX 22.0 / **20.1**) → overnight carry lost -183.
- 09-30 (sideways): BUY 09:20 ADX **21.6** → closed 15:20 -144 (session-open entry chopped all day).
- 10-01 (trending_up): BUY 11:35 ADX 20.4 (held; flat real).

**Finding (100% separation on the batch):** every composite_trend entry with ADX ≥ 28.0 at
entry won; every entry with ADX ≤ 22.0 lost. All 4 losing entries sat at the 20.0 ADX floor.

## 4. Hypothesis (ONE, pre-registered before the A/B arm)
H1: "composite_trend's directional entries taken at ADX 20–22 (just above the 20.0 floor) —
typical of range/low-vol days — are noise; the batch's winners all entered at ADX ≥ 28. Raising
the trend-mode ADX strength floor (adx_min) from 20 to 25 should suppress the boundary entries
while preserving the winning entries."

- WHY: indicator guide + vote discipline: trend entries should demand measurable trend strength.
- CHANGE (ONE param, ONE preset): composite_trend `adx_min: 20 → 25`.
- Meets "no risk-control weakening": only entry strength gate; 2% stop, sizing, daily-loss
  controls unchanged; champion + mean_reversion + breakout unchanged (controls).

## 5. A/B on the exact same 5 days (same engine, same config, full-domain replay)
composite_trend realized on the 5 days: baseline **+173.09** → A/B(adx_min=25) **-285.94**.
The change made the target-day batch WORSE, not better.

Cause (attribution): the engine is fully stateful across the pre-OOS domain; raising adx_min
changed flips on earlier 2025 days, so by 04-08 the two arms held OPPOSITE carried positions
(baseline LONG → +275/+10 wins; A/B SHORT → -228.43 close) and 09-04's +109 winner vanished
(the short never opened). Keeping the winners / cutting only losers did NOT reproduce —
**the mechanism claimed by H1 failed**.

## 6. Repeatability (batch 2)
Batch 2 = next chronological pre-OOS days after 2025-10-01 = **only 2025-10-03**
(2025-10-02 absent from dataset; 10-04..05 weekend). Documented limitation: 1-day repeatability.
composite_trend: baseline realized -10.53 (1 losing RT) → A/B 0.00 (flat). Direction consistent
with "fewer boundary trades" but N=1, no significance.

## 7. FULL-DOMAIN secondary observation (NOT promotion evidence)
Same variant vs baseline over the entire pre-OOS domain (composite_trend alone):

| arm | RTs | W/L | realized | fees | net equity |
|---|---|---|---|---|---|
| baseline | 1767 | 244/639 | -28,431.95 | 10,921.16 | -39,382.81 |
| adx_min 25 | 1167 | 180/403 | -22,476.00 | 7,226.72 | **-29,856.34** |

34% fewer round trips, ~3,694 ₹ fewer fees, net loss reduced ~24%. This is a DIFFERENT claim
from H1 (cost reduction, not the targeted day-level mechanism). Recorded as motivation for a
SEPARATE future single-change iteration (pre-OOS only) — NOT used to flip this verdict.

## 8. Decision
**H1 REJECTED** on the pre-registered day-level criterion (A/B did not improve the 5-day batch;
the keep-winners/cut-losers mechanism did not reproduce).
Promotion: NONE. Registry/champion: untouched. No code algorithm change accepted this iteration.

## 9. ALGO READY
**NO.** (index/benchmark results must not be represented as option CALL/PUT profitability;
OPTION DATA = NOT AVAILABLE / NOT PROVEN.)

## 10. Compliance / integrity
- Live gate CLOSED; zero live orders; no credential use. Risk controls (2% stop, sizing,
  daily-loss limits) preserved unchanged in both arms.
- Frozen MA(5,21) baseline unmodified; looped champion signals verified decision-exact vs analyze().
- No historical result rewritten; no protected OOS loaded.
- Engineering infra carried forward: O(n) sma/vwap (zero-volume fallback = anchor-window
  typical mean), latched O(n) composite signal streaming, engine signals_for hook, causal Wilder,
  full-suite tests green (artifacts: 1_baseline, 1_ab_adx25).
- All artifacts immutable under `runs/research/day_batch/<run_id>/` (summary.json,
  day_stats.json, metrics_table.csv, journal_*.csv, round_trips.csv).
- Batch-2 shortfall and the 04-08/09-04 cascade contamination are documented above.

## 11. Next iteration (NOT this one)
Candidate for iteration 002 (single change, pre-registered before running): the full-domain
cost-reduction evidence above motivates re-testing `adx_min 20→25` on composite_trend with the
*day-level* claim REPLACED by a *domain-level cost/efficiency* claim, plus a fresh batch of 5
pre-OOS days not used today (to avoid the tested-day bias). Remains pre-OOS only, ALGO READY NO.

## 12. Corrigendum (iteration 003 housekeeping — reporting taxonomy)
Additive; no economic values rewritten. Label fixes only, to the Research-Quality-Audit §10
standard (`evaluation/metrics.py` is the canonical taxonomy from iteration 003 on).
- §7 table: the "RTs" column header is WRONG. 1767 / 1167 are **FILLS** (executed entry+exit
  fills), not round trips. Round trips were 883 (baseline) / ~583 (adx25); W/L are closed
  round trips by sign of realized P&L.
- §7 "net equity" column = `BacktestResult.total_pnl` (realized P&L + final carry MTM −
  commissions) — the canonical NET. It is NOT realized − commissions.
- §2 "batch realized": per-day `realized_pnl` sums only realized P&L on those dates (costs and
  carry MTM reported separately in each cell's second number). Label stays as stated.
- No economic value was changed anywhere in this file; only these labels/definitions are now
  unambiguous (`FILL` / `ROUND TRIP` / `NET`).