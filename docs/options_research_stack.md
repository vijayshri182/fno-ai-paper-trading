# Options research stack (Phase 5) — implementation

Status: **FOUNDATION SHIPPED — deterministic, provider-neutral, replay-safe data
quality. No dataset, no signals, no orders, no live.** This document describes
the `src/fno_ai_paper_trading/research/options/` package delivered in phase 5.

Honest current state ties directly to `docs/options_data_contract.md`:

- No options-chain dataset exists in the repository; nothing here claims
  otherwise. The dataset contract spec is unchanged and remains the gate for
  any future acquisition.
- This stack provides the **parsing → normalization → quality-classification**
  plumbing that any such dataset must pass before an experiment may use it.
- Nothing here places orders, reads credentials, or connects to a broker.

---

## 1. Purpose

Phase 6+ (contract selection, trade-quality scoring, entry/exit, OI/IV signals,
expiry/strike selection) requires a trustworthy, deterministic floor: every
option-chain payload must first be parsed identically across providers and
classified into an explicit quality state. This package ships exactly that
floor and nothing above it (boundaries in section 7).

## 2. Files added

| File | Role |
|---|---|
| `src/.../research/options/models.py` | `OptionSide`, `Greeks`, `OptionQuote`, `OptionChainSnapshot` (frozen dataclasses) |
| `src/.../research/options/errors.py` | `OptionDataError`, `OptionNormalizationError` (under `MarketDataError`) |
| `src/.../research/options/normalization.py` | strict raw→model parsing; `normalize_quote`/`normalize_chain`; canonical round-trip + `chain_fingerprint` |
| `src/.../research/options/validation.py` | `ChainValidator` → `VALID` / `INVALID` / `UNAVAILABLE` per field, per quote, per chain |
| `src/.../research/options/protocol.py` | `OptionChainProvider` (read-only boundary), `StaticChainProvider`, `OptionChainDataUnavailableError` |
| `src/.../research/options/__init__.py` | public exports |
| `tests/test_options_research_stack.py` | 46 tests, hermetic |
| `docs/options_research_stack.md` | this file |

Modified: `0` production/research/report/config/dataset files outside the
package and its test. No previously shipped line of `src/`, `tests/`, frozen
benchmarks, hash pins, risk limits, or safety gates was touched.

## 3. Architecture

```
provider payload (any vendor)
   -> normalization.normalize_chain()      [structural + deterministic]
        -> research.options.models         [frozen, Decimal, naive IST]
   -> validation.ChainValidator.validate() [semantic tri-state]
        -> ChainValidation (per quote + global, deterministic)
   -> later phases consume only VALID data
```

- **Models are lenient.** `OptionQuote` coerces `Decimal`/`int` and preserves
  absent fields as `None`; it does not enforce bid ≤ ask or sign rules — the
  validator owns semantics. Contract identity (`expiry`, `strike`, CE/PE) and
  the shared `Instrument` are the only hard constraints at construction.
- **Normalization is strict on structure, permissive on absence.** A row with a
  missing/unparseable `expiry`/`strike`/`option_type`/`timestamp` raises
  `OptionNormalizationError`. An optional field (`oi`, `iv`, `greeks`, spot,
  bid, ask, …) the provider omitted stays `None` = explicitly unavailable.
- **Validation is the tri-state classifier.** Any `INVALID` anywhere dominates;
  else any `UNAVAILABLE`; else `VALID`. All reports carry a machine-readable
  field name, state, reason and value, and are deterministic for the same
  snapshot + reference time (no clock reads inside; the caller passes
  `ref_time`).
- **Provider neutrality via a read-only boundary.** Business/research code sees
  only `OptionChainProvider` (`provider_id`, `supports`, `fetch_chain`).
  `StaticChainProvider` serves pre-built snapshots verbatim for replay.

## 4. Exact validation rules

Per quote (`validate_quote`):

