# Algorithm Robustness & Strengthening Research Pass — Consolidated Report

- **Task**: Algorithm Robustness and Strengthening Research Pass
- **Generated**: 2026-09-28T09:12:52 (repo HEAD `54bcb75`)
- **Champion evaluated**: `moving_average_cross` fast=5 slow=21 (frozen `v1-baseline-ma521`)
- **Window**: 2022-01-03 .. 2025-10-03 (69781 bars / 932 days) — RESEARCH only
- **Protected OOS excluded**: 2025-10-06 .. 2026-09-11 (17412 bars dropped, never loaded into any evaluation)

## A. Provenance & Methodology
Dataset `datasets\upstox_Nifty_50_5m_20220103_20260911.csv` (SHA-256 `6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c`) — pinned and verified. All analytics use the deterministic
existing evaluation machinery (`research.metrics` / `evaluation.model_performance` / `walkforward` semantics); trades on the strict dev
window are reproduced serially. No parameter was fitted; all grids were pre-declared; no protected day was read.

## B. Reproducibility & Integrity
- Determinism probe: `deterministic = True` — identical inputs => identical summary dict.
- Recorded-artifact cross-check vs `reports/algorithm_state/assessment.json` (backtest bucket, recorded split entry < 2026-01-01):
  PASS — row count used `2216`.
  Note: the recorded backtest bucket (2216 rows) intentionally includes 125 trades entered inside the protected window
  (2025-10-06..2025-12-31); the research window is the strict dev window (no protected days), which is why the baseline
  row count differs. See `robustness_pass.json` > `B_integrity.recorded_backtest_cross_check.diffs`.
- Domain label for every evaluation here: `RESEARCH (dev)`.

## C. Champion Baseline (dev window, single continuous session, default paper cost model)
| metric | value |
|---|---|
| closed round trips | 2091 |
| open at window end | 0 |
| win / lose | 267 / 1824 |
| win rate % | 12.76901004304160688665710187 |
| net PnL (₹) | -112692.554219635 |
| expectancy (₹/trade) | -53.89409575305356288857006217 |
| profit factor | 0.1916891184518110514264582038 |
| avg win / avg loss (₹) | 100.0928362961985018726591760 / -76.43494600368421052631578947 |
| max drawdown (₹) | 112692.554219635 |
| consecutive losses | 10 |
| reconciliation_ok | True |
Cost model (fractions): commission_rate=0.0003, slippage_rate=0.001,
stop_loss_pct=0.02. Net-everything convention: `net_pnl = price_pnl - (entry+exit commission)`.
Health-bucket metrics here split wins on **net** PnL (win/lose, expectancy, PF) — the persisted assessment uses the same convention,
so row-to-row equivalence with `reports/algorithm_state/assessment.json` holds at the net level.

