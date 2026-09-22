# Daily Paper Trading Track — Audit & Design (design-only, no implementation)

Status: DESIGN ONLY. No production code was added, changed, or enabled by this document or the audit that produced it. See "Explicit list of changes not made" in the companion report.

Scope: define a SEPARATE daily paper-trading operating track that can later be automated WITHOUT touching Fresh-OOS collection/validation, protected OOS, strategy parameters, live-execution gates, risk controls, WS 7.x behavior, vNext semantics, or research artifacts/hash pins.

---

## 1. What exists today (audit summary)

All references are `src/fno_ai_paper_trading/...` unless prefixed.

### Runner/CLI
- `scripts/run_paper_agent.py` — WS 7.8 continuous paper agent. Modes: `--smoke` (synthetic), `--csv` (offline replay), `--upstox` (live historical polling). `--once/--cycles N/--forever`. Heartbeat to `reports/algorithm_state/paper_agent.json` (`run_paper_agent.py:148-151`). It is MANUAL only — no scheduled task invokes it.

### Core runtime
- `services/paper_session.py` — `PaperSession`. Long-only by design (docstring `services/paper_session.py:11`; portfolio constructed `long_only=True` at `:143`). Default strategy `MovingAverageCrossStrategy()` at `:138`. Completed-bar rule `bar.timestamp + timedelta(minutes=self.interval_minutes) <= now` at `:301-302`. NSE gate in `_phase_gate` at `:304-309`. Long-only handlers at `:356-402`. Stop handling `_handle_stop` at `:427-454`. Deterministic `run_loop` (`for _ in range(polls)`) at `:244-255`. Snapshot `:554`, restore `:581`.
- `agent/agent.py` — `ContinuousPaperAgent`: `start` `:225`, `halt` `:280`, `cycle` `:324`, watchdog STOP `:394`, `_checkpoint` `:523`, `max_bar_age` `:639-641`, `_restore_session` `:683`. Clock: `now()` returns `datetime.now()` when `config.clock is None` (`:181-184`).
- `broker/paper_broker.py` — `PaperBroker` + `PaperBrokerConfig`. `is_live: bool = False` at `:46`; constructor raises if ever truthy `:53-54`. Fill = full size at slippage-adjusted bar close `:69,72-75`. Slippage directional `_apply_slippage` `:129-132`; commission `_compute_commission` `:134-136`. Costs: commission 0.0003 of notional, fixed 0, slippage 0.001 (`:33-35`) — byte-identical to `evaluation/records.py` `EvaluationConfig` defaults (100000/1/0.0003/0/0.001).
- `broker/base.py` — `Broker` abstract; `is_live=False` at `:22`.
- `risk/manager.py` — `RiskManager.evaluate` `:40-71`: max position qty 75, max order notional 250000, max daily loss 10000 (`config/settings.py:236-238`); reject reasons at `:55-69`. `_resulting_quantity` `:73-78` computes `current - qty` for SELL and never rejects a negative result (no naked-short guard).
- `risk/sizer.py` — `RiskBasedPositionSizer` `:95-204`: 1% equity risk, 2% stop distance, lot-rounded down, cash-fit `:171-190`, single-position skip `:122-123`.
- `risk/stop_loss.py` — `StopLossPolicy` `:73-107`: stop = entry×(1 − 2%) post-slippage `:87-89`; LONG-ONLY `:98`; entry candle excluded `:100-101`; gap→open exit / intrabar→stop exit `:103-106`. `enforce_stop` `:110-157` bypasses RiskManager deliberately (`:20-28,123-125`).
- `services/trading_service.py` — `submit_order` risk-gates BEFORE filling `:85-89`, then `broker.place_order` then `portfolio.apply_fill` with NO rollback try/except `:96-101`; `protective_exit` `:111-141`.
- `portfolio/portfolio.py` — `apply_fill` long-only guard raises on a short-opening SELL `:68-74`; BUY debits notional+commission, SELL credits notional−commission `:76-79`.
- `execution/gate.py` — `LiveExecutionTestGate.decision` `:127-155`; requires `FNO_LIVE_EXECUTION_TEST_ENABLED=1` + consent file + token fingerprint; `ExecutionMode.LIVE` "never returned here and never implemented" `:43`; `ExecutionMode.PAPER` otherwise.
- `execution/risk.py` — `OvernightGuard` (no entry after 15:30 IST − 900s buffer) `:29-31,48-72,147-156` used by the live-test manager ONLY, not by the paper session.
- `data/market_hours.py` — `NSE_TZ` `:21`, continuous 09:15–15:30 `:23-25`, `HOLIDAYS_2026` `:30-34`, `is_trading_day` `:47`, `market_phase` `:53`, `is_market_open` `:66`, `market_session` `:71`, `next_open` `:91`.
- `persistence/session_store.py` — `DEFAULT_STATE_DIR = "paper_state"` `:45`; `save_session` `:97`; SHA-256 `state_hash` `:128`, verify `:173-176`; `load_session` `:148`.
- `strategies/moving_average_cross.py` — `MovingAverageCrossStrategy`, `fast=5, slow=21` `:27`, deterministic `analyze` `:33-79`. This is the frozen strategy.
- `strategies/end_of_day.py` — `model_3_end_of_day` research strategy (late-session entries 13:00–14:45, forced exit 15:20, ATR stop, short-capable). NOT wired into `PaperSession` (default is moving_average_cross, `services/paper_session.py:138`).
- `evaluation/paper_trades.py` — paper trade ledger store.
- `reporting/paper_dashboard.py`, `services/session_monitoring.py` — reports/HTML/log lines.
- `config/settings.py` — `PaperSettings` `:42` (limits `:50-52`, risk/stop `:60-61`); env wiring `:236-241`.

