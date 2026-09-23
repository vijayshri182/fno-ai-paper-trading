# Research Quality Audit — Iteration 001 & Iteration 002 (day-batch research)

- report: `runs/research/day_batch/RESEARCH_QUALITY_AUDIT.md` (single audit artifact)
- audit session: 2026-09-15
- scope: `scripts/run_day_batch_research.py`; iterations recorded in `ITERATION_001.md` / `ITERATION_002.md`;
  artifacts under `runs/research/day_batch/`; dataset `datasets/upstox_Nifty_50_5m_20220103_20260911.csv`
- audit constraints honored: **no new algorithm, no tuning, no Iteration 003, no production artifact modified,
  no protected OOS data used, no commit/push**. All diagnostics were READ-ONLY scripts in a temp area;
  the engine/strategy/risk/cost source was read but never changed. The only file created in the repo is this report.
- all figures below were **re-derived** from source + dataset + artifacts during this audit (not re-quoted from memory).

---

## 1. Executive summary

Iteration 001 (ADX floor 20→25) and Iteration 002 (ADX entry-confirmation overlay at 25) are methodologically
sound experiments whose headline outcomes are **fully reproducible**, whose hypotheses were pre-registered,
whose verdicts (H1 REJECTED, H2 REJECTED) are **correct on the re-derived numbers**, and whose most serious
substantive error — the Iteration-001 "100% ADX separation at entry" claim — was **already detected,
quantitatively confirmed, and corrected in place** by Iteration 002 §2. This audit independently reconstructs
that artifact.

The audit found **no CRITICAL and no HIGH** integrity failures, **two MEDIUM** reporting/taxonomy issues
(fill-count vs round-trip-count labeling; non-uniform "net" definition across the two iteration reports),
and several LOW notes (all non-binding for the conclusions). The remaining risk is presentational and hygiene,
not methodological.

**Verdict: PASS WITH WARNINGS.** Iteration 003 must not begin until the required remediation in §10 is applied.

## 2. Verdict summary

| Area | Verdict |
|---|---|
| Overall audit | **PASS WITH WARNINGS** |
| Critical findings | 0 |
| High findings | 0 |
| Medium findings | 2 |
| Low findings | 4 |
| Findings that block Iteration 003 | none (remediation below is required first) |

## 3. Method (read-only)

1. **Source inspection (read-only):** `strategies/indicators.py`, `strategies/base.py`, `strategies/composite.py`,
   `backtest/engine.py`, `backtest/config.py`, `backtest/result.py`, `broker/paper_broker.py`,
   `portfolio/portfolio.py`, `risk/manager.py`, `risk/stop_loss.py`, `data/dataset_store.py`,
   `evaluation/records.py`, `models/{order,position,market}.py`, `scripts/run_day_batch_research.py`.
2. **Test suite:** `python -m pytest -q` → **1335 passed in 33.78s** (no failures, no skips reported).
3. **Data integrity:** full-file scan of the 87,193-row dataset (ordering, duplicates, OHLC validity,
   timezone, session-gap and per-day bar-count analysis, hash re-derivation).
4. **Replay / determinism / causality (temp read-only scripts):**
   - `bulk_signals(bar i) == strategy.analyze(bars[:i+1])` on 96 sample indices (0 mismatches);
   - identical double replay → identical report (all fields);
   - full-domain composite_trend baseline + confirm-25 replay;
   - Iter-001 adx_min=25 A/B replay;
   - batch-1 / batch-2 per-day realized P&L re-derivation (both arms);
   - engine→portfolio P&L identity: `total_pnl == Σ realized + final carry MTM − Σ commission`;
   - stop / risk-rejection re-count across the full-domain journals (both arms).
5. **Artifact cross-check:** `summary.json`, `round_trips.csv`, `journal_*.csv`, `confirm_*audit*.json`,
   `domain_diagnostic.json`, and the prior iteration reports were compared against the re-derived numbers.

## 4. Evidence base

