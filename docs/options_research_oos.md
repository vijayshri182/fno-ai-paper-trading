# Phase 12 — Options Research and OOS Evaluation (framework + capability gate)

**Status**: STOPPED-AT-DATA-GATE (by design). The historical options-data capability gate returned
`HISTORICAL_OPTIONS_DATA_UNAVAILABLE`, so Phase 12 stops at the first gate: no historical backtest,
no OOS performance claims, no fabrication. The leakage-safe research/evaluation framework is
implemented, tested and ready to consume a validated historical options dataset when (or if) one
exists under the frozen contract.

## Verdict

| Gate | Verdict |
| --- | --- |
| Historical options-data capability (`options_research/capability.py`) | **`HISTORICAL_OPTIONS_DATA_UNAVAILABLE`** |
| Strategy specification (`options_research/protocol.py`) | **`STRATEGY_SPECIFICATION_REQUIRED`** |

Phase 12 does **not** claim an edge, does **not** promote anything, does **not** touch the
protected OOS window, and does **not** start Phase 13.

## Why the gate is UNAVAILABLE (evidence, no fabrication)

1. **No historical options dataset is materialised.** `docs/options_data_contract.md` freezes the
   12-field `OPTION_CHAIN_BAR` schema (`options_chain_bar_v1`) plus the layout
   `<repo>/options_data/options_manifest.json`; that directory **does not exist** and the contract's
   status stays `NOT AVAILABLE`.
2. **The current provider path cannot serve historical chains.**
   - `upstox-live-chain` (`research/options/upstox_adapter.py` `UpstoxOptionChainProvider`) is
     **live-snapshot only** (GET `/v2/option/chain` + GET `/v2/option/contract`, no as-of
     parameter) → classified `NOT_HISTORICAL`.
   - `upstox-expired-contracts` (Upstox Expired Instruments API, the only documented historical
     F&O endpoint) returned **HTTP 401 UDAPI1149 — Upstox Plus plan required** on the current
     access token → classified `BLOCKED` (DATA-002 audit; also evidenced by
     `datasets/futures/futures_contract_inventory.csv`, whose rows are `NOT_ACQUIRED` with reason
     `UPSTOX_PLUS_REQUIRED_UNKNOWN_UDAPI1149`).
3. **No scraping / ToS-bypass / paid-adapter acquisition** was attempted (in-scope-readable sources
   only, read-only access, per the phase constraints).

The capability gate classifies exactly these findings into the closed set `{ADEQUATE,
INSUFFICIENT, NOT_HISTORICAL, BLOCKED, SYNTHETIC_FIXTURE}` and derives the verdict
deterministically. A synthetic fixture can never satisfy the gate (fixtures carry an explicit
`SYNTHETIC_FIXTURE` label and are only used for hermetic tests, never as observed market data).

## What was built (reusable, leakage-safe research framework)

Package `src/fno_ai_paper_trading/options_research/` — composes existing Phases 5-11 modules and
repository conventions; no parallel framework.

| Module | Purpose |
| --- | --- |
| `capability.py` | The four gate verdicts (`READY`/`PARTIAL`/`UNAVAILABLE`/`INVALID`), per-source classification, deterministic `assess_readiness`. |
| `dataset.py` | Frozen `OPTION_CHAIN_BAR` row + directory/manifest validation: schema exactness, SHA-256 provenance (LF-normalised, same convention as the fresh-OOS pool), timezone normalisation (naive=IST, aware-UTC→IST, other-aware rejected), 5m alignment, duplicates/order/coverage gaps with `NO_DATA` sentinel support, CE/PE/futures identity, and the **underlying-prices-can-never-masquerade-as-option-premiums** guard. |
| `windows.py` | Chronological split integrity, no-lookahead check, RESEARCH/PROTECTED/FRESH classification, protected-window refusal and single-use consumed-window registry. Reuses `FRESH_OOS_BOUNDARY` (`2026-09-11`) and asserts the frozen invariant `PROTECTED_OOS_END == FRESH_OOS_BOUNDARY`. |
| `protocol.py` | Preregistered `ResearchProtocol` + SHA-256 fingerprint that changes on any mutation; default status `STRATEGY_SPECIFICATION_REQUIRED`. |
| `metrics.py` | Deterministic evaluation metrics computed **from the Phase 11 daily-report ledger** (`options_paper.report.build_daily_report`), with explicit denominators: every mathematically-undefined metric is `None` plus a `reasons` entry — never 0, never infinity. |

Protected-window guarantees preserved: protected OOS stays `2025-10-06 .. 2026-09-11` (inclusive,
consumed once by the OUR-ALGO-004 lineage), fresh windows remain single-use, and no Phase 12 code
loads or re-labels them.

## Tests

`tests/test_options_research_oos.py` — 61 tests covering: capability verdicts (no fixture ever
yields READY), dataset contract (schema/identity/OHLC/timezone/alignment/duplicates/order/gaps/
sentinels), manifest integrity + `file_sha256_lf` CRLF/LF stability, protocol fingerprint
mutation-sensitivity, OOS split integrity/protected refusal/no-lookahead, metrics denominators and
`None`-semantics, Phase 11 ledger integration (`build_daily_report` → `sample_from_daily_report` →
`compute_metrics`), and safety (AST import scan forbidding `execution`/`upstox`/`credentials`/
`broker`; no wall-clock reads in the package).

Regression: Phase 5-11 options pipeline **406 passed**; fresh-OOS/protected-OOS integrity group
**77 passed**; full suite **3112 passed, 0 failed** (baseline was 3051).

## Re-run

```powershell
$env:PYTHONPATH="C:\Vijay_GitHub\fno-ai-paper-trading\src"
.\.venv\Scripts\python.exe -m pytest tests\test_options_research_oos.py -q
```

## Acceptance checks (returned to the user at phase end)

1. `ALGO READY = NO` unchanged; no promotion status changed.
2. `live_trading = false`; `LiveExecutionTestGate` closed; no orders placed or prepared.
3. No real/sandbox/live execution path touched; no OAuth/credential handling in the new package.
4. No automatic commit/push performed (`reports/forensics` remains git-ignored).
5. Phase 13 not started; phase stopped at the data gate by design.