# F&O AI Paper Trading System

A modular Python system for paper-trading Futures & Options instruments.

**Phase 1 scope:** configuration, data models, in-memory data provider, pre-trade
risk management, a simulated paper broker, portfolio accounting and a test suite.

**Phase 2 scope:** real market data behind the `MarketDataProvider` abstraction
(Zerodha Kite Connect v3 adapter, market hours, typed errors, rate-limit/retry
handling) plus the first deterministic strategy (moving average crossover) and a
strategy service that executes signals through the paper broker only.

> **Safety guarantee:** This system is paper-trading only. No code in this
> repository places live orders, contacts a broker API, or executes real-money
> trades. The paper broker is completely isolated from any external service;
> the market-data vendor adapter is read-only.

---

## Architecture

```
src/
  main.py                             # Entry point — Phase 1 + Phase 2 paper demos
  fno_ai_paper_trading/
    config/settings.py                # Environment-based PaperSettings + KiteSettings
    models/                           # Domain objects: Instrument, Order, Fill,
                                      #   Position, Trade, MarketPrice, MarketQuote,
                                      #   MarketSession, enums
    data/
      provider.py                     # MarketDataProvider ABC
      mock_provider.py                # InMemoryMarketDataProvider (deterministic samples)
      kite_provider.py                # Kite Connect v3 adapter (read-only market data)
      market_hours.py                 # NSE session hours / market status helpers
      errors.py                       # Typed MarketDataError hierarchy
    strategies/
      base.py                         # Strategy ABC + SignalResult
      moving_average_cross.py         # First deterministic strategy
      engine.py                       # Replays bars through a strategy
    risk/
      manager.py                      # RiskManager — pre-trade quantity/notional/loss checks
    broker/
      base.py                         # Broker ABC (is_live = False guard)
      paper_broker.py                 # PaperBroker — simulated fills, slippage, commission
    portfolio/
      portfolio.py                    # Cash, positions, average entry price, P&L
    backtest/                         # Reserved for Phase 4
    services/
      trading_service.py              # Orchestrates data → risk → broker → portfolio
      strategy_service.py             # Signals → risk → paper broker (paper orders only)
    utils/
      logging.py                      # Structured logging setup (no secrets in output)
      functions.py                    # Decimal helpers, id generation, notional calc
      http.py                         # Dependency-free urllib HTTP transport
      retry.py                        # Exponential-backoff retry helper
tests/                                # Unit tests — no external dependencies
```

All money/price fields use `decimal.Decimal` throughout. Interfaces (ABCs) are
stable, so backtesting (remaining Phase 2) and AI (Phase 3) plug in without
touching the core accounting/risk/strategy paths.

---

## Setup

### Prerequisites

* Python 3.13+

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Only two packages are installed:

| Package | Purpose |
|---|---|
| `python-dotenv` | Load `.env` configuration files |
| `pytest` | Run the test suite |

### 3. (Optional) Create your `.env` file

```bash
copy .env.example .env
```

Edit `.env` to change defaults (capital, loss limits, commission rates, etc.).
If `.env` is absent, sensible defaults are used.

---

## Running the application

```bash
python src/main.py
```

This prints the system title and runs two paper-only demonstrations:

1. **Phase 1 walkthrough:** creates three sample instruments (a future + two
   options) and submits two buy orders through risk manager → paper broker →
   portfolio.
2. **Phase 2 strategy demo:** replays a deterministic 55-bar series through the
   moving-average crossover strategy, submitting paper orders when the fast MA
   crosses the slow MA (one BUY then one SELL in the sample series), then prints
   final cash and portfolio value.

Both demos finish with the same guarantee: no real orders are placed.

Credentials (if you configure Kite) enable the read-only market-data adapter,
but the demos always use the deterministic in-memory provider, so `python
src/main.py` works offline with zero credentials.

---

## Running tests

```bash
pytest
```

Or for verbose output:

```bash
pytest -v
```

All tests run locally with zero network calls and no external dependencies
beyond `pytest`.

### What the tests cover

