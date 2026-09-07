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
| 2 | Real market data + first deterministic strategy + backtest harness | **DONE** — commits `a86ec64`, `feat: add deterministic backtest harness` |
| 3 | Strategy research & robustness evaluation (costs, regimes, in/out-of-sample, walk-forward, sensitivity, benchmark, metrics, experiments, HTML notebook) | **DONE** — `feat: add strategy research and robustness framework` |
| 4 | Historical-data CLI, AI analysis / decision support | Planned (not started) |
| 5 | Backtesting engine + analytics | Folded into Phase 2 (engine implemented); analytics extended by the research framework (Phase 3) |

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