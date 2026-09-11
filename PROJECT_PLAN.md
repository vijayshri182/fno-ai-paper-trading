# F&O AI Paper Trading System — Master Project Plan

## 1. Project Vision

Build a modular Python-based F&O (Futures & Options) AI-assisted paper-trading platform.

The system will initially operate in **paper-trading mode only**.

The architecture must support integration with **real market-data APIs** in a later phase without requiring major changes to the core trading, risk, portfolio, strategy, or paper-broker components.

The long-term objective is to build a system that can:

1. Receive real market data.
2. Normalize instruments and market prices.
3. Generate trading signals using deterministic strategies.
4. Use AI for market analysis and decision support.
5. Apply strict risk controls.
6. Execute simulated paper trades.
7. Track positions, cash, P&L and exposure.
8. Backtest strategies.
9. Provide analytics and reports.
10. Eventually provide a dashboard/UI.
11. Keep real-money execution disabled unless explicitly designed, reviewed and enabled as a separate future capability.

---

# 2. Core Principle

The system must separate:

- Market data
- Strategy logic
- AI analysis
- Risk management
- Order execution
- Portfolio management
- Analytics

No single component should contain all trading logic.

The intended architecture is:

```text
                    REAL MARKET DATA APIs
                             |
                             v
                    +--------------------+
                    | Data Provider Layer|
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | Data Normalization  |
                    | Instruments / OHLCV |
                    | Quotes / Prices     |
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | Strategy Engine     |
                    | Rules / Indicators  |
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | AI Analysis Layer   |
                    | Context / Signals   |
                    | Explanation         |
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | Risk Manager        |
                    | Limits / Exposure   |
                    | Loss Controls       |
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | Paper Broker        |
                    | Simulated Execution |
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | Portfolio           |
                    | Positions / Cash    |
                    | P&L / Trades        |
                    +--------------------+
                             |
                             v
                    +--------------------+
                    | Analytics / Reports |
                    | Dashboard (future)  |
                    +--------------------+
```

---

# 3. Non-Negotiable Safety Rules

These rules apply to the entire project.

1. Initial system is **paper trading only**.
2. No real-money order execution.
3. No live broker order placement.
4. No automatic transition from paper trading to live trading.
5. No hard-coded API keys.
6. No hard-coded passwords.
7. No hard-coded access tokens.
8. No secrets committed to Git.
9. `.env` must remain excluded by `.gitignore`.
10. `.env.example` must contain only safe example/template values.
11. AI must never bypass the RiskManager.
12. Strategies must never directly bypass the RiskManager.
13. Strategies must not directly execute broker orders.
14. PaperBroker must remain isolated from real broker implementations.
15. External APIs must be abstracted behind interfaces/adapters.
16. Real broker integration, if ever introduced, must be a separate explicitly controlled capability.
17. Coding agents must not commit or push to Git unless explicitly instructed.
18. Major architectural changes must be documented in this file.

---

# 4. Technology Baseline

Current development environment:

* Python 3.13.x
* Windows
* Virtual environment: `.venv`
* Git
* GitHub
* pytest
* python-dotenv

Financial calculations should use `Decimal` instead of binary floating-point where monetary precision matters.

Keep dependencies minimal.

Do not add large frameworks or libraries unless there is a clear reason.

HTTP transport note: on Windows the HTTP layer shells out to `curl.exe` (schannel
TLS) because the stdlib `urllib` TLS fingerprint is blocked by the Cloudflare WAF
in front of `api.upstox.com`; non-Windows systems use the `urllib` fallback. See
the Change Log 2026-09-08 and `utils/http.py`.

---

# 5. Project Development Phases

## Phase 1 — Foundation

### Status

**COMPLETE** (2026-09-07) — foundation, models, paper broker, portfolio, risk
manager, configuration, logging, entry point and tests are implemented and pushed.
See Definition of Done §25.

Build the core foundation:

* Core domain models
* Configuration
* Data provider interface
* Sample/in-memory market data
* Broker interface
* PaperBroker
* Portfolio
* P&L
* RiskManager
* Logging
* Unit tests
* Basic application entry point

### Phase 1 does NOT include:

* AI
* Trading strategies
* Backtesting
* Real market-data APIs
* Real broker APIs
* Live order execution
* Web UI

---

# Phase 2 — Real Market Data + Strategy Foundation

### Status: implemented (2026-09-07)

Implemented the real-market-data provider interface, the strategy engine, and
the deterministic backtest harness.

**Delivered:**
- `MarketDataProvider` ABC extended: `get_instrument`, `get_quote`,
  `get_market_session`, `get_historical_ohlcv` (+ existing `get_prices`).
- Read-only **Zerodha Kite Connect v3** adapter (`data/kite_provider.py`):
  quote, LTP, position-size/contract CSV master, historical candles and market
  status over `FNO_KITE_*` credentials; exponential-backoff retries on
  rate-limit/5xx/network errors; typed `MarketDataError` hierarchy.
- NSE market hours helper (`data/market_hours.py`): session phases
  (PRE_OPEN/OPEN/CLOSED), weekend/holiday handling, next-open rollover.
- Extended models: `MarketPhase` enum, `MarketQuote`, `MarketSession`,
  `open_interest`, `exchange`/`exchange_token`.
- Deterministic strategy engine: `Strategy` ABC, `StrategyEngine` replay, and
  `MovingAverageCrossStrategy` (fast/slow SMA crossover).
- `StrategyService`: evaluates signals and submits paper orders only, through
  RiskManager → PaperBroker.
- **Backtest harness** (`fno_ai_paper_trading.backtest`): deterministic
  `BacktestEngine` (no look-ahead, bar-timestamped fills), `BacktestConfig`
  execution assumptions (capital, sizing, commission, slippage, risk limits),
  `BacktestResult`/`EquityPoint` metrics (P&L, win rate, profit factor,
  drawdown, equity curve), hand-verified dataset fixtures (long/short, winning,
  losing, drawdown, multiple trades, no-trade) and an offline
  `python -m fno_ai_paper_trading.backtest` demo. Paper-broker only — no
  credentials, no network. See `tests/test_backtest.py` for correctness and
  safety regression coverage.

The system should be able to receive real market information while continuing to execute trades only through the PaperBroker.

Potential capabilities:

* Real-time or delayed market prices
* OHLCV
* LTP/quotes where supported
* Instrument master
* Futures contracts
* Options contracts
* Expiry information
* Strike prices
* Lot sizes
* Open interest where available
* Volume
* Historical market data
* Market status
* Market hours

### Important architecture

```text
Real Market Data API
        |
        v
MarketDataProvider
        |
        v
Internal Domain Models
        |
        v
Strategy Engine
        |
        v
Risk Manager
        |
        v
Paper Broker
```

The rest of the application should not depend directly on a specific market-data vendor.

---

# 6. Market Data API Design

The market-data layer must use an abstraction such as:

```text
MarketDataProvider
        |
        +-- InMemoryProvider
        |
        +-- HistoricalProvider
        |
        +-- RealMarketDataProvider
```

Phase 1:

```text
InMemoryProvider
```

Phase 2:

```text
RealMarketDataProvider
```

The provider-specific implementation should contain:

* API authentication
* HTTP/API communication
* request handling
* response parsing
* retry logic
* timeout handling
* rate-limit handling
* provider-specific error handling

