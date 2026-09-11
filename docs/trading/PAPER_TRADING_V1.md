# Paper Trading V1 — Contract Specification

> Status: **CONTRACT — Phase 6 implemented** (work streams 6.2–6.7 delivered;
> WS 6.1 keeps this document in step with the repository).
> This document defines the agreed V1 contract. It was first written against commit
> `4518dda` (`chore: add paper trading config scaffolding`); the Phase 6 items marked
> *planned* then have since been implemented (see §2.4, §4–§14). Anything this
> document describes as *implemented* exists in code and is covered by the automated
> suite; anything still marked *planned* / *not implemented* holds **no code today**.

---

## 1. Purpose and scope

The purpose of Paper Trading V1 is to run the system's existing deterministic
strategy engine against **current market data** (completed 5-minute candles) and
fill simulated orders through the existing paper broker, so virtual capital, risk
and P&L evolve in near real time without any connection to a real broker.

The system is and remains **paper-trading only**. V1 adds a market-data-driven
execution loop; it never places a real order and never uses real money.

| Aspect | V1 scope |
|---|---|
| Data source | Upstox V3 historical-candle API (read-only), `5m` bars |
| Cadence | Completed 5-minute candles (never tick-by-tick) |
| Instrument | NIFTY 50 index (`NSE_INDEX\|Nifty 50`) |
| Direction | Long-only |
| Strategy | Moving-average crossover, `fast=5, slow=21` |
| Stop-loss | Fixed 2% from entry, enforced as an automatic paper exit |
| Risk per trade | 1% of current virtual equity |
| Capital | Virtual ₹1,00,000 (default) |
| Execution | `PaperBroker` simulation only; no broker order placement |
| Persistence | Session state persists across restarts via JSON snapshots under git-ignored `paper_state/` (WS 6.5) |

The historical backtest harness (`fno_ai_paper_trading.backtest`) and the research
framework (`fno_ai_paper_trading.research`) remain **separate** offline tools. V1's
live loop reuses the same domain objects, risk manager and paper broker, but is not
a backtest and cannot run offline against stored datasets.

---

## 2. Current implementation status

Statuses used throughout this document:

- **IMPLEMENTED** — exists in code, covered by tests.
- **CONFIGURED/SCAFFOLDED** — configuration or interface exists; no runtime behavior yet.
- **PLANNED / NOT IMPLEMENTED** — no code exists; described here as the target contract.

### 2.1 Foundation (IMPLEMENTED)

| Capability | Module(s) | Notes |
|---|---|---|
| Domain models | `models/enums.py`, `models/instruments.py`, `models/order.py`, `models/position.py`, `models/market.py`, `portfolio/account.py` | `Order`, `Fill`, `Position`, `Trade`, `MarketPrice`, `MarketQuote`, `MarketSession`, `PaperAccount`; `Order.transition()` enforces deterministic lifecycle transitions |
| Market-data abstraction | `data/provider.py` (`MarketDataProvider` ABC) | `get_ohlcv`, `get_historical_ohlcv`, `get_quote`, `get_market_price`, `get_last_price`, `get_market_session`, `get_instrument(s)` |
| In-memory provider | `data/mock_provider.py` (`InMemoryMarketDataProvider`) | Deterministic; offline test/demo provider |
| Paper broker | `broker/base.py`, `broker/paper_broker.py` (`PaperBroker`) | `is_live` hard-coded `False`; slippage + commission model; timestamped by wall clock |
| Portfolio accounting | `portfolio/portfolio.py` (`Portfolio`) | Cash, positions, average entry price, realized/unrealized P&L, `total_value`, `realized_pnl_today` |
| Pre-trade risk | `risk/manager.py` (`RiskManager`) | Position quantity, order notional, daily-loss limits |
| Orchestration | `services/trading_service.py` (`TradingService`), `services/strategy_service.py` (`StrategyService`) | Signal → risk → paper broker → portfolio |
| Strategy | `strategies/moving_average_cross.py` (`MovingAverageCrossStrategy`), `strategies/engine.py` | Deterministic, stateless; `fast/slow` cross of the close |
| NSE session clock | `data/market_hours.py` | NSE_TZ (UTC+05:30), pre-open 09:00, open 09:15–15:30, `market_session`, `is_market_open`, `next_open`, advisory `HOLIDAYS_2026` |

### 2.2 Market-data readiness (IMPLEMENTED, read-only)

