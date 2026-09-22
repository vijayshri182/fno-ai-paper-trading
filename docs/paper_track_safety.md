# Daily Paper Trading Track — Safety & Isolation Contract

Status: **LIVE (paper-only) — scheduler DISABLED — NOT YET AUTHORIZED.**

This document is the enforceable safety contract for the Daily Paper Trading
Track. Every rule below is either hard-coded into the track, verified by a test,
or a standing operator prohibition. The track exists to make paper trading a
fail-closed, deterministic, fully isolated activity — **and nothing more.**

---

## 1. The hard rules (do not violate)

1. **No live orders, ever.** Every order is simulated by
   `broker/paper_broker.py::PaperBroker` with `is_live = False` hard-coded
   (`paper_broker.py:46`); the constructor raises if it is ever passed a truthy
   value (`:53-54`). No call path in the track reaches `execution/gate.py`,
   `execution/manager.py`, or any live adapter.
2. **No scheduler.** The Windows Task Scheduler is **DISABLED for the paper
   track until a separate, explicit approval step.** The enabling command is
   *never executed here*; it is delivered as text in the final approval report
   only. Until then `schtasks /query /tn "FNO_PaperTradingDaily"` must return
   "not found".
3. **Do not touch the live gate or execution adapters.** `execution/` is
   read-only for the track; its closed-state behavior (`ExecutionMode.PAPER`
   unless `FNO_LIVE_EXECUTION_TEST_ENABLED=1` + consent + token fingerprint) is
   left intact. The track is proven not to import those modules (see §3).
4. **Do not touch protected / Fresh-OOS.** The track never calls
   `fresh_oos_validation.consume`, `.job.run_controlled_validation`, or any
   research run consuming protected OOS; it never writes to `data/fresh_oos/`,
   `datasets/`, or `fresh_oos_manifest.json`. Paper artifacts live only under
   `data/paper_trading/` (store), `paper_state/` (session), and
   `reports/paper_trading/` (daily reports + heartbeat).
5. **Do not tune anything.** The strategy is the frozen
   `MovingAverageCrossStrategy(fast=5, slow=21)`; risk controls, costs, and
   sizing are the frozen shared defaults. No parameter tuning, no promotion:
   paper results can never flip `PromotionRegistry`/gate or mark any algorithm
   READY/uploaded.
6. **No credentials.** Tokens are read only from the environment (the `upstox`
   command reads `FNO_UPSTOX_ACCESS_TOKEN`); never accepted on a command line,
   never printed, never persisted into any report or checkpoint.
7. **Publication policy.** The track is version-controlled on the authorized
   branch (`master` → `origin/master`). Only reviewed phase files are staged;
   credentials, `.env` files, runtime artifacts, and unrelated working-tree
   changes are never staged or pushed.

---

## 2. Why the isolation exists

The audit that produced `docs/daily_paper_trading_track.md` found three defects
in the pre-existing paper path that this track deliberately avoids (in-isolation,
without changing shared modules):

- **Interval mismatch (High):** `scripts/run_paper_agent.py:75` builds
  `UpstoxHistoricalDataProvider` with no interval → provider default `"1d"`
  (`data/upstox_provider.py:149,164,463`) → daily bars enter a 5m session
  (`services/paper_session.py:297`). The track always passes `interval="5m"`.
- **No pre-trade naked-short guard (High):** the shared `RiskManager`
  (`risk/manager.py:73-78`) allows a SELL that would result in a negative
  quantity. The track refuses any SELL while flat and is long-only everywhere.
- **Fill-before-accounting without rollback (High):**
  `services/trading_service.py:96-101`. The track applies fills atomically and
  reconciles the ledger at every day end.

The track additionally closes the audit's medium findings locally: broker-level
order-id idempotency, single-writer run lock, restart cross-validation against
the last checkpoint, deterministic injected clock, and per-day bounded
persistence (O(sessions² · bars), far below any leak threshold).

---

## 3. Proven by subprocess (test_paper_track_isolation.py)

The isolation suite runs real subprocesses and asserts:

- **Import graph:** a real `TrackEngine` run imports none of
  `fno_ai_paper_trading.{fresh_oos, fresh_oos_validation, execution,
  live_scheduler}`.
- **Live temptation:** an engine run under an environment that sets
  live-execution flags still stays paper (no live gate decision is ever
  consulted).
- **Source tree scan:** the track's source contains none of the forbidden
  tokens (`fresh_oos`, `execution.manager`, `execution.gate`,
  `live_execution_test`, `live_scheduler`, `kiteconnect`). The word `upstox` is
  *allowed* — the runner legitimately imports the read-only upstox provider for
  `cmd_upstox`.
- **Report cleanliness:** `assert_report_clean()` finds zero credential/live
  tokens in every report field, and a deliberately-tampered report is flagged.

---

## 4. Fail-closed behavior (the track's own rules)

- Outside the operating window (weekend/holiday/pre-open/after-close) the
  engine SKIPs: no fetch, no orders, clean exit.
- A bar that fails validation is poisoned and skipped with a `data_skips`
  entry — it can never reach the strategy, a signal, or a broker.
- Entries are RiskManager-gated and logged in `entry_approvals`; exits
  (SELL/stop/EOD flatten) are protective and bypass the entry gate only in the
  deliberate, safe way the design specifies.
- Daily-loss cap reached → no further entries; exits and stops still
  executable.
- Stop-loss: 2% post-slippage, entry candle excluded, gap→open exit.
- At 15:20 the engine flattens; ≥15:30 an open position triggers an emergency
  flatten; any checkpoint that would carry an overnight position is refused
  (`TrackRecoveryError`).
- One process per run: the run lock (600s stale-after, stale locks broken)
  guarantees serialized, idempotent execution; a repeated run-id cannot
  double-execute.

---

## 5. Operator prohibitions (standing)

- Never point the track at live data feeds for execution — the upstox command is
  read-only historical polling only.
- Never bypass the gate via `FNO_LIVE_EXECUTION_TEST_ENABLED` for the track.
- Never add an entry point or flag that would enable the scheduler from the CLI.
- Never reuse the track's store for research evidence — paper evidence is
  labeled with strategy identity + run-id + policy so it can never be mistaken
  for Fresh-OOS validation evidence.
- Never delete the shield defenses (the isolation tests) as "too strict".

---

## 6. Status carried by this track (read-only inputs)

- Algorithm health: **RED**, promotion: **NO**, live-execution gate: **CLOSED**.
  The track observes these; it cannot change them. Carry them forward exactly in
  any report or handoff.

---

## 7. Accident/refusal playbook

| Observing | Do |
|---|---|
| Invariant violation printed by any command | Do not paper-trade; fix + rerun the full track suite before any session |
| `data_skips` / `errors` non-empty in a report | Treat the session as incomplete; replay deterministically from the checkpoint |
| `schtasks` shows a paper task | Disable immediately (`schtasks /end` / `/delete`) and re-audit §1 |
| A report containing a token-like string | Quarantine; never publish; the report is non-compliant by construction |
| A live-gate decision in a paper trace | Stop; the isolation boundary was breached; re-run the isolation suite |