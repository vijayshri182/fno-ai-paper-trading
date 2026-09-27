# Historical Options Data — Source Decision & Acquisition Plan (DATA-004)

Dated: 2026-09-27 | Workstream: `historical_options_data_source_decision` | Decision: **documentation-only, no purchase, no acquisition**

> Defines the legitimate acquisition path for a research-grade historical NIFTY
> options dataset satisfying the frozen contract (`docs/options_data_contract.md`).
> This decision does **not** authorize, schedule, or perform acquisition: that
> remains a separate, gated activity with its own human approval. It does **not**
> modify the frozen contract, the Phase 12 status (`docs/options_research_oos.md`),
> the capability audit (`docs/options_historical_data_capability.md`), or any
> strategy/OOS file. No dataset is materialised; `options_data/` stays absent;
> availability stays `NOT AVAILABLE`.

## 1. Purpose

Resolve HOW a legitimate, research-grade historical NIFTY options dataset for the
research window (2022-01-03 .. 2025-10-03) can be acquired — which provider(s)
could satisfy which requirement level, at what documented cost/access, and under
what acquisition policy — so that a future, separately-approved Phase 13 can
proceed without re-deriving this decision. Requirements are taken verbatim from
the frozen contract and are **not weakened because a provider lacks a field**.

## 2. Evidence basis

Every provider fact below is taken from documented, dated sources:

- Upstox developer docs — Expired Instruments API family, expired historical
  candle (OHLC + volume + OI; 1/3/5/15/30min, day) and its Plus-plan gating
  (error `UDAPI1149`); Upstox Plus T&C (plan "can be activated for free
  initially"; brokerage changes — equity options flat ₹30 under Plus).
- Live capability audit 2026-09-26 (`reports/forensics/historical_options_capability_2026-09-26.{json,md}`,
  git-ignored): all three expired-instruments endpoints return HTTP 401
  UDAPI1149 with the current token; live chain/contract are snapshot-only
  (`NOT_HISTORICAL`); `/v3/historical-candle` serves per-contract candles only
  from the contract listing date.
- NSE India official pages — free F&O EOD bhavcopy (columns and publication
  timing), `Imp_volt_ddmmmyyyy.csv` implied-volatility file, Historical
  Contract-wise Price Volume Data report (CSV download, custom range), EOD /
  Historical Order & Trade data subscription (SFTP EOD / online historical, FAO
  ORDER and TRADE files with trigger files), Real-time data products (L1 best
  bid/ask, L2 depth 5, L3 depth 20, tick-by-tick full order book, multicast,
  real-time only), NSE Data & Analytics tariff sheets effective 2026-04-01.
- NSE F&O bhavcopy format change 2024-07-08 to UDiFF Common Bhavcopy Final
  (fields incl. actual expiry, strike, option type, underlying price,
  settlement price, OI, change-in-OI, traded quantity/value, trades, lot size).
- Zerodha Kite Connect, official staff statements (2021, 2024-2026): "We do not
  provide historical data for expired option instruments. Historical data is
  available only for expired futures instruments, limited to day-level candles";
  instrument tokens for live contracts only; the intraday-historical lookback
  limit applies to supported/active contracts.
- Third-party vendor quotes (2019, dated, therefore **UNVERIFIED today**):
  TrueData (NSE options tick data, monthly subscription; claimed from 2018-10),
  GFDL (NSE F&O tick data from 2018-01), Breeze/other intraday F&O vendors
  (costlier tick). Kaggle F&O bhavcopy 2010-2019 (daily, ends 2019, no bid/ask).

Prices stated for vendors and NSE tariff figures in this document are **not**
relied on as fixed costs: exact NSE pricing is per published tariff sheet /
request to `marketdata@nse.co.in` and vendor quotes are dated. No purchase or
activation is performed or recommended by this decision.

## 3. Requirement matrix (Level 1 vs Level 2)

Two requirement levels are defined so a provider is judged against exactly what
the frozen contract needs at each stage, without letting a missing field weaken a
requirement.

### Level 1 — foundational EOD research dataset (`options_chain_bar_v1`, daily)

| # | Requirement | Needed for |
|---|---|---|
| L1-1 | Contract identity: segment, instrument type (OPTIDX/OPTSTK), underlying symbol | `instrument_key`, `asset` |
| L1-2 | Expiry date (actual/revised) per contract | `expiry` |
| L1-3 | Strike price and option type (CE/PE) per contract | `strike`, `asset` |
| L1-4 | Daily open/high/low/close + settlement price | `open/high/low/close` |
| L1-5 | Traded contracts (volume) and value | `volume` |
| L1-6 | Open interest + change in OI | `oi` |
| L1-7 | Implied volatility (exchange-published when available) | research context |
| L1-8 | Lot size / multiplier | premium/notional scaling |
| L1-9 | Source provenance + reproducibility (official publisher, canonical files) | `source`, manifest |
| L1-10 | Full coverage of the research window 2022-01-03..2025-10-03 | coverage rule |
| L1-11 | Session/timestamp integrity (trading calendar, expiry rule, no silent gaps) | validation rule 4/5 |

### Level 2 — intraday 5-minute, realistic entry/exit

Everything in Level 1, plus:

| # | Requirement | Needed for |
|---|---|---|
| L2-1 | Per-contract intraday 5-minute OHLCV+OI across the full research window | `ts` 5m-aligned bars |
| L2-2 | IST timestamps, 5-minute boundary, monotonic per identity, `NO_DATA` markers | contract validation rules 1-5 |
| L2-3 | Full-surface reconstruction per session (all expiries x strikes x CE/PE) | chain research RQ1-RQ5 |
| L2-4 | Observed bid/ask history (order book) for realistic entry/exit fills | realistic simulation |
| L2-5 | Checksummed provenance (SHA-256, LF-normalized) for every chunk | manifest refusal semantics |

Requirement L2-4 (observed bid/ask) is **mandatory for realistic fills** — a
5m OHLCV+OI-only source (Upstox Expired Instruments) satisfies L2-1..L2-3 but
classifies **PARTIAL** for Level 2 on its own, exactly as the capability audit
already concluded (`INSUFFICIENT`, not `ADEQUATE`).

## 4. Provider comparison

Verdict vocabulary (closed set): `MEETS_CONTRACT` · `PARTIAL` · `NOT_SUFFICIENT` ·
`ACCESS_BLOCKED` · `UNVERIFIED`. Verdicts are per requirement level.

| Provider / path | Access & cost (documented) | Level-1 verdict | Level-2 verdict | Gap vs contract |
|---|---|---|---|---|
| **NSE official F&O EOD bhavcopy** (free) | Official publisher; daily `fo_*.csv.zip` + `Imp_volt` IV file; UDiFF from 2024-07-08; historical files + Contract-wise PV report for the full window. Website automation subject to NSE site terms. | **MEETS_CONTRACT** | **NOT_SUFFICIENT** | Daily only; no intraday `ts`, no bid/ask |
| **NSE Historical Order & Trade data** (paid, official) | Via NSE Data & Analytics; SFTP (EOD) / online platform (historical); FAO ORDER + TRADE files with trigger files; full F&O incl. options order book (bid/ask) and every trade; ~50-70 GB compressed/day F&O; tariff per published sheet / request to `marketdata@nse.co.in`. | **MEETS_CONTRACT** | **MEETS_CONTRACT** (potentially) | Cost/licensing on request (undertaking required); heavy storage; exact tariff not pinned here |
| **Upstox Expired Instruments API** (Plus-gated) | Requires Upstox Plus plan (official T&C: currently free to activate; brokerage changes to ₹30 options flat). With current token: **HTTP 401 UDAPI1149** (probed 2026-09-26). | **MEETS_CONTRACT** (if Plus) | **PARTIAL** (needs Plus; no bid/ask) | Coverage from contract listing date; no observed bid/ask anywhere |
| **Upstox live option chain/contract + v3 candle** | Snapshot-only live APIs; per-contract candle only from listing date; no `as-of` historic chain. | **NOT_SUFFICIENT** | **NOT_SUFFICIENT** | `NOT_HISTORICAL` for a closed research window |
| **Zerodha Kite Connect** | Official: no historical data for expired option instruments (futures only, day-level); live instrument tokens only; intraday historical limited to active/current contracts. | **NOT_SUFFICIENT** | **NOT_SUFFICIENT** | No expired-option history at all |
| **Commodity of third-party vendors (TrueData, GFDL, Breeze, etc.)** | Historical intraday F&O incl. options tick; quotes are dated (2019) and third-party; provenance not checksummed/reproducible; pricing unverified today. | **UNVERIFIED** | **UNVERIFIED** | Not relied upon; no official undertaking/licence equivalence |
| **Kaggle F&O bhavcopy 2010-2019** | Daily bhavcopy; ends 2019, before window; no bid/ask. | **NOT_SUFFICIENT** | **NOT_SUFFICIENT** | Window mismatch, daily granularity |

## 5. Cost / access decision (nothing purchased)

- **Level 1 — acquire from the NSE official EOD bhavcopy at zero licence cost.**
  It is the canonical, official, reproducible, checksummable source for every
  Level-1 field including IV and lot size, spanning the full research window.
  This makes the Phase 12 gate upgradable from `UNAVAILABLE` to `PARTIAL` once a
  validated dataset is materialized (still not `ADEQUATE` until bid/ask exists).
- **Level 2 needs observed bid/ask for realistic fills.** Two documented paths:
  1. **Upstox Plus activation** (official, organised, low friction) grants
     per-contract 5m OHLCV+OI for expired contracts but **no bid/ask** → Level 2
     **PARTIAL**; sufficient for single-leg cash/close-based simulation of CE/PE
     premium only if fills are explicitly degenerate (close-based, no spread).
  2. **NSE Historical Order & Trade data** (official) is the only documented
     path to full order-book bid/ask history → Level 2 **MEETS_CONTRACT**; cost,
     storage, and licensing must be obtained from NSE (tariff sheet / marketdata
     inquiry) before any budget decision — classified **on-request**, not
     assumed.
- No purchase, no activation, no credential change is made by this decision.

## 6. Recommended architecture

A provider-neutral acquisition boundary (future Phase 13 code, not added here):

`HistoricalOptionsDataProvider` — one interface per source with
- acquisition: `list_expiries()`, `list_contracts()`, `fetch_bars(contract, interval, from, to)`;
- metadata: date coverage, granularity, observed bid/ask presence, timezone,
  licensing/reproducibility flags (mapped onto `SourceFinding` in the Phase 12 gate);
- normalization: emit rows exactly as the frozen `options_chain_bar_v1` schema;
- validation: re-use the existing deterministic validator (`options_research/dataset.py`)
  and the Phase 12 gate (`options_research/capability.py`) as the **canonical
  validation boundary** — nothing new may claim READY/ADEQUATE without going
  through them;
- fingerprinting: per-file SHA-256, LF-normalized, recorded in
  `options_data/options_manifest.json` exactly per contract §5.

## 7. Acquisition policy

Pipeline (each stage is irreversible and evidence-preserving):

`Acquire → Preserve Raw Data → Record Provenance → Normalize → Validate → Fingerprint → Freeze → Research`

- Raw provider exports are preserved untouched before any normalization
  (per-chunk lineage recorded). Nothing is repaired: a failing row/file is
  quarantined and reported, never edited (contract §4, §6).
- Protected OOS `2025-10-06..2026-09-11` and fresh boundary `2026-09-11` are
  never contaminated: only `ts < 2026-09-11` belongs to the research/in-sample
  domain; anything later follows fresh-pool single-use rules (contract rule 6).
- Manifest hash mismatch ⇒ dataset REFUSED, never silently re-hashed.
- No strategy design, tuning, or backtest work is performed as part of
  acquisition.

## 8. Safety confirmation

- Files created: this decision doc only.
- Files modified: 0 production/research/report/config/dataset files.
- Network: 0. Purchases/activations: 0. Credentials: unchanged.
- Options data availability: unchanged (`NOT AVAILABLE`); Phase 12 gate verdict:
  `HISTORICAL_OPTIONS_DATA_UNAVAILABLE` (recorded in `docs/options_research_oos.md`).
- OOS: 0 consumed. Protected artifacts: untouched.
- Commits: one documentation commit (`Define historical options data acquisition path`);
  no push.