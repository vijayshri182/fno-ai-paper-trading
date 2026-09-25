# Model Research Final Report — WS 7.18 Research Domain (2025-10-03)

Status: **STOPPING CONDITION B — no credible, robust, reproducible edge demonstrated.**

This report supersedes, within the current WS 7.18 framework, the earlier
WS 7.16 report (`docs/model_research_final_report.md`), which used a different
evaluation protocol (continuous single-session replay, selection boundary
2026-01-01). Both reach the same conclusion; the numbers below are the
canonical WS 7.18 daily-reset observation set for the research domain.

Every number in this report is produced deterministically from
`scripts/research_final_assessment.py` and persisted to
`reports/research/research_final_assessment.json`. The walk ledger, summary,
versions and state are **read-only** here; nothing was rewritten.

---

## A. Objective & Scope

Determine, from research-domain evidence only, the strongest evidence-backed
NIFTY F&O trading algorithm among the frozen champion `model_0`
(MA cross 5/21) and the frozen candidate set c1–c5, under the WS 7.18
daily-reset walk semantics with explicit promotion-gate criteria.

Scope exclusions (NOT AVAILABLE in this repo): option/expiry/IV data, alternate
data providers, live execution, anything after the protected OOS boundary.

## B. Data & Boundaries

- Single canonical dataset:
  `datasets/upstox_Nifty_50_5m_20220103_20260911.csv` (87,193 bars, 5m OHLCV,
  NIFTY 50 INDEX; volume/OI zero; lot 1, multiplier 1, tick 0.05).
  `data_hash` prefix `6c400b016c1c…` (hash-validated on load).
- Research domain: **2022-01-03 … 2025-10-03 = 932 trading days**.
- Protected out-of-sample boundary: **2025-10-06** (walk config
  `protected_oos_start`, persisted in `reports/walkforward/summary.json`,
  config_hash `ae118ee…`). Days ≥ 2025-10-06 are **never loaded or processed**
  — this assessment asserts no OOS day enters the domain.
- Historical deviation record: the earlier WS 7.16 protocol used boundary
  2026-01-01 and read its OOS segment (2026-01-01 … 2026-09-11) once for c2/c4
  (`reports/model_performance/oos_confirmation.json`, conclusion B). That
  segment overlaps the WS 7.18 protected OOS; it is therefore **consumed
  historically and NOT used again**. This report makes no additional OOS read.

## C. Environment & Costs (reproducibility)

- Engine: WS 7.18 `WalkForwardEngine`, walkforward-1.0; champion strategy
  `moving_average_cross fast=5 slow=21` via `MovingAverageCrossStrategy`.
- Daily-reset semantics: every day a fresh `BacktestEngine`/portfolio at
  ₹100,000; day P&L = day equity delta; champion day P&L sums to the champion
  totals. Strategy sees only `bars[:i+1]` (no look-ahead).
- Costs: commission 0.03%/side, slippage 0.10%/side, fixed 0, stop-loss 2%,
  risk manager on (max qty 75, max notional 250,000, max daily loss 10,000),
  quantity 1.
- Regime detector: MA-gap trend (±0.05) + volatility ratio (0.7/1.5);
  labels e.g. `sideways_normal`; no look-ahead.
- Champion totals reproduced **bit-for-bit** (`reports/research/…json`,
  `champion_control.reproduced = true`): net −80,269.643793365; costs
  82,616.243793365; slippage 63,550.91015; 1293 RT; W/L 63/1230;
  maxDD/day 0.53962935253500%; exposure-unit 28,834.77 → this validates the
  whole harness (provider signals ≡ `StrategyEngine.evaluate`, asserted by
  existing tests).

## D. Baseline champion (`model_0`) — results & failure analysis

Champion totals (canonical, 932 research days, daily-reset):

