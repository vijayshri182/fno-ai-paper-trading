# vNext Option Execution State Machine — Implementation Notes

Status: isolated implementation, deterministic-test only (no network, no Upstox,
no paper/live orders, no backtest). This document is a companion to
`execution_semantics_vnext_design.md` and `tests/test_execution_semantics_vnext_spec.py`.

## Scope and non-goals

This phase implements ONLY the isolated vNext option-execution state machine.
It is NOT wired into the WS 7.9 live/paper harness and cannot be accidentally
activated by it:

- No module under `src/fno_ai_paper_trading/execution/` other than `vnext/`
  imports anything from `execution/vnext/`. Likewise, `execution/vnext/` imports
  exclusively its own modules.
- No existing WS 7.9 file (`execution/signal.py`, `execution/manager.py`,
  `execution/state.py`, `execution/upstox.py`, `scripts/run_live_execution_test.py`)
  was modified.
- EOD behavior is out of scope: no EOD flattening, no change to the overnight
  guard, no change to the 25-day hold.
- Validation is execution-semantics only. There is no options performance claim
  and no backtest.

## Architecture

```
execution/vnext/
    enums.py            vocabulary (SignalDirection, OptionLeg, ContractType,
                        TxSide, OrderAction, PositionState, OrderStatus,
                        SafetyStopReason, TransitionStatus)
    errors.py           exception types (VNextError hierarchy)
    contract.py         OptionContract + MarketContext + ContractResolver
                        protocol + table-based resolver (deterministic fake)
    broker.py           Broker protocol + OrderRequest/Ticket/Status/Position
                        snapshots. order_request validation: OPEN must be BUY.
    order_semantics.py  leg<->contract pairing (CALL->CE, PUT->PE), entry/close
                        sides, order-semantics validator
    mapping.py          direction_to_leg + plan_orders (pinned transition table)
    state.py            reconcile_to_state (broker snapshot -> position state),
                        position_to_contract_type
    guards.py           SlotRegistry + SingleSlotGuard (single-slot invariant)
    order_request.py    build_order_request (step + resolved contract -> order)
    machine.py          VNextOptionExecutionMachine (confirmation-gated)
```

The machine depends only on the `Broker` and `ContractResolver` protocols.
Tests implement both deterministically (`tests/vnext_helpers.py`); a future real
adapter (e.g. Upstox) would implement those protocols without the machine changing.

## State machine

States: `FLAT`, `LONG_CALL`, `LONG_PUT`. Transitions (mirror of design section C):

| Position  | Signal | Plan                          | Result      |
|-----------|--------|-------------------------------|-------------|
| FLAT      | FLAT   | (none)                        | HELD        |
| FLAT      | LONG   | OPEN BUY CE                   | LONG_CALL   |
| FLAT      | SHORT  | OPEN BUY PE                   | LONG_PUT    |
| LONG_CALL | LONG   | (none)                        | HELD        |
| LONG_PUT  | SHORT  | (none)                        | HELD        |
| LONG_CALL | FLAT   | CLOSE SELL CE                 | FLAT        |
| LONG_PUT  | FLAT   | CLOSE SELL PE                 | FLAT        |
| LONG_CALL | SHORT  | CLOSE SELL CE → OPEN BUY PE   | LONG_PUT    |
| LONG_PUT  | LONG   | CLOSE SELL PE → OPEN BUY CE   | LONG_CALL   |

Hold signals never place an order. Long-only positions (`LONG_SHORT` in some
vocabularies) are NOT part of vNext: `LONG_CALL`/`LONG_PUT` are the only held
states. Every transition is synchronous and confirmation-gated: the machine
submits an order, then reconciles the broker position before accepting it.

## Order semantics

- Entry into any position is BUY (CE or PE). `OPEN` with `SELL` is rejected.
- Closing a held contract is SELL (CE or PE). `CLOSE` with `BUY` is rejected.
- PUT never means SELL: SHORT resolves to BUY PE (a long put), never a written
  option.
- Instrument pairing enforced: CALL↔CE, PUT↔PE. CALL+PE, PUT+CE, LONG_CALL+PE,
  LONG_PUT+CE are rejected.
- The broker never infers the contract from the transaction side; the machine
  sends `order_action + resolved contract + tx_side + quantity` together.

## Reversal atomicity

A reversal (LONG_PUT → LONG, LONG_CALL → SHORT) is TWO separately confirmed
transactions, sequenced by the machine:

1. Submit SELL of the held leg.
2. Poll until the SELL is FILLED, then reconcile that the position is FLAT.
3. Only then submit BUY of the opposite leg.
4. Poll until the BUY is FILLED, then reconcile that the intended position
   (LONG_CALL / LONG_PUT) materialised.

