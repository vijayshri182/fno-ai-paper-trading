# F&O AI Paper Trading System

A modular Python system for paper-trading Futures & Options instruments.

**Phase 1 scope:** configuration, data models, in-memory data provider, pre-trade
risk management, a simulated paper broker, portfolio accounting and a test suite.

**Phase 2 scope:** real market data behind the `MarketDataProvider` abstraction
(Zerodha Kite Connect v3 adapter, market hours, typed errors, rate-limit/retry
handling), the first deterministic strategy (moving average crossover), a
strategy service that executes signals through the paper broker only, and a
deterministic backtest harness for offline research.

> **Safety guarantee:** This system is paper-trading only. No code in this
> repository places live orders, contacts a broker API, or executes real-money
> trades. The paper broker is completely isolated from any external service;
> the market-data vendor adapter is read-only. The backtest engine executes
> through the same paper broker only — it needs no credentials and makes no
> network calls.

---

## Architecture

```
src/
  main.py                             # Entry point — Phase 1, Phase 2 strategy, backtest demos
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
    backtest/
      config.py                       # BacktestConfig — explicit execution assumptions
      engine.py                       # BacktestEngine + paper-only fill broker
      result.py                       # BacktestResult + EquityPoint (metrics)
      datasets.py                     # Deterministic historical datasets
      __main__.py                     # Offline backtest demo (python -m ...)
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
stable, so additional strategies, historical-data tooling (Phase 3) and AI
(Phase 3) plug in without touching the core accounting/risk/strategy paths.

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

This prints the system title and runs three paper-only demonstrations:

1. **Phase 1 walkthrough:** creates three sample instruments (a future + two
   options) and submits two buy orders through risk manager → paper broker →
   portfolio.
2. **Phase 2 strategy demo:** replays a deterministic 55-bar series through the
   moving-average crossover strategy, submitting paper orders when the fast MA
   crosses the slow MA (one BUY then one SELL in the sample series), then prints
   final cash and portfolio value.
3. **Backtest demo:** replays a deterministic 13-bar series with `fast=2`,
   `slow=3` through the backtest engine and prints the resulting performance
   metrics (final equity, total P&L, return, max drawdown, trade statistics,
   commission, profit factor).

All three demos finish with the same guarantee: no real orders are placed.

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
| `tests/test_backtest.py` | Backtest engine: exact-cost P&L, slippage/commission, drawdown, equity curve, no look-ahead, signal timing, long & short round trips, determinism, risk gating, end-of-data open positions, paper-only offline safety |

---

## Backtesting

The backtest harness replays historical OHLCV bars through a strategy and the
same `PaperBroker` used by live paper trading, producing performance metrics
and an equity curve.

```bash
python -m fno_ai_paper_trading.backtest
```

This runs the offline demo (fast/slow = 2/3 against a deterministic dataset) and
prints a summary. Programmatic use:

```python
from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.datasets import build_profitable_series
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy

bars = build_profitable_series(instrument)          # deterministic fixture
result = BacktestEngine().run(
    bars,
    MovingAverageCrossStrategy(fast=2, slow=3),
    BacktestConfig(initial_capital=100000, quantity=10),
)
print(result.final_equity, result.total_return_pct, result.profit_factor)
```

### Execution assumptions

All assumptions are explicit in `BacktestConfig` (defaults shown):

| Config key | Default | Meaning |
|---|---|---|
| `initial_capital` | `100000` | Starting equity |
| `quantity` | `1` | Size of every simulated order |
| `commission_rate` | `0.0003` | Commission as fraction of filled notional |
| `commission_fixed` | `0` | Flat commission per fill |
| `slippage_rate` | `0.001` | Adverse price slippage applied to each fill |
| `enable_risk_manager` | `True` | Apply the same `RiskManager` limits as live paper trading |
| `max_position_quantity` / `max_order_notional` / `max_daily_loss` | `75` / `250000` / `10000` | Risk-limit values used when the risk manager is enabled |

### Behavioural guarantees

- **No look-ahead:** at bar `i` the strategy sees only `bars[:i+1]`; orders
  generated at bar `i` fill at bar `i`'s close. The tests assert the strategy
  stays flat until the first signal bar.
- **Determinism:** fills carry the bar timestamp (not wall-clock time) and every
  cost is explicit `Decimal` arithmetic, so a given strategy + dataset always
  produces the identical result. Repeated-run equality is asserted in tests.
- **Round-trip trade statistics:** a trade is counted only when a position is
  closed (a `BUY` opening a long, then a `SELL` closing it — and vice versa for
  shorts). Gross profit/loss and win rate reflect closed round trips only.
- **Positions left open at the end of data are reported as-is** (with unrealized
  P&L in the last equity snapshot) — no phantom closing trade is manufactured.
- **Risk gating is identical to live paper trading:** orders pass through
  `RiskManager` first; a rejected order is skipped entirely.

### Metric definitions

- `total_pnl` / `total_return_pct` — net of all commission and slippage costs.
- `gross_profit` / `gross_loss` / `profit_factor` — based on closed round-trip
  realised P&L *before* costs; `profit_factor = gross_profit / |gross_loss|`
  (rendered as `Infinity` when there is no losing trade).
- `max_drawdown` / `max_drawdown_pct` — largest peak-to-trough decline in the
  equity curve.
- `equity_curve` — per-bar snapshots (`timestamp`, `equity`, `cash`,
  `unrealized_pnl`, `drawdown_from_peak`).

### Backtest datasets

`fno_ai_paper_trading.backtest.datasets` ships deterministic, hand-verified
OHLCV fixtures for correctness tests: `build_profitable_series`,
`build_losing_series`, `build_drawdown_series`, `build_multiple_trades_series`,
`build_no_trade_series`, and `build_short_profit_series` (a `SELL`-to-open short
closed by a `BUY`).

> **Disclaimer:** results from these fixtures illustrate mechanics, not
> profitability. Past performance does not guarantee future results.

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

## Safety limitations

- **No live execution:** the `PaperBroker.is_live` property is hard-coded to
  `False`. No code path in the system contacts an external broker or API. The
  strategy service and the backtest engine submit paper orders only.
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
- **Backtesting is offline and paper-only:** the engine instantiates its own
  paper broker and never reads credentials, connects to a network, or touches
  the Kite adapter. Backtest results reflect the configured execution
  assumptions only and are not a basis for real-money decisions.

---

## Future phases

| Phase | Scope |
|---|---|
| **Phase 2 (done)** | Strategy engine, real market-data provider interface, moving-average crossover strategy, read-only Kite Connect adapter, deterministic backtest harness |
| **Phase 3** | Historical-data CLI/tooling, AI analysis/explainability (behind an interface, never autonomous execution) |
| **Phase 4** | Real broker adapter behind an interface, required to remain disabled by default |

---

## License

Internal / proprietary — not distributed.