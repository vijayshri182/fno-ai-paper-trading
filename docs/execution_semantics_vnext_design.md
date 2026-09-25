# DESIGN + TEST SPECIFICATION — Execution-Semantics vNext

**Status:** PROPOSAL ONLY (read-only design candidate). Not approved, not wired, not implemented.
**Scope:** A correct F&O option expression of the existing LONG/SHORT (BUY/SELL/HOLD) signal, replacing the current CALL/PUT label semantics. Based on the completed CALL/PUT AND EOD BEHAVIOR AUDIT.
**Change scope now:** NONE. This document modifies no production code, no research result, no EOD behavior, no committed artifact. The isolated executable test spec lives at `tests/test_execution_semantics_vnext_spec.py`.
**Ownership:** "execution-semantics vNext" — a proposal to be separately reviewed before any implementation workstream is opened.

---

## A. Current implementation verified (from the CALL/PUT AND EOD BEHAVIOR AUDIT)

### A.1 The signal is BUY/SELL/HOLD, not LONG/SHORT/FLAT
`models/enums.py` defines `Signal` as `BUY / SELL / HOLD`. The research/paper lineage later expresses these as LONG(index)/SHORT(index)/FLAT. This spec uses LONG/SHORT/FLAT as the semantic signal vocabulary and treats `Signal.BUY == LONG`, `Signal.SELL == SHORT`, `Signal.HOLD == FLAT` for translation purposes.