| Capability | Module(s) | Notes |
|---|---|---|
| Canonical intervals | `data/intervals.py` | `5m` ∈ `CANONICAL_INTERVALS`; maps to Upstox `("minutes", 5)` |
| Upstox V3 adapter | `data/upstox_provider.py` (`UpstoxHistoricalDataProvider`) | **Read-only** historical candles only; no order API, no write endpoint, no live-execution path |
| Curated instruments | `data/instrument_registry.py` | `RESEARCH_INSTRUMENTS` includes NIFTY 50 (`NSE_INDEX\|Nifty 50`, `lot_size=1`, `tick_size=0.05`, `multiplier=1`) |
| Dataset persistence | `data/dataset_store.py` | `datasets/<name>.csv` + `.meta.json` with SHA-256 `data_hash`; `datasets/` git-ignored |
| Validation/reporting | `data/validation.py` | Report-only dataset checks; never repairs data |
| Acquisition/verification scripts | `scripts/acquire_dataset.py`, `scripts/upstox_smoke_test.py`, `scripts/research_real_data.py` | Opt-in, credential-gated, read-only; fail closed without `UPSTOX_ACCESS_TOKEN` |

### 2.3 Paper-session configuration (CONFIGURED/SCAFFOLDED)

The five additive `PaperSettings` fields below are declared at
`src/fno_ai_paper_trading/config/settings.py:57-62`. Three are consumed as
`PaperSession` defaults (`paper_interval`, `paper_risk_per_trade_pct`,
`paper_stop_loss_pct`); two are not yet consumed by any runtime
(`paper_lookback_days`, `paper_state_dir` — the persistence store uses the matching
constant `DEFAULT_STATE_DIR = "paper_state"`). None of the five are wired to
environment variables by `load_settings()` — no `FNO_PAPER_INTERVAL` etc. exist yet.

| Field | Default | Meaning |
|---|---|---|
| `paper_interval` | `"5m"` | Canonical bar interval token for the session loop |
| `paper_lookback_days` | `3` | Calendar days of bars to fetch on each update |
| `paper_risk_per_trade_pct` | `Decimal("0.01")` | 1% of current equity risked per trade |
| `paper_stop_loss_pct` | `Decimal("0.02")` | Fixed stop distance from entry price |
| `paper_state_dir` | `"paper_state"` | Planned git-ignored ledger/snapshot directory |

`FNO_PAPER_INITIAL_CAPITAL` (`100000`), `FNO_PAPER_MAX_POSITION_QUANTITY` (`75`),
`FNO_PAPER_MAX_ORDER_NOTIONAL` (`250000`), `FNO_PAPER_MAX_DAILY_LOSS` (`10000`),
`FNO_PAPER_COMMISSION_RATE` (`0.0003`), `FNO_PAPER_COMMISSION_FIXED` (`0`) and
`FNO_PAPER_SLIPPAGE_RATE` (`0.001`) are already wired in `load_settings()` and used
by `RiskManager`, the backtest harness and the demos.

### 2.4 Paper-session capabilities (Phase 6 delivery status)

| Capability | Status | Notes |
|---|---|---|
| Live 5-minute paper-session loop | **IMPLEMENTED** | `services/paper_session.py` (WS 6.4b): `--once`/`--loop` polling, environment guard, deterministic clock/fill-price seams; 38 session tests |
| Risk-based position sizing | **IMPLEMENTED** | `risk/sizer.py` `RiskBasedPositionSizer`; optional in `StrategyService` (BUY entries sized; fixed-quantity path intact) |
| Stop-loss | **IMPLEMENTED** | `risk/stop_loss.py` (`StopLossPolicy`/`enforce_stop`) via `TradingService.protective_exit` (WS 6.4); wired into the session (WS 6.4b); 41 stop tests |
| Long-only gating | **IMPLEMENTED** | `Portfolio.long_only`/`PaperAccount` reject `SELL`-to-open/oversell at `apply_fill`; session-level routing implemented (WS 6.4b): BUY ignored when already long, SELL when flat |
| Cash/leverage guard | **IMPLEMENTED** | Enforced by the `RiskBasedPositionSizer` capital bound (§6 rule 6): sized quantity never exceeds available cash; generic `Portfolio` accounting unchanged |
| Session state persistence | **IMPLEMENTED** | `persistence/session_store.py` (WS 6.5): JSON snapshots + SHA-256 `state_hash` under git-ignored `paper_state/`; 34 persistence tests |
| Env wiring for the five new fields | **NOT IMPLEMENTED** | `load_settings()` does not yet read `FNO_PAPER_*` for the five scaffolding fields |
| Live/streaming quotes | **NOT IMPLEMENTED** | The Upstox adapter derives quotes from the historical endpoint; there is no tick/websocket path |

> **Phase numbering note:** `PROJECT_PLAN.md` §17d and `ACTIVITY_LOG.md` now
> both assign **Phase 6** to Paper Trading V1; WS 6.1 aligns all three documents.

---

