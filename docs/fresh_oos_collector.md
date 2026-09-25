# Fresh OOS Collector & Scheduler — architecture and deployment

Status marker: code **implemented locally and fully tested**. **Local laptop
deployment configured on 2026-09-20** via Windows Task Scheduler (see "Local
deployment record"); the scheduler is restart-safe (existing collector lock,
stale-lock protection kept) and non-overlapping
(`MultipleInstances=IgnoreNew` + the lock). The token is consumed **dynamically
at runtime** by the credential provider (see "Runtime credential model") from
the account's *user-level environment variable*
(`FNO_UPSTOX_ACCESS_TOKEN`) -- never from the repo, task arguments/XML, working
directory, manifest, logs, reports, source files or command history. The token
is **PRESENT** at the user-environment source on this deployment (operator
provisioned after verification), so scheduled passes now acquire; the scheduled
path is additionally gated by the **NIFTY market-session gate** (see "NIFTY
market-session gate"): no Upstox request is made when the NIFTY cash session is
closed.

## Local deployment record (2026-09-20)

- Windows Task Scheduler task `FNO_FreshOosCollector`, state `Ready`, runs as
  the current account (`user`, SID `S-1-5-21-2908977792-2441188351-1009931771-1003`,
  Interactive/limited, no password).
- Action: `<repo>\.venv\Scripts\python.exe` `-m fno_ai_paper_trading.fresh_oos.scheduler --once`, working directory `<repo>`.
- Trigger: repeating time trigger every 15 minutes (PT15M, 10-year duration), enabling day-batch/`--once` catch-up.
- Settings: `MultipleInstances=IgnoreNew`, `StartWhenAvailable`, 30-min execution limit,
  battery conditions set to allow running on battery (`DisallowStartIfOnBatteries=false`,
  `StopIfGoingOnBatteries=false` -- the PS 5.1 registration defaults block battery runs;
  verified and corrected on 2026-09-20).
- Safeguards unchanged: exclusive collector lock (`.collector.lock`, `O_CREAT|O_EXCL`,
  15-min stale takeover) prevents overlap/restart races; the lock and stale protection
  were **not** weakened.
- Complete task XML was audited: only the venv python, the module arguments, the
  working directory and the account SID appear -- **no credential named, stored or
  computed anywhere** (verified `FNO_UPSTOX_ACCESS_TOKEN` absent from `<Environment>`,
  `<Arguments>`, `<Command>`, `<WorkingDirectory>`).
- Credential state: dynamic provider now reports `PRESENT` for the environment-var
  source. The scheduled pass has since caught up the freshly-eligible days strictly
  after 2026-09-11 (2026-09-14 recorded `NO_DATA` -- the provider returned zero bars
  for that session date; 2026-09-15/16 reused `NOOP_ESTABLISHED` from `datasets/`;
  2026-09-17/18 stored `ACQUIRED`, 75 bars each, SHA-256 verified). From now on the
  **NIFTY market-session gate** prevents any network request while the session is
  closed (see below).

## NIFTY market-session gate

The scheduled entry point (`python -m fno_ai_paper_trading.fresh_oos.scheduler
[--once]`) evaluates a deterministic NIFTY cash-session gate
(`fresh_oos/session_hours.py` :func:`evaluate_session`) **before** invoking the
collector. This is an efficiency guard only: it never changes storage,
boundary or integrity rules, never makes a network request, and never touches
credentials.

- Time zone: `Asia/Kolkata` (fallback `UTC+05:30`, identical since India has no
  DST) -- explicit and machine-local-timezone-independent.
- Session window: Monday-Friday `[09:15, 15:30)`: 09:15:00 OPEN, 15:29:59 OPEN,
  15:30:00 CLOSED, before 09:15 CLOSED.
- Weekends (Saturday/Sunday) are always CLOSED.
- When CLOSED the pass returns a clean SKIP (exit 0): no lock taken, no Upstox
  client request, no candle fetch, no manifest change, no date marked `NO_DATA`,
  and -- critically -- an absent token does **not** turn into `AUTH_REQUIRED`
  (the credential/network path is never reached). The audit line prints a
  machine-readable reason: `WEEKEND` or `OUTSIDE_NIFTY_MARKET_HOURS`.
- Manual collection via `collect --once` remains ungated; direct/manual
  acquisition stays available whenever explicitly invoked.
- NSE holiday awareness is **NOT** implemented: weekends are the only known
  closed days. `data/market_hours.HOLIDAYS_2026` is explicitly best-effort and
  advisory and is not reused by the gate; full holiday awareness belongs to a
  trusted NSE calendar the repository does not (yet) ship.
- Historical Fresh-OOS data integrity and the OOS boundary (strictly after
  2026-09-11) are unchanged by the gate.
- Scheduler frequency is unchanged (15 minutes); the Task Scheduler definition
  is unchanged.

## Runtime credential model

Architecture (`Task Scheduler -> scheduler --once -> credential provider -> Upstox V3 client`):

```
FNO_FreshOosCollector (Task Scheduler, same user)
   -> python -m fno_ai_paper_trading.fresh_oos.scheduler --once
      -> RuntimeCredentialProvider  (fresh_oos/credential_provider.py)
         -> resolves FNO_UPSTOX_ACCESS_TOKEN from the process environment
            only when a fetch is attempted; returned in memory only
      -> UpstoxHistoricalDataClient -> GET /v3/historical-candle (read-only)
```

Guarantees of `fresh_oos/credential_provider.py`:

* resolve at runtime only -- no construction-time reads, no CLI arguments;
* in-memory only -- never persisted, printed, logged or written to any file;
* operator reporting is exactly `PRESENT` / `ABSENT` (status CLI + `probe()`),
  never the value or a fingerprint;
* fail-closed -- absent token raises `CredentialsUnavailableError`, recorded as
  `AUTH_REQUIRED` (zap network);
* the resolved token is stripped from downstream exception text
  (`redact_error_text`) before it can reach the manifest;
* works under Task Scheduler -- Windows task-launched processes inherit the
  account's user-level environment variables (empirically verified on this
  machine with a non-secret probe variable).

