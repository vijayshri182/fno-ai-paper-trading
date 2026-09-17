# RESEARCH DECISION LEDGER

Chronological, append-only record of candidate-algorithm research decisions on the project's
discovery algorithm. **PAPER-ONLY.** Entry does NOT imply promotion. LIVE GATE CLOSED;
PROMOTION=NO; ALGO READY=NO; algorithm health=RED. No commit triggered by research here.

---

## Entry 001 — OUR-ALGO-002 (overlay reversal-flat-hold)

- Date: recorded earlier (pre-OUR-ALGO-003).
- Artifact: `runs/research/day_batch/our_algo_002_transition_exit.json`.
- Decision: **REJECTED / NON-IMPLEMENTABLE** — overlay added concurrent held legs on top of all
  226 benchmark entries, violating the real single-slot execution constraint. Led to OUR-ALGO-003.

---

## Entry 002 — OUR-ALGO-003 (in-loop single-slot reversal-flat-hold) — REJECTED

- **Question**: does the reversal confluence-flat exit suppression survive REALISTIC single-slot
  execution when implemented IN-LOOP inside the frozen Iteration-009 engine state machine (the
  exact treatment OUR-ALGO-002 could not model)?
- **Experiment**: A = frozen Iteration-009 VOL-led (guarded, byte-identical). B = same single-slot
  engine loop, exactly ONE structural change: REVERSAL-labeled positions (locked from the prior
  session's causal sign) suppress the first-of-day confluence-flat exit and keep holding under the
  frozen machinery; slot-occupancy displacement is ledgered end-to-end. Zero tuning, zero new
  thresholds, pre-OOS only, no OOS use.
- **Artifact**: `runs/research/day_batch/our_algo_003_single_slot_reversal_hold.json`
  (sha256 `5775c3ec16e1f5f056a157c8cd90477739910e5ee33a413ecde1d1693acbdd87`; double-run
  byte-identical = deterministic).
- **Report**: `runs/research/day_batch/OUR_ALGO_003_RESEARCH.md`.
- **Evidence summary** (A → B):
  - Net +12065.80 → +12769.36 (+703.56); gross edge 24352.10 → 13282.30; RT 226 → 123;
    WR 60.18% → 53.66%; **maxDD 2388.66 → 5219.84 (2.19x ≥ 1.25x gate)**.
  - Reversal pool (whole period): per-RT 62.72 → 131.91 (+110%), WR 62.71% → 65.62%.
  - Temporal halves (independent warmups): half1 per-RT 66.94 → 179.65 & net +5931 ↔ +8030
    (improves); **half2 per-RT 53.84 → 33.88 & net +4445 ↔ +2684 (degrades)**.
  - Displacement: 254 suppressions; 100 displaced entries, all attributed (ledger complete=True).
  - Single-slot invariant: 29 bars with |position| > 1 in B (engine fill stacking under long-carry
    stop-reference divergence) — treatment does not cleanly hold one unit in every bar.
  - J: B journal RT-reconstruction partial under long-carry (full_identity_resid6 -113.34); engine
    totals authoritative; determinism itself passes.
- **Acceptance A–J**: PASS = B, D, E, F, H. FAIL = A (single-slot invariant), C
  (flat-suppression reason-isolation coupled to displacement), G (maxDD 2.19x), I (half2
  degrades), J (B identity partial).
- **Classification**: **REJECTED** — decisive on G+I (economics alone), consistent with
  single-slot realism; integrity A/C/J also fail.
- **Decision**: the reversal-flat-hold hypothesis does NOT survive realistic single-slot
  execution. **No further chaining on this hypothesis.** Other directions remain on the roadmap
  but are NOT scheduled as follow-on experiments here.
- **Status**: PAPER ONLY. PROMOTION=NO, ALGO READY=NO, algorithm health=RED, LIVE GATE CLOSED.
  OOS validation (2025-10-06 .. 2026-09-11) untouched. Nothing committed.

---

## Entry 003 — ENGINE-INTEGRITY FINDING: protective-stop PHANTOM-CYCLE (why OUR-ALGO-003's B measurement was contaminated)

- **Trigger**: the 29 bars with |position|>1 in OUR-ALGO-003's B journal (reported under A/J as
  "engine fill stacking") were re-investigated under autonomous research ownership before relying on
  any affected results.