The rest of the system should receive normalized internal objects.

---

# 7. API Integration Rules

When real APIs are introduced:

1. API credentials must come from environment variables.
2. Credentials must never be stored in source code.
3. Credentials must never be committed to Git.
4. Provider-specific code must remain isolated.
5. Core business logic must not directly depend on a vendor SDK.
6. API responses must be converted into internal domain models.
7. Timeouts must be implemented.
8. Retries should be implemented where appropriate.
9. Rate limits must be respected.
10. API failures must not crash the entire trading engine unnecessarily.
11. Market timestamps must be normalized.
12. Time zones must be handled explicitly.
13. Tests should use mocks/fixtures rather than relying on live APIs.
14. API connectivity should be independently testable.
15. A real API outage must not corrupt portfolio state.

---

# 8. Strategy Architecture

Strategies will be introduced in Phase 2.

A strategy should:

* Consume market data
* Calculate indicators/features
* Generate BUY/SELL/HOLD signals
* Provide reasoning/metadata where useful
* Never directly place orders
* Never bypass RiskManager

Architecture:

```text
Market Data
     |
     v
Strategy
     |
     v
Signal
     |
     v
RiskManager
     |
     v
PaperBroker
```

Possible future strategies:

* Moving average strategies
* Momentum
* Mean reversion
* Breakout
* Volatility-based
* Options-specific strategies
* Futures strategies
* Multi-factor strategies

No strategy should be assumed profitable without testing.

---

# 9. AI Architecture

AI will be introduced in **Phase 7** (post-V1), strictly as an **advisory
decision-support layer**. It is **PLANNED** — no AI model, API or learning loop
is implemented anywhere in the repository. The V1 baseline is the frozen
MA(5,21) strategy; any future AI or regime-aware candidate must clear the
strategy-evaluation discipline (§17e) before it can influence decisions.

AI should primarily provide:

* Market analysis
* Context
* Signal assistance
* Candidate trade ranking
* Explanation
* Risk/context commentary
* Structured reasoning

A future AI recommendation will be a structured, advisory object capturing at a
minimum: an action (`BUY`/`HOLD`/`SELL`), a confidence, a rationale, the market
regime/context, a model/version tag and a timestamp. It is advisory only — it
never creates an order by itself (see §17e).

AI should NOT:

* Directly place broker orders
* Bypass RiskManager
* Override hard risk limits
* Access secrets unnecessarily
* Automatically enable live trading

Preferred flow:

```text
Market Data
     |
     v
Indicators / Strategy
     |
     v
AI Analysis
     |
     v
Structured Signal
     |
     v
RiskManager
     |
     v
PaperBroker
```

AI should be treated as a decision-support component.

---

# 10. Risk Management Architecture

RiskManager is a mandatory gatekeeper.

```text
Strategy / AI
     |
     v
Order Request
     |
     v
RiskManager
     |
     +---- REJECT ----> No execution
     |
     +---- APPROVE ---> PaperBroker
```

Initial risk controls:

* Maximum order quantity
* Maximum position quantity
* Maximum order notional
* Maximum daily loss
* Exposure limits
* Invalid quantity checks
* Invalid price checks

Future risk controls may include:

* Portfolio exposure
* Underlying exposure
* Sector exposure
* Volatility-adjusted sizing
* Options Greeks
* Drawdown limits
* Correlation limits
* Concentration limits
* Daily trade limits
* Maximum number of open positions

RiskManager must remain independent of any particular strategy.

---

# 11. Paper Broker Architecture

The first execution engine is:

```text
PaperBroker
```

It must simulate execution.

Responsibilities:

* Receive approved orders
* Validate order properties
* Simulate fills
* Track order status
* Generate fills
* Apply configurable commission
* Apply configurable slippage
* Support deterministic testing

PaperBroker must NOT:

* Call a real broker
* Submit real orders
* Store real broker credentials
* Automatically switch to live execution

Future architecture:

```text
Broker
   |
   +-- PaperBroker
   |
   +-- RealBroker (future)
```

RealBroker must remain a separate implementation.

---

# 12. Portfolio Architecture

Portfolio owns the simulated account state.

It should track:

* Cash
* Positions
* Orders
* Fills
* Trade history
* Realized P&L
* Unrealized P&L
* Portfolio value
* Exposure

Portfolio must not depend directly on a specific broker vendor.

Financial calculations should use precise numerical handling, preferably `Decimal`.

---

# 13. Core Domain Models

## Instrument

Should support:

* Symbol
* Underlying
* Expiry
* Strike
* Instrument type
* Lot size

Instrument types should be extensible.

Examples:

```text
EQUITY
FUTURE
OPTION
```

---

## Order

Should contain information such as:

* Order ID
* Instrument
* Side
* Order type
* Quantity
* Price where applicable
* Timestamp
* Status

---

## Fill

Should contain:

* Order ID
* Fill ID
* Fill quantity
* Fill price
* Timestamp
* Commission/fees

---

## Position

Should track:

* Instrument
* Quantity
* Average entry price
* Current price
* Realized P&L
* Unrealized P&L

---

## Signals

Initial signal enum:

```text
BUY
SELL
HOLD
```

---

# 14. Configuration

Configuration should be centralized.

Possible environment variables:

```text
PAPER_INITIAL_CASH
PAPER_MAX_POSITION_QTY
PAPER_MAX_ORDER_NOTIONAL
PAPER_MAX_DAILY_LOSS
PAPER_COMMISSION
PAPER_SLIPPAGE
LOG_LEVEL
```

Future API credentials may include provider-specific variables, but these must never be placed in source code.

Example:

```text
API_KEY
API_SECRET
ACCESS_TOKEN
```

These are examples only and must never contain real values in committed files.

---

# 15. Phase 1 Repository Structure

```text
fno-ai-paper-trading/
|
+-- PROJECT_PLAN.md
+-- README.md
+-- .env.example
+-- .gitignore
+-- requirements.txt
|
+-- src/
|   +-- __init__.py
|   +-- main.py
|   |
|   +-- config/
|   |   +-- __init__.py
|   |   +-- settings.py
|   |
|   +-- models/
|   |   +-- __init__.py
|   |   +-- enums.py
|   |   +-- instruments.py
|   |   +-- order.py
|   |   +-- position.py
|   |
|   +-- data/
|   |   +-- __init__.py
|   |   +-- provider.py
|   |   +-- sample_provider.py
|   |
|   +-- broker/
|   |   +-- __init__.py
|   |   +-- base.py
|   |   +-- paper_broker.py
|   |
|   +-- portfolio/
|   |   +-- __init__.py
|   |   +-- portfolio.py
|   |
|   +-- risk/
|   |   +-- __init__.py
|   |   +-- manager.py
|   |
|   +-- utils/
|       +-- __init__.py
|       +-- logging.py
|
+-- tests/
    +-- __init__.py
    +-- test_models.py
    +-- test_paper_broker.py
    +-- test_portfolio.py
    +-- test_risk.py
```

---

# 16. Testing Strategy

All important functionality must have automated tests.

Minimum Phase 1 coverage:

## Models

* Valid instrument creation
* Invalid instrument validation
* Order creation
* Invalid order validation
* Position creation

## Paper Broker