**Why user-level environment, not `.env`:** the fresh-OOS process never calls
`load_dotenv` (by design it is server-launchable and credential-agnostic). A
token placed in the git-ignored repo `.env` would be invisible to the scheduled
process -- and placing credentials in a project file is exactly what this model
forbids. The account's user environment variable is the existing, supported
mechanism; it lives in `HKCU\Environment` (the same registry store Windows uses
for all user env vars), is inherited by Task Scheduler processes, and never
touches the repository, task XML, manifest, logs or command history.

**Provisioning (operator, once):** run the following in a PowerShell console so
the value never appears in the command line / command history:

```powershell
$secure = Read-Host -AsSecureString -Prompt "Upstox analytics/data token"
$plain  = [System.Net.NetworkCredential]::new("", $secure).Password
[Environment]::SetEnvironmentVariable("FNO_UPSTOX_ACCESS_TOKEN", $plain, "User")
```

(The value is stored in the Windows user environment, i.e. `HKCU\Environment`;
a Task Scheduler process launched under this account picks it up at runtime --
no re-logon required, as verified on this deployment.) The next scheduled
`--once` pass then performs GET-only fresh-OOS acquisition; `status` will show
`Credential : PRESENT`.

## What this is

A deterministic, immutable, READ-ONLY acquisition service for NIFTY 50 5-minute
bars recorded **strictly after** 2026-09-11 (the lineage's consumed protected
OOS window ends 2026-09-11 inclusive). It is the only sanctioned way fresh OOS
candles enter the system: detect -> fetch (GET-only Upstox REST V3 historical)
-> validate -> SHA-256 -> atomic store -> manifest -> readiness report.

It never triggers validation, never tunes parameters, never places orders, and
never imports the strategy/execution/research stack.

## Layout

```
src/fno_ai_paper_trading/fresh_oos/
  protocol.py      boundary, thresholds, statuses, RunOutcome, ReadinessReport
  errors.py        typed errors + exception->status mapping + credential redaction
  credential_provider.py  runtime token resolver (PRESENT/ABSENT; in-memory only; redacts)
  client.py        HistoricalDataClient seam + Upstox adapter (GET historical only)
  validation.py    75-bar session integrity checks (report-only, never repairs)
  store.py         immutable per-day store + established datasets/ scan
  manifest.py      protocol block, pool, run log, readiness (atomic JSON)
  lock.py          cross-process run lock (O_EXCL, stale takeover)
  factory.py       wiring shared by CLIs and scheduler
  collector.py     collect_once orchestration (chronological catch-up)
  scheduler.py     resident loop (or one-shot for Task Scheduler/cron)
  session_hours.py deterministic NIFTY session gate (09:15-[15:30) IST; weekdays only)
  collect.py       CLI: acquire (--once default, --date, --json, --no-verify...)
  status.py        CLI: read-only status block
```

Store layout (byte-identical to the `datasets/` CSV + SHA-256 convention, so an
accepted day is hash-comparable with established datasets):

```
data/fresh_oos/
  NIFTY_50_5m/YYYY-MM-DD/data.csv
  NIFTY_50_5m/YYYY-MM-DD/metadata.json
  NIFTY_50_5m/YYYY-MM-DD/sha256.txt
  fresh_oos_manifest.json
```

Consumed-window single-day files already present in `datasets/` (e.g. the WS
7.26 sessions `upstox_Nifty_50_5m_20260915_20260915` / `...20260916...`) are
detected and reused (`NOOP_ESTABLISHED`) — never duplicated or overwritten. A
pre-boundary day such as `20260910` is excluded from reuse: it belongs to the
consumed protected window.

