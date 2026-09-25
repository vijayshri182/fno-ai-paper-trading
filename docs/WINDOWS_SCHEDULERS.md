# Windows Task Schedulers — F&O data tasks

Status marker: **documentation only**. This file records the *verified current
configuration* of the two F&O Windows Task Scheduler tasks on this machine
(WS-14777, account `user`, SID `S-1-5-21-2908977792-2441188351-1009931771-1003`).
It does not change any task, any code, any gate, or any Watchdog rule.

## A. Overview

There are two F&O scheduler tasks. They are complementary and one is strictly
read-only:

| Task | Data scope | Safety role |
| --- | --- | --- |
| `FNO_FreshOosCollector` | Completed *historical* NIFTY 50 5-min trading days, per its existing eligibility logic (fetch is attempted for eligible past dates, **not** for today) | Read-only historical acquisition; never places orders |
| `FNO_Today5mReadinessMonitor` | *Today's* Upstox NIFTY 50 5-min candles, during the live NSE window | Read-only market-data readiness probe; never places orders; never launches trading |

Explicit facts to keep in mind:

- `FNO_FreshOosCollector` processes **completed historical dates** according to
  its existing eligibility logic (`eligible_dates` requires `day < today`). It
  is the only sanctioned way fresh-OOS historical candles enter the system
  (see `docs/fresh_oos_collector.md`).
- `FNO_Today5mReadinessMonitor` specifically checks **today's** Upstox 5-minute
  candles in the NSE derivative session window and reports whether they are
  available and Watchdog-fresh.
- The readiness monitor is **read-only**: it performs a GET-only probe plus the
  production fetch path routed by date — **Intraday Candle V3** for the current
  trading day (`UpstoxHistoricalDataClient.fetch_intraday_day`) with **no**
  Intraday→Historical fallback, and **Historical V3** for completed days
  (`UpstoxHistoricalDataClient.fetch_5m_day`). No order execution, no order
  placement, no execution/paper-trading API is imported or reachable (enforced
  by tests).
- **Neither task is allowed to place BUY/SELL orders.**
- `FNO_Today5mReadinessMonitor` does **NOT** trigger live trading when it
  becomes `READY`. `READY` is a *market-data readiness signal only*; actual
  live trading requires a separate, explicit, operator-controlled step.

## B. Task configuration

This is the configuration as verified live on the machine (25-09-2026). Do not
treat it as authority to change the tasks.

### 1. `FNO_FreshOosCollector`

- **Task name:** `FNO_FreshOosCollector`
- **Purpose:** runs the existing Fresh-OOS collector/scheduler for historical
  5-min NIFTY data — a deterministic, immutable, read-only acquisition service
  for NIFTY 50 5-minute bars recorded strictly after the protected OOS boundary.
- **Executable:** `C:\Vijay_GitHub\fno-ai-paper-trading\.venv\Scripts\python.exe`
- **Arguments:** `-m fno_ai_paper_trading.fresh_oos.scheduler --once`
- **Working directory:** `C:\Vijay_GitHub\fno-ai-paper-trading`
- **Schedule:** repeating time trigger every 15 minutes (`PT15M`), duration
  `P3650D` (10 years), `StopAtDurationEnd = True`. The task is enabled
  (`Scheduled Task State: Enabled`).
- **Run as:** `user` (Interactive/limited, no password).
- **Settings:** `MultipleInstances=IgnoreNew`, `StartWhenAvailable=true`,
  30-min execution limit, batteries allowed.
- **Important safety behavior:** the scheduled entry point applies the NIFTY
  market-session gate *before* any Upstox request; it is restart-safe
  (collector lock, stale-lock protection), non-overlapping, and credential-free
  (token resolved at runtime from the user-level environment variable
  `FNO_UPSTOX_ACCESS_TOKEN`; never stored in the task). It never places orders
  and never imports strategy/execution/research stacks.

### 2. `FNO_Today5mReadinessMonitor`

- **Task name:** `FNO_Today5mReadinessMonitor`
- **Purpose:** runs the read-only `scripts\check_today_upstox_5m.py` during NSE
  trading hours to determine whether today's Upstox NIFTY 50 5-minute candles
  are available and fresh enough for the live-data prerequisite.
- **Executable:** `C:\Vijay_GitHub\fno-ai-paper-trading\.venv\Scripts\python.exe`
- **Arguments:** `C:\Vijay_GitHub\fno-ai-paper-trading\scripts\check_today_upstox_5m.py`
- **Working directory:** `C:\Vijay_GitHub\fno-ai-paper-trading`
- **Schedule (verified trigger `MSFT_TaskWeeklyTrigger`):**
  - Monday–Friday
  - start 09:15 IST (`StartBoundary 2026-09-25T09:15:00+05:30`)
  - repeat every 15 minutes (`PT15M`)
  - duration `PT6H15M` → the window ends at 15:30 IST (`StopAtDurationEnd = True`)
  - enabled (`Enabled = True`)