## 3. V1 trading assumptions

The following are the agreed, fixed assumptions for V1 (per the scaffolded config):

| # | Assumption | Value / rule |
|---|---|---|
| 1 | Virtual starting capital | ₹1,00,000 (`FNO_PAPER_INITIAL_CAPITAL` default; confirmed by `PaperSettings.initial_capital`) |
| 2 | Bar cadence | 5-minute **completed** candles (`paper_interval = "5m"`); decisions use the closed bar only |
| 3 | Instrument | NIFTY 50 index — `InstrumentType.INDEX`, symbol `Nifty 50`, key `NSE_INDEX\|Nifty 50`, `lot_size=1`, `multiplier=1` |
| 4 | Direction | **Long-only**; `BUY` opens, `EXIT` closes; no `SELL`-to-open |
| 5 | Strategy | `MovingAverageCrossStrategy(fast=5, slow=21)` — MA(5) crossing above MA(21) = BUY, crossing below = EXIT |
| 6 | Stop-loss | Fixed 2% below entry price (`paper_stop_loss_pct`), enforced as an automatic paper exit |
| 7 | Risk per trade | 1% of **current** virtual equity (`paper_risk_per_trade_pct`) |
| 8 | Short positions | Forbidden in V1 |
| 9 | Real orders | Never |
| 10 | Real money | Never |
| 11 | Leverage | None; position notional is bounded by available cash (default `max_order_notional` also applies) |

These assumptions are configuration choices, not market claims. `paper_interval`,
`paper_stop_loss_pct`, `paper_risk_per_trade_pct`, instrument and direction are
intended to be changeable later, but V1 freezes them at the values above.

---

## 4. Market-data flow

Intended flow (all boxes implemented; the Upstox adapter is polled by the session
at `paper_interval` cadence):

```text
Upstox V3 historical-candle API (read-only)
        |
        v
UpstoxHistoricalDataProvider (data/upstox_provider.py)   [IMPLEMENTED, read-only]
        |  normalized 5m MarketPrice bars, naive-IST timestamps
        v
MarketDataProvider abstraction (data/provider.py)        [IMPLEMENTED]
        v
MovingAverageCrossStrategy (strategies/)                 [IMPLEMENTED]
        v
Paper-session loop (services/paper_session.py)           [IMPLEMENTED (WS 6.4b)]
        |  BUY/EXIT decisions, 1% risk sizing, 2% stop
        v
RiskManager -> PaperBroker -> Portfolio                  [IMPLEMENTED]
        v
Virtual capital / P&L (in-memory totals + snapshots)     [IMPLEMENTED]
```

Key facts about the Upstox adapter, from `data/upstox_provider.py`:

- It calls only `GET /v3/historical-candle/{key}/{unit}/{interval}/{to_date}/{from_date}`.
  There is **no order API**, no write endpoint and no live-execution path in the
  class (module docstring, lines 1-36).
- Authentication is `Authorization: Bearer {access_token}`; the provider raises
  typed `data.errors` subclasses and refuses to issue a request without a configured
  token (`ProviderConfigurationError`).
- Quotes / last price are **derived from the historical endpoint**, not streamed:
  `get_market_price()` returns the most recent stored bar; `get_quote()` builds a
  `MarketQuote` from that bar (`upstox_provider.py:470-491`).
- Data depth: minutes/hours available since **Jan 2022**; per-request retrieval cap
  for `5m` is **30 calendar days** (`UPSTOX_MAX_WINDOW_DAYS["5m"] = 30`),
  transparently chunked and merged.
- Timestamps are normalized to naive IST; callers must pass naive IST datetimes
  (`_historical` rejects aware datetimes).

The live session will therefore fetch **completed 5m bars** for the window
`[now - paper_lookback_days, now]` on each evaluation and decide on the last
completed bar's close. There is **no running tick feed** in V1 (and none exists in
code today).

> The **live/current-data paper session is implemented** (`services/paper_session.py`,
> WS 6.4b) and driven by the Upstox adapter via polling (`run_once`/`run_loop`).
> `StrategyService` (explicit bar lists) and the backtest/research harnesses remain
> separate offline paths.

---

## 5. Signal lifecycle

Per-candle lifecycle for V1. Each step is labeled with its current status.

