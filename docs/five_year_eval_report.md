# Five-Year Historical Evaluation — Run & Report (WS 7.5 / §17g)

Run date: **2026-09-13**. This is the FIRST completed five-year replay over the
real intraday dataset: every available trading day of the NIFTY 50 5m series was
validated and deterministically replayed through the **frozen MA(5,21)
baseline**. It does not claim more than was processed.

## 1. Scope and data

* Available 5m history actually starts **2022-01-03** (Upstox returns no 5m
  bars for earlier windows; a 2021-09-13..2021-12-31 acquisition attempt returned
  *empty* for every window). The run therefore covers **~4.7 calendar years**,
  which is the honest maximum, not a full five calendar years.
* Source: `datasets/upstox_Nifty_50_5m_20220103_20260911.csv` (canonical CSV +
  `.meta.json` with SHA-256 `data_hash`), day-chunked into
  `datasets/five_year_5m/` so the intraday series is evaluated alone (the 1d /
  smoke / demo CSVs are excluded from this run).
* `87193` 5m bars grouped into **1165 trading days**; `0` invalid days.

Result artifacts (git-ignored under `reports/`):
`reports/five_year_replay.json`, `reports/five_year_replay.html`,
`reports/five_year_replay.progress.json` (resumable checkpoint).

## 2. Method (honest by construction)

* Each trading day is validated (`validate_bars`) and replayed **independently**
  with the same deterministic `HistoricalEvaluator` / `BacktestEngine` used by
  WS 7.4/7.5 — no look-ahead (prefix-only bars), no future leakage, identical
  cost assumptions every day.
* Cost model (`EvaluationConfig` defaults): initial capital **₹100,000**,
  commission **0.03%/side**, fixed ₹0, slippage **0.1%/side**, 2% stop-loss
  enabled. MA(5,21) is the frozen champion baseline; nothing was tuned on this
  data.
* Period labels (training / validation / out-of-sample) are assigned up front
  from the processed-day count (`split_period`, 60/20/20), never influenced by
  results: **699 / 233 / 233** days.
* `status` is `COMPLETE` only because `days_processed == days_available` and no
  day was skipped as invalid — the progress store makes an interrupted run
  resumable and two data sources are never confused (date+hash keys).
* Limitation (documented in `docs/model_performance_report.md` §4/§10): this is
  **one-session-per-day** replay (each day restarts a fresh ₹100k portfolio and
  the composite equity curve sums the daily increments). It is NOT the same as a
  single continuous multi-year session replay; the two are different, equally
  honest measurements and must not be mixed. This run also does not consume the
  protected OOS — it aggregates recorded evidence for the promotion gate, it is
  not a strategy-selection device.

## 3. Aggregate results (frozen MA(5,21), long-only)

| Metric | Value |
|---|---|
| Trading days (available / processed / invalid) | 1165 / 1165 / 0 |
| Span | 2022-01-03 .. 2026-09-11 |
| Status | **COMPLETE** |
| Round trips | 1622 |
| Winning / losing | 83 / 1539 |
| Win rate | 5.12% |
| **Net P&L** | **−69.16 ₹** |
| Net return % | −0.069% |
| Transaction costs (commission + slippage) | 107 561.65 ₹ |
| Average trade | −0.043 ₹ |
| Profit factor | 0.038 |
| Max drawdown | 945.05 ₹ (0.94%) |
| Exposure (time in market) | 30.98% |
| Losing-streak (bars) | 10 |

Reading: after ~4.7 years and 1622 round trips the baseline is essentially at
breakeven on price-by-price moves while paying **≈ ₹107.5k in friction**. The
near-zero net P&L is fully explained by trading costs dominating a barely
positive gross price P&L. This is consistent with the established conclusion
(**B — no credible edge; MA(5,21) stays frozen**): a strategy whose net result is
a cost-driven breakeven never merits real-money promotion (§17e/§17f.7).

## 4. Relationship to prior findings

* The **continuous single-session replay** over the same 87,193 bars
  (`docs/model_performance_report.md`) nets **−143.21%**, also dominated by
  friction. This run replays each day independently (a different, weaker-cost
  framing per §10 of that report) and lands near breakeven. Either way the
  conclusion is identical: no net-of-cost edge.
* No strategy behavior changed, no promotion was executed, and the protected
  OOS was not touched by this run.

## 5. Conclusion for §17g

WS 7.5's five-year replay capability has now been exercised on the real 5m
history to its maximum available span: **1165/1165 trading days COMPLETE**.
The deliverable is evidence plus honest coverage (`start`/`end`/`days_processed`
are recorded, not assumed). Any future "five years" claim requires the provider
to serve 5m bars before 2022 — which it currently does not.