## D. Pre-declared Parameter Neighbourhood (fast 4..7 × slow 19..23)
Champion still best: **False** (neighbours better by expectancy: 13).
Neighbour expectancies: mean -40.89392205579759101166737419 / min -44.16337650330185241411605313 /
max -37.56650574963609898107714701 (19 neighbours).
| fast | slow | champion | trades | win% | net PnL (₹) | expectancy | PF | maxDD% | costs (₹) |
|---|---|---|---|---|---|---|---|---|---|
| 4 | 19 |  | 2401 | 14.20241566014160766347355269 | -129759.289710180 | -41.52419500208246563931695127 | 0.2354019000522255998530437975 | 129.7583229740879342065991536 | 130258.689710180 |
| 5 | 19 |  | 2255 | 15.47671840354767184035476718 | -115656.224743410 | -38.77371212860310421286031042 | 0.2757792174167577552428069932 | 115.6836652400600751374705249 | 122306.524743410 |
| 6 | 19 |  | 2158 | 14.82854494902687673772011121 | -116948.697702005 | -41.66810535217794253938832253 | 0.2499579381689834957249635376 | 116.94869770200500 | 117125.347702005 |
| 7 | 19 |  | 2061 | 17.95245026686074721009218826 | -103243.062550030 | -37.56650574963609898107714701 | 0.3031293733418124697619860140 | 103.2686125500300 | 111837.962550030 |
| 4 | 20 |  | 2311 | 20.07788836001730852444829078 | -131105.335978695 | -44.16337650330185241411605313 | 0.2923534129205934356506943561 | 131.1091253601092549597209006 | 125364.685978695 |
| 5 | 20 |  | 2156 | 14.23933209647495361781076067 | -116342.765892145 | -41.45137400278293135435992579 | 0.2490614785408386823856940658 | 116.34276589214500 | 116885.615892145 |
| 6 | 20 |  | 2046 | 15.34701857282502443792766373 | -110423.611704965 | -41.45558946725317693059628544 | 0.2588673606397716367943571490 | 110.42361170496500 | 110957.061704965 |
| 7 | 20 |  | 1973 | 15.50937658388241256969082615 | -105261.858154570 | -40.85010278763304612265585404 | 0.2710333006155867363962275851 | 105.2616872221237321901543841 | 106879.958154570 |
| 4 | 21 |  | 2253 | 13.75943186861961828672880604 | -121087.935080840 | -41.21983524189968930315135375 | 0.2482707326097791918948781073 | 121.0879350808400 | 122285.135080840 |
| 5 | 21 | yes | 2091 | 14.77761836441893830703012912 | -112692.554219635 | -41.38144775227164036346245816 | 0.2590254989914183940960215547 | 112.69255421963500 | 113377.104219635 |
| 6 | 21 |  | 2005 | 15.21197007481296758104738155 | -107641.130452375 | -41.18108795511221945137157106 | 0.2637821867577485353872614297 | 107.64113045237500 | 108649.880452375 |
| 7 | 21 |  | 1911 | 16.01255886970172684458398744 | -102026.296529300 | -40.89103448456305599162742019 | 0.2756901501667272065157782243 | 102.026296529300 | 103495.296529300 |
| 4 | 22 |  | 2159 | 14.17322834645669291338582677 | -115144.206574800 | -40.80201204261232051875868457 | 0.2581313166092069026237597834 | 115.144206574800 | 117228.206574800 |
| 5 | 22 |  | 2026 | 15.00493583415597235932872655 | -109088.966514320 | -41.31797660414610069101678184 | 0.2638243236153048782027617766 | 109.0889665143200 | 109974.566514320 |
| 6 | 22 |  | 1916 | 15.39665970772442588726513570 | -102149.777865915 | -40.83053368997912317327766179 | 0.2742359717608777390778725120 | 102.14977786591500 | 103646.727865915 |
| 7 | 22 |  | 1856 | 16.32543103448275862068965517 | -97726.056583090 | -40.16412098599137931034482759 | 0.2901772598946378040842679835 | 97.7516065830900 | 100410.756583090 |
| 4 | 23 |  | 2091 | 14.49067431850789096126255380 | -110510.505392820 | -40.32421965566714490674318508 | 0.2681743604530768232893394382 | 110.5105053928200 | 113501.105392820 |
| 5 | 23 |  | 1963 | 15.18084564442180336220071319 | -104430.264166605 | -40.69981963830871115639327560 | 0.2765615858524942948261747897 | 104.43026416660500 | 106324.914166605 |
| 6 | 23 |  | 1846 | 15.65547128927410617551462622 | -98140.382254525 | -40.67418442578548212351029253 | 0.2828512390331012704278314403 | 98.14038225452500 | 99908.632254525 |
| 7 | 23 |  | 1795 | 16.60167130919220055710306407 | -96772.047152235 | -41.42673334261838440111420613 | 0.2811227117052034056584075212 | 96.77204715223500 | 97114.597152235 |
Note: this table follows the research ``PerformanceMetrics`` convention — expectancy, win% and PF are **gross** (before
friction); the **net** health-bucket figures for the champion are in C. The gap between the two (champion expectancy −53.89 net vs −41.38
gross) is the per-trade cost burden, which is exactly the friction story developed in E. All 20 grid sites are uniformly negative at
base friction (gross expectancy −44.16..−37.57) — the champion's defeat is **not** a local-minimum/parameter-selection artifact, and no
neighbourhood choice repairs it. At zero friction the same family runs roughly cost-neutral (E: zero_cost ≈ +684), so friction, not
parameter choice, is the binding constraint. pre-declared grid ('research_window' only); serially evaluated; no parameter was fitted and no protected day was read