| # | Step | Behavior | Status |
|---|---|---|---|
| 1 | Completed candle | Wait for the current 5m bar to finish; use its close. Never act on a forming (partial) bar. Skip trading when not in the NSE OPEN phase | Session loop: **IMPLEMENTED** (WS 6.4b); clock helper `market_session`/`is_market_open`: **IMPLEMENTED** |
| 2 | Strategy evaluation | `MovingAverageCrossStrategy.analyze(bars)` over the completed-candle history; `SignalResult` with `BUY`/`SELL`/`HOLD` | **IMPLEMENTED** (`strategies/moving_average_cross.py`) |
| 3 | BUY/EXIT decision | Long-only mapping: `BUY` when flat or when the signal is BUY; `SELL` signal maps to **EXIT** of the long position only (never short-to-open). `HOLD` → no order | Mapping logic: **IMPLEMENTED** in `PaperSession` (WS 6.4b) — BUY ignored when already long, SELL ignored when flat |
| 4 | Order creation | `Order(instrument, side, quantity)` — quantity from the risk sizer (Section 6) | **IMPLEMENTED** (`models/order.py`; quantity supplied by `RiskBasedPositionSizer`, `risk/sizer.py` — optional in `StrategyService`) |
| 5 | Risk evaluation | `RiskManager.evaluate(order, portfolio, fill_price)` — enforces `max_position_quantity`, `max_order_notional`, `max_daily_loss` | **IMPLEMENTED** (`risk/manager.py`); rejection recorded as `RejectionReason` |
| 6 | Simulated fill | `PaperBroker.place_order(order, market_price)` — applies slippage to the reference close and computes commission; rejects with `REJECTED` if no price | **IMPLEMENTED** (`broker/paper_broker.py`) |
| 7 | Position update | `Portfolio.apply_fill(fill)` — updates cash, position quantity/average entry, records a `Trade`, updates realized P&L | **IMPLEMENTED** (`portfolio/portfolio.py`) |
| 8 | P&L/equity update | `Portfolio.total_value(prices)`, `unrealized_pnl`, `realized_pnl_today` at the candle close | **IMPLEMENTED** |
| 9 | Stop-loss handling | While a long position is open, evaluate the 2% stop after every completed candle; if triggered, submit an automatic EXIT (Section 7) | **IMPLEMENTED** — session evaluates the stop after each completed candle while long; automatic paper exit on breach (`risk/stop_loss.py` + `TradingService.protective_exit`, WS 6.4/6.4b) |
| 10 | Duplicate guard | A candle may drive at most one decision; repeated fetches must not double-apply a signal | **IMPLEMENTED** — `_consumed` guard deduplicates by bar timestamp (WS 6.4b/6.5); re-fetching the same bars yields identical decisions |

Execution-path guarantees that exist today and must be preserved:

- Signals never submit orders directly; everything goes through `RiskManager` →
  `PaperBroker` → `Portfolio` (`services/strategy_service.py`, `services/trading_service.py`).
- `TradingService.submit_order` supports a `reference_price` override (used today for
  backtests); the live loop should pass the completed candle's close so the paper
  fill never depends on a mid-candle quote.
- `RiskManager` never mutates orders and never touches a broker; callers decide how
  to handle a rejection.
- **Warm-up rule:** `MovingAverageCrossStrategy.analyze` requires at least `slow + 1`
  bars before it can produce a signal — for MA(5,21) that is at least **22 completed
  5m bars**. Below that minimum the strategy returns `Signal.HOLD`
  (`moving_average_cross.py:34-40`), and the session must not generate a trading
  order during warm-up.

---

## 6. Position sizing and risk rules

V1 sizing contract (**IMPLEMENTED** — `risk/sizer.py` `RiskBasedPositionSizer`; the data it needs is available):

Let:

- `E` = current virtual equity = `portfolio.total_value({symbol: current_close})`
  (cash + open-position market value; implemented in `Portfolio`).
- `P_entry` = the completed candle's close at entry (the execution reference price).
- `risk_pct` = `paper_risk_per_trade_pct` = `0.01` (1%).
- `stop_pct` = `paper_stop_loss_pct` = `0.02` (2%).
- `lot` = `instrument.lot_size` (NIFTY 50 index: `1`), `mult` = `instrument.multiplier`.

Rules, in order:

1. **Risk amount:** `R = E * risk_pct` (1% of **current** equity, recomputed per trade).
2. **Stop distance:** `D = P_entry * stop_pct` (2% of entry price, in price units).
3. **Raw size:** `qty_raw = R / (D * mult)`.
4. **Lot rounding:** `qty = floor(qty_raw / lot) * lot` (round **down** to a valid lot).
5. **Skip below-lot:** if `qty < lot`, skip the trade (signal recorded, no order).
6. **Capital constraint:** if `qty * P_entry * mult > available_cash`, reduce `qty`
   to the largest whole lot satisfying the constraint, or skip if that is `< lot`.
   (Available cash = `portfolio.cash`; no leverage in V1.)
7. **Existing risk-manager limits** still apply on top: `max_position_quantity`,
   `max_order_notional`, `max_daily_loss` (`RiskManager`, default `75` / `250000` /
   `10000`); a rejected order is not filled.

