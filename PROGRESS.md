# Project Progress Tracker — F&O AI Paper Trading System

> Living tracker. Update after every development task. Based on `PROJECT_PLAN.md`
> (incl. §17d Roadmap), `docs/trading/PAPER_TRADING_V1.md`, the git history and
> the **actual repository state** (files, tests, commits). No progress is
> reported from intent — only from code, tests and git.
>
> **State as of:** 2026-09-09 · HEAD `75d3a44` (`feat: Phase 6 WS 6.4 automatic 2% stop-loss enforcement`).
> Working tree clean except this handoff document. Full suite: **444 passed** (offline, deterministic).

---

## 1. Overall project status

The analysis/backtest/historical-data stack (Phases 1–4) is **complete** and the
repo is mid-**Phase 6 (Paper Trading V1)**. WS 6.2 (domain/model completion), WS 6.3
(V1 risk-based sizing) and the **automatic 2% stop-loss** component of WS 6.4 are
done and **committed** (`75d3a44`); the live/current-data session
engine (`services/paper_session.py`) does **not exist yet**. Live trading, AI and
real-broker integration remain explicitly out of scope.

### Git checkpoint

| Item | Value |
|---|---|
| Branch | `master` |
| HEAD SHA | `75d3a44073607315068267993bf6ecad4f855cc3` |
| origin/master | `75d3a44073607315068267993bf6ecad4f855cc3` |
| HEAD == origin/master | ✅ yes |
| Working tree | clean — only untracked `PROGRESS.md` (this handoff doc) |

Committed: `75d3a44` (WS 6.4) → `b4f8129` (WS 6.3) → `e853667` (WS 6.2). All pushed.

## 2. Current phase

**Phase 6 — Paper Trading V1** (per `PROJECT_PLAN.md` §17d and commit naming).

| Work stream | Scope | Status |
|---|---|---|
| 6.1 | Documentation / plan alignment | IN PROGRESS — specs exist; need refresh after WS 6.4 |
| 6.2 | Paper-trading domain/model completion | **DONE** — commit `e853667` |
| 6.3 | V1 position sizing and risk enforcement | **DONE** — commit `b4f8129` |
| 6.4 | Current-data paper-session engine (+ stop-loss) | **IN PROGRESS** — stop-loss component **DONE** — commit `75d3a44`; session loop NOT STARTED |
| 6.5 | State persistence / recovery | PLANNED — **out of scope** unless approved |
| 6.6 | Session operations / monitoring | PENDING |
| 6.7 | Session testing + acceptance replay | PENDING — blocked by 6.4 engine |

## 3. MVP progress percentage

> MVP = every capability required for an end-to-end **paper-trading-only** system
> that can trade current data with 1% risk sizing and 2% stop-loss — i.e.
> Phases 1, 2, 3, 4 **plus** Phase 6 (V1). AI (Phase 5) and live-broker (Phase 7)
> are **not** MVP capabilities (see §9).

**Current estimate: 84%**

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
| 12 | Paper-session configuration (+ env wiring) | 0.5 | Five `paper_*` fields scaffolded; `FNO_PAPER_*` env wiring NOT STARTED |
| 13 | V1 risk-based position sizing | 1.0 | `RiskBasedPositionSizer` (1% / 2%, lot-down, cash bound); tested |
| 14 | Long-only gating + cash/no-leverage guard (session level) | 0.5 | `Portfolio.long_only` + `PaperAccount` + sizer cash bound done; V1 session routing missing |
| 15 | Automatic 2% stop-loss enforcement | 0.5 | `StopLossPolicy`/`enforce_stop` + `OrderType.STOP` + engine wiring + 41 tests **committed** `75d3a44`; V1 session routing pending |
| 16 | Live/current-data paper-session engine + acceptance replay | 0.0 | `services/paper_session.py` does not exist |

Sum = 11×1.0 + 0.5 + 1.0 + 0.5 + 0.5 + 0.0 = **13.5 / 16 = 84.4% → 84%**

## 4. Phase-by-phase status

| Phase | Scope | Status | Evidence |
|---|---|---|---|
| 1 | Foundation (models, config, broker, portfolio, risk, tests) | **COMPLETE** | commit `448e02f` |
| 2 | Real market data + strategy + backtest harness | **COMPLETE** | commits `a86ec64`, `a1c1d7d` |
| 3 | Strategy research & robustness framework | **COMPLETE** | commits `fb3f1c5`, `3a60a41` |
| 4 | Historical-data CLI + real-data research | **COMPLETE** | commits `da1b59a`, `e1a2b38` |
| 5 | AI analysis / decision support | **NOT STARTED** | no AI code; **out of MVP scope** |
| 6 | Paper Trading V1 | **IN PROGRESS** | WS 6.2/6.3 done; WS 6.4 partial; 6.5–6.7 pending |
| 7 | Real broker / live boundary | **NOT STARTED** | explicitly out of scope |

## 5. MVP capabilities detail

