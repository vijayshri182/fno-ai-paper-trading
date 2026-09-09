# Project Progress Tracker — F&O AI Paper Trading System

> Living tracker. Update after every development task. Based on `PROJECT_PLAN.md`
> (incl. §17d Roadmap), `docs/trading/PAPER_TRADING_V1.md`, the git history and
> the **actual repository state** (files, tests, commits). No progress is
> reported from intent — only from code, tests and git.
>
> **State as of:** 2026-09-09 · HEAD `b05a17c` (`feat: Phase 6 WS 6.4b deterministic paper session runtime`).
> Working tree clean. Full suite: **482 passed** (offline, deterministic).

---

## 1. Overall project status

The analysis/backtest/historical-data stack (Phases 1–4) is **complete** and the
repo is deep in **Phase 6 (Paper Trading V1)**. WS 6.2 (domain/model completion),
WS 6.3 (V1 risk-based sizing), WS 6.4 (automatic 2% stop-loss) and **WS 6.4b**
(the deterministic current-data paper-session runtime) are done and
**committed** (`b05a17c`). The live/current-data session engine now exists
(`services/paper_session.py`) and is fully wired to the sizer, risk manager,
stop-loss and broker through two deterministic seams. Live trading, AI and
real-broker integration remain explicitly out of scope; **WS 6.5 (persistence)
is the next approved workstream and has NOT started**.

### Git checkpoint

| Item | Value |
|---|---|
| Branch | `master` |
| HEAD SHA | `b05a17c25bfa27eb10374803b21fc64bc6ff9f37` |
| origin/master | `b05a17c25bfa27eb10374803b21fc64bc6ff9f37` |
| HEAD == origin/master | ✅ yes |
| Working tree | clean |

Committed: `b05a17c` (WS 6.4b) → `75d3a44` (WS 6.4) → `b4f8129` (WS 6.3) → `e853667` (WS 6.2). All pushed.

## 2. Current phase

**Phase 6 — Paper Trading V1** (per `PROJECT_PLAN.md` §17d and commit naming).

| Work stream | Scope | Status |
|---|---|---|
| 6.1 | Documentation / plan alignment | IN PROGRESS — specs exist; need refresh after WS 6.4b |
| 6.2 | Paper-trading domain/model completion | **DONE** — commit `e853667` |
| 6.3 | V1 position sizing and risk enforcement | **DONE** — commit `b4f8129` |
| 6.4(+b) | Current-data paper-session engine + stop-loss | **DONE** — commit `75d3a44` (stop-loss) + commit `b05a17c` (session runtime) |
| 6.5 | State persistence / recovery | **NOT STARTED** — next approved workstream |
| 6.6 | Session operations / monitoring | PENDING |
| 6.7 | Session testing + acceptance replay | PENDING — 38 unit tests landed with WS 6.4b (see §11); full criteria 1–30 replay pending |

## 3. MVP progress percentage

> MVP = every capability required for an end-to-end **paper-trading-only** system
> that can trade current data with 1% risk sizing and 2% stop-loss — i.e.
> Phases 1, 2, 3, 4 **plus** Phase 6 (V1). AI (Phase 5) and live-broker (Phase 7)
> are **not** MVP capabilities (see §9).

**Current estimate: 94%**

### Calculation

16 MVP capabilities, each weighted 1/16. Completion per capability: **1.0** =
implemented + tested (+ committed for its defined slot); **0.5** = core component
implemented and tested but the consuming V1 session wiring is missing; **0.0** =
no code.