Rounding example for NIFTY 50 (`lot_size=1`): `R = ₹1,000`, `P_entry = 24,000`,
`D = 480`, `qty_raw = 1,000 / 480 ≈ 2.08` → `qty = 2` (with `lot=1`). Should the
concept of a minimum trade-unit later differ from `lot_size`, that value is the floor.

**Deterministic rule:** results are a pure function of (equity, entry price,
settings). The exact `Decimal` arithmetic above must be used; no `float` in the
sizing path.

---

## 7. Stop-loss rules

**IMPLEMENTED** (WS 6.4/6.4b) — enforced automatically by `risk/stop_loss.py`
(`StopLossPolicy` decision rule + `enforce_stop` executor) through
`TradingService.protective_exit`; the session evaluates the stop after every
completed 5m candle while a position is open.

- Every long entry records `stop_price = P_entry * (1 - stop_loss_pct)`.
- The stop is evaluated after each **completed 5m candle** while a position is open.
- Trigger condition: the current completed candle's `low` (or a quote derived from it)
  breaches the stop. The paper exit fill price is fixed as the **worse of the candle
  open and the stop price** for the long position (a deterministic V1 rule, asserted
  by acceptance criterion 12), recorded with its timestamp.
- The stop exit is **enforced automatically**: it is a real paper `Order`/`Fill`/`Trade`
  routed through `RiskManager` → `PaperBroker` → `Portfolio`, not a manual override.
  Realized P&L, commission and slippage for the stop exit are accounted precisely as
  for any other fill.
- A stop exit sets the position flat; later `BUY` crossovers may open a new position.
- Because V1 is long-only, the 2% stop is **below** the entry. A symmetric take-profit
  is out of scope for V1.

Rationale: the 2% stop paired with 1% risk-per-trade makes `qty` from Section 6 the
same number computed forwards from entry (risk fixed at entry) — position sizing does
not need to change while the position is open.

---

## 8. Capital, equity and P&L accounting

All implemented via `Portfolio` (`portfolio/portfolio.py`); the V1 session consumes
these rather than reimplementing them.

| Quantity | Definition | Status |
|---|---|---|
| Starting virtual capital | ₹1,00,000 (`PaperSettings.initial_capital`, default from `FNO_PAPER_INITIAL_CAPITAL`) | **IMPLEMENTED** — the V1 session constructs `Portfolio` with `initial_capital` (WS 6.4b); `PaperAccount` records it as `initial_cash` |
| Cash | `portfolio.cash`, debited on `BUY` (notional + commission), credited on `SELL` (notional − commission) | **IMPLEMENTED** |
| Open position | `Position` (signed quantity, `average_entry_price`) keyed by symbol | **IMPLEMENTED** |
| Unrealized P&L | `Portfolio.unrealized_pnl(prices)` at current close | **IMPLEMENTED** |
| Realized P&L | `Portfolio.realized_pnl`; per-trade on close/reversal; `realized_pnl_today` for daily limit | **IMPLEMENTED** |
| Virtual equity | `Portfolio.total_value(prices)` = cash + market value of open positions | **IMPLEMENTED** |
| Daily risk limit | `max_daily_loss` via `RiskManager` (`realized_today <= -max_daily_loss` rejects) | **IMPLEMENTED** |

Cost model (identical between paper fills and backtests): fill price is the
reference close adjusted by `slippage_rate` adversarially (buy: ×(1+s), sell:
×(1−s)); commission = `notional × commission_rate + commission_fixed`, mirrored by
`PaperBrokerConfig` and `BacktestConfig`.

> **Correctional caveat:** the generic `Portfolio` does not itself guard a
> negative-cash fill; the V1 cash guard (Section 6 rule 6) lives in the
> `RiskBasedPositionSizer` capital bound and is **IMPLEMENTED**, and the session
> routes every BUY entry through the sizer.

> **Research-baseline caveat:** the Phase 3 historical study evaluated MA(5,21) on
> real NIFTY 50 data under realistic Indian costs. It is a **research baseline, not
> a profitability guarantee**, and none of the Phase 6 acceptance criteria depend on
> its percentages. In particular, no claim is made here that the historical backtest
> used ₹1,00,000 of capital; `BacktestConfig` defaults `initial_capital` to `100000`
> for the offline harness/demos, but the research baseline is reported in net-of-cost
> terms, and backtests run historical data while V1 runs current data with simulated
> orders.

---

## 9. Orders, fills, positions and trades

Data model (all **IMPLEMENTED** unless noted):