### A.2 The mapping chain (verified links)
1. `execution/signal.py:37-43` — `signal_to_call_put`: `BUY -> CALL`, `SELL -> PUT`, `HOLD -> NONE`.
2. `execution/signal.py:30-34` — `CallPutSignal.order_side`: `CALL -> BUY`, `PUT -> SELL` (reference: `tests/test_live_execution_test.py:390-392`).
3. `scripts/run_live_execution_test.py:279` — `option_type_pref = "PE" if args.side == "PUT" else "CE"` (`--side auto` and `--side CALL` are always `CE`; only pinned `--side PUT` selects `PE`).
4. `execution/manager.py:332-338` — `side = leg.order_side` (the leg's order_side drives the transaction).
5. `execution/upstox.py:383` — `transaction_type = order.side.value` (the upstream order payload).
6. `execution/instrument.py:64-65` — `option_type` validated to `CE|PE`; a PE instrument CAN be resolved, but no code path ever BUYs it.

### A.3 Consequence matrix (current behavior, confirmed by tests)
| Signal input | leg label | contract selected | transaction | Gross economic expression | Delta sign |
|---|---|---|---|---|---|
| BUY (`LONG`) | CALL | CE (`auto`/`CALL`) | BUY | BUY CE (long call) | bull |
| SELL (`SHORT`) | **PUT** | **CE (auto)** | **SELL** | **SELL CE (naked short call)** | **bear via short call — NOT a bought put** |
| SELL (`SHORT`) pinned PUT | PUT | PE (pinned: `script:279`) | SELL | SELL PE (write a put) | **bull (contradicts the bearish label)** |
| HOLD (`FLAT`) | NONE | — | (refused, no order) | no trade | — |

Crucially: in the forced-roundtrip tests the "PUT" round trip executes **SELL -> BUY on a CE instrument** (`tests/test_forced_live_round_trip.py:82-87` `_underlying()` is `option_type="CE"`; entry `OrderSide.SELL` at lines 257-271 / 321-334). **The current PUT label is NEVER equivalent to BUY PE in any executed path.** This is the audit's BUG-1/BUG-2 and the root cause this vNext design fixes.

### A.4 EOD behavior audit facts
- The live harness forbids overnight positions by design: `execution/risk.py` OvernightGuard (lines 8, 72, 147-156) rejects entries that cannot finish hold + finalization before the 15:30 close.
- No flatten-at-close hook exists anywhere except the unused `strategies/end_of_day.py` `model_3`.
- Research holds multi-day intentionally (`max_hold_days=25`, first-of-day entries/exits, >=1-session flat gap on reversal; `research/iteration008` state machine, `research/iteration006` fingerprint).
- Engine + stop loss are LONG-only: `risk/stop_loss.py:98`, `backtest/engine.py:253` (short legs unprotected — audit BUG-3).
- Single-slot exposure is enforced only per-run (one entry + one exit), never across legs or across runs (audit finding).

---

## B. Proposed semantic model (vNext)

**Governing rule: "CALL/PUT identifies the option contract; BUY/SELL identifies the broker transaction."** They are two independent axes. `PUT` is a contract name, never a synonym for SELL.

### B.1 Signal vocabulary (input axis)
- `LONG` (== current `Signal.BUY`)
- `SHORT` (== current `Signal.SELL`)
- `FLAT` (== current `Signal.HOLD`)

### B.2 Contract axis (vNext)
- `LONG`  -> **CALL** contract (CE)
- `SHORT` -> **PUT** contract (PE)
- `FLAT`  -> no contract

### B.3 Transaction axis (vNext)
- Entry: `LONG` -> BUY CALL (BUY CE); `SHORT` -> **BUY PUT (BUY PE)**; `FLAT` -> no entry.
- Exit/close: SELL the held contract back (SELL CE when LONG_CALL held; SELL PE when LONG_PUT held).
- SELL is only ever a CLOSING transaction of an already-held contract. **A SELL alone must never open a position, and `PUT` alone must never imply SELL.**

### B.4 Position states
- `FLAT` — no position.
- `LONG_CALL` — holding a bought call (CE).
- `LONG_PUT` — holding a bought put (PE).

The system expresses research direction via **bought directional options only**. Written (short) options are out of scope for vNext and are research questions, not execution defaults.

---

## C. Reversal state-transition table (vNext)

Inputs: current position state; new signal mandate (LONG / SHORT / FLAT).

| # | Current state | Signal | Proposed action (ordered) | Resulting state |
|---|---|---|---|---|
| 1 | FLAT | LONG | resolve CE; BUY CE; confirm fill | LONG_CALL |
| 2 | FLAT | SHORT | resolve PE; BUY PE; confirm fill | LONG_PUT |
| 3 | FLAT | FLAT | hold; no order | FLAT |
| 4 | LONG_CALL | LONG | hold position | LONG_CALL |
| 5 | LONG_CALL | FLAT | SELL CE (close); confirm flat | FLAT |
| 6 | LONG_CALL | SHORT | **SELL CE (close) + confirm exit BEFORE any opposite leg**; then resolve PE; BUY PE; confirm fill | LONG_PUT |
| 7 | LONG_PUT | SHORT | hold position | LONG_PUT |
| 8 | LONG_PUT | FLAT | SELL PE (close); confirm flat | FLAT |
| 9 | LONG_PUT | LONG | **SELL PE (close) + confirm exit BEFORE any opposite leg**; then resolve CE; BUY CE; confirm fill | LONG_CALL |

### C.1 Hard invariants (must be enforced by any future implementation)
1. **Never both CALL and PUT open simultaneously.** At most one directional option position exists system-wide (`FLAT / LONG_CALL / LONG_PUT` are mutually exclusive). Any implementation that would place a second option order while a position is open is rejected pre-trade.
2. **Exit must be confirmed before any opposite leg is placed** (reversal, rows 6 and 9). No blind fill assumptions; no simultaneous close/open.
3. **Entry confirmation failure -> remain at the prior state (or FLAT), fully reconciled.** Never assume a fill; never invent a position.
4. **A SELL is only a closing transaction** for an already-confirmed open position. A SELL that has no position behind it is an error, not a new trade.
5. **PUT semantics are fixed to the PE contract.** `PUT` never maps to SELL; `LONG_PUT` always means "hold a bought PE".

---

## D. Single-slot / exposure-control architecture recommendation

Goal: exactly one directional option position system-wide, never both legs, every run and across runs.

Recommendation (for the FUTURE implementation, not built now):
- A dedicated option-position controller owns `FLAT / LONG_CALL / LONG_PUT` (single source of truth for the slot), distinct from the per-run orchestration. It exposes only legal transitions from section C.
- `RiskManager` pre-trade veto is the enforcement chokepoint: every option order must pass a check that the slot is free (position controller + pre-trade risk approval). Per-symbol caps already exist (`risk/manager.py`); the NEW invariant is a cross-leg/cross-run single-slot guarantee.
- The live lab (WS 7.9) remains per-run single entry + single exit; vNext adds the *slot-level* invariant on top, so a reversal (rows 6/9) is internally two sequenced orders under one holding-slot, never two open positions.
- On crash/restart, the controller must reconcile from broker positions before emitting any next order (never assume a lost fill).

---

## E. EOD policy separation (explicitly separated concerns)

| Concern | Policy A (intraday flatten) | Policy B (multi-day) |
|---|---|---|
| Also called | today / intraday lab policy | tomorrow / swing policy |
| Holds | intraday only; flat before close | multi-day with expiry/strike/roll rules |
| Drivers | OvernightGuard (`execution/risk.py`), 5-min hold, per-run round trip | expiry/strike selection, roll, theta, gap, overnight, liquidity, continuity policies |
| Risk surface | no overnight gap, no expiry day | gaps, expiry-day, assignment/exercise, theta decay |

**OUR-ALGO-004 belongs to NEITHER validated options policy.** It is an index-research candidate (signal efficacy measurement: holds up to 25 days, first-of-day entries/exits, gap-normalized). It must never be assumed to be a validated options expression or EOD policy. Its execution path today is: live harness = Policy A (forced/short intraday round trip, default-off), research = multi-day *notional* holds. Neither is a validated options policy; both are context for future work.

---

## F. Unresolved options research questions

**Do NOT assume the mapping is validated.** Specifically:

- RQ1 — "LONG index => BUY CALL" is **proposed**, not proven. No research currently maps index signal efficacy to option-premium outcomes.
- RQ2 — "SHORT index => BUY PUT" is **proposed**. The option literature/practice requires comparing BUY PUT vs SHORT CALL vs SHORT FUTURE expresses of the same signal, including premium cost, expiry strike, moneyness, IV/theta, spreads, slippage, financing, and the assignment/exercise path.
- RQ3 — Continuity: consecutive same-direction mandates on different expiry cycles (roll) is an open policy question (Policy B), not solved here.
- RQ4 — Valuation/exit: how an option position realizes (intrinsic vs time decay) at signal FLAT vs at expiry vs at theta threshold is unresolved research.
- RQ5 — Empirical validation plan: no OOS study may reuse the protected window (`2025-10-06..2026-09-11`) or the untouched fresh pool; a new "options expression" study requires a fresh, uncontaminated design.

**Separation of tracks:** RESEARCH (index signal efficacy — frozen, per ledger + hashes) | EXECUTION (vNext semantics — this proposal) | OPTIONS RESEARCH (RQ1-RQ5 above — new, non-existent, must be opened separately and never bump RESEARCH artifacts).

---

## G. Test matrix (the agreed spec, implemented as an isolated executable spec)

The spec tests `tests/test_execution_semantics_vnext_spec.py` are **self-contained** — local pure helpers encode the vNext model; **no production module is modified or imported for behavior**, and no current test is touched. Cases:

| Case | Name | What it pins |
|---|---|---|
| A | Signal mapping | LONG->CALL, SHORT->PUT(PE), FLAT->NONE; entry tx BUY for both CALL and PUT (never PUT=SELL) |
| B | Closing | LONG_CALL+FLAT -> SELL CE -> FLAT; LONG_PUT+FLAT -> SELL PE -> FLAT |
| C | Reversal ordering | LONG_CALL+SHORT: SELL CE confirmed then BUY PE; LONG_PUT+LONG: SELL PE confirmed then BUY CE; never open opposite before close confirmed |
| D | Failure safety | Exit-confirmation failure -> no opposite leg; entry-confirmation failure -> revert to prior/FLAT; no blind fill |
| E | Single-slot | never CALL+PUT concurrently; at most one open position; each open only from FLAT |
| F | Instrument consistency | reject: PUT+CE pairing, BUY PUT on CE, LONG_PUT on CE, CALL+PE; LONG_CALL requires CE, LONG_PUT requires PE |
| G | Compatibility pin | the vNext model is documented to DIFFER from the current pinned mapping (CALL->BUY, PUT->SELL) — guarding that the WS 7.9 harness must NOT silently change |

Current-suite evidence (unchanged, future work may extend): `tests/test_live_execution_test.py` (`TestSignal`, `test_order_side`, instrument tests) and `tests/test_forced_live_round_trip.py` (forced round trips 1-12 + STEP 3-A/B) already pin the CURRENT semantics; vNext adds only to the isolated file above.

---

## H. Current WS 7.9 harness compatibility impact

- **No behavior change now.** The WS 7.9 harness, forced one-lot seam (default-OFF, `scripts/run_live_execution_test.py`), gate, OvernightGuard, and all current tests remain byte-for-byte the same. No production `src/`, `scripts/`, or existing test is edited by this proposal.
- **WS 7.9 pinned-PUT note for the FUTURE workstream (do not silently change):** today, "pinned PUT" executes SELL(PE) -> BUY(PE) at the script level, and SELL(CE)->BUY(CE) inside the forced round-trip tests (where the fixture contract is CE). Under vNext semantics, a "forced PUT" round trip would become BUY(PE) -> SELL(PE). That is a **behavior change vs the current forced test** and must live behind a distinctly-named vNext switch/fixture, reviewed separately, and must NEVER flip the current `--force-one-lot-round-trip` seam silently.
- **Target posture:** default-OFF isolation identical to how the force seam is isolated today (`tests/test_forced_live_round_trip.py` tests 11-12 prove the autonomous runner cannot invoke the seam).

---

## I. Files that WOULD need modification in a FUTURE implementation (none now)

Illustrative target surface for a separate, gated workstream — NOT modified here:
- `execution/signal.py` (or a new `execution/vnext_signal.py`) — new LONG/SHORT/FLAT -> CE/PE + BUY mapping / decision object.
- `execution/manager.py` + `execution/state.py` — reversal orchestration, sequenced close-then-open, confirmation-gated transitions.
- `execution/instrument.py` — PE resolution for SHORT entries (already structurally supported; buy-side only in vNext).
- `execution/upstox.py` — unchanged transaction mapping applies; BUY PE becomes reachable via the new signal layer only.
- `execution/risk.py` (OvernightGuard interplay), `risk/manager.py` (single-slot pre-trade veto).
- `strategies/end_of_day.py` — Policy A/B decisions (future, separate).
- `scripts/run_live_execution_test.py`, the forced-seam tests, and new vNext tests.

## J. Files explicitly protected from modification (must stay untouched)

- Research + evaluation: `research/*` (our_algo_001-004, iteration006-012 engines), `tests/test_our_algo_*.py`, `tests/test_iter*.py`, `tests/test_candidates_eval.py`, `tests/test_five_year.py`.
- OUR-ALGO-004 artifact/hash pins (sha `BD523B...E5AD7`), frozen params (fast 9 / slow 26 / slope_window 5 / lookback 20 / stop_atr_mult 4.0 / max_hold_days 25 / warmup 54), `coherent` config, and any `enable_stop_loss` line — do NOT change values or semantics.
- Reports + ledgers (`docs/RESEARCH_DECISION_LEDGER.md`, `docs/WS_7.28_HANDOFF.md`, `docs/PROJECT_PLAN.md`, `docs/PROGRESS.md`, `reports/**`), datasets (`datasets/**`, including the protected master and untouched fresh files), `experience_store/**`, `runs/**`.
- OOS boundaries: protected OOS `2025-10-06..2026-09-11` and fresh-after `2026-09-11` — no re-run, no relabel, no consumption.
- Credentials: `.env`, `.env.txt`, `.env.example` — never read or modified.
- Current execution harness + its full test suite (unchanged by design).

## K. Safety confirmation

- Files created (isolated spec artifacts only): `docs/execution_semantics_vnext_design.md`, `tests/test_execution_semantics_vnext_spec.py`.
- Files modified: **0** production/research/report/config/dataset file.
- Network: **0 data exchange**; **0 Upstox API**, **0 paper orders**, **0 live orders**.
- OOS: **0** (neither protected nor fresh consumed).
- OUR-ALGO-004 / research artifacts / hashes / EOD behavior: **unchanged**.
- Commits / pushes: **0**.
- After this report the work session stops: no implementation, no live execution, no OOS study, no commit/push.