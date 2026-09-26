"""Materialize immutable runs/ evidence + SQLite read model from a cycle report.

Builds the decision journal by *reconstructing* the exact discovery replay
(same dataset, definition, config, engine and per-day fresh-portfolio
semantics) then writing ``runs/discovery/<run_id>/`` artifacts and syncing them
idempotently into ``data/paper_trading.db``.

Run:
    .venv\\Scripts\\python.exe scripts/materialize_runs.py --report reports\\discovery_cycle_cycle_20260914_103035.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints

# allow running from a naked checkout without installing the package
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from fno_ai_paper_trading.data import load_dataset  # type: ignore[import-not-found]
from fno_ai_paper_trading.discovery.catalog import CandidateDefinition  # noqa: E402
from fno_ai_paper_trading.observability.ingest import Database  # noqa: E402
from fno_ai_paper_trading.observability.migrations import apply_migrations  # noqa: E402
from fno_ai_paper_trading.observability.reconstruct import reconstruct_candidate  # noqa: E402
from fno_ai_paper_trading.observability.run_store import (  # noqa: E402
    create_run_base,
    file_hashes,
    write_run_candidates,
    write_run_config,
    write_run_decisions,
    write_run_manifest,
    write_run_provenance,
    write_run_results,
)
from fno_ai_paper_trading.walkforward.config import (  # noqa: E402
    FRAMEWORK_VERSION,
    WalkForwardConfig,
)

_DECIMAL_FIELDS = {
    "initial_capital", "commission_rate", "commission_fixed", "slippage_rate",
    "max_order_notional", "max_daily_loss", "stop_loss_pct", "min_profit_factor",
    "max_drawdown_pct", "max_tail_loss_pct", "min_net_pnl_per_cost",
    "max_trades_per_day", "regime_degradation_tolerance",
}
_DATE_FIELDS = {"first_date", "last_date", "protected_oos_start"}
_INT_FIELDS = {
    "quantity", "max_position_quantity", "max_research_days", "min_evidence_days",
    "research_cadence_days", "research_window_days", "nightly_review_window_days",
    "min_hypothesis_regime_trades", "max_challengers_per_round",
    "max_challengers_total", "validation_window_days", "promotion_cadence_days",
    "min_validation_trades", "min_regime_trades",
}


def build_config(payload: dict[str, Any]) -> WalkForwardConfig:
    kwargs: dict[str, Any] = {}
    hints = get_type_hints(WalkForwardConfig)
    for name, value in payload.items():
        if value is None or name not in hints:
            kwargs[name] = value
            continue
        hint = str(hints[name])
        if name in _DATE_FIELDS:
            kwargs[name] = date.fromisoformat(str(value)) if value else None
        elif name in _DECIMAL_FIELDS:
            kwargs[name] = Decimal(format(value, "f")) if isinstance(value, float) else Decimal(str(value))
        elif name in _INT_FIELDS:
            kwargs[name] = int(value)
        elif "bool" in hint:
            kwargs[name] = bool(value)
        else:
            kwargs[name] = value
    return WalkForwardConfig(**kwargs)


def load_definition(definitions_dir: Path, candidate_id: str, version: str) -> CandidateDefinition:
    path = definitions_dir / f"{candidate_id}.{version}.definition.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CandidateDefinition(**payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=_ROOT / "reports" / "discovery_cycle_cycle_20260914_103035.json")
    parser.add_argument("--definitions-dir", default=_ROOT / "reports" / "candidates")
    parser.add_argument("--runs-dir", default=_ROOT / "runs")
    parser.add_argument("--db", default=None)
    parser.add_argument("--migrations-dir", default=_ROOT / "gui" / "backend" / "db" / "migrations")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing run artifacts (evidence is write-once by default)")
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    run_id = report.get("run_id") or Path(str(args.report)).stem
    if not run_id:
        raise SystemExit("report has no run_id and filename yields none")

    config = build_config(report["config"])
    config_exact = config.config_hash == report.get("config_hash")
    if not config_exact:
        print(f"[warn] config_hash mismatch: report={report.get('config_hash')} rebuilt={config.config_hash}")

    stored = load_dataset(report["provenance"]["dataset_path"])
    dataset_id = stored.data_hash
    bars = list(stored.bars)

    watch = report["watch_window"]
    start = date.fromisoformat(watch["train_start"])
    end = date.fromisoformat(watch["validation_end"])
    train_end = date.fromisoformat(watch["train_end"])

    base = create_run_base(Path(args.runs_dir), "discovery", run_id)
    overwrite = args.overwrite

    candidates_out: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    all_decisions: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    defn_by_id: dict[str, CandidateDefinition] = {}
    exact_flags: dict[str, bool] = {}

    for sc in report["scorecards"]:
        cid = sc["candidate_id"]
        version = str(sc.get("version", "1.0"))
        defn = load_definition(Path(args.definitions_dir), cid, version)
        defn_by_id[defn.candidate_id] = defn
        rec = reconstruct_candidate(
            defn, bars, config,
            start=start, end=end, train_end=train_end,
            run_id=run_id, dataset_id=dataset_id,
            source_artifact=str(report["provenance"]["dataset_path"]),
            source_artifact_hash=dataset_id,
        )
        scorecard_net = Decimal(str(sc["net_pnl"]))
        net_ok = rec.train_net == scorecard_net
        exact = config_exact and net_ok
        exact_flags[cid] = exact
        print(f"[{cid}] net train={rec.train_net} scorecard={scorecard_net} exact={exact} "
              f"decisions={len(rec.decisions)} trades={len(rec.trades)}")

        for d in rec.decisions:
            d["reconstruction_status"] = "exact" if exact else "unavailable"
            all_decisions.append(d)
        all_trades.extend(rec.trades)
        candidates_out.append({
            **defn.to_dict(),
            "scorecard": sc,
            "reconstruction": {
                "exact": exact,
                "config_hash_match": config_exact,
                "train_net": format(rec.train_net, "f"),
                "scorecard_train_net": format(scorecard_net, "f"),
                "evaluated": len(rec.decisions),
                "trades_rebuilt": len(rec.trades),
            },
            "day_nets": rec.day_nets,
        })
        results[cid] = {
            "train_net": format(rec.train_net, "f"),
            "scorecard_net": format(scorecard_net, "f"),
            "exact_match": net_ok,
            "decision_rows": len(rec.decisions),
            "trades": len(rec.trades),
            "segment": {"train": watch["train_start"], "train_end": watch["train_end"],
                        "validation_start": watch["validation_start"], "validation_end": watch["validation_end"]},
        }

    exact_run = all(exact_flags.get(sc["candidate_id"], False) for sc in report["scorecards"])

    manifest = {
        "run_id": run_id,
        "run_type": "discovery",
        "framework_version": FRAMEWORK_VERSION,
        "schema_version": 1,
        "status": "completed",
        "created_at": report.get("timestamp"),
        "dataset": {
            "dataset_id": dataset_id,
            "path": str(args.report),
            "bars": len(bars),
            "bar_count_provenance": report["provenance"]["candle_count"],
        },
        "candidates": [sc["candidate_id"] for sc in report["scorecards"]],
        "reconstruction": {
            "exact": exact_run,
            "config_hash": config.config_hash,
            "config_hash_matches_report": config_exact,
            "per_candidate": exact_flags,
        },
    }

    write_run_manifest(base, manifest, overwrite=overwrite)
    write_run_config(base, {"config": report["config"], "config_hash": config.config_hash,
                            "config_hash_matches_report": config_exact, "walkforward_version": FRAMEWORK_VERSION},
                     overwrite=overwrite)
    write_run_candidates(base, candidates_out, overwrite=overwrite)
    write_run_decisions(base, all_decisions, overwrite=overwrite)
    results["_summary"] = {
        "candidates": list(results),
        "exact": exact_run,
        "protected_oos_start": watch["protected_oos_start"],
        "algo_ready": report.get("algo_ready"),
        "best": report.get("current_best"),
        "next_action": report.get("next_action"),
    }
    write_run_results(base, results, overwrite=overwrite)
    provenance = {
        **report.get("provenance", {}),
        "source_report": str(args.report),
        "source_report_hash": None,  # filled below from artifact store
        "derived_at": config.config_hash,
    }
    write_run_provenance(base, provenance, overwrite=overwrite)
    hashes = file_hashes(base)

    db_path = Path(args.db) if args.db else _ROOT / "data" / "paper_trading.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    applied = apply_migrations(db_path, Path(args.migrations_dir))
    db = Database(db_path, migrate=False)
    try:
        ds = stored.metadata
        db.upsert_dataset(
            dataset_id=dataset_id,
            name=Path(report["provenance"]["dataset_path"]).name,
            source=ds.get("provider", "upstox"),
            symbol=ds.get("instrument", {}).get("symbol", "Nifty 50"),
            interval=ds.get("interval", "5m"),
            start_time=ds.get("start_date"),
            end_time=ds.get("end_date"),
            bar_count=len(bars),
            data_hash=dataset_id,
            metadata={"reported_data_hash": report["provenance"]["data_hash"], "instrument": ds.get("instrument")},
        )
        for defn in defn_by_id.values():
            db.upsert_algorithm(
                algorithm_id=f"{defn.candidate_id}.{defn.version}",
                candidate_id=defn.candidate_id,
                family=defn.family,
                strategy_name=defn.provider_path.split(".")[-1],
                strategy_version=defn.version,
                configuration_version=defn.version,
                definition_hash=defn.definition_hash,
                description=defn.description,
            )
        db.upsert_run(
            run_id=run_id,
            run_type="discovery",
            status="completed",
            description=f"discovery cycle {run_id} (reconstructed decision journal)",
            config_hash=config.config_hash,
            dataset_hash=dataset_id,
            source_artifact=str(args.report),
            protected_oos_start=watch["protected_oos_start"],
            schema_version=1,
            started_at=report.get("timestamp"),
            completed_at=report.get("timestamp"),
        )
        ids = db.insert_decisions_batch(all_decisions)
        for d in all_decisions:
            row_id = db.existing_decision_id(d["run_id"], d["algorithm_id"], d["candle_index"])
            if row_id:
                for rc in (d.get("risk_checks") or []):
                    db.insert_risk_check(row_id, rc)
                db.insert_no_trade_reasons(row_id, d.get("final_reason"))
        for t in all_trades:
            db.insert_trade({"strategy_id": None, **t})
        for name, digest in hashes.items():
            db.insert_artifact(run_id=run_id, artifact_type=name.split(".")[0], path=str(base / name), content_hash=digest)
        db.commit()
        print("counts:", db.counts())
    finally:
        db.close()

    print(f"run_id={run_id} exact={exact_run} config_exact={config_exact} applied_migrations={applied}")
    print(f"artifacts -> {base}")
    print(f"sqlite    -> {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())