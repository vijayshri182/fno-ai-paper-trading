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
| 2 | Real market data behind `MarketDataProvider` + first deterministic strategy | **IN PROGRESS** |
| 3 | AI analysis / decision support | Planned (not started) |
| 4 | Backtesting engine + analytics | Planned (not started) |

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

## 5. Open Topics / Risks

- The Kite Connect credential flow (api key + access token) is implemented and
  tested with mocked HTTP. It has **not** been exercised against the live
  service end-to-end; that requires a real session token and is left for the
  user (out of scope for offline verification).
- `.env.example` intentionally contains placeholders only — the user is
  responsible for supplying real credentials via `.env`.
- The NSE 2026 holiday calendar in `data/market_hours.py` is best-effort;
  verify it against the official NSE calendar before relying on it.