- **Root cause (traced, reproduced, journal-verified)**: the research engine's independent WS 6.4
  protective stop (`BacktestEngine` / `enforce_stop`) is a LONG-ONLY, BAR-LOW-BASIS stop
  (`if ... is_long`; 2% fixed from entry). The in-loop candidate state machine protects both sides
  via its own provider ATR stop on BAR-CLOSE. Because the two layers use different bases and the
  loop has NO broker-position reconciliation channel, one protective stop can silently flatten the
  engine while the loop still believes it holds:
  1. 2024-05-09 12:45 the engine protective stop fired on B's LONG (bar 43605; the ONLY engine stop
     in the whole B run; entry 22545.3228, low-basis breach). The loop's ATR stop (close basis)
     never fired, so the loop believed it remained LONG.
  2. 2024-05-28 09:15 the loop's first LONG exit ("ensemble confluence broken - exits", SELL) hit a
     FLAT engine and WAS EXECUTED AS AN UNINTENDED OPPOSITE-SIDE OPEN → phantom SHORT -1
     (bar 44484) that then sat on the book while the loop believed it was flat.
  3. The phantom short SELF-SUSTAINS across the whole window: every subsequent legitimate LONG entry
     merely ANNULLED the phantom short (engine returned to flat, the intended long never materialised),
     so the next LONG exit re-created another phantom SELL open; every legitimate "enter short" then
     BLENDED onto the phantom short → |position| = 2 (29 stacking bars on 29 distinct dates, always
     09:15 enter-short onto SHORT -1). The phantom/stacked shorts were never engine-stopped because
     the protective stop only covers longs.
- **Quantified evidence** (same bars, frozen params, `EvaluationConfig().backtest()`):
  - B default (protective stop ON): 33 phantom opposite-side SELL opens; 29 stacking bars;
    single-slot invariant **False** (max qty 2); net **+12769.36**; maxDD **5219.84 (2.185x A)**.
  - B coherent (protective stop OFF; provider ATR stop still active every bar): 0 phantom opens,
    0 stacking bars; single-slot invariant **True** (max qty 1); fills 303→302; net **+15598.66**;
    maxDD **2370.63 (0.99x A, BELOW the benchmark)**.
  - A unchanged under both configs: 0 stops, 0 phantoms, 0 stacking, crashes the FULL Iter-009
    guard byte-identically (net 12065.801915115, dd 2388.664922445, fills 453, 226 RT, 136/90
    W/L). The frozen benchmark is therefore NOT affected by this finding.
- **Impact scope**: only candidate arms whose long-carry crosses the 2% low-basis stop are affected
  (here: B only). Iteration-006..012, OUR-ALGO-001/002 and the Iteration-009 benchmark never fire
  the engine protective stop on their schedules → **no retroactive result correction is required**.
  OUR-ALGO-003's REJECTED classification STANDS (its B numbers were a contaminated measurement), but
  its rejection basis (G: maxDD 2.19x; A/J: single-slot integrity) is now traced to the measurement
  contamination, not to an economic failure of the treatment.
- **Classification**: engine-vs-strategy **state divergence (harness/architecture gap)** — NOT data
  contamination, NOT non-determinism (double-run byte-identical), NOT a model bug in the treatment
  loop. The corrective measurement for future single-slot candidates is a coherent config
  (`enable_stop_loss=False`, strategy stream = single source of truth; provider ATR stop retained;
  research-only — live/WS 6.4 stop framework untouched, LIVE GATE CLOSED).
