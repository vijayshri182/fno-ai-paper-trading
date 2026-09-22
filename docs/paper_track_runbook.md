# Daily Paper Trading Track — Operations Runbook

Status: **LIVE (paper-only) — scheduler DISABLED — NOT YET AUTHORIZED.**

This track is a fully separate, intraday, long-only, single-position, 5-minute
NIFTY-50 paper simulation. It never places a real order, never routes to any
execution adapter, and never runs unattended: **the Windows Task Scheduler is
currently DISABLED and will not be enabled without a separate, explicit approval
step.** See `docs/paper_track_safety.md` for the isolation contract.

---

## 1. What it is

- One cumulative account (default `nifty_5m_daily`) that trades once per day.
- Strategy: frozen `MovingAverageCrossStrategy(fast=5, slow=21)` on completed
  5-minute bars (bar `T` is decided at `T+5`).
- Long-only, one position, no entries by shorts, no overnight risk (flat at day
  end), no positions after the daily-loss cap.
- Risk and costs are byte-identical to the frozen research model: 1% risk /
  2% stop / lot-rounded / cash-fit sizing; stop-loss 2% post-slippage, entry
  candle excluded; commission 0.03%, slippage 0.1%, initial cash 100000.
- Every order is simulated by `PaperBroker` (`is_live = False`, hard-coded).

### Session gate (all times NSE/IST)
| Window | Policy |
|---|---|
| Weekend / holiday / pre-open | clean SKIP — no fetch, no orders |
| 09:15 – 15:20 | TRADING — entries and exits allowed |
| 15:20 – 15:30 | FLATTENING — exits/stops/flatten only, no new entries |
| ≥ 15:30 | CLOSING — emergency flatten if a position is still open |

Every processed bar triggers a per-day checkpoint (SHA-256 protected) under a
single-writer run lock, so a restart at any point resumes deterministically.

---

## 2. Commands

Run from the repo root with the project virtualenv:

```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py <command> [opts]
```

Every command prints the banner:

```
PAPER-ONLY: no live orders; scheduler remains DISABLED — NOT YET AUTHORIZED
```

### 2.1 `smoke` — one deterministic synthetic session
```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py smoke --account nifty_5m_daily
```
Uses today's trading day, a synthetic feed seeded `--seed` (default
`20260921`). Prints the daily report path, accounting block, fingerprint, and
final state. Exits `0` on clean invariants; `2` if any invariant or
credential-leak check fails.

### 2.2 `simulate` — N deterministic synthetic sessions
```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py simulate --days 30 --seed 555
```
Runs one cumulative engine across the next N NSE trading days, ends flat, and
cross-checks all 15 invariants. Uses the frozen `FixedClock` (start 1970-01-01),
so runs are fully deterministic: the same `--seed` reproduces the same
fingerprint, realized P&L, and cash byte-for-byte.

Reference result (verified) for `simulate --days 5`:
`realized_pnl=432.54463 cash=100222.417358891 fills=18 data_skips=0 errors=0`.

### 2.3 `list` — recorded runs
```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py list
```
Prints run-id → metadata from the store manifest under `data/paper_trading/`.

### 2.4 `checkpt` — checkpoint integrity
```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py checkpt
```
Lists every per-day checkpoint for the account with run-id, bars consumed,
EOD status, and fill count.

### 2.5 `upstox` — one real-5m-bar session (read-only)
```powershell
$env:FNO_UPSTOX_ACCESS_TOKEN = "<token>"   # from .env only, never inline
.\.venv\Scripts\python.exe scripts\run_paper_track.py upstox
```
Fetches historical 5-minute bars via the read-only provider. **The track always
passes `interval="5m"` explicitly** — this is the deliberate fix for the upstream
`run_paper_agent.py` defect where the provider default (`"1d"`) fed daily bars
into a 5m session. The token is read from the environment **only**; it is never
accepted on a command line, printed, or written to any report.

Two properties make the seam honest (added 2026-09-22):

* **Symbol-aligned instrument.** The engine is given *the same* `Instrument`
  object the provider resolves for "NIFTY 50" (symbol `Nifty 50`). Without this
  the bar validator rejected every real bar as `unexpected instrument 'Nifty 50'`
  (a case mismatch against the canonical `NIFTY 50`), so a session could
  "complete" with zero fills and zero data errors while silently consuming
  nothing.
* **Fail-closed empty session.** If the endpoint serves no completed bars for the
  day (verified for the current session), `upstox` prints the reason, removes the
  empty report it would otherwise leave behind, and exits `2`. The zero-bar
  signal is `engine.last_processed is None` — the session tick-clock always
  advances (idle ticks), so a clock-based check could never detect a bar-less
  day.

