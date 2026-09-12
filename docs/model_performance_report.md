# Model Performance / Trading Performance Report — Champion MA(5,21)

**Deliverable status:** IMPLEMENTED (generated from the committed evaluation harness)
**Evidence period:** 2022-01-03 .. 2026-09-11 (real Uptox NIFTY 50 5-minute history)
**PAPER TRADING ONLY — NO LIVE ORDER.** Everything in this report is produced by a
deterministic evaluation harness over historical data using the paper execution path
(`BacktestEngine` -> `PaperBroker` -> `Portfolio` -> `RiskManager` -> stop-loss). It is a
model-performance record, not a recommendation to trade and not investment advice.

---

## 1. Executive summary

The registered champion **MovingAverageCrossStrategy(fast=5, slow=21)** was replayed in a
**single continuous paper session** over all 87,193 real 5-minute NIFTY 50 bars from
2022-01-03 to 2026-09-11 with the product's paper cost/risk assumptions
(initial capital ₹100,000, quantity 1, commission 0.03%/side, slippage 0.10%/side,
2% protective stop-loss, daily-loss and notional risk gates).

Headline (net of costs):

| Metric | Value |
|---|---|
| Net return | **-143.21%** (final equity ₹-43,210 on ₹100,000) |
| Max drawdown | **-143.29%** of peak (equity went negative) |
| Round trips | 2,601 (≈1.2/day) |
| Win rate | 15.53% (404 wins / 2,197 losses) |
| Profit factor | 0.27 |
| Sharpe / Sortino (annualized, per-bar) | -1.95 / -2.08 |
| Transaction costs paid | ₹146,129 (commission ₹33,722 + slippage ₹112,407) |
| NIFTY buy-and-hold, same period (gross) | **+27.96%** (1d bars) / +29.62% (5m bars) |

The honest verdict:

- **The champion loses -143% net over available real history.** Every year was negative
  (2022 -₹21,101; 2023 -₹25,808; 2024 -₹37,513; 2025 -₹36,793; 2026 -₹21,995).
- **Before friction the strategy is roughly break-even**: gross trading P&L was +₹2,918
  over the whole period (+2.9% on capital), and the zero-cost scenario returns +2.92%.
  The strategy trades far too often for its math edge — slippage (₹112k) and commission
  (₹34k) destroy it. Annualized per-bar vol of ~1.4% and the 15.5% win-rate confirm a
  whipsaw-dominated signal.
- **There is no verifiable positive in-sample or out-of-sample result on this data.**
  Train/validation/test segments were all negative (see §8), walk-forward combined return
  was **-66.79%**, and every decision-time regime bucket was negative (see §7).
- This is a **clear negative result for the registered champion on real data**. The
  earlier promotion evidence was generated on synthetic smoke data (296 daily bars,
  2 trades) and is **not representative**: with real friction this strategy does not
  trade profitably.

**Action for the product:** keep MA(5,21) **frozen**; do not promote or expand it. Route
the next promotion/variant work through the staged evaluation gates in §10.

---

## 2. What exactly was evaluated

| Item | Value |
|---|---|
| Strategy | `MovingAverageCrossStrategy(fast=5, slow=21)` (frozen, stateless) |
| Engine | `BacktestEngine.run` — one continuous session, no per-day portfolio resets |
| No-look-ahead | signals at bar *i* are a pure function of `bars[:i+1]`; regimes use `RegimeDetector.detect_prefix` at the entry bar |
| Broker | `PaperBroker` backtest fill path (same cost model as live paper fills) |
| Risk | `RiskManager` (notional/daily-loss) + 2% stop-loss, both enabled |
| Initial capital / size | ₹100,000 / absolute quantity 1 per signal |
| Costs | commission 0.0003, commission_fixed 0, slippage 0.001 (per fill) |

### Continuous replay (why this matters)

The previous "five-year" evaluation replayed **one day per session** (a fresh `Portfolio`
every day), which reset the strategy's MA state and its open position every day. That hid
the compounding/state effects of MA(5,21) on 5-minute bars. This report replays the entire
history in **one session**, so the strategy, position and equity compound exactly as a live
paper session would. (See `src/fno_ai_paper_trading/evaluation/model_performance.py`,
`engine.run(..., signals=...)`.)