| Field | Present + OK | Absent | Rule violations → INVALID |
|---|---|---|---|
| `timestamp` | valid | cannot be absent at construction | timezone-aware (must be naive IST) |
| `freshness` | age ≤ `max_age_seconds` | n/a | future timestamp; age > limit (stale) |
| `side_mapping` | CE/PE matches `instrument_type` | n/a | option_type vs instrument_type mismatch |
| `expiry_consistency` | matches snapshot expiry | n/a | differs from snapshot expiry |
| `bid` / `ask` | present, ≥ 0 | `None` → `bid`/`ask` UNAVAILABLE | negative value; crossed (`bid > ask`) via `bid_ask` |
| `bid_ask` | clean two-sided book | both missing, or only half depth → UNAVAILABLE | crossed |
| `last_price` | ≥ 0 | UNAVAILABLE | negative |
| `price_presence` | ≥ 1 of bid/ask/last present | n/a | **all three missing** |
| `open_interest` | ≥ 0 | UNAVAILABLE | negative |
| `oi_change` | any value | UNAVAILABLE | — |
| `volume` | ≥ 0 | UNAVAILABLE | negative |
| `iv` | ≥ 0 | UNAVAILABLE | negative |
| `greeks_*` | present (gamma/vega ≥ 0) | UNAVAILABLE | gamma or vega negative |

Per chain (global reports):

| Report | Rule |
|---|---|
| `duplicate` | same canonical key twice (or more) → INVALID; rows are **preserved**, never silently dropped |
| `spot_unavailable` | spot absent → UNAVAILABLE (never substituted, and never injected into any quote); spot negative → INVALID; present OK → VALID |
| `side_pair` | enabled via `require_side_pair=True`: a strike with only CE or only PE → UNAVAILABLE |

Tri-state precedence: `INVALID > UNAVAILABLE > VALID` at quote and chain level.

## 5. No-fabrication guarantees

- Absent optional fields stay `None` and surface as `UNAVAILABLE` with the
  field name.
- The underlying spot is read only from the snapshot-level `spot_price` /
  `underlying_price`; it is never copied into a quote. `without_spot()`
  explicitly forces spot absent for replay consumers.
- Duplicate rows are flagged, never merged, never averaged, never repaired.
- Missing-session/OI/IV data cannot become "zero" or invented values — the
  classifier names the missing field instead.

## 6. Determinism & canonical round-trip

`snapshot_to_dict` / `quote_to_dict` emit stable, sorted-key dicts with
canonical `str(Decimal)` forms; `snapshot_from_dict` / `quote_from_dict` invert
them; `chain_fingerprint` is SHA-256 over the stable JSON of the canonical
dict. Row order never changes the normalized snapshot (quotes are sorted by
`(expiry, strike, side)`), and double-normalization is the identity — these are
the tests that enforce replay equality.

## 7. Non-goals / boundaries (unchanged from the phase planning)

NOT implemented in this phase: contract-selection strategy, trade-quality
scoring, option entry/exit logic, ML, OI/IV signals, automatic expiry/strike
selection, real/sandbox/live orders, scheduler activation, live-gate bypass,
algorithm promotion. No historical options dataset was synthesized. The frozen
Donchian benchmark, `ALGO READY` status, Phase 4 verification artifacts,
dataset hashes, risk limits and safety gates are untouched.

## 8. Tests

`tests/test_options_research_stack.py` (46) covers: valid chain, malformed
chain (5 structural mutations + envelope cases), missing OI (unavailable vs
zero valid vs negative invalid), missing IV/greeks, stale quote (include the
age boundary and disabled-age path), crossed/invalid bid-ask, half-depth,
no-price-at-all, duplicate contracts, inconsistent expiry, CE/PE mapping,
deterministic normalization (row order, round-trip, fingerprint stability),
provider-independence (classification across sources, static provider,
abstract boundary), no-fabrication (missing/negative spot, `without_spot`,
explicit unavailability preservation), timestamp forms (epoch s/ms, aware ISO →
naive IST, aware timestamps invalid), plus a shared-model sanity check.

Run: `.venv\Scripts\python.exe -m pytest tests/test_options_research_stack.py -q`

## 9. Safety confirmation

- Files added: the 6 package files, 1 test file, this doc.
- Files modified: 0 production/research/report/dataset/config files.
- Network: 0. Credentials: 0. Orders: 0. OOS consumed: 0.
- Commits / pushes: 0. `ALGO READY`: NO (unchanged).