* Place paper order
* Fill order
* Status transition
* Commission
* Slippage
* Invalid order rejection

## Portfolio

* Initial cash
* Buy position
* Sell position
* Average entry price
* Realized P&L
* Unrealized P&L
* Portfolio value

## Risk

* Valid order approval
* Quantity rejection
* Notional rejection
* Position limit rejection
* Daily loss rejection

Tests must be deterministic.

Tests must never place real orders.

Tests must not require external API connectivity.

---

# 17. Backtesting Architecture

Backtesting is implemented within Phase 2 (2026-09-07) in the
`fno_ai_paper_trading.backtest` package; see the Phase 2 delivery notes above.
A historical-data CLI that loads external OHLCV is planned for Phase 3.

The backtesting engine reuses the same core concepts where practical.

Preferred architecture (as implemented):

```text
Historical Data
      |
      v
Strategy
      |
      v
Signal
      |
      v
RiskManager
      |
      v
Simulated Execution
      |
      v
Portfolio
      |
      v
Performance Analytics
```

Backtesting accounts for:

* Commission
* Slippage
* Position sizing
* Entry/exit rules
* Available cash
* Risk limits
* Drawdown
* P&L

The backtesting engine avoids look-ahead bias (strategy sees only `bars[:i+1]`
at bar `i`) and is fully deterministic (fills carry bar timestamps; all costs
are explicit `Decimal` arithmetic).

---

# 17b. Strategy Research & Robustness Framework

Implemented 2026-09-07 in the `fno_ai_paper_trading.research` package as a
deterministic, paper-only addition to the backtest harness. Purpose: evaluate
whether a strategy has an edge **after realistic costs**, with in-sample and
out-of-sample evidence, never to cherry-pick a profitable-looking configuration.

**Delivered:**

- `costs.py` — configurable Indian cost schedule (`IndiaCostSchedule`) and per-
  fill breakdown (`ChargeBreakdown`): brokerage, STT (sell side), exchange
  charges, SEBI, stamp duty (buy side), GST on the taxable base (brokerage +
  exchange + SEBI), and other per-order charges. Exact `Decimal` arithmetic.
  `nse_fo_illustrative()` ships **documented illustrative values, explicitly not
  a claim of current real fees**.
- `execution.py` — `ExecutionAssumptions` (slippage + half-spread + impact →
  single `total_adverse_rate` applied per fill).
- `regimes.py` — six deterministic, hand-verifiable synthetic regimes
  (sustained uptrend/downtrend, sideways/choppy, volatile, trend reversal,
  low-volatility), float-free close formulas.
- `split.py` — chronological contiguous train/validation/out-of-sample split
  (default 60/20/20, must sum to 1).
- `walkforward.py` — rolling `[train][test] → advance` windows (non-overlapping,
  step ≥ test), each test window strictly out-of-sample; `build_strategy(train)`
  is the explicit "fit on train" seam.
- `sensitivity.py` — evaluates only explicitly enumerated `(fast, slow)`
  combinations; invalid pairs are skipped and reported, never executed. This is
  **not** an optimizer.
- `benchmark.py` — gross buy-and-hold benchmark (100% exposure) on identical
  bars; unfunded positions raise.
- `metrics.py` — net-of-cost metrics (P&L, return, CAGR, drawdown + duration,
  win rate, profit factor, expectancy, annualized vol / Sharpe / Sortino,
  exposure) with documented "unavailable" (None) semantics.
- `experiment.py` — self-describing experiment record (`ExperimentConfig`) with
  strategy/dataset/date/capital/cost/slippage provenance and a deterministic
  `config_hash` (SHA-256, first 16 hex); `run_experiment(...)` returns metrics
  plus an optional benchmark.
- `report.py` + `scripts/generate_research_report.py` — labelled HTML notebook
  (`reports/research_report.html`, git-ignored) covering costs, execution,
  regimes, per-regime experiments, in/out-of-sample, walk-forward, sensitivity,
  and benchmark comparison.
- Backtest wiring (backward compatible): `BacktestConfig.cost_schedule`
  (duck-typed `compute(side, notional, quantity) -> .total`) and
  `BacktestConfig.execution` (duck-typed `total_adverse_rate`) override the
  legacy commission/slippage fields when set; defaults `None` keep existing
  behaviour unchanged.
- `backtest/datasets.py` now exposes `closes_to_bars` as a public alias for the
  regime builders.
- `tests/test_research.py` (45 tests): hand-verified cost math, execution
  assumptions, exact regime close sequences, split/walk-forward boundaries,
  sensitivity skip semantics, benchmark arithmetic, metrics None-cases, config
  hashes, and engine wiring of the cost schedule / execution assumptions.

**Anti-overfitting stance.** No grid search, no optimizer, no ML. Parameters are
fixed explicitly; sensitivity runs only user-enumerated combinations; every
claim is reported net of the configured costs with the out-of-sample evidence
shown alongside.

**Interface to safety rules.** The research package imports only the paper-only
backtest engine and deterministic mock instruments — no credentials, no network,
no live execution path (verified by tests and by the offline demos).

### 17b.1 Historical baseline research results (MA(5,21) on real NIFTY 50 data)

Produced by `scripts/research_real_data.py` (2026-09-07) against the read-only
Upstox historical pipeline; source dataset:
`datasets/upstox_Nifty_50_1d_20150101_20241231.meta.json`.

| Item | Value |
|---|---|
| Window | 2015-01-01 through 2024-12-31 |
| Bars | 2,477 daily bars |
| Dataset SHA-256 `data_hash` | begins `2dde47d4`, ends `6021b660` |
| Full-period net return | **−17.66%** (74 trades, 20.13% max drawdown) |
| Out-of-sample net return (locked params) | **+7.08%** (11 trades, 3.29% max drawdown) |
| Buy-and-hold benchmark (gross) | **+30.72%** |

> These are **historical backtest/research results only** — not profitability
> claims, not trading results, and not evidence that the future paper session will
> be profitable. The full-period MA(5,21) result is negative net of the configured
> illustrative costs. The historical study does **not** claim a ₹1,00,000 virtual
> account; backtests run historical data and report net-of-cost returns, while the
> V1 paper session starts from virtual ₹1,00,000 on current data.

---

# 17c. Paper Trading V1 — Contract & Current Boundaries

Paper Trading V1 is the first **live/current-data paper session**: it evaluates
the existing deterministic strategy engine against **completed 5-minute candles**
of current market data and fills simulated orders through the paper broker, so
virtual capital, risk and P&L evolve near real time without any connection to a
real broker. It is fully specified in `docs/trading/PAPER_TRADING_V1.md`.

## V1 specification (agreed contract)

