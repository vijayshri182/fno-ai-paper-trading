# FNO AI-DAPT — 20-Item Engineering Report

**Project.** AI-Enabled F&O Decision-Support & Adaptive Paper-Trading
(AI-FNO-DAPT), repo root `C:\Vijay_GitHub\fno-ai-paper-trading`.
**Status.** Phase 6 (Paper Trading V1) **READY**; Phase 7 evaluation-first work
streams **7.1–7.5 and 7.9–7.14 implemented** (11 of 14); WS 7.6 (regime-aware
evaluation), WS 7.7 (GUI), WS 7.8 (continuous agent) remain **PLANNED** behind
the evaluation discipline.
**For.** Human review. Prepared 2026-09-12 after WS 7.14
(`39b7791`); `master == origin/master`, full suite **788 passed offline**.

Every item below states a fact, the evidence pointer, and the safety guard
attached to it. Nothing here claims live-broker trading, live market data
handling beyond read-only adapters, or real-money results.

---

## The 20 engineering items

**1. Paper-only system boundary.** The product is paper trading; real-broker
execution is out of scope. Evidence: broker/portfolio/risk/session code stamps
paper mode (`is_live=False`), AL candidates are advisory-only, and a set of AST
import-boundary tests (`test_promotion.py`, `test_learning_loop.py`,
`test_alerting.py`) prove that the promotion, learning, and alerting layers
import **no** broker/portfolio/risk/service/execution code. Nothing the AI/loop
writes can place an order.

**2. The frozen baseline.** MA(5,21) long-only crossover on NIFTY 50 is the
registered champion and is **frozen**. No single session — including the
10-Sep-2026 real-data paper replay loss (~₹194.68) — triggers parameter changes
or strategy replacement. Evidence: §17d/§17f.2 of `PROJECT_PLAN.md`, registry
baseline at model start.

**3. Risk and execution are deterministic gates.** `RiskManager` static caps are
the final authority; any risk-check failure rejects the order with no execution.
Sizing is risk-based quantity via exact `Decimal`, rounded down to lot size, skip
below lot, bounded by cash; stop-loss evaluates at the worse of candle open and
stop price. Verified by the V1 acceptance replay (30/30) and the offline suite.

**4. Currency and numeric integrity.** All money/quantity math uses exact
`Decimal` (INR); no float rounding in the trading path; cent/step handling per
instrument rules. Mixing `FixedDecimal`/`Decimal`/`float` is disallowed in the
domain layer.

**5. No look-ahead, by construction.** The backtest engine feeds each bar
`bars[:i+1]`; orders fill at that bar's close; fills are stamped with **bar
timestamps** for full determinism. Regime labels at decision time use only
`detect_prefix` over the bars seen so far. Capture records the regime at the
**entry bar** — never the exit/future regime.

**6. Deterministic core.** The session loop uses an injected clock and
deterministic data sources; duplicate-candle guard, cash/leverage guard,
long-only gating, and NSE OPEN-phase/holiday gating are enforced; every run is
reproducible offline. Evidence: WS 6.2–6.7, acceptance replay, `PaperSession`
seams.

**7. Failure-safe data handling.** External errors surface as a typed
`MarketDataError` hierarchy; validation reports issues **and never repairs or
drops data**; the research pipeline re-validates every stored dataset and raises
`ValueError` on bad data before backtesting; `acquire_dataset.py` exits 2 on
invalid data (read-only by default).

**8. Standardized historical evaluation.** `evaluation/historical.py` +
`scripts/evaluate_historical.py` run costs (commission, slippage, min spread),
IS/OOS splits, walk-forward, sensitivity on explicitly enumerated parameter
pairs only — deliberately **not** an optimizer; every claim is reported net of
configured costs with out-of-sample evidence shown alongside.

**9. Five-year replay capability.** `scripts/evaluate_five_year.py` replays the
~5-year stored dataset offline, producing labelled HTML research reports (all
evidence labeled historical/synthetic, gross/net, benchmark, trade stats).
Evidence: WS 7.5.

**10. Deterministic market-regime detection.** `regime/detector.py` classifies
volatility regimes from decision-time data; purely deterministic, no look-ahead,
used for context in research and in the (non-autonomous) challenger. Evidence:
WS 7.3.

**11. Durable experience store.** `experience/store.py` persists append-only,
immutable trade-outcome records with deterministic IDs; `merge` is idempotent so
replay/capture are safe to re-run and cannot double-count evidence. Evidence:
WS 7.9.

**12. Gated candidate generation.** `learning/generation.py` emits **inert
hypotheses only**; candidate-model generation sits behind an insider-threat
DOC-gate so learning output can never auto-modify the baseline strategy. It
suggests; the promotion gate disposes.

