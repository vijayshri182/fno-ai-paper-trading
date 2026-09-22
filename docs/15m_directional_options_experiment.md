# 15M_DIRECTIONAL_OPTIONS_EXPERIMENT (contract)

Status: **IMPLEMENTED and TESTED — isolated, paper-only, deterministic.**
`EXPERIMENT_ID = "15M_DIRECTIONAL_OPTIONS_EXPERIMENT"`.

This document is the contract for an isolated experiment that explores the
directional BULLISH/BEARISH/NEUTRAL leg of the frozen research signal expressed
as **label-only option position intents** on the NIFTY 50 index, under an
explicit **zero-premium simplification**. It is a self-contained paper
experiment: it cannot touch live order flow, the live scheduler, real market
data, real options data, or the frozen MA(5,21) paper-trading algorithm.

This is NOT an options expectancy study, does not fabricate option
premium/strike/expiry, and does not replace `docs/options_experiment_spec.md`
(that preregistration remains SPEC ONLY and unchanged).

---

## 1. Problem and scope

The frozen algorithm `MA(5,21)` is daily-restrictive and index-LONG-only. We
want to study whether the *directional* signal of the committed research
strategy `DonchianBreakout(entry_channel=20, exit_channel=10)` is fertile on a
15-minute cadence, without ever coupling to the frozen algorithm or to
real option chains.

Scope boundary (isolate-by-structure):

- The signal source (`strategies/research_candidates.py::DonchianBreakout`)
  is **read-only** in this experiment (imported, called, never edited).
- The MA(5,21) algorithm, its config, and its store account are **never
  launched and never modified** by any experiment code path.
- The only broker reachable is `PaperBroker` (`is_live=False`). The module
  contains no scheduler, no token parameter, and no network data path.

## 2. The 15-minute decision machine

Every trading day is processed at the standard 5-minute paper-track cadence
(`day_ticks(day)`: 09:14, 09:15, then 09:20..15:30, 15:36).

- Completed bars are validated by the unmodified `BarValidator` +
  `SessionPolicy` and pushed into a `DecisionWindowAggregator`.
- A **decision point** is `09:30 + 15*k` for `k = 0..23` (24 per day;
  first 09:30, last 15:15). A decision fires only when the window is
  `complete`: exactly the three bars `{m-15m, m-10m, m-5m}` have been
  delivered and no bar is missing/duplicated/out-of-order. The reference
  price is the close of the `m-5m` (last completed) bar.
- An incomplete/duplicate/out-of-order window yields **no decision** and is
  recorded in `data_quality`. No action can ever occur mid-window.
- Decisions, protective stops, leg switches, and the EOD flatten happen only
  at decision points (or at the fixed 15:20 flatten moment for a day).

Signal mapping (frozen): the last `SignalResult` of
`DonchianBreakout(20,10).analyze(full_history)` is mapped
BUY → BULLISH, SELL → BEARISH, HOLD → NEUTRAL.
`analyze` is a pure function of the delivered history, so every decision is
bit-reproducible from the same history. Empty history defensively maps to
NEUTRAL.

## 3. Leg state machine and execution (label-only)

States: `FLAT`, `CALL`, `PUT`. Transitions at a decision point only:

| state | BULLISH | BEARISH | NEUTRAL |
|---|---|---|---|
| FLAT  | ENTER_CALL | ENTER_PUT | stay FLAT |
| CALL  | stay CALL | SWITCH_CALL_TO_PUT | EXIT_CALL → FLAT |
| PUT   | SWITCH_PUT_TO_CALL | stay PUT | EXIT_PUT → FLAT |

Execution is **label-only on the index** (zero-premium simplification):

- `CALL` = LONG index → `BUY NIFTY 50` (a real index paper fill).
- `PUT` = SHORT index → `SELL NIFTY 50` (needs `Portfolio(long_only=False)`).
- Exits/flatten are market SELL/BUY at the decision reference close.
- No strike, no expiry, no option-type, no premium field is ever fabricated;
  fills carry the underlying index instrument only. Reported P&L is the index
  move because the option premium would be zero in this simplification
  (documented, never labeled "option P&L").

Fills go through the unmodified `PaperBroker` (slippage 0.1% of close,
commission 0.03% of slip-adjusted notional), then the unmodified
`RiskManager` gate + `RiskBasedPositionSizer` (1% risk slots, 2% stop) and the
experiment's own `DirectionalStopPolicy` (CALL long stop = close below
entry×(1−2%); PUT short stop = close above entry×(1+2%); checked only at
decision moments; the shared `StopLossPolicy` is LONG-ONLY and is never used).

## 4. Isolation guarantees (tests)

- Experiment store default `data/experiments/15m_directional_options`;
  the baseline account for `compare` lives in `<store>/_baseline`
  (`account = nifty_5m_daily`) and is produced by the **unmodified**
  `run_sessions`. A test proves the experiment leaves that store byte-identical.
- Once-per-day: a day that already produced a report raises
  `DayAlreadyReported`; sessions are never replayed or double-counted.
- `resume=True` restores the latest checkpoint and continues state; a
  checkpoint/restart run and an uninterrupted run produce the **same**
  stable fingerprint on identical bars.
- Fingerprint = SHA-256 of the full experiment state, including account
  `cash = initial_cash + realized_pnl − commissions` reconciliation invariant.
- No credential/secret/token-carrying key ever appears in report/checkpoint
  output (asserted by walk of every key/value).

## 5. Metrics recorded (report)

- accounting: net/gross/commission split + reconciliation.
- decisions: decision points, signal counts, entries/exits per leg,
  switches CALL↔PUT, stops fired, EOD flattens, `mid_window_actions = 0`.
- performance: round trips, win rate, gross average, max drawdown (at
  15-minute points), time-in-position, worst trade / worst day,
  consecutive losses.
- execution: broker class, `is_live=false`, `label_only=true`,
  `zero_premium=true`.
- data_quality: poisoned, anomalies, data_errors (incomplete windows).
- baseline_comparison: only truth/engineering columns, and
  `declared_winner = null` always — the experiment never declares a winner,
  and drawdown granularity differs baseline vs experiment (documented).

## 6. Usage

From the repo root (paper-only banner always printed):

```
.venv\Scripts\python.exe scripts\run_15m_directional_experiment.py --days 3 replay
.venv\Scripts\python.exe scripts\run_15m_directional_experiment.py status
.venv\Scripts\python.exe scripts\run_15m_directional_experiment.py report --day 2026-09-22
.venv\Scripts\python.exe scripts\run_15m_directional_experiment.py --days 3 compare
```

Tests: `pytest tests/test_15m_directional_options.py` (31 tests: window
completeness/dedupe/ordering, signal mapping + determinism, full state table,
directional stops, replay end-flat + reconciliation, deterministic fingerprint,
restart-resume identity, store isolation from baseline/day-refusal, credential
scan, no-winner comparison).

## 7. Honest limitations

- Ran so far only on synthetic sessions. A full-session live-data validation
  is currently impossible with the read-only data endpoint (0 current-session
  bars on 2026-09-22); the same fail-closed discipline applies.
- "Option" here is a label-only intent with zero premium: this measures the
  directional skill of the signal on the index, not option premium/time-value
  behavior. Nothing in this experiment is an options performance claim and it
  does not advance RQ1/RQ5 of the preregistration.
- The MA(5,21) frozen algorithm is untouched; compare output must never be
  read as a recommendation to change it.