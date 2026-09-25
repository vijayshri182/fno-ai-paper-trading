# WS 7.28 — Human Operator Consent & Controlled Live-Test Readiness Handoff

Authoritative repo: `C:\Vijay_GitHub\fno-ai-paper-trading`
Mode: **HANDOFF ONLY** — read-only to code/credentials/consent, no push, no enable, no consent fabrication, no orders.

---

## 1. Current Checkpoint

- **HEAD:** `00b749d94863c633b6960f779137799a8faee73a`
- **Previous WS:** WS 7.27 (committed as `708d907` → advanced by this workstream to `00b749d`)
- **Test status (this session, both exits logged):**
  - Full suite: **1805 passed / 0 failed / exit 0** (141.94s)
  - Forced focused (`tests/test_forced_live_round_trip.py`): **12 passed / exit 0** (run twice)
  - `tests/test_live_execution_test.py` forced+breadth: **green, exit 0**
- **Working-tree status:** `PROGRESS.md`, `PROJECT_PLAN.md`, `ACTIVITY_LOG.md` **modified** (doc-only, pre-existing). No source files dirty.
- **Files changed in WS 7.27 (now committed at `00b749d`):**
  - `scripts/run_live_execution_test.py` (+159/−21) — threads `--force-one-lot-round-trip`, `--expiry-bucket`, `--strike`, `--signal-strategy`, `--instruments-file`; Upstox-master contract resolution; sanitized dry-run payload
  - `src/fno_ai_paper_trading/execution/upstox.py` (+56/−8) — `_quote_node` key normalization; broker-token reverse-map; registered/SEGMENT|TOKEN suffix identity
- **Push status:** local-only, **1 commit ahead of `origin/master`**, upstream tracked, **NOT pushed** (per handoff policy: no push without human confirmation).

## 2. Code Readiness

- **Forced one-lot round-trip seam:** implemented and committed (`scripts/run_live_execution_test.py` → `LiveExecutionTestManager(force_one_lot_round_trip=...)` → forced CALL / PUT single-lot BUY→SELL→FLAT).
- **Default-OFF:** `--force-one-lot-round-trip` is `store_true` → **off by default**; manager default `False`. Proven by `test_flag_defaults_off_and_isolated`, `test_flag_absent_hold_unchanged`.
- **Gate enforcement (mandatory):** Upstox gate stays **closed by default** (`enabled=False`, `dry_run=True`); forced flag **cannot** open a closed gate (`test_forced_gate_closed_still_refuses_flagged`, `test_flag_gate_closed_refuses`).
- **Autonomous-loop isolation:** `scripts/run_autonomous_loop.py` has **no** force flag; autonomous loop **cannot** invoke it (`test_autonomous_loop_cannot_invoke_flag`).
- **Upstox master contract resolution:** `namespace</think>/fnof_fno_ai_paper_trading/data/upstox_instruments.py` `resolve_from_upstox_master` resolves real current contract (expiry bucket, ATM strike from spot) — **strike/expiry no longer hardcoded** (old `2026-12-24`/`24500` defaults removed).
- **Current environment default safety state:** exactly one live seam (handler/test-exclusive); 35 env-source reads, zero hardcoded credentials; no `Environment.LIVE` member; gate CLOSED, `dry_run=True`, parseable **not enabled**; no consent; no real order path auto-enabled.

## 3. What Has Been Proven (green evidence)

- 1805/1805 full suite (exit 0) — WS 7.27 evidence passes on top of WS 7.26.
- 12/12 forced round-trip tests (exit 0).
- Gate **rejects** flagged round-trip while closed (honors CLOSED over FLAG).
- Consent/token mismatch → gate refuses (no silent bypass).
- Offline broker/dry-run seam works through manager.
- **Exact limitation:** a **real broker order + real fill** has **NOT** been proven in any environment. Everything above is paper/simulated/offline.

## 4. Human-Only Pending Items (cannot be automated)