### Automation status
- ONE scheduled task exists: `FNO_FreshOosCollector` (Ready, PT15M, `.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.scheduler --once`, repo cwd, batteries disabled). No task invokes paper trading. NO paper session persists today (`paper_state/` does not exist). Daily paper trading is NOT automated.

### Evidence separation
- Grep across `src/` for `fresh_oos|protected_oos` shows references only inside the `fresh_oos/`/`fresh_oos_validation/` packages (their own modules and docstrings) and read-only research reports (`walkforward/reports.py`, `evaluation/*`). The paper/broker/session/dashboard paths contain no `fresh_oos` or protected-OOS writes and no `data/fresh_oos` path. Paper artifacts live in `paper_state/`, `reports/`, `runs/`.

---

## 2. Audit findings — the 9 questions

1. **Reusable components:** strategy MA(5,21) + `PaperSession` + `PaperBroker` (+cost model) + `RiskManager` + `Sizer` + `StopLossPolicy` + `TradingService` + long-only `Portfolio` + `market_hours` + tamper-checked persistence + agent checkpoint/heartbeat + offline test hooks (injectable clock/bars). All tested (see §Testing).
2. **Incomplete / unsafe for automation:**
   - High: **interval mismatch** — `run_paper_agent.py:75` builds `UpstoxHistoricalDataProvider(access_token=token)` with no interval → provider default `"1d"` (`data/upstox_provider.py:149,164,463`) → `PaperSession._fetch_bars` `services/paper_session.py:297` feeds DAILY bars to a 5m session. Must be fixed before any unattended run.
   - High: **no pre-trade naked-short guard** (`risk/manager.py:73-78`; enforcement only post-fill `portfolio/portfolio.py:68-74`, after broker recorded the fill).
   - High: **fill-before-accounting without rollback** (`services/trading_service.py:96-101`).
   - Medium: **no broker-level order idempotency** (`paper_broker.py:65` always mints a new id; dedup only via the session `_consumed` set or execution state machine).
   - Medium: **restart = pure deserialization, no cross-validation** (`paper_session.py:28-29,581-611`; session_store only tamper-checking). No file lock for concurrent `--upstox` invocations.
   - Medium: **naive local clock** when none injected (`agent/agent.py:181-184`), correctness depends on machine TZ.
   - Low: unbounded in-memory ledger growth; same-candle signal-then-stop ordering (`paper_session.py:348-349`); `dry_run` contradiction in `execution/memory.py:39,70`.
