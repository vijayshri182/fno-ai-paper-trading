"""Idempotent SQLite ingestion for the observability read model.

The :class:`Database` class opens ``data/paper_trading.db`` and provides
upsert helpers for runs, datasets, algorithms, decisions, risk checks,
no-trade reasons, trades, and artifacts. All inserts use
``INSERT OR IGNORE`` backed by unique constraints so re-ingestion is safe
and idempotent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fno_ai_paper_trading.observability.hashes import canonical_json
from fno_ai_paper_trading.observability.migrations import apply_migrations

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "paper_trading.db"

_FROZEN_NO_TRADE_CODES = frozenset({
    "SIDEWAYS_MARKET", "WEAK_TREND", "LOW_VOLATILITY", "EXTREME_VOLATILITY",
    "NO_PULLBACK", "NO_MOMENTUM", "NO_BREAKOUT", "POOR_RISK_REWARD",
    "INSUFFICIENT_EXPECTED_EDGE", "HIGH_COST", "TIME_CUTOFF", "RISK_LIMIT", "DAILY_LOSS_LIMIT",
})

_MECHANICAL_CODES = frozenset({"WARM_UP", "HOLDING", "TIME_NOT_OPEN"})

_ALL_KNOWN_REASONS = _FROZEN_NO_TRADE_CODES | _MECHANICAL_CODES

_CATEGORY_MAP = {
    "SIDEWAYS_MARKET": "regime", "WEAK_TREND": "regime",
    "LOW_VOLATILITY": "volatility", "EXTREME_VOLATILITY": "volatility",
    "NO_PULLBACK": "signal", "NO_MOMENTUM": "momentum", "NO_BREAKOUT": "signal",
    "POOR_RISK_REWARD": "risk", "INSUFFICIENT_EXPECTED_EDGE": "risk",
    "HIGH_COST": "cost", "TIME_CUTOFF": "time", "RISK_LIMIT": "risk",
    "DAILY_LOSS_LIMIT": "risk",
    "WARM_UP": "mechanical", "HOLDING": "mechanical", "TIME_NOT_OPEN": "time",
}


def _category(code: str) -> str:
    return _CATEGORY_MAP.get(code, "other")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin wrapper over the SQLite read-model used by the Node GUI."""

    def __init__(self, db_path: str | Path | None = None, *, migrate: bool = True) -> None:
        self.db_path = str(db_path or _DEFAULT_DB)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if migrate:
            apply_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def rowcount(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # datasets
    # ------------------------------------------------------------------

    def upsert_dataset(
        self,
        *,
        dataset_id: str,
        name: str,
        source: str,
        symbol: str,
        interval: str,
        start_time: str | None,
        end_time: str | None,
        bar_count: int,
        data_hash: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO datasets
               (dataset_id, name, source, symbol, interval, start_time, end_time,
                bar_count, data_hash, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dataset_id, name, source, symbol, interval,
                start_time, end_time, bar_count, data_hash,
                json.dumps(metadata or {}, default=str), _now_iso(),
            ),
        )

    # ------------------------------------------------------------------
    # algorithms
    # ------------------------------------------------------------------

    def upsert_algorithm(
        self,
        *,
        algorithm_id: str,
        candidate_id: str,
        family: str,
        strategy_name: str | None = None,
        strategy_version: str | None = None,
        configuration_version: str | None = None,
        definition_hash: str,
        description: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO algorithms
               (algorithm_id, candidate_id, family, strategy_name, strategy_version,
                configuration_version, definition_hash, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                algorithm_id, candidate_id, family, strategy_name,
                strategy_version, configuration_version, definition_hash, description,
            ),
        )

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------

    def upsert_run(
        self,
        *,
        run_id: str,
        run_type: str,
        status: str = "completed",
        description: str | None = None,
        config_hash: str | None = None,
        dataset_hash: str | None = None,
        source_artifact: str | None = None,
        source_artifact_hash: str | None = None,
        protected_oos_start: str | None = None,
        schema_version: int = 1,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO runs
               (run_id, run_type, created_at, started_at, completed_at, status,
                description, config_hash, dataset_hash, source_artifact,
                source_artifact_hash, protected_oos_start, schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, run_type, _now_iso(), started_at, completed_at, status,
                description, config_hash, dataset_hash, source_artifact,
                source_artifact_hash, protected_oos_start, schema_version,
            ),
        )

    # ------------------------------------------------------------------
    # decision journal (idempotent via UNIQUE(run_id, algorithm_id, candle_index))
    # ------------------------------------------------------------------

    def insert_decision(self, d: dict[str, Any]) -> int | None:
        """Insert one decision row; returns decision_id or None if skipped."""
        sql = """INSERT OR IGNORE INTO decision_journal
                 (run_id, run_type, timestamp, candle_index, dataset_id,
                  candidate_id, algorithm_id, strategy_version, configuration_version,
                  config_hash, segment, open, high, low, close, volume, regime,
                  volatility, features_json, raw_signal, final_decision, final_reason,
                  position_before_json, position_after_json, risk_checks_json,
                  execution_status, trade_id, entry_price, exit_price, stop_price,
                  target_price, realized_pnl, unrealized_pnl, decision_source,
                  reconstruction_status, source_artifact, source_artifact_hash, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        params = (
            d.get("run_id"), d.get("run_type"), d.get("timestamp"), d.get("candle_index"),
            d.get("dataset_id"), d.get("candidate_id"), d.get("algorithm_id"),
            d.get("strategy_version"), d.get("configuration_version"), d.get("config_hash"),
            d.get("segment"), d.get("open"), d.get("high"), d.get("low"), d.get("close"),
            d.get("volume"), d.get("regime"), d.get("volatility"),
            json.dumps(d.get("features"), default=str) if d.get("features") else None,
            d.get("raw_signal"), d.get("final_decision"), d.get("final_reason"),
            json.dumps(d.get("position_before"), default=str) if d.get("position_before") else None,
            json.dumps(d.get("position_after"), default=str) if d.get("position_after") else None,
            json.dumps(d.get("risk_checks"), default=str) if d.get("risk_checks") else None,
            d.get("execution_status"), d.get("trade_id"),
            d.get("entry_price"), d.get("exit_price"),
            d.get("stop_price"), d.get("target_price"),
            d.get("realized_pnl"), d.get("unrealized_pnl"),
            d.get("decision_source"), d.get("reconstruction_status"),
            d.get("source_artifact"), d.get("source_artifact_hash"), _now_iso(),
        )
        cur = self.conn.execute(sql, params)
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

    def insert_decisions_batch(self, rows: list[dict[str, Any]]) -> int:
        inserted = 0
        for row in rows:
            did = self.insert_decision(row)
            if did is not None:
                inserted += 1
        return inserted

    def existing_decision_id(
        self, run_id: str, algorithm_id: str, candle_index: int
    ) -> int | None:
        cur = self.conn.execute(
            """SELECT decision_id FROM decision_journal
               WHERE run_id = ? AND algorithm_id = ? AND candle_index = ?""",
            (run_id, algorithm_id, candle_index),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None

    # ------------------------------------------------------------------
    # risk checks
    # ------------------------------------------------------------------

    def insert_risk_check(self, decision_id: int, rc: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO risk_checks
               (decision_id, check_name, actual_value, required_value, status, reason, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id, rc.get("check_name"), rc.get("actual_value"),
                rc.get("required_value"), rc.get("status"), rc.get("reason"),
                json.dumps(rc.get("metadata"), default=str) if rc.get("metadata") else None,
            ),
        )

    # ------------------------------------------------------------------
    # no trade reasons
    # ------------------------------------------------------------------

    def insert_no_trade_reasons(self, decision_id: int, reason: str | None) -> None:
        """Record only canonical frozen NO-TRADE codes; filler reasons are skipped.

        The same code set is used by the GUI's NO TRADE Analysis screen so counts
        stay honest (a filled BUY/SELL reason is never a ``no_trade_reason``).
        """
        if not reason:
            return
        codes = [c.strip() for c in reason.split("/") if c.strip()]
        for code in codes:
            if code not in _ALL_KNOWN_REASONS:
                continue
            self.conn.execute(
                """INSERT OR IGNORE INTO no_trade_reasons
                   (decision_id, reason_code, description, category) VALUES (?, ?, ?, ?)""",
                (decision_id, code, reason, _category(code)),
            )

    # ------------------------------------------------------------------
    # trades
    # ------------------------------------------------------------------

    def insert_trade(self, t: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO trades
               (trade_id, session_id, run_id, strategy_id, candidate_id, algorithm_id,
                entry_time, exit_time, side, entry_price, exit_price, quantity,
                commission, slippage, gross_pnl, net_pnl, exit_reason, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.get("trade_id"), t.get("session_id"), t.get("run_id"),
                t.get("strategy_id"), t.get("candidate_id"), t.get("algorithm_id"),
                t.get("entry_time"), t.get("exit_time"), t.get("side"),
                t.get("entry_price"), t.get("exit_price"), t.get("quantity"),
                t.get("commission"), t.get("slippage"),
                t.get("gross_pnl"), t.get("net_pnl"), t.get("exit_reason"),
                json.dumps(t.get("metadata"), default=str) if t.get("metadata") else None,
                _now_iso(),
            ),
        )

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------

    def insert_artifact(self, *, run_id: str, artifact_type: str, path: str, content_hash: str) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO artifacts
               (run_id, artifact_type, path, content_hash, created_at) VALUES (?, ?, ?, ?, ?)""",
            (run_id, artifact_type, path, content_hash, _now_iso()),
        )

    # ------------------------------------------------------------------
    # positions (paper only)
    # ------------------------------------------------------------------

    def insert_position(self, p: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO positions
               (session_id, timestamp, symbol, side, quantity, entry_price,
                current_price, stop_price, target_price, unrealized_pnl, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                p.get("session_id"), p.get("timestamp"), p.get("symbol"),
                p.get("side"), p.get("quantity"), p.get("entry_price"),
                p.get("current_price"), p.get("stop_price"), p.get("target_price"),
                p.get("unrealized_pnl"), p.get("status"),
            ),
        )

    def insert_pnl_snapshot(self, s: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO pnl_snapshots
               (session_id, timestamp, cash, equity, realized_pnl, realized_pnl_today,
                unrealized_pnl, daily_loss)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                s.get("session_id"), s.get("timestamp"), s.get("cash"), s.get("equity"),
                s.get("realized_pnl"), s.get("realized_pnl_today"),
                s.get("unrealized_pnl"), s.get("daily_loss"),
            ),
        )

    # ------------------------------------------------------------------
    # counts for summary
    # ------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        tables = [
            "runs", "datasets", "algorithms", "decision_journal", "risk_checks",
            "no_trade_reasons", "trades", "positions", "pnl_snapshots", "artifacts",
        ]
        result: dict[str, int] = {}
        for t in tables:
            result[t] = self.rowcount(f"SELECT COUNT(*) FROM {t}")
        return result
