# Model Research Final Report — MA-Cross Family on Real NIFTY 50 5m

**Version:** 1.0 · **Date:** 2026-09-12 · **Status:** FINAL
**Deliverable for:** human review — primary engineering + research owner
**Stopping condition reached:** **B** (no credible, robust, reproducible edge demonstrated)

This report documents the complete controlled research cycle (WS 7.16) on the
frozen champion **MA(5,21)** and its pre-registered challengers. It follows the
20-item structure required before any further optimization or promotion is
justified. Everything here is **research evidence on historical data through the
deterministic paper engine only** — nothing trades live, nothing was routed to a
broker, and no risk/sizing/stop-loss/execution code was altered or bypassed.

---

## 1. Literature Research Summary

A short, honest summary of the background that informed this cycle (offline
sources; standard textbook/reference knowledge — no URLs are fabricated here):

- **Trend-following / moving-average crossovers** are among the oldest systematic
  signal families. Their well-known failure mode is churn in range-bound or
  sideways conditions: frequent alternating crossovers generate many small
  round trips whose aggregate, once realistic friction is applied, dominates any
  gross signal edge. Win rates materially below 50% are expected; the strategy
  is economically viable only when a small number of large winning trends more
  than pays for many small losers — the classic "trend-following payoff shape"
  (small negative expectancy per trade, positively-skewed tail, long holding
  periods on winners).
- **Crossovers and holding periods.** The champion audit showed exactly the
  opposite shape on real NIFTY 5m data: only the 51+ bar hold bucket was
  profitable (96.9% WR over 97 trades) while 70.7% of all trades were
  sub-20-bar holds and each sub-20-bar bucket was a net loss. This is
  documented evidence of **anti-trend churn**, consistent with the classic
  failure mode, not a one-off anomaly.
- **Regime-aware entry gating (momentum / trend filters).** A standard
  prescription for crossover churn is to require the entry to agree with a
  trend/momentum filter. The candidates c3/c4 operationalize this; the evidence
  below shows the filter reduces frequency and costs, but never produces a
  positive, robust, friction-adjusted edge on this window.
- **Larger-phase drift.** MA-cross gating that removes or reduces
  counter-trend exposure can "beat" the champion by losing less in a market the
  champion fights (NIFTY 5m up-crossing +34% over 2022–2026 while the champion
  realized only shorts). That is a **benchmark/cost artifact, not alpha**:
  after correcting for costs, the residual per-trade edge is decisively
  negative (section 6).

Net literature conclusion: the prescribed remedies were applied and tested;
they did not generate an edge — consistent with this family having no
exploitable edge at this frequency and offset after realistic cost.

## 2. Hypotheses (Registered)

Six hypotheses were registered **before** any segment results (nothing was
selected on out-of-sample data; nothing was retuned after seeing results):

| ID | Hypothesis | Decision rule (locked) | Rationale source |
|----|-----------|------------------------|------------------|
| H0 | Frozen champion MA(5,21) | both-side crossover, `BacktestConfig()` defaults | baseline; no change ever |
| H1 (c1) | Long-only MA(5,21) removes counter-trend shorts | up-cross opens, down-cross closes; no shorts | audit: 0/2601 long entries |
| H2 (c2) | Slower long-only MA(20,50) fixes sub-20-bar churn | long-only crossover, 20/50 | audit: hold buckets; flip spacing |
| H3 (c3) | Momentum-gated MA(5,21) suppresses churn both sides | prior 5-bar return sign gates each entry | audit: 51+ bar holds only profitable |
| H4 (c4) | Trend-gated MA(5,21) requires MA-gap trend sign | `|ma_gap| ≥ 0.05%` gates each entry | audit: regime matrix all negative |
| H5 (c5) | Donchian 20/10 breakout forces long multi-bar holds | channel breakout of prior 20-bar extremes, 10-bar exit | audit: 51+ bar hold bucket won |

Each hypothesis carries an explicit falsifiable claim: *"after realistic cost,
this rule nets a positive, robust, out-of-sample edge vs. the champion on the
same data/assumptions"*. None survived.

## 3. Methods & Reproducibility