- **Settings:** `MultipleInstances=IgnoreNew`, `StartWhenAvailable=true`,
  10-min execution limit, batteries allowed.
- **Verified run info (at doc time):** `LastRunTime 25-09-2026 08:10:40`,
  `LastTaskResult 1` (= the monitor's intentional `NOT_READY` exit; see §D),
  `NextRunTime 25-09-2026 09:15:45`.

#### Readiness states (exactly)

The monitor reports exactly one of three final states, and maps them to exit
codes:

```text
TODAY_5M = READY      exit code 0
TODAY_5M = NOT_READY  exit code 1
TODAY_5M = ERROR      exit code 2
```

- `TODAY_5M = READY` — today's candles exist **AND** the latest candle is within
  the Watchdog freshness window. Market data for today is available and fresh.
- `TODAY_5M = NOT_READY` — zero candles, candles not dated today, or the latest
  candle is outside the Watchdog freshness window. Today's data is not yet ready
  (this is the normal pre/post-market or data-lag state; **HTTP 200 with an
  empty `candles` array is NOT an error** — it exits 1).
- `TODAY_5M = ERROR` — HTTP/API/credential/config failure (exit code 2).

The monitor is read-only: it reuses the production
`UpstoxHistoricalDataClient` (routed by date — `fetch_intraday_day` for the
current day, `fetch_5m_day` for completed days) plus the shared
`UpstoxHistoricalDataProvider` transport and the runtime analytics credential
`FNO_UPSTOX_ACCESS_TOKEN` (the same in-memory runtime credential provider the
collector uses — `PRESENT`/`ABSENT` only, never printed or persisted). It uses
the existing `Watchdog` with its **default 5-minute `max_bar_age`** — no new
freshness threshold was introduced and the Watchdog is never bypassed. Today's
candles must come from Intraday Candle V3; when that feed is unavailable the
monitor reports `NOT_READY`/fails closed (the dated Historical endpoint is
never used as today's source). LTP is never substituted for candles and nothing
is fabricated.

Outputs:

- Atomic status file `reports\live_readiness\upstox_today_5m_status.json`
  (scratch + fsync + `os.replace`), containing `checked_at`, `nse_date`,
  `instrument`, `interval`, `session_start/end`, `request`, `source`,
  `http_status`, `api_status`, `candle_count`, `first_candle`, `latest_candle`,
  `latest_candle_age_seconds`, `watchdog_fresh`, `credential_present`, `ready`,
  `state`, `blocker`.
- Human-readable line appended to `reports\live_readiness\upstox_today_5m.log`.

The `source` field is `intraday` for the current trading day (Intraday Candle
V3, path `/v3/historical-candle/intraday/{key}/minutes/5`) and `historical` for
completed days (dated `/v3/historical-candle/{key}/minutes/5/{to}/{from}`).

## C. Verification commands

All commands are run from a PowerShell console with the repository available.
They are read-only — none of them modifies a task.

### 1. Verify task exists/state

```powershell
Get-ScheduledTask -TaskName "FNO_FreshOosCollector" |
    Select-Object TaskName, State, Enabled

Get-ScheduledTask -TaskName "FNO_Today5mReadinessMonitor" |
    Select-Object TaskName, State, Enabled
```

### 2. Verify run information

```powershell
Get-ScheduledTaskInfo -TaskName "FNO_FreshOosCollector" |
    Select-Object LastRunTime, LastTaskResult, NextRunTime

Get-ScheduledTaskInfo -TaskName "FNO_Today5mReadinessMonitor" |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
```

### 3. Verify action and working directory

```powershell
$task = Get-ScheduledTask -TaskName "FNO_Today5mReadinessMonitor"
$task.Actions | Format-List Execute,Arguments,WorkingDirectory
```

Equivalent inspection for `FNO_FreshOosCollector`:

```powershell
$task = Get-ScheduledTask -TaskName "FNO_FreshOosCollector"
$task.Actions | Format-List Execute,Arguments,WorkingDirectory
```

### 4. Verify trigger/repetition

```powershell
$task = Get-ScheduledTask -TaskName "FNO_Today5mReadinessMonitor"
$task.Triggers | Format-List *
$task.Triggers.Repetition | Format-List *
```

Equivalent for `FNO_FreshOosCollector`:

```powershell
$task = Get-ScheduledTask -TaskName "FNO_FreshOosCollector"
$task.Triggers | Format-List *
$task.Triggers.Repetition | Format-List *
```

A full listing of the task registry record (including `Days`, `Repeat: Every`,
`Repeat: Until: Duration`) is available via:

```powershell
schtasks /Query /TN "FNO_FreshOosCollector" /V /FO LIST
schtasks /Query /TN "FNO_Today5mReadinessMonitor" /V /FO LIST
```

### 5. Verify today's readiness status

```powershell
Get-Content "C:\Vijay_GitHub\fno-ai-paper-trading\reports\live_readiness\upstox_today_5m_status.json"
```

### 6. Verify monitor log

```powershell
Get-Content "C:\Vijay_GitHub\fno-ai-paper-trading\reports\live_readiness\upstox_today_5m.log" -Tail 20
```

### 7. Manually run ONLY the read-only readiness monitor

```powershell
cd C:\Vijay_GitHub\fno-ai-paper-trading
.\.venv\Scripts\python.exe scripts\check_today_upstox_5m.py
```

This command runs **only** the read-only readiness monitor. It does **NOT** run
the live execution test and does **NOT** place any order.

## D. Expected readiness interpretation

```text
HTTP 200 + API success + candle_count = 0
    => NOT_READY
```

and

```text
candle_count > 0
+ latest candle within Watchdog freshness threshold
    => READY
```

Important: `LastTaskResult = 1` can represent the monitor's **intentional**
`NOT_READY` exit status and should **not** automatically be interpreted as a
Windows Task Scheduler failure. Distinguish it using the status JSON + log:

- `LastTaskResult = 0` + state `READY` → today's data is fresh and available.
- `LastTaskResult = 1` + state `NOT_READY` → intentional; see the `blocker`
  field for the reason (typically *zero candles* or *stale*).
- `LastTaskResult = 2` + state `ERROR` → HTTP/API/credential/config failure;
  inspect `http_status`, `api_status`, and `blocker`.

The status JSON's `state` field is authoritative for what the monitor itself
concluded; the Windows task merely ran it and propagated its exit code.

## E. Troubleshooting

Use the table to decide *what* failed. Always start from the status JSON and
log entries, then map to the task `LastTaskResult` *and* its `LastRunTime`.

| Symptom (status JSON / log) | Category | Meaning / next step |
| --- | --- | --- |
| Task `State` not `Ready`/`Running`; `LastTaskResult` a Windows-side non-1/2 code; missing log line for the run window | 1. Windows scheduler failure | Task-level problem: disabled task, missed trigger, bad principal, execution limit. Check `Get-ScheduledTask`, `Get-ScheduledTaskInfo`, and Task Scheduler history. |
| Log line absent for a window / process crashed (no entry written) | 2. Monitor execution failure | The Python monitor errored before writing output (e.g., config, import, unexpected exception). Check stderr / run the script manually (§C.7). |
| `http_status` non-2xx or `api_status` an Upstox error; `state ERROR`; `LastTaskResult 2` | 3. Upstox HTTP/API failure | Upstox endpoint/rate-limit/credential issue. On 4xx/5xx, resolve the API error. Exited 2 intentionally. |
| `HTTP 200` + `api status success` + `candle_count = 0`; `state NOT_READY`; `LastTaskResult 1` | 4. Upstox returning zero candles | Upstox simply has not published today's 5m candles yet. **Expected** outside/early in the session or during vendor lag. Not an error. |
| `candle_count > 0` but `latest_candle` old; `watchdog_fresh: false`; `state NOT_READY` | 5. Stale candles | The latest candle falls outside the Watchdog freshness window (`max_bar_age`, default 5 minutes). Data exists but is too old to be considered live-ready. |

Do **not** bypass the Watchdog. Do **not** suggest changing the production
collector to fetch today's data — the collector's eligibility logic is by
design (`day < today`); today's data is the readiness monitor's job.

## F. Safety boundary

- `FNO_Today5mReadinessMonitor` is **read-only**.
- It must not import or invoke order execution.
- It must not place BUY/SELL orders.
- It must not automatically launch `run_live_execution_test.py`.
- `READY` is a **market-data readiness signal only**.
- Actual live trading requires a separate explicit operator-controlled step
  (the existing live-execution gate, consent file, and
  `FNO_LIVE_EXECUTION_TEST_ENABLED=1` environment must all be in place, and
  the operator must run the live test explicitly). Nothing in either scheduler
  task triggers it.
- Never print access tokens or credentials (both the collector and the monitor
  report only `PRESENT`/`ABSENT`; the token is resolved in memory at runtime).
- Do not weaken live gates or Watchdog freshness rules (the monitor reuses the
  existing production `Watchdog` with its default 5-minute `max_bar_age`).