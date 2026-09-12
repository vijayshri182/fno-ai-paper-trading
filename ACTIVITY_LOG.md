# F&O AI Paper Trading System — Activity Log

> Working log of implementation progress for the `fno-ai-paper-trading` project.
> This file records *what* was built, *why it was designed that way*, and the
> exact safety guarantees that hold after each change.

---

## 1. Log Conventions

- One dated entry per working session.
- Entries list: scope, decisions, files touched, verification performed, and any
  open risks/issues.
- A change is only marked **done** after tests pass and the entry point runs.
- Nothing is marked "done" purely on intent — only after verification.

---

## 2. Phase Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation (models, config, provider interface, paper broker, portfolio, risk, tests) | **DONE** — commit `448e02f` |
| 2 | Real market data + first deterministic strategy + backtest harness | **DONE** — commits `a86ec64`, `a1c1d7d` |
| 3 | Strategy research & robustness evaluation (costs, regimes, in/out-of-sample, walk-forward, sensitivity, benchmark, metrics, experiments, HTML notebook) | **DONE** — commits `fb3f1c5`, `3a60a41` |
| 4 | Historical-data CLI + real-data research (NIFTY 50 1d 2015–2024) | **DONE** — commits `da1b59a`, `e1a2b38`; real dataset acquired 2026-09-08 |
| 5 | AI analysis / decision support; backtesting engine + analytics | **Backtest engine:** folded into Phase 2 (implemented); analytics extended by the research framework (Phase 3). **AI analysis / decision support:** planned (not started) |
| 6 | Paper Trading V1 — current-data paper session | **DONE — Phase 6 COMPLETE · V1 READY** — WS 6.2 (`e853667`), WS 6.3 (`b4f8129`), WS 6.4 stop-loss (`75d3a44`), WS 6.4b session runtime (`b05a17c`), WS 6.5 persistence (`118e976`), WS 6.7 acceptance replay (`0e5ce1c`, 30 tests), WS 6.1 doc alignment (`248c895`), WS 6.6 session ops / monitoring (`00ed8ed` + docs `c4570ba`; 561 suite at close), all pushed. 2026-09-11 documentation finalization → **Phase 6 COMPLETE · V1 IMPLEMENTATION COMPLETE · V1 STATUS: READY** (561/561 tests, 30/30 acceptance, paper-only boundary, deterministic runtime, persistence/recovery, monitoring, no live broker execution) |

*Phase numbers in this table follow the activity log's own scheme; for plan-level numbering see PROJECT_PLAN §17d (Paper Trading V1 = Phase 6).*

---

## 3. Non-Negotiable Guarantees

These hold for every phase and every entry below:

1. Paper trading only — no live broker, no live order placement anywhere.
2. No hard-coded credentials, tokens, or secrets; credentials come from
   environment variables / `.env` (git-ignored).
3. External APIs are reached only through the `MarketDataProvider` abstraction.
4. The strategy engine never places orders directly — signals flow through
   `RiskManager` → `PaperBroker` → `Portfolio`.
5. No `git push` unless explicitly requested by the user.

---

## 4. Session Log

### 2026-09-06 — Phase 1: Foundation (commit `448e02f`)

- Repository initialized; Python 3.13.14 virtualenv in `.venv`.
- Built core domain models: `Instrument`, `Order`, `Fill`, `Position`, `Trade`,
  `MarketPrice`, enums (`OrderSide`, `OrderStatus`, `OrderType`, `Signal`,
  `InstrumentType`, `RejectionReason`).
- Built environment-based configuration (`config/settings.py`, `.env.example`).
- Defined `MarketDataProvider` ABC and the deterministic in-memory provider
  (`data/mock_provider.py`) — the only provider shipped in Phase 1.
- Built `PaperBroker` (simulated fills, slippage, commission), `Portfolio`
  (cash/positions/P&L), and `RiskManager` (quantity, notional, daily-loss limits).
- Added `TradingService` orchestration and a minimal paper-only demo
  (`src/main.py`).
- Added unit tests (models, broker, portfolio, risk) plus an HTML test-report
  generator script.
- Verified: `python src/main.py` runs; full test suite passes offline.

### 2026-09-07 — Phase 2: Real Market Data + Strategy Foundation (in progress)

**Objective.** Add a real market-data provider (Zerodha Kite Connect v3),
isolated behind the `MarketDataProvider` ABC, plus the first deterministic
strategy (moving average cross). Everything continues to execute through
`PaperBroker` only.

**Design decisions (recorded before coding).**

1. **Provider isolation.** The rest of the system depends only on the
   `MarketDataProvider` ABC. The Kite implementation (`data/kite_provider.py`)
   is the only module that knows the vendor, its URL scheme, its
   `X-Kite-Version: 3` header, and its auth format. All responses are converted
   into internal domain models.

2. **No new dependencies.** HTTP is handled with the standard-library `urllib`
   transport (`utils/http.py`). Retries use a small exponential-backoff helper
   (`utils/retry.py`) with injectable sleep for deterministic tests. This keeps
   `requirements.txt` at `python-dotenv` + `pytest`.

3. **Credentials only via environment.** Kite needs `api_key` +
   `access_token`. A dedicated `KiteSettings` dataclass is loaded from
   `FNO_KITE_*` variables. `.env.example` shows only safe placeholders.
   `ProviderConfigurationError` is raised if the provider is asked to reach the
   network without credentials.

4. **Market hours are explicit.** `data/market_hours.py` models the NSE
   derivatives session in `Asia/Kolkata` (09:00–09:15 pre-open, 09:15–15:30
   continuous) with a best-effort 2026 holiday calendar. Session state is
   exposed as a normalized `MarketSession` model so callers do not do their own
   timezone math.

5. **Rate limits / failures cannot corrupt the portfolio.** Rate-limit (429)
   and server/transient failures are retried with backoff, then surfaced as
   typed `MarketDataError` subclasses. Failing market data never advances the
   portfolio — orders are simply not created.

6. **Strategy layer is deterministic and execution-free.** `Strategy` produces
   `SignalResult` (BUY/SELL/HOLD + reason + metadata). `StrategyEngine` replays a
   price series bar-by-bar. A `StrategyService` translates signals into orders
   *only through* `TradingService`, which already guards that the broker is a
   `PaperBroker`. Strategies can never call a broker directly.

7. **Sandboxed paper-run.** The Phase 2 demo builds a deterministic price series
   (producing a genuine fast/slow crossover both ways), runs the moving-average
   strategy over it, and logs every emitted signal plus the resulting paper
   fills. Instruments come from the sample "plugin"
   (`build_sample_instruments()`), not from a live instrument fetch, so the
   run is reproducible and offline. Vendor-specific live features (instrument
   master CSV, historical candles, live quotes) live entirely inside the Kite
   provider and are exercised by unit tests with mocked HTTP — never during a
   demo run.

**Change log.**

- Added `ACTIVITY_LOG.md` (this file).
- Models: added `MarketQuote`, `MarketSession`, `open_interest` on
  `MarketPrice`, `exchange`/`exchange_token` on `Instrument`, `MarketPhase`
  enum.
- `utils/http.py`: urllib JSON/CSV GET transport with timeouts and typed
  `HttpError`.
- `utils/retry.py`: exponential-backoff `retry_call` with injectable sleep.
- `data/errors.py`: `MarketDataError` hierarchy (`ProviderConfigurationError`,
  `AuthenticationError`, `RateLimitError`, `InstrumentNotFoundError`,
  `MarketClosedError`, `UnavailableError`).
- `data/market_hours.py` + `data/kite_provider.py`; extended the ABC and the
  in-memory provider to the full Phase 2 interface.
- `strategies/`: `base.py` (Strategy + SignalResult), `engine.py`,
  `moving_average_cross.py`.
- `services/strategy_service.py`: `SignalDecision` + `StrategyService`
  (evaluate-only and paper-execute modes) and the deterministic demo series.
- Config: `KiteSettings` + `FNO_KITE_*` env vars; `.env.example` extended.
- Entry point: `src/main.py` gains a paper-only strategy demo (in-memory
  provider, deterministic series, `PaperBroker` only).
- Tests: market hours, quote/session/OI models, retry/http helpers, Kite
  provider (mocked HTTP), moving-average strategy, strategy-service integration.
- Docs: README + PROJECT_PLAN updated.

**Verification (final pass).**

- `pytest -q`: **134 passed, 0 failed** (0.47s).
  - Fixed latent Phase 1 defect: `Trade.realized_pnl` was forced
    non-negative, crashing `portfolio.apply_fill` on realized losses; now
    signed Decimal (finite), with regression test.
  - Fixed test bugs (not product bugs): insufficient-bars assertion expected
    `21` but strategy requires `22` (slow+1) bars; header-forwarding assertion
    iterated dict keys instead of `.items()`.
- `python src/main.py`: Phase 1 demo + Phase 2 strategy demo run paper-only
  (BUY at bar 25, SELL at bar 50, flat end state, `cash 99369.76`).
- `scripts/generate_test_report.py`: `reports/test_report.html` — 134 tests /
  0 failures / 0.38s.
- `git diff` review for the single commit (no push) — see next entry.

---

## 4b. Phase 2 — commit record (2026-09-07)

- Commit message: `feat: implement phase 2 market data and strategy foundation`
- Not pushed (consistency with Phase 1; pending user visibility).

---

## 4c. 2026-09-07 — Phase 2: Deterministic Backtest Harness

**Objective.** Build a deterministic backtest harness (`fno_ai_paper_trading
.backtest`) that replays historical OHLCV bars through a strategy and the same
`PaperBroker` used for paper trading, produces performance metrics and an equity
curve, and reuses Phase 1/2 core paths unchanged. Paper-broker only — no
credentials, no network, no live orders.

**Design decisions (recorded before coding).**

1. **Stage-1 scope, explicit assumptions.** Included: data/bar interface,
   deterministic engine with chronological replay, explicit execution
   assumptions (`BacktestConfig`), performance metrics + equity curve, costs
   (commission + slippage), and deterministic datasets. Explicitly deferred:
   market-session handling, multi-instrument, order-expiry, venue/partial fills,
   and a historical-data CLI (Phase 3).
2. **No look-ahead.** At bar `i` the strategy receives only `bars[:i+1]`. Orders
   generated at bar `i` fill at bar `i`'s close price (with slippage). Tests
   assert the backlog stays flat until the first signal bar.
3. **Determinism without wall-clock time.** A `BacktestBroker(PaperBroker)`
   override of `place_order` injects `_fill_timestamp` from the engine's
   current bar via `set_timestamp`, so fills carry bar timestamps instead of
   `datetime.now()`. Every cost is explicit `Decimal` arithmetic, so identical
   input always yields identical output (repeat-run equality is asserted).
4. **Round-trip trade statistics.** A trade counts only when a position is
   closed (`_is_closing_fill`); gross profit/loss and win rate reflect closed
   round trips. `BacktestResult.total_commission` is tracked separately.
   Gross P&L excludes costs; net P&L (`final_equity - initial_capital`)
   includes all costs.
5. **Reuse, don't fork.** The engine drives the existing
   `MovingAverageCrossStrategy.analyze`, `StrategyEngine` signals, `RiskManager`
   (built from `PaperSettings` with `Environment.TEST`), `PaperBroker`, and
   `Portfolio.apply_fill`. Risk limits behave identically to paper trading; a
   rejected order is skipped. `BacktestConfig.enable_risk_manager=False`
   bypasses risk checks for trade-level mechanics tests (matching Griffin).