- **Engine:** deterministic `BacktestEngine` (paper fills through
  `PaperBroker` → `Portfolio`, `RiskManager` enforced, 2% stop-loss when the
  stop fires before the next model signal), `Decimal` arithmetic, no wall-clock
  dependency.
- **Signals:** each candidate is an O(n) provider returning one `SignalResult`
  per bar, decided strictly from `bars[:i+1]` (no look-ahead). Equivalence
  `provider(bars)[i] == Strategy.analyze(bars[:i+1])` is asserted in
  `tests/test_research_candidates.py`, together with a future-perturbation
  no-look-ahead test and determinism tests.
- **Replay:** `candidates.run_candidate_plan` replays a full signal stream in
  one engine pass per segment and builds round trips via the same
  `pair_round_trips` path used for the champion.
- **Costs:** `BacktestConfig()` defaults (commission 0.0003 + slippage 0.001 of
  notional per fill) — identical for champion and challengers.
- **Metrics:** the shared `PerformanceMetrics` set (net P&L, net return %,
  max drawdown %, win rate, profit factor, transaction costs incl. separate
  commission/slippage).
- **Reproducibility commands** (identical inputs ⇒ identical outputs):
  1. `python scripts/audit_champion_failure.py` → `reports/model_performance/audit_champion_failure.json`
  2. `python scripts/evaluate_candidates.py` → `reports/model_performance/candidates_eval.json` (design+validation selection output)
  3. `python scripts/finalize_oos_confirmation.py` → `reports/model_performance/oos_confirmation.json` (the single OOS read)
- Source of truth for candidates: `src/fno_ai_paper_trading/strategies/research_candidates.py`
  (registry `CANDIDATES` with locked params, perturbation grids, rationale) and
  the harness `src/fno_ai_paper_trading/evaluation/candidates.py`.
- **Test gate:** full suite **843 passed** (813 baseline + 30 new candidate/harness tests), deterministic, offline.

## 4. Datasets

| Dataset | Bars | Range | Source | Status |
|---|---|---|---|---|
| `datasets/upstox_Nifty_50_5m_20220103_20260911.csv` | 87,193 | 2022-01-03 09:15 → 2026-09-11 15:25 | Upstox historical (read-only), NSE Index NIFTY 50 | validated; SHA-256 registered in meta; git-ignored |
| `datasets/upstox_Nifty_50_1d_20050103_20260911.csv` | 5,382 | 2005-01-03 → 2026-09-11 | Upstox (read-only) | validated; benchmark reference (buy-and-hold +27.96%) |

Segment design (locked before any evaluation): DESIGN/TRAIN = bars < 2025-07-01
(64,831 bars), VALIDATION = 2025-07-01..2025-12-31 (9,387 bars), PROTECTED OOS =
2026-01-01..2026-09-11 (12,975 bars). All candidate selection used design +
validation only.

## 5. Raw Quantitative Result Tables (per hypothesis)

All figures are net-of-cost (commission + slippage), the engine defaults, 5m bars,
on the same segments. `net %` is relative to the ₹100,000 virtual account.

**Champion H0 — MA(5,21) both sides:**

| Segment | Trades | Win % | Net % | Net P&L | MaxDD % | Friction ₹ | Recon |
|---|---|---|---|---|---|---|---|
| Full window | 2601 | 15.53 | **-143.21** | -143 210.37 | 143.29 | 146 128.77 | True |
| Design | 1944 | 14.71 | -104.12 | -104 119.83 | 104.13 | 103 840.48 | True |
| Validation | 271 | 17.34 | -16.49 | -16 492.74 | 16.68 | 17 860.09 | True |
| Protected OOS | 384 | — | -21.76 | -21 759.37 | 21.84 | — | True |

**Candidates H1–H5 (design / validation):**

| ID | Rule | Design trades | Design net % | Validation trades | Validation net % | Net delta vs champ (val) |
|---|---|---|---|---|---|---|
| H0 | MA(5,21) both | 1944 | -104.12 | 271 | -16.49 | — |
| H1 | Long-only 5/21 | 1943 | -96.26 | 271 | -16.49 | 0.00 |
| H2 | Long-only 20/50 | 680 | -37.11 | 90 | -4.70 | +11 795.63 |
| H3 | Momentum-gated 5/21 | 1912 | -99.52 | 269 | -17.80 | -1 308.87 |
| H4 | Trend-gated 5/21 | 232 | -12.68 | 13 | -0.60 | +15 894.35 |
| H5 | Donchian 20/10 | 1838 | -90.64 | 252 | -15.62 | +872.03 |