3. **Automatic today?** No. Manual CLI only; no paper task; `paper_state/` absent.
4. **Long-only?** Paper **session** is long-only (`paper_session.py:11,143,356-402`; portfolio long_only; sizer BUY-only). The execution-test/signal path (`execution/signal.py:31-43` PUT→SELL) and `EndOfDayStrategy` can emit SELL-to-open-short, and `StrategyService` submits SELL without a long_only pre-check (`services/strategy_service.py:81,100-105`). No naked-short guard broker-wide.
5. **Overnight?** No EOD flatten exists in the session; persisted sessions can be restored next day with the position still open (persistence is designed for that, `test_full_restart_equivalence`). The overnight guard exists only in the live-test manager path (`execution/risk.py`).
6. **Realistic costs?** Yes: 0.03% commission + 0.1% directional slippage identical to research `EvaluationConfig`, applied inside `PaperBroker` (`paper_broker.py:129-136`).
7. **Restart/reconciliation safe?** Deterministic and tamper-checked, but not reconcilable: no provider/broker cross-check on restore, no flock, single-process dedup only.
8. **Fresh-OOS contamination?** No structural route today (separation confirmed; fresh-OOS writes only via `fresh_oos.collector`, single-use validation only via `fresh_oos_validation.consume` behind `--explicit`). Paper must keep to its own namespaces.
9. **Bugs/contradictions:** interval mismatch; `memory.py` dry_run contradiction; fill-before-accounting ordering; risk-notional vs fill-price divergence (`risk/manager.py:62-64` vs `paper_broker.py:69`); stop bypasses daily-loss by design (`risk/stop_loss.py:123-125`); EOD research strategy is short-capable; `StrategyService` SELL path.

---

## 3. Recommend operating model (default + implications)

Comparison (do not silently choose):

| Policy | Default-wired today | Overnight exposure | Automation risk | Recommendation |
|---|---|---|---|---|
| Intraday-only, close by close | No (holds until signal) | None | Low | Preferred base |
| Multi-day holding | Possible (restore continues) | Unbounded gap risk; stop model is intraday 2% | Higher | Not the default |
| EOD flatten | No mechanism in session | None (forced exit) | Low–medium, needs explicit order | ADD as the default exit |

**Recommended default: 5m intraday paper trading, long-only, single position, 2% intrabar stop, forced EOD flatten by 15:20 IST, hard stop on 15:30, flat overnight, no new entries after the daily-loss cap.**

Implications:
- Reuses `market_hours` window 09:15–15:30 IST (`data/market_hours.py:23-25,53-66`), completed 5m bars only (`paper_session.py:301-302`).
- Long-only matches session/portfolio semantics; a SELL is always an exit of an existing long, never a short-open (enforce pre-trade).
- EOD flatten reuses the session's long-only SELL close (`paper_session.py:396-402`) at the last completed bar at/after 15:20; at/after 15:30 no orders.
- Reports must state strategy identity `moving_average_cross(fast=5, slow=21)` + interval + run-id + session policy everywhere.

---

## 4. Automation design (fail closed)

New scheduled entry `FNO_PaperTradingDaily` (e.g., every 5 min, `IgnoreNew`, batteries disabled, venv python, repo cwd). Interaction policy: independent of `FNO_FreshOosCollector`; paper pass NEVER invokes the collector and the collector NEVER invokes paper; both write disjoint namespaces.