- **Decision**: schedule one follow-on controlled experiment — **OUR-ALGO-004** — re-running the
  exact reversal-flat-hold treatment under the coherent config with the frozen Iter-009 benchmark
  guard still enforced, all A–J criteria and halves recomputed. (Ledger Entry 004.)

---

## Entry 004 — OUR-ALGO-004 (COHERENT re-test of the reversal-flat-hold) — PROMISING (with criterion-C AMENDMENT-1)

- **Question**: under the coherent single-slot measurement mandated by Entry 003
  (`enable_stop_loss=False`, strategy stream = single source of truth, provider ATR stop retained),
  does the single-slot reversal-flat-hold satisfy ALL pre-registered acceptance criteria?
- **Experiment**: A = frozen Iteration-009 VOL-led benchmark, replayed byte-identically under the
  coherent config (0 engine protective stops ever fire on A's schedule — verified). B = the SAME
  exact in-loop treatment as OUR-ALGO-003 (one structural change: REVERSAL-held positions suppress
  the first-of-day confluence-flat exit; frozen broken/ATR/max-hold machinery continues; displacement
  ledgered). The contaminated default-config B is kept ONLY as a diagnostic control (contrast).
- **Artifact**: `runs/research/day_batch/our_algo_004_reversal_hold_coherent.json`
  (sha256 `bd523b653a227eea582ab10e05b2a677d3f962f4a16fe82bbf2d45a5113e5ad7`; double-run
  byte-identical = deterministic).
- **Criterion-C AMENDMENT-1 (pre-registered in this entry and in the module docstring)**: the
  OUR-ALGO-003 form of C demanded `echo_match` (every suppression bar must be a GENUINE benchmark
  flat-exit bar) and `other_flat_unchanged` (every non-suppressed benchmark flat exit must still be
  a flat exit in B). Those are **structurally impossible for any single-slot hold candidate**:
  (i) a multi-day reversal hold legitimately carries SUPPRESS_REASON on EVERY flat first-of-day,
  while the benchmark exits on the FIRST such day only (verified: 167 of 254 suppression bars are
  extension bars where the benchmark already exited); (ii) the flat exits of displaced legs
  necessarily vanish (verified: 26 of 165 benchmark flat exits are displaced-leg exits). Both are
  the SAME single displacement mechanism the ledger under D already accounts for — not a second
  change. AMENDMENT-1 replaces the two impossible sub-checks with a PROVABLE mechanism-purity
  check: every benchmark exit bar (ANY reason) absent from B must be EITHER suppressed OR a
  displaced-leg exit, with ZERO unexplained removals. The exact sub-checks (suppressed_holds,
  no_invented_flat) are retained. Applies prospectively to THIS and future candidates; verdicts for
  001/002/003 are NOT retroactively changed.
- **Evidence summary — amended C partition** (benchmark flat exits = 165): suppressed (variant
  HOLDs) = 87; displaced-leg exits = 26; intact flat exits = 52; extension hold bars (beyond the
  benchmark's first flat day) = 167; unexplained removals = 0 flat, 0 non-flat. suppressed_holds =
  True, no_invented_flat = True, partition reconciles exactly (87+26+52 = 165).
- **Evidence summary — economics (A → B coherent)**:
  - Net +12065.80 → +15598.66 (+3532.86, +29%); gross edge 24352.10 → 23925.75; RT 226 → 151;
    fills 453 → 302; WR 60.18% → 59.60%.
  - **maxDD 2388.66 → 2370.63 (0.99x — BELOW the benchmark; G gate ≤1.25x passes)**.
  - Reversal pool: per-RT 62.72 → 141.58 (+126%), WR 62.71% → 64.71%, net +7400.39 → +12034.00.
  - Temporal halves (independent warmups): half1 per-RT 66.94 → 179.65 & net +5931 ↔ +8030
    (improves); **half2 per-RT 53.84 → 105.88 & net +4445 ↔ +5513 (IMPROVES — I criterion now
    passes)**.
  - Displacement: 254 suppressions; 100 displaced entries, complete=True, 0 unexplained.
  - Single-slot invariant (coherent B): holds=True, max_qty 1. Economic identity FULL+CLOSED True
    on A AND B (J passes); causality violations 0; determinism double-run byte-identical.
  - Diagnostic control (contaminated B, default config, contrast only): net +12769.36, maxDD
    5219.84, 33 phantom SELL opens, 29 stacking bars, 1 protective stop, single-slot False.
- **Acceptance A–J**: full pass A..J (A single-slot, B benchmark identity, C flat-suppression-only
  under AMENDMENT-1, D displacement ledger, E reversal quality improves, F net not worse, G risk
  safe, H cost efficient (cost 8327 ≤ 12364; cost/RT 55.15 ≤ 1.10x 54.71), I temporal stability
  (BOTH halves improve), J identity+causality+determinism).
- **Classification**: **PROMISING** (research-level; NOT a promotion/have-LIVE candidate).
- **Decision**: the coherent measurement confirms the reversal-flat-hold hypothesis as a causally
  sound, single-slot, cost-aware, risk-controlled, temporally robust research candidate — the
  OUR-ALGO-003 rejection is fully explained by the Entry-003 protective-stop contamination, not by
  an economic failure. This is the research finalization candidate pending the final report;
  downstream gates (OOS dry-run, live paper, human approval) remain separate, later, and unstarted.
- **Ratification**: Criterion-C AMENDMENT-1 RATIFIED by the task-giver on finalization. The search
  therefore concludes here: **FINAL RESEARCH CANDIDATE IDENTIFIED — ALGORITHM SEARCH COMPLETE.**
- **Status**: PAPER ONLY. PROMOTION=NO, ALGO READY=NO, algorithm health=RED, LIVE GATE CLOSED.
  OOS validation (2025-10-06 .. 2026-09-11) untouched. Nothing committed.

---

## Entry 005 — AUTONOMOUS LOOP: BLOCKED-NO-LONGER-SILENT

- **Recorded**: 2026-09-17T06:31:37Z
- **Candidate**: OUR-ALGO-004 (coherent single-slot reversal-flat-hold; PROMISING per Entry 004).
- **Blocked operation**: run protected-OOS validation of OUR-ALGO-004 B on 2025-10-06..2026-09-11.
- **Reason**: the protected out-of-sample window 2025-10-06..2026-09-11 is already consumed exactly once for this lineage (iteration006: 17,412 bars / 233 days, engine_runs=1; WS 7.16 walk-forward: 12,975 bars / 173 days with validation 9,387 bars, conclusion B). PROJECT_PLAN §17e.11 forbids a re-run and forbids relabelling the consumed interval as fresh OOS.
- **Evidence**: runs/research/day_batch/iteration_006_protected_oos_n3.json; reports/model_performance/oos_confirmation.json; reports/walkforward/summary.json; docs/project_state.json.
- **Prohibited action**: re-run on 2025-10-06..2026-09-11; relabel that interval as fresh OOS; weaken PROJECT_PLAN §17e.11 / WS 7.16 gates; change health to GREEN.
- **Available legitimate paths**: A fresh untouched OOS data collected strictly after 2026-09-11 (single-use; coverage 20 days / 1500 bars / 30 trades) then one validation run; B research of a separately permitted family over the pre-OOS domain (no OOS touched); C new pre-registered hypothesis set on the research domain. Data collection, registration, inspection, validation and promotion remain separate steps.
- **Human decision required**: not at entry time — the loop waits explicitly for fresh OOS data (WAITING_FOR_FRESH_OOS_DATA) and states exactly what data remains required; a human-decision request (A/B/C) is raised only when no protocol-compliant path remains.
- **Loop behavior changed**: a BLOCKED condition no longer silently halts candidate progression: the autonomous research/validation loop now returns one terminal token and either continues on a permitted path, waits with an explicit data requirement, or escalates with an A/B/C human-decision report.
- **Fresh-data pool**: fresh pool [upstox_Nifty_50_5m_20260916_20260916: 75 bars / 1 days / untouched] readiness=needs 20 trading days, has 1; needs 1500 bars, has 75; no collected window is registered yet
- **Acquisition status**: automated acquisition is credential-gated (FNO_UPSTOX_ACCESS_TOKEN)
- **Tests**: 12 focused deterministic loop tests (tests/test_autonomous_loop.py) -- all passed
- **Full pytest**: full pytest suite -- 1782 passed
- **Current algorithm state**: PROMOTION=NO, ALGO READY=NO, algorithm health=RED, ALGORITHM SEARCH COMPLETE (Entry 004), final research candidate identified.
- **Current live gate state**: LIVE GATE CLOSED; no real order; paper-only.
- **Decision**: WAITING_FOR_FRESH_OOS_DATA

---

## Entry 006 — WS 7.24 FRESH OOS ACQUISITION + AUTONOMOUS CONTINUE

- **Recorded**: 2026-09-17T07:40:00Z
- **Work stream**: WS 7.24 resume autonomous algorithm research
- **Credential check**: FNO_UPSTOX_ACCESS_TOKEN present in .env and effective post load_dotenv; authenticate PASS via GET /v3/historical-candle (HTTP 200)
- **Fresh OOS acquisition**: acquired upstox_Nifty_50_5m_20260915_20260915 (75 bars, sha256 5b21d855...) strictly after 2026-09-11; 2026-09-17 not yet served (REST completed-session only)
- **Fresh-data pool**: upstox_Nifty_50_5m_20260915_20260915: 75 bars / 1 days / untouched; upstox_Nifty_50_5m_20260916_20260916: 75 bars / 1 days / untouched; no window registered yet; readiness needs 20 days has 1, needs 1500 bars has 75
- **Decision**: CONTINUE_AUTONOMOUSLY (next action acquire_fresh_oos_data); pooled fresh data remains UNTOUCHED and unregistered
- **Tests**: 12 focused deterministic loop tests passed; full pytest 1782 passed
- **Constraints honored**: no orders, LIVE GATE CLOSED, no promotion, protected OOS 2025-10-06..2026-09-11 untouched, no commit/push
- **Ledger continuity**: append-only; entries 001-005 unchanged

---

## Entry 007 — WS 7.26 FRESH OOS ACCUMULATION + CONTINUE

- **Work stream**: WS 7.26 FRESH OOS DATA ACCUMULATION & READINESS
- **Audit**: fresh pool audited: 2 UNTOUCHED sessions post-boundary (upstox_Nifty_50_5m_20260915_20260915 sha256 5b21d855..., upstox_Nifty_50_5m_20260916_20260916 sha256 464352d6...), 75 bars each, NIFTY 50 5m INDEX, hashes re-verified load_dataset OK, registered=false, no relabelling
- **Acquisition probe**: GET-only /v3/historical-candle: 2026-09-14 served 0 bars (not a servable session); 2026-09-17 (today, closed 15:30 IST) served 0 bars ~2h post-close (REST completed-session-only, consistent with 09-16 servable only next day); no new session materialized; nothing manufactured or padded
- **Fresh-data pool**: upstox_Nifty_50_5m_20260915_20260915: 75 bars / 1 days / untouched; upstox_Nifty_50_5m_20260916_20260916: 75 bars / 1 days / untouched; 150 bars / 2 days total; no window registered; readiness needs 20 days has 1, needs 1500 bars has 75
- **Autonomous continuation**: loop decision CONTINUE_AUTONOMOUSLY (next action acquire_fresh_oos_data); missing ~18 trading days / ~1350 bars; algorithm health color does not terminate research
- **Tests**: 23 focused autonomous-loop tests passed; full pytest 1805 passed, 0 failed, 0 errors
- **Constraints honored**: no orders, LIVE GATE CLOSED, consent untouched, no promotion, protected OOS 2025-10-06..2026-09-11 untouched, no commit/push
- **Ledger continuity**: append-only; entries 001-006 unchanged