### 2.6 Dev-only injection points
`--failpoint A..J` injects a simulated process crash at a specific point in the
per-tick pipeline (test/dev only). It exists so disaster recovery can be
exercised; it has no role in normal operation.

### 2.7 `status` — account summary + cumulative reconciliation
```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py status
```
Observer command (token-free, offline). Prints the account's span, start/final
cash, lifetime net (gross − costs), cumulative fill/entry/exit/stop/error
counts, the last persisted day's EOD status, every cumulative reconciliation
result, and the stable fingerprint. Exits `0` when the persisted state
reconciles cleanly, `2` if a persisted report is corrupt or fails to reconcile
(fail-closed observability), and `0` with "no persisted days" on an empty
account.

### 2.8 `report` — one persisted day's full paper report
```powershell
.\.venv\Scripts\python.exe scripts\run_paper_track.py report [--day YYYY-MM-DD]
```
Dumps one day's report JSON (defaults to the latest persisted day). Exits `0`
on success, `1` if the day has no report, `2` if the report fails its hash /
credential-freedom check (`TrackCheckpointError`).

---

## 3. Determinism contract

- A tick's bar batch is filtered to bars strictly after the last consumed
  timestamp before validation, so cumulative/overlapping provider deliveries
  never re-poison already-consumed bars (no spurious "repeated bar" anomalies).
- Bars validated by `BarValidator` are classified as `valid` / `prior`
  (legitimate history from earlier sessions) / `poisoned` (tampered or
  future-dated). Only `valid` bars are consumable; `poisoned` bars increment
  `data_skips` and never reach the strategy or a broker.
- Fill prices use bar-close + slippage; timestamps in fingerprints are excluded,
  so wall-clock `filled_at` values never leak into the determinism signal.
- A restart from the last checkpoint resumes **at the crashed tick itself**
  (a crashed tick never persisted its work), reproducing the un-crashed run
  byte-for-byte.

---

## 4. What a day produces

Under `data/paper_trading/` (per account):
- `checkpoints/<account>.<DATE>.json` — per-day state (last write per day wins;
  each embeds the cumulative history needed for deterministic resume).
- `reports/<account>.<DATE>.json` — daily report: strategy identity, run-id,
  lifecycle, orders, fills, accounting, risk, data-quality, equity, EOD status,
  fingerprint.
- `manifest.json` — run records (`paper-run-*`).

The daily report is guaranteed free of credentials by `assert_report_clean()`
(redaction tokens: `access_token`, `upstox`, `kite`, `dotenv`, `client_secret`,
`password`, `token=`).

---

## 5. Verification checklist (before any go-live)