The opposite leg is never submitted before the close is flat-confirmed. If the
second (entry) transaction fails after a confirmed close, the final state is
FLAT and the failure is recorded.

## Failure safety

- Exit (close) rejected, cancelled, or unfilled → `SAFETY_STOP`, position left
  as-is, opposite leg NOT opened, reason recorded (`CLOSE_REJECTED`,
  `CLOSE_UNFILLED`).
- Reconciliation after a supposedly-filled close shows a non-flat position →
  `SAFETY_STOP` with `RECONCILIATION_NOT_FLAT`; opposite leg not opened.
- Broker snapshot shows CE and PE simultaneously → `AMBIGUOUS_POSITIONS`
  safety stop. Unexpected short quantity → `UNEXPECTED_SHORT` safety stop.
- Entry rejected/cancelled/unfilled → `ENTRY_FAILED_FLAT`: final state FLAT
  (with `ENTRY_REJECTED`/`ENTRY_UNFILLED` reason).
- The machine never assumes a fill: all fills are verified against the broker's
  reported position before the state changes.

## Single-slot invariant

`SlotRegistry` is process-wide (shared across runs) and symbol-independent.
While any run holds a slot, a DIFFERENT holder asking to open any leg (CE or PE
alike) is rejected with `SingleSlotViolationError` before any order is
submitted. A CE and a PE are different instruments but the same directional
slot. The same holder may re-acquire its own slot during a reversal without
re-triggering the check; the slot is released when the holder's position closes.

## Restart / reconciliation

At construction the machine reconciles against the broker:

- CE quantity > 0 → `LONG_CALL` (slot re-recorded)
- PE quantity > 0 → `LONG_PUT`
- both zero → `FLAT`
- CE > 0 AND PE > 0 → `AMBIGUOUS_POSITIONS` safety stop (refuses to construct)
- unexpected short → `UNEXPECTED_SHORT` safety stop (refuses to construct)

The machine never auto-trades to repair an unexpected state.

## Idempotency

`on_signal` refuses a NEW signal while a transition is in flight. A repeated
identical signal while in flight re-polls the existing gates and submits no
duplicate order. Each transition has a deterministic key (`<state>:<signal>`).
Enabled/disabled flags are unaffected — this phase declares no hooks into the
WS 7.9 harness.

## Verification

Only isolated vNext tests run in this phase:

- `tests/test_execution_semantics_vnext_spec.py` — pinned acceptance spec (29 tests)
- `tests/test_execution_semantics_vnext.py` — implementation suite (39 tests)
- `tests/test_execution_semantics_vnext_adversarial.py` — hardening suite (47 tests)
- `tests/test_execution_semantics_vnext_isolation.py` — import-isolation audit (37 tests)
- helpers: `tests/vnext_helpers.py`, `tests/conftest.py` (tests dir on sys.path)

Command: `.venv\Scripts\python.exe -m pytest tests/test_execution_semantics_vnext.py tests/test_execution_semantics_vnext_spec.py tests/test_execution_semantics_vnext_adversarial.py tests/test_execution_semantics_vnext_isolation.py -q`

---

# vNext-2 adversarial hardening record

This phase stress-tested the vNext machine against duplicate orders, CE+PE
exposure, phantom fills, restart state, opposite-leg-before-exit, stale broker
state, ambiguous positions, contract/leg mismatches, race-like signals, slot
release and accidental short exposure. Defects found during an independent code
review were fixed inside the isolated `execution/vnext/` package only.

## Defects found and fixed