**Shortlist selection rule (pre-registered, design+validation only):** a
candidate enters the OOS single-read only if it was not worse than the champion
on validation and kept a stable (non-imploding) net result under bounded
perturbations. **H2 and H4 qualify** (H2 validation -4.70% with 90 trades; H4
-0.60% with 13 trades — the latter is statistically thin but was given its one
shot anyway). H1/H3/H5 were dropped: no meaningful change vs the champion on
design+validation, and no robustness (all still deeply negative).

**Protected OOS single-read (12,975 bars; buy & hold in OOS = -10.58%):**

| ID | Rule | Trades | Win % | Net % | Net P&L | MaxDD % | Friction ₹ |
|---|---|---|---|---|---|---|---|
| H0 | MA(5,21) both | 384 | — | -21.76 | -21 759.37 | 21.84 | — |
| H2 | Long-only 20/50 | 130 | 28.46 | -6.51 | -6 509.55 | 6.92 | 8 235.25 |
| H4 | Trend-gated 5/21 | 50 | 20.00 | -3.59 | -3 594.48 | 3.64 | 3 113.98 |

**Result:** every hypothesis is net-negative on design, validation **and** the
protected OOS. No hypothesis produced a positive, robust, cost-covering edge.

## 6. t-Stat / Statistical Significance

Per-trade P&L t-statistics (slippage-inclusive fills, **pre-commission**) on the
protected OOS — the fairest possible case for the strategies:

| Strategy | OOS trades | Mean per-trade ₹ | t-stat | Reading |
|---|---|---|---|---|
| Champion H0 | 384 | -42.07 | **-6.76** | significantly negative |
| H2 long-only 20/50 | 130 | -35.45 | **-2.05** | significantly negative (p < 0.05) |
| H4 trend-gated 5/21 | 50 | -57.52 | **-2.95** | significantly negative (p < 0.01) |

Full-window champion context (audit): gross bar-close edge +1.12/trade
(t = +0.66, indistinguishable from zero); slippage-adjusted realized
-42.09/trade (t = **-24.60**). **Conclusion: no measurable positive edge at
any friction level; the residual edge is significantly negative even before
commissions.**

## 7. Regime Analysis

Champion decision-time entry regimes (audit, full window) — every bucket negative:

| Regime @ entry | Trades | Win % | Net ₹ |
|---|---|---|---|
| down_high | 91 | 13.2 | -6 904 |
| down_low | 25 | 16.0 | -1 681 |
| down_normal | 38 | 18.4 | -1 446 |
| sideways_high | 447 | 15.4 | -26 101 |
| sideways_low | 972 | 9.8 | -55 768 |
| sideways_normal | 1 028 | 15.6 | -51 310 |
| up_* (any) | 0 | — | — |

- The champion **never entered long** across 4.7 years; all 2,601 entries were
  shorts realized into a market that rose +34% (5m). The entire short tail was
  counter-trend churn.
- H4 (trend-gated) cut entries from 2,601 to 232 (design) by requiring an MA-gap
  trend, but the regime where it does trade is still net-negative; on OOS its
  50 residual trades are significantly negative, so "filtering by regime" did not
  find a regime with a positive edge.
- WS 7.11's `RegimeFilteredMovingAverageCross` (BUY-gate only) is a structural
  no-op on this window: with zero long entries, a BUY-only gate changes nothing
  (documented in the audit; dean odds zero-long artifact).

## 8. Cost & Slippage Sensitivity

OOS cost scans (canonical params), net return % vs friction scenario:

| Scenario (× base commission/slippage) | H2 long-only 20/50 | H4 trend-gated 5/21 |
|---|---|---|
| 0× (zero cost) | +1.70 | -0.48 |
| 0.5× (low) | -2.40 | -2.04 |
| 1× (base) | -6.51 | -3.59 |
| 2× (high) | -14.74 | -6.71 |

