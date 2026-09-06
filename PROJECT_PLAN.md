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

---

# 5. Project Development Phases

## Phase 1 — Foundation

### Status

**IN PROGRESS**

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

### Planned

Integrate real market-data APIs.

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

AI will be introduced in Phase 3.

AI should primarily provide:

* Market analysis
* Context
* Signal assistance
* Candidate trade ranking
* Explanation
* Risk/context commentary
* Structured reasoning

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

Backtesting begins in Phase 4.

The backtesting engine should reuse the same core concepts where practical.

Preferred architecture:

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

Backtesting should account for:

* Commission
* Slippage
* Position sizing
* Entry/exit rules
* Available cash
* Risk limits
* Drawdown
* P&L

The backtesting engine must avoid look-ahead bias.

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

## Current Entry Point

```text
python src/main.py
```

Expected initial output:

```text
F&O AI Paper Trading System
```

## Current Phase

**Phase 1 — Foundation**

Status:

**IN PROGRESS**

---

# 25. Definition of Done — Phase 1

Phase 1 is complete when:

* [ ] Project structure exists
* [ ] Configuration works
* [ ] Core models work
* [ ] Data provider interface exists
* [ ] Sample provider works
* [ ] Broker interface exists
* [ ] PaperBroker works
* [ ] Portfolio works
* [ ] P&L calculations work
* [ ] RiskManager works
* [ ] Logging works
* [ ] `.env.example` exists
* [ ] No secrets are committed
* [ ] Tests cover core functionality
* [ ] All tests pass
* [ ] `python src/main.py` works
* [ ] README is updated
* [ ] Git diff reviewed
* [ ] Phase 1 committed
* [ ] Phase 1 pushed to GitHub

---

# 26. Definition of Done — Phase 2

Phase 2 is complete when:

* [ ] Real market-data provider is integrated
* [ ] API credentials are environment-based
* [ ] No credentials are committed
* [ ] Instrument data is normalized
* [ ] Market prices are normalized
* [ ] API failures are handled
* [ ] Rate limits are respected
* [ ] Real market data can be consumed by the application
* [ ] Paper trading continues to use PaperBroker
* [ ] No real orders can be placed
* [ ] First deterministic strategy is implemented
* [ ] Strategy tests exist
* [ ] Integration tests exist
* [ ] Documentation is updated

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

* [ ] Historical data can be loaded
* [ ] Strategies can be replayed
* [ ] Risk rules are applied during backtests
* [ ] Simulated execution works
* [ ] Commission is modeled
* [ ] Slippage is modeled
* [ ] P&L is calculated
* [ ] Drawdown is calculated
* [ ] Performance statistics are generated
* [ ] Look-ahead bias is avoided
* [ ] Backtest results are reproducible

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