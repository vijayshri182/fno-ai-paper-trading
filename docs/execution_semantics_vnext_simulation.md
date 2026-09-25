# vNext Execution Semantics — Deterministic End-to-End Simulation

Status: isolated simulation harness, deterministic-test only (no network, no
Upstox, no paper/live orders, no backtest, no credentials touched). Companion
to `execution_semantics_vnext_design.md`,
`execution_semantics_vnext_implementation.md` and
`tests/test_execution_semantics_vnext_simulation.py`.

## Purpose

The isolated vNext state machine is driven through a scripted, in-memory fake
broker and fake resolver so its *whole execution path* — signal planning, order
submission, fills, rejections, reconciliation, restarts, slot races — can be
asserted deterministically. Two properties matter most:

1. **No creative freedom in the simulation.** It can only fail a run, never
   invent an order, a position or a fill.
2. **Byte-for-byte reproducibility.** Same event script -> identical canonical
   ledger, so a "double-run" is a real determinism demonstration, not a timing
   or randomness artifact.

## Architecture

`tests/vnext_simulation.py` provides three deterministic fakes plus a harness:

- `SimulationBroker` — implements the vNext broker protocol. Position quantity
  per leg (CE/PE) changes *only* through audited `AppliedMutation`s from filled
  orders, driver `external_flip`s, and due delayed mutations applied inside
  `reconcile()`. Submission behaviors are queue-ordered `BrokerAction`s with
  per-order status (`FILLED`/`PENDING`/`REJECTED`/`UNFILLED`/`CANCELLED`),
  mutation semantics (`apply` / `none` / `wrong_leg` / `both`), an apply delay,
  a raise-on-submit hook, and a `flatten_on_reject` venue-liquidation mode.
- `ScriptedResolver` — wraps the table resolver; consumes one-shot faults
  (`missing`, `wrong_pair`, `expired`) so resolution failures are scriptable.
- `SimulationHarness` — owns one broker/resolver/registry/machine triple, appends
  one deterministic line per driver action to `events`, and exposes
  `drive()`, `poll()`, `advance()`, `mark_filled/mark_rejected/mark_cancelled`,
  `external_flip()`, `queue_submit_raise()`, `queue_reconcile_raise()`,
  `restart()` and `canonical()` (the full-run fingerprint). The harness also
  records order-submission windows and completion moments so the invariants can
  be checked *at the exact tick* the event happened.
- `InvariantChecker` — the 18 invariants (section below). Each check is
  precondition-scoped: it reports a violation only where the invariant is
  well-defined (a disagreement safety stop legitimately keeps a reservation
  while the broker is flat; in-flight polls may legitimately lag).

Two harnesses share one `SlotRegistry` for the slot-race scenarios: the
contested run is refused (0 orders, in-flight bookkeeping discarded) and
proceeds cleanly only after the winner releases the slot.

## Drivers and the event model

Driver operations are pure function calls (no wall-clock, no UUID, no unseeded
randomness). Event ops used by the stress generator:

```
SIGNAL:LONG|SHORT|FLAT   drive a signal
POLL                     re-poll the in-flight transition
ADVANCE                  elapse one broker step (delayed mutations)
MARK_FILLED              fill the current open/close order
MARK_FILLED_DELAYED      fill with the mutation landing next reconcile
MARK_REJECTED/CANCELLED  reject/cancel the current order
FLIP_CE|PE_0|75          driver-set broker quantity (third-party drift)
FAULT_MISSING            one-shot resolver "no contract" fault
FAULT_WRONG_PAIR         one-shot resolver opposite-leg fault
RAISE_SUBMIT_ONCE        one-shot submit-time exception
RAISE_RECONCILE_ONCE     one-shot reconcile-time exception
RESTART                  rebuild the machine from the broker snapshot
RACE_LONG|SHORT|FLAT     contested harness drives the same registry
```

`MARK_*` target `transition.open_order_id or transition.close_order_id` (noop
when nothing is in flight). External flips are *audited* into the ledger as
`ExternalFlip`s, so cross-run agreement is always verifiable.

## Scenarios (26)