- H2's only non-negative result is the impossible zero-friction gross (+1.70%
  over ~8.5 months); the first realistic friction step already makes it
  negative.
- H4 is negative even at zero cost.
- Champion whole-window: only zero-cost is positive (+2.92%); base/high deeply
  negative (from `docs/model_performance_report.md`).

Champion friction decomposition (audit): per-trade friction ₹56.18 =
slippage ₹43.22 + commission ₹12.96; edge/cost ratio ≈ 0.020 (i.e. friction ~50×
the gross edge). **Any strategy in this family must clear ~₹50/trade of friction
before it is even at breakeven; none did.**

## 9. Robustness / Perturbation Analysis

Bounded ±1-step perturbations around canonical params on the OOS segment
(single read, evaluation only):

| H2 variant | Net % | H4 variant | Net % |
|---|---|---|---|
| fast=15 | -6.89 | threshold=0.02 | -9.02 |
| fast=26 | -6.65 | threshold=0.10 | -0.49 |
| slow=40 | -9.65 | | |
| slow=60 | -5.56 | | |

Design+validation robustness (during selection): H2 stayed negative across all 4
variants (-35.0 .. -42.0 net %), H4 across both (-48.3, -1.7); canonical sign
(albeit negative) was stable. **No variant flipped non-negative on OOS.** The
"less bad" outcomes are frequency reductions — fewer trades, lower friction —
not a stable positive signal.

## 10. Live Paper Performance vs Simulated

- **No live trading ever occurred.** All paths are paper-only (`is_live=False`).
- The only current-data paper replay observation is the 10-Sep-2026 MA(5,21)
  paper session (~₹194.68 loss) — consistent in sign with the simulated result.
- Challengers H1–H5 were **never** run in the live/current-data paper session;
  their simulated results are the only evidence, and they are negative.

## 11. OOS Protocol & Integrity Statement

- Protected OOS ≥ 2026-01-01 (12,975 bars, 173 trading days) was **not** read
  during hypothesis registration, and was **not** used for selection.
- Selection was performed strictly on design + validation
  (`scripts/evaluate_candidates.py` prints only design/validation; the
  artifact also writes protected-OOS numbers but explicitly labels them
  withheld).
- The OOS was consumed exactly once, in `scripts/finalize_oos_confirmation.py`,
  for the two shortlist candidates (H2, H4). **No parameter was tuned against
  it. No candidate was added or removed as a result of seeing it.**
- No walk-forward or repeated-OOS sampling was run on OOS data.
- Any future use of 2026 data for *selection* would require a fresh, untouched
  period and would invalidate this statement.

## 12. Comparison vs Known Baselines

| Baseline | Period | Net/ gross return |
|---|---|---|
| NIFTY 50 1d buy-and-hold (gross) | full 2022–2026 | **+27.96%** |
| NIFTY 50 5m buy-and-hold (gross) | full 2022–2026 | +29.62% |
| Champion H0 (net) | full window | -143.21% |
| H2/H4 (net) | design+validation+OOS | all negative |
| NIFTY 50 5m buy-and-hold (gross) | protected OOS | **-10.58%** |
| H2 / H4 (net) | protected OOS | -6.51% / -3.59% |

The shortlist "beats" both the champion and (on OOS) buy-and-hold, but only by
losing less — with a statistically **negative** per-trade edge and a positive
result only at impossible zero friction. That is **not** an outperforming
strategy.

## 13. Learning-Loop Evidence Table

The WS 7.13 continuous feedback machinery (capture → experience store →
candidate hypotheses → comparison → gate → registry) exists and is covered by
tests, but this research consumed the pre-registered evidence directly through
the deterministic champion/challenger harness rather than the day-batched loop.
The evidence table the loop would have produced (from complete closed round
trips, decision-time regimes, no look-ahead):