| Metric | Value |
|---|---|
| Net P&L | **−80,269.64** (−80.27% on ₹100k) |
| Costs | 82,616.24 (slippage 63,550.91 + commission 19,065.33) |
| Gross price P&L (net + costs) | **+2,346.60 ≈ +0.03%/yr → no gross edge** |
| Round trips | 1293 (1.39/day) |
| Win rate | 63 W / 1230 L = **4.87%** |
| Max drawdown/day | 0.5396% |
| Buy-and-hold reference (day-close, qty 1, gross) | +7,259.65 (+7.26%) |

Trade-level (1293 completed round trips; realized incl. slippage on closing fills):

- Realized sum −69,997.18; **mean −54.14/trade, t = −59.1** → significantly
  negative, not a sampling artifact. Net after closed-trade commissions
  −86,172.05.
- Sides symmetric: CALL 660 RT (5.15% WR) −44,248; PUT 633 RT (4.58% WR)
  −41,924. A short-only pathology is NOT present in the daily-reset method
  (unlike the prior full-dataset continuous audit, which saw 0/2601 long
  entries).
- Years all negative and deteriorating: 2022 −19,750 (8.3% WR); 2023 −17,839;
  2024 −27,582; 2025(partial) −21,002 (3.0% WR).
- Time of day: 62% of round trips open 11:00–13:00 (7.1% WR); 36% open
  13:00–15:00 with **1.3% WR (6 wins/461)** — afternoon entries were toxic.
- Entry regime: 97.1% of round trips open in sideways regimes
  (`sideways_normal` 819, `sideways_low` 369, `sideways_high` 68). **No regime
  bucket is profitable.**
- Holding duration: 1–5 bars 420 RT (0 wins, mean −63.0), 6–10 bars 279 RT
  (0 wins, −70.8), 11–20 bars 371 RT (1.1% WR), 21–50 bars 223 RT (26.5% WR,
  mean −14.0, t −5.2). Holding longer is "least bad" but still negative; no
  overnight/holding ≥ 51 bars possible in daily-reset intraday method.
- 2%-stop-like exits: 0 / 1293 (stop essentially never triggered at qty 1 on
  the index intraday); per-trade slippage NOT PERSISTED (day-level slippage
  only).

Interpretation: a price-crossover machine with ~zero gross edge and full
cost-loading. It loses to both costs and even to the foulest-looking buckets;
cost scan (see G) confirms gross breakeven.

## E. Candidate hypotheses (frozen; parameters locked before this run)

Families registered in `strategies/research_candidates.py` (CANDIDATES) and
evaluated identically:

| Candidate | Family | Logic (decision-time only) | Params |
|---|---|---|---|
| c1 | LONG_ONLY_TREND | MA(5,21) up-cross opens long, down-cross closes; never short | fast=5 slow=21 |
| c2 | LONG_ONLY_TREND_SLOW | 2.5× slower legs, long-only, to cut flip frequency | fast=20 slow=50 |
| c3 | MOMENTUM_CONFIRMATION | MA(5,21) both sides; entries only with prior 5-bar momentum sign | momentum_bars=5 |
| c4 | REGIME_AWARE_TREND_GATE | MA(5,21) both sides; longs only if MA-gap > +0.05, shorts only if < −0.05 | trend_threshold_pct=0.05 |
| c5 | VOLATILITY_BREAKOUT | Donchian 20-bar entry / 10-bar exit, both sides (fewer, longer holds) | entry=20 exit=10 |

Regime-selector family (from the walk itself): `suppress_buys_not_up` /
`suppress_sells_not_down` variants were validated future-only in 20-day
windows and closed **INSUFFICIENT_EVIDENCE** (0–1 validation trades < 10):
regime filtering suppresses activity to near-zero, i.e., economically dead.

Families deliberately NOT added this cycle: mean reversion (audit: all sub-20
bar holds lose; contradicts), options premium/IV (data NOT AVAILABLE).

## F. Evaluation method (future-only, single-pass, no tuning)

- Every strategy replayed over the same 932-day research domain with the walk's
  daily-reset semantics; candidate parameters are frozen constants (never
  tuned on any result of this assessment).
