# Options expression experiment spec (preregistration)

Status: **SPEC ONLY — not run, not authorized, no data, no network, no code.**
This document preregisters the design that any future vNext options-expression
study MUST follow, resolving RQ1/RQ2/RQ5 of
`docs/execution_semantics_vnext_design.md` ("LONG index => BUY CALL",
"SHORT index => BUY PUT", empirical validation) without contaminating any
protected window.

Honest current state: this experiment has **not** been run. There is no options
dataset (see `docs/options_data_contract.md`), no result, no conclusion, no
"validated" mapping. Nothing here is an options performance claim.

---

## 1. Research question (RQ1/RQ2)

- RQ1 — Does the index LONG signal, expressed as BUY CE, earn positive
  premium-based expectancy after costs?
- RQ2 — Does the index SHORT signal, expressed as BUY PE (never PUT=SELL),
  earn positive premium-based expectancy after costs, and how does it compare
  with a SELL-CALL / SHORT-FUTURE expression of the same signal?

## 2. Expression under test (frozen mapping)

- LONG  index  -> BUY (CE)   — opening transaction only, quantity = lot size.
- SHORT index  -> BUY (PE)   — opening transaction only, quantity = lot size.
- FLAT         -> SELL the held leg (CE or PE) — SELL is never an OPEN.
- Reversal     -> SELL held leg confirmed flat, then BUY opposite leg.
This is exactly the vNext machine semantics (`execution/vnext/*`, pinned in
`tests/test_execution_semantics_vnext*.py`); the experiment evaluates that
mapping, it does not license new execution behavior.

## 3. Data contract

- The study dataset MUST satisfy every rule of `docs/options_data_contract.md`.
- No dataset may reuse the protected window `2025-10-06..2026-09-11` as OOS.
- Any option-chain/futures bars dated `>= 2026-09-11` are **single-use**:
  consumed once by this preregistered design, then pinned and never re-read
  (the exact fresh-OOS pool discipline).
- Acquisition (if ever authorized by the operator with real data and consent)
  is independent and gated; it is not part of or prerequisite to this spec.

## 4. Preregistration

Before ANY off-sample measurement, the following must be written into this
document (or a versioned update) exactly:

1. Dataset identity: manifest SHA-256 pins of the option-chain/futures files.
2. Sample window and session coverage (dates, identity keys, # bars).
3. Endpoints (frozen, no post-hoc selection):
   - expectancy per trade (premium in, premium out, slippage, commissions),
   - win rate, profit factor, max drawdown,
   - premium-cost split and theta/gap attribution per RQ1/RQ2.
4. Cost model: the pinned research evaluation cost model
   (`EvaluationConfig` defaults; commission 0.03% on slip-adjusted fill notional,
   slippage 0.1%) — identical to the result-pinned cost-parity guard.
5. Hypotheses with direction: H0 = zero/negative expectancy; H1 = positive
   expectancy with a pre-specified minimum effect and sample size.
6. Contamination declaration: which parts, if any, are IN-SAMPLE by design.
7. Promotion gate: the threshold an OOS result must clear to be eligible for
   promotion review (mirrors the algorithm promotion gate; promotion itself
   still requires human approval).

Rule: a study that reports a result without this preregistration on file is
invalid by construction and is discarded.

## 5. Success / promotion criteria

A result is "eligible for promotion review" only when ALL hold, and promotion
itself is a separate human gate:

- options data satisfied the data contract,
- preregistration recorded endpoints and H1 before measurement,
- off-sample (fresh before 2026-09-11 untouched, after 2026-09-11 single-use)
  positive net expectancy, above the recorded minimum effect,
- risk surface honored (single-slot, no written options, no naked short,
  no overnight gap without an explicit Policy B check),
- cost-parity discipline held (research net = central net + r*s*(payout),
  verified by guard test as in the validation job).

No automatic READY/live/promotion ever results from this spec.

## 6. What this spec does NOT do

- Does not run, authorize, schedule, or fabricate any options study.
- Does not change RQ1–RQ5 status (still UNRESOLVED).
- Does not modify OUR-ALGO-004, the frozen algorithm, any hash pin, or any
  protected research artifact.

## 7. Safety confirmation

- Files created: this spec only.
- Files modified: 0 production/research/report/config/dataset files.
- Network: 0. Options data availability: unchanged (NOT AVAILABLE).
- OOS: 0 consumed. No study run.
- Commits / pushes: 0.