### How it works (verified by tests)

- `moving_average_cross_signals(bars, fast=5, slow=21)` precomputes the exact signal
  sequence in O(n) and **equals** `strategy.analyze(bars[:i+1])` for every prefix
  (`tests/test_fast_signal.py`).
- The engine accepts a precomputed signal stream and is proven identical to the per-bar
  `analyze` path by test (`tests/test_model_performance.py::test_engine_signals_path_matches_analyze_path`).
- Round-trip table reconciliation holds exactly: Σ round-trip net P&L == `result.total_pnl`
  (₹-143,210.372044480 both ways).
- Full suite: **813 passed** (788 pre-existing + 25 new).

---

## 3. Data coverage and quality (Section A)

Both datasets were fetched read-only from Upstox V3
(`scripts/acquire_model_performance_data.py`, 0 failed windows) and validated with
`validate_bars`.

| | 5m series (evaluation) | 1d series (benchmark/context) |
|---|---|---|
| Provider | Upstox V3 | Upstox V3 |
| Instrument | NSE_INDEX Nifty 50 | NSE_INDEX Nifty 50 |
| Range | 2022-01-03 .. 2026-09-11 | 2005-01-03 .. 2026-09-11 |
| Bars | 87,193 | 5,382 |
| Canonical hash | `6c400b016c1c…` | `aa24cf2db465…` |
| Validation | OK (no errors) | OK (no errors) |

Coverage facts (5m): 1,170 trading days present; modal bars/day 75 (full NSE 5m session);
partial-day and spacing notes are listed in `reports/model_performance/summary.json`
under `data.5m_coverage`.

**Coverage limits (documented honestly):**
- Real 5m history for this instrument starts **2022-01-03**. The evidence period is
  **~4.7 years, not five full years**; earlier "five-year" claims were synthetic.
- "Candidate missing weekday days" are an upper bound: the repo's market calendar only
  encodes the current year's NSE holidays, so official holidays in 2022–2025 are counted
  as candidate gaps rather than confirmed missing data.
- No data was synthesized or repaired; gaps ≥ 5× expected cadence are warnings only.

---

## 4. Headline results and equity path

Detailed artifacts: `reports/model_performance/{summary.json, trades.csv, equity_curve.csv, model_performance.html}`.

- Equity started at ₹100,000 and fell in every calendar year; minimum equity ₹-43,292 in
  September 2026. Max drawdown duration 87,093 bars (almost the whole series underwater).
- Exposure: 47.9% of bars had an open position.
- Mean over the 2,601 round trips: ₹-55.06 net/trade (expectancy negative).

Regeneration:

```
.venv\Scripts\python.exe scripts\generate_model_performance_report.py
```

---

## 5. Benchmark comparison

| Metric | Champion (5m, net) | NIFTY buy&hold 5m (gross) | NIFTY buy&hold 1d same period (gross) |
|---|---|---|---|
| Return % | -143.21 | +29.62 | +27.96 |
| Final equity (₹100k) | -43,210 | +129,625 | +127,964 |
| Max drawdown % | 143.29 | 15.41 | (see summary.json) |

Index gross price return over the period (close-to-close, no costs): **+33.91%** (5m close
series) / same 1d period value in `summary.json`. The buy-and-hold benchmark holds most of
the capital for the whole window; the champion trades 1 unit at a time (24% style
exposure), so the comparison reflects a fundamentally different risk profile — which is
itself a finding (§11).

---

## 6. Year-by-year and monthly breakdown

Yearly realized P&L (by exit period):

| Year | Trades | Wins | Losses | Net P&L | Avg/trade | End equity |
|---|---|---|---|---|---|---|
| 2022 | 543 | 92 | 451 | -21,101 | -38.86 | 78,961 |
| 2023 | 521 | 54 | 467 | -25,808 | -49.54 | 53,091 |
| 2024 | 593 | 68 | 525 | -37,513 | -63.26 | 15,578 |
| 2025 | 558 | 66 | 492 | -36,793 | -65.94 | -21,261 |
| 2026 | 386 | 67 | 319 | -21,995 | -56.98 | -43,210 |