| # | MVP capability | Completion | Rationale |
|---|---|---|---|
| 1 | Core domain models | 1.0 | Models + validation + order lifecycle; tested |
| 2 | Config, env settings, logging | 1.0 | `load_settings()` wired; `.env`-based |
| 3 | Paper broker | 1.0 | Fills, slippage, commission, status machine; tested |
| 4 | Portfolio & P&L accounting | 1.0 | Cash, positions, equity, trades; tested |
| 5 | RiskManager gates | 1.0 | qty / notional / daily-loss; tested |
| 6 | Market-data abstraction + in-memory provider | 1.0 | `MarketDataProvider` ABC + `InMemoryMarketDataProvider` |
| 7 | Read-only real data adapters + registry + dataset store | 1.0 | Upstox + Kite (read-only), intervals, validation |
| 8 | Strategy engine + deterministic MA strategy | 1.0 | Tested |
| 9 | Order orchestration (StrategyService / TradingService) | 1.0 | Signal → risk → broker → portfolio; tested |
| 10 | Deterministic backtest harness | 1.0 | Engine, costs, metrics, datasets; tested |
| 11 | Research / robustness framework + real-data baseline | 1.0 | Tested; NIFTY 50 1d baseline produced |
| 12 | Paper-session configuration (+ env wiring) | 0.5 | Five `paper_*` fields consumed by the session defaults; `FNO_PAPER_*` env wiring NOT STARTED |
| 13 | V1 risk-based position sizing | 1.0 | `RiskBasedPositionSizer` (1% / 2%, lot-down, cash bound); tested |
| 14 | Long-only gating + cash/no-leverage guard (session level) | 1.0 | `Portfolio.long_only` + session-level mapping (BUY-when-long/short ignored, SELL-when-flat ignored); tested |
| 15 | Automatic 2% stop-loss enforcement | 1.0 | `StopLossPolicy`/`enforce_stop` + `TradingService.protective_exit` wired into the session (signal-first, stop-second, entry candle excluded); tested |
| 16 | Live/current-data paper-session engine + acceptance replay | 0.5 | `services/paper_session.py` **exists** + 38 tests; WS 6.7 acceptance-replay stream NOT STARTED |

Sum = 14×1.0 + 0.5 + 0.5 = **15 / 16 = 93.75% → 94%**

## 4. Phase-by-phase status

| Phase | Scope | Status | Evidence |
|---|---|---|---|
| 1 | Foundation (models, config, broker, portfolio, risk, tests) | **COMPLETE** | commit `448e02f` |
| 2 | Real market data + strategy + backtest harness | **COMPLETE** | commits `a86ec64`, `a1c1d7d` |
| 3 | Strategy research & robustness framework | **COMPLETE** | commits `fb3f1c5`, `3a60a41` |
| 4 | Historical-data CLI + real-data research | **COMPLETE** | commits `da1b59a`, `e1a2b38` |
| 5 | AI analysis / decision support | **NOT STARTED** | no AI code; **out of MVP scope** |
| 6 | Paper Trading V1 | **IN PROGRESS** | WS 6.2/6.3/6.4/6.4b done; 6.5 planned next, 6.6–6.7 pending |
| 7 | Real broker / live boundary | **NOT STARTED** | explicitly out of scope |

## 5. MVP capabilities detail

Status legend: ✅ COMPLETE · 🟡 IN PROGRESS · ⬜ NOT STARTED · ⛔ BLOCKED

