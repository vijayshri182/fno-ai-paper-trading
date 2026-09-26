# Phase 11 — Options Paper-Trading Simulation (Design)

Deterministic, auditable, **paper-only** options trade-lifecycle layer that consumes
explicit entry/exit events produced by the Phases 5–10 pipeline and drives the
existing paper infrastructure. It reuses the repository's order/fill/broker
conventions, session policy, hashed persistence and accounting identities. It
never re-runs, never re-derives and never silently repairs upstream results.

## 1. Scope and safety boundary

Strictly paper-only. This module:

* never constructs a broker order except through `PaperBroker` (hard-coded
  `is_live = False`; constructing it live raises);
* never imports `execution.*` (Upstox adapter, OAuth, live gate, live prep,
  audit/signal/state) and never touches credentials;
* never reads the wall clock — all timestamps come from injected events and an
  injected `now_fn`;
* does not enable live trading, does not open the live gate, does not schedule
  anything;
* preserves `live_trading = false` and `ALGO READY = NO` unchanged.

The Phase 11 layer is long-only (buy-to-open / sell-to-close), consistent with
the Phase 10 risk engine, which is structured for long options. A direction
other than `LONG` in an entry event is invalid (documented limitation, never a
silent reinterpretation).

## 2. Architecture

Pipeline: `NIFTY 5M → Regime → Validated Option Chain → Contract Selection →
Trade Quality → Risk → Paper Simulation → Ledger/Reconciliation`.

The paper simulation layer receives an **entry event** carrying the Phase 5–10
results. It *validates* identity/gate/fingerprint/timestamp consistency and
then simulates order → fill → position → exit → close → reconcile explicitly,
recording every step. `PaperBroker` supplies order lifecycle, slippage and
commission; the record carries rupee financials on the Phase 10 exposure basis.

```
EntryEvent ──► gate/identity validation ──► PaperBroker ──► PAPER_FILLED ──► POSITION_OPEN
                                                     │                                │
ExitEvent ──► contract/session validation ───────────┴──► EXIT_FILLED ──► POSITION_CLOSED
                                                                              │
                                                          reconcile() ──► RECONCILED
```

## 3. Entry-event contract

`EntryEvent` (frozen dataclass): `event_id`, `decision_timestamp` (naive IST),
`direction` (`LONG`), `quantity` (whole contracts), the validated
`snapshot: OptionChainSnapshot`, `regime: MarketRegimeReport`,
`selection: ContractSelectionResult`, `quality: TradeQualityResult`,
`risk: RiskResult`. It is a **simulation input from the caller/test harness**,
not a trade signal, and never a recommendation.

The engine enforces (never re-runs the upstream engines):

* **Phase 8/9/10 gates** — `selection.outcome == SELECTED` with a selected
  contract, `quality.outcome == PASS`, `risk.outcome == ELIGIBLE`,
  `regime.has_regime`. Any other upstream outcome rejects the attempt
  (UNAVAILABLE → no simulated trade, status preserved; INVALID → rejected with
  the reason; FAIL/BLOCKED → no order).
* **Identity/fingerprint pins** — recomputed
  `selection_fingerprint(selection)` must equal `quality.selection_fingerprint`
  and `risk.selection_fingerprint`; the contract keys must all be identical;
  `risk.timestamp == decision_timestamp`. Mismatch → reject, never repair.
* **Quantity/lot pins** — `quantity` a positive integer ≤ `risk.allowed_quantity`
  (and covered by the risk evaluation), `instrument.lot_size == risk.lot_size`,
  decision premium = `selected_contract.last_price` (never fabricated; `None` →
  unavailable), computed premium exposure must equal the risk engine's
  `candidate_premium_exposure` when present.
* **Session gate** — reuse `SessionPolicy.gate(decision_timestamp)`; entries are
  only accepted in `TRADING`.

## 4. Lifecycle state machine

States (explicit and auditable), from `LifecyclePhase`:

`CANDIDATE → REJECTED` | `CANDIDATE → PAPER_ORDER_CREATED → PAPER_FILLED →
POSITION_OPEN → EXIT_REQUESTED → EXIT_FILLED → POSITION_CLOSED → RECONCILED`

* Out-of-order transitions are rejected; `REJECTED` and `RECONCILED` are
  terminal.
* Duplicate processing: the same `event_id` re-submitted is idempotent (returns
  the existing record, never double-fills). A conflicting duplicate payload is
  recorded as a `REJECTED` attempt with `duplicate_conflict` and the original
  record is untouched.