| Test file | Covers |
|---|---|
| `tests/test_models.py` | Instrument, Order, Fill, Position, Trade, MarketPrice, MarketQuote, MarketSession — creation and validation |
| `tests/test_risk.py` | Position quantity limits, notional limits, daily loss limit, unknown instrument |
| `tests/test_broker.py` | PaperBroker fill, slippage, commission, status transitions, cancellation |
| `tests/test_portfolio.py` | Cash changes, position tracking, average entry price, realized/unrealized P&L, total value |
| `tests/test_market_hours.py` | NSE session phases, weekend/holiday closed state, next-open rollover |
| `tests/test_strategies.py` | Moving-average crossover signals, engine replay, determinism |
| `tests/test_strategy_service.py` | Signals → risk → paper broker integration; risk gating; hold = no order |
| `tests/test_kite_provider.py` | Kite adapter mapping, CSV master, candles, sessions, retries and typed errors (mocked HTTP) |
| `tests/test_utils_http_retry.py` | HTTP transport, retry/backoff behaviour |

---

## Configuration reference

All variables (prefixed `FNO_`) are read from environment variables or `.env`.
Defaults are shown next to each variable in `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `FNO_ENVIRONMENT` | `development` | Run environment (`development`, `test`, `paper`) — all are paper-only |
| `FNO_PAPER_INITIAL_CAPITAL` | `100000` | Starting paper capital (rupees) |
| `FNO_PAPER_MAX_POSITION_QUANTITY` | `75` | Max absolute position quantity per instrument |
| `FNO_PAPER_MAX_ORDER_NOTIONAL` | `250000` | Max single order notional |
| `FNO_PAPER_MAX_DAILY_LOSS` | `10000` | Risk manager rejects further orders beyond this realized loss |
| `FNO_PAPER_COMMISSION_RATE` | `0.0003` | Commission as fraction of notional |
| `FNO_PAPER_COMMISSION_FIXED` | `0` | Flat commission per fill |
| `FNO_PAPER_SLIPPAGE_RATE` | `0.001` | Price slippage applied to simulated fills |
| `FNO_LOG_LEVEL` | `INFO` | Logging verbosity |
| `FNO_KITE_API_KEY` | *(empty)* | Kite Connect API key (read-only market data)—**never commit a real key** |
| `FNO_KITE_ACCESS_TOKEN` | *(empty)* | Kite Connect access token—**never commit a real token** |
| `FNO_KITE_BASE_URL` | `https://api.kite.trade` | Kite endpoint base URL |
| `FNO_KITE_TIMEOUT_SECONDS` | `10` | HTTP timeout for Kite calls |
| `FNO_KITE_MAX_RETRIES` | `3` | Retries on rate-limit/5xx/network errors |

Never commit real values to `.env` — the file is git-ignored.

---

## Safety limitations (Phase 2)

- **No live execution:** the `PaperBroker.is_live` property is hard-coded to
  `False`. No code path in the system contacts an external broker or API. The
  strategy service submits paper orders only.
- **No secrets in code:** all credentials belong in `.env` (git-ignored) or
  environment variables, never in source. The Kite adapter refuses to run
  without credentials and raises a typed `ProviderConfigurationError`.
- **No AI/strategy claims:** the strategy engine is deterministic (moving
  average crossover). AI analysis is planned for Phase 3 and will sit behind an
  interface with non-autonomous execution.
- **Market data is offline-safe:** the only provider exercised by the demos is
  `InMemoryMarketDataProvider`. The Kite Connect adapter is a read-only market
  data client, unit-tested against mocked HTTP; it never places orders and it
  is exercised live only when `FNO_KITE_API_KEY` / `FNO_KITE_ACCESS_TOKEN` are
  configured by the user.

---

## Future phases

| Phase | Scope |
|---|---|
| **Phase 2 (done)** | Strategy engine, real market-data provider interface, moving-average crossover strategy, read-only Kite Connect adapter |
| **Phase 2 (remaining)** | Backtesting harness, historical-data CLI/tooling |
| **Phase 3** | AI analysis/explainability (behind an interface, never autonomous execution) |
| **Phase 4** | Real broker adapter behind an interface, required to remain disabled by default |

---

## License

Internal / proprietary — not distributed.