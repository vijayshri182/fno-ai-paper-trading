# DATA-005 — NSE F&O EOD Level-1 Daily Research Acquisition

Status: **APPROVED — implementation milestone (WP-1)**
Contract: `docs/options_data_contract.md` (frozen, not modified) — authority.
Decision that selects this route: `docs/options_research_data_source_plan.md` (DATA-004).

## Source identification

| Field | Value |
|---|---|
| Source id | `NSE_FO_EOD_BHAVCOPY` |
| Source | National Stock Exchange of India (official), F&O end-of-day derivative report ("fo" bhavcopy) |
| Availability | Free, public, per-trading-day archive (historic) |
| Granularity | **Daily** (end-of-day values only: open/high/low/close, traded contracts, OI, OI change) |
| Content level | **Level 1** — no intraday bars, no historical bid/ask, no order/trade-tape detail |
| License/terms | NSE terms of use for market data; public archive. No purchase required. |
| Verdict | Unsupported for intraday execution-backtest; **daily research only** |

The DATA-004 decision (Route A) selected the free, directly NSE-hosted EOD file as
the entry-level dataset. The runner preserves the raw archive bytes and records the
documented retrieval URL, so license/source terms remain inspectable.

## Why this is explicitly NOT the intraday contract schema

The frozen contract `options_chain_bar_v1` governs **5-minute intraday** option and
futures bars, with session-gap logic keyed to 5-minute spacing. Level-1 daily rows
would inevitably fail that validator (consecutive trading days are far more than
`(3+1)*5m` apart) — correctly so. DATA-005 therefore:

* reuses the *same twelve field names and types* as the contract
  (`ts, instrument_key, asset, expiry, strike, open, high, low, close, volume, oi,
  source`) so a future Level-2 intraday dataset can land on identical conventions;
* assigns its own schema version `nse_fo_eod_level1_daily_v1` and label
  `LEVEL_1_DAILY_RESEARCH_ONLY`;
* validates with Level-1 daily semantics (dedup per `(identity, ts)`, daily
  ordering, daily coverage-gap checks with NO_DATA markers, price/identity/expiry
  rules) rather than the 5-minute chain validator;
* stores `ts` at the session end **15:30 IST == 10:00:00Z** (deterministic from the
  trading date, on a 5-minute boundary) and never claims intraday granularity.

## Procedure

```
scripts/nse_level1_eod_acquire.py \
  --source-path <nse_fo_<date>bhav.csv.zip> \   # locally preserved official file
  --trade-date YYYY-MM-DD                       # required when using --source-path
  [ --fetch ]                                   # opt-in read-only GET of archive URL
  [ --source-url URL ] [ --retrieval-timestamp RFC3339 ]
  [ --dataset-root datasets/options/level1_daily ]   # git-ignored
  [ --report-root reports/level1_daily ]             # git-ignored
```

Steps (deterministic; no orders, no posts, no credentials):

1. **Source discovery** — `--source-path` (recommended, an official file already
   placed) or `--fetch` (single market-data `GET` to the documented NSE archive
   URL; blocked/errored fetches record `ACCESS_BLOCKED` and exit 3, never
   fabricate).
2. **Raw preservation** — the raw CSV/ZIP is copied byte-for-byte to
   `<dataset-root>/raw/` and SHA-256 recorded. Identical bytes → `ALREADY_ACQUIRED`
   (no rewrite); differing bytes for an already-recorded name → `REFUSED` (no
   silent overwrite).
3. **Parse** — classic `fo` header (`SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP,
   OPEN, HIGH, LOW, CLOSE, SETTLE_PR, CONTRACTS, VAL_INLAKH, OPEN_INT, CHG_IN_OI,
   TIMESTAMP`); required columns enforced, unknown extras ignored, ZIP must
   contain exactly one CSV.
4. **Normalize** (12-field, daily) — CE/PE identities require a positive strike
   and option type; others normalize as `FUTURES`; `volume` = `CONTRACTS`,
   `oi` = `OPEN_INT`; derived Level-1 identity keys `NSE_FO|SYM|EXP|STK|OPT`
   are documented research identities, not exchange/Upstox tokens.
5. **Validate** — structured codes (`SCHEMA`, `SOURCE_MISSING`, `TS_PARSE`,
   `TS_TZ`, `NUMERIC`, `NEGATIVE`, `OHLC`, `IDENTITY_*`, `EXPIRY`, `DUPLICATE`,
   `ORDER`, `COVERAGE_GAP`, `SYNTHETIC_REQUIRED`). Missing days beyond 14 calendar
   days require an explicit NO_DATA marker row; nothing is imputed.
6. **Fingerprint + manifest + report** — normalized CSV written with LF newlines
   and SHA-256 (LF-normalized); machine manifest `manifest.json` (source, level,
   schema, coverage, per-file sha256/validation/provenance, raw hash) merged
   without silent overwrite; human Markdown report with full provenance.

Data artifacts are written only under the git-ignored `datasets/`, `reports/`
(and `data/`) roots; nothing is auto-committed.

## Acceptance criteria (WP-1)

| # | Criterion | Verification |
|---|---|---|
| AC-1 | Parse both CSV and ZIP layouts | `tests/test_options_research_level1_nse.py` (parse group) |
| AC-2 | Normalize to the frozen 12-field layout, daily only | normalize group — identity/volume/OI mapping, 15:30 IST anchor |
| AC-3 | Validation rejections (schema/required/ts/tz/date/numeric/negative/OHLC/identity/expiry/duplicate/order/coverage/no-data/source/synthetic) | validation group |
| AC-4 | Deterministic, mutation-sensitive SHA-256 fingerprinting | fingerprint group |
| AC-5 | Manifest refuses silent overwrites (`REFUSED`/idempotent `ALREADY_ACQUIRED`) | manifest group + runner REFUSED test |
| AC-6 | Manifest + report carry full provenance (source id, URL, retrieval ts, date range, filename, size, hash, status) | provenance report test |
| AC-7 | `SYNTHETIC_FIXTURE` can never present as market evidence | synthetic-label group |
| AC-8 | Clock-free, no forbidden imports (AST safety) | module AST scan tests |

## Limitations and frozen safeguards

* **Level-1 daily only.** This dataset is NOT sufficient for intraday 5-minute
  execution simulation and contains no historical bid/ask or intraday quotes.
  It is labeled `LEVEL_1_DAILY_RESEARCH_ONLY` in schema, manifest, report and
  module constants.
* **No fabrication.** Missing observations are recorded as NO_DATA or rejected;
  blocked network access is recorded as `ACCESS_BLOCKED`.
* **No protected-OOS reuse.** Dataset rows are validated with the Phase 12
  windows semantics; research use remains inside the research domain.
* **Paper-only.** Nothing here creates orders, touches Upstox, or reads
  credentials; the module passes the package's import-safety and clock-free scans.
* A real acquisition adds no new external behavior: the runner performs at most a
  single documented `GET` of the official NSE archive when `--fetch` is passed.