- **A.** Valid broker authentication (real operator Upstox session) — obtainable only by human.
- **B.** Intended-account verification (operator confirms the target Upstox account).
- **C.** Genuine operator consent for a controlled live test.
- **D.** Consent/token fingerprint match (gate checks consent + token fingerprints; both must match operator-held values).
- **E.** Explicit LIVE/UPLINK enablement (human flips gate `enabled` only with full operator awareness).
- **F.** Human approval immediately before each BUY.
- **G.** Monitoring of the BUY fill.
- **H.** Human-controlled SELL approval/reconciliation.

None of A–H may be automated, fabricated, or substituted (see §6).

## 5. 09:15 Pre-Flight Checklist

Initial status of every line: **PASS / FAIL / UNKNOWN**. A **FAIL or UNKNOWN on any critical line ⇒ NO ORDER**.

| # | Item | Status (start) | Why critical |
|---|---|---|---|
| 1 | Operator autulthenication valid (real session) | UNKNOWN | **Critical** — no valid session ⇒ no real order |
| 2 | Intended account confirmed by operator | UNKNOWN | No ambiguous fills/account confusion |
| 3 | Genuine human consent documented (item C) | UNKNOWN | Gate requires genuine consent |
| 4 | Consent + token fingerprints match operator-held | UNKNOWN | Gate fails on mismatch |
| 5 | Explicit LIVE/UPLINK enablement (item E) | UNKNOWN | Live is off by default |
| 6 | Operator approval immediately pre-BUY (F) | UNKNOWN | Mandatory before order |
| 7 | BUY fill monitored & confirmed (G) | UNKNOWN | Pre-requisite to SELL |
| 8 | Operator approval SELL + reconciliation (H) | UNKNOWN | End-state = FLAT |
| 9 | Broker/order/position/lot invariants hold the whole time | UNKNOWN | Any drift ⇒ HOLD |
| 10 | Final decision: proceed-or-not after all F/U resolved | UNKNOWN | **Binding gate** |

**Rule: every line must be PASS and operator-confirmed before any order; else NO ORDER.**

## 6. Hard-Stop Conditions

Abort immediately — **no order, no fill, hold ALL** — if any of:

1. **Token mismatch** — consent/token fingerprints don't match operator-held value.
2. **Expired/missing consent** — no genuine, current operator consent.
3. **Gate rejection** — gate refuses (whatever the flag).
4. **Unexpected instrument** — resolved contract ≠ operator-authorized instrument.
5. **Unexpected quantity** — quantity ≠ 1 lot (broker-reported).
6. **Broker rejection** — order returned/rejected by the broker.
7. **Partial/ambiguous fill** — fill not a clean lot.
8. **SELL failure** — forced SELL leg fails to fill → position stays open.
9. **Position mismatch** — reported position ≠ expected after fill.
10. **Reconciliation failure** — book doesn't return to FLAT.

On any hard-stop: gate stays closed, nothing further is sent; operator must diagnose.

## 7. Security (non-negotiable)

- **Never** put token/API key in chat.
- **Never** put token in the consent file.
- **Never** commit credentials (no literals, no env-echo into git).
- **Never** fabricate consent or fingerprint.
- **Never** treat the force flag as authorization; it bypasses nothing.
- **Never** bypass the gate.

## 8. Live-Test Scope (the only intended test)

Exactly **one** controlled lot:
**BUY → verify actual fill → SELL → verify actual fill → reconcile to FLAT.**

This is an **execution integration test** — it proves broker round-trip plumbing. It is **NOT** evidence the trading algorithm is profitable.

## 9. Current Final Status

```
CODE: READY
TESTS: GREEN
LIVE GATE: CLOSED
REAL ORDERS: NONE
CONSENT: HUMAN ACTION REQUIRED
CREDENTIALS: HUMAN ACTION REQUIRED
LIVE ENABLEMENT: HUMAN ACTION REQUIRED
REAL-BROKER FILL: NOT YET PROVEN
OVERALL: CONDITIONALLY READY — HUMAN OPERATIONAL CHECKS PENDING
```

## 10. Next Action

The **next operator** must complete the human-only items **A–H** (§4) **without changing any safety control**, then run the **09:15 pre-flight checklist** (§5) immediately before any proposed real order.

**No real order is currently authorized.** The system is closed, offline-only, and ready for a human to decide whether to open it with genuine consent + real credentials.