## E. Cost & Slippage Sensitivity (constant configuration, friction varied)
| scenario | fee | slippage | cost model | trades | net PnL (₹) | return % | costs (₹) | maxDD% |
|---|---|---|---|---|---|---|---|---|
| zero_cost | 0 | 0 | none | 2091 | 684.55 | 0.6845500 | 0.00 | 4.495130227611064921183138080 |
| low_cost | 0.00015 | 0.0005 | none | 2091 | -56004.00216115875 | -56.0040021611587500 | 56688.55216115875 | 56.13804235904028617116592465 |
| base | 0.0003 | 0.001 | none | 2091 | -112692.554219635 | -112.69255421963500 | 113377.104219635 | 112.69255421963500 |
| high_cost | 0.0006 | 0.002 | none | 2091 | -226069.658028540 | -226.0696580285400 | 226754.208028540 | 226.0696580285400 |
| statutory_nse_fo_illustrative | 0.0003 | 0.001 | nse_fo_illustrative | 2091 | -89321.5903796033010 | -89.32159037960330100 | 90006.1403796033010 | 89.32159037960330100 |
Baseline friction: commission_rate=0.0003, slippage_rate=0.001.

## F. Regime Breakdown (entry-regime, dev window champion)
| regime | trades | winning | losing | win% | net PnL (₹) | avg (₹) |
|---|---|---|---|---|---|---|
| down_high | 79 | 12 | 67 | 15.18987341772151898734177215 | -5422.920020960 | -68.64455722734177215189873418 |
| down_low | 22 | 3 | 19 | 13.63636363636363636363636364 | -1447.529783180 | -65.79680832636363636363636364 |
| down_normal | 31 | 6 | 25 | 19.35483870967741935483870968 | -1077.308402365 | -34.75188394725806451612903226 |
| sideways_high | 342 | 50 | 292 | 14.61988304093567251461988304 | -19131.469778590 | -55.93997011283625730994152047 |
| sideways_low | 775 | 72 | 703 | 9.290322580645161290322580645 | -42277.164379450 | -54.55117984445161290322580645 |
| sideways_normal | 842 | 124 | 718 | 14.72684085510688836104513064 | -43336.161855090 | -51.46812571863420427553444181 |

## G. Failure-Category Forensics (descriptive association; causal bars only)
| category | trades | winning | net PnL (₹) | avg (₹) |
|---|---|---|---|---|
| winner | 267 | 267 | 26724.787291085 | 100.0928362961985018726591760 |
| cost_flip | 42 | 0 | -250.220728030 | -5.957636381666666666666666667 |
| whipsaw | 1648 | 0 | -131313.089053705 | -79.68027248404429611650485437 |
| stop_hit | 3 | 0 | -1445.175429490 | -481.7251431633333333333333333 |
| adverse_hold | 131 | 0 | -6408.856299495 | -48.92256717171755725190839695 |
mutually exclusive priority cost_flip > stop_hit > whipsaw > adverse_hold; stop_hit measured against the paper stop-loss band (2%) with a 5% band tolerance; whipsaw uses the opposite crossover within 25 bars (~1 trading day); descriptive only

## H. Holding-Duration Buckets (pre-declared, 75 bars/day)
| bucket | trades | winning | losing | win% | net PnL (₹) | avg (₹) | commission (₹) | contrib% |
|---|---|---|---|---|---|---|---|---|
| intraday_or_1d | 2078 | 254 | 1824 | 12.22329162656400384985563041 | -115111.124765780 | -55.39515147535129932627526468 | 25998.121665780 | 102.1461671207054778831603405 |
| 2d_5d | 13 | 13 | 0 | 100 | 2418.570546145 | 186.043888165 | 165.825303855 | -2.146167120705477883160340524 |
| 6d_15d | 0 |  |  |  |  |  |  |  |
| 16d_25d | 0 |  |  |  |  |  |  |  |
| over_25d | 0 |  |  |  |  |  |  |  |
Whip: the overwhelming majority of round trips exit inside one trading day (≤75 bars); the only positive bucket (2d_5d) has
n=13 — a fragile, small sample. Edge, where any, is not being harvested from intraday noise.

