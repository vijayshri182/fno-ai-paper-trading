# Project Progress Tracker — F&O AI Paper Trading System

> Living tracker. Update after every development task. Based on `PROJECT_PLAN.md`
> (incl. §17d Roadmap), `docs/trading/PAPER_TRADING_V1.md`, the git history and
> the **actual repository state** (files, tests, commits). No progress is
> reported from intent — only from code, tests and git.
>
> **State as of:** 2026-09-11 · HEAD `aedd0bc` (`docs: update progress tracker and activity log after WS 6.7`).
> Working tree clean. Full suite: **546 passed** (offline, deterministic).

---

## 1. Overall project status

The analysis/backtest/historical-data stack (Phases 1–4) is **complete** and the
repo is deep in **Phase 6 (Paper Trading V1)**. WS 6.2 (domain/model completion),
WS 6.3 (V1 risk-based sizing), WS 6.4 (automatic 2% stop-loss) and **WS 6.4b**
(the deterministic current-data paper-session runtime) are done and
**committed** (`b05a17c`). **WS 6.5 (state persistence / recovery)** is now
**committed and pushed as `118e976`** (516 passed). The live/current-data
session engine now exists (`services/paper_session.py`) and is fully wired to
the sizer, risk manager, stop-loss and broker through two deterministic seams;
`PaperSession.snapshot()`/`restore()` now let a session survive a restart. Live
trading, AI and real-broker integration remain explicitly out of scope.

### Git checkpoint

| Item | Value |
|---|---|
| Branch | `master` |
| HEAD SHA | `aedd0bc` |
| origin/master | `aedd0bc` |
| HEAD == origin/master | ✅ yes (after push) |
| Working tree | clean |

Committed: `aedd0bc` (docs) → `0e5ce1c` (WS 6.7) → `118e976` (WS 6.5) → `b05a17c` (WS 6.4b) → `75d3a44` (WS 6.4) → `b4f8129` (WS 6.3) → `e853667` (WS 6.2). All pushed.

## 2. Current phase

**Phase 6 — Paper Trading V1** (per `PROJECT_PLAN.md` §17d and commit naming).

| Work stream | Scope | Status |
|---|---|---|
| 6.1 | Documentation / plan alignment | IN PROGRESS — specs exist; need refresh after WS 6.4b |
| 6.2 | Paper-trading domain/model completion | **DONE** — commit `e853667` |
| 6.3 | V1 position sizing and risk enforcement | **DONE** — commit `b4f8129` |
| 6.4(+b) | Current-data paper-session engine + stop-loss | **DONE** — commit `75d3a44` (stop-loss) + commit `b05a17c` (session runtime) |
| 6.5 | State persistence / recovery | **DONE** — commit `118e976` (pushed) |
| 6.6 | Session operations / monitoring | PENDING |
| 6.7 | Session testing + acceptance replay | **DONE** — commit `0e5ce1c` (30 offline acceptance-replay tests; full criteria 1–30 verified) |

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
| 16 | Live/current-data paper-session engine + acceptance replay | 1.0 | `services/paper_session.py` **exists**; 38 WS 6.4b + 34 WS 6.5 + **30 WS 6.7 acceptance-replay tests** (all 546 passing) |

Sum = 15×1.0 + 0.5 = **15.5 / 16 = 96.9% → 97%**

## 4. Phase-by-phase status

