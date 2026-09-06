# F&O AI Paper Trading System

A modular Python system for paper-trading Futures & Options instruments.

**Phase 1 scope:** configuration, data models, in-memory data provider, pre-trade
risk management, a simulated paper broker, portfolio accounting and a test suite.

> **Safety guarantee:** This system is paper-trading only. No code in this
> repository places live orders, contacts a broker API, or executes real-money
> trades. The paper broker is completely isolated from any external service.

---

## Architecture

```
src/
  main.py                             # Entry point — runs a small paper demo
  fno_ai_paper_trading/
    config/settings.py                # Environment-based PaperSettings (Decimal-backed)
    models/                           # Domain objects: Instrument, Order, Fill,
                                      #   Position, Trade, MarketPrice, enums
    data/
      provider.py                     # MarketDataProvider ABC
      mock_provider.py                # InMemoryMarketDataProvider (deterministic samples)
    strategies/                       # Reserved for Phase 2
    risk/
      manager.py                      # RiskManager — pre-trade quantity/notional/loss checks
    broker/
      base.py                         # Broker ABC (is_live = False guard)
      paper_broker.py                 # PaperBroker — simulated fills, slippage, commission
    portfolio/
      portfolio.py                    # Cash, positions, average entry price, P&L
    backtest/                         # Reserved for Phase 2
    services/
      trading_service.py              # Orchestrates data → risk → broker → portfolio
    utils/
      logging.py                      # Structured logging setup (no secrets in output)
      functions.py                    # Decimal helpers, id generation, notional calc
tests/                                # Unit tests — no external dependencies
```

All money/price fields use `decimal.Decimal` throughout. Interfaces (ABCs) are
stable and ready for Phase 2 strategy and backtest additions.

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

This prints the system title and runs a small paper-trading demo that:

1. Loads settings from environment (or defaults).
2. Creates three sample instruments (a future + two options).
3. Submits two buy orders through the risk manager → paper broker → portfolio.
4. Prints final cash, unrealized P&L and total portfolio value.

No real orders are placed.

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
| `tests/test_models.py` | Instrument, Order, Fill, Position, Trade, MarketPrice — creation and validation |
| `tests/test_risk.py` | Position quantity limits, notional limits, daily loss limit, unknown instrument |
| `tests/test_broker.py` | PaperBroker fill, slippage, commission, status transitions, cancellation |
| `tests/test_portfolio.py` | Cash changes, position tracking, average entry price, realized/unrealized P&L, total value |

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

Never commit real values to `.env` — the file is git-ignored.

---

## Safety limitations (Phase 1)

- **No live execution:** the `PaperBroker.is_live` property is hard-coded to
  `False`. No code path in the system contacts an external broker or API.
- **No secrets in code:** all credentials belong in `.env` (git-ignored) or
  environment variables, never in source.
- **No AI/strategy claims:** no automated signal or strategy logic ships in
  Phase 1. The `strategies/` and `backtest/` packages are placeholders.
- **No network calls:** the only data provider is `InMemoryMarketDataProvider`.
  No HTTP/WebSocket connections are made anywhere in the codebase.

---

## Future phases

| Phase | Scope |
|---|---|
| **Phase 2** | Strategy engine, real market-data provider interface, backtesting harness |
| **Phase 3** | AI analysis/explainability (behind an interface, never autonomous execution) |
| **Phase 4** | Real broker adapter behind an interface, required to remain disabled by default |

---

## License

Internal / proprietary — not distributed.