- Champion control must reproduce the persisted ledger totals exactly (it
  does — gate; section C).
- Promotion decision = WS 7.18 `WalkForwardGate` over the whole research
  domain (adaptation of the gate, which officially validates on 20-day
  future-only windows; whole-domain verdict stated explicitly as an
  adaptation).
- Robustness grid and cost scans use the same domain only. Protected OOS was
  never opened. This ran **once**; the report is a single deterministic pass.

## G. Results

Candidates (net-of-cost, 932 research days, daily-reset):

| Candidate | Net P&L | RT | W/L (WR%) | Costs | MaxDD% | PF | Gate |
|---|---|---|---|---|---|---|---|
| c1 long_only_ma_cross | −67,950.75 | 1081 | 48/1033 (4.44) | 70,781.30 | 0.5396 | 0.031 | REJECT |
| c2 slow_long_only | **−8,521.61** | 41 | 0/41 (0) | 7,835.21 | 0.2733 | 0 | REJECT |
| c3 momentum_gated | −78,173.23 | 1267 | 63/1204 (4.97) | 81,098.03 | 0.5396 | 0.032 | REJECT |
| c4 trend_gated | **−3,724.49** | 53 | 6/47 (11.3) | 3,755.04 | 0.5396 | 0.119 | REJECT |
| c5 donchian_breakout | −71,860.55 | 1092 | 70/1022 (6.41) | 76,472.80 | 0.5396 | 0.045 | REJECT |

Rejection reasons (all five): net P&L not positive; PF < 1.0; consecutive
losing days > 8 (c2: 80, c4: 226); net P&L < costs; regime
degradation (c1, c3, c4, c5). c4 (best net) still loses ₹3.7k with only 53 RT
and gross edge of just **+30.55** at zero cost (vanishing).

Best-by-net order: c4 (−3.7k) < c2 (−8.5k) < c1 (−68.0k) < c5 (−71.9k) < c3
(−78.2k) compared with champion −80.3k. The two best candidates barely differ
from "do nothing except pay no costs".

Benchmark: champion −80.3k vs buy-and-hold gross +7,259.65 — the champion is
77× worse than simply holding the day-close index series.

## H. Robustness (research domain, frozen perturbations)

20 single-parameter perturbations across champion + c1–c5 (grid per CANDIDATES).
**0 of 20 are net-positive.** Best variant: c4@threshold 0.10 → −338.32 with
only 6 RT (below min evidence AND negative). Champion sensitivity −98.2k
(fast=3) … −62.1k (slow=26): uniformly negative.

Cost scan (champion, c2, c4):

| Scenario | commission/slippage | model_0 net | c2 net | c4 net |
|---|---|---|---|---|
| zero | 0 / 0 | +2,346.60 | −686.40 | **+30.55** |
| low | 0.01% / 0.02% | −16,718.68 | −2,494.31 | −836.00 |
| base | 0.03% / 0.10% | −80,269.64 | −8,521.61 | −3,724.49 |
| high | 0.05% / 0.20% | −156,530.88 | −15,755.94 | −7,190.65 |

The champion's entire P&L is cost-dominated; its gross edge (+2,347 over 4
years) is indistinguishable from zero. c4's zero-cost edge (+30.55) is noise.

## I. Decision & Status

- **Decision: NO_PROMOTION.** No candidate cleared the gate; 0/5 positive net
  after costs; 0/20 robustness variants positive. Champion remains
  `model_0` (MA 5/21) by default, not by merit.
- Promotion history of the walk itself: 5 regime-selector challengers, all
  INSUFFICIENT_EVIDENCE; 0 promotions ever recorded; `model_0` remains the
  only version (ACTIVE).
- Status: **Algorithm Health RED — ALGO READY: NO — live trading: false —
  research-only: true**. The walk summary.json already reflects
  `live_trading:false`, `research_only:true`, `protected_oos_start:2025-10-06`;
  untouched.

## J. Assumptions & Limitations