| Phase | Scope | Status | Evidence |
|---|---|---|---|
| 1 | Foundation (models, config, broker, portfolio, risk, tests) | **COMPLETE** | commit `448e02f` |
| 2 | Real market data + strategy + backtest harness | **COMPLETE** | commits `a86ec64`, `a1c1d7d` |
| 3 | Strategy research & robustness framework | **COMPLETE** | commits `fb3f1c5`, `3a60a41` |
| 4 | Historical-data CLI + real-data research | **COMPLETE** | commits `da1b59a`, `e1a2b38` |
| 5 | AI analysis / decision support | **NOT STARTED** | no AI code; **out of MVP scope** |
| 6 | Paper Trading V1 | **IN PROGRESS** | WS 6.2–6.5 + **6.7** done (all committed + pushed); WS 6.6 (ops) and WS 6.1 (doc alignment) pending |
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
| 16. Live paper-session engine + acceptance replay | ✅ | `services/paper_session.py` exists: completed-candle cadence, duplicate guard, NSE phase/holiday gating, 22-bar warm-up, long-only BUY/SELL/HOLD mapping, daily-loss policy (entries gated, exits executable), signal-before-stop ordering, injected clock/fill-price determinism, `--once`/`--loop` polling; **30 offline acceptance-replay tests (WS 6.7)** covering all §13 criteria 1–30 end-to-end | ✅ `tests/test_paper_session.py` (38 tests), `tests/test_session_persistence.py` (34 tests), **`tests/test_acceptance_replay.py` (30 tests, commit `0e5ce1c`)** | `services/paper_session.py`, `broker/paper_broker.py`, `services/trading_service.py`, `services/__init__.py`, `tests/test_acceptance_replay.py` |

## 6. Security / compliance status