- **Order** (`models/order.py`) — `instrument`, `side` (`BUY`/`SELL`), `quantity`,
  `order_type` (`MARKET`), lifecycle status (`PENDING → SUBMITTED → FILLED`; a
  missing price rejects with `REJECTED`), `rejection_reason`, fill fields. Status
  changes are enforced by `Order.transition()`; invalid transitions raise
  `ValueError` and leave the order unchanged.
- **Fill** (`models/order.py`) — per-execution record: `order_id`, `instrument`,
  `side`, `quantity`, `price`, `commission`, `filled_at`, `notional`.
- **Position** (`models/position.py`) — signed `quantity` (long = positive), average
  entry price, `realized_pnl`, `opened_at`, `is_long/is_short/is_flat`,
  `unrealized_pnl`, `market_value`. A `Position` cannot have zero quantity.
- **Trade** (`models/position.py`) — completed transaction moving risk: `trade_id`,
  `instrument`, `side`, absolute `quantity`, `price`, `commission`, `executed_at`,
  `realized_pnl`, `notional`.

V1 conventions:

- Every decision maps to **exactly one** `Order`; fills in V1 are always full fills
  (`filled_quantity == quantity`; `PARTIALLY_FILLED` exists in the model but V1's
  market-style paper fills are all-or-nothing).
- `BUY` opens or adds to a long. `SELL` may only reduce/close an existing long in V1
  (short-to-open **forbidden**, see Section 3). Reversal is not used in V1.
- Fill timestamps come from the paper broker's wall clock (`datetime.now()`) in the
  live loop, consistent with current `PaperBroker`; the backtest variant stamps with
  the bar's timestamp instead (`BacktestBroker`).

---

## 10. Session/state requirements

**Current system state (IMPLEMENTED):**

- `PaperSession` (WS 6.4b) owns the session lifecycle: completed-bar selection
  (`bar.timestamp + interval <= now`), a `_consumed` duplicate-candle guard, NSE
  phase/holiday gating, 22-bar warm-up gate, long-only signal mapping, daily-loss
  policy, and signal-first/stop-second ordering.
- State persistence (WS 6.5): `persistence/session_store.py` writes JSON snapshots
  (payload + meta sidecar, SHA-256 `state_hash`) under `paper_state/`, which is
  git-ignored; `save_session`/`load_session` validate schema and hash.
  `PaperSession.snapshot()`/`restore()` rebuild a session (accounting, broker
  orders/fills, consumed/entry-candle timestamps, counters) after a restart.
- The scaffolded `paper_state_dir = "paper_state"` default matches the store's
  `DEFAULT_STATE_DIR`. `FNO_PAPER_*` env wiring for the five scaffolded fields is
  still not implemented.

**V1 session requirements (DELIVERED by WS 6.2–6.7):**

| Requirement | Contract | Status |
|---|---|---|
| In-memory run | A `--once` mode evaluates the latest completed candle and exits; a `--loop` mode iterates until stopped | **IMPLEMENTED** (`run_once`/`run_loop`) |
| Determinism inputs | Inject the "now" clock and the data source so the session is testable without network | **IMPLEMENTED** (`clock` + provider injection; `PaperBroker(now_fn=...)`) |
| Duplicate-candle guard | Track the last consumed bar timestamp; never evaluate a bar twice | **IMPLEMENTED** (`_consumed`) |
| Stop-loss + sizing | Sizing per §6; stop-loss per §7 | **IMPLEMENTED** (`RiskBasedPositionSizer`; `risk/stop_loss.py` wired through the session) |
| State persistence | Ledger + snapshots under git-ignored `paper_state/`, the session's only on-disk state | **IMPLEMENTED** (WS 6.5) |
| Environment guard | The session must not start outside `Environment.PAPER` (or an explicit sandbox override), and must use a paper-only broker | **IMPLEMENTED** |

---

## 11. Safety boundaries

Non-negotiable, enforced by design (boundaries are **IMPLEMENTED** except where noted):

| Boundary | Separation | Enforcement today |
|---|---|---|
| Historical backtesting vs. paper trading | `backtest/` and `research/` are offline; `PaperBroker` is the only broker in both paths | `BacktestBroker` extends `PaperBroker` (`is_live=False`); backtests need no credentials and make no network calls |
| Paper trading vs. real-money trading | `PaperBroker.is_live = False` hard-coded; `TradingService` raises `RuntimeError` for any non-paper broker | `broker/base.py`, `broker/paper_broker.py`, `services/trading_service.py:83-86` |
| No broker order placement in V1 | There is no order-capable broker surface; Upstox/Kite adapters are read-only market data only | `data/upstox_provider.py` (no order API), `data/kite_provider.py` |
| Analytics/read-only market data vs. order-capable auth | The Upstox access token authenticates **historical-candle reads only**; `UpstoxSettings` documents client id/secret as SSO-token placeholders, not order credentials | `config/settings.py:108-141`, `upstox_provider.py` |