- Tests: 1335 passed (33.78 s).
- Dataset: 87,193 rows; meta hash `6c400b016c1c6d3c99a80db9a8355bc3e195e17c43537bcb93b7e198e85b7f5c` ✓
  (starts `6c400b016c1c…`).
- Pre-OOS domain exactly **69,781 bars** (2022-01-03 → last bar 2025-10-03 15:25) ✓ (runner filter `< 2025-10-06`).
- Historical control (verified in `docs/model_research_final_report_ws718.md`, `PROJECT_PLAN.md:1847`,
  `PROGRESS.md:970`, `docs/project_state.json`): champion `model_0` net **−80,269.64 ₹**, **1293 RT**,
  **W/L 63/1230 → win rate 4.87 %**, costs **82,616.24 ₹** (slippage 63,550.91 + commission 19,065.33),
  gross price edge +2,346.60 ≈ 0.

## 5. Audit area findings

### 5.1 Causality / no look-ahead (signal path) — PASS
- `indicators.py`: stateless, pure-`Decimal`, value at index `i` depends only on bars `[:i+1]`; warm-ups emit
  `None` at the front only; macd signal line, stochastic %D, adx, atr use **trailing-block seeded alignment**
  (`start = n − len(seeded)`) which is causal; RSI first value at `period` uses `gains[:period]` (causal);
  supertrend/keltner/OBV/VWAP causal. MFI/OBV degenerate under zero volume but remain causal.
- `composite.py::_compute_stacks` computes full-series stacks; `_features_at(i)` reads only offsets `i`, `i−1`,
  `i−14`, all `≥ 0` once warm (warmup=40). Structural causality holds.
- Empirical check: **96/96 sampled indices** — `bulk_signals(latch=False)` decision equals
  `strategy.analyze(bars[:i+1])` exactly. This validates the O(n) `signals_for` contract that both iterations rely on.

### 5.2 Execution semantics — PASS
- `engine.py`: signal actionable → order placed only if risk-approved → `broker.set_timestamp(bar.timestamp)`
  → `place_order(order, bar)` fills at **the signal bar's close + adverse slippage** (BUY `×1.001`, SELL `×0.999`,
  `paper_broker.py:129-132`). Not next-bar-open. One order per actionable signal; stop evaluated after the signal
  (signal-first, stop-second), never on the entry candle (`opened_at >= bar.timestamp` excluded).
- Verified re-derivation: batch metrics, full-domain fills, slippage totals all reproduce exactly.

### 5.3 Portfolio accounting / signed-quantity P&L — PASS (one LOW cosmetic note)
- `portfolio.py`: signed-delta `apply_fill`; SELL while LONG nets to FLAT (and realizes); SELL while FLAT opens
  SHORT; flip (LONG→SHORT) realizes the close then re-enters at the fill price; `engine._is_closing_fill`
  identifies closing/reversing fills. Consistent with the documented engine semantics.
- P&L identity verified on the full-domain baseline replay:
  `total_pnl (−39,382.805795840) == Σ realized (−28,431.94690) + final carry MTM (−29.69980) − Σ commission (10,921.159095840)` → **True**.
- `result.slippage_cost` (36,403.84670) is a **reporting-only** decomposition (slippage is already embedded in the
  fill prices inside `realized_pnl`); it must not be added to `total_pnl` (would double count). `BacktestResult`
  docstring states this correctly.
- **LOW:** after a long nets to zero the `Position` object persists with qty 0 and the journal renders it
  `"SHORT+0"` (`engine._pos_state` falls to the `is_long` branch), which is semantically FLAT. Cosmetic only;
  identical in both arms; next entry sees `old_qty == 0` and re-anchors correctly.

### 5.4 Risk controls — PASS
- Protective 2% stop (`stop_loss_pct=0.02`), LONG-only, anchored to the slippage-inclusive average entry;
  open-gap exits at candle open, intrabar breach at stop; entries on the entry candle excluded.
- Risk manager: position-quantity cap (75), order-notional cap (250,000), **daily-loss limit 10,000 ₹**
  (evaluated on each actionable bar with deterministic `realized_on_date`).