1. `smoke` exits 0 with `data_skips=0` and `errors=0`.
2. `simulate --days 30` exits 0, ends flat, all 15 invariants clean.
3. Full track test-suite green (see §6); full repo suite keeps its baseline
   (2379 passed, 2 skipped; the 4 documented failures are pre-existing and are
   listed in the FINAL REPORT's KNOWN FAILURES section).
4. `checkpt` shows one checkpoint per trading day, each `eod` in
   `{FLAT, FLATTENED}`.
5. Algorithm status is unchanged HTTP `RED` / promotion `NO` / live gate
   `CLOSED` — the track cannot flip any of these (see safety doc §6).
6. Scheduler disabled: `schtasks /query /tn "FNO_PaperTradingDaily"` returns
   "not found" until the enabling step is separately approved.

---

## 6. Test suites (the track's contract)

All under `tests/` using the shared `paper_track_testkit.py` helpers:

| Suite | Verifies |
|---|---|
| `test_paper_track_bars.py` | adversarial bar validation, poisoning, prior/history classification |
| `test_paper_track_orders.py` | order lifecycle, risk refusals, no shorts, loss-cap behavior |
| `test_paper_track_eod.py` | gate boundaries, flatten/no-overnight guarantees |
| `test_paper_track_accounting.py` | independent P&L/cash/commission reconciliation |
| `test_paper_track_report.py` | report schema, invariants, fingerprint stability, credential freedom |
| `test_paper_track_crash.py` | failpoints A–J, per-tick crash recovery, byte-identical resume |
| `test_paper_track_concurrency.py` | single-run lock, stale-lock breaking, heartbeat |
| `test_paper_track_isolation.py` | subprocess proof the track cannot touch live/oos/execution code |
| `test_paper_track_simulation.py` | 30-session determinism, per-day invariants, restart parity, bounds |
| `test_paper_track_server_runner.py` | daily server seam: CLI command, Phase-2 token source, Phase-3 feed seam, PaperBroker backend, duplicate-day refusals, no-live gate |
| `test_paper_track_daily_session.py` | warm-up orchestration, `run_sessions`, report/checkpoint wiring |
| `test_paper_track_multiday.py` | cumulative account across days, deposit preservation, restart determinism, day-completion guards |
| `test_paper_track_cumulative.py` | `build_cumulative_report` reconciliation, per-day deltas, fingerprint |
| `test_paper_track_recovery.py` | stale-lock reclaim, corrupted-checkpoint refusal, near-close crash + resume byte-parity, runner heartbeat |
| `test_paper_track_cli.py` | `status` / `report` observer commands, rc contract, existing commands intact |
| `test_paper_track_token_provider.py` | env-only, fail-closed token resolution (never reads the live-execution token) |
| `test_paper_track_upstox_ws.py` | WebSocket feed seam is token-safe and not wired to execution |
| `test_upstox_warmup_instrument.py` + `_runs` | register-backed NIFTY 50 instrument for 5m warm-up, provider wiring |

Run the fast suites:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_paper_track_bars.py tests\test_paper_track_orders.py tests\test_paper_track_eod.py tests\test_paper_track_accounting.py tests\test_paper_track_report.py tests\test_paper_track_crash.py tests\test_paper_track_concurrency.py tests\test_paper_track_isolation.py tests\test_paper_track_multiday.py tests\test_paper_track_cumulative.py tests\test_paper_track_recovery.py tests\test_paper_track_cli.py tests\test_paper_track_server_runner.py tests\test_paper_track_daily_session.py tests\test_paper_track_token_provider.py tests\test_paper_track_upstox_ws.py tests\test_upstox_warmup_instrument.py tests\test_upstox_warmup_instrument_runs.py -q
```
The long simulation suite (15–30 sessions) is marked `slow` and takes several
minutes:
```powershell
.\.venv\Scripts\python.exe -m pytest "tests\test_paper_track_simulation.py" "tests\test_paper_track_orders.py" -q
```

---

## 7. Real-market paper evidence (2026-09)

Replayed through the **frozen on-paper contract** (MA(5,21) default, SessionGate
09:30→entry / 15:20 flatten, 1%-per-trade risk sizing, 2% stop, broker
commission + slippage):

| Window | Bars | Result (paper execution only) |
|---|---|---|
| 14 completed NSE sessions, 2026-09-01 → 2026-09-21 (1050 real 5m bars, read-only Upstox endpoint) | 1050 | 26 round trips, **1 win / 25 losses (3.8% win rate)**, gross profit ₹75.29, gross loss ₹3,195.08, costs ₹732.44, **lifetime net −₹3,119.79**, avg trade −₹120.00, max day-end drawdown ₹3,119.79, 0 stop-outs, 4 EOD flattens, 0 data errors, reconciliation `True` |

Cumulative fingerprint `8f1c0f7650d2ac2fa92b49afc5c1307068c0cbf4055f1e8f57230375dbea9f67`
(account `realformer15b`, evidence store under
`%TEMP%\opencode\evidence_store`, reproducibility script kept out of the repo by
design). Every session ended `FLAT`/`FLATTENED`; the track never risked an
overnight position.

### 7.1 Honest reading of this window

* The frozen MA(5,21) strategy **disconfirms** on real NIFTY 5m in this window —
  this is genuine evidence, not a fabricated payout. It matches the shipped
  research audit (`docs/champion_failure_audit.md`): MA(5,21) has no gross edge
  and churned through 26 round trips in 14 sessions.
* The value of the trail is the **faithful execution**, not the P&L: real bars →
  validation → strategy → sizing → paper broker → hashed report → cumulative
  reconciliation all ran with zero data errors or invariant violations.
* This is prior-session **bar replay**, not a live session. The read-only
  historical endpoint serves no bars for the current session (verified
  2026-09-22: NSE session open, `get_historical_ohlcv(..., 09:05 → 10:31)`
  returned 0 bars for today while 75-bar complete sessions existed for every
  prior trading day). A full-session **live** validation is therefore not
  achievable with the existing seam and is reported as an open limitation; the
  track has no live-execution path and no scheduler anywhere in its source
  (see `docs/paper_track_safety.md`).
* Separately, an earlier 5-session replay (09-15..09-21) run with tomorrow's
  *unfixed* seam produced 0 fills and 0 data errors — silently void. That defect
  is what §2.5 documents; never treat a zero-fill report as "validated".