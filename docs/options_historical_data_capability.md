# Historical Options Data — Acquisition Capability (DATA-003)

Dated: 2026-09-26 | Workstream: `historical_options_data_capability` | Outcome: **C — UNAVAILABLE**

> Data-acquisition capability milestone for Phase 12. This document records the
> live provider capability audit and the resulting capability-gate verdict. It
> does **not** modify the frozen contract (`docs/options_data_contract.md`), the
> Phase 12 status (`docs/options_research_oos.md`), or any strategy/OOS file.
> No dataset was materialised, no strategy work was started, and the live
> execution/paper changes are untouched (`ALGO READY=NO` unchanged).

## 1. Objective

Resolve the Phase 12 blocker `HISTORICAL_OPTIONS_DATA_UNAVAILABLE`: test whether a
verified historical NIFTY options-chain dataset for the research window can be
retrieved by an approved/supported mechanism with the existing provider access,
and materialised under the frozen `options_chain_bar_v1` contract. Read-only
capability checks only.

## 2. Provider & API surfaces tested (read-only GET, 2026-09-26)

Live evidence captured by `scripts/upstox_historical_options_capability.py`
(reads `FNO_UPSTOX_ACCESS_TOKEN`, never prints it; writes the git-ignored
artifact `reports/forensics/historical_options_capability_2026-09-26.{json,md}`).

| Surface | Request | Result |
|---|---|---|
| `GET /v2/option/contract` | `instrument_key=NSE_INDEX\|Nifty 50` | 200 — 1824 rows, 18 expiries (`2026-09-29..2031-06-24`) |
| `GET /v2/option/chain` | `instrument_key` + `expiry_date=2026-09-29` | 200 — 138 rows; 13 fields observed (ltp, bid/ask, volume, oi, prev_oi, iv, delta/gamma/theta/vega, strike, option_type, instrument_key) |
| `GET /v2/expired-instruments/expiries` | `instrument_key` (documented shape) | **401** — "This API is available exclusively with an Upstox Plus plan subscription" |
| `GET /v2/expired-instruments/option/contract` | `instrument_key` + `expiry_date=2025-09-25` | **401** — "…exclusively with an Upstox Plus plan subscription…" |
| `GET /v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to}/{from}` | documented path shape (`NSE_FO|73507|24-04-2025/5minute/…`) | **401** — "…exclusively with an Upstox Plus plan subscription…" |
| `GET /v3/historical-candle/{NSE_FO|65881}/minutes/5/{to}/{from}` | window `2024-01-02..2024-01-05` | 200 — **0 candles** (pre-listing window for the currently-listed contract) |

Notes:

- The live chain and contract endpoints are **snapshot-only**: they expose no
  `as-of` parameter, so they can never serve historical chains (classified
  `NOT_HISTORICAL`).
- `GET /v3/historical-candle` serves per-contract 5m OHLCV+OI candles **only
  from the contract listing date** (0 rows in the pre-listing probe). It cannot
  reconstruct a historical full-chain surface for a closed research window.
- The **only** documented historical F&O API is the Upstox **Expired
  Instruments** API, and all three of its endpoints are **Upstox Plus plan
  gated**. The current token (`FNO_UPSTOX_ACCESS_TOKEN`) has no Plus plan:
  every request is refused with HTTP 401 and the exact entitlement message
  preserved verbatim (never bypassed). Prior evidence (`runs/research/day_batch/
  DATA_002_NIFTY_FUTURES_ACQUISITION.md`) records the same blocker (UDAPI1149)
  for expired futures.

## 3. Capability-gate verdict (Phase 12, unchanged)

Run via the deterministic, credential-free `capability_probe.diagnose`:

- Verdict: **`HISTORICAL_OPTIONS_DATA_UNAVAILABLE`**
- `upstox-live-chain` -> `NOT_HISTORICAL`
- `upstox-expired-contracts` -> `BLOCKED`
- `upstox-v3-option-candle` -> `NOT_HISTORICAL`
- Dataset: none — `options_data/` absent, `options_manifest.json` absent.
- Contract: `options_chain_bar_v1` **not modified**; nothing materialised,
  validated or fingerprinted.

This matches accepted outcome **C**: with current entitlement the historical
options-chain data is not retrievable through an approved/supported mechanism,
so the Phase 12 gate stays closed and nothing is fabricated.

## 4. Required subscription / entitlement path to unblock

The single blocker is entitlement: the documented **Upstox Plus plan** is
required for the expired-instruments API. If a Plus entitlement is provisioned
for the token, re-run the same read-only runner to re-grade access:

```powershell
python scripts/upstox_historical_options_capability.py
```

When entitled, the flow is: `expiries` -> `option/contract` (expired), then
per-contract `historical-candle` with `expired_instrument_key`. Even then, the
per-contract 5m OHLCV+OI surface lacks observed bid/ask, so the source would
classify `INSUFFICIENT` (not `ADEQUATE`) for realistic entry/exit fills unless
a separate bid/ask (tick) history is also provided.

## 5. Safety

- Read-only HTTP GETs against market-data endpoints only; no orders, no
  execution, no price modification.
- Token never printed/logged; provider text token-redacted; artifact contains
  `credentials_in_output: false`.
- Probe module is provider-neutral, network-free in tests, deterministic, and
  contains no `execution`/`upstox`/`credentials` imports, no wall-clock reads
  and no protected-window references (AST-enforced by tests).
- Protected OOS `2025-10-06..2026-09-11` untouched (`protected_oos.touched: false`).
- No dataset written, no backtests, no strategy design/tuning, no
  promotion decision. Live execution state unchanged: `ALGO READY=NO`.

## 6. Tests executed

- New hermetic suite `tests/test_options_research_capability_probe.py` (23 tests) — passed.
- Options research + paper + risk + adapter group — passed (303).
- Fresh-OOS / protected group — passed (136).
- Full suite — **3135 passed, 0 failed** (3129 non-slow + 6 slow).