**13. Champion vs challenger evidence.** `evaluation/champion_vs_challenger.py`
compares the frozen champion against candidate variants using shared evaluation,
per-segment breakdowns, and robustness/verdict evidence — comparison only; a
regime-filtered challenger never modifies the baseline. Evidence: WS 7.11.

**14. Controlled promotion and rollback.** `promotion/gate.py` +
`promotion/registry.py`: promotion requires a candidate to clear the gate
(out-of-sample minimum days, performance edge, robustness) and the registry is
an append-only versioned log (`list`/`start`/`promote`/`rollback [--to]`).
MA(5,21) stays registered until a candidate clears the gate on real data;
rollback is first-class. Evidence: WS 7.12.

**15. Continuous feedback / learning loop.** `learning/loop.py` +
`scripts/run_learning_loop.py` tie everything together per cycle: paper-trade
replay → experience capture → store merge → hypotheses → gate → promoted
champion feeds the next cycle; the active champion is resolved explicitly (fail
loud, never silently fall back); HTML output is header-stamped **PAPER TRADING —
NO LIVE ORDER**. Evidence: WS 7.13.

**16. Capture correctness.** `learning/capture.py` records **closed round trips
only**; open positions are counted but never materialized as evidence (no
unrealized P&L); FIFO pairing per instrument; the same candle data and decision
rules are used as in live paper trading. Deterministic identifiers keep merges
idempotent.

**17. Alerting and operational hardening.** `alerting/` provides a pluggable
sink engine (file/collector/chained) with `AlertCategory` trading/AI-learning/
risk/system; every emitted alert carries the forced environment
`PAPER TRADING — NO LIVE ORDER`; `Watchdog` yields SAFE/WATCH/STOP with
deterministic staleness (`is_stale`, aware/naive mix rejected) and
bar-sequence-integrity checks; a STOP is advisory data instructing operators to
**HOLD/STOP paper execution rather than guess**. No third-party notification
integration ships. Evidence: WS 7.14, `scripts/run_watchdog.py`.

**18. Test suite and acceptance.** 788 offline tests across 37 modules, no
network and no external dependencies, including the 30/30 V1 acceptance replay
and per-module AST import-boundary guards. `pytest -q` runs green from a clean
clone. Phase-7 test counts grew monotonically 626 → 788 as work streams landed.

**19. Operations and artifacts.** 13 CLI scripts cover data acquisition,
research/evaluation reports, five-year replay, paper-session reports,
promotion/rollback, learning-loop runs, and the watchdog health report. Runtime
state (`datasets/`, `reports/`, `paper_state/`, `experience_store/`,
`model_registry/`) is git-ignored; all reports are offline, labelled HTML files
with a consistent research CSS; credentials live only in `.env` (git-ignored),
never in source.

**20. Scope honesty and what is deliberately not implemented.** Not built — WS
7.6 regime-aware strategy evaluation, WS 7.7 GUI/monitoring dashboard, WS 7.8
continuous paper-trading agent, third-party notification integrations, and any
real-broker capability. They remain PLANNED capabilities in `PROJECT_PLAN.md`
§17f/§17h–§17l and implementing them is gated by the evaluation discipline; this
report claims nothing that does not compile, test, and run offline.

---

## Metrics and evidence

| Metric | Value |
| --- | --- |
| Test suite | **788 passed**, 37 modules, offline, no deps |
| Phase-7 commits (WS 7.1→7.14) | `cfba9b6` … `39b7791` (11 feature commits, master == origin/master) |
| Exchange/broker surface | read-only historical OHLCV (`UpstoxAdapter`), no order routing; `InMemoryMarketDataProvider` default |
| Currency/precision | exact `Decimal` (INR); lot rounding; cap gate final authority |
| Baseline | MA(5,21), frozen, registered champion |
| Learning auto-mutation | impossible by construction (AST boundary + inert hypotheses + gate) |
| Alert environment | always `PAPER TRADING — NO LIVE ORDER` |
| CLI scripts | 13 (`scripts/`) |
| Phase-7 packages | ai, alerting, experience, evaluation, features, learning, models, promotion, regime |

Phase-7 work-stream status in `PROJECT_PLAN.md` §17d: WS 7.1–7.5, 7.9–7.14
**DONE**; WS 7.6–7.8 **PLANNED**.

## References
- `PROJECT_PLAN.md` §17d (roadmap/status), §17e (evaluation discipline), §17f–§17l (capabilities & controls)
- `docs/trading/PAPER_TRADING_V1.md` §13 (acceptance criteria), §2.4 (phase plan)
- `docs/architecture/ARCHITECTURE.md` (design, boundaries, capacity table)
- `PROGRESS.md` §1–§27 (per-work-stream evidence)
- `ACTIVITY_LOG.md` §4 (dated change log, incl. WS 7.14 `4ad`)