| Control | Status |
|---|---|
| Paper-trading only | ✅ `PaperBroker.is_live = False` hard-coded; `TradingService` raises `RuntimeError` for non-paper brokers; `PaperSession` requires `Environment.PAPER` (opt-in TEST/DEVELOPMENT sandbox override) |
| No hard-coded secrets | ✅ No keys/tokens in source; `.env` git-ignored; `.env.example` placeholders only |
| Vendor surface read-only | ✅ Upstox/Kite adapters expose historical data reads only — no order-capable endpoint |
| Risk gatekeeper | ✅ Every strategy/order path routes through `RiskManager`; session BUY entries go through `RiskManager` via `TradingService.submit_order` |
| Protective stop exit | ⚠️ Deliberate, documented: protective exits leave open positions executable after the daily-loss cap (`TradingService.protective_exit` and the session's signal-exit-at-cap `_force_close` skip new-entry gating) — a designed risk-control rule, not an AI/strategy bypass |
| AI constraints (Phase 5) | ✅ N/A — no AI code exists; plan requires AI to never bypass `RiskManager` |
| Offline/ deterministic tests | ✅ 546 tests pass with no network, no credentials |
| Uncommitted work | ✅ None — WS 6.7 committed `78623a5` and pushed; only `PROGRESS.md`/`ACTIVITY_LOG.md` docs remain uncommitted |
| `.gitignore` | ✅ excludes `.env`, `datasets/`, `reports/`, `paper_state/` target |

## 7. Current blockers

- **None code-level.** WS 6.7 is committed (`0e5ce1c`) and pushed; docs updated
  (`cb170a5`). Next candidate workstreams: WS 6.1 (doc alignment) and
  WS 6.6 (ops/monitoring). No code blockers.
- (Deferred, non-blocking) Real-data research/session runs need a valid
  `UPSTOX_ACCESS_TOKEN`; offline smoke tests exist.
- (Pre-existing, non-blocking) `services/__init__.py` has no trailing newline —
  cosmetic, pre-existing.

## 8. Recently completed work

1. **Committed `118e976` — WS 6.5 state persistence / recovery** (reviewed, approved, pushed to origin/master):
   - `src/fno_ai_paper_trading/persistence/session_store.py` (new, ~435 lines): `SessionSnapshot` (frozen), `StoredSession`, `save_session`/`load_session`. File layout: payload `<name>.json` + sidecar `<name>.meta.json` under `paper_state/` (git-ignored); `state_hash` = SHA-256 over canonical JSON (`json.dumps(indent=2, sort_keys=True) + "\n"`); `SCHEMA_VERSION = "1"`; `_safe_name()` filename sanitization. Serializes instrument, portfolio (cash, non-flat positions, `initial_cash`, `realized_pnl`), broker orders/fills, session `_consumed`/`_entry_candle`/counters. Money via `Decimal(str())`; datetimes naive ISO strings. Load validates schema + hash (`ValueError` on mismatch/corruption, `FileNotFoundError` for a missing sidecar).
   - `src/fno_ai_paper_trading/persistence/__init__.py` (new): exports the public API.
   - `broker/paper_broker.py`: `snapshot() -> tuple[list[Order], list[Fill]]` (copies via `replace`); `restore(orders, fills)` (requires an empty broker, else `RuntimeError`).
   - `services/paper_session.py`: `snapshot()` and `restore(snapshot)` + helpers (`_validate_restore_target`, `_portfolio_from_live`, `_portfolio_from_snapshot`). Restore contract: session not running, snapshot instrument `symbol` + `exchange_token` + `interval_token` + `warmup_bars` match, broker empty; rebuilds `TradingService`, restores accounting state + `_consumed`/`_entry_candle`/counters, forces `_running=False`. `Portfolio.apply_fill` remains the sole accounting path.
   - `tests/test_session_persistence.py` (new, 34 tests): file-layer save/load, hash-tamper/corruption/schema guards, broker snapshot/restore, session round trips (Decimal exactness, `initial_cash` preserved, consumed-timestamps block reprocessing), **restart equivalence** (snapshot mid-run → restore → resume == uninterrupted run), stop-fires-after-restore (entry candle persisted), no-`backtest.*`-import guard.
   - Full suite: **516 passed in 2.99s** (482 baseline + 34 new). No lint/typecheck tooling exists in this repo.
2. **Committed `b05a17c` — WS 6.4b deterministic paper-session runtime** (reviewed, approved, pushed to origin/master):
   - `services/paper_session.py` (new, 584 lines): `PaperSession` orchestrator + `SessionStep`/`SessionResult`. Owns exactly the session concerns: completed-bar selection (`bar.timestamp + interval <= now`), duplicate-candle guard (`_consumed`), NSE phase/holiday gating, warm-up gate (default = `strategy.slow + 1`, i.e. 22 for MA(5,21)), long-only signal mapping, daily-loss policy, protective-stop ordering (signal first, stop second, entry candle excluded), deterministic equity snapshots, counters, `--once`/`--loop` (`run_once`/`run_loop`), `Environment.PAPER` guard with opt-in sandbox override. **Contains no sizing/risk/stop arithmetic, no persistence, no env parsing, never imports `backtest.*`.**
   - `broker/paper_broker.py` (modified): injectable `now_fn: Callable[[], datetime] | None`; `submitted_at`/`filled_at` use `self._now()`; default `datetime.now` preserved.
   - `services/trading_service.py` (modified): new keyword `fill_bar: MarketPrice | None` on `submit_order`; broker fills against that bar's close when supplied, else provider's latest — default behavior unchanged.
   - `services/__init__.py` (modified): exports `PaperSession`, `SessionResult`, `SessionStep`.
   - `tests/test_paper_session.py` (new, 38 tests): construction/env guard, completed-candle cadence, duplicate guard, phase/holiday gates, warm-up (incl. boundary), signal mapping, daily-loss policy, stop integration, clock/fill prices, determinism, `--once`/`--loop`, provider-failure recovery, broker/risk/accounting failures, zero-persistence.
   - 5 files, **+1386/−4**. Full suite: **482 passed** (444 baseline + 38 new).
2. **Committed `75d3a44` — WS 6.4 automatic 2% stop-loss enforcement** (reviewed, approved, pushed): `risk/stop_loss.py` (`StopLossPolicy` decision rule + `enforce_stop` single authoritative executor), `OrderType.STOP`, `BacktestConfig.enable_stop_loss`/`stop_loss_pct`, `BacktestEngine` integration (signal-first, stop-second, before equity snapshot), `TradingService.protective_exit`, `risk/__init__.py` exports, deterministic `Position.opened_at`, `tests/test_stop_loss.py` (41 tests), 2 legacy backtest tests scoped via `enable_stop_loss=False`. 9 files, +775/−4.
3. **Committed `b4f8129` — WS 6.3 V1 risk-based sizing**: `RiskBasedPositionSizer` (1% equity risk over 2% stop, lot-rounded down, cash-bound, single-position skip), `SizerConfig`, `tests/test_sizer.py`.
4. **Committed `e853667` — WS 6.2 domain/model completion**: long-only + `PaperAccount` accounting-layer gating.
5. **Committed `0e5ce1c` — WS 6.7 acceptance replay tests** (reviewed, approved, pushed to origin/master): `tests/test_acceptance_replay.py` (30 deterministic, offline tests) replaying every V1 acceptance criterion (§13, criteria 1–30) end-to-end through `PaperSession`. Full suite: **546 passed** (516 committed baseline + 30 new). Single new file; no production code changed.

## 9. Intentionally out of scope (current phase)

- **AI analysis / decision support (Phase 5)** — no code; must sit behind an interface, never autonomous execution.
- **Real broker / live trading (Phase 7)** — separate, explicitly controlled capability; disabled forever by default.
- **Streaming / tick quotes** — V1 uses the read-only historical endpoint (quotes derived from last bar); no websocket/streaming code.
- **Session operations / monitoring (WS 6.6)** — logging/dashboards for the running session.
- **Short selling, leverage, futures/options** — V1 is long-only NIFTY 50 index, cash-bounded.
- **Take-profit / trailing stops / partial exits / intra-bar execution** — completed 5m candles only; full-close exits.
- **State persistence / recovery (WS 6.5)** — **DONE** (commit `118e976`, pushed). No `FNO_PAPER_*` env wiring was added in this stream; `paper_state/` stays git-ignored.
- **Env wiring / `.env.example` rows for the five scaffolded `paper_*` fields** — only after the session lands.

## 10. Next recommended task

> **WS 6.7 is DONE** (commit `0e5ce1c`, pushed). Next approved workstreams: **WS 6.1** (doc alignment) and **WS 6.6** (ops/monitoring) — requires user approval before starting.

1. ~~**WS 6.5 — State persistence / recovery**~~ DONE — commit `118e976`, pushed.
2. ~~**WS 6.7 — Acceptance replay tests**~~ DONE — commit `0e5ce1c`, pushed; 30 tests, 546 suite passing.
3. **WS 6.1 — Documentation / plan alignment** (pending user approval): refresh specs post-WS 6.4b/6.5/6.7.
4. **WS 6.6 — Session operations / monitoring** (pending user approval): logging/dashboards for the running session.

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

## 12. WS 6.5 handoff — new-session instructions

### Scope and architecture decisions

- **Persistence is a separate module.** `src/fno_ai_paper_trading/persistence/session_store.py`
  owns ALL serialization (models → dict → canonical JSON). `services/paper_session.py` stays
  thin: `snapshot()` assembles a `SessionSnapshot` from live collaborators; `restore()`
  validates and re-instantiates. No disk I/O and no arithmetic in the session layer.
- **File layout + integrity.** Payload `<safe_name>.json` beside `<safe_name>.meta.json`
  (schema, save time, `state_hash`) under `DEFAULT_STATE_DIR = "paper_state"` (git-ignored).
  `state_hash` = SHA-256 over canonical JSON = `json.dumps(indent=2, sort_keys=True) + "\n"`.
  Load refuses unsupported `SCHEMA_VERSION`, missing sidecar, or hash/corruption mismatch
  (`ValueError`/`FileNotFoundError`).
- **Restore contract.** `restore()` requires `not self._running` (RuntimeError), snapshot
  instrument `symbol` + `exchange_token` + `interval_token` + `warmup_bars` to match
  (ValueError), and an empty broker (RuntimeError). It replaces the portfolio, restores the
  broker ledger, rebuilds `TradingService`, restores `_consumed`/`_entry_candle`/counters,
  forces `_running=False`. **No re-execution**: `Portfolio.apply_fill` remains the sole
  accounting path.
- **Two dataclass subtleties.** (1) `Portfolio.initial_cash` is assigned AFTER construction —
  `__post_init__` sets `initial_cash = cash`. (2) A live `Position.realized_pnl` may be
  negative (partial close at a loss) but the constructor only accepts `>= 0`, so it is
  assigned after construction in `_position_from_dict`, `_portfolio_from_live` and
  `_portfolio_from_snapshot`.
- **Only non-flat positions are persisted** — a flat `Position(quantity=0)` cannot be
  reconstructed from its invariants.
- **Money and time.** Money is `Decimal` (serialized via `str()`); coercion helpers
  `_money`/`_int` raise `ValueError` instead of silently coercing. Datetimes use naive ISO
  strings (IST), matching the runtime's naive clock.
- **No env wiring, no backtest coupling.** No `FNO_PAPER_*` env wiring added; `persistence/`
  never imports `backtest.*`. `paper_session.py` docstring now reads "WS 6.4b / WS 6.5".

### Files changed (committed `118e976`)

| File | Change |
|---|---|
| `src/fno_ai_paper_trading/persistence/session_store.py` | NEW — `SessionSnapshot`, `StoredSession`, `save_session`/`load_session`, model serializers |
| `src/fno_ai_paper_trading/persistence/__init__.py` | NEW — public exports |
| `src/fno_ai_paper_trading/broker/paper_broker.py` | `snapshot()` / `restore()` (+ `from dataclasses import replace`) |
| `src/fno_ai_paper_trading/services/paper_session.py` | `snapshot()` / `restore()` + `_validate_restore_target`, `_portfolio_from_live`, `_portfolio_from_snapshot` |
| `.gitignore` | `paper_state/` appended |
| `tests/test_session_persistence.py` | NEW — 34 deterministic tests |

### Test verification

- Full suite: **516 passed in 2.99s** (482 committed baseline + 34 new). No lint/typecheck
  tooling exists in this repo; pytest is the gate.

### Risks / deviations

- Test-only dead code: `tests/test_session_persistence.py` contains an unused
  `_make_session_with_cost` helper referencing a nonexistent `settings_override=` kwarg —
  harmless (never called), candidate for later removal.
- No known code-level blockers.

---

## 13. WS 6.7 handoff — new-session instructions

### Scope and approach

- **Single new file, no production changes.** `tests/test_acceptance_replay.py` is
  entirely self-contained (no imports from other test files; duplicates the small
  helper pattern from `test_paper_session.py`). It contains exactly 30 offline,
  deterministic acceptance tests — one per V1 acceptance criterion (§13, criteria
  1–30) — exercised end-to-end through the real `PaperSession` runtime.
- **Grouping mirrors the spec.** Tests are grouped into 7 classes matching §13's
  own section headings: `TestAcceptanceRiskSizing` (1–6), `TestAcceptanceStopLoss`
  (7–12), `TestAcceptanceLongOnly` (13–14), `TestAcceptanceCadence` (15–18),
  `TestAcceptanceCapitalAccounting` (19–21), `TestAcceptanceDeterminism` (22–23),
  `TestAcceptanceSafety` (24–30). Every criterion has exactly one method named
  `test_ac_XX_*`.
- **Helpers (duplicated, not imported).** `_ts`, `_index`, `_bar`, `_bars`,
  `_provider`, `_settings`, `_ScriptedStrategy`, `_strategy`, `_make_session`,
  `_buying_steps`, `_all_recorded_money`. These mirror the `test_paper_session.py`
  helpers and must stay independent (the repo convention is zero cross-test imports).
- **Warm-up default.** `PaperSession` sets `warmup_bars = 1` when the scripted
  strategy is used (no `slow` attribute → fallback to 1). AC tests that enter at
  bar index 2 therefore work without passing `warmup_bars` explicitly. AC-17 passes
  `warmup_bars=22` and enters at bar index 21 (boundary: prefix length 22 ≥ 22).
- **Clock seam.** Default clock is `lambda: FILL_CLOCK` (= `datetime(2026,9,2,10,0)`).
  This is injected into both the session and, via `PaperBroker(now_fn=...)`, the
  broker, so fills get `filled_at = FILL_CLOCK` and determinism is preserved.
- **Sizing semantics verified by tests.** Equity-basis sizing (AC-02) relies on
  `RiskBasedPositionSizer` sizing from current equity, not initial capital. The
  risk-sized quantity is capped by available cash (AC-04). Below-lot quantities
  result in `approved=False` (AC-03). Sizing is skipped when already long (AC-05).
  Risk budget is never exceeded after rounding (AC-06).
- **Protective stops bypass `RiskManager`** (AC-08, documented deviation). The test
  asserts the exit produces a paper `Fill`/`Trade` via `PaperBroker` →
  `Portfolio.apply_fill`, which is the criterion's intent. AC-09 verifies the
  slippage/commission math end-to-end. AC-12 verifies both intrabar (exit at stop
  price) and gap/open-through (exit at open) fill paths.
- **Stop arithmetic is pure.** `StopLossPolicy.stop_price`, `evaluate`, and
  `enforce_stop` are tested through the session's `SessionStep.stop_result`
  without mocking. The session uses `replace(position, opened_at=entry_ts)` so the
  WS 6.4 entry-candle exclusion works consistently with the session clock.
- **Safety / persistence tested end-to-end.** AC-24/25 verify broker isolation and
  read-only provider surface. AC-26 checks no credentials leak (debug log + CWD
  unchanged). AC-27 verifies violation orders are rejected with an explicit reason
  string. AC-29 seeds a portfolio at the daily-loss cap and verifies entry
  suspension. AC-30 saves/restores via `save_session`/`load_session` into a
  `tmp_path` and verifies counters, cash, consumed count and no CWD pollution.
  `paper_state/` is confirmed present in `.gitignore`.

### Critical bugs fixed during the run (before first green)

1. **`_buying_steps` filter (AC-02).** Original helper captured all steps with a
   non-`None` fill, including SELL fills. Fixed to filter only `Signal.BUY` steps.
   Without this fix the risk-amount assertion would have failed on the intermediate
   SELL step.
2. **`realized_pnl` assertion (AC-02).** Initial value `-7800` was a miscalculation
   (incorrectly including the unrealized re-entry). Correct value: `-8000` (the
   closed SELL round-trip only; the re-entry BUY has no realized P&L yet).
3. **Dead assertion line (AC-16).** A vestigial `assert [...] for _bar in () == []`
   was removed — it was unreachable dead code that obscured the real duplicate-guard
   assertions.
4. **Determinism tag comparison (AC-22).** `run("a") == run("b")` always failed
   because the tuples included the differing tag strings. Fixed to
   `run("a")[1:] == run("b")[1:]` so only the numeric/data portions are compared.

### Files changed (uncommitted, pending commit approval)

| File | Change |
|---|---|
| `tests/test_acceptance_replay.py` | NEW — 30 deterministic offline acceptance-replay tests (§13, criteria 1–30) |

### Test verification

- Acceptance suite: **30 passed in 0.33s**.
- Full suite: **546 passed in 2.50s** (516 committed baseline + 30 new).
  No lint/typecheck tooling exists in this repo; pytest is the gate.

### Risks / deviations

- **AC-08 wording divergence** is intentional and documented in the test docstring
  and §6: protective stops deliberately bypass `RiskManager`. The test asserts the
  exit still produces a paper fill/trade through `PaperBroker` →
  `Portfolio.apply_fill`, which is the criterion's true intent.
- **AC-17 warm-up boundary** relies on the gate being
  `len(prefix) < self.warmup_bars` (strict less-than). With warm-up 22, bar index
  21 has prefix length 22 and is therefore the first eligible bar. This matches the
  existing WS 6.4b `TestWarmup.test_signal_allowed_at_warmup_boundary` test.
- No known code-level blockers. Next workstreams: WS 6.1 (doc alignment) and
  WS 6.6 (ops/monitoring).

---

*Sources: `PROJECT_PLAN.md` (§17b–17d, DoD §25–28), `docs/trading/PAPER_TRADING_V1.md`, `ACTIVITY_LOG.md`, `git log`, repository tree, and `pytest` results.*