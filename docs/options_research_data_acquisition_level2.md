# Historical Options Data — Level-2 Source Evaluation (DATA-006)

Dated: 2026-09-27 | Workstream: `historical_options_data_level2_source_evaluation` | Decision: **documentary only — CONFIRMED / PARTIAL / NOT AVAILABLE / UNVERIFIED classification; no purchase, no activation**

> Compares the two documented Level-2 (intraday 5-minute, realistic entry/exit
> fills) source paths for the frozen research window (2022-01-03 .. 2025-10-03):
> **Upstox Plus Expired Instruments API** vs **NSE Historical Order & Trade data**.
>
> This is a **documentary evaluation only**. It performs no purchase, no plan
> activation, no credential change, no network call, and no dataset
> materialisation. It does **not** weaken the frozen contract
> (`docs/options_data_contract.md`), the capability audit outcome
> (`docs/options_historical_data_capability.md`), the source decision
> (`docs/options_research_data_source_plan.md`), or the Phase 12 status
> (`docs/options_research_oos.md`). Level-2 availability stays `NOT AVAILABLE`
> until a separately-approved, human-gated acquisition actually materialises and
> validates a dataset.

## 1. Purpose

Phase 12 endorses Level-1 (daily EOD bhavcopy) and marks Level-2 `INSUFFICIENT`
because the 5-minute OHLCV surface alone cannot support realistic entry/exit
fills **without observed bid/ask**. This document compares the only two
documented paths that could take Level-2 further:

1. **Upstox Plus** — the official Expired Instruments API family (per-contract
   5-minute OHLCV + OI for expired F&O contracts).
2. **NSE Historical Order & Trade data** — the official NSE Data & Analytics
   FAO ORDER + TRADE files (order book and every trade, with trigger files).

For each, every Level-2 requirement is classified in a closed vocabulary:
`CONFIRMED` · `PARTIAL` · `NOT AVAILABLE` · `UNVERIFIED`. Nothing here upgrades
or downgrades the Phase 12 gate; the gate's verdict stays whatever the
deterministic `capability_probe` computes from the current entitlement.

## 2. Evidence basis (all dated, documented)

- **DATA-003 live audit, 2026-09-26** (`docs/options_historical_data_capability.md`,
  artifact `reports/forensics/historical_options_capability_2026-09-26.{json,md}`,
  git-ignored): all three `expired-instruments` endpoints return HTTP 401 with
  the exact entitlement message "This API is available exclusively with an Upstox
  Plus plan subscription"; the current token (`FNO_UPSTOX_ACCESS_TOKEN`) has no
  Plus entitlement. Live `option/chain` and `option/contract` are snapshot-only
  (`NOT_HISTORICAL`); `v3/historical-candle` serves per-contract candles only
  from the contract listing date (0 rows in the pre-listing probe).