The V1 session adds the following guards to this list (each **IMPLEMENTED** by
WS 6.2–6.4b):

- Reject startup if any configured component is not the paper broker (`Environment.PAPER`
  guard + `PaperBroker.is_live = False`; sandbox override only via `allow_sandbox`).
- Reject `SELL`-to-open orders (long-only rule) at both the accounting layer
  (`Portfolio.long_only`/`PaperAccount`) and the session routing layer.
- Never read, log, or expose the access token value; all secrets stay in `.env`.

---

## 12. Out of scope for V1

| Item | Reason |
|---|---|
| Real-money execution, real broker orders | Project-wide non-negotiable rule; requires an explicitly controlled future capability |
| NIFTY futures / options | V1 trades the index (cash-equivalent, `INDEX` type) only |
| Short selling | Long-only V1 |
| Leverage | Position notional bounded by available cash |
| AI-driven signals | AI analysis is a later phase and must sit behind an interface with non-autonomous execution |
| Persistent database / durable state | JSON snapshots under `paper_state/` are implemented (WS 6.5); a database / full multi-session ledger remains out of scope |
| Broker order placement / order APIs | No order-capable vendor surface exists or is planned for V1 |
| Tick-by-tick / intra-bar execution | Completed 5m bars only |
| Take-profit / trailing stops / partial exits | Deferred; V1 exits are full-close on signal or stop |

---

## 13. Acceptance criteria for the Phase 6 implementation

Concrete and testable. Each criterion is replayed offline in the automated suite
(`tests/test_acceptance_replay.py`, WS 6.7 — one test per criterion, criteria
1–30) following the project's offline, deterministic test convention.

### Risk sizing

All six risk-sizing criteria are **IMPLEMENTED** by `RiskBasedPositionSizer`
(`risk/sizer.py`, `tests/test_sizer.py`); the session consumes the sizer (WS 6.4b).

1. `qty = floor(risk_amount / (stop_distance × instrument.multiplier) / lot) × lot`; `Decimal` only.
2. `risk_amount = 0.01 × current_equity`, with current equity = cash + open-position
   market value at the reference close (supplied at decision time, never read by the sizer).
3. A computed `qty < lot` produces **no order** and a recorded skip reason.
4. Notional `qty × price × instrument.multiplier` never exceeds available cash; when it
   would, qty is reduced to the largest satisfying whole lot or skipped if `< lot`.
5. Sizing does not change mid-position; risk is fixed at entry (`1%`), stop fixed at
   `2%` below the entry price, so `qty` is stable over the life of a trade.
6. Property: `qty × stop_distance × instrument.multiplier ≤ risk_amount` (rounding
   down never exceeds the risk budget).

### Stop-loss

7. A long entered at `P` carries `stop = P × (1 − 0.02)`.
8. The stop is checked after every completed candle; a breach produces a **paper**
   exit `Fill`/`Trade` routed through `RiskManager` → `PaperBroker` → `Portfolio`.
9. The stop exit records its timestamp and, on the resulting `Trade`, the
   slippage-adjusted exit price, commission, realized P&L and per-trade P&L — all
   `Decimal`. Slippage needs no separate field: `Trade` has no slippage attribute,
   so slippage is represented through the adjusted exit fill price.
10. After a stop exit the position is flat; a later `BUY` crossover may re-enter.
11. A stop exit can never result in a short position.
12. Stop fill pricing is deterministic: for a long position whose candle breaches
    the stop, the simulated exit fill uses the **worse of the candle open and the
    stop price** (exactly as specified in §7).

### Long-only behavior

13. Only `BUY` may increase position quantity; `SELL` signals may only reduce an
    existing long to zero (never below). Short-to-open is rejected/ignored.
14. `HOLD` and non-actionable signals never create an order.

### Completed-candle cadence

15. The session only evaluates fully-formed 5m bars; a forming bar is ignored.
16. Each consumed candle produces **at most one** decision/order (duplicate-candle
    guard). Re-fetching an identical bar list yields identical decisions.
17. Warm-up: no trading order is generated before at least **22 completed bars**
    (MA(5,21) needs `slow + 1`); the strategy returns `HOLD` during the warm-up
    period (see §5).
18. The session does not trade outside the NSE OPEN phase (09:15–15:30 IST) and
    places **no orders on non-trading days** (weekends and `market_hours` holidays),
    using `market_hours.market_session` / `is_market_open`; when the market is
    closed the session stays flat and opens no new position.

### Virtual capital and accounting

19. Starting equity is ₹1,00,000 (`initial_capital` default); equity =
    `portfolio.total_value` at the current close.