Fail-closed rules (explicit operating window — no network and no simulated orders outside it):
- **Market-hours gate:** pure `data/market_hours.py` (`is_market_open`); outside Monday–Friday 09:15–15:30 IST (or holiday) → clean SKIP, exit 0, no network, no orders, no manifest write; reason printed (WEEKEND / HOLIDAY / PRE_OPEN / AFTER_CLOSE). No override flag in the scheduled path.
- **Startup:** load session state; restore guards (instrument/interval/warmup/hash, `test_restore_guards_*`); reconcile broker ledger vs snapshot; refuse to start on mismatch unless explicit `--repair` (human) flag.
- **Shutdown:** checkpoint snapshot + agent state, exit 0; SIGTERM/SIGINT handlers.
- **Missed runs:** bounded catch-up (only completed bars strictly after last consumed timestamp); never fabricate fills.
- **Duplicate prevention:** file lock (own lock path under `data/paper_trading/`) + run-id registration; one active session/process per day.
- **Restart recovery:** deterministic replay to last checkpoint; `_consumed` timestamps block reprocessing; no re-execution of recorded fills.
- **Disconnection:** bounded retries with backoff; persistent failure → soft abort + checkpoint + alert; never fill on missing/stale data.
- **Stale prices:** completed-bar filter + `max_bar_age` bound (reuse `agent/agent.py:639-641` default).
- **Partial/rejected fills:** `PaperBroker` always full-fills at bar close; a risk-refused order is recorded with no position and processing continues; partial fills are not produced — asserted by test.
- **Reconciliation:** at each poll verify `portfolio.current_quantity` equals the mapped broker fills; mismatch → stop, alert, checkpoint, no new orders.
- **EOD:** flatten by the 15:20 decision on the last completed bar; nothing after 15:30.
- **Daily report:** `reports/paper_trading/<YYYY-MM-DD>.json` (strategy id, run-id, bars consumed, fills/trades, P&L, flags) + dashboard heartbeat; redact credentials.
- **Alerts:** reuse `AlertEngine`/`FileAlertSink` (`reports/agent/agent-alerts.jsonl`) with `PAPER_TRADING_LABEL`; never print credentials.

---

## 5. Risk and accounting preservation (design must not weaken)

Preserve exactly:
- `RiskManager`: max_position_quantity 75, max_order_notional 250000, max_daily_loss 10000 (`config/settings.py:236-238`); exit/stops remain executable at the loss cap.
- Sizing: 1% risk / 2% stop / lot-rounded / cash-fit / single-position skip (`risk/sizer.py`).
- Stop-loss: 2% fixed, post-slippage anchor, intrabar + gap, entry candle excluded (`risk/stop_loss.py`).
- Costs: 0.03% commission + 0.1% slippage (parity with `EvaluationConfig`).
- Accounting: `Portfolio.apply_fill` cash/equity and `realized_pnl_today` for the daily cap.
- Duplicate-order prevention: consumed-bar set + execution state machine; broker-level idempotency gap is a design fix for the new track.
- Single-slot: one position; second BUY ignored.

Gaps to address IN the new track (no weakening of shared controls):
- Pre-trade guard: reject SELL whose resulting quantity would be negative (no naked shorts) — add at the track's risk boundary; keep `Portfolio` guard as backstop.
- Atomic accounting: fill → apply with rollback/void on failure (address `trading_service.py:96-101` within the track only).
- Broker idempotency: refuse a repeat `order_id` in the track's submission path.

---

## 6. Evidence separation (Fresh-OOS / protected-OOS isolation)

- Paper artifacts ONLY under: `data/paper_trading/` (new store + own manifest with run-id prefix `paper-run-<yyyy-mm-dd>-<uuid>`), `paper_state/`, `reports/paper_trading/`, `reports/algorithm_state/paper_agent.json`.
- Never write to: `data/fresh_oos/`, protected-OOS datasets, `datasets/`, `fresh_oos_manifest.json`.
- Never call: `fresh_oos_validation.consume`, `.job.run_controlled_validation`, or any research-run that consumes protected OOS.
- No automatic promotion: paper results can never flip `PromotionRegistry`/gate or mark any algorithm READY/uploaded; promotion stays human/autonomous-loop-only.
- Every report labels strategy identity + run-id + policy so paper evidence can never be mistaken for Fresh-OOS validation evidence.