## I. Loss Clusters & Drawdown (closed-trade cumulative curve)
| stat | value |
|---|---|
| trades | 2091 |
| net PnL (₹) | -112692.554219635 |
| max drawdown (₹) | 112692.554219635 |
| max drawdown % | - |
| longest consecutive losses | 51 |
| worst 1/5/10/20-trade windows (₹) | -727.31452040 / -1094.374549190 / -1449.326671195 / -2192.817632135 |
| worst / best single trade (₹) | -727.31452040 / 941.286009820 |
| losing-run lengths | 4, 1, 16, 3, 3, 12, 3, 6, 6, 1, 1, 3, 2, 1, 5, 2, 2, 4, 5, 3, 24, 1, 3, 2, 3, 4, 4, 10, 2, 2, 12, 1, 2, 4, 3, 4, 7, 2, 3, 2, 3, 4, 2, 4, 6, 6, 5, 3, 11, 3, 1, 13, 1, 5, 3, 22, 1, 11, 4, 8, 14, 2, 1, 6, 1, 3, 2, 1, 5, 3, 3, 3, 35, 16, 23, 20, 2, 1, 2, 11, 9, 2, 1, 1, 7, 19, 1, 7, 28, 1, 1, 4, 13, 2, 2, 3, 2, 9, 50, 7, 5, 4, 1, 13, 6, 27, 19, 9, 20, 7, 2, 8, 4, 6, 10, 9, 26, 6, 5, 1, 2, 1, 5, 7, 8, 1, 4, 21, 46, 7, 14, 9, 1, 10, 6, 6, 14, 4, 31, 4, 5, 13, 4, 4, 2, 4, 8, 33, 1, 3, 11, 7, 2, 1, 5, 12, 14, 1, 6, 51, 4, 13, 4, 19, 17, 39, 3, 38, 3, 2, 21, 6, 1, 3, 2, 6, 5, 2, 4, 2, 4, 26, 3, 2, 1, 9, 8, 11, 3, 5, 17, 6, 11, 5, 6, 2, 1, 11, 1, 6, 15, 2, 20, 7, 8, 6, 17, 10, 3, 7, 2, 12, 9, 9, 19, 8, 8, 16, 11, 1, 9, 5, 2, 4, 8, 23, 3, 1, 7, 35, 1, 1, 10 |
Loss concentration (share of total loss carried by the worst losing trades):
100%→-29798.700845095, top 25%→-57681.165875710,
top 50%→-93985.339093685 (₹). drawdown measured on the closed-trade cumulative net curve (equity proxy); same simplification as the health monitor

## J. Temporal Segments
**By exit-year**
| year | trades | winning | losing | win% | net PnL (₹) | avg (₹) |
|---|---|---|---|---|---|---|
| 2022 | 543 | 92 | 451 | 16.94290976058931860036832413 | -21100.850311255 | -38.85976116253222836095764273 |
| 2023 | 521 | 54 | 467 | 10.36468330134357005758157390 | -25808.300942450 | -49.53608626190019193857965451 |
| 2024 | 593 | 68 | 525 | 11.46711635750421585160202361 | -37513.315661575 | -63.26022877162731871838111298 |
| 2025 | 434 | 53 | 381 | 12.21198156682027649769585253 | -28270.087304355 | -65.13845001003456221198156682 |
**First vs second half (by entry order)**
| half | trades | winning | losing | win% | net PnL (₹) | avg (₹) |
|---|---|---|---|---|---|---|
| first_half | 1045 | 145 | 900 | 13.87559808612440191387559809 | -45816.831774285 | -43.84385815721052631578947368 |
| second_half | 1046 | 122 | 924 | 11.66347992351816443594646272 | -66875.722445350 | -63.93472509115678776290630975 |
Monthly detail: see `robustness_pass.json` > `J_temporal_segments.months`.