- The **0 stops / 0 risk-rejections** observed in both arms are **real engine outcomes**, not missing
  instrumentation: the journal records `risk_approved`/`risk_checks` for every actionable signal and `stop_fill`
  for every stop execution. Re-counted on full-domain journals: baseline fills 1767, stops 0, rejections 0;
  confirm-25 fills 2726, stops 0, rejections 0. Day-level realized P&L never approaches ₹10,000 in either arm.
- Note: shorts are unprotected by the stop (LONG-only by design); this is a project-wide constant and identical
  across both arms and the prior 5-year methodology — not an iteration-specific defect.

### 5.5 Data integrity — PASS WITH WARNINGS (LOW/MEDIUM)
- **Clean:** monotonic unique timestamps (0 violations), 0 OHLC inconsistencies, 0 non-positive prices,
  naive-IST timestamps (0 tz-aware), meta hash re-derived exactly.
- **Zero volume:** 87,193/87,193 bars (100 %) have `volume == 0`. OBV/MFI degenerate to 0 money flow and VWAP
  falls back to the anchor-window typical-price mean. This is a **project-wide input constraint** (documented in
  the prior reports) that applies uniformly to both arms — it does not bias the A/B — but limits what
  "volume" votes can represent.
- **Bar-count anomalies:** 89 days with 77 bars (86 days in 2022-03-24..07-22 carrying a 09:10 pre-open bar;
  5 days in 2025-04/05 carrying post-close 15:35/15:55 bars) and 6 partial days (12/21 bars: half-days /
  special Saturdays, incl. 2025-10-21 in the OOS region). **None of the anomalous days is a batch-1 or batch-2
  day.** Session-edge spacing is the only irregularity (no interior midday gaps).

### 5.6 Domain boundary / OOS protection — PASS
- Runner truncates strictly before `OOS_START = 2025-10-06` (`build_domain_bars`); verified 69,781 pre-OOS bars,
  last pre-OOS bar 2025-10-03 15:25.
- The file physically contains 17,412 post-OOS bars (2025-10-06..2026-09-11); the research path **never loads
  them** — no bar, statistic, or parameter from 2025-10-06+ is consumed by either iteration.
- The single-day OOS-region acquisition file (`datasets/upstox_Nifty_50_5m_20260910_20260910.csv`) and the
  discovery-cycle snapshot (`nifty50_5m_research_snapshot.csv`) are **not referenced by any research `.py`**
  (grep-verified). The `nifty50_5m_research_snapshot.csv` is used only by the separate WS 7.18 walkforward
  discovery path — out of scope for this day-batch audit.
- Batch-2 boundary verified: after batch-1's last day (2025-10-01), the next chronological pre-OOS day is
  **2025-10-03 only** (2025-10-02 absent from the dataset, 10-04/05 weekend, then OOS) → documented N=1 limitation is legitimate.