---

## 7. Testing strategy (proposed; none added by this audit)

New isolated, offline `tests/test_daily_paper_track.py` family (injectable clock + in-memory provider, never network) covering:
- Market-hours boundary (PRE_OPEN/OPEN/CLOSE/holiday/weekend, next_open rolls), reuse `data/market_hours.py`.
- Weekends → clean SKIP exit 0, no orders.
- Duplicate scheduler invocation → second pass SKIPs (lock/run-id).
- Restart recovery → determinism equivalence, no re-executed fills.
- Stale market data → skipped, never filled.
- Rejected paper order (risk refusal) → recorded, no position, continues.
- Partial fills → asserted app-level behavior (PaperBroker full-fills).
- Stop-loss execution → intrabar + gap, entry-candle exclusion.
- EOD flatten → flat after 15:20/15:30, no overnight.
- Overnight policy → previous-day position flat next open (or explicit HOLD decision).
- Max daily loss → entries blocked, exits/stops still allowed.
- Position reconciliation → mismatch stops the track.
- Cost calculation → commission/slippage parity vs `EvaluationConfig`.
- P&L reconciliation → equity/cash/realized consistency.
- Fresh-OOS isolation → paper store never touches `data/fresh_oos`; run-id prefix separation.
- Live-mode refusal → gate closed; `is_live` False path asserted.
- Credential redaction → nothing in reports/alerts.
- Determinism/idempotency → identical replay → identical state; repeated run-id refused.

Existing coverage already present (all green today, 2219 total): fills/slippage/commission `test_broker.py`, risk `test_risk.py`, stop `test_stop_loss.py`, sizer `test_sizer.py`, EOD signal `test_end_of_day.py`, market hours `test_market_hours.py`, persistence/restart `test_session_persistence.py` (incl. `test_full_restart_equivalence`, `test_consumed_timestamps_block_reprocessing`), monitoring/report `test_session_monitoring.py`, live gate/credentials `test_live_execution_test.py` + `test_credential_separation.py`, agent lifecycle `test_agent*.py`.

---

## 8. Proposed file changes (NOT made — pending separate approval)

- NEW `src/fno_ai_paper_trading/paper_track/runner.py` — fail-closed daily loop (gate → reconcile → step → EOD → report/checkpoint).
- NEW `src/fno_ai_paper_trading/paper_track/policy.py` — session policy constants (window, flatten time, overnight=flat).
- NEW `src/fno_ai_paper_trading/paper_track/store.py` — `data/paper_trading/` store + manifest with `paper-run-*` ids.
- FIX (in track isolation only) `run_paper_agent`/provider wiring: pass explicit interval `5m` to `UpstoxHistoricalDataProvider` and to `PaperSession._fetch_bars`.
- NEW `scripts/run_paper_track.py` + scheduled task `FNO_PaperTradingDaily`.
- NEW `tests/test_daily_paper_track.py`.
- NEW `docs/daily_paper_trading_runbook.md` (approval-gated).
- NO change to shared risk/stop/broker/session/dashboard/fresh-oos modules beyond the isolated interval-wiring fix.

---

## 9. Signed-off constraints (repeated from the task)

No real orders; no live enablement; no options/futures research data fetches; no Fresh-OOS consumption; no parameter tuning; no change to frozen MA(5,21) or OUR-ALGO-004 params; no automatic promotion; no protected-OOS mutation; no commit/push; no credential printing/requesting; all existing gates preserved; the new scheduler's interaction with `FNO_FreshOosCollector` is documented above (independent, disjoint namespaces).