- Instrument is the NIFTY 50 INDEX, not a traded F&O contract; no expiry/roll,
  no OI, no IV, no options surface (expiry-based analysis NOT AVAILABLE).
- quantity=1 throughout; fixed commission/slippage model; execution is a
  deterministic paper broker (bar-timestamp fills; no intra-bar fills/skip).
- Daily-reset intraday semantics: positions never carried overnight; holding
  ≥ 51 bars is impossible within a day (audit's profitable multi-day holds do
  not exist in this method).
- Regime labels are entry-time; exit may cross regimes (not modeled).
- Results are single-terminal-config; no rolling refit was performed (frozen
  parameters, single pass).

## K. Reproducibility

- Command: `.venv\Scripts\python.exe scripts\research_final_assessment.py`
  (from repo root). Read-only: writes only `reports/research/…json`.
- Artifacts read: `reports/walkforward/summary.json` (control targets),
  `datasets/upstox_Nifty_50_5m_20220103_20260911.csv` + `.meta.json`
  (hash-validated).
- Artifact written: `reports/research/research_final_assessment.json`
  (machine-readable: champion_control, failure_analysis, candidates,
  robustness_grid, cost_scan, decision).
- Verification: full suite `pytest` → **1110 passed** (59s), incl.
  strategy/provider equivalence and walk gate tests.
- History: walk ledger/summary/versions/state NOT modified.

## L. Conclusion & Next Required Test

Conclusion: After 932 research days of deterministic, future-only, net-of-cost
evaluation, neither the champion MA(5,21) nor any of five frozen candidate
families shows a credible, robust, reproducible edge. The champion is a
cost-dominated churn machine with zero gross edge; the best candidate (c4) has
no measurable edge before costs either. This is stopping-condition B: **do not
promote; do not trade.**

Next required test (when data becomes available): evaluate with true NIFTY 50
**F&O near-month contract** data (expiry, roll, OI), which changes both the
instrument profile and cost schedule; until such data exists, any further
intraday indicator tuning on the index series is expected to reproduce the same
no-edge result and is not economically justified.

---

## Final summary

- **BEST CURRENT ALGORITHM**: `model_0` — Moving Average Crossover, fast=5,
  slow=21 (frozen WS 7.18 champion; no promoted algorithm exists).
- **LOGIC**: BUY on fast-MA up-cross of slow-MA, SELL on down-cross, qty 1,
  5m bars, regime detector (MA-gap trend + volatility) for daily labeling.
- **WHY**: it is the only validated algorithm; it was NOT chosen on merit —
  all five frozen candidates and all walk challengers failed the promotion
  gate.
- **NET PERFORMANCE (research domain 2022-01-03 → 2025-10-03, daily-reset)**:
  −80,269.64 after costs 82,616.24 (slippage 63,550.91 + commission 19,065.33);
  1293 RT; 4.87% win rate; gross price edge +2,346.60 ≈ 0; buy-and-hold
  reference +7,259.65 (gross).
- **ROBUSTNESS**: 0/5 candidates net-positive; 0/20 perturbation variants
  net-positive; cost scan flat-to-worse at every cost level; every year and
  every regime bucket negative; t-stat of per-trade realized −59.
- **STATUS**: RED — ALGO READY: **NO** — health RED — no promotion history,
  only `model_0` ACTIVE (research was completed with no algorithm change).
- **LIVE TRADING**: OFF (scope.live_trading=false, research_only=true,
  paper/historical only).
- **REMAINING RISKS**: (1) data/instrument mismatch — index series, no
  F&O contract/expiry/OI/IV; (2) cost-model sensitivity — slippage dominates
  and is per-trade NOT PERSISTED; (3) single dataset, no second source;
  (4) daily-reset method cannot capture multi-day holds.
- **NEXT REQUIRED TEST**: repeat this exact protocol on true NIFTY 50 F&O
  near-month contract data (expiry + roll + OI) when available; nothing else
  is economically justified.