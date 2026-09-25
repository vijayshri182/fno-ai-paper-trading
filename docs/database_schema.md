# Database Schema — data/paper_trading.db

SQLite, WAL-capable, opened read-only for API queries. The **SQLite file is a
regenerable read model** — it is produced deterministically by the ingestion pipeline
(`scripts/materialize_runs.py`) and is never the system of record (the immutable JSON
artifacts under `runs/` are).

## Versioned migrations

All DDL lives in `gui/backend/db/migrations/*.sql`, applied in filename order and tracked in
`schema_migrations(version, applied_at)`. Fresh DB → `apply_migrations` returns the number
applied; re-run → 0.

| Migration | Content |
|---|---|
| `0001_init.sql` | core tables + indexes |
| `0002_seed_reasons.sql` | seeds `canonical_reasons` (13 frozen NO-TRADE codes + 3 mechanical) |
| `0003_add_unique_guards.sql` | idempotency guards: `ux_trades_origin`, `ux_artifacts_run_type_path` |

## Tables

### Provenance / system
- `runs(run_id PK, run_type, status, config_hash, dataset_hash, protected_oos_start,
  source_artifact, build_version, created_at)` — one row per research discovery run.
- `datasets(dataset_id PK, name, source, symbol, interval, start_time, end_time, bar_count,
  data_hash UNIQUE, metadata)` — the frozen cleaned dataset (16,435 bars).
- `algorithms(algorithm_id PK, candidate_id, family, strategy_name, strategy_version,
  definition_hash UNIQUE, description)` — the 7 frozen deck candidates.
- `artifacts(artifact_id PK, run_id, artifact_type, path UNIQUE(run_id,artifact_type,path),
  content_hash, framework_version, metadata)` — every file written under `runs/<run>/`.

### Decision replay
- `decision_journal(decision_id PK UNIQUE(run_id, algorithm_id, candle_index) …)` —
  one row per candidate per 5-minute bar (7 × 14,110 = **98,770 rows**). Fields:
  `timestamp, segment, candle_index, ohlcv (open/high/low/close/volume as JSON), regime,
  volatility, raw_signal, final_decision, final_reason, decision_source
  (=reconstructed), reconstruction_status (exact|unavailable), execution_status,
  position_before/after (JSON), order_side, fill_price, stop_price, target_price,
  realized_pnl, unrealized_pnl, trade_id, strategy_version, features (JSON),
  source_artifact, source_artifact_hash`.
- `no_trade_reasons(no_trade_id PK, decision_id FK, reason_code, category)` — rows only for
  canonical codes from `canonical_reasons` (78,192 rows; the ~20k diff to decisions is the
  trades/HOLD candles). `UNIQUE(decision_id, reason_code)`.
- `canonical_reasons(reason_code PK, category, frozen, description)` — seeded, never drifted.

### Ingestion
- `risk_checks(risk_check_id PK, decision_id FK, run_id, candidate_id, timestamp,
  check_name, status, reason)` — pre-trade admission evaluations (7,329 rows).
- `trades(trade_id PK, run_id, candidate_id, side, entry_time, exit_time, entry_price,
  exit_price, quantity, commission, net_pnl, exit_reason, entry_decision_id,
  exit_decision_id, UNIQUE(run_id, candidate_id, entry_time, exit_time))` — 3,620
  reconstructed round trips. `trade_id` = deterministic `TRD-` + sha256 of
  `run|candidate|entry_time|entry_price` first 16 hex.
- `positions(…)` and `pnl_snapshots(…)` — reserved for future paper sessions (0 rows now).

## Idempotency guarantees
- `INSERT OR IGNORE` + the unique keys above ⇒ materializing a run a second time changes
  nothing (verified byte-identical counts on second pass).
- Trade PKs are content-addressed, not `new_id()`-based.
- Note: enabling the trade unique guard made the pre-existing DB inconsistent (old random
  trade ids), so the DB was regenerated once from scratch; keep `ux_trades_origin` on.

## Queries used by the API
`gui/backend/src/db/repo.js` owns every query: health counts, runs list/detail, decisions
paging + detail, no-trade breakdown (canonical only), trades + replay window, risk summary,
P&L research/paper split, provenance (datasets/runs/artifacts), system-health, and the
win-learning analytics.