| MVP capability | Status | Implementation | Test status / files | Relevant files |
|---|---|---|---|---|
| 1. Core domain models | ✅ | `Instrument`, `Order`/`Fill` (`Order.transition` lifecycle), `Position`/`Trade`, `MarketPrice`, enums | ✅ `tests/test_models.py`, `test_order_state.py`, `test_account.py` | `src/fno_ai_paper_trading/models/` |
| 2. Config, env settings, logging | ✅ | Environment-based `PaperSettings`/`UpstoxSettings`/`KiteSettings`; `.env` + `.env.example` | ✅ covered in suite | `config/settings.py`, `utils/logging.py`, `.env.example` |
| 3. Paper broker | ✅ | Slippage + commission model; `is_live=False` hard-coded; PENDING→SUBMITTED→FILLED; **injectable `now_fn` clock** (WS 6.4b seam) | ✅ `tests/test_broker.py` | `broker/base.py`, `broker/paper_broker.py` |
| 4. Portfolio & P&L accounting | ✅ | Cash, positions, avg entry, realized/unrealized P&L, `total_value`, `realized_pnl_today` | ✅ `tests/test_portfolio.py`, `test_account.py` | `portfolio/portfolio.py`, `portfolio/account.py` |
| 5. RiskManager gates | ✅ | `max_position_quantity`, `max_order_notional`, `max_daily_loss` | ✅ `tests/test_risk.py` | `risk/manager.py`, `risk/__init__.py` |
| 6. Market-data abstraction + in-memory provider | ✅ | `MarketDataProvider` ABC + deterministic mock | ✅ suite coverage | `data/provider.py`, `data/mock_provider.py` |
| 7. Read-only real adapters + registry + dataset store | ✅ | Upstox V3 + Kite v3 (read-only), intervals, curated registry, SHA-256 dataset store, validation | ✅ `test_upstox_provider.py`, `test_kite_provider.py`, `test_instrument_registry.py`, `test_dataset_store.py`, `test_data_quality.py`, `test_intervals.py` | `data/upstox_provider.py`, `data/kite_provider.py`, `data/instrument_registry.py`, `data/dataset_store.py`, `data/intervals.py`, `data/validation.py`, `data/errors.py` |
| 8. Strategy engine + deterministic MA strategy | ✅ | `Strategy` ABC, `StrategyEngine`, `MovingAverageCrossStrategy` (fast/slow) | ✅ `tests/test_strategies.py` | `strategies/base.py`, `strategies/engine.py`, `strategies/moving_average_cross.py` |
| 9. Order orchestration | ✅ | `StrategyService` (signal→order), `TradingService` (risk→broker→portfolio; rejects non-paper broker; **`fill_bar` fill-price seam**, WS 6.4b) | ✅ `tests/test_strategy_service.py` | `services/strategy_service.py`, `services/trading_service.py` |
| 10. Deterministic backtest harness | ✅ | `BacktestEngine`, `BacktestBroker` (bar-stamped fills), costs, metrics, hand-verified datasets | ✅ `tests/test_backtest.py` | `backtest/config.py`, `backtest/engine.py`, `backtest/result.py`, `backtest/datasets.py` |
| 11. Research framework + real-data baseline | ✅ | costs/execution/regimes/split/walk-forward/sensitivity/benchmark/metrics/experiment/report; NIFTY 50 1d baseline | ✅ `tests/test_research.py`, `test_real_data_research.py` | `research/*`, `scripts/research_real_data.py` |
| 12. Paper-session configuration | 🟡 | Five `paper_*` fields scaffolded and **consumed by the session** (scaffolding ✅); `FNO_PAPER_*` env wiring ⬜ | ✅ defaults validated | `config/settings.py` |
| 13. V1 risk-based sizing | ✅ | `RiskBasedPositionSizer`/`SizerConfig` (1% risk, 2% stop, lot-down, cash bound, skip-when-open) | ✅ `tests/test_sizer.py` | `risk/sizer.py` |
| 14. Long-only gating + cash guard | ✅ | `Portfolio.long_only`/`PaperAccount` reject SELL-to-open at `apply_fill`; sizer cash bound; **session-level routing done** (BUY when already long/short ignored, SELL when flat ignored) | ✅ `test_account.py`, `test_sizer.py`, `tests/test_paper_session.py` | `portfolio/portfolio.py`, `portfolio/account.py`, `risk/sizer.py`, `services/paper_session.py` |
| 15. Automatic 2% stop-loss enforcement | ✅ | `StopDecision`/`StopExitResult`/`StopLossPolicy`/`enforce_stop`; `OrderType.STOP`; `TradingService.protective_exit`; **session wiring done** (signal-first, stop-second, entry candle excluded) — commits `75d3a44` + `b05a17c` | ✅ `tests/test_stop_loss.py` (41 tests); `tests/test_paper_session.py` stop integration; 2 legacy `test_backtest.py` tests scoped with `enable_stop_loss=False` | `risk/stop_loss.py`, `models/enums.py`, `risk/__init__.py`, `backtest/config.py`, `backtest/engine.py`, `services/trading_service.py`, `portfolio/portfolio.py`, `services/paper_session.py` |
| 16. Live paper-session engine + acceptance replay | 🟡 | `services/paper_session.py` exists: completed-candle cadence, duplicate guard, NSE phase/holiday gating, 22-bar warm-up, long-only BUY/SELL/HOLD mapping, daily-loss policy (entries gated, exits executable), signal-before-stop ordering, injected clock/fill-price determinism, `--once`/`--loop` polling | ✅ `tests/test_paper_session.py` (38 tests, see §11); WS 6.7 acceptance criteria 1–30 replay ⬜ | `services/paper_session.py`, `broker/paper_broker.py`, `services/trading_service.py`, `services/__init__.py` |