Status legend: ✅ COMPLETE · 🟡 IN PROGRESS · ⬜ NOT STARTED · ⛔ BLOCKED

| MVP capability | Status | Implementation | Test status / files | Relevant files |
|---|---|---|---|---|
| 1. Core domain models | ✅ | `Instrument`, `Order`/`Fill` (`Order.transition` lifecycle), `Position`/`Trade`, `MarketPrice`, enums | ✅ `tests/test_models.py`, `test_order_state.py`, `test_account.py` | `src/fno_ai_paper_trading/models/` |
| 2. Config, env settings, logging | ✅ | Environment-based `PaperSettings`/`UpstoxSettings`/`KiteSettings`; `.env` + `.env.example` | ✅ covered in suite | `config/settings.py`, `utils/logging.py`, `.env.example` |
| 3. Paper broker | ✅ | Slippage + commission model; `is_live=False` hard-coded; PENDING→SUBMITTED→FILLED | ✅ `tests/test_broker.py` | `broker/base.py`, `broker/paper_broker.py` |
| 4. Portfolio & P&L accounting | ✅ | Cash, positions, avg entry, realized/unrealized P&L, `total_value`, `realized_pnl_today` | ✅ `tests/test_portfolio.py`, `test_account.py` | `portfolio/portfolio.py`, `portfolio/account.py` |
| 5. RiskManager gates | ✅ | `max_position_quantity`, `max_order_notional`, `max_daily_loss` | ✅ `tests/test_risk.py` | `risk/manager.py`, `risk/__init__.py` |
| 6. Market-data abstraction + in-memory provider | ✅ | `MarketDataProvider` ABC + deterministic mock | ✅ suite coverage | `data/provider.py`, `data/mock_provider.py` |
| 7. Read-only real adapters + registry + dataset store | ✅ | Upstox V3 + Kite v3 (read-only), intervals, curated registry, SHA-256 dataset store, validation | ✅ `test_upstox_provider.py`, `test_kite_provider.py`, `test_instrument_registry.py`, `test_dataset_store.py`, `test_data_quality.py`, `test_intervals.py` | `data/upstox_provider.py`, `data/kite_provider.py`, `data/instrument_registry.py`, `data/dataset_store.py`, `data/intervals.py`, `data/validation.py`, `data/errors.py` |
| 8. Strategy engine + deterministic MA strategy | ✅ | `Strategy` ABC, `StrategyEngine`, `MovingAverageCrossStrategy` (fast/slow) | ✅ `tests/test_strategies.py` | `strategies/base.py`, `strategies/engine.py`, `strategies/moving_average_cross.py` |
| 9. Order orchestration | ✅ | `StrategyService` (signal→order), `TradingService` (risk→broker→portfolio; rejects non-paper broker) | ✅ `tests/test_strategy_service.py` | `services/strategy_service.py`, `services/trading_service.py` |
| 10. Deterministic backtest harness | ✅ | `BacktestEngine`, `BacktestBroker` (bar-stamped fills), costs, metrics, hand-verified datasets | ✅ `tests/test_backtest.py` | `backtest/config.py`, `backtest/engine.py`, `backtest/result.py`, `backtest/datasets.py` |
| 11. Research framework + real-data baseline | ✅ | costs/execution/regimes/split/walk-forward/sensitivity/benchmark/metrics/experiment/report; NIFTY 50 1d baseline | ✅ `tests/test_research.py`, `test_real_data_research.py` | `research/*`, `scripts/research_real_data.py` |
| 12. Paper-session configuration | 🟡 | Five `paper_*` fields scaffolded (scaffolding ✅); `FNO_PAPER_*` env wiring ⬜ | ✅ defaults validated | `config/settings.py` |
| 13. V1 risk-based sizing | ✅ | `RiskBasedPositionSizer`/`SizerConfig` (1% risk, 2% stop, lot-down, cash bound, skip-when-open) | ✅ `tests/test_sizer.py` | `risk/sizer.py` |
| 14. Long-only gating + cash guard | 🟡 | `Portfolio.long_only`/`PaperAccount` reject SELL-to-open at `apply_fill`; sizer cash bound — session-level routing ⬜ | ✅ `test_account.py`, `test_sizer.py` | `portfolio/portfolio.py`, `portfolio/account.py`, `risk/sizer.py` |
| 15. Automatic 2% stop-loss enforcement | 🟡 | `StopDecision`/`StopExitResult`/`StopLossPolicy`/`enforce_stop`; `OrderType.STOP`; `BacktestConfig.enable_stop_loss`/`stop_loss_pct`; `BacktestEngine` wiring (signal-first, stop-second); `TradingService.protective_exit` — **committed** `75d3a44` | ✅ `tests/test_stop_loss.py` (41 tests); 2 legacy `test_backtest.py` tests scoped with `enable_stop_loss=False` | `risk/stop_loss.py` (new), `models/enums.py`, `risk/__init__.py`, `backtest/config.py`, `backtest/engine.py`, `services/trading_service.py`, `portfolio/portfolio.py` (deterministic `opened_at`) |
| 16. Live paper-session engine + acceptance replay | ⬜ | `services/paper_session.py` (completed-5m loop, NSE OPEN/holiday gating, 22-bar warm-up, duplicate-candle guard, `--once`/`--loop`, paper-env guard) | ⬜ none (WS 6.7 acceptance criteria 1–30 pending) | `services/paper_session.py` (does not exist) |