| Bucket | Trades | Net ₹ | Win % | Learned fact used by hypotheses |
|---|---|---|---|---|
| Hold ≤ 5 bars | 648 | -57 004 | 0.0 | anti-trend churn (H2/H5) |
| Hold 6–10 | 451 | -42 860 | 0.0 | anti-trend churn (H2/H5) |
| Hold 11–20 | 741 | -58 876 | 0.7 | anti-trend churn (H2/H5) |
| Hold 21–50 | 664 | -391 | 37.3 | marginal |
| Hold 51+ | 97 | +15 921 | 96.9 | long holds win (H5) |
| **Total** | **2 601** | **net negative** | 15.53 | — |

Insufficient-evidence groups (e.g. c4's 13 validation trades, H5 channel
sensitivities) are reported and not treated as signal.

## 14. Champion / Challenger Status

| Entity | Status |
|---|---|
| MA(5,21) champion | **FROZEN and inert** — no promotion, no parameter change. It remains the V1 baseline and the reference for the gate. |
| H1/H3/H5 | **DROPPED** at selection (design+validation grounds). |
| H2, H4 | **REJECTED at the credible-edge gate**: OOS per-trade t = -2.05 / -2.95; both OOS nets negative; zero-friction gross ≈ noise or negative. |
| Promotion gate | Run once on the shortlist. Under default `PromotionCriteria` (`require_positive_oos_pnl=False`) the gate mechanically returns PROMOTE for both because the champion is catastrophically negative — **this is a criterion artifact, not an edge** (`PROMOTE` here means "loses less than a -21.8% champion"). Under the criteria a "credible edge" requires (positive OOS net P&L), both are **REJECT** with a single blocker: *"out-of-sample net P&L is not positive"*. |
| Version registry | Unchanged — no promotion was executed; MA(5,21) remains the only ACTIVE champion. |

## 15. Deployment / State of the System

- **Nothing is deployed or live.** All execution is paper (`PaperBroker` only),
  data is validated historical files, credentials never committed.
- Champion MA(5,21) remains the frozen V1 baseline (`services/paper_session.py`
  + registry). No challenger replaced it.
- The evaluation harness, candidate module, audit script, selection script and
  single-use OOS script are additive, deterministic, and covered by tests
  (**843 passed**).
- Artificial "promotion via default criteria" was deliberately **not** applied
  to a strategy whose OOS net P&L is negative — for the same reason the gate
  criteria default is a decision about raw P&L, not a license to claim alpha
  from a strongly negative champion baseline.

## 16. Recommendations

1. **Do not promote H2 or H4** (or any other MA-cross family variant) — no
   evidence of a credible, robust, reproducible net-of-cost edge exists on this
   window. Treat the mechanical PROMOTE under default gate criteria as a known
   benchmark artifact of a deeply negative champion.
2. **Stop further optimization of this family at 5-minute Nifty 50 frequency**
   as the stopping condition is satisfied (item 19). Additional parameter
   grids, smaller stops, or added filters would be re-fitting a family whose
   friction-adjusted per-trade edge is significantly negative.
3. If research is to continue at all, the evidence points elsewhere, not toward
   more crossover tweaks: longer-horizon (daily/weekly) trend filters, position
   sizing driven by volatility, and cost-aware frequency targets — each still
   subject to the same pre-registration + protected-OOS discipline.
4. Revisit the promotion gate's default `require_positive_oos_pnl` for use in
   this regime: promoting a challenger that merely bleeds less than a negative
   champion is misleading and should require positive OOS P&L for such
   baselines.
5. Maintain MA(5,21) as the frozen champion; no risk, sizing, stop-loss,
   broker, or portfolio change is justified by this research.

## 17. References & Credits

- Prior work streams this cycle builds on (all in-repo, see git history):
  WS 7.4/7.5 evaluation + five-year replay, WS 7.9 experience store, WS 7.10
  candidate generation, WS 7.11 champion/challenger, WS 7.12 promotion gate +
  registry, WS 7.13 learning loop, WS 7.14 alerting/watchdog.
- `docs/model_performance_report.md` — the champion negative-result deliverable
  this research explains.
- `docs/champion_failure_audit.md` — root-cause decomposition used for the
  hypothesis set.
- `PROJECT_PLAN.md` §17d/§17e/§17f — roadmap and evaluation discipline.
- Classic trend-following/failure-mode concepts (cross-over churn in
  range-bound markets, friction dominating gross edge) as referenced generically
  in item 1; no external URLs are cited because none were fetched.