Monthly breakdown: 57 monthly rows in `reports/model_performance/monthly.csv` — negative
in the large majority of months (full table + HTML chart).

---

## 7. Regime analysis (decision-time regimes at entry)

Classified with the exact `RegimeDetector` (MA gap + short/long variance ratio) at the
**entry bar only** — never a future regime.

| Entry regime | Trades | Wins | Losses | Win rate % | Net P&L | Avg/trade |
|---|---|---|---|---|---|---|
| down_high | 91 | 12 | 79 | 13.19 | -6,904 | -75.87 |
| down_low | 25 | 4 | 21 | 16.00 | -1,681 | -67.23 |
| down_normal | 38 | 7 | 31 | 18.42 | -1,446 | -38.05 |
| sideways_high | 447 | 69 | 378 | 15.44 | -26,101 | -58.39 |
| sideways_low | 972 | 95 | 877 | 9.77 | -55,768 | -57.37 |
| sideways_normal | 1028 | 160 | 868 | 15.56 | -51,310 | -49.91 |

**No regime bucket is profitable.** The largest loss concentration is in sideways-low
volatility (972 trades, win rate under 10%): precisely the whipsaw regime an index
MA(5,21) crossover struggles with. This is an incentive-clear negative-regime result, not
evidence for a regime filter to be promoted blind (§10 gate).

---

## 8. In-sample / validation / out-of-sample

Chronological 60/20/20 split, each segment evaluated with the same frozen champion under
identical assumptions (MA(5,21) has **no data-fitted parameters**, so these segments
demonstrate stability across time, not parameter fitting).

| Segment | Range | Bars | Net return % | Trades | Win rate % | Max DD % | Transaction costs |
|---|---|---|---|---|---|---|---|
| train | 2022-01-03 .. 2024-10-25 | 52,315 | -79.26 | 1,560 | 14.81 | 79.56 | 80,057 |
| validation | 2024-10-25 .. 2025-10-03 | 17,438 | -32.55 | 528 | 15.91 | 32.78 | 33,191 |
| test (OOS) | 2025-10-03 .. 2026-09-11 | 17,440 | -30.52 | 510 | 18.63 | 30.60 | 32,752 |

Walk-forward (fixed champion, 14 non-overlapping windows, train 22,000 / test 4,400 bars):
combined test-side return **-66.79%**, 1,824 test trades, total transaction costs on test
≈ ₹105.8k. All 14 per-window rows are in `summary.json`. Because the champion is frozen
every window, this is a **stability audit across time — not an optimization loop**.

---

## 9. Cost sensitivity (bounded, explicit)

Strategy decisions never read costs, so entries/exits are identical across scenarios
(trade counts are constant); the table isolates friction:

| Scenario | Commission | Slippage | Trades | Net return % | Transaction costs | Max DD % |
|---|---|---|---|---|---|---|
| zero_cost | 0 | 0 | 2,601 | +2.92 | 0 | ~143.29 |
| low_cost (0.5×) | 0.00015 | 0.0005 | 2,601 | -70.15 | 73,064 | 143.29 |
| base (1×) | 0.0003 | 0.001 | 2,601 | -143.21 | 146,129 | 143.29 |
| high_cost (2×) | 0.0006 | 0.002 | 2,601 | -289.34 | 292,257 | 143.29 |

The zero-cost result (+2.92%) is the *only* positive scenario and it is below the
transaction cost of a single average trade. **There is no cost regime that makes this
strategy attractive on NIFTY 5m.**

---

## 10. Implications, evaluation gates, and next steps

Verdict language (noise limits explicit):

- **Clear signal:** on the available real evidence the champion does not work net of costs.
- **Overfit risk:** none of the splits show recoverable edge; do **not** shop for a
  parameterization on this single 4.7-year window — that would be overfitting.