### 5.7 Determinism / reproducibility — PASS
- Identical replay twice → identical report for every replayed field.
- Every headline number was re-derived exactly (all within the reports' own rounding):
  - Iter-001 A/B adx_min=25 batch-1 realized **−285.93840 ≈ −285.94** (ITERATION_001 §5 ✓),
    full-domain total_pnl **−29,856.340448 ≈ −29,856.34** (ITERATION_001 §7 net ✓).
  - Iter-002 baseline batch-1 realized **+173.08820 ≈ +173.09**; per-day 04-08 +286.06, 09-04 +109.00,
    09-11 −77.90, 09-30 −144.07, 10-01 0 ✓; batch-2 (2025-10-03) **−10.53110 ≈ −10.53** ✓.
  - Iter-002 confirm-25 batch-1 realized **+69.42480 ≈ +69.43** (04-08 +100.53, 09-04 +91.19, 09-11 −122.29,
    09-30 0, 10-01 0) ✓; batch-2 **−229.88305 ≈ −229.88** ✓.
  - Full-domain diagnostic re-derived exactly vs `domain_diagnostic.json` (both arms, including maxDD to full
    precision, entries 1363 / exits 1363 / defers 16,187) — **exact match**.
- Artifacts live under immutable timestamped run dirs; `summary.json` embeds `dataset_hash` and `config_hash`.

### 5.8 Hypothesis framing discipline — PASS (self-correcting path verified)
- Iter-001: H1 stated before the A/B arm; single change (`adx_min` 20→25, one preset); A/B on the identical
  5 days; verdict REJECTED on the pre-registered criterion. When the "ADX separates winners/losers at entry"
  premise was later shown to be an artifact, the report was corrected in place (ITERATION_002 §2).
- Iter-002: H2 pre-registered; single architectural change (entry-confirmation overlay, fixed threshold 25);
  **threshold sweeps (23/24/26/27/28) explicitly excluded** (ITERATION_002 §4); engine/risk/costs untouched;
  controls (champion, mean_reversion, breakout) untouched; verdict REJECTED; full-domain evidence recorded as
  diagnostic only, not used to flip the day-level verdict.
- **No tuning on protected OOS data** occurred in either iteration (see 5.6).

### 5.9 Metrics & reporting hygiene — PASS WITH WARNINGS (MEDIUM)
- **W1 (MEDIUM): label taxonomy.** The full-domain tables present one column as bytestring that is really *fills*:
  baseline 1,767 (884 entries + 883 exits) and confirm 2,726 (1,363 + 1,363), while the true round-trip count is
  883 (baseline) / 1,363 (confirm). Iteration-001 §7 labelled the column **"RTs" (= 1,767)** — a 2× overstatement
  of round trips; Iteration-002 §10 labelled it "trades" (= fills). The W/L columns (244/639, 366/997) are
  correct closed-trip counts. No conclusion depends on this, but the labels are misleading.
- **W2 (MEDIUM): net definition is not uniform.** Iteration-001 §7 reports net −39,382.81 (= engine `total_pnl`,
  incl. final carry MTM −29.70); Iteration-002 §10/`domain_diagnostic.json` reports net −39,353.11
  (= `Σ realized − Σ commission`, excluding the final carry mark). Difference = −29.69980 (the domain-end open
  position's MTM). Both arms were computed identically within each report, so every delta (e.g. −23,827.02 net,
  +959 fills, +23,747.41 maxDD) is internally consistent and verdict-neutral; the :spelling:`non-uniform definition` is the issue.
- `day_pnl` (equity-delta incl. carry MTM) and `realized_pnl` are two different metrics sitting in the same
  tables; the reports correctly used `realized_pnl` for the criterion but the tables do not always carry a
  header clarifying which column is which.

### 5.10 Iter-002 overlay faithfulness / isolation — PASS
- `confirm_entries()` in the runner preserves the raw vote → latch sequence bit-for-bit (same strategy instance,
  same `adx_min=20` gate); only flat→position transitions are gated on ADX ≥ 25; flip-exits emit unconditionally;
  deferral re-visited while the same bias persists, dropped on bias flip.
- Faithfulness audit (`audit_confirmation`): **emitted 2726 = engine fills 2726, divergences [ ], stop_fills 0,
  risk_rejected [ ], ok true** — present identically in both `1_b_confirm25…` and `2_b_confirm25…` runs.
  Re-derivation also matched exactly (3rd independent implementation agrees with the two prior variants).
- The 0-divergence result is over the **entire domain** (the shadow is domain-wide), not only batch days —
  stronger than the report's own wording.
- Isolation verified: controls (champion/mean_reversion/breakout) are unchanged; timestamps of both runs in the
  same session; same `dataset_hash`; same `config_hash`; engine/risk/costs untouched in this audit's re-run too.

## 6. Iteration-001 ADX artifact — quantitative reconstruction (§5.9 of the audit)

The artifact is the claim that "every composite_trend entry with ADX ≥ 28.0 won; every entry ≤ 22.0 lost."
True flat→position **entries** (by the actual latched state machine; exits excluded), on the batch-1 days:

| target day | true entry bar | entry ADX | batch-1 outcome |
|---|---|---|---|
| 04-08 (carry-in from 04-07) | 04-07 13:40 | **28.4** | +275.42 exit (win) |
| 04-08 | 11:05 BUY | **35.3** | +10.63 RT (win) |
| 09-04 (carry-in from 09-03) | 09-03 13:50 | **20.6** | +109.00 exit (win) |
| 09-11 | 09:35 BUY | **20.3** | −77.90 (loss) |
| 09-11 | 12:05 BUY | **20.1** | carry (loss next day) |
| 09-30 | 09:20 BUY | **21.6** | −144.07 (loss) |
| 10-01 | 11:35 BUY | **20.4** | flat |

Bars that Iteration-001 §3 listed as "entries with high ADX" that are actually **flip-EXITS**:
04-08 10:35 SELL (ADX 44.0), 04-08 13:45 SELL (ADX 33.7), 09-04 11:20 SELL (ADX 28.0), 09-11 11:35 SELL (ADX 22.0) —
i.e. **3 of the 5 apparent "winning entries" (ADX 44.0/33.7/28.0) were exits of carried positions**, not entries.

Winner entry ADX: 28.4 / 35.3 / **20.6**; loser entry ADX: **20.3** / 21.6 / (31.1 carry / 33.6 carry as
worst-case entries by the carry probe) / 20.1. The distributions overlap (a winner at 20.6; losers at 20.3–33.6),
so **the "100 % separation" was an attribution artifact**. Confirmed independently of the Iteration-002 report.

## 7. Iteration-002 H2 audit — claim-by-claim

| ITERATION_002 claim | audit result |
|---|---|
| OOS untouched | PASS (5.6) |
| Iter-001 ADX claim was an entry/exit mislabel | PASS — reconstructed quantitatively above |
| Overlay preserves the underlying signal sequence | PASS — code + 2726/2726 bulk zero-divergence audit (domain-wide) |
| Baseline batch-1 realized +173.09 | PASS — re-derived +173.08820 |
| Confirm-25 batch-1 realized +69.43 (−103.66) | PASS — re-derived +69.42480; day deltas match |
| Winning trades NOT preserved (09-04 +109.00 carry, entry ADX 20.6, not preserved; carry winners reconstituted via churn) | PASS — consistent with the entry-ADX tables; -233.27 churn came from the highest-ADX confirmed entry (41.1) |
| Low-ADX losses NOT selectively removed (09-30 removed, 09-11 became two losses) | PASS — matches day re-derivation (+0 on 09-30, −68.66/−53.63 pair on 09-11) |
| Repeatability batch-2: −10.53 → −229.88, opposite, N=1 | PASS — re-derived −10.53 / −229.88; boundary (only 2025-10-03) verified |
| Full-domain diagnostic (fills/W/L/realized/fees/net/maxDD/defers 16,187) | PASS — exact re-derivation, W/L = real closed-trip counts, "trades" = fills (see W1) |
| H2 REJECTED verdict | PASS — correct on the re-derived day-level criterion |

## 8. Historical-control reconciliation

The control numbers quoted in the iteration reports match the recorded `model_0` walkforward evaluation:
net −80,269.64 ₹, 1,293 RT, 63/1230 W/L (4.87 %), costs 82,616.24 ₹, gross edge ≈ 0. These are from the frozen
champion's reference-domain evaluation (`docs/model_research_final_report_ws718.md`, `PROJECT_PLAN.md:1847`);
they are context, and the day-batch iterations’ composite preset is a separate, educational challenger.

## 9. Research Integrity Matrix

| # | Area | Verdict | Evidence | Risk | Required action |
|---|---|---|---|---|---|
| 1 | Signal-path causality / no look-ahead | PASS | indicators structural read; 96/96 prefix-equivalence; engine `bars[:i+1]` | NONE | none |
| 2 | Execution semantics (bar-close fills, adverse slippage) | PASS | engine/broker source; re-derived fills/slippage | NONE | none |
| 3 | Portfolio accounting / signed P&L | PASS | P&L identity holds; SHORT+0 cosmetic quirk | LOW | document SHORT+0 = FLAT in journal schema |
| 4 | Risk controls (2% stop, ₹10k daily loss, caps) | PASS | source; real 0-stop/0-rejection outcomes re-counted | NONE | none |
| 5 | Data integrity | PASS (warnings) | full-file scan clean; 100 % zero volume; 89×77-bar + 6 partial days, none on batch days | LOW/MEDIUM | document volume caveat; log anomalous days to a known-issues note |
| 6 | OOS boundary protection | PASS | filter verified; 69,781 bars; OOS region never loaded in research path; batch-2 N=1 verified | NONE | keep truncation in every future runner |
| 7 | Determinism / reproducibility | PASS | double-replay identical; all headline numbers re-derived exactly | NONE | none |
| 8 | Hypothesis discipline / no OOS tuning | PASS | pre-registered H1/H2; sweeps excluded; self-correction on record | NONE | keep pre-registration discipline |
| 9 | Metrics & reporting hygiene | PASS WITH WARNINGS | fills-vs-RT label error (W1); net definition non-uniform (W2); day_pnl-vs-realized ambiguity | MEDIUM | apply §10 remediation 1–2 |
| 10 | Iter-002 overlay faithfulness / isolation | PASS | 2726/2726, 0 divergences domain-wide; 3rd independent re-derivation matches | NONE | none |

## 10. Required remediation (before Iteration 003)

1. **Fix the metric taxonomy:** define once — *fills* (portfolio.trade_history length), *round trips* (closed,
   realized≠0), *realized P&L* (Σ over closing fills), *net* (engine `total_pnl`, incl. final carry MTM and all
   costs) — and re-tag the domain diagnostic with explicit field names; never call fills "RTs".
2. **Make the domain diagnostic an immutable artifact:** write it under a timestamped run dir (like the other
   runs) instead of overwriting `domain_diagnostic.json` in place, and include `total_pnl`, `slippage_cost`,
   and final carry MTM alongside `Σ realized − Σ commission`.
3. **Add labelled corrigenda (not rewrites):** a note in `ITERATION_001.md §7` that its "RTs" column was fills
   (true RTs 883); and a definitions block inside `domain_diagnostic.json`.
4. **Keep the quarantine rule:** any future claimed attributions (e.g. "entry quality") must be computed on
   true flat→position entry bars with the exit/entry split explicit, mirroring the §6 reconstruction.
5. **Data hygiene note file:** record the 89 × 77-bar and 6 partial-day anomalies and the zero-volume
   constraint as a known-issues note so future iterations can check batch days before selecting them.

## 11. Fixed status lines (audit-confirmed, unchanged by this audit)

```
MODEL_0            : FROZEN        (registry/docs: frozen baseline, 0 promotions ever)
PROMOTION          : NO            (none accepted in any iteration)
ALGO READY         : NO            (index/benchmark must not be represented as option profitability; OPTION DATA not available/proven)
ALGORITHM HEALTH   : RED           (champion and all challengers net-negative after costs; zero credible edge)
LIVE TRADING       : DISABLED      (gate CLOSED; PaperBroker raises on any live construction; zero live orders)
OOS DATA USED      : NO            (iterations 001/002 consumed only pre-2025-10-06 bars)
```

## 12. Final verdict and next research question

**Verdict: PASS WITH WARNINGS.** Both completed iterations are sound, reproducible, self-correcting, and
correctly rejected; the findings are limited to reporting taxonomy (W1), net definition uniformity (W2),
and low-severity hygiene notes. No methodology error that changes a conclusion was found.

Recommended **next single research question** (NOT executed by this audit, remains gated):
> On the existing composite_trend state machine, replace ADX as the gate and test an **exit-quality** variable —
> hold a position only while the composite's own confidence/vote count supports it, exiting on vote-degradation
> rather than the current flip — leaving entries, costs, risk controls, and OOS protection unchanged; the same
> batch-1 A/B + batch-2 repeatability + full-domain diagnostic discipline applies (Iteration 002 §15 candidate).

Remain pre-OOS only. ALGO READY stays NO until promotion evidence is produced through the project's gates.