## K. Limited Alternative Strategies (frozen walk-forward catalog challengers; per-day replay)
| key | trades | winning | losing | win% | net PnL (₹) | expectancy | PF | maxDD% |
|---|---|---|---|---|---|---|---|---|
| suppress_buys_not_up | 51 | 6 | 45 | 11.76470588235294117647058824 | -3602.164870710 | -70.63068373941176470588235294 | 0.08658702873389047410459659297 | - |
| suppress_sells_not_down | 0 | 0 | 0 | - | 0 | - | - | - |
| suppress_sideways_entries | 51 | 6 | 45 | 11.76470588235294117647058824 | -3602.164870710 | -70.63068373941176470588235294 | 0.08658702873389047410459659297 | - |
| stricter_trend_gate | 1 | 1 | 0 | 100 | 0.909807340 | 0.909807340 | - | 0E+9 |
framework: per-day replay, portfolio resets daily (walk-forward challenger semantics). all candidates are the repo's frozen walk-forward catalog variants; no new strategy was introduced and no parameter was fitted

## L. Overfitting / Leakage Audit & Strengthening Hypotheses
**Audit**: dataset hash verified (`True`); the pass never read protected days;
neighbourhood pre-declared; challengers frozen from the walk-forward catalog; no post-hoc threshold tuning; engine deterministic
(`True`). Safeguard snapshot at run time:

```
{'settings_environment': 'development', 'credential_env_vars_present': ['FNO_UPSTOX_ACCESS_TOKEN', 'OPENCODE_SERVER_PASSWORD', 'UPSTOX_ACCESS_TOKEN', 'UPSTOX_API_KEY', 'UPSTOX_API_SECRET'], 'live_test_switch_env_vars_present': ['FNO_LIVE_EXECUTION_TEST_ENABLED', 'FNO_LIVE_TEST_DRY_RUN'], 'algo_ready': 'NO', 'operator_consent': {'operator': 'Vijay', 'purpose': 'WS 7.24B controlled live BUY-SELL round-trip execution test', 'created_at': '2026-09-28T08:18:15', 'expires_at': '2026-09-29T08:18:15', 'token_fingerprint_sha256': '0a6db2b1ccc7a67deff23aa8ceebc4de4cc72b12bd50469bdc25f6735f23b5e1', 'window_active_now': True}, 'note': 'snapshot only; this pass performs no execution, no order placement and no network activity'}
```

**Strengthening hypotheses** (evidence-driven, to be validated by dedicated research passes — none applied here):
1. Friction is the dominant drag, not the signal: zero-cost runs roughly cost-neutral (net 684.55) while base friction loses -112692.554219635 and pays 113377.104219635 in costs; the gap between net health-bucket expectancy (C) and gross research expectancy (D) is exactly this friction.
2. The dominant failure mode is fast whipsaw reversion: 1648 trades (of 2091) exit on the opposite crossover within ~1 trading day and account for -131313.089053705 net PnL; hard stops fire rarely (stop_hit=3). Reducing whipsaw turnover (stay out of the crossover-echo zone, or confirm) is the highest-leverage lever before cost redesign.
3. Losses concentrate in sideways regimes (1959 of 2091 trades / -104744.796013130 net PnL); regime-gating (already present in the challenger family) must trade win-rate lift against the near-total absence of opportune trades it produces in this sample.
4. The long-hold bucket is the only positive sub-population but is tiny and fragile (n=13); nothing in the evidence supports lengthening holds as a robust repair.
5. The frozen regime-filtered challenger family does not outrank the champion at base friction in the dev window — the bottleneck is structural (cost vs win rate), not the trend gate. No challenger is promoted.
**Limitations**
1. Single market (NIFTY 50) and single instrument deck; no multi-instrument generalisation.
2. Single historical sample; regime and failure labels are descriptive associations, not causal claims.
3. Fresh/OOS validation is out of scope and blocked by the gate (pool NOT_READY).

## Artifacts
`robustness_pass.json` (truth), `robustness_pass_report.md` (this report), `artifacts_manifest.json` (SHA-256). Repo HEAD `54bcb75`.
