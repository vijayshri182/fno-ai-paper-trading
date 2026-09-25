# Run Storage & Immutable Artifacts

## Layout

```
runs/
  discovery/
    cycle_20260914_103035/            ← run_id == cycle_20260914_103035
      run.manifest.json               run metadata, config_hash, dataset_hash, oos bounds
      config.json                     full walk-forward config (hardened snapshot)
      dataset.json                    dataset descriptor + provenance
      decisions/                      per-candidate decision journals (write-once)
        model_0.control.json … d6_structure.json
      trades/                         per-candidate round trips
      risk/                           per-candidate pre-trade risk evaluations
      framework.txt                   framework + fingerprint version
```

Authoritative inputs that seed storage (never modified):
- `reports/discovery_cycle_cycle_20260914_103035.json`
- `datasets/nifty50_5m_discovery_cycle_20260914_103035.csv` (16,435 bars)
- `reports/candidates/*.definition.json`

## Rules

1. **Write-once.** Artifacts are created once; re-materializing refuses to overwrite without
   an explicit `--overwrite` flag.
2. **Content-addressed where possible.** `data_hash`, `definition_hash`, `config_hash`,
   `content_hash` pin each artifact so any journal row can be traced to its exact inputs.
3. **Deterministic journal.** Decision, trade and risk rows are re-derived from the full
   dataset + frozen definitions so the read model can be rebuilt byte-for-byte.

## Ingestion (Python)

```
python scripts/materialize_runs.py            # build/refresh data/paper_trading.db
python scripts/materialize_runs.py --overwrite # allow artifact rewrites
```

Pipeline (in `src/fno_ai_paper_trading/observability/`):
- `hashes.py` — sha256 helpers.
- `schemas.py` — canonical NO-TRADE codes, decision kinds, trade enum, journal contract.
- `migrations.py` — the same versioned migration set later used by the Node backend
  (`gui/backend/db/migrations/*.sql`); tracks `schema_migrations`.
- `run_store.py` — artifact inventory, run/dataset/algorithm registration.
- `ingest.py` — decision/trade/risk inserts with uniques + canonical-only no-trade codes.
- `reconstruct.py` — exact re-derivation of decisions (raw + final) and round trips
  (entry/exit pairing, both trade ids deterministic).

`materialize_runs.py` verifies reconstruction exactness against the report's scorecards and
records `config_exact=True`/`exact=True`.

## Expected counts (single run, all 7 candidates)

| table | rows |
|---|---|
| runs | 1 |
| datasets | 1 |
| algorithms | 7 |
| decision_journal | 98,770 (7 × 14,110) |
| risk_checks | 7,329 |
| no_trade_reasons | 78,192 |
| trades | 3,620 |
| positions / pnl_snapshots | 0 (no paper sessions yet) |
| artifacts | 6 |

Rerunning the pipeline is a no-op: same run id, same counts, same checksums.