| # | Assumption | Value / rule |
|---|---|---|
| 1 | Virtual starting capital | ₹1,00,000 (`FNO_PAPER_INITIAL_CAPITAL` default) |
| 2 | Bar cadence | 5-minute **completed** candles (`paper_interval = "5m"`); closed bar only, never a forming bar |
| 3 | Instrument | NIFTY 50 index — key `NSE_INDEX\|Nifty 50`, `lot_size=1`, `multiplier=1` |
| 4 | Direction | **Long-only**; `BUY` opens, `SELL` maps to EXIT of the long; short-to-open forbidden |
| 5 | Strategy | `MovingAverageCrossStrategy(fast=5, slow=21)`; MA(5) above MA(21) = BUY, below = EXIT; 22-bar warm-up |
| 6 | Stop-loss | Fixed 2% below entry (`paper_stop_loss_pct`); automatic paper exit at the worse of the candle open and the stop price |
| 7 | Risk per trade | 1% of **current** virtual equity (`paper_risk_per_trade_pct`) |
| 8 | Position sizing | Risk-based quantity computed from (equity, entry, risk %, stop %) via exact `Decimal`; rounded **down** to the instrument lot size; skip below lot; bounded by available cash |
| 9 | Real orders / real money | Never |
| 10 | Leverage | None; notional bounded by available cash (existing `max_order_notional` also applies) |
| 11 | Persistence | JSON snapshots under git-ignored `paper_state/` (WS 6.5); a database / multi-session ledger remains out of scope |

This is the agreed **contract and now-implemented** capability: the Phase 6 work
streams (WS 6.2–6.7) delivered the implementation described below; only the items
listed under PLANNED / NOT IMPLEMENTED hold no code today.

## Current boundaries (status vs. source)

### IMPLEMENTED (foundation the session will reuse)

* Historical research/backtesting harness (`backtest/`, `research/`) — offline, deterministic, paper-only.
* Market-data acquisition: read-only Upstox V3 historical-candle adapter, interval mapping, curated instrument registry, dataset store with SHA-256 hashes, report-only validation.
* HTTP transport: Windows `curl.exe` path with `urllib` fallback (see Change Log 2026-09-08).
* Strategy baseline: `MovingAverageCrossStrategy(fast=5, slow=21)` with warm-up.
* Foundation components already present and tested: domain models, `MarketDataProvider` ABC, `PaperBroker`, `Portfolio`, `RiskManager`, `TradingService`, `StrategyService`, market-hours clock, typed error hierarchy.
* Phase 6 session layer (WS 6.1–6.7): `PaperSession` runtime, `RiskBasedPositionSizer`, `risk/stop_loss.py`, `persistence/session_store.py`, `services/session_monitoring.py`, `scripts/paper_session_report.py`, `docs/architecture/architecture.svg`, and `tests/test_acceptance_replay.py` + `tests/test_session_monitoring.py` — see §17d for per-work-stream status.

### DECLARED CONFIG (partial consumers; no env wiring yet)

Declared at `src/fno_ai_paper_trading/config/settings.py:57-62`. Three fields are
**consumed as `PaperSession` defaults** (WS 6.4b) — `paper_interval`,
`paper_risk_per_trade_pct`, `paper_stop_loss_pct`; two are declared but not consumed
by any runtime — `paper_lookback_days`, `paper_state_dir` (the persistence store uses
the constant `DEFAULT_STATE_DIR = "paper_state"`). None of the five are wired into
`load_settings()` (no `FNO_PAPER_INTERVAL` etc. yet):

| Field | Default | Consumer |
|---|---|---|
| `paper_interval` | `"5m"` | `PaperSession` interval default (WS 6.4b) |
| `paper_lookback_days` | `3` | none (declared only) |
| `paper_risk_per_trade_pct` | `0.01` (1%) | `PaperSession` sizer default (WS 6.4b) |
| `paper_stop_loss_pct` | `0.02` (2%) | `PaperSession` stop-policy default (WS 6.4b) |
| `paper_state_dir` | `"paper_state"` | none (store uses `DEFAULT_STATE_DIR`) |

The already-wired runtime settings are `FNO_PAPER_INITIAL_CAPITAL` (100000),
`FNO_PAPER_MAX_POSITION_QUANTITY` (75), `FNO_PAPER_MAX_ORDER_NOTIONAL` (250000),
`FNO_PAPER_MAX_DAILY_LOSS` (10000), commission/slippage — all consumed by
`RiskManager`, the backtest harness, the demos and `PaperSession`.

### PLANNED / NOT IMPLEMENTED

* Env wiring for the five scaffolded `paper_*` fields (`FNO_PAPER_INTERVAL`, etc.)
  plus `.env.example` rows — **DEFERRED TO PHASE 7** (out of V1 scope; V1 consumes
  the typed `PaperSettings` defaults).
* Live/streaming quotes (V1 derives prices from completed historical candles) —
  out of V1 scope.

## Important boundary: static caps ≠ V1 sizing

The **existing `RiskManager` static limits** — `max_position_quantity` (75),
`max_order_notional` (250000), `max_daily_loss` (10000) — are **implemented** and
continue to gate every order. They are **absolute ceiling caps**.

The **V1 1%-risk sizing rule** (quantity = 1% of current equity risked over a 2%
stop distance, lot-rounded) is a **separate mechanism** that computes an order
quantity. It is implemented by `RiskBasedPositionSizer` (WS 6.3, `risk/sizer.py`)
and routed through the session (WS 6.4b); the caps remain the final authority — V1
sizing is **not** a replacement for the caps.

## Historical backtest vs. V1 capital — clarification

* The **historical research baseline** (MA(5,21) on real NIFTY 50 data) is reported
  in **net-of-cost return terms**; it used the research capital/quantity configured
  in `scripts/research_real_data.py` and must **not** be claimed to have used a
  ₹1,00,000 virtual account. It is a research baseline, not a profitability claim.
* **V1 paper session** starts from virtual ₹1,00,000 (`initial_capital`
  default) on **current** data with simulated orders. These are two separate things.

---

# 17d. Roadmap — Remaining Work

Phase 6 delivery status below; the historical-research milestone and the
live/current-data paper path are implemented and committed. Live trading stays
explicitly out of scope.

Per `docs/trading/PAPER_TRADING_V1.md` §2.4, the plan assigns **Phase 6** to Paper
Trading V1. Phase 6 is broken into ordered work streams, each annotated with its
delivery state.

**Phase 6 — Paper Trading V1 (live/current-data paper session)**
*Work stream 6.1 — Documentation / plan alignment* — **DONE** (`248c895`)
* Keep `PAPER_TRADING_V1.md`, `ARCHITECTURE.md` and this plan consistent with the
  repository as implementation proceeds; validate cross-references.

*Work stream 6.2 — Paper-trading domain/model completion* — **DONE** (`e853667`)
* Complete the remaining session-level domain gaps identified by the V1 spec:
  long-only gating, cash/leverage guard, duplicate-candle guard.

*Work stream 6.3 — V1 position sizing and risk enforcement* — **DONE** (`b4f8129`)
* Implement the V1 sizer (1% equity → quantity, lot rounding, capital bound) and
  route it through the existing `RiskManager` static caps (cap gate remains the
  final authority — V1 sizing is **not** a replacement for the caps).

*Work stream 6.4 — Current-data paper-session engine* — **DONE** (`75d3a44`
stop-loss; `b05a17c` session runtime)
* `services/paper_session.py`: completed-5m-candle loop, NSE OPEN-phase + holiday
  gating, 22-bar warm-up, stop-loss evaluation, `--once`/`--loop` modes,
  deterministic (injected clock + data source). Consumes the Upstox adapter.

*Work stream 6.5 — State persistence and recovery* — **DONE** (`118e976`)
* Session ledger + snapshots under git-ignored `paper_state/` (per V1 spec §10).

