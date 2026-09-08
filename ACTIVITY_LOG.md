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
| 6 | Paper Trading V1 — current-data paper session | **SPECIFIED / PLANNED** — spec `59d831d` (PROJECT_PLAN §17d assigns Phase 6); session loop, sizing, persistence NOT IMPLEMENTED |

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

## 5. Open Topics / Risks

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