## 6. Security / compliance status

| Control | Status |
|---|---|
| Paper-trading only | ✅ `PaperBroker.is_live = False` hard-coded; `TradingService` raises `RuntimeError` for non-paper brokers; `PaperSession` requires `Environment.PAPER` (opt-in TEST/DEVELOPMENT sandbox override) |
| No hard-coded secrets | ✅ No keys/tokens in source; `.env` git-ignored; `.env.example` placeholders only |
| Vendor surface read-only | ✅ Upstox/Kite adapters expose historical data reads only — no order-capable endpoint |
| Risk gatekeeper | ✅ Every strategy/order path routes through `RiskManager`; session BUY entries go through `RiskManager` via `TradingService.submit_order` |
| Protective stop exit | ⚠️ Deliberate, documented: protective exits leave open positions executable after the daily-loss cap (`TradingService.protective_exit` and the session's signal-exit-at-cap `_force_close` skip new-entry gating) — a designed risk-control rule, not an AI/strategy bypass |
| AI constraints (Phase 5) | ✅ N/A — no AI code exists; plan requires AI to never bypass `RiskManager` |
| Offline/ deterministic tests | ✅ 482 tests pass with no network, no credentials |
| Uncommitted work | ✅ None — WS 6.4b committed `b05a17c`; `PROGRESS.md` updated in its own docs commit |
| `.gitignore` | ✅ excludes `.env`, `datasets/`, `reports/`, `paper_state/` target |

## 7. Current blockers

- **None code-level.** WS 6.4b is committed and pushed; WS 6.5 (persistence) is
  the approved next workstream and is not started.
- (Deferred, non-blocking) Real-data research/session runs need a valid
  `UPSTOX_ACCESS_TOKEN`; offline smoke tests exist.
- (Pre-existing, non-blocking) `services/__init__.py` has no trailing newline —
  cosmetic, pre-existing.

## 8. Recently completed work

1. **Committed `b05a17c` — WS 6.4b deterministic paper-session runtime** (reviewed, approved, pushed to origin/master):
   - `services/paper_session.py` (new, 584 lines): `PaperSession` orchestrator + `SessionStep`/`SessionResult`. Owns exactly the session concerns: completed-bar selection (`bar.timestamp + interval <= now`), duplicate-candle guard (`_consumed`), NSE phase/holiday gating, warm-up gate (default = `strategy.slow + 1`, i.e. 22 for MA(5,21)), long-only signal mapping, daily-loss policy, protective-stop ordering (signal first, stop second, entry candle excluded), deterministic equity snapshots, counters, `--once`/`--loop` (`run_once`/`run_loop`), `Environment.PAPER` guard with opt-in sandbox override. **Contains no sizing/risk/stop arithmetic, no persistence, no env parsing, never imports `backtest.*`.**
   - `broker/paper_broker.py` (modified): injectable `now_fn: Callable[[], datetime] | None`; `submitted_at`/`filled_at` use `self._now()`; default `datetime.now` preserved.
   - `services/trading_service.py` (modified): new keyword `fill_bar: MarketPrice | None` on `submit_order`; broker fills against that bar's close when supplied, else provider's latest — default behavior unchanged.
   - `services/__init__.py` (modified): exports `PaperSession`, `SessionResult`, `SessionStep`.
   - `tests/test_paper_session.py` (new, 38 tests): construction/env guard, completed-candle cadence, duplicate guard, phase/holiday gates, warm-up (incl. boundary), signal mapping, daily-loss policy, stop integration, clock/fill prices, determinism, `--once`/`--loop`, provider-failure recovery, broker/risk/accounting failures, zero-persistence.
   - 5 files, **+1386/−4**. Full suite: **482 passed** (444 baseline + 38 new).
2. **Committed `75d3a44` — WS 6.4 automatic 2% stop-loss enforcement** (reviewed, approved, pushed): `risk/stop_loss.py` (`StopLossPolicy` decision rule + `enforce_stop` single authoritative executor), `OrderType.STOP`, `BacktestConfig.enable_stop_loss`/`stop_loss_pct`, `BacktestEngine` integration (signal-first, stop-second, before equity snapshot), `TradingService.protective_exit`, `risk/__init__.py` exports, deterministic `Position.opened_at`, `tests/test_stop_loss.py` (41 tests), 2 legacy backtest tests scoped via `enable_stop_loss=False`. 9 files, +775/−4.
3. **Committed `b4f8129` — WS 6.3 V1 risk-based sizing**: `RiskBasedPositionSizer` (1% equity risk over 2% stop, lot-rounded down, cash-bound, single-position skip), `SizerConfig`, `tests/test_sizer.py`.
4. **Committed `e853667` — WS 6.2 domain/model completion**: long-only + `PaperAccount` accounting-layer gating.

## 9. Intentionally out of scope (current phase)

- **AI analysis / decision support (Phase 5)** — no code; must sit behind an interface, never autonomous execution.
- **Real broker / live trading (Phase 7)** — separate, explicitly controlled capability; disabled forever by default.
- **Streaming / tick quotes** — V1 uses the read-only historical endpoint (quotes derived from last bar); no websocket/streaming code.
- **Session operations / monitoring (WS 6.6)** — logging/dashboards for the running session.
- **Short selling, leverage, futures/options** — V1 is long-only NIFTY 50 index, cash-bounded.
- **Take-profit / trailing stops / partial exits / intra-bar execution** — completed 5m candles only; full-close exits.
- **State persistence / recovery (WS 6.5)** — **NOT STARTED; approved as the next workstream.** No code exists yet; any future `paper_state/` stays git-ignored.
- **Env wiring / `.env.example` rows for the five scaffolded `paper_*` fields** — only after the session lands.

## 10. Next recommended task

> Approved next workstream: **WS 6.5 — Persistence** (NOT STARTED).

1. **WS 6.5 — State persistence / recovery**: define and implement the persistence layer for a running paper session (snapshot/checkpoint + restore) so a session can survive a restart. Must respect the current invariants: `Portfolio.apply_fill` remains the sole accounting path, Decimal money math, no `backtest.*` coupling, `paper_state/` git-ignored. Reuse the established `AbstractDatasetStore`-style patterns from `data/dataset_store.py`; add deterministic round-trip tests. Do NOT add `FNO_PAPER_*` env wiring in this stream unless approved.
2. Then **WS 6.7**: offline replay tests for V1 acceptance criteria 1–30 (risk sizing, stop-loss, long-only, cadence, capital, determinism, safety); then WS 6.1 doc alignment + WS 6.6 ops if approved.

## 11. WS 6.4b handoff — new-session instructions

### Scope and architecture decisions

- **Single-file orchestrator.** `services/paper_session.py` is a thin runtime that owns session lifecycle concerns **only**. All domain math stays where it already lives: sizing in `RiskBasedPositionSizer`, static caps in `RiskManager` (`max_position_quantity`, `max_order_notional`, `max_daily_loss`), the stop rule/executor in `risk/stop_loss.py` (`StopLossPolicy` + `TradingService.protective_exit`), accounting in `Portfolio.apply_fill`. The session does no sizing/risk/stop arithmetic and never mutates cash/positions/P&L itself.
- **Determinism by injection, everywhere.** `PaperSession(settings, provider, strategy, *, instrument, broker, portfolio, risk_manager, sizer, quantity, stop_policy, clock, interval, warmup_bars, allow_sandbox)` — every collaborator is injectable; omitted ones are built from `PaperSettings`. The injected `clock` drives both the decision time (`poll(when=None)` uses it) and, via `PaperBroker(now_fn=...)`, the broker's `submitted_at`/`filled_at`. A full replay is bit-for-bit reproducible.
- **Completed-candle policy.** A bar is eligible only when `bar.timestamp + interval <= now`; the forming bar is never traded. Each bar is processed at most once (`_consumed` set). Order determination is per-candle and chronological (`prefix = ordered[:index+1]`).
- **Warm-up.** Default `warmup_bars = strategy.slow + 1` (22 for the MA(5,21) production strategy; 1 for the scripted test strategy). Bars with fewer completed predecessors are skipped with a recorded reason.
- **Long-only mapping.** BUY opens only when flat; BUY while long/short is ignored with a reason. SELL closes only an open long; SELL when flat is ignored. HOLD never creates an order.
- **Signal-before-stop ordering.** Per candle: phase gate → warm-up gate → strategy signal → signal action (BUY / SELL / HOLD) → protective stop → equity snapshot. The **entry candle itself is never a stop candle** (`_entry_candle` map); the stop presents `replace(position, opened_at=entry_ts)` so the WS 6.4 `opened_at >= bar.timestamp` exclusion stays consistent with the session's candle clock.
- **Daily-loss policy (entry-only gate).** `_daily_loss_hit` = `realized_pnl_today(today=now.date()) <= -max_daily_loss`. New **entries** are suspended at the cap (skipped with reason `daily-loss limit reached; new entries suspended`). **Exits stay executable**: a signal SELL-to-close at the cap routes through `_force_close`, a single-price MARKET SELL filled at the completed-bar close that bypasses `RiskManager` **exactly like protective stops** (deliberate, documented — see §6). If the risk manager rejects an exit with `DAILY_LOSS`, the session retries the same path.
- **Deterministic seams.** `PaperBroker.__init__(config, now_fn=None)` (fill clock) and `TradingService.submit_order(..., fill_bar=None)` (fill price pinned to a specific completed bar's close). Both default to the original behavior; zero behavior change for existing callers, verified by the unchanged 444-test baseline.
- **No persistence; no live/streaming.** In-memory only; restarting constructs a fresh runtime. No env parsing in the session (settings come from `PaperSettings`). No websocket/streaming, no order-capable vendor endpoint, `is_live=False` unchanged.

### Files changed (commit `b05a17c`)

| File | Change |
|---|---|
| `src/fno_ai_paper_trading/services/paper_session.py` | NEW — `PaperSession`, `SessionStep`, `SessionResult` (+584) |
| `tests/test_paper_session.py` | NEW — 38 deterministic tests (+754) |
| `src/fno_ai_paper_trading/broker/paper_broker.py` | `now_fn` clock seam (+8/−3) |
| `src/fno_ai_paper_trading/services/trading_service.py` | `fill_bar` fill-price seam (+8/−1) |
| `src/fno_ai_paper_trading/services/__init__.py` | export `PaperSession`, `SessionResult`, `SessionStep` (+4) |

### Test verification

- Full suite: **482 passed** (444 committed baseline + 38 new). `git diff --check` clean at commit time.
- New coverage (grouped): construction & environment guard (8), completed-candle cadence & duplicate guard (3), phase/holiday gates (4), warm-up (2), signal mapping (5), daily-loss policy (2), stop integration (3), clock & fill prices (2), determinism (1), `--once`/`--loop` (3), provider/broker/risk/accounting failures & no-persistence (5).

### Risks / deviations

- **`SessionResult.empty` semantic** finalized as `consumed == 0` (a provider-failure poll that consumed nothing is empty; the failure itself is surfaced via `provider_error`).
- **Daily-loss cap exit** is a new dedicated path (`_force_close`) returning a minimal `OrderResult` with reason `signal exit at daily-loss cap`; it reuses the exact broker + `Portfolio.apply_fill` accounting path and contains no stop arithmetic.
- Test-only fixes during the run: warm-up `skipped_candles` is 21 (not 24); determinism round-trip P&L is 100 (2×Δ50); `run_once` no-candle case uses an identical decision time; loss-cap entry test seeds a **flat** portfolio at the cap. All are expectation corrections, not behavior changes.
- No known code-level blockers. WS 6.5 (persistence) is next and NOT started.

### Git / remote status

- `master` == `origin/master` == `b05a17c25bfa27eb10374803b21fc64bc6ff9f37`; working tree clean; all pushed.

---
*Sources: `PROJECT_PLAN.md` (§17b–17d, DoD §25–28), `docs/trading/PAPER_TRADING_V1.md`, `ACTIVITY_LOG.md`, `git log`, repository tree, and `pytest` results.*