20. Every session fill changes cash exactly as `Portfolio.apply_fill` specifies
    (notional ± commission), realized/unrealized P&L and `realized_pnl_today` update
    accordingly.
21. A `--once` run that consumes no new candle changes nothing (no phantom trades).

### Deterministic behavior

22. Given the same provider bar series and injected clock, the full session replay
    produces identical orders, fills, trades, equity and P&L (asserted by running the
    session against a deterministic in-memory series twice).
23. No `float` in sizing, fill-price or P&L numeric paths.

### Safety boundaries

24. The session constructs/uses only `PaperBroker` (constructor refuses `is_live`).
25. The session opens no order-capable endpoint; the only vendor call is the Upstox
    historical-candle read.
26. No access token value is printed, logged, or persisted by the session.
27. `SELL`-to-open, notional exceeding available cash (per §6 rule 6), and signal
    duplication are either rejected or skipped with a recorded reason — never
    silently executed.
28. The session cannot start outside a paper environment: start is blocked unless
    `Environment.PAPER` (or an explicitly approved development/test sandbox
    override) is active.
29. Existing `RiskManager` limits — `max_position_quantity`, `max_order_notional`
    and `max_daily_loss` — must gate paper-session orders exactly as in the
    implemented risk rules (a rejected order is never filled). This is an
    acceptance criterion for the session; `tests/test_acceptance_replay.py`
    (`test_ac_29`) replays it end-to-end.
30. Session artifacts are confined to `paper_state/`, which is git-ignored; none are
    written elsewhere. Delivered by WS 6.5 and replayed by `test_ac_30`.

---

## 14. Known limitations and future extensions

### Known limitations

- **Upstox data depth:** intraday minute/`5m` history exists only since **Jan 2022**;
  the V1 session's historical context is similarly bounded per fetch (`paper_lookback_days`,
  30-day max per request window for `5m`).
- **No live quotes:** the adapter derives prices from completed historical candles;
  a forming-bar or real-time quote is unavailable, which is why V1 is completed-close
  only.
- **Holiday calendar is advisory:** `market_hours.HOLIDAYS_2026` is best-effort; verify
  against the official NSE calendar annually.
- **Restart recovery is snapshot-based (WS 6.5):** `save_session`/`load_session`
  restore a session from JSON under `paper_state/`; snapshot schema migration and a
  searchable multi-session store are not supported.
- **Fixed-cost caveats:** the paper cost model is illustrative (0.03% commission,
  0.1% slippage); it is not a claim of any broker's real fees. Backtests and the V1
  session share this model.
- **Single-instrument, long-only:** V1 holds at most one NIFTY 50 position; the risk
  and P&L calculations assume no correlated positions.
- **Research ≠ trading:** historical MA(5,21) results are a research baseline, not a
  guarantee that the live V1 session will be profitable.

### Future extensions (not before V1)

- Multi-instrument and multi-position portfolios with portfolio-level risk.
- `SELL`-to-open / shorting, futures and options once an explicit, reviewed decision
  authorizes them.
- Take-profit, trailing stops, and intra-day exit rules beyond the fixed stop.
- Session state persistence under `paper_state/` — **delivered in WS 6.5** (JSON
  payload + meta sidecar, SHA-256 `state_hash`, git-ignored; per-day P&L ledger
  still future).
- Session operations / monitoring — **delivered in WS 6.6**:
  `services/session_monitoring.py` (`SessionHealth`/`health()`, `SessionReport`/
  `build_report`/`report_from_snapshot`, `log_results`/`log_health`,
  `report_to_html`/`write_html_report`, HTML consistent with `research/report.py`)
  and the offline operator CLI `scripts/paper_session_report.py`. OS-level
  scheduled runs (Task Scheduler / cron wrapping `run_loop` + the CLI) remain an
  operator deployment concern.
- Env-variable wiring for the five scaffolded fields (`FNO_PAPER_INTERVAL`,
  `FNO_PAPER_LOOKBACK_DAYS`, `FNO_PAPER_RISK_PER_TRADE_PCT`, `FNO_PAPER_STOP_LOSS_PCT`,
  `FNO_PAPER_STATE_DIR`) plus `.env.example` rows — still open (the session now
  consumes the typed defaults for three of the five).
- Live/streaming quotes behind an interface, if a vendor and token shape are approved.
- AI-driven signal support behind the `Strategy` interface, never autonomous execution.
- A real broker adapter remains a separate, explicitly controlled capability, disabled
  by default, and is out of scope for V1 entirely.

---

*Contract maintained against repository HEAD `00ed8ed` (WS 6.6). Paper-trading only. No live orders, no real money.*