## 18. Version & AI-Responsibility Disclosure

- Report version 1.0, generated 2026-09-12 by **opencode (big-pickle)** acting
  as autonomous engineering + research owner under the user's directive.
- All quantitative claims are reproducible from the committed scripts, encoded
  artifacts (git-ignored `reports/model_performance/*.json`) and 843-test suite.
- This report is a research assessment, not investment advice. No automated
  decision, order, or risk-state change was made by its production. Where a
  judgment call existed (default gate vs credible-edge gate), the case is shown
  with both numbers rather than hidden.
- Deviations/decisions recorded: finalize script conclusion logic changed from
  "default gate says PROMOTE ⇒ A" to "credible edge (positive OOS P&L) ⇒ A" so a
  negative-OOS result can never be reported as a positive outcome (item 14).

## 19. Conclusion & Stopping-Condition Determination

Applied stopping conditions from the research mandate:

- **A (credible robust OOS edge passes the existing promotion gate) — NOT met.**
  Both shortlisted challengers (H2, H4) have **negative** OOS net P&L
  (-6.51%, -3.59%), significantly negative per-trade OOS edges (t = -2.05,
  -2.95), negative results across all bounded perturbations, and no positive
  result above the impossible zero-friction line. The only "PROMOTE" verdicts
  arise under default gate criteria that do not require positive OOS P&L and
  therefore reward merely losing less than a -21.8% champion; under the
  credible-edge criteria both are REJECTED.
- **B (no credible edge; further optimization not scientifically justified) —
  MET.** Six registered rules (champion + 5 challengers), all pre-registered,
  all evaluated on identical data/costs with a protected single-use OOS period,
  and every one is net-negative on every segment. Per-trade edges on OOS are
  significantly negative across the family even before commissions. Continued
  parameter optimization of this family would be data-snooping against a family
  whose edge is negative, not merely underpowered.

**Determination: B. No promotion. MA(5,21) stays the frozen champion. The
candidate research cycle is closed with a clearly documented negative result.**

## 20. Evidence Artifacts Appendix

Reproducible inputs/scripts (committed):

- `src/fno_ai_paper_trading/strategies/research_candidates.py` — H1–H5 providers + wrappers + `CANDIDATES` registry (locked params, perturbations, rationale).
- `src/fno_ai_paper_trading/evaluation/candidates.py` — harness (evaluate/date-split/plan/cost-scan/robustness/gate-views).
- `scripts/audit_champion_failure.py` — champion decomposition.
- `scripts/evaluate_candidates.py` — design+validation selection (OOS withheld from output).
- `scripts/finalize_oos_confirmation.py` — the single protected-OOS read + gate + t-stats.
- `tests/test_research_candidates.py`, `tests/test_candidates_eval.py` — equivalence, no-look-ahead, determinism, harness (30 tests).

Output artifacts (git-ignored `reports/`, deterministic):

- `reports/model_performance/summary.json` + trades/equity CSV — champion continuous replay.
- `reports/model_performance/audit_champion_failure.json` — full-window decomposition (reconciliation True).
- `reports/model_performance/candidates_eval.json` — per-candidate design + validation + robustness + cost scans (protected OOS present but labelled withheld).
- `reports/model_performance/oos_confirmation.json` — buy&hold context, per-shortlist OOS summary, per-trade t-stats, OOS robustness/cost scans, both gate verdicts, conclusion B.

Raw numbers table (key facts, single source of truth):

| Metric | Champion H0 | H2 (20/50) | H4 (5/21, 0.05) |
|---|---|---|---|
| Full net % | -143.21 | (only segments shown) | (only segments shown) |
| Design net % | -104.12 | -37.11 | -12.68 |
| Validation net % | -16.49 | -4.70 | -0.60 |
| OOS net % (single read) | -21.76 | **-6.51** | **-3.59** |
| OOS per-trade t (pre-commission) | -6.76 | **-2.05** | **-2.95** |
| OOS zero-friction net % | (n/a, positive only there) | +1.70 | -0.48 |
| Credible-edge gate verdict | — | **REJECT** | **REJECT** |