| # | Scenario | Pinned outcome |
|---|----------|----------------|
| 1 | clean LONG entry | COMPLETED LONG_CALL, CE +75, registry CE 75 |
| 2 | clean SHORT entry | COMPLETED LONG_PUT, PE +75, registry PE 75 |
| 3 | duplicate signal while entry pending | STOPPED (idempotent poll), exactly 1 order |
| 4 | pending entry fills | COMPLETED only after the explicit fill |
| 5 | clean exit | COMPLETED FLAT, broker flat, slot released |
| 6 | pending exit | STOPPED "opposite leg NOT opened"; CLOSE only |
| 7 | rejected exit | SAFETY_STOP CLOSE_REJECTED, reservation kept |
| 8 | unfilled exit | SAFETY_STOP CLOSE_UNFILLED, no phantom flat |
| 9 | partial exit gating | COMPLETED only after fill confirmation |
| 10 | close filled but not flat | SAFETY_STOP RECONCILIATION_NOT_FLAT |
| 11 | close rejected + venue liquidation | audited external flip; CLOSE_REJECTED |
| 12 | FLAT on a flat broker | HELD, zero orders |
| 13 | entry rejected after close | ENTRY_FAILED_FLAT, slot released |
| 14 | entry rejected while broker holds | SAFETY_STOP, conservative reservation |
| 15 | entry broker unreachable | VNextError -> SAFETY_STOP; raw exception surfaced, 0 orders |
| 16 | missing contract | SAFETY_STOP RESOLUTION_FAILED, 0 orders |
| 17 | wrong contract type | SAFETY_STOP RESOLUTION_FAILED |
| 18 | wrong CE/PE pairing | SAFETY_STOP RESOLUTION_FAILED |
| 19 | expired contract | SAFETY_STOP RESOLUTION_FAILED |
| 20 | resolution failure then retry | clean retry COMPLETED |
| 21 | clean reversal | COMPLETED LONG_PUT via CLOSE+OPEN (3 orders) |
| 22 | reversal close rejected | SAFETY_STOP CLOSE_REJECTED, opposite never opened |
| 23 | reversal close filled-not-flat | SAFETY_STOP RECONCILIATION_NOT_FLAT |
| 24 | failed reversal | no phantom, no auto-retry |
| 25 | restart adoption | adopts exactly the broker snapshot; refuses unsafe |
| 26 | slot race + determinism | contested refusal; byte-identical double run |

## The 18 invariants

Scoped to "wherever well-defined". A check that returns nothing means the
invariant held on that event.

- **I01** single-slot projection: a BUY never opens a leg that is already open.
- **I02** projectable non-negative CE quantity.
- **I03** projectable non-negative PE quantity.
- **I04** a COMPLETED held state rests on a clean broker reconcile (checked at
  the exact completion moment).
- **I05** no short quantity at the broker.
- **I06** every OPEN is a BUY.
- **I07** every CLOSE is a SELL.
- **I08** no sealed (BUY-CLOSE / SELL-OPEN) order ever appears.
- **I09** opens never precede the confirming close of a held leg.
- **I10** every order's CALL/CE and PUT/PE pairing is legal.
- **I11** no expired contract is ever submitted.
- **I12** the broker position always equals the audited mutation replay (no
  phantom fill from an exception), outside pending delayed mutations.
- **I13** a poll-in-flight never emits orders; a transition emits at most 2.
- **I14** the slot registry matches the machine's order-backed open (type and
  quantity) once the machine settles; mirror/conservative reservations are not
  double-counted.
- **I15** a safety stop never auto-submits an order.
- **I16** a restart never invents a position and refuses unsafe snapshots.
- **I17** every audited mutation traces to a submitted order (no auto-repair).
- **I18** no phantom opposite leg appears after a failed reversal.

I01/I09/I18 are audited by replaying applied+external book events in broker-tick
order, whitelisting only the explicitly injected `mutate="both"` ambiguity.

## Determinism evidence

`test_s26_determinism_double_run_is_byte_identical` drives 200 events from a
fixed seed through two separate harness pairs and compares the full canonical
ledger (events + orders + applied mutations + external flips + positions +
machine + registry). `test_s26_different_seed_differs` confirms the comparison
is meaningful. In development the same sweep ran 300 seeds x 150 events with
all invariants asserted after **every** event and double runs byte-identical.
The `MARK_CANCELLED` driver is part of the pool and invariant-clean.

## Run

```
.venv\Scripts\python.exe -m pytest tests\test_execution_semantics_vnext.py tests\test_execution_semantics_vnext_spec.py tests\test_execution_semantics_vnext_adversarial.py tests\test_execution_semantics_vnext_isolation.py tests\test_execution_semantics_vnext_simulation.py -q
```

## Files changed in this phase

- `tests/vnext_simulation.py` — new: deterministic broker/resolver/harness/checker.
- `tests/test_execution_semantics_vnext_simulation.py` — new: 26 scenarios +
  invariants + determinism tests (32 tests).
- `docs/execution_semantics_vnext_simulation.md` — this document.
- `src/fno_ai_paper_trading/execution/vnext/machine.py` — hardening: an OPEN
  step now pre-reconciles before submitting (never opens into a broker that
  already holds a position or snaps an unsafe snapshot), resolving a scenario
  found by the stress sweep where injected drift let an OPEN stack quantity on
  top of an ambient position. Placement after `guard.record_open` preserves the
  conservative reservation semantics pinned by the adversarial suite.

## Safety confirmations

- No network, no Upstox, no paper/live orders, no credentials, no `.env`.
- No changes to WS 7.9 live execution code, strategies, research, or backtest.
- No EOD/overnight/25-day-hold behavior changed.
- No commits or pushes were made.