*Work stream 6.6 — Session operations / monitoring* — **DONE** (`00ed8ed`)
* `services/session_monitoring.py`: `SessionHealth`/`health()`, `SessionReport`/
  `build_report`/`report_from_snapshot`, `log_results`/`log_health`,
  `report_to_html`/`write_html_report` (HTML consistent with `research/report.py`
  CSS); operator CLI `scripts/paper_session_report.py` renders an offline report
  from a stored session payload. Scheduled runs remain operator-level (Task
  Scheduler / cron wrapping `run_loop` + CLI), per ARCHITECTURE §18.1.

*Work stream 6.7 — Paper-session testing and acceptance* — **DONE** (`0e5ce1c`)
* Deterministic replay tests for the session; execute the V1 acceptance criteria
  (§13 of `PAPER_TRADING_V1.md`) offline in the automated suite (30 tests).

**Phase 7 — Post-V1 evolution, evaluation-first (paper-trading-only)**

The V1 MA(5,21) strategy is the **frozen baseline**. A single losing session —
including the 10-Sep-2026 real-data paper replay (~₹194.68 loss) — must **NOT**
trigger parameter changes or strategy replacement. The evaluation discipline in
§17e is positioned **before** any tuning or replacement of the baseline:

1. **Freeze & measure** — replay the MA(5,21) baseline over multiple validated
   NIFTY 50 sessions and record the baseline metrics (§17e.3).
2. **Regime analysis** — measure baseline behavior across trending,
   sideways/choppy, high-volatility and low-volatility regimes (§17e.4).
3. **Regime-aware hypotheses** — investigate a future regime filter
   (§17e.5). Hypotheses only; not implemented rules.
4. **AI decision support** — advisory-only structured recommendations behind an
   interface (§17e.6), never autonomous execution.
5. **Compare** — evaluate any enhanced candidate against the frozen baseline on
   the same validated data and comparable assumptions (§17e.8).
6. **Out-of-sample validate** — no acceptance on design/tuning data alone
   (§17e.9).
7. **Adopt only on evidence** — a candidate replaces or augments the baseline
   only when evidence shows meaningful, robust improvement while respecting
   risk constraints (§17e.10).

* Any real-broker adapter — a **separate, explicitly controlled capability**, never
  silently enabled, still gated by `RiskManager`.

---

# 17e. Strategy Evaluation & Improvement Discipline

This discipline governs ALL post-V1 strategy work (regime filters, AI decision
support, parameter variations). **PLANNED** for Phase 7 — the discipline itself
is a documented contract, not a training/learning implementation.

## 17e.1 Frozen V1 baseline

* MA(5,21) is the V1 baseline strategy and is **frozen**.
* A single losing session must NOT trigger parameter changes. In particular, the
  10-Sep-2026 real-data paper replay loss (~₹194.68) is **an evaluation
  observation, not a reason by itself to change the algorithm**. It demonstrates
  why multi-session evaluation and regime analysis are required.

## 17e.2 Historical evaluation

* Replay **multiple** validated historical NIFTY 50 sessions (validated, hashed
  datasets via `data/dataset_store.py`/`data/validation.py`) before any
  conclusion can be drawn about a strategy or candidate.

## 17e.3 Baseline metrics

* Record, at minimum: total P&L; return %; win rate; number of round trips;
  average trade; transaction costs; maximum drawdown; maximum drawdown %;
  exposure / position size; losing streak where applicable.

## 17e.4 Market regime analysis

* Evaluate strategy behavior across regimes: **trending**, **sideways/choppy**,
  **high volatility**, **low volatility**. Regime definitions and measurement
  are themselves Phase 7 research.

## 17e.5 Regime-aware improvement (hypotheses only)

* These are **design hypotheses, NOT implemented trading rules**:
  * strong trend → allow crossover signals
  * sideways/choppy → prefer `HOLD` / avoid weak entries
  * high volatility → consider reduced risk/position size
  * weak signal → `HOLD`
  * strong signal + confirmation → allow recommendation
* Every hypothesis must enter the evaluation discipline like any candidate
  (§17e.8–17e.10).

## 17e.6 AI decision support (future, advisory only)

* A future AI decision support layer may produce advisory `BUY` / `HOLD` /
  `SELL` recommendations with: confidence, rationale, market regime/context,
  model/version, timestamp.
* **Hard safety boundary:** AI must NEVER directly execute an order, bypass
  `RiskManager`, bypass position sizing, bypass stop-loss, bypass `PaperBroker`,
  modify `Portfolio` accounting, or enable live trading automatically.

## 17e.7 Design principle (preferred flow)

```text
Validated Market Data
→ Feature Engineering
→ Market Regime Detection
→ Baseline Strategy
→ AI Decision Support            (future, advisory)
→ Signal + Confidence
→ Deterministic Risk Gate        (authoritative)
→ Position Sizing
→ Stop Loss
→ Paper Execution
→ Portfolio Accounting
→ Monitoring
→ Historical Evaluation
→ Baseline vs Enhanced Comparison
→ Out-of-Sample Validation
```

AI remains advisory. **Deterministic controls remain authoritative.**

## 17e.8 Comparison framework

* **Baseline:** MA(5,21).
* **Enhanced candidate:** MA(5,21) + Market Regime Filter + AI Decision Support
  (future).
* The enhanced approach must be evaluated against the frozen baseline using the
  **same historical data and comparable assumptions**.

## 17e.9 Out-of-sample validation

* A strategy improvement must NOT be accepted solely because it performs better
  on the data used to design/tune it.
* Require: training/design period; validation period; out-of-sample evaluation;
  no look-ahead bias; no future-data leakage; reproducible replay.

## 17e.10 Adoption rule

* A new strategy/filter/AI enhancement should only replace or augment the
  baseline when evidence demonstrates **meaningful improvement while respecting
  risk constraints**.

## 17e.11 Distinctions preserved

* V1 (Phase 6) = completed/stable paper-trading infrastructure.
* Phase 7 = research & evaluation (this discipline).
* Future AI decision support = advisory layer being evaluated (PLANNED).
* Future real broker integration = separate, explicitly controlled capability
  (disabled by default).

---

# 18. Analytics

Future analytics should include:

* Total P&L
* Realized P&L
* Unrealized P&L
* Win rate
* Loss rate
* Average winning trade
* Average losing trade
* Profit factor
* Maximum drawdown
* Exposure
* Number of trades
* Average holding period
* Strategy-level performance

Additional metrics can be added later.

---

# 19. Logging

The application should provide structured and useful logging.

Logs should include:

* Application events
* Orders
* Fills
* Risk decisions
* Errors
* API events
* Strategy signals
* Portfolio changes

Logs must never expose:

* API keys
* Passwords
* Access tokens
* Secrets

---

# 20. Error Handling

The system should fail safely.

Examples:

```text
Market API unavailable
        |
        v
Do not generate unsafe execution
```

```text
Risk check fails
        |
        v
Order rejected
        |
        v
No execution
```

```text
Invalid market data
        |
        v
Reject/ignore invalid data
        |
        v
Do not corrupt portfolio state
```

External API errors must be separated from business logic errors.

---

# 21. Future Real Broker Integration

Real broker integration is explicitly outside the initial implementation.

If introduced later:

```text
Broker
   |
   +-- PaperBroker
   |
   +-- RealBroker
```

The RealBroker implementation must:

* Be separately tested
* Have explicit configuration
* Require explicit activation
* Use secure credentials
* Enforce RiskManager
* Maintain audit logs
* Provide clear failure handling
* Never be silently enabled

A live-trading flag alone should not be considered sufficient safety.

---

# 22. AI Coding Agent Rules

Any AI coding agent working on this repository, including:

* OpenCode
* Claude
* Codex
* Other coding agents

must follow these rules.

### Before making changes

1. Read `PROJECT_PLAN.md`.
2. Inspect the current repository.
3. Check `git status`.
4. Understand existing implementation.
5. Do not delete working functionality unnecessarily.

### During implementation

1. Follow the architecture in this file.
2. Keep modules focused.
3. Avoid unnecessary dependencies.
4. Do not hard-code secrets.
5. Do not introduce live trading.
6. Do not bypass RiskManager.
7. Maintain tests.
8. Use Python 3.13-compatible code.

### After implementation

1. Run tests.
2. Run the application.
3. Check imports.
4. Check `git diff`.
5. Check `git status`.
6. Report files changed.
7. Report test results.
8. Do not commit or push unless explicitly instructed.

### Architecture changes

If a major design decision changes:

1. Explain why.
2. Ask for approval when appropriate.
3. Update `PROJECT_PLAN.md`.
4. Record the decision in the Change Log.

---

# 23. Git Workflow

Recommended workflow:

```text
Make change
    |
    v
Run tests
    |
    v
Review git diff
    |
    v
Review git status
    |
    v
Commit
    |
    v
Push
```

Coding agents must not automatically commit or push.

Suggested commit style:

```text
Initial project setup
Add master project architecture
Implement Phase 1 models
Implement paper broker
Implement portfolio tracking
Implement risk management
Add Phase 1 tests
Integrate market data provider
Add strategy engine
Add AI analysis
Add backtesting
```

---

# 24. Current Repository Status

## Verified

* Python 3.13.14 installed
* Virtual environment `.venv` exists
* Virtual environment can be activated
* Git repository initialized
* GitHub remote configured
* GitHub authentication working
* Initial Git commit exists
* `master` branch tracks `origin/master`
* `.gitignore` covers `.env`
* `src/main.py` verified as executable
* Latest checkpoint: Phase 6 documentation finalization (`docs: finalize Phase 6 and F&O Paper Trading V1`) committed on top of base `302bf35`; HEAD == origin/master, working tree clean
* Full test suite: **561 passed** (offline, deterministic, 0 skipped / 0 xfailed)
* Acceptance replay: **30/30** V1 acceptance criteria passing (WS 6.7)
* Execution boundary: **paper-only** — `is_live=False` everywhere; no real-broker code; no live order placement

## Current Entry Point

```text
python src/main.py
```

Expected initial output:

```text
F&O AI Paper Trading System
```

## Current Phase

**COMPLETE — Phase 6 (Paper Trading V1) · F&O Paper Trading V1 IMPLEMENTATION
COMPLETE · V1 STATUS: READY.** Strategy/backtest/research work is done; WS 6.1–6.7
are implemented, committed and pushed (see §17d and the Change Log). WS 6.6
(session operations / monitoring): logging, health checks, reports
(`build_report`/`report_from_snapshot`, HTML output) and the operator CLI
(`scripts/paper_session_report.py`) are implemented with 15 monitoring tests.
Real-data execution is gated on `UPSTOX_ACCESS_TOKEN`; offline smoke tests are
provided.

Status: **PHASE 6 COMPLETE — F&O PAPER TRADING V1 IMPLEMENTATION COMPLETE — V1
STATUS: READY** — see §17b, §17c–17d, the data-layer notes above, the Change Log,
`ACTIVITY_LOG.md`, and `scripts/research_real_data.py`. Real historical research
runs when the user supplies `UPSTOX_ACCESS_TOKEN` (read-only historical data
only). The live/current-data paper-session loop is implemented
(`services/paper_session.py`, WS 6.4b) with stop-loss (WS 6.4), persistence
(WS 6.5), monitoring/ops (WS 6.6), 30 offline acceptance-replay tests (WS 6.7)
and an architecture diagram (`docs/architecture/architecture.svg`). State of
record at V1 READY: **563/563 committed tests passing (0 skipped, 0 xfailed),
30/30 V1 acceptance criteria passing, paper-only execution boundary (`is_live=False`
everywhere, no real-broker code, no live order placement), deterministic session
runtime, persistence/recovery, session monitoring, acceptance replay, no live
broker execution.** `FNO_PAPER_*` env wiring for the five scaffolded fields is
deferred to **Phase 7** (out of V1 scope). **Phase 7 (post-V1) is
evaluation-first:** the MA(5,21) baseline is frozen, and any regime-aware or AI
candidate must clear the discipline in §17e (the 10-Sep-2026 real-data paper
replay loss of ~₹194.68 is an evaluation observation, not a reason to change the
algorithm). Live trading remains explicitly out of scope.

---

# 25. Definition of Done — Phase 1

Phase 1 is complete when:

* [x] Project structure exists
* [x] Configuration works
* [x] Core models work
* [x] Data provider interface exists
* [x] Sample provider works
* [x] Broker interface exists
* [x] PaperBroker works
* [x] Portfolio works
* [x] P&L calculations work
* [x] RiskManager works
* [x] Logging works
* [x] `.env.example` exists
* [x] No secrets are committed
* [x] Tests cover core functionality
* [x] All tests pass
* [x] `python src/main.py` works
* [x] README is updated
* [x] Git diff reviewed
* [x] Phase 1 committed
* [x] Phase 1 pushed to GitHub

---

# 26. Definition of Done — Phase 2

Phase 2 is complete when:

* [x] Real market-data provider is integrated
* [x] API credentials are environment-based
* [x] No credentials are committed
* [x] Instrument data is normalized
* [x] Market prices are normalized
* [x] API failures are handled
* [x] Rate limits are respected
* [x] Real market data can be consumed by the application
* [x] Paper trading continues to use PaperBroker
* [x] No real orders can be placed
* [x] First deterministic strategy is implemented
* [x] Strategy tests exist
* [x] Integration tests exist
* [x] Backtesting harness implemented (deterministic engine, execution assumptions, metrics, datasets, offline demo)
* [x] Historical-data CLI/tooling (deferred to Phase 3; delivered as `scripts/acquire_dataset.py`, `scripts/upstox_smoke_test.py`, `scripts/research_real_data.py`)
* [x] Documentation is updated

---

# 27. Definition of Done — Phase 3

Phase 3 is complete when:

* [ ] AI service abstraction exists
* [ ] AI provider can be configured securely
* [ ] AI analysis is structured
* [ ] AI output is logged safely
* [ ] AI cannot bypass RiskManager
* [ ] AI cannot directly place broker orders
* [ ] AI-assisted signals are testable
* [ ] Documentation is updated

---

# 28. Definition of Done — Phase 4

Phase 4 is complete when:

