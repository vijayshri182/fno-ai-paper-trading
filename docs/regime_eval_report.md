# WS 7.6 — Regime-Aware Strategy Evaluation (recorded champion MA(5,21))

> Work stream 7.6 of PROJECT_PLAN §17d. **Evaluation-only.** No order was
> placed, no risk/execution code changed, and the frozen baseline MA(5,21) was
> not modified. The protected single-use out-of-sample window is never read for
> any computation here.

## 1. Scope

Investigate the §17e.5 regime-filter hypotheses for the registered champion
(MA(5,21), frozen V1 baseline):

| Hyp | Hypothesis (§17e.5) | Gate |
|---|---|---|
| R1 | strong trend → allow crossover signals | BUY gate / trend strength |
| R2 | sideways/choppy → prefer HOLD / avoid weak entries | SHORT gate |
| R3 | high volatility → reduced risk/position size | sizing |
| R4 | weak signal → HOLD | HOLD, needs signal strength |
| R5 | strong signal + confirmation → allow recommendation | HOLD, needs confirmation |

## 2. Evidence used (recorded, never re-run on OOS)

* `reports/model_performance/trades.csv` — the recorded continuous replay of the
  frozen champion over real NIFTY 50 5-minute bars (2,601 round trips,
  2022-01-03 … 2026-09-11), each carrying decision-time `entry_regime`,
  `price_pnl`, `commission`, `net_pnl`.
* `reports/model_performance/summary.json` — recorded aggregates (net P&L
  −143,210.37, 2,601 trades, per-regime rows all net-negative).
* Recorded WS 7.16 OOS confirmation — the protected OOS (entries ≥ 2026-01-01)
  was **separated, not reused**: the safe design+validation slice made of the
  2,216 trades entered before the OOS start is the only data any hypothesis is
  computed on. On top of it, the recorded single-use OOS results for the closest
  prior tests (WS 7.11 `RegimeFilteredMovingAverageCross`; WS 7.16 candidates)
  are cited as facts, not recomputed.

## 3. Structural result first

The champion **never enters long**: 0 of 2,601 recorded round trips are BUY
(0 of 2,216 on the safe slice). Every entry is a SELL. Therefore:

* every **BUY-side** regime filter — including WS 7.11
  `RegimeFilteredMovingAverageCross` (suppresses BUY only) — is a **structural
  no-op** on this champion: the filtered candidate is byte-for-byte the baseline.
* the only meaningful question left is whether a **SHORT-side** gate (R2/R4/
  confirmation) can turn the residual into a positive edge.

## 4. Empirical results (safe slice, 2,216 recorded trades)

Baseline: net P&L −121,332.89, expectancy **−54.75/trade**, win rate 12.64%.

Per decision-time entry regime — **all negative**:

| Regime | n | Win rate | Net P&L | Expectancy |
|---|---|---|---|---|
| sideways_normal | 891 | 14.48% | −46,956.31 | −52.70 |
| sideways_low | 817 | 9.18% | −45,320.42 | −55.47 |
| sideways_high | 374 | 14.71% | −20,992.60 | −56.13 |
| down_high | 80 | 15.00% | −5,470.55 | −68.38 |
| down_normal | 32 | 18.75% | −1,145.48 | −35.80 |
| down_low | 22 | 13.64% | −1,447.53 | −65.80 |

By trend (shorts only): sideways 2,082 trades / −54.40 per trade; down 134 /
−60.18. No UP-trend-regime entries exist to gate.

By volatility: normal 923 / −52.11; low 839 / −55.74; high 454 / −58.29 — all
negative.

SHORT-gate simulations (suppress entries when trend ∈ set; residual stats):

| Gate | Removed | Residual n | Residual expectancy |
|---|---|---|---|
| hold when sideways | 2,082 | 134 | **−60.18** |
| hold when up | 0 | 2,216 | −54.75 (=baseline, no-op proof) |
| hold when sideways/up | 2,082 | 134 | −60.18 |
| hold when down | 134 | 2,082 | −54.40 |

## 5. Hypothesis verdicts (objective)

* **R1 (BUY gate).** `structural_no_op` — 0 long entries; WS 7.11 filtered ≡
  baseline. Not promotable.
* **R2 (avoid sideways entries).** `rejected` — the residual (down-only, 134
  trades) is still −60.18/trade; on the recorded protected OOS the closest
  prior trend-gate candidate (WS 7.16 H4) was rejected (net −3.59%, per-trade
  −57.52, t −2.95).
* **R3 (volatility sizing).** `rejected` — sizing changes magnitude, never sign;
  all volatility groups negative.
* **R4 (weak signal → HOLD).** `not_testable_recorded` — MA(5,21) emits a binary
  crossover with no strength output; the flat-gap proxy (sideways) is already
  rejected under R2.
* **R5 (confirmation gate).** `not_testable_recorded` — needs next-bar
  alignment not present in the recorded round trips; nearest recorded test
  (WS 7.16 H4) was rejected on protected OOS.

**Conclusion.** On recorded evidence, no §17e.5 regime-filter hypothesis shows a
credible, robust edge for this family. The regime-aware improvement hypotheses
remain inert; the frozen baseline MA(5,21) is unchanged and no promotion is
executed. Any future candidate must be evaluated with a **fresh** untouched
out-of-sample window (the current one is single-use and already consumed by
WS 7.16).

## 6. Reproducibility

* Module: `evaluation/regime_eval.py`
* CLI: `scripts/eval_regime_hypotheses.py` → writes
  `reports/regime_eval/regime_eval.json` (evidence snapshot, git-ignored)
* Tests: `tests/test_regime_eval.py` (14 tests), including a recorded-replay
  reconciliation test (safe slice 2,216 = ledger backtest bucket; per-regime
  partition sums exactly to baseline → no double counting).

## 7. Safety statement

PAPER ONLY. Nothing in this work-stream places orders, alters risk/position
sizing/stop-loss logic, contacts a broker, or modifies recorded state beyond
this report and the dashboard. Real-money execution remains disabled.