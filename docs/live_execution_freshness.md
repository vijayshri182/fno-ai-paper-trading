# Live Execution Market-Data Freshness (5-minute boundary)

> Reference for the fixed 300-second Watchdog freshness window and why
> `TODAY_5M = READY` is a *moment-in-time* result. It is **not** a guarantee
> that the controlled live execution test can still start later.

## 1. TODAY_5M READY is time-sensitive

`scripts/check_today_upstox_5m.py` reads the latest published Upstox Intraday V3
candle for the current trading day and evaluates it with the production
`Watchdog` (`max_bar_age = 5 minutes`, `src/fno_ai_paper_trading/alerting/health.py`).
`TODAY_5M = READY` means the latest candle was **fresh at the instant it was
checked**. Because the feed publishes one candle every 5 minutes, that verdict
expires: the same candle ages past 300 seconds shortly after the check.

## 2. A READY result can become stale before the live execution preflight

Observed on 2026-09-28:

| Evaluation instant | Latest candle | Age | Watchdog verdict |
|---|---|---|---|
| 10:54:14 IST | 10:50 | 254 s | fresh — READY |
| 10:55:01 IST | 10:50 | 301 s | stale — STOP (market_data CRITICAL) |

Both evaluations used the *same* candle. The controlled live run
(`scripts/run_live_execution_test.py --live`) crossed the fixed 300-second
boundary between the readiness check and the preflight (`execution/manager.py`
→ `execution/risk.py`), so the Watchdog correctly rejected the run **before any
order**. No stale-feed, timezone, or truncation defect existed: `_current_day_signal_bars`
keeps the newest N bars via `merged[-limit:]` and the execution path uses the
same naive-IST candle stream as the readiness check.

## 3. Operator guidance

* Launch the controlled execution test **immediately** after `TODAY_5M = READY`;
  the margin below the 300 s ceiling is a few tens of seconds, not minutes.
* If the run is blocked by `watchdog STOP ... market_data`, re-confirm readiness
  and retry right away — the check is fail-as-designed, not an error.
* A fresh READY at check time does not authorize waiting: the preflight re-checks
  freshness at order time.

## 4. The threshold must NOT be changed or bypassed

The 300-second `max_bar_age` is a fixed safety control for the fail-sealed
preflight. It must **not** be increased, overridden, or bypassed — and stale
candles must never be used as a fallback. ALGO READY, the LIVE_EXECUTION_TEST
gate, RiskPreflight, and market-data health all remain mandatory and unchanged.

## 5. Regression coverage

* `tests/test_alerting.py` — `is_stale` boundary (exactly 300 s fresh, >300 s
  stale, future-dated stale) and the documented 10:54:14 vs 10:55:01 race at the
  Watchdog level.
* `tests/test_live_execution_test.py` — `_current_day_signal_bars` merges
  history + intraday chronologically, retains the newest N bars, and returns the
  newest available Intraday V3 candle as `bars[-1]`.