## 6. Security / compliance status

| Control | Status |
|---|---|
| Paper-trading only | ✅ `PaperBroker.is_live = False` hard-coded; `TradingService` raises `RuntimeError` for non-paper brokers |
| No hard-coded secrets | ✅ No keys/tokens in source; `.env` git-ignored; `.env.example` placeholders only |
| Vendor surface read-only | ✅ Upstox/Kite adapters expose historical data reads only — no order-capable endpoint |
| Risk gatekeeper | ✅ Every strategy/order path routes through `RiskManager` |
| Protective stop exit | ⚠️ Deliberate, documented: protective exits bypass `RiskManager.evaluate` so they stay executable after the daily-loss cap (`risk/stop_loss.py`) — this is a designed risk-control rule, not an AI/strategy bypass |
| AI constraints (Phase 5) | ✅ N/A — no AI code exists; plan requires AI to never bypass `RiskManager` |
| Offline/ deterministic tests | ✅ 444 tests pass with no network, no credentials |
| Uncommitted work | ✅ None — WS 6.4 stop-loss committed `75d3a44`; only `PROGRESS.md` is untracked |
| `.gitignore` | ✅ excludes `.env`, `datasets/`, `reports/`, `paper_state/` target |

## 7. Current blockers

- **None code-level.** WS 6.4 stop-loss is committed and pushed; wiring it into a
  live session (next task) is a normal next step.
- (Deferred, non-blocking) Real-data research/session runs need a valid
  `UPSTOX_ACCESS_TOKEN`; offline smoke tests exist.

## 8. Recently completed work

1. **Committed `75d3a44` — WS 6.4 automatic 2% stop-loss enforcement** (reviewed, approved, pushed to origin/master): `risk/stop_loss.py` (`StopLossPolicy` decision rule + `enforce_stop` single authoritative executor), `OrderType.STOP`, `BacktestConfig.enable_stop_loss`/`stop_loss_pct`, `BacktestEngine` integration (signal-first, stop-second, before equity snapshot), `TradingService.protective_exit`, `risk/__init__.py` exports, deterministic `Position.opened_at`, `tests/test_stop_loss.py` (41 tests), 2 legacy backtest tests scoped via `enable_stop_loss=False`. 9 files, +775/−4.
2. **Committed `b4f8129` — WS 6.3 V1 risk-based sizing**: `RiskBasedPositionSizer` (1% equity risk over 2% stop, lot-rounded down, cash-bound, single-position skip), `SizerConfig`, `tests/test_sizer.py`.
3. **Committed `e853667` — WS 6.2 domain/model completion**: long-only + `PaperAccount` accounting-layer gating.
4. **Verification**: `pytest` → **444 passed** (403 committed + 41 new).

## 9. Intentionally out of scope (current phase)

- **AI analysis / decision support (Phase 5)** — no code; must sit behind an interface, never autonomous execution.
- **Real broker / live trading (Phase 7)** — separate, explicitly controlled capability; disabled forever by default.
- **State persistence / recovery (WS 6.5)** — `paper_state/` only if separately approved; git-ignored.
- **Session operations / monitoring (WS 6.6)** — logging/dashboards for the running session.
- **Short selling, leverage, futures/options** — V1 is long-only NIFTY 50 index, cash-bounded.
- **Take-profit / trailing stops / partial exits / intra-bar execution** — completed 5m candles only; full-close exits.
- **Streaming / tick quotes** — V1 uses the read-only historical endpoint (quotes derived from last bar).
- **Env wiring / `.env.example` rows for the five scaffolded `paper_*` fields** — only after the session lands.

## 10. Next recommended task

> Requires review/approval before starting.

1. **Continue WS 6.4**: implement `services/paper_session.py` (completed-5m-candle evaluation, NSE OPEN-phase + holiday gating via `market_hours`, 22-bar warm-up via MA(5,21), duplicate-candle guard, `--once`/`--loop`, `Environment.PAPER` guard, injected clock/provider for determinism) consuming `RiskBasedPositionSizer` + `enforce_stop`. This file does not exist yet.
2. Then **WS 6.7**: offline replay tests for V1 acceptance criteria 1–30 (risk sizing, stop-loss, long-only, cadence, capital, determinism, safety); then WS 6.1 doc alignment + WS 6.6 ops if approved.

---
*Sources: `PROJECT_PLAN.md` (§17b–17d, DoD §25–28), `docs/trading/PAPER_TRADING_V1.md`, `ACTIVITY_LOG.md`, `git log`, repository tree, and `pytest` results.*