* Restart recovery: records persist with a SHA-256 sidecar; on load the engine
  restores prior records (including open positions) and refuses to double-open
  the same contract.

## 5. Fill model (deterministic, versioned)

`FILL_MODEL_VERSION = "options-paper-fill-1"`.

* **Entry reference** — the selected quote's observed `ask`, else its observed
  `last_price`, else unavailable (no fill; never fabricate). A crossed market
  (`bid > ask`) is unavailable (no fill).
* **Exit reference** — the exit quote's observed `bid`, else its observed
  `last_price`. Missing/invalid/crossed exit data → `EXIT_REQUESTED` remains,
  the limitation is recorded, and **no exit price is fabricated** (includes the
  EOD-flatten-without-data case).
* **Fill price** — reuse `PaperBroker` semantics: carries the reference as a
  `MarketPrice` whose `open == high == low == close == reference` (a synthetic
  carrier whose value is one of the observed fields recorded verbatim in the
  record), then `PaperBroker.place_order` applies the repo slippage convention
  (BUY `×(1+rate)`, SELL `×(1−rate)`).
* **Commission** — `PaperBroker._compute_commission` convention:
  `qty × fill_price × multiplier × rate + fixed`, on the quoted premium value
  (`multiplier == 1` for options). This is the *traded premium value*, kept
  distinct from `premium_exposure` (below).
* **Money basis** — rupee figures use `quantity × lot_size × multiplier`, i.e.
  the same basis as the Phase 10 exposure math (`option_price × qty × lot ×
  mult`). Gross realized `= (exit_fill − entry_fill) × qty × lot × mult`;
  net `= gross − (entry_commission + exit_commission)`.
* **Premium exposure** — `option_price × qty × lot × mult`, cross-checked
  against `risk.candidate_premium_exposure`. Premium exposure is **not** a
  stop-loss and is never equated with stop risk; no options stop-loss is
  invented (Phase 10 default `STOP_MODEL_NOT_YET_DEFINED` is preserved and
  surfaces as a rejected/unavailable attempt).
* **Unrealized P&L** — only when a `MarkEvent` supplies a reliable reference
  (`last_price`, else `mid` from a valid bid/ask, else unavailable); otherwise
  `None` (never fabricated).

## 6. Reconciliation, session and EOD integration

* `reconcile(record_id)` re-derives the ledger identities from stored data:
  `net == gross − commissions`, fills exist for entry and exit, lifecycle
  progression is valid, `paper_only` is always `True`. A clean reconciliation
  transitions the record to `RECONCILED`; violations are recorded, never
  repaired.
* Session/EOD reuses `paper_track.policy` (`TRADING`/`FLATTENING`/`CLOSING`/
  `SKIP`). Exits are accepted under `TRADING`/`FLATTENING`/`CLOSING`; a required
  exit whose data is unavailable is a recorded limitation (the position stays
  open and the day report flags it) — mirroring the track's "never sleep with
  fabricated risk" stance without ever fabricating a price.
* Daily P&L is bucketed by close date; day reports carry net P&L, total
  commissions, exposure totals, open/closed/rejected counts, limitations and a
  `paper_only` flag plus a report fingerprint.

## 7. Data limitations (documented, never overcome by fabrication)

* No historical options-chain backtests; no out-of-sample tuning over chains.
* NIFTY underlying prices are never substituted for option premiums.
* Missing bid/ask/last, volume, OI, IV, Greeks or fills are limitations — never
  invented.
* `quantity` means whole contracts (lots); `lot_size` is authoritative provider
  metadata (`risk.lot_size` must match).
* Only Long (CE/PE buy-to-open) is supported (Phase 10 long-bias risk).

## 8. Tests (focused Phase 11)

Upstream gate outcomes and failures; identity/fingerprint/timestamp mismatch;
no reselection or gate bypass; lifecycle transitions, duplicates, idempotency,
recovery, reconciliation; fills with missing/invalid/crossed references; Decimal
precision, lot size, exposure, P&L, fees, slippage, daily-loss interplay; no
premium-exposure ≈ stop-loss conflation; EOD/session; determinism, no look-ahead,
no wall-clock reads, import-safety (no `execution.*`), no credentials, paper-only.

## 9. Deliverables

* `src/fno_ai_paper_trading/paper_track/options_paper/` — module (models,
  engine, fill model, persistence, report).
* `tests/test_options_paper_simulation.py` — focused tests.
* `reports/forensics/phase11_options_paper_simulation.md` (+ optional JSON
  evidence) — git-ignored per repo policy; never force-added.
* No commit/push at the end of Phase 11.