- **Exposure mismatch:** 1-unit absolute sizing with a 2% stop is structurally different
  from a fully-funded hold; comparisons in §5 reflect that.

Staged release / evaluation gates (this is the framework the report already exercises):

1. **Gate 1 (now):** champion stays frozen; document negative evidence (this report).
2. **Gate 2 (research only):** investigate signal variants only inside the continuous
   replay harness with identical cost/risk assumptions; require positive **net-of-cost**
   result on train+validation before any OOS look.
3. **Gate 3 (promotion):** a challenger may be promoted only if it beats the frozen
   champion on net returns, max drawdown and per-trade expectancy on the OOS window and
   walk-forward combined return — and survives a zero-cost sanity check like §9.
4. **Gate 4 (any live path):** re-verify on the live paper broker, paper state and risk
   manager with the exact same decision-time capture before any go/no-go. No direct-to-live.

Recommended next research items (PLANNED, not yet implemented):
- Cost reduction study: fewer, higher-conviction signals (hold-bar filter) to reduce the
  0.10%/side slippage exposure — quantify trades-vs-slippage trade-off.
- A regime-gated variant (long only in UP regimes, skip sideways-low) evaluated *only*
  through the same gate pipeline — not promoted on this window.
- Longer history once available (real 5m coverage currently begins 2022-01-03).

---

## 11. Limitations (documented honestly)

1. **Not five years.** Real 5m history starts 2022-01-03 (~4.7 years to 2026-09-11);
   anything labeled "five-year" previously was synthetic smoke data.
2. **No cash/margin floor in the paper model.** Both the backtest and the live paper
   broker fill orders regardless of available cash. The negative-equity stretch
   (min ₹-43,292) is an implicit-leverage artifact of the paper model, not a claim about a
   real broker rejecting trades.
3. **Benchmark is gross; champion is net.** Buy-and-hold pays no costs by design.
4. **Cost model for NIFTY index is a fixed assumption** (0.03% commission + 0.10%
   slippage per side). Actual broker/tax friction is not modeled (no STT/charges for
   index units).
5. **Candidate missing days are upper bounds** (historical NSE holidays not in calendar).
6. **Single instrument, single window.** Results are specific to NIFTY 50 5m 2022–2026;
   they are evidence, not a distributional law.

---

## 12. Reproducibility and artifacts

Commands:

```
.venv\Scripts\python.exe scripts\acquire_model_performance_data.py   # fetch (once)
.venv\Scripts\python.exe scripts\generate_model_performance_report.py # evaluate + write artifacts
.venv\Scripts\python.exe -m pytest tests\ -q                          # 813 passed
```

Artifacts (git-ignored `reports/model_performance/`):

| Artifact | Contents |
|---|---|
| `summary.json` | full machine-readable evidence bundle |
| `trades.csv` | 2,601 round trips with entry/exit regimes, holding bars, P&L |
| `yearly.csv` / `monthly.csv` | realized P&L by exit period |
| `regime.csv` | decision-time regime breakdown |
| `equity_curve.csv` | 87,193 mark-to-market snapshots |
| `model_performance.html` | self-contained report with "PAPER TRADING — NO LIVE ORDER" banner |

New code (committed):

- `src/fno_ai_paper_trading/evaluation/fast_signal.py` — O(n) exact MA-cross signals
- `src/fno_ai_paper_trading/evaluation/model_performance.py` — continuous replay, RT table,
  aggregation, benchmark, splits, walk-forward, cost sensitivity
- `src/fno_ai_paper_trading/evaluation/coverage.py` — data coverage/quality audit
- `src/fno_ai_paper_trading/evaluation/perf_reports.py` — CSV/JSON/HTML writers
- `src/fno_ai_paper_trading/backtest/engine.py` — optional precomputed `signals` stream
  (backward compatible, covered by the existing 788 tests + 25 new)
- `scripts/acquire_model_performance_data.py`, `scripts/generate_model_performance_report.py`
- `tests/test_fast_signal.py`, `tests/test_model_performance.py`, `tests/test_coverage.py`