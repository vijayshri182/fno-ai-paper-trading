# Options / Futures data contract (spec)

Status: **SPEC ONLY — no code, no data, no network.** This document pins the
immutable data contract that any future options/futures research dataset MUST
satisfy before it may be parsed, cached, or used by any experiment. It resolves
the "no OI, no IV, no options surface" gap noted in the model research reports
(`docs/model_research_final_report.md`, `docs/model_research_final_report_ws718.md`)
and the open RQ1–RQ5 of `docs/execution_semantics_vnext_design.md`.

Honest current state:

- No options-chain or futures OHLC dataset exists in this repository today.
- No OI/IV/premium/surface fields exist anywhere in `datasets/`.
- Nothing may claim options data availability until a dataset passes the
  validation rules in section 4 on real files; absence stays "NOT AVAILABLE"
  (as today). Nothing here authorizes acquiring new data; acquisition is a
  separate, gated activity with its own human approval.

---

## 1. Purpose

Options research (RQ1: "LONG index => BUY CALL", RQ2: "SHORT index => BUY PUT",
and premium-cost/moneyness/theta comparisons) is impossible without historical
option-chain bars keyed to a tradable leg and expiry, plus the underlying
futures series for slippage/premium context. This document fixes the contract
those datasets must satisfy.

## 2. Contract scope

Two related record families:

| Family | Identity | Use |
|---|---|---|
| `OPTION_CHAIN_BAR` | (instrument_key, expiry, strike, call/put, ts) | signal->premium outcome measurement, spread/moneyness/IV studies |
| `FUTURES_BAR` | (instrument_key, expiry, ts) | futures expression RQ2 context, roll/premium basis |

Both families share one bar schema so a file may hold either mixed or
single-family rows; identity fields disambiguate.

## 3. Field schema (frozen)

Every record MUST contain exactly these columns, in this order, with these
types (JSON Numbers or CSV decimal strings):

| # | Field | Type | Rules |
|---|---|---|---|
| 1 | `ts` | RFC 3339 UTC timestamp | monotonic within an instrument; 5-minute boundary; no duplicates per (identity, ts) |
| 2 | `instrument_key` | string | non-empty; `SEGMENT\|instrument_token` shape enforced like `OptionContract.instrument_key` |
| 3 | `asset` | string | one of `UNDERLYING_INDEX`, `FUTURES`, `CE`, `PE` |
| 4 | `expiry` | ISO date | for `FUTURES`/`CE`/`PE`; empty for `UNDERLYING_INDEX` |
| 5 | `strike` | number | for `CE`/`PE`; 0 for `FUTURES`/`UNDERLYING_INDEX` |
| 6 | `open` | number | non-negative |
| 7 | `high` | number | non-negative; >= open and close of the same row |
| 8 | `low` | number | non-negative; <= open and close of the same row |
| 9 | `close` | number | non-negative |
| 10 | `volume` | number | non-negative integer |
| 11 | `oi` | number | open interest; non-negative integer; absent only where the exchange does not publish it |
| 12 | `source` | string | provider identifier (e.g. `UPSTOX_V3`); never free text about the machine |

Nullability: `ts`, `instrument_key`, `open`, `high`, `low`, `close` are always
present. `asset`, `expiry`, `strike`, `volume`, `oi` may be empty per the rules
above — empty is "not applicable", never "missing data".

## 4. Validation rules (a dataset is usable only if ALL hold)

1. **Schema exactness** — every row has exactly the 12 fields of section 3 and
   no row contains a field outside it. Unknown fields reject the file.
2. **Identity consistency** — `CE` rows have strike > 0; `PE` rows have strike > 0
   and, for the same (expiry, strike, timestamp), the matching `CE`/`PE` pair
   shares `instrument_key` ±1 token. `FUTURES` rows carry an expiry.
   `UNDERLYING_INDEX` rows carry neither strike nor expiry.
3. **OHLC integrity** — `high >= max(open, close)`, `low <= min(open, close)`,
   all non-negative. Violating rows reject the dataset.
4. **Timestamp integrity** — timestamps are 5-minute aligned; strictly
   increasing per identity; no interleaved source clock gaps beyond a day
   boundary without a documented `NO_DATA` marker.
5. **Coverage** — each identity spans contiguous sessions; a missing session is
   recorded as an explicit `NO_DATA` sentinel, never by silent absence.
6. **Fresh-boundary rule** — any dataset whose `ts >= 2026-09-11T00:00:00+05:30`
   is part of the **single-use fresh pool**: consumed once by exactly one
   preregistered experiment, then hash-pinned and never re-read (mirrors the
   fresh-OOS pool discipline).
7. **Protected-window rule** — no dataset may silently reuse the protected
   window `2025-10-06..2026-09-11` as "new" options evidence; any use must be
   declared as IN-SAMPLE per the experiment spec.

## 5. File layout

- Directory: `<repo>/options_data/` (new; does not exist today).
- One provider export per session, or verified historical chunks, named
  `options_YYYY-MM-DD.{csv,jsonl}`; a sidecar `options_manifest.json` records
  per-file SHA-256 (LF-normalized hash, same convention as the fresh-OOS pool),
  row counts, identity coverage, and the acquisition source.
- The manifest is part of the dataset: a file whose hash does not match its
  manifest entry is REFUSED.

## 6. Refusal semantics

A dataset is REFUSED (never cached, never parsed into a study) when any rule in
section 4 fails, with the failing rule named. The system never "repairs"
option data: a file that fails validation is quarantined and reported, exactly
like the fresh-OOS collector treats a bad session.

## 7. What this spec does NOT do

- Does not authorize or schedule acquisition (separate gated activity).
- Does not assert any options performance or backtest claim.
- Does not change any research artifact, hash pin, or RQ1–RQ5 resolution.
- Does not modify OUR-ALGO-004 or any protected file.

## 8. Safety confirmation

- Files created: this spec only.
- Files modified: 0 production/research/report/config/dataset files.
- Network: 0. Options data availability: unchanged (NOT AVAILABLE).
- OOS: 0 consumed.
- Commits / pushes: 0.