* [x] Historical data can be loaded (read-only Upstox adapter + dataset store)
* [x] Strategies can be replayed (`BacktestEngine` + research harness)
* [x] Risk rules are applied during backtests (risk gating identical to paper trading)
* [x] Simulated execution works (paper-only `BacktestBroker`/`PaperBroker`)
* [x] Commission is modeled (commission rate/fixed + `cost_schedule`)
* [x] Slippage is modeled (slippage rate + `execution` assumptions)
* [x] P&L is calculated
* [x] Drawdown is calculated
* [x] Performance statistics are generated (`BacktestResult`, `PerformanceMetrics`)
* [x] Look-ahead bias is avoided (`bars[:i+1]` prefix per bar, asserted in tests)
* [x] Backtest results are reproducible (bar-timestamp fills, `Decimal`, deterministic datasets)

---

# 29. Architectural Decision Principles

When choosing between implementations, prefer:

1. Simplicity
2. Testability
3. Clear interfaces
4. Separation of concerns
5. Safety
6. Maintainability
7. Extensibility
8. Deterministic behavior
9. Minimal dependencies

Avoid premature optimization.

Avoid unnecessary frameworks.

Avoid tightly coupling the system to one API vendor.

Avoid putting business logic inside UI code.

Avoid putting strategy logic inside the broker.

Avoid putting risk logic inside individual strategies.

---

# 30. Project North Star

The final system should conceptually look like:

```text
                 +----------------------+
                 |  REAL MARKET DATA    |
                 |       APIs           |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |   DATA PROVIDERS     |
                 | Normalized Internal  |
                 |      Models          |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |  STRATEGY ENGINE     |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |    AI ANALYSIS       |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |    RISK MANAGER      |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |    PAPER BROKER      |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 |     PORTFOLIO        |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | ANALYTICS / REPORTS  |
                 +----------------------+
```

The system should be capable of using **real market information while remaining completely paper-trading-only**.

---

# 31. Change Log

## 2026-09-11 — Phase 7 roadmap: strategy evaluation & regime-aware discipline (docs only)

* Documentation-only. No `src/`, `tests/` or trading behavior changed.
* §9 (AI Architecture) updated to Phase 7 (post-V1, advisory-only, PLANNED).
* §17d Phase 7 rewritten as an **evaluation-first** sequence; new §17e
  (Strategy Evaluation & Improvement Discipline) added: frozen MA(5,21)
  baseline, no single-session algorithm changes, historical multi-session
  evaluation, baseline metrics, market-regime analysis, regime-aware hypotheses
  (not implemented), advisory AI decision support with a hard safety boundary,
  baseline-vs-enhanced comparison, out-of-sample validation, and the adoption
  rule. The 10-Sep-2026 real-data paper replay loss (~₹194.68) is documented as
  an evaluation observation, not a trigger for algorithm change.
* "Current Phase" state-of-record updated to 563/563 committed tests and the
  Phase 7 evaluation-first framing.

## 2026-09-11 — Phase 6 WS 6.6: Session operations / monitoring

* `services/session_monitoring.py` (commit `00ed8ed`): operator-facing
  observability for `PaperSession` — `SessionHealth`/`health()` (live counters,
  cash, mark-to-market equity, open-quantity posture), `SessionReport`/
  `build_report`/`report_from_snapshot` (fees, ledger, equity curve, win/loss
  stats; built live or offline from a stored snapshot), deterministic operator
  logging (`log_results`/`log_health`), and `report_to_dict` / `report_to_html` /
  `write_html_report` reusing `research/report.py` CSS so session pages look
  consistent with research reports.
* `scripts/paper_session_report.py` (commit `00ed8ed`): offline CLI that loads a
  `paper_state/` payload via `load_session`, prints a text summary, and writes
  `--html` / `--json` reports; the operator-level "scheduled run" seam.
* `docs/architecture/architecture.svg` (commit `00ed8ed`): hand-authored layered
  SVG diagram covering data → strategy → risk → broker → portfolio →
  session → persistence + monitoring → operator CLI → tests.
* 15 new monitoring tests (`tests/test_session_monitoring.py`); full suite
  **561 passing offline**.
* Docs refreshed: `ARCHITECTURE.md` (§1/§4/§5/§16/§17/§18/§19 + diagram
  reference), this plan (§17b/§17c/§17d/Current Phase), `PAPER_TRADING_V1.md`,
  `PROGRESS.md`, `ACTIVITY_LOG.md`. Remaining PLANNED items are `FNO_PAPER_*`
  env wiring and live/streaming quotes.

## 2026-09-11 — Phase 6 delivery (WS 6.2–6.7) + WS 6.1 documentation alignment

* Paper-trading domain completion (WS 6.2, `e853667`): `PaperAccount`, long-only
  policy, cash/duplicate guards.
* V1 risk-based position sizing (WS 6.3, `b4f8129`): `RiskBasedPositionSizer`.
* Automatic 2% stop-loss (WS 6.4, `75d3a44`) + deterministic paper-session runtime
  (WS 6.4b, `b05a17c`): `services/paper_session.py`.
* State persistence / recovery (WS 6.5, `118e976`): `persistence/session_store.py`,
  git-ignored `paper_state/`.
* Offline acceptance replay (WS 6.7, `0e5ce1c`): 30 tests covering all §13
  criteria; full suite **546 passing**.
* WS 6.1 doc alignment (this change): `PROJECT_PLAN.md` §17c/§17d/Current Phase,
  `docs/trading/PAPER_TRADING_V1.md` and `docs/architecture/ARCHITECTURE.md`
  refreshed to the implemented state; remaining PLANNED items were WS 6.6
  monitoring (delivered later the same day — see the WS 6.6 Change Log entry),
  `FNO_PAPER_*` env wiring, and live/streaming quotes.

## 2026-09-08 — Paper V1 scaffolding + Cloudflare-safe Upstox transport + architecture docs

* HTTP transport hardening (Cloudflare resolution):
  * `utils/http.py` — on Windows the system now shells out to `curl.exe`
    (schannel TLS) because the stdlib `urllib` TLS fingerprint is blocked by the
    Cloudflare WAF in front of `api.upstox.com` (HTTP 403 / error 1010
    browser-signature-banned); non-Windows falls back to `urllib`. Public
    interface (`http_request`/`http_get`/`HttpResponse`/`HttpError`,
    `is_retryable_status`) unchanged. Commits `d0bd276`
    (`fix: encode upstox historical data request path`) and `fc03a08`
    (`fix: use curl transport for Upstox historical data`).
* Paper-session configuration scaffolding (commit `4518dda`,
  `chore: add paper trading config scaffolding`): five additive `PaperSettings`
  fields — `paper_interval`, `paper_lookback_days`, `paper_risk_per_trade_pct`,
  `paper_stop_loss_pct`, `paper_state_dir` — with defaults only. **CONFIGURED-
  SCAFFOLDED**: not read by any code and not wired into `load_settings()`, so they
  change no runtime behavior.
* Paper Trading V1 contract (commit `59d831d`,
  `docs: define paper trading v1 specification`): `docs/trading/PAPER_TRADING_V1.md`
  — complete V1 specification (5-minute completed candles, NIFTY 50 index,
  long-only, MA(5,21), 2% stop-loss, 1% risk-per-trade sizing, ₹1,00,000 virtual
  capital, JSON snapshot persistence) with 30 acceptance criteria. At the time V1
  itself remained **PLANNED / NOT IMPLEMENTED** (see §17c); it has since been
  delivered by WS 6.2–6.7 (see the 2026-09-11 Change Log entry).