| # | Defect | Fix |
|---|--------|-----|
| D1 | `_verify_open`'s filled-but-unexpected branch collapsed "unsafe snapshot" AND "broker flat" AND "wrong leg" into a single `ENTRY_FAILED_FLAT` that released the slot and claimed FLAT even when the broker held a conflicting position (phantom flat). | Filled-by-order but broker-disagreeing is now a hard `SAFETY_STOP` with `RECONCILIATION_DISAGREEMENT`; the slot reservation is KEPT until an explicit decision. |
| D2 | `_fail_entry` released the slot and reported FLAT without reconciling — a rejected entry while the broker actually holds a position produced a phantom flat claim. | `_fail_entry` now reconciles: only a broker-confirmed FLAT yields `ENTRY_FAILED_FLAT` (slot released); a held position or unsafe snapshot yields `SAFETY_STOP` and the reservation is kept. |
| D3 | `_fail_exit` kept the pre-transition state without reconciling — a rejected close while the broker is already flat left the machine claiming a position it no longer held. | `_fail_exit` reconciles: broker flat → slot released, state FLAT; broker holding a position → state mirrors the broker, reservation kept. |
| D4 | Resolver / contract/leg mismatches and stale contracts could be submitted to the broker (the machine never validated the resolved instrument against the step). | `_submit_step` enforces resolved `option_leg`/`contract_type` equality with the step and rejects a contract whose `expiry < context.as_of` before anything reaches the broker. |
| D5 | A `SingleSlotViolationError` (lost slot race) left the loser's in-flight transition set, wedging it forever. | The transition is discarded on slot-acquisition failure so the loser can retry later with zero orders ever submitted. |
| D6 | Broker/resolver exceptions mid-transition propagated with stale in-flight state. | `VNextError` → `SAFETY_STOP` (`RESOLUTION_FAILED` / `RECONCILIATION_DISAGREEMENT` / `BROKER_UNAVAILABLE`) with best-effort cleanup; unknown exceptions are re-raised after clearing the transition. |
| D7 | A slot reservation could be leaked if `submit_order` raised after `record_open`. | Cleanup reconciles the broker: reservation is released only when the broker is flat; otherwise it is kept. |
| D8 | `reconcile_to_state` summed non-integer / boolean quantities as positions. | Non-integer (non-bool) quantities → `INVALID_QUANTITY` safety stop; malformed details raise `ReconciliationError`. |
| D9 | `OrderRequest` accepted float/boolean quantities. | Quantity must be a non-bool `int` and positive. |
| D10 | `_adopt_broker_position` surfaced raw `AttributeError`/`RuntimeError` for malformed or unavailable initial snapshots. | All initial-reconcile failures are wrapped as `ReconciliationError`; the machine refuses to construct. |
| D11 | Close orders were sized to the configured/lot quantity even when the broker held fewer — a restart with a partial position over-sold into a short and tripped `UNEXPECTED_SHORT`. | Close steps are sized to the broker-confirmed held quantity of the leg (never a hardcoded assumption); a close planned against a leg the broker reports empty is refused before submission. |
| D12 | During a reversal the slot reservation was released on flat-confirm then re-acquired on the opposite-leg open, leaving a window where another run could steal the slot mid-reversal. | The reservation is carried continuously across a reversal (leg metadata refreshed on re-acquire) and released only at the final close or a flat-confirmed failure. |

## Invariants verified

- `OPEN` is always `BUY`; `SELL` is only ever a closing transaction.
- `SELL` with no held position is refused — at the semantics layer and at the
  machine layer (a close is never submitted when the broker reports no held
  quantity for the leg).
- Pairings: `PUT+PE+BUY` valid OPEN / `PUT+PE+SELL` valid CLOSE only;
  `CALL+CE+BUY` valid OPEN / `CALL+CE+SELL` valid CLOSE only. Mis-pairings
  (PUT+CE, CALL+PE) are rejected.
- The plan orders at most one OPEN and never opens both legs; a reversal plan
  orders CLOSE before OPEN and the machine never submits the OPEN until the
  CLOSE is flat-confirmed by reconcile.
- Event/state consistency: `LONG_CALL ⇔ CE>0 ∧ PE=0`, `LONG_PUT ⇔ PE>0 ∧ CE=0`,
  `FLAT ⇔ CE=0 ∧ PE=0` (all non-safety terminals mirror the broker).
- `SAFETY_STOP` and `ENTRY_FAILED_FLAT` generate no order on their own;
  recovery requires an explicit new decision.

## Disagreement policy (order status vs broker reconcile)

Any contradiction between an order's reported status and the broker position is
a hard stop, not a silent assumption either way:

- entry reported FILLED but position is FLAT (phantom fill) → `SAFETY_STOP`
  (`RECONCILIATION_DISAGREEMENT`), reservation kept;
- entry reported FILLED but a different leg materialised → same, state mirrors
  the broker;
- entry rejected but the broker holds a position → `SAFETY_STOP`, no phantom
  flat, reservation kept;
- close reported FILLED but the position is not FLAT → `SAFETY_STOP`
  (`RECONCILIATION_NOT_FLAT`), opposite leg never opened;
- close rejected but the broker is already FLAT → `SAFETY_STOP`, slot released,
  state FLAT (no phantom hold).

## Remaining limitations and non-goals

- `SAFETY_STOP` is an event, not a latched state: the machine stops trading
  immediately and never auto-repairs, but a fresh explicit signal after the
  stop starts a new transition. The suite pins "no order without an explicit
  signal", not "no further transitions ever".
- While a reservation is retained after a disagreement the registry is
  conservative by design: it may briefly describe a leg different from the
  broker's reality until an operator resolves it. Other runs are still blocked
  (safe) during that window.
- The broker/contract protocols remain deterministic fakes; no real adapter is
  added in this phase, and nothing wires vNext into the WS 7.9 harness.
- EOD behavior, overnight-guard changes, performance claims and backtests
  remain out of scope.