6. **Fixtures are hand-verified, not generated blindly.** Every dataset is a
   deliberately shaped close vector whose expected fills and P&L are computed by
   hand for `MovingAverageCrossStrategy(fast=2, slow=3)` (needs slow+1 = 4 bars
   before a signal). Series: profitable (BUY@110 → SELL@120), losing
   (BUY@110 → SELL@100), drawdown (BUY@110 → SELL@90), multiple trades (two
   round trips), no-trade (never crosses), and short profit (SELL@140 opens,
   BUY@130 closes, gross +100).

**Change log.**

- `backtest/config.py`: validated `BacktestConfig` (capital, quantity,
  commission rate/fixed, slippage, risk toggle + limits).
- `backtest/result.py`: `EquityPoint` + `BacktestResult` (total P&L / return,
  bar count, signals/orders/fills, round-trip trade stats, gross profit/loss,
  profit factor, total commission, max drawdown, equity curve).
- `backtest/engine.py`: `BacktestBroker` (paper-only, stamped fills) +
  `BacktestEngine.run(bars, strategy, config)`; per-bar equity snapshot incl.
  unrealized P&L and drawdown-from-peak; leaves end-of-data positions open.
- `backtest/datasets.py`: six deterministic fixture series.
- `backtest/__init__.py` + `backtest/__main__.py`: package exports + offline
  backtest demo (`python -m fno_ai_paper_trading.backtest`).
- `src/main.py`: third demo (backtest) after Phase 1 + Phase 2 strategy demos.
- `tests/test_backtest.py` (22 tests): exact-cost P&L (1% slippage qty1 → 7.70;
  commission → 0.68997), costs/slippage reduce P&L, drawdown, chronological
  equity curve, no-look-ahead, signal timing (buy [5], sell [10]), signed
  realized P&L (Phase 1 regression guard), determinism-repeatability, short
  round trip, end-of-data open position, risk-limit rejection vs disabled,
  paper-only offline safety, empty series.
- Docs: README (backtest section, metric definitions, safety), PROJECT_PLAN
  (Phase 2 status, Section 17, DoD, Change Log), this log.

**Verification (final pass).**

- `pytest -q`: **156 passed, 0 failed** (0.45s) — 134 existing + 22 new.
- `python src/main.py`: all three demos run paper-only; backtest demo prints
  `final equity 100097.0100300`, `total P&L 97.0100300` for the 13-bar
  profitable fixture (BUY fill 110.11, SELL fill 119.88, commission 0.68997,
  drawdown 201.5596400 / 0.2009596%).
- `python -m fno_ai_paper_trading.backtest`: offline demo runs with no
  credentials and no network.
- `scripts/generate_test_report.py`: `reports/test_report.html` — 156 tests /
  0 failures / 0.42s.
- No provider/network interaction in backtest path: verified by offline smoke
  run and by the test that runs the engine with no Kite credentials set.

---

## 4d. 2026-09-07 — Phase 3: Strategy Research & Robustness Evaluation

**Objective.** Build a deterministic, paper-only research framework on top of
the backtest harness whose purpose is *scientific*: determine whether the
moving-average cross strategy has a meaningful edge after realistic costs, over
in-sample and out-of-sample periods alike, without ever optimizing parameters
against the data the result is reported on. Research output is clearly labelled
as synthetic/historical evidence.

**Design decisions (recorded before coding).**

1. **Costs are a first-class, configurable schedule, not a single number.** The
   backtest's flat commission/slippage fields are kept for compatibility, and
   `BacktestConfig` gains two duck-typed, optional overrides: `cost_schedule`
   (anything with `compute(side, notional, quantity) -> .total`) and
   `execution` (anything with `total_adverse_rate`). Defaults are `None`, so all
   existing behaviour and numbers are unchanged (regression-tested).
2. **The illustrative schedule is honest.** `IndiaCostSchedule.nse_fo_illustrative()`
   documents example component rates (STT sell-side 0.0000125, exchange 0.00002,
   SEBI 0.000001, stamp duty buy-side 0.000002, GST 18% on brokerage + exchange +
   SEBI) and is explicitly not a claim of any broker's current fees. GST excludes
   STT and stamp duty (verified against the implementation and by hand in tests).
3. **No optimizer, no ML, no giant grid.** `run_parameter_sensitivity` evaluates
   only caller-enumerated `(fast, slow)` combinations; impossible ones
   (fast ≥ slow) are reported as skipped, never executed. The research question
   is robustness of an explicitly chosen configuration, not "which maximizes
   the backtest".
4. **Out-of-sample honesty.** Splits are chronological/contiguous/disjoint.
   Walk-forward windows advance `[train][test]` with step ≥ test (non-overlap);
   each test window is evaluated against a fresh `build_strategy(train_bars)`
   strategy over the test bars only — no trading ahead, no warm-up leakage.
5. **Determinism everywhere.** Regime datasets are float-free, formula-derived
   close sequences (no randomness); regime shapes guarantee real MA crossovers
   (a pure monotone ramp produces *zero* crossovers, which was discovered and
   corrected by making the counter-drift outlast the slow window). Experiments
   carry a stable `config_hash` (SHA-256 over canonical JSON) so identical runs
   are provably reproducible.
6. **Metrics are robust and None-safe.** CAGR requires a minimum trading
   horizon; Sharpe/Sortino require enough return samples; exposure counts bars
   in which any position is held (including an open position marked to market at
   end-of-data). "Unavailable" values render as `n/a`, never fake zeros.

**Change log.**

- `research/` package added:
  - `costs.py` — `IndiaCostSchedule` + `ChargeBreakdown` (exact Decimal math,
    documented assumptions, compute contract for the engine).
  - `execution.py` — `ExecutionAssumptions` (slippage + half-spread + impact →
    `total_adverse_rate`).
  - `regimes.py` — `build_sustained_uptrend/downtrend`, `build_sideways_choppy`,
    `build_volatile_market`, `build_trend_reversal`, `build_low_volatility`,
    `REGIME_BUILDERS`, `regime_stats`.
  - `split.py` — `SplitScheme`/`Split`/`split_bars`/`split_indices` (60/20/20
    default, must sum to 1).
  - `walkforward.py` — `plan_windows` (step ≥ test_size), `run_walk_forward`
    (per-window OOS results, compounded combined return), `WalkForwardStep`/`Result`.
  - `sensitivity.py` — `run_parameter_sensitivity`, `SensitivityRow(+Skipped)`,
    `validate_ma_pairs`.
  - `benchmark.py` — `buy_and_hold` (gross, 100% exposure, `ValueError` if
    unfunded) + `BenchmarkResult`.
  - `metrics.py` — `compute_metrics` + `PerformanceMetrics` (net P&L/return,
    CAGR, drawdown+duration, win rate, profit factor, expectancy, avg win/loss,
    annualized vol / Sharpe / Sortino, exposure).
  - `experiment.py` — `ExperimentConfig` (provenance + `config_hash`),
    `ExperimentResult`, `run_experiment`.
  - `report.py` + `__main__.py` (offline demo) + `__init__.py` exports.
- Backtest wiring: `BacktestConfig.cost_schedule` / `BacktestConfig.execution`
  (backward compatible); `BacktestBroker._resolve_commission`; engine slippage
  override; public `closes_to_bars` alias in `backtest/datasets.py`.
- `scripts/generate_research_report.py` → `reports/research_report.html`
  (sections: costs, execution, regimes, experiments, in/out-of-sample,
  walk-forward, sensitivity, benchmarks).
- `tests/test_research.py` (45 tests) — hand-verified values throughout.
- Docs: README (research section + test table + future phases), PROJECT_PLAN
  (§17b + Change Log + current-phase status), this log.

**Verification (final pass).**

- `pytest -q`: **201 passed, 0 failed** (156 + 45 new).
- `python -m fno_ai_paper_trading.research`: offline demo runs with no
  credentials and no network; shows a closed round trip on trend-reversal, OOS
  trading on volatile, 6-combo sensitivity on choppy, walk-forward OOS, and
  gross buy-and-hold benchmark.
- `python scripts/generate_research_report.py`: writes
  `reports/research_report.html` (git-ignored).
- Engine wiring verified by tests: flat cost schedule per fill (2 × 1.23 =
  2.46 total commission) and execution `total_adverse_rate` replacing slippage
  (BUY@110→115.5, SELL@120→114, qty10 → P&L −15).

---

## 4e. 2026-09-07 — Phase 3 audit fixes + Upstox historical-data readiness

**Objective.** Audit the Phase 3 research framework for cost/slippage
transparency, out-of-sample integrity and reproducibility; fix any defects; then
make the project ready for real historical research via Upstox behind a
vendor-neutral provider interface — read-only, no credentials in the repo,
optionally testable with a real token, fully offline by default.

**Audit findings fixed.**

1. **Slippage was invisible.** Backtest results reported net P&L and commission
   but never the slippage component separately, so total friction could not be
   attributed. `BacktestResult`/`PerformanceMetrics` now carry `slippage_cost`
   and a `transaction_costs` property (= commission + slippage). The engine
   accumulates `abs(fill.price − bar.close) × quantity × multiplier` per fill.
   Hand-verified: BUY@110→115.5, SELL@120→114, qty 10 → `slippage_cost = 115`.
2. **Experiment hash ignored cost settings.** Two runs with different
   commissions produced the same `config_hash`. `ExperimentConfig` now folds a
   canonical JSON of commission/slippage/risk scalars (`backtest_settings`) into
   the deterministic SHA-256 hash; regression-tested (different commission ⇒
   different hash, identical config ⇒ identical hash).
3. **Case collision in interval tokens.** `canonical_interval("1M")` (month)
   lowercased into `1m` (minute). Interval resolution is now case-sensitive for
   the minute/month pair while still tolerating casing/whitespace for other
   tokens.

**Historical-data readiness (vendor-neutral).**

- `data/intervals.py` — canonical interval tokens (`1m`…`1M`) + per-vendor
  mappings (Upstox `unit`/`interval`, Kite bucket labels) + minute lengths.
- `data/upstox_provider.py` — `UpstoxHistoricalDataProvider`, historical OHLCV
  read-only (`GET /v3/historical-candle/...`), normalized `MarketPrice` bars,
  typed error mapping as of this commit (401/403→AuthenticationError, 404→
  InstrumentNotFound, UDAPI date-range codes→MarketDataError, 429→RateLimitError,
  5xx→Unavailable; the 403→AuthenticationError mapping was corrected on
  2026-09-08 — see §4g: 401 stays AuthenticationError, 403 becomes a
  Cloudflare-aware MarketDataError), candle validation (timestamps ISO/epoch,
  ordering, duplicates), timezone→naive
  IST. No order-touching surface at all.
- `data/dataset_store.py` — local cache: `<name>.csv` (canonical columns) +
  `<name>.meta.json` (provider, interval, instrument, range, count,
  checkpointed-at, SHA-256 `data_hash`); rejects mis-ordered/duplicate input
  rather than silently re-sorting. `datasets/` is git-ignored.
- `data/validation.py` — report-only dataset checks (OHLC sanity, ordering,
  duplicates, timezone hygiene, cadence gaps); warns on >5× gaps; never repairs.