* Architecture document (commit `baeab01`,
  `docs: add system architecture`): `docs/architecture/ARCHITECTURE.md` — 20-section,
  source-verified architecture with IMPLEMENTED / CONFIGURED-SCAFFOLDED / PLANNED
  statuses and explicit safety boundaries.
* Full suite: **334 passed** (offline, deterministic); `master` in sync with
  `origin/master` at `baeab01`; working tree clean.

## 2026-09-07 (evening) — Real-data baseline research pipeline

* Implemented a reproducible real-data research study for NIFTY 50 daily bars:
  * `research/real_data.py` — `run_real_data_research(...)` orchestrates
    acquisition-agnostic analysis: dataset validation, descriptive statistics,
    in-sample / out-of-sample split, full-period buy-and-hold benchmark,
    parameter sensitivity on in-sample data (small enumerated grid), rolling
    walk-forward OOS evaluation, and date-based regime slices. Uses the existing
    `MovingAverageCrossStrategy` with locked baseline parameters (fast=5,
    slow=21) and the existing illustrative cost/slippage schedules.
  * `scripts/research_real_data.py` — CLI that either fetches real NIFTY 50
    daily data via the read-only Upstox adapter (requires `UPSTOX_ACCESS_TOKEN`)
    or runs `--smoke` with deterministic synthetic data for offline validation.
    Produces a labelled HTML report under `reports/` and persists datasets under
    `datasets/` (both git-ignored). Exits with code 2 when no token is supplied,
    never fabricating data.
  * `tests/test_real_data_research.py` — 7 tests: dataset statistics, end-to-end
    pipeline run on synthetic data, deterministic repeatability, OOS separation,
    CLI token-gating, and offline smoke report generation.
  * README + PROJECT_PLAN updated.
* Full suite: **331 passed** (324 + 7 new).

## 2026-09-07 (afternoon) — Phase 3 audit + Upstox history readiness

* Audit fixes (commit message intended: `feat: audit phase 3 and add upstox historical data readiness`):
  * `BacktestResult`/`PerformanceMetrics` gained `slippage_cost` and a
    `transaction_costs` property (commission + slippage); engine accumulates
    per-fill adverse-price cost; hand-verified triples.
  * `ExperimentConfig` folds `backtest_settings` (canonical JSON of
    commission/slippage/risk scalars) into the deterministic `config_hash`.
  * `canonical_interval` is case-sensitive for the minute/month pair (`1m` vs
    `1M`); casing/whitespace still tolerated for all other tokens.
* Historical-data readiness (vendor-neutral, read-only, offline-safe):
  * `data/intervals.py` — canonical interval tokens + Upstox/Kite mappings.
  * `data/upstox_provider.py` — `UpstoxHistoricalDataProvider`, historical OHLCV
    read-only, typed error mapping, candle validation, naive-IST normalization.
  * `data/dataset_store.py` — local CSV+JSON dataset cache with SHA-256
    `data_hash`; `datasets/` git-ignored.
  * `data/validation.py` — report-only dataset quality checks.
  * `config/settings.py` — `UpstoxSettings` (`UPSTOX_*`).
  * `scripts/upstox_smoke_test.py` — opt-in read-only connectivity check.
  * `.env.example` — Upstox section; tests added for all new modules.
  * Full suite: **298 tests pass** (201 + 97 new); reports regenerated.

## 2026-09-07

* Strategy Research & Robustness framework implemented (see §17b), commit
  message intended: `feat: add strategy research and robustness framework`.
  * `research/` package: `costs.py`, `execution.py`, `regimes.py`, `split.py`,
    `walkforward.py`, `sensitivity.py`, `benchmark.py`, `metrics.py`,
    `experiment.py`, `report.py`, `__main__.py` (offline demo).
  * Backtest wiring: `BacktestConfig.cost_schedule` + `BacktestConfig.execution`
    (duck-typed, backward compatible); `closes_to_bars` public alias in
    `backtest/datasets.py`.
  * `scripts/generate_research_report.py` produces `reports/research_report.html`
    (git-ignored).
  * Full suite: 201 tests pass (156 + 45 new); test report regenerated.
  * Methodology: costs/execution charged before any claim, fixed parameters,
    strictly disjoint in/out-of-sample splits, walk-forward OOS-only evaluation,
    explicit (non-optimizing) sensitivity, gross benchmark, illustrative-cost
    disclaimer throughout.
* Phase 2 backtest harness implemented (completing Phase 2):
  * `src/fno_ai_paper_trading/backtest/` package added: `config.py`
    (`BacktestConfig` execution assumptions), `engine.py` (`BacktestEngine` +
    paper-only `BacktestBroker` that stamps fills with bar timestamps),
    `result.py` (`BacktestResult`, `EquityPoint` with full P&L / win-rate /
    profit-factor / drawdown metrics), `datasets.py` (deterministic,
    hand-verified fixtures incl. long & short round trips, winning, losing,
    drawdown, multiple trades, no-trade), `__main__.py` (offline demo).
  * Correctness guarantees: no look-ahead (`bars[:i+1]` per bar), deterministic
    reproducibility (asserted in tests), round-trip trade statistics, positions
    left open at end-of-data are reported with unrealized P&L (no phantom
    closing trade), risk limits identical to live paper trading.
  * `src/main.py` now also runs a backtest demo (paper-only).
  * `tests/test_backtest.py` added: exact-cost P&L (slippage 1% → 7.70,
    commission → 0.68997), drawdown, chronology, signal timing, signed realized
    P&L regression, determinism-repeatability, end-of-data open position, risk
    rejection vs disabled-risk, paper-only offline safety.
  * Full suite: 156 tests pass (134 + 22 new); test report regenerated.
* Phase 2 first increment implemented:
  * `MarketDataProvider` ABC extended (`get_instrument`, `get_quote`,
    `get_market_session`, `get_historical_ohlcv`).
  * Read-only Kite Connect v3 adapter (`data/kite_provider.py`) with
    `FNO_KITE_*` environment credentials, retries and a typed `MarketDataError`
    hierarchy; `KiteSettings` added to config.
  * NSE market hours helper (`data/market_hours.py`) with session phases and
    holiday/weekend/next-open handling.
  * Models extended: `MarketPhase`, `MarketQuote`, `MarketSession`,
    `open_interest`, `exchange`/`exchange_token`.
  * Deterministic strategy engine: `Strategy` ABC, `StrategyEngine`,
    `MovingAverageCrossStrategy`.
  * `StrategyService` evaluates signals and submits paper orders only.
  * `main.py` now runs the Phase 1 demo + a strategy demo (paper-only).
  * Discovered and fixed latent Phase 1 defect: `Trade.realized_pnl` is now
    signed (losses allowed) rather than forced non-negative.
  * All 134 unit tests pass; test report generated.

## 2026-09-06

* Project initialized as `fno-ai-paper-trading`.
* Python 3.13.14 verified.
* Git repository initialized.
* GitHub remote configured.
* GitHub authentication verified.
* Initial project commit created.
* Working `src/main.py` verified.
* `.gitignore` verified to exclude `.env`.
* Master project architecture established.
* Phase 1 defined as the foundation layer.
* Real market-data API integration explicitly planned for Phase 2.
* Real broker execution explicitly excluded from initial phases.
* AI integration planned for Phase 3.
* Backtesting planned for Phase 4.