## Operating commands

Everything is `python -m` driven from the venv (importable because the venv
carries a `fno-src.pth` pointing at `src/`; alternative wiring is documented in
"Environment bootstrap").

```powershell
# Read-only status (never writes, never touches the network; shows Credential: PRESENT/ABSENT)
.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.status

# One catch-up pass over every eligible completed trading day after 2026-09-11
.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.collect --once

# Force exactly one date (still boundary- and integrity-enforced)
.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.collect --once --date 2026-09-17

# Reuse locally-verified established datasets without re-fetching them
.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.collect --once --no-verify

# Resident scheduler (guardable loop; default interval 900s)
.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.scheduler
```

Environment overrides: `FNO_FRESH_OOS_ROOT`, `FNO_FRESH_OOS_DATASETS_DIR`,
`FNO_FRESH_OOS_MAX_DAYS_PER_RUN`, `FNO_FRESH_OOS_SCHEDULE_SECONDS`. Credentials
come only from the runtime credential provider reading the user-level
environment variable `FNO_UPSTOX_ACCESS_TOKEN` (never printed, never persisted,
never in CLI args/manifest/logs; missing token -> `AUTH_REQUIRED` exit, no data
modified).

Exit codes: 0 clean/no-op, 1 failure status, 2 usage error.

## Safety invariants (enforced and tested)

- Fresh OOS is **strictly after 2026-09-11**. Any forced date or returned bar on
  or before the boundary fails the run (`PROTOCOL_VIOLATION`); nothing is
  silently filtered and nothing is stored. The protected consumed window
  (`2025-10-06..2026-09-11`) and the pre-2026 eval window are covered by the
  same firewall.
- GET-only historical data; no execution endpoint is reachable through the
  client; paper/live order counts remain 0.
- Atomic, refus-to-overwrite writes: scratch + fsync + `os.replace`; re-reads
  re-verify the SHA-256 against both metadata and `sha256.txt`.
- Deterministic catch-up; restart-safe manifest loading; cross-process run lock
  with stale takeover; read-only `status`.
- No strategy/execution/promotion/walkforward imports (checked by tests via AST
  import-graph scanning).
- Readiness is `DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION` only when
  `>=20 trading days AND >=1500 bars` are accepted; never `VALIDATION_READY`.
  The `>=30 trade` requirement is evaluated later only by the separate
  single-use validation job — the collector never runs it.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\test_fresh_oos_boundary.py `
  tests\test_fresh_oos_integrity.py tests\test_fresh_oos_idempotency.py `
  tests\test_fresh_oos_atomicity.py tests\test_fresh_oos_scheduler.py `
  tests\test_fresh_oos_readiness.py tests\test_fresh_oos_safety.py `
  tests\test_fresh_oos_cli.py tests\test_fresh_oos_credentials.py `
  tests\test_fresh_oos_session_gate.py -q
```

`tests/fresh_oos_testkit.py` supplies the deterministic bar factory and a fake
client, so the suite is 100% network-free.

## Deployment steps (this laptop: done 2026-09-20; other/server hosts)

This repo's laptop is deployed and scheduled (see "Local deployment record").
To deploy on another host with the same guarantees:

1. Provide the token as the **user-level environment variable** (never a repo
   file, never the task command line):
   `[Environment]::SetEnvironmentVariable("FNO_UPSTOX_ACCESS_TOKEN", <value>, "User")`
   (prefer reading the value with `Read-Host -AsSecureString` so it never lands
   in command history; on Linux/macOS export it in the service account's
   environment).
2. Verify `.venv\Scripts\python.exe -m fno_ai_paper_trading.fresh_oos.status`
   from the repo venv shows `Credential : PRESENT`, then run `collect --once`
   and confirm the JSON summary (AUTH_REQUIRED is the expected result until the
   token is provisioned).
3. Schedule it. Two supported options:
   - Windows Task Scheduler: run the venv python with
     `-m fno_ai_paper_trading.fresh_oos.scheduler --once` every N minutes
     (no credential anywhere in the task); or
   - Resident mode: `-m fno_ai_paper_trading.fresh_oos.scheduler` under a
     supervisor (runs a pass each interval, guarded against overlap).
4. Do **not** wire any fresh-OOS validation or trade-count evaluation here; that
   is a separate, later, single-use controlled job.

## Environment bootstrap

The repo venv has no `fno_ai_paper_trading` package installed and no
setuptools. Two equivalent ways to make `python -m fno_ai_paper_trading.*`
resolve, from the repo root:

- Place `C:\Vijay_GitHub\fno-ai-paper-trading\src` on `PYTHONPATH`
  (`set PYTHONPATH=src`), or
- Install the package in editable mode once tooling is available, or
- Use the local `.pth` (this repo): the file
  `.venv\Lib\site-packages\fno-src.pth` contains the absolute `src` path so the
  commands above already work in this venv.