- **DATA-004, 2026-09-27** (`docs/options_research_data_source_plan.md`):
  requirement matrix L1-1..L1-11 / L2-1..L2-5; the NSE official EOD bhavcopy
  (+ `Imp_volt` IV file, UDiFF from 2024-07-08, Contract-wise Price Volume
  report) covering Level 1 at zero licence cost; the NSE Historical Order &
  Trade subscription (SFTP EOD / online historical, FAO ORDER+TRADE with trigger
  files, ~50-70 GB compressed/day F&O); Upstox Plus T&C ("can be activated for
  free initially", equity-options brokerage to ₹30 flat); tariff sheets effective
  2026-04-01; NSE pricing on request to `marketdata@nse.co.in`.
- Prior blocker record `runs/research/day_batch/DATA_002_NIFTY_FUTURES_ACQUISITION.md`
  (same UDAPI1149 for expired futures under the same token).

## 3. Classification vocabulary

| Verdict | Meaning |
|---|---|
| `CONFIRMED` | Capability is documented by the official publisher AND independently verifiable from current evidence (no entitlement or purchase required to assert it). |
| `PARTIAL` | Meets the requirement only partially, or only under an entitlement/subscription that is documented but **not currently provisioned**. |
| `NOT AVAILABLE` | No documented, official path exists for the requirement. |
| `UNVERIFIED` | Claim comes from a source that is dated, third-party, or not reproducible today; not relied upon. |

## 4. Requirement-level classifier (Level 2 = Level 1 requirements + L2-1..L2-5)

| # | Requirement | Upstox Plus (Expired Instruments) | NSE Historical Order & Trade |
|---|---|---|---|
| L1-1..L1-8 (identity, OHLC, volume, OI, IV, lot) | per-contract candles carry OHLCV + OI; identity from `expired_instrument_key` | `PARTIAL` (entitled only; today `ACCESS_BLOCKED` 401 UDAPI1149) | `CONFIRMED` (official FAO files carry full contract identity) |
| L1-10 full-window coverage | from contract listing date only; not the full window for later-listed contracts | `PARTIAL` (coverage gap by construction) | `CONFIRMED` (archive across the full window) |
| L1-11 session/timestamp integrity | 5-minute boundary candles, IST | `PARTIAL` (needs entitlement to verify at scale) | `CONFIRMED` (official daily files + trigger files) |
| L2-1 per-contract intraday 5m OHLCV+OI | documented | `PARTIAL` (entitled only) | `CONFIRMED` |
| L2-2 IST 5m-boundary monotonic bars | documented | `PARTIAL` (entitled only) | `CONFIRMED` |
| L2-3 full-surface reconstruction per session | per-contract pull only; surface reconstructed by many calls | `PARTIAL` | `CONFIRMED` (all contracts in daily files) |
| L2-4 observed bid/ask history for realistic fills | **no bid/ask anywhere** in the Expired Instruments API | `NOT AVAILABLE` | `CONFIRMED` (FAO ORDER files carry the order book, TRADE files every trade) |
| L2-5 checksummed provenance per chunk | SHA-256 is the consumer's job over API bytes | `PARTIAL` (must be built on acquisition) | `PARTIAL` (must be fingerprinting over SFTP/export files on arrival) |
| Fill realism stand-alone | 5m OHLCV close-based only, no spread | `PARTIAL` (degenerate fills only) | `CONFIRMED` |
| Cost / access certainty | Plus T&C "initially free to activate", brokerage change to ₹30 options flat; entitlement not provisioned | `PARTIAL` (activation human-gated; cost behavioural, not material) | tariff sheet on request to `marketdata@nse.co.in`; ~50-70 GB compressed/day F&O storage; exact NSE pricing `UNVERIFIED` until officially quoted |

### Consolidated verdict

| Path | Overall Level-2 verdict | Driving reason |
|---|---|---|
| Upstox Plus Expired Instruments API | **`PARTIAL`** | L2-1..L2-3 achievable on entitlement, but L2-4 (observed bid/ask) is `NOT AVAILABLE`; coverage is listing-date-limited. |
| NSE Historical Order & Trade | **`CONFIRMED`** for the data (full surface + order book + trades); **`UNVERIFIED`** for cost/carriage en-route | Only documented path to observed bid/ask; tariff and storage must be confirmed with NSE before any budget decision. |
| Combined (Upstox Plus for OHLCV+OI + NSE H&T for bid/ask) | `CONFIRMED` only if separately purchased; today **`NOT AVAILABLE`** as a materialised dataset | Both entitlements absent; no dataset exists. |

## 5. Gate implication (unchanged)

- With current entitlement the Phase 12 gate stays `HISTORICAL_OPTIONS_DATA_UNAVAILABLE`
  / `INSUFFICIENT` for the intraday surface — exactly as recorded in
  `docs/options_research_oos.md`. This evaluation does **not** change that verdict.
- Evidence rules for a future upgrade are preserved verbatim from the frozen
  contract and DATA-004: nothing may claim READY/ADEQUATE without passing the
  deterministic validator (`options_research/dataset.py`) and the Phase 12 gate
  (`options_research/capability.py`); a manifest hash mismatch is REFUSED, never
  silently re-hashed; raw bytes are preserved before any normalization.

## 6. Decision record (documentary only)

- **Upstox Plus: `PARTIAL`.** Its only true deficiency for Level 2 is the
  complete absence of observed bid/ask (L2-4 `NOT AVAILABLE`). Any future
  single-leg, close-based simulation under Plus would have to state that fills
  are degenerate (no spread) — it cannot be presented as bid/ask-realistic.
- **NSE Historical Order & Trade: `CONFIRMED`** as the only documented path to
  L2-4; the open items are commercial (tariff sheet), storage (~50-70 GB
  compressed/day F&O) and carriage/licensing terms — classified `UNVERIFIED`
  until an official quote is obtained. NSE pricing is per published tariff sheet
  or `marketdata@nse.co.in`.
- **No purchase, no activation, no gating decision is made here.** Level-2
  remains `NOT AVAILABLE` as a materialised dataset; the research window stays
  unsatisfied for realistic fills until a human-approved acquisition runs.

## 7. Safety confirmation

- Files created: this evaluation doc only.
- Files modified: 0 (production/research/report/config/dataset).
- Network: 0. Purchases / plan activations: 0. Credentials: unchanged.
- Options-data availability: unchanged (`NOT AVAILABLE`); Phase 12 gate:
  unchanged; `ALGO READY=NO`: unchanged.
- OOS: 0 consumed. Protected artifacts: untouched.
- Commit: one documentation commit; no push.