- `config/settings.py` — `UpstoxSettings` + `load_upstox_settings`
  (`UPSTOX_*` env vars, requires only the access token).
- `scripts/upstox_smoke_test.py` — opt-in read-only connectivity check; exits 2
  without a token; optional `--save` writes a validated local dataset.
- `.env.example` — Upstox section (placeholders only) + `.gitignore` gains
  `datasets/`.

**Tests.** 298 passed (201 existing + new: `test_intervals.py`,
`test_upstox_provider.py` [mocked HTTP, incl. smoke-script gating via empty
env], `test_dataset_store.py`, `test_data_quality.py`, and audit-fix sections
in `test_research.py`). Regenerated `reports/test_report.html` and
`reports/research_report.html` (both git-ignored).

---

## 4f. 2026-09-07 — Real-data baseline research pipeline

**Objective.** Wire the existing Upstox historical-data provider, dataset store,
backtest harness, and research framework into a single reproducible real-data
study on the NIFTY 50 index. Keep the baseline strategy un-tuned; report honest
in-sample / out-of-sample evidence; block cleanly when no Upstox token is
available.

**Design decisions (recorded before coding).**

1. **Reuse, don't rebuild.** The pipeline uses `UpstoxHistoricalDataProvider`,
   `save_dataset`/`load_dataset`, `validate_bars`, `run_experiment`,
   `run_dataset_experiment`, `split_bars`, `run_parameter_sensitivity`,
   `run_walk_forward`, `buy_and_hold`, and the existing HTML report builders.
   No new market-data transport, no new cost model, no new backtest engine.
2. **Baseline parameters are locked.** `MovingAverageCrossStrategy(fast=5,
   slow=21)` is used everywhere. Sensitivity is run only on the in-sample data
   to check robustness, not to select a "best" parameter set for the OOS period.
3. **No look-ahead / OOS honesty.** The dataset is split 60/20/20 (train /
   validation / test). Parameter sensitivity uses only train+validation. The
   test segment, walk-forward test windows, and regime slices are evaluated with
   the locked baseline.
4. **Clean credential handling.** The acquisition script exits code 2 when
   `UPSTOX_ACCESS_TOKEN` is absent. A `--smoke` mode uses deterministic
   synthetic data and clearly labels the output as a pipeline smoke test, so no
   real data is fabricated.
5. **Cost-model limitation is explicit.** NIFTY 50 is a broad index; the
   illustrative NSE F&O cost schedule is unlikely to match its exact trading
   costs. The report lists this under limitations.

**Change log.**

- Added `research/real_data.py` with `DatasetStatistics`,
  `compute_dataset_stats(...)`, `evaluate_regimes(...)`, and
  `run_real_data_research(...)`.
- Added `scripts/research_real_data.py`: fetches NIFTY 50 1d data from Upstox
  (read-only, token required), validates, saves, runs the full research
  pipeline, and writes `reports/real_data_research_report.html`; `--smoke` runs
  offline for pipeline validation.
- Added `tests/test_real_data_research.py` (7 tests) covering statistics,
  end-to-end pipeline, determinism, OOS separation, CLI token gating, and smoke
  report generation.
- Updated README and PROJECT_PLAN.

**Verification.**

- `pytest -q`: **331 passed** (324 existing + 7 new), 0 failed.
- `python scripts/research_real_data.py --smoke` runs offline, writes the smoke
  report, and prints a summary (240 synthetic bars, MA(5,21), benchmark,
  walk-forward, sensitivity).
- `python scripts/research_real_data.py` without a token exits code 2 with a
  clear message and writes no report.
- No credentials, tokens, or dataset files are committed; `datasets/` and
  `reports/` remain git-ignored.

**Post-delivery bug fix (same session).**

- Fixed `upstox_provider.py` to URL-encode the instrument key in the
  `GET /v3/historical-candle/...` path. The raw key `NSE_INDEX|Nifty 50`
  contained a space, which the stdlib HTTP transport rejected. The key is now
  percent-encoded using `urllib.parse.quote`, matching the Upstox docs' curl
  examples.
- Added regression tests covering `NSE_INDEX|Nifty 50` encoding.
- Adjusted CLI token-gating tests so an empty `UPSTOX_ACCESS_TOKEN` env var
  correctly prevents `python-dotenv` from loading a token from `.env`.
- Real-data run attempted after the fix: the request *reached* Upstox but came
  back HTTP 403. That was initially mis-attributed to the access token being
  rejected, and acquisition was wrongly described as "blocked on credentials".
  **This reading was wrong.** On 2026-09-08 (§4g) the 403 was traced to a
  Cloudflare WAF "browser signature banned" block (Error 1010) caused by the
  stdlib `urllib` TLS fingerprint — a transport/fingerprint problem, not a
  credential rejection. The same token reached the API once Windows used
  `curl.exe` (Schannel TLS); acquisition and the real-data research run then
  succeeded.

---

## 4g. 2026-09-08 — Cloudflare-safe Upstox transport: real-data acquisition + baseline research

**Root cause of the HTTP 403.** The 403 was **not** a credential rejection.
Every stdlib `urllib` request to `api.upstox.com` was stopped by the Cloudflare
WAF with HTTP 403 / Error 1010 "browser signature banned" — a TLS-fingerprint /
transport block. Proof: the same access token succeeded via `curl.exe`.

**Transport resolution (`d0bd276` encode + `fc03a08` curl transport, both
2026-09-08; `utils/http.py`).**

- On Windows the HTTP layer now shells out to `curl.exe` (schannel TLS);
  on non-Windows it keeps the stdlib `urllib` fallback.
- Auth/request headers are written to a temporary header file and passed via
  `curl -H @file`, so the access token never appears on the command line.
- Response headers are captured with `curl -D` into a scratch file and parsed
  (after `--location` redirects only the last `HTTP/...` block is kept).
- The public transport interface is preserved unchanged: `http_request` /
  `http_get` / `HttpResponse` / `HttpError`; `is_retryable_status` still treats
  only 429 and 5xx as retryable.
- `data/upstox_provider.py` error mapping corrected to distinguish:
  * `401` → `AuthenticationError` — access token rejected.
  * `403` → `MarketDataError`; when the body flags Cloudflare
    (`"cloudflare": true`), raised as "request blocked before the API (HTTP 403,
    Cloudflare \<code\> \<error_name\>)", otherwise the generic API 403 carries
    the server's reason.
  * Historical response ordering normalized: Upstox returns candles
    newest-first within a window; the provider now reverses them to ascending
    and validates the merged series is globally chronological and
    duplicate-free.

**Real-data acquisition (2026-09-08).**

- Dataset: `datasets/upstox_Nifty_50_1d_20150101_20241231` (CSV + `meta.json`);
  `datasets/` is git-ignored. Pulled via `scripts/acquire_dataset.py` /
  `scripts/upstox_smoke_test.py` (commit `da1b59a`) and the curated instrument
  registry (`data/instrument_registry.py`).
- Instrument: NIFTY 50 index (`NSE_INDEX|Nifty 50`, `lot_size=1`,
  `multiplier=1`).
- Period: 2015-01-01 through 2024-12-31; **2,477 daily bars**; validation
  passed.
- Dataset SHA-256 `data_hash`: begins `2dde47d4`, ends `6021b660`.
- This is **historical research data only** — input to the baseline study
  below, not used by any live or paper session.

**Baseline research (MA(5,21) on the real dataset, 2026-09-08).**

| Item | Value |
|---|---|
| Full-period net return | **−17.66%** (74 trades, 20.13% max drawdown) |
| Out-of-sample net return (locked params) | **+7.08%** (11 trades, 3.29% max drawdown) |
| Buy-and-hold benchmark (gross) | **+30.72%** |

- These are **historical backtest/research results only** — not live or paper
  trading results, and **not a profitability claim**.
- The historical study does **not** use a ₹1,00,000 virtual account; it reports
  net-of-cost returns from the research configuration in
  `scripts/research_real_data.py`.
- Test suite at the close of this work: **334 passed** (offline, deterministic).

---

## 4h. 2026-09-08 — Paper Trading V1 specification, config scaffolding, architecture doc

**Config scaffolding (commit `4518dda`, `chore: add paper trading config
scaffolding`).** Five additive `PaperSettings` fields with defaults only:

| Field | Default |
|---|---|
| `paper_interval` | `"5m"` |
| `paper_lookback_days` | `3` |
| `paper_risk_per_trade_pct` | `0.01` (1%) |
| `paper_stop_loss_pct` | `0.02` (2%) |
| `paper_state_dir` | `"paper_state"` |

**CONFIGURED-SCAFFOLDED** — they are configuration scaffolding/defaults only:
not read by any code and not wired into `load_settings()`, so no runtime
behavior changes.

**V1 specification (commit `59d831d`, `docs: define paper trading v1
specification`).** `docs/trading/PAPER_TRADING_V1.md` records the agreed V1
contract:

- Virtual starting capital ₹1,00,000; 5-minute **completed** candles; NIFTY 50
  index; **LONG-ONLY**; `MovingAverageCrossStrategy(fast=5, slow=21)`; max
  **1% risk per trade**; **2% fixed stop-loss**; risk-based sizing with
  valid-quantity/lot rounding; paper session only — no real orders, no live
  trading; persistence and the session loop are **not** part of the current
  build.
- 30 acceptance criteria; status **SPECIFICATION** (planned). The document is a
  contract/plan — the implementation is not yet written. Per its §2.4, the plan
  assigns Paper Trading V1 to **Phase 6** (PROJECT_PLAN §17d).

**Architecture documentation (commit `baeab01`, `docs: add system
architecture`).** `docs/architecture/ARCHITECTURE.md` created with the 20
required architecture sections; IMPLEMENTED / CONFIGURED-SCAFFOLDED / PLANNED
boundaries are documented explicitly.

---

## 4i. 2026-09-09 — Project plan alignment + current repository state

**Project plan alignment (commit `f2c1030`, `docs: align project plan with
current state`).** `PROJECT_PLAN.md` aligned with the actual repository state:
implementation status, real-data research results, the V1 contract, the
architecture, the roadmap (Paper V1 = Phase 6), and the current test status.

**Current repository state (as of `f2c1030`).**

- Test suite: **334 passed** (offline, deterministic).
- Branch: `master`; **HEAD == origin/master** at `f2c1030`; working tree clean.
- No secrets committed; `.env` remains git-ignored.

**Status boundary (explicit).**

- **IMPLEMENTED:** data acquisition (read-only Upstox adapter), dataset
  validation, historical research/backtesting harness, MA(5,21) strategy
  baseline, paper-trading foundation components (domain models, provider ABC,
  `PaperBroker`, `Portfolio`, `RiskManager`, services), and the
  architecture/specification documentation.
- **CONFIGURED-SCAFFOLDED:** the five `PaperSettings` V1 fields (defaults only;
  not consumed by any runtime path).
- **NOT IMPLEMENTED / PLANNED:** current-data paper-session loop;
  completed-candle scheduler; V1 risk-based position sizing; V1 2% stop
  execution; lot-size-aware sizing; persistence/state recovery; operational
  paper-session monitoring (as applicable); any live/real-money trading.
- **Boundary note:** the existing `RiskManager` static caps (max quantity 75 /
  max notional 250,000 / max daily loss 10,000) are absolute ceiling limits and
  are **not** the same mechanism as the planned V1 1%-of-equity risk sizing.

---

## 4j. 2026-09-09 — Phase 6 WS 6.2: Paper-trading domain/model completion

**Scope.** First Phase 6 work stream: complete the missing paper-trading domain
models identified by `docs/trading/PAPER_TRADING_V1.md` without implementing the
session loop, risk sizing, stop-loss, persistence or live data.

**Implemented.**

- `PaperAccount` (`portfolio/account.py`) — explicit virtual-account state with
  `account_id`, `initial_capital`, `created_at` and an embedded `Portfolio`;
  long-only policy enabled by default for V1.
- `Portfolio.long_only` guard (`portfolio/portfolio.py`) — `apply_fill` rejects
  any `SELL` fill that would create or increase a short position before any
  cash/position state is mutated. Generic `Portfolio` behavior is preserved by
  default (`long_only=False`).
- `Order.transition()` state machine (`models/order.py`) — deterministic lifecycle
  transitions, terminal-state protection, integrity checks for `FILLED`,
  `PARTIALLY_FILLED` and `REJECTED`, plus convenience helpers `submit()`,
  `reject()`, `cancel()`, `mark_filled()`.
- Wired execution paths through `Order.transition()`: `PaperBroker`,
  `BacktestBroker` (`backtest/engine.py`) and `TradingService`
  (`services/trading_service.py`).

**Explicitly NOT implemented.**

- V1 risk-based position sizing (1% equity → quantity, lot rounding, capital
  bound) and 2% stop-loss execution.
- Current-data paper-session loop and completed-candle scheduler.
- Persistence / recovery under `paper_state/`.
- Live/real-money trading.

**Files changed.** `src/fno_ai_paper_trading/portfolio/account.py` (new),
`src/fno_ai_paper_trading/portfolio/portfolio.py`,
`src/fno_ai_paper_trading/portfolio/__init__.py`,
`src/fno_ai_paper_trading/models/order.py`,
`src/fno_ai_paper_trading/broker/paper_broker.py`,
`src/fno_ai_paper_trading/backtest/engine.py`,
`src/fno_ai_paper_trading/services/trading_service.py`,
`tests/test_order_state.py` (new), `tests/test_account.py` (new), plus targeted
updates to `docs/trading/PAPER_TRADING_V1.md`, `docs/architecture/ARCHITECTURE.md`
and this log.

**Verification.** Targeted domain/execution tests: 44 passed. Complete suite:
378 passed (up from 334). `git diff --check` clean. Only intended source/test/doc
files modified; no `.env`/credentials/datasets/reports touched.

**Status.** Committed as `e853667` (`feat: Phase 6 WS 6.2 paper-trading domain/model
completion`) and pushed to `origin/master`; the subsequent sizing work stream is
recorded in §4k.

---

## 4k. 2026-09-09 — Phase 6 WS 6.3: V1 risk-based position sizing

**Scope.** Third Phase 6 work stream: implement the V1 risk-based position sizer
(1% of current equity risked over a 2% stop distance, lot-rounded **down**,
cash/no-leverage bound) as a pure component and wire it into `StrategyService`
without disturbing the fixed-quantity path. Does **not** implement 2% stop-loss
execution, the current-data session loop, persistence or live data.

**Decisions (locked).**

- Pure sizer: `RiskBasedPositionSizer` (`risk/sizer.py`) receives explicit
  decision-time inputs only — `equity`, `available_cash`, `entry_price`,
  `instrument`, `current_quantity` — never a `Portfolio`/`PaperAccount`. It never
  performs accounting; the caller supplies the numbers.
- Formula: `risk_amount = equity * risk_per_trade_pct`;
  `stop_distance = entry_price * stop_loss_pct`;
  `stop_price = entry_price * (1 - stop_loss_pct)`;
  `raw_quantity = risk_amount / (stop_distance * instrument.multiplier)`;
  `quantity = floor(raw_quantity / lot_size) * lot_size` — round **down** only.
- Cash/no-leverage guard: largest whole-lot quantity with
  `qty * entry * mult * (1 + commission_rate) + commission_fixed <= available_cash`;
  skip with a recorded `skip_reason` when below one lot. Uses the existing paper
  commission convention; the sizer only *estimates* the commission for the bound.
- Single-position V1 and BUY-only: `current_quantity != 0` skips ("position
  already open"); a `SELL` never passes through sizing (never creates a short).
- `RiskManager` unchanged and authoritative: `max_position_quantity` /
  `max_order_notional` / `max_daily_loss` still gate the sized order; sizing math
  exists **only** in the sizer. `PaperSettings.paper_*` env wiring remains
  scaffolded-only (no `load_settings()` consumer).
- Backward compat: `sizer=None` leaves the fixed-`quantity` path equivalent; a
  rejected sizing submits no order.

**Implemented.**

- `risk/sizer.py` (new): `SizerConfig` (frozen; validates risk>0, 0<stop<1,
  non-negative commission params), `SizingResult` (frozen: approved, quantity,
  risk_amount, stop_distance, stop_price, skip_reason), `RiskBasedPositionSizer`.
  All money math is `Decimal` (`Decimal(str(value))` for float inputs); invalid /
  non-finite inputs skip with a reason instead of raising.
- `risk/__init__.py`: exports the three new public names.
- `services/strategy_service.py`: optional `sizer` parameter; BUY entries are sized
  before order construction with equity marked to the bar close
  (`_decision_equity`); a rejected sizing appends a `SignalDecision` carrying the
  `SizingResult` and submits no order. SELL path and the no-sizer path are
  unchanged; `SignalDecision` gains a `sizing` field.
- Tests: `tests/test_sizer.py` (new; 21 tests) covering the reference numbers
  (qty 2 @ 100000 / 24000 / 1% / 2% / lot 1 / mult 1), round-down (2.5 → 2, never
  up), below-increment skip, lot-75 rounding, multiplier scaling, current-vs-initial
  equity, cash reduction (40000 → qty 1) and cash skip (10000), invalid
  equity/entry/cash/non-finite inputs, invalid config, single-position skip,
  determinism, and the §13.6 risk-budget property. `tests/test_strategy_service.py`
  gains 4 scenarios: fixed-quantity path unchanged without a sizer, BUY sized /
  SELL fixed (index lot 1), rejected sizing submits no order (future lot 75), and
  `RiskManager` rejecting a sizer-approved sized order (notional cap).

**Explicitly NOT implemented (unchanged).**

- 2% stop-loss execution (worse-of-open-and-stop rule) and stop exits as paper
  orders.
- Current-data 5m paper-session loop / completed-candle scheduler.
- Persistence / recovery under `paper_state/`, session monitoring, live trading.
- `PaperSettings.paper_risk_per_trade_pct` / `paper_stop_loss_pct` are still
  scaffolded-only; the sizer defaults mirror them but no env wiring was added.

**Files changed.** `src/fno_ai_paper_trading/risk/sizer.py` (new),
`src/fno_ai_paper_trading/risk/__init__.py`,
`src/fno_ai_paper_trading/services/strategy_service.py`, `tests/test_sizer.py`
(new), `tests/test_strategy_service.py`, plus targeted updates to
`docs/trading/PAPER_TRADING_V1.md`, `docs/architecture/ARCHITECTURE.md` and this
log. Only risk-based sizing is marked IMPLEMENTED; stop-loss, session loop,
persistence, monitoring and live trading stay PLANNED. No profitability claims
were added.

**Verification.** Targeted sizer + strategy-service tests: 29 passed. Complete
suite: 403 passed (up from 378). `git diff --check` clean. Only intended
source/test/doc files changed; no `.env`/credentials/datasets/reports touched.

**Status.** Uncommitted; awaiting review before Git checkpoint.

---

## 4l. 2026-09-09 — Phase 6 WS 6.4: Automatic 2% stop-loss enforcement

**Scope.** Fourth Phase 6 work stream: the deterministic 2% protective stop-loss
for paper-trading V1 — decision rule plus a single authoritative executor.

**Decisions (locked).**

- The decision rule (`StopLossPolicy`) and the executor (`enforce_stop`) live in
  `risk/stop_loss.py`. The stop is fixed at entry: `stop_price = P_entry ×
  (1 − stop_loss_pct)`; it is evaluated after every completed candle while a
  position is open, signal-first stop-second, and the entry candle is never a
  stop candle.
- `OrderType.STOP` added (`models/enums.py`); the backtest harness carries
  `BacktestConfig.enable_stop_loss` / `stop_loss_pct` — legacy backtests keep
  `enable_stop_loss=False`.
- The stop exit is a real paper `Order`/`Fill`/`Trade`: `enforce_stop` →
  `TradingService.protective_exit` → `PaperBroker` → `Portfolio.apply_fill` (the
  **sole accounting path** — realized P&L, commission and slippage accounted
  exactly as any other fill). Protective stops **deliberately bypass
  `RiskManager`**: the daily-loss cap gates new *entries* only and must never
  prevent a protective exit from closing an open position (documented in
  `docs/trading/PAPER_TRADING_V1.md` §§6–7 and §13 AC-08).
- Deterministic fill pricing: the worse of the candle open and the stop price
  (later asserted by §13 AC-12). `Position.opened_at` is set deterministically at
  the entry timestamp so the entry candle stays excluded.

**Implemented.**

- `risk/stop_loss.py` (new): `StopDecision`, `StopExitResult`, `StopLossPolicy`,
  `enforce_stop`. All money math is `Decimal`.
- `services/trading_service.py`: `protective_exit` — submits the protective
  stop-market exit through the broker and applies the fill to the portfolio,
  bypassing entry-only risk gates.
- `models/enums.py` (`OrderType.STOP`), `backtest/config.py`
  (`enable_stop_loss`/`stop_loss_pct`), `backtest/engine.py` (signal-first,
  stop-second, before the equity snapshot), `portfolio/portfolio.py`
  (deterministic `Position.opened_at`), `risk/__init__.py` exports.
- Tests: `tests/test_stop_loss.py` (new; 41 tests); 2 legacy `test_backtest.py`
  tests scoped via `enable_stop_loss=False`.

**Explicitly NOT implemented (unchanged).** Take-profit / trailing stops /
partial exits / intra-bar execution — V1 is full-close, completed-5m-candle
exits only.

**Files changed.** 9 files, **+775/−4**.

**Verification.** Full suite: **444 passed** (403 committed baseline + 41 new;
2 legacy tests re-scoped, hence net 41). `git diff --check` clean.

**Status.** Committed `75d3a44` (`feat: Phase 6 WS 6.4 automatic 2% stop-loss
enforcement`) and pushed to origin/master.

---

## 4m. 2026-09-09 — Phase 6 WS 6.4b: Deterministic paper session runtime

**Scope.** Companion to WS 6.4: the deterministic current-data paper-session
engine — the runtime that trades completed 5m candles with the sizer, risk
manager, stop-loss and broker through two injection seams.

**Decisions (locked).**

- Single-file orchestrator `services/paper_session.py` owns session lifecycle
  concerns **only**: no sizing/risk/stop arithmetic, never mutates
  cash/positions/P&L itself, never imports `backtest.*`.
- Determinism by injection: the `clock` drives decision time; `PaperBroker(now_fn=…)`
  drives `submitted_at`/`filled_at`; `TradingService.submit_order(…, fill_bar=…)`
  pins the fill price to a completed bar's close. Both seams default to the
  original behavior (verified against the unchanged 444-test baseline).
- Completed-candle policy (`bar.timestamp + interval <= now`), duplicate-candle
  guard (`_consumed`), NSE phase/holiday gating, warm-up gate (default
  `strategy.slow + 1`, 22 for MA(5,21)), long-only BUY/SELL/HOLD mapping,
  daily-loss policy (entries gated, exits executable via `_force_close`),
  signal-first stop-second ordering, entry candle never a stop candle
  (`_entry_candle`), deterministic equity snapshots, `--once`/`--loop`.
- `Environment.PAPER` guard with opt-in sandbox override. No persistence, no env
  parsing, no websocket/streaming.

**Implemented.**

- `services/paper_session.py` (new, 584 lines): `PaperSession`, `SessionStep`,
  `SessionResult`.
- `broker/paper_broker.py`: injectable `now_fn` clock seam (+8/−3).
- `services/trading_service.py`: `fill_bar` fill-price seam (+8/−1).
- `services/__init__.py`: exports `PaperSession`, `SessionResult`, `SessionStep`.
- Tests: `tests/test_paper_session.py` (new; 38 tests).

**Explicitly NOT implemented (unchanged).** Persistence/recovery (next stream,
WS 6.5), session monitoring, live trading, `FNO_PAPER_*` env wiring.

**Files changed.** 5 files, **+1386/−4**.

**Verification.** Full suite: **482 passed** (444 committed baseline + 38 new).
`git diff --check` clean.

**Status.** Committed `b05a17c` (`feat: Phase 6 WS 6.4b deterministic paper
session runtime`) and pushed to origin/master.

---

## 4n. 2026-09-11 — Phase 6 WS 6.5: State persistence / recovery

**Scope.** Fifth Phase 6 work stream: deterministic snapshot/checkpoint + restore
so a running paper session survives a restart. Mirrors the established
`AbstractDatasetStore`-style patterns from `data/dataset_store.py`. Respects all
WS 6.5 invariants: `Portfolio.apply_fill` remains the sole accounting path,
Decimal money math, no `backtest.*` coupling, `paper_state/` git-ignored, no
`FNO_PAPER_*` env wiring.

**Decisions (locked).**

- Persistence owns all serialization in
  `src/fno_ai_paper_trading/persistence/session_store.py`; the session layer
  stays thin (assembles/validates a `SessionSnapshot`, no disk I/O, no math).
- File layout: payload `<safe_name>.json` + sidecar `<safe_name>.meta.json`
  (schema, save time, `state_hash`) under `paper_state/`;
  `state_hash` = SHA-256 over canonical JSON
  (`json.dumps(indent=2, sort_keys=True) + "\n"`); `SCHEMA_VERSION = "1"`.
  Load refuses unsupported schema, missing sidecar, hash/corruption mismatch.
- Restore contract: session not running, snapshot instrument `symbol` +
  `exchange_token` + `interval_token` + `warmup_bars` match, empty broker;
  rebuilds `TradingService`, restores accounting state +
  `_consumed`/`_entry_candle`/counters, forces `_running=False`. No re-execution.
- `Portfolio.initial_cash` assigned after construction (`__post_init__` sets it to
  `cash`); live `Position.realized_pnl` may be negative so it is assigned after
  construction in the serializers. Only non-flat positions are persisted.
- Money is `Decimal` (serialized via `str()`); datetimes are naive ISO (IST);
  `_money`/`_int` coercion helpers raise `ValueError` instead of silently coercing.

**Implemented (committed `118e976`).**

- `persistence/session_store.py` (new): `SessionSnapshot` (frozen),
  `StoredSession`, `save_session`/`load_session`, model serializers.
- `persistence/__init__.py` (new): public exports.
- `broker/paper_broker.py`: `snapshot()` / `restore()` (restore requires an
  empty broker → `RuntimeError`); `replace` import added.
- `services/paper_session.py`: `snapshot()` / `restore()` +
  `_validate_restore_target`, `_portfolio_from_live`, `_portfolio_from_snapshot`;
  docstring updated to "WS 6.4b / WS 6.5".
- `.gitignore`: `paper_state/`.
- `tests/test_session_persistence.py` (new, 34 tests): file-layer save/load,
  hash-tamper/corruption/schema guards, broker snapshot/restore, session round
  trips (Decimal exactness, `initial_cash` preserved, consumed-timestamps block
  reprocessing), restart equivalence (mid-run snapshot → restore → resume ==
  uninterrupted run), stop-fires-after-restore, no-`backtest.*`-import guard.

**Verification.** Full suite: **516 passed in 2.99s** (482 committed baseline +
34 new). No lint/typecheck tooling in the repo; pytest is the gate.

**Status.** Committed as `118e976` (`feat: Phase 6 WS 6.5 persistent paper session
state`) and pushed to `origin/master`.

---

## 4o. 2026-09-11 — Phase 6 WS 6.7: Offline acceptance replay tests

**Scope.** Seventh Phase 6 work stream: deterministic, session-level replay of all
30 V1 acceptance criteria (`docs/trading/PAPER_TRADING_V1.md` §13, criteria 1–30)
through the real `PaperSession` runtime, offline, with no network, no credentials
and no sleep. Every criterion gets exactly one pytest method named `test_ac_XX_*`.

**Decisions (locked).**

- Single new file (`tests/test_acceptance_replay.py`), no production code changed.
  File is fully self-contained: duplicates the small helpers pattern from
  `test_paper_session.py` (no cross-test-module imports).
- 7 test classes mirror §13's own section headings: risk sizing (1–6), stop-loss
  (7–12), long-only (13–14), cadence (15–18), capital/accounting (19–21),
  determinism (22–23), safety (24–30).
- Default clock seam: `lambda: FILL_CLOCK` (= `datetime(2026,9,2,10,0)`), injected
  into both session and broker so `filled_at` is deterministic.
- Warm-up default 1 for scripted strategy (no `slow` attribute → fallback to 1 per
  `paper_session.py` line 193). AC-17 tests the 22-bar boundary explicitly.
- AC-08 (protective stop via `RiskManager`) asserts the criterion's *intent*
  (exit still produces a paper fill/trade through `PaperBroker` →
  `Portfolio.apply_fill`) rather than the literal pre-WS-6.4 wording; documented
  deviation (§6 / test docstring).
- AC-24 `_DummyBroker` satisfies the `Broker` ABC (3 abstract methods: `place_order`,
  `cancel_order`, `get_order`) — identical to the existing `_DummyBroker` in
  `test_paper_session.py`.

**Implemented (uncommitted, pending commit approval).**

- `tests/test_acceptance_replay.py` (new, ~775 lines, 30 tests):
  - Helpers: `_ts`, `_index`, `_bar`, `_bars`, `_provider`, `_settings`,
    `_ScriptedStrategy`, `_strategy`, `_make_session`, `_buying_steps`,
    `_all_recorded_money`.
  - 30 named methods (`test_ac_01_*` … `test_ac_30_*`) grouped into 7 classes.
  - Notable precision: AC-02 equity-basis sizing verified across a round-trip;
    AC-04 cash-cap sizer reduction; AC-09 slippage/commission math to 9 decimal
    places (entry `24024`, exit `23519.97648`, commission `14.111985888`);
    AC-12 intrabar vs gap fill paths; AC-20 cash `100171.14` and realized `200`;
    AC-22 determinism tuple with `(4,4,4,0,0)` counters; AC-29 max-quantity +
    seeded daily-loss gate; AC-30 save/load/restore into `tmp_path` with
    `.gitignore` check.

**Bugs fixed during the run (before first green):**

1. `_buying_steps` filter captured SELL fills → fixed to `Signal.BUY` only (AC-02).
2. `realized_pnl` assertion `-7800` was wrong → corrected to `-8000` (AC-02).
3. Dead unreachable assertion line removed (AC-16).
4. `run("a") == run("b")` always failed due to differing tag strings → fixed to
   compare `[1:]` slices (AC-22).

**Verification.** Acceptance suite: **30 passed in 0.33s**. Full suite: **546 passed
in 2.50s** (516 committed baseline + 30 new). No lint/typecheck tooling in the
repo; pytest is the gate.

**Status.** Committed as `0e5ce1c` (`feat: Phase 6 WS 6.7 acceptance replay tests`)
and pushed to `origin/master`.

## 4p. 2026-09-11 — Phase 6 WS 6.1: Documentation / plan alignment

**Scope.** Refresh the V1 contract, project plan and architecture docs so they
match the Phase 6 implementation delivered by WS 6.2–6.7, and keep
cross-references valid. No production or test code changed.

**Changes.**
- `docs/trading/PAPER_TRADING_V1.md`: header → **CONTRACT** (Phase 6 implemented);
  §1 persistence row; §2.3 config-consumption wording; §2.4 capabilities table
  (session loop / stop-loss / long-only gating / persistence → **IMPLEMENTED**;
  env wiring + streaming quotes remain NOT IMPLEMENTED); §4 flow + notice; §5
  step statuses; §7 stop-loss; §8 capital row + correctional caveat; §10
  session/state requirements; §11 safety guards; §12 persistence out-of-scope
  row; §13 header + criteria 29/30; §14 limitations/future extensions; footer
  commit reference.
- `PROJECT_PLAN.md`: §17c (persistence contract row, DECLARED CONFIG section +
  reduced PLANNED list, static-caps vs sizer boundary, V1-capital clarification),
  §17d roadmap annotated with per-work-stream status + commit SHAs (6.2–6.7
  **DONE**, 6.1 **IN PROGRESS**, 6.6 **PLANNED**), Current Phase section, and a
  new 2026-09-11 Change Log entry.
- `docs/architecture/ARCHITECTURE.md`: header/footer commit refs (`→ 3166aee`,
  546 tests), §1 out-of-scope persistence, §10 risk-gate note, §11 config wiring
  note, §12 market-hours purpose, §13 session-runtime block, §16 persistence
  state note, §17 status-table rows (config PARTIAL; stop-loss / session loop /
  persistence / acceptance replay → **IMPLEMENTED**), §18 limitations 1/2/3/6/10,
  §19 future 1/5.
- `README.md`: architecture tree gains `risk/sizer.py`, `risk/stop_loss.py`,
  `services/paper_session.py`, `persistence/`; safety-limitations bullet mentions
  `PaperSession`; Future-phases table gains a Phase 6 row.
- `PROGRESS.md`: stale HEAD SHA `aedd0bc` → `3166aee`; WS 6.1 row status.

**Verification.** Full suite: **546 passed** (unchanged; docs-only change).

**Status.** Committed as `248c895` (`docs: WS 6.1 align Phase 6 documentation
(spec + plan + architecture)`) and pushed to `origin/master`.

---

## 4q. 2026-09-11 — Phase 6 WS 6.6: Session operations / monitoring

**Scope.** Operator-facing observability for the running `PaperSession`:
logging, health checks, offline/online reporting and an operator CLI. No change
to session runtime behavior, accounting, risk or persistence. Scheduled runs
stayed operator-level (Task Scheduler / cron wrapping `run_loop` + CLI).

**Changes.**
- `services/session_monitoring.py` (new):
  - `SessionHealth` + `health(session, when=None)` — live counters, environment,
    warm-up flag, cash, mark-to-market equity (provider last price; entry-price
    fallback), realized/realized-today, open-quantity posture.
  - `SessionReport` + `build_report(session, results=None, when, mark_prices)` +
    `report_from_snapshot(snapshot, mark_prices, when)` — summary, session
    counters, win/loss + Decimal win-rate, ledger (`SessionLedgerRow`: per
    consumed step with skip/order/stop/error actions, or per recorded fill),
    equity curve (`EquityPoint`); snapshot path marks open positions at entry
    (cost basis) unless mark prices are supplied.
  - `log_results` / `log_health` — deterministic operator logging on the
    `session.operations` logger; never logs secrets or environment values.
  - `report_to_dict` (plain/JSON-serialisable), `report_to_html` (self-contained
    labelled page reusing `research/report.py` CSS / card / table / kv_rows),
    `write_html_report(path)` — the module's only disk-touching helper.
- `scripts/paper_session_report.py` (new): `load_session` on a `paper_state/`
  payload → text summary on stdout → optional `--html` / `--json` report files.
- `tests/test_session_monitoring.py` (new, 15 tests): health (fresh/open/
  realized@stopped), live report with results (ledger + equity curve lengths,
  win-rate), fill-based ledger, loss trade, dict/HTML rendering, UTF-8 HTML
  write, snapshot-vs-live equivalence, offline entry-mark fallback, explicit
  `mark_prices`, caplog logging assertions, offline/purity guard.
- `docs/architecture/architecture.svg` (new): hand-authored layered SVG diagram
  (data → strategy → risk → broker → portfolio → `PaperSession` → persistence +
  monitoring → CLI → tests), WS 6.6 highlighted.

**Docs refreshed.** `ARCHITECTURE.md` (§1 out-of-scope dashboard note, §4 diagram
reference + services/persistence bullets, §5 repository tree incl. `architecture.svg`,
§16 session-monitoring flow + state notes, §17 status rows + 561-test count, §18.1
scheduled-run wording, §19.1 → delivered), `PROJECT_PLAN.md` (§17b/§17c/§17d WS 6.6 →
DONE, Current Phase, new Change Log entry), `docs/trading/PAPER_TRADING_V1.md` (§14
future-extension bullet), this log and `PROGRESS.md`.

**Verification.** Full suite: **561 passed in 2.82s** (546 baseline + 15 new).

**Status.** Committed as `00ed8ed` (`feat: WS 6.6 session monitoring - health
checks, reports, operator logging, HTML output`) and `c4570ba` (`docs: WS 6.6
session monitoring + architecture SVG (561 tests)`), pushed to `origin/master`.

---

## 4r. 2026-09-11 — Phase 7 roadmap: strategy evaluation & regime-aware discipline (docs only)

**Objective.** Position Phase 7 as evaluation-first: keep MA(5,21) frozen as the V1
baseline, measure it across multiple validated NIFTY 50 sessions, analyse
market-regime behavior, and only then investigate regime-aware/AI candidates —
validated out-of-sample and compared against the baseline before adoption.
Documentation-only; no `src/` or `tests/` changes.

**Design decisions (recorded before editing).**

1. **Freeze the baseline.** MA(5,21) is declared the frozen V1 baseline. A single
   losing session — including the 10-Sep-2026 real-data paper replay loss
   (~₹194.68) — must never trigger parameter changes or strategy replacement.
2. **Evaluation discipline as a contract (§17e).** Added `PROJECT_PLAN.md` §17e:
   historical multi-session evaluation, baseline metrics (P&L, return %, win rate,
   round trips, avg trade, transaction costs, max drawdown + %, exposure, losing
   streaks), regime analysis (trending / sideways-choppy / high-vol / low-vol),
   regime-aware hypotheses (explicitly NOT implemented rules), advisory AI decision
   support (action / confidence / rationale / regime / model+version / timestamp)
   with a hard safety boundary (never executes, never bypasses risk gate, sizing,
   stop-loss, `PaperBroker`, `Portfolio`, never auto-enables live trading),
   baseline-vs-enhanced comparison on the same data/assumptions, out-of-sample
   validation, and an adoption rule. Includes the design pipeline
   `Validated Market Data → … → Out-of-Sample Validation` (deterministic risk gate
   remains authoritative).
3. **Roadmap reordering (§17d).** Phase 7 in `PROJECT_PLAN.md` §17d rewritten as an
   evaluation-first sequence placed BEFORE any tuning or replacement of the
   baseline. Everything is labelled **PLANNED** — nothing is claimed as implemented.
4. **Consistency across docs.** README.md (future-phases table, safety limitations,
   Phase refs), `docs/architecture/ARCHITECTURE.md` (scope bullet, status table,
   limitations, future evolution) and `docs/trading/PAPER_TRADING_V1.md`
   (out-of-scope row, future extensions, footer HEAD `02c18f3`) reconciled to the
   same framing; `PROGRESS.md` header + new §14 entry updated (563 committed tests).
5. **Observation-only loss reference.** The 10-Sep-2026 ~₹194.68 replay loss is
   cited exactly once per document as an evaluation observation motivating
   multi-session evaluation — the roadmap is not overfit to that session.

**Files changed (documentation only).** `README.md`, `PROJECT_PLAN.md`,
`PROGRESS.md`, `ACTIVITY_LOG.md` (this entry), `docs/architecture/ARCHITECTURE.md`,
`docs/trading/PAPER_TRADING_V1.md`.

**Verification.** Full committed suite: **563 passed** (0 skipped / 0 xfailed).
No source or test files changed by this workstream.

**Status.** Documentation-only, committed as
`docs: strengthen strategy evaluation and regime-aware roadmap`, pushed to
`origin/master`.

---

## 4s. 2026-09-11 — Phase 7+ roadmap reconciliation: continuous adaptive paper-trading platform (docs only)

**Objective.** Consolidate the long-term direction — an "AI-Enabled F&O Market
Decision Support & Continuous Adaptive Paper-Trading Platform" — into a single
authoritative documentation-only roadmap (`PROJECT_PLAN.md` §17d–§17l).
Documentation changes only; no `src/` or `tests/` changes.

**Design decisions (recorded before editing).**

1. **Single comprehensive commit.** This workstream (planned commit `docs: define
   continuous adaptive paper trading roadmap`) absorbs the earlier uncommitted
   adaptive-learning requirements into one reconciled roadmap (§17f–§17l) rather
   than a separate commit.
2. **Roadmap numbering §17d → 7.1–7.14.** AI decision-support foundation; feature
   engineering; market regime detection; historical strategy evaluation; five-year
   historical replay/evaluation; regime-aware strategy evaluation; GUI/monitoring
   dashboard; continuous paper-trading agent; experience/trade-outcome store;
   adaptive learning & candidate generation; champion vs challenger; controlled
   promotion & rollback; continuous feedback loop; alerting & operational
   hardening. All **PLANNED**.
3. **Continuous adaptive learning (§17f).** Product vision (V1 deterministic →
   historical evaluation → AI decision support → regime awareness → continuous
   agent → experience/outcome learning → adaptive learning → champion/challenger →
   controlled promotion/rollback → continuous feedback loop); the learning
   principle "every completed paper trade contributes evidence…; adaptation only
   after sufficient evidence, controlled evaluation and validation"; loss analysis
   (learn WHY, never reactively change the algorithm); 9 learning-architecture
   components (experience store, outcome analyzer, regime analyzer, learning layer,
   evaluation engine, champion/challenger, promotion gate, version registry,
   rollback); experience-store fields; a hard safety boundary (must never bypass
   risk/sizing/stop-loss/broker/portfolio accounting, never enable live trading);
   champion = MA(5,21) frozen, challenger gated by historical + out-of-sample
   evidence; promotion gate + version registry + rollback.
4. **§17g Five-Year Historical Evaluation.** ~5 years of validated NIFTY 50
   intraday data replayed deterministically; train/validation/out-of-sample and
   walk-forward; no look-ahead, no leakage; explicitly NOT yet completed.
5. **§17h Continuous agent, §17i GUI, §17j alerts, §17k watchdog, §17l principles.**
   Agent has MARKET CLOSED/OPEN safe states (live data never = live execution);
   GUI is read-only-first and always labeled "PAPER TRADING — NO LIVE ORDERS";
   alert engine is pluggable with paper-tagged alerts; watchdog fails safely
   (HOLD/STOP, never guess); 15 non-negotiable product principles documented.
6. **Reconciliation across docs.** README.md now explicitly separates what exists
   today from what is planned next (long-term direction note + Phase 7/7+ rows);
   `ARCHITECTURE.md` gains the PLANNED complete-target-architecture pipeline
   (Market Data → … → Feedback Loop) plus supporting services (GUI, Alert Engine,
   Agent Scheduler, Watchdog); `PAPER_TRADING_V1.md` future extensions extended;
   `PROGRESS.md` header checkpoint updated and new §15 entry added. Nothing
   planned is claimed as implemented, and no roadmap is overfit to the
   10-Sep-2026 observation (~₹194.68 — MA(5,21) stays the frozen baseline).

**Files changed (documentation only).** `README.md`, `PROJECT_PLAN.md`,
`PROGRESS.md`, `ACTIVITY_LOG.md` (this entry), `docs/architecture/ARCHITECTURE.md`,
`docs/trading/PAPER_TRADING_V1.md`.

**Verification.** Documentation-only diff; full committed suite **563 passed**
(0 skipped / 0 xfailed; 567 total on disk including the pre-existing untracked
AI-contract tests). No source or test files changed.

**Status.** Committed as `docs: define continuous adaptive paper trading roadmap`
and pushed to `origin/master`.

---

## 4t. 2026-09-11 — WS 7.1 AI decision-support foundation

**Objective.** Land the advisory AI decision-support contract layer (offline glue found uncommitted
in the repo, reconciled and committed).

**Decisions.**
1. `ai/` package = `DecisionSupport` ABC + `HoldDecisionSupport` deterministic baseline + frozen
   `AIDecision`/`DecisionContext` contracts; Decimal-only features, no floats, immutable mappings.
2. Advisory only, zero coupling to broker/risk/portfolio/session (AST-verified by test).

**Files.** `src/fno_ai_paper_trading/ai/{__init__,base,decision}.py`, `tests/test_ai_decision_support.py`.

**Status.** Committed `cfba9b6`, pushed.

## 4u. 2026-09-11 — WS 7.2 Feature engineering

**Objective.** Deterministic decision-time feature engineering consumable by the AI boundary.

**Decisions.**
1. `features/` package: `FeatureEngineer` (stateless; fast=5/slow=21 matching MA(5,21) warm-up).
2. Indicators in `features/indicators.py`: `sma`, `rsi`, `close_return`, `mean_squared_return`,
   `volatility_ratio` — all Decimal, all `None` on insufficient data.
3. No-look-ahead contract: `compute_prefix(bars, i)` feeds only `bars[:i+1]`; tests assert prefix
   features equal full-prefix features.

**Files.** `src/fno_ai_paper_trading/features/{__init__,base,indicators}.py`, `tests/test_features.py`.

**Verification.** Full suite **584 passed** (567 + 17 new).

**Status.** Committed as `feat: WS 7.2 deterministic feature engineering`, pushed.

---

## 4v. 2026-09-11 — WS 7.3 Market regime detection

**Objective.** Deterministic, decision-time regime classification over validated bars.

**Decisions.**
1. `regime/` package: `RegimeDetector` (stateless) → `TrendState` (UP/DOWN/SIDEWAYS via
   `ma_gap_pct`) and `VolatilityState` (LOW/NORMAL/HIGH via short/long variance ratio);
   configurable thresholds, safe defaults.
2. Built on WS 7.2 features; `detect_prefix(bars, i)` never uses bars after *i*.
3. Descriptive only — no ordering/risk actions inside the package.

**Files.** `src/fno_ai_paper_trading/regime/{__init__,detector}.py`, `tests/test_regime.py`.

**Verification.** Full suite **597 passed** (584 + 13 new).

**Status.** Committed as `feat: WS 7.3 deterministic market regime detection`, pushed.

---

## 4w. 2026-09-11 — WS 7.4 Historical strategy evaluation

**Objective.** Deterministic multi-session evaluation of the frozen baseline (or any strategy)
over validated datasets, with the standardized metric set.

**Decisions.**
1. New `evaluation/` package on top of the existing backtest + research experiment machinery
   (`run_dataset_experiment`, `compute_metrics`) — no re-implementation of replay.
2. Records: `EvaluationConfig`, `SessionEvaluation`, `EvaluationAggregate`, `EvaluationRun`;
   aggregate chains per-session equity curves into one composite (per-session increments onto
   a running total with shared `initial_capital`).
3. Standardized metrics derived from `BacktestResult`: P&L, return %, win rate, round trips,
   avg trade, transaction costs, max drawdown (+%), exposure, losing streak, profit factor.
4. `scripts/evaluate_historical.py` CLI (offline, read-only) writes JSON + HTML reports.
5. Reuses `research.report` CSS/helpers for a labelled "FROZEN BASELINE" report page.

**Files.** `src/fno_ai_paper_trading/evaluation/{__init__,records,historical,report}.py`,
`tests/test_evaluation.py`, `scripts/evaluate_historical.py`.

**Verification.** Full suite **606 passed** (597 + 9 new); CLI smoke run over 3 datasets,
370 bars, 4 round trips, net P&L −₹125.46 (evidence only).

**Status.** Committed as `feat: WS 7.4 standardized historical strategy evaluation`, pushed.

---

## 4x. 2026-09-11 — WS 7.5 Five-year historical replay capability

**Objective.** Deterministic, resumable, day-by-day replay over a configurable
multi-year date range with honest progress accounting and train/validation/OOS
split discipline.

**Decisions.**
1. `DayBars` wraps per-day chronologically-ordered bars with a `source_hash`
   identity; per-day validation errors are counted and skipped without aborting.
2. `ProgressStore` keys by `date|source_hash` so different sources for the same
   date are tracked independently. Interrupted runs resume cleanly via JSON
   checkpoint.
3. `PeriodSplitConfig` assigns contiguous training / validation / out-of-sample
   labels before any evaluation occurs.
4. `FiveYearReport.status` is `COMPLETE` only when every provided day was
   processed and no days were skipped as invalid — never claims five years
   complete without evidence.
5. `scripts/evaluate_five_year.py` chunks validated dataset CSVs by trading day,
   replays frozen MA(5,21), and writes JSON + HTML + resumable progress.
6. Uses the WS 7.4 `HistoricalEvaluator.replay_bars()` → `build_run()` API
   cleanly, avoiding double-replay and preserving composite curve correctness.

**Files.** `src/fno_ai_paper_trading/evaluation/five_year.py`,
`scripts/evaluate_five_year.py`, `tests/test_five_year.py`.

**Verification.** Full suite **626 passed** (606 + 20 new); CLI smoke run over
3 datasets: 296 trading days processed, 2 round trips, net P&L Rs 0.00
(evidence only).

**Status.** Committed as `feat: WS 7.5 five-year historical replay capability`, pushed.

---

## 4y. 2026-09-11 — WS 7.9 Durable experience store

**Objective.** Durable, evidence-only persistence for the adaptive-learning
loop: decision-time context, realized paper-trade outcomes, and AI-advisory
metadata — stored idempotently and recoverable across restarts, with no
execution capability (no orders, no broker, no risk bypass).

**Decisions.**
1. Domain (`experience/`) is fully decoupled from storage
   (`persistence/experience_store.py`), so a future backend can replace the
   JSONL store without changing the evidence contract.
2. IDs are deterministic SHA-256 digests of the full decision-time payload
   (`decision_identity`), so re-appending identical evidence is a no-op; the
   single legal in-place transition is `pending_outcome` → `complete`
   (outcome finalized later), enforced by the store.
3. No-look-ahead is enforced by construction: `DecisionContext` contains only
   decision-time data, `decision_payload()` has no outcome/exit/result fields,
   and `AdvisoryEvidence` hard-wires `advisory_only=True` with no order/trade
   reference.
4. `ExperienceRecord.status` is derived: `complete` / `pending_outcome` /
   `no_trade`. Records are immutable; a corrected outcome is a new record.
5. Storage is an append-only JSONL log + `.meta.json` (schema version, count,
   timestamps). `load()` is strict: missing metadata, corrupt lines, duplicate
   ids, and unsupported schema versions raise instead of dropping evidence.
6. Serialization is deterministic and type-preserving (features keep their
   `Decimal`/`int`/`str` types via type tags); money/timestamps are canonical
   strings, mirroring the WS 6.5 session-store convention.

**Files.** `src/fno_ai_paper_trading/experience/` (records, enums, classification,
queries, builders), `src/fno_ai_paper_trading/persistence/experience_store.py`,
`src/fno_ai_paper_trading/persistence/__init__.py`, `tests/test_experience_store.py`.

**Verification.** Full suite **680 passed** (626 + 54 new): idempotent
append/merge, pending→complete upgrade, strict recovery (corrupt/missing/duplicate
metadata), type-preserving round-trip, query/find/count_by filters, and
no-execution/no-look-ahead guarantees.

**Status.** Committed as `feat: add durable experience store`, pushed.

---

## 4z. 2026-09-11 — WS 7.10 Adaptive learning & candidate generation

**Objective.** "Learn by outcome, do not react" (§17f.2/§17f.4/§17f.7): read the
experience store, produce a deterministic outcome summary, and emit improvement
*hypotheses* only after hard evidence thresholds — where a single loss (or win)
can never change the algorithm.

**Decisions.**
1. `learning/outcome.py` computes the aggregate from **complete** records only;
   `pending_outcome`/`no_trade` records are counted but excluded. Metrics:
   net P&L, gross win/loss, win rate, expectancy, avg win/loss, profit factor,
   per-regime / per-signal / per-advisory-usage breakdowns.
2. `learning/candidates.py` gates generation on `min_completed` (=20), per-regime
   minimums (=10), and a `win_rate_delta` edge (=0.05). Emitted candidates are
   inert data: `regime_focus` (a regime out/underperforms the overall win rate)
   and `advisory_alignment` (accepted vs rejected/overridden advisory split).
   Insufficient-evidence groups are reported, not silently dropped.
3. Everything is immutable and deterministic (`now` injectable for tests).
4. Hard boundary: the `learning` package has no import of strategy / risk /
   sizing / stop-loss / broker / portfolio / paper-session code (AST test), so
   candidates cannot be applied in-process; WS 7.11/7.12 own evaluation.

**Files.** `src/fno_ai_paper_trading/learning/` (outcome.py, candidates.py,
`__init__.py`), `tests/test_learning.py`.

**Verification.** Full suite **700 passed** (680 + 20 new); threshold, breakdown,
single-loss/single-win, no-look-ahead and import-boundary tests all green.

**Status.** Committed as `feat: WS 7.10 adaptive learning and candidate generation`, pushed.

---

## 4aa. 2026-09-12 — WS 7.11 Champion vs challenger evaluation

**Objective.** Evaluate challenger candidates against the frozen MA(5,21)
champion on *shared* data with *identical* cost/execution assumptions so any
difference is attributable to signal selection alone — evidence only, no
promotion (gating is WS 7.12).

**Decisions.**
1. New challenger `RegimeFilteredMovingAverageCross` (`strategies/regime_filtered.py`):
   wraps MA(5,21) and suppresses BUY entries when the regime trend at the
   decision bar is not in `allowed_trends` (default `("UP",)`). SELL/HOLD pass
   through untouched, so the challenger can only stay flat longer — it never
   widens an open risk state. Regime comes from `RegimeDetector.detect_prefix`,
   the same no-look-ahead feature prefix used by the AI boundary; the baseline is
   never modified.
2. `evaluation/champion_challenger.py`: `ChampionChallenger` iterates the same
   `HistoricalEvaluator` over champion + each challenger (`run`/`run_bars`/
   `run_days`). `run_days` reuses the five-year `split_period`, producing an
   overall `ComparisonReport` plus per-period reports (training / validation /
   out-of-sample), with labels computed up front from day counts.
3. `ChallengerDelta` records raw deltas (net P&L, win rate, max drawdown %,
   profit factor) + a single `beats_champion` boolean — explicitly only a
   headline summary, marked `evidence_only=True`. No adoption/rollback logic
   exists in this workstream.
4. Serialization (`comparison_report_to_dict` / `multi_period_comparison_to_dict`)
   and HTML (labelled "evidence, not promotion") follow the WS 7.4/7.5 report
   conventions. CLI: `scripts/evaluate_champion_challenger.py`.

**Files.** `src/fno_ai_paper_trading/strategies/regime_filtered.py`,
`src/fno_ai_paper_trading/evaluation/champion_challenger.py`,
`tests/test_champion_challenger.py`, `scripts/evaluate_champion_challenger.py`,
`strategies/__init__.py`, `evaluation/__init__.py`.

**Verification.** Full suite **724 passed** (700 + 24 new). Strategy tests cover
UP-allowed BUY, SIDEWAYS up-cross suppression, SELL passthrough, prefix-only
regime equivalence vs `detect_prefix`, and determinism; comparison tests cover
delta correctness, period isolation, twin-with-no-filter == champion (zero
delta), and serialization/HTML smoke.

**Status.** Committed as `feat: WS 7.11 champion vs challenger evaluation`, pushed.

---

## 4ab. 2026-09-12 — WS 7.12 Controlled model promotion & rollback

**Objective.** Let a challenger become the champion only through a promotion
gate over robust out-of-sample evidence that preserves risk constraints, tracked
in a model/strategy version registry with rollback (§17f.9) — all purely a
decision over evidence.

**Decisions.**
1. `promotion/gate.py`: pure, deterministic gate over `DeltaView` (a plain,
   JSON-friendly slice of a WS 7.11 delta). `PromotionCriteria` defaults:
   `oos_min_days=3`, `require_validation_not_worse=True`,
   `require_positive_oos_pnl=False`. To promote, the challenger must beat the
   champion out-of-sample (net P&L >= AND maxDD % <=), have enough OOS days, and
   not be worse on validation. Verdicts include every reason + evidence snapshot;
   rejection leaves the registry untouched. Adapters support WS 7.11
   `ChallengerDelta` objects, `MultiPeriodComparison` objects, and serialized
   report JSON.
2. `promotion/registry.py`: append-only JSONL `VersionRegistry`
   (`start`/`promote`/`rollback`/`rollback_to`), replayed into state on load;
   exactly one ACTIVE champion, previous versions RETIRED / ROLLED_BACK; corrupt
   or unknown log lines raise. Default `model_registry/` (gitignored).
3. Safety boundary: `promotion/` imports no broker / portfolio / risk / service
   / backtest code — promotion is bookkeeping over evidence; it can never enable
   live trading or weaken risk controls; MA(5,21) remains champion until a
   candidate clears the gate.
4. CLI `scripts/manage_model_versions.py`: `list`, `start`, `promote`
   (gate-checked; rejected candidates are not written), `rollback [--to]`.

**Files.** `src/fno_ai_paper_trading/promotion/` (gate.py, registry.py,
`__init__.py`), `tests/test_promotion.py`, `scripts/manage_model_versions.py`,
`.gitignore` (+`model_registry/`).

**Verification.** Full suite **751 passed** (724 + 27 new); registry persistence
round-trips, corrupt-log rejection, gate accept/reject matrices, DeltaView
adapters, and an end-to-end gate over a real `MultiPeriodComparison` all green.

**Status.** Committed as `feat: WS 7.12 controlled promotion and rollback`, pushed.

---

## 4ac. 2026-09-12 — WS 7.13 Continuous feedback / learning loop

**Objective.** Turn the §17f.3 loop into a repeatable, evidence-only program:
replay the champion (Paper Trading), capture completed trades as experience,
analyze/generate candidate hypotheses, run champion vs challenger, gate any
promotion, and let the promoted model lead the next cycle (Repeat). Remains
paper-only — no orders, no risk changes, no live trading.

**Decisions.**
1. `learning/capture.py`: the missing wiring between the deterministic replay
   machinery (WS 7.4/7.5/7.11) and the durable experience store (WS 7.9).
   Champion replay per day-batch uses the same `HistoricalEvaluator`, round
   trips are paired FIFO per instrument via `pair_round_trips`, and only
   *closed* trades become `ExperienceRecord`s (open positions counted, never
   recorded). Decision-time regime comes from `RegimeDetector.detect_prefix`
   at the entry bar — no look-ahead. Deterministic IDs make store merges
   idempotent across re-runs.
2. `learning/loop.py`: `LearningLoop.run_cycle` composes the existing stages
   (comparison -> capture -> store -> hypotheses -> gate -> registry.promote)
   with zero execution imports (AST-verified). `resolve_active_champion` +
   `default_strategy_factories` implement the repeat leg from the WS 7.12
   registry; a rejected candidate never touches the registry.
3. CLI `scripts/run_learning_loop.py`: chunks days into cycles, auto-starts the
   registry baseline when empty, resolves the next champion after each cycle,
   and writes per-cycle JSON + HTML artifacts.
4. Safety boundary: the loop is bookkeeping over evidence; a promotion is only
   a registry record consumed by later cycles as a reference. MA(5,21) stays
   the champion until a candidate clears the gate on real data.

**Files.** `src/fno_ai_paper_trading/learning/capture.py`, `learning/loop.py`,
`learning/__init__.py` (+exports), `tests/test_learning_loop.py`,
`scripts/run_learning_loop.py`.

**Verification.** Full suite **769 passed** (751 + 18 new); capture
determinism/idempotence, open-trade exclusion, promotion/rejection cycles, the
repeat leg, serialization and the execution-import boundary all green.

**Status.** Committed as `feat: WS 7.13 continuous feedback learning loop`, pushed.

---

## 4ad. 2026-09-12 — WS 7.14 Alerting and operational hardening

**Objective.** Implement the pluggable alert engine and watchdog/health/
fail-safe layer (§17j, §17k): trading / AI-learning / risk / system alerts —
every trading alert carrying the **PAPER TRADING — NO LIVE ORDER** marker —
and a watchdog that fails safely (HOLD/STOP rather than guessing). The whole
layer is advisory data and delivery; it never executes.

**Decisions.**
1. `alerting/alerts.py`: `AlertCategory`, `AlertLevel`, frozen `Alert` with
   `to_dict`/`from_dict`, `PAPER_TRADING_LABEL` constant, and `trading_alert`
   builder whose environment field is unforgetably the paper label.
2. `alerting/engine.py`: pluggable `AlertSink` — `FileAlertSink` (JSONL),
   `CollectingAlertSink`, `ChainedAlertSink` — with `AlertEngine` that stamps
   the environment marker on every emitted alert (even if a caller omits it),
   keeps a bounded history, and isolates sink failures. No notification
   integration ships; all sinks are local so nothing can reach external
   services accidentally.
3. `alerting/health.py`: deterministic `Watchdog` — `is_stale`,
   `bar_sequence_is_valid` for duplicate/out-of-order candles, per-component
   `HealthReport`, and `TradingSafety` (SAFE / WATCH / STOP with the instruction
   to HOLD/STOP paper execution rather than guess). `alerts_for` maps the
   report into paper-labelled SYSTEM/RISK alerts. A STOP is data for operators;
   the watchdog never places orders or changes risk controls.
4. `scripts/run_watchdog.py`: health report over dataset sequences, experience
   store and model registry; JSON + HTML artifacts; optional `--as-of` /
   `--max-bar-age-hours` wall-clock staleness bound.
5. Safety boundary: alerting imports no broker / portfolio / risk / service /
   execution code (AST-verified); delivery and fail-safe decisions are pure
   bookkeeping.

**Files.** `src/fno_ai_paper_trading/alerting/` (alerts.py, engine.py, health.py,
`__init__.py`), `tests/test_alerting.py`, `scripts/run_watchdog.py`.

**Verification.** Full suite **788 passed** (769 + 19 new); alert serialization,
engine dispatch/history/file sink, watchdog SAFE/STOP/WATCH paths, bar-sequence
validation, and the execution-import boundary all green.

**Status.** Committed as `feat: WS 7.14 alerting and operational hardening`, pushed.

---

## 4ae. 2026-09-12 — Phase-7 close-out: 20-item engineering report

**Objective.** Produce the final engineering deliverable for human review once
WS 7.14 landed (`39b7791`).

**Decisions.** A 20-item report (`docs/engineering_report.md`) grounded in the
implemented evidence rather than intent: paper-only boundary + AST import-boundary
proofs; frozen MA(5,21) baseline; deterministic risk/execution gates; Decimal
currency integrity; no-look-ahead construction; deterministic core; failure-safe
data; standardized evaluation incl. five-year replay; regime detection; durable
experience store; gated candidate generation; champion-vs-challenger evidence;
promotion/rollback; the feedback loop; capture correctness; alerting + watchdog;
test suite/acceptance (788 passed, 37 modules); operations artifacts (13 CLIs,
git-ignored state, offline HTML, `.env`-only secrets); and an explicit
scope-honesty item (WS 7.6–7.8, notification integrations, live broker NOT
implemented). Updated `PROJECT_PLAN.md` §17d intro and README capability rows to
match reality (alert engine/adaptive learning/feedback loop/watchdog moved from
PLANNED to implemented).

**Status.** Committed as `docs: Phase-7 close-out 20-item engineering report`,
pushed.

---

## 4af. 2026-09-12 - Real-data champion model-performance report (continuous 5m replay)

**Scope.** Replace the one-session-per-day "five-year" style evaluation with a
**continuous** paper replay of the frozen champion MA(5,21) over real NIFTY 50
5-minute history (87,193 bars, 2022-01-03 .. 2026-09-11) plus the full evidence
bundle and documentation deliverable
(`docs/model_performance_report.md`, git-ignored
`reports/model_performance/`).

**Verification.** 813 passed (788 + 25 new): `test_fast_signal.py` (O(n) signal
precompute bit-for-bit equal to `strategy.analyze(bars[:i+1])` on 406 real
sampled prefixes), `test_model_performance.py` (engine `signals=` path ===
per-bar analyze path; round-trip reconciliation exact; decision-time entry
regimes; period/regime aggregation partition; benchmark/index math; splits;
walk-forward equivalence to `run_walk_forward`; cost sensitivity; determinism;
save/load round trip), `test_coverage.py` (full calendar, weekend/gap,
missing-day, duplicate-timestamp validation, histogram/hash).

**Headline findings (honest, real data).** Net return **-143.21%** (final equity
-₹43,210 from ₹100,000), max DD 143.29%, 2,601 round trips, 15.53% win rate,
Sharpe -1.95, friction ₹146,129 (commission ₹33,722 + slippage ₹112,407) against
**gross trading P&L ≈ +₹2,918**; every year negative (2022..2026); NIFTY
buy-and-hold +27.96% (1d) / +29.62% (5m). Train/validation/test all negative;
walk-forward combined return -66.79%; every decision-time regime bucket negative
(9.77%-18.42% win rates); cost sensitivity: only the zero-cost scenario is
positive (+2.92%), base/high both deeply negative. This is a **clear negative
result for the registered champion on real data** — consistent in direction with
the 10-Sep-2026 note below, and much larger once continuous compounding, stop-loss
re-entry and decision-time regimes are included.

**Decisions.** Engine gained a backward-compatible optional `signals=` stream;
new `evaluation/` modules (fast_signal, model_performance, coverage, perf_reports)
and two scripts (acquire data, generate report). Kept MA(5,21) **frozen**; no
parameter shopping on this single 4.7-year window (explicit overfit caveat in the
report). Documented limitations: ~4.7 years real history (not five), no cash/margin
floor in either paper broker (negative equity is an implicit-leverage artifact),
gross benchmark vs net strategy, illustrative cost model, candidate missing-day
upper bounds.

**Status.** Committed as `feat: champion model-performance evaluation on real
NIFTY 5m + report evidence` (550b5c8), pushed.

---

## 5. Open Topics / Risks

- **12-Sep-2026 continuous real-data replay: champion MA(5,21) nets -143.21%**
  over 87,193 real NIFTY 5m bars (friction ₹146,129 vs gross trading P&L ≈
  +₹2,918; all years and all decision-time regimes negative; only the zero-cost
  scenario is positive). An evaluation observation — MA(5,21) stays the frozen V1
  baseline and is NOT changed; this is documented evidence for the promotion gate
  (see `docs/model_performance_report.md`), not a reason to alter the algorithm.
  Any candidate/variant must pass the net-of-cost, continuous-replay, regime +
  walk-forward + OOS gates from §10 of that report before promotion.
- **10-Sep-2026 real-data paper replay loss (~₹194.68).** An evaluation
  observation, NOT a reason to change the algorithm (MA(5,21) stays the frozen V1
  baseline). Multi-session replay, regime analysis and out-of-sample validation are
  required before any candidate is considered — see `PROJECT_PLAN.md` §17d/§17e.
- The Kite Connect credential flow (api key + access token) is implemented and
  tested with mocked HTTP. It has **not** been exercised against the live
  service end-to-end; that requires a real session token and is left for the
  user (out of scope for offline verification).
- `.env.example` intentionally contains placeholders only — the user is
  responsible for supplying real credentials via `.env`.
- The NSE 2026 holiday calendar in `data/market_hours.py` is best-effort;
  verify it against the official NSE calendar before relying on it.
- The research cost schedule (`IndiaCostSchedule.nse_fo_illustrative()`) is
  explicitly illustrative. **It must be replaced with the real fee schedule of
  the target broker/segment before any backtest conclusion is drawn** — this is
  stated in the module docstring, the report, and the README. Same for
  `ExecutionAssumptions` (slippage/spread/impact defaults).
- MA-cross behaviour is regime-dependent by design: on a strict monotone ramp
  the fast average is already above the slow one when the slow average becomes
  computable, so no crossover fires (a pure breakaway trend generates a single
  entry and holds). The regime builders avoid this by leading with a
  counter-drift longer than the slow window; the walk-forward OOS leg still
  re-warms on test bars only.