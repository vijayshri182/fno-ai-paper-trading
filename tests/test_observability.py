"""Tests for the observability layer: migrations, seed reasons, idempotent
SQLite ingestion, research/paper separation, and deterministic reconstruction."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice

from fno_ai_paper_trading.discovery.catalog import build_control, build_deck
from fno_ai_paper_trading.observability.ingest import Database
from fno_ai_paper_trading.observability.migrations import apply_migrations
from fno_ai_paper_trading.observability.reconstruct import (
    _classify,
    _deterministic_trade_id,
    reconstruct_candidate,
)
from fno_ai_paper_trading.observability.schemas import (
    ALL_KNOWN_REASONS,
    FROZEN_NO_TRADE_CODES,
    MECHANICAL_CODES,
)
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig


def _nifty() -> Instrument:
    return Instrument(
        symbol="Nifty 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


def _intraday(n_days: int = 8, bars_per_day: int = 20) -> list[MarketPrice]:
    """Oscillating intraday series so crossovers fire entries, exits and EVs."""
    start = date(2025, 6, 2)
    out: list[MarketPrice] = []
    for d in range(n_days):
        day = start + timedelta(days=d)
        for i in range(bars_per_day):
            ts = datetime.combine(day, time(9, 15)) + timedelta(minutes=5 * i)
            phase = (i + d * 3) % bars_per_day
            level = 100 + ((20 - phase) * 0.05)
            out.append(MarketPrice(
                instrument=_nifty(),
                timestamp=ts,
                open=Decimal(str(level)),
                high=Decimal(str(level + 0.3)),
                low=Decimal(str(level - 0.3)),
                close=Decimal(str(level + 0.1)),
                volume=1000,
            ))
    return out


def _small_config() -> WalkForwardConfig:
    return WalkForwardConfig(
        first_date=date(2025, 6, 2),
        last_date=date(2025, 6, 9),
        protected_oos_start=None,
    )


# ---------------------------------------------------------------------------
# migrations + seeds
# ---------------------------------------------------------------------------

class TestMigrations:
    def test_apply_is_idempotent(self, tmp_path) -> None:
        db = tmp_path / "m.db"
        assert apply_migrations(db) == 3
        assert apply_migrations(db) == 0
        con = Database(db, migrate=False)
        try:
            tables = {r[0] for r in con.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"runs", "datasets", "algorithms", "decision_journal",
                    "risk_checks", "trades", "positions", "pnl_snapshots",
                    "no_trade_reasons", "artifacts", "canonical_reasons",
                    "schema_migrations"} <= tables
        finally:
            con.close()

    def test_canonical_reasons_seeded(self, tmp_path) -> None:
        db = Database(tmp_path / "s.db", migrate=True)
        try:
            rows = {r[0] for r in db.conn.execute(
                "SELECT reason_code FROM canonical_reasons")}
            assert FROZEN_NO_TRADE_CODES <= rows
            assert MECHANICAL_CODES <= rows
        finally:
            db.close()

    def test_schema_migrations_table_tracks_all(self, tmp_path) -> None:
        apply_migrations(tmp_path / "v.db")
        con = Database(tmp_path / "v.db", migrate=False)
        try:
            versions = [r[0] for r in con.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version")]
            assert versions == [1, 2, 3]
        finally:
            con.close()


# ---------------------------------------------------------------------------
# ingestion semantics
# ---------------------------------------------------------------------------

def _decision(run_id: str, algorithm_id: str, idx: int, *, final="NO TRADE", final_reason="SIDEWAYS_MARKET") -> dict:
    return {
        "run_id": run_id, "run_type": "discovery", "timestamp": "2025-06-02T09:15:00",
        "candle_index": idx, "dataset_id": "ds-1", "candidate_id": "model_0.control",
        "algorithm_id": algorithm_id, "strategy_version": "1.0",
        "configuration_version": "1.0", "config_hash": "cfg-1", "segment": "train",
        "close": 100.0, "volume": 1000, "raw_signal": "BUY", "final_decision": final,
        "final_reason": final_reason, "execution_status": "NO_ORDER",
        "decision_source": "reconstructed", "reconstruction_status": "exact",
        "source_artifact": "ds.csv", "source_artifact_hash": "h",
        "risk_checks": [{"check_name": "pre_trade_risk", "status": "PASSED"}],
    }


def _seed_parents(db: Database, run_id: str = "r1", algorithm_id: str = "model_0.control.1.0") -> None:
    db.upsert_run(run_id=run_id, run_type="discovery", status="completed",
                  config_hash="cfg-1", dataset_hash="ds-1")
    db.upsert_dataset(dataset_id="ds-1", name="x.csv", source="upstox",
                      symbol="Nifty 50", interval="5m", start_time="2025-06-02",
                      end_time="2025-06-09", bar_count=160, data_hash="ds-1")
    db.upsert_algorithm(algorithm_id=algorithm_id, candidate_id="model_0.control",
                        family="TREND_FOLLOWING", strategy_name="ma_cross",
                        strategy_version="1.0", configuration_version="1.0",
                        definition_hash="df-1")
    db.commit()


class TestIngestion:
    def test_duplicate_run_not_duplicated(self, tmp_path) -> None:
        db = Database(tmp_path / "i.db", migrate=True)
        try:
            _seed_parents(db)
            rows = [_decision("r1", "model_0.control.1.0", i) for i in range(5)]
            assert db.insert_decisions_batch(rows) == 5
            assert db.insert_decisions_batch(rows) == 0
            assert db.rowcount(
                "SELECT COUNT(*) FROM decision_journal WHERE run_id='r1'") == 5
        finally:
            db.close()

    def test_distinct_runs_keep_separate_history(self, tmp_path) -> None:
        db = Database(tmp_path / "h.db", migrate=True)
        try:
            _seed_parents(db, run_id="r1")
            _seed_parents(db, run_id="r2")
            db.insert_decisions_batch([_decision("r1", "model_0.control.1.0", 0)])
            db.insert_decisions_batch([_decision("r2", "model_0.control.1.0", 0)])
            ids = {r[0] for r in db.conn.execute(
                "SELECT run_id FROM decision_journal")}
            assert ids == {"r1", "r2"}
        finally:
            db.close()

    def test_same_run_different_config_separate(self, tmp_path) -> None:
        db = Database(tmp_path / "c.db", migrate=True)
        try:
            _seed_parents(db, run_id="r9")
            _seed_parents(db, run_id="r9", algorithm_id="model_0.control.1.p1")
            a = _decision("r9", "model_0.control.1.0", 0)
            b = _decision("r9", "model_0.control.1.p1", 0)
            assert db.insert_decisions_batch([a]) == 1
            assert db.insert_decisions_batch([b]) == 1
            assert db.rowcount(
                "SELECT COUNT(*) FROM decision_journal WHERE run_id='r9'") == 2
        finally:
            db.close()

    def test_trades_idempotent_via_unique_guard(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db", migrate=True)
        try:
            _seed_parents(db, run_id="r1")
            trade = {
                "trade_id": "TRD-abc", "run_id": "r1",
                "candidate_id": "model_0.control", "algorithm_id": "model_0.control.1.0",
                "entry_time": "2025-06-02T09:15:00", "exit_time": "2025-06-02T09:20:00",
                "side": "CALL", "entry_price": 100.0, "exit_price": 101.0,
                "quantity": 1, "commission": 0.6, "net_pnl": 0.4,
            }
            db.insert_trade(trade)
            db.insert_trade(trade)
            db.commit()
            assert db.rowcount("SELECT COUNT(*) FROM trades") == 1
        finally:
            db.close()

    def test_no_trade_reasons_only_canonical(self, tmp_path) -> None:
        db = Database(tmp_path / "n.db", migrate=True)
        try:
            _seed_parents(db)
            d = dict(_decision("r1", "model_0.control.1.0", 0,
                               final="NO TRADE", final_reason="SIDEWAYS_MARKET"))
            did = db.insert_decision(d)
            db.insert_no_trade_reasons(did, "SIDEWAYS_MARKET")
            db.insert_no_trade_reasons(did, "fast MA crossed above slow MA")
            db.insert_no_trade_reasons(did, "DAILY_LOSS_LIMIT/DAILY_LOSS_LIMIT")
            db.commit()
            codes = {r[0] for r in db.conn.execute(
                "SELECT reason_code FROM no_trade_reasons")}
            assert codes == {"SIDEWAYS_MARKET", "DAILY_LOSS_LIMIT"}
        finally:
            db.close()

    def test_research_paper_run_types_separate(self, tmp_path) -> None:
        db = Database(tmp_path / "sep.db", migrate=True)
        try:
            db.upsert_run(run_id="research-1", run_type="discovery", status="completed")
            db.upsert_run(run_id="paper-1", run_type="paper", status="active")
            types = {r[0]: r[1] for r in db.conn.execute(
                "SELECT run_id, run_type FROM runs")}
            assert types == {"research-1": "discovery", "paper-1": "paper"}
        finally:
            db.close()

    def test_input_data_flagged_not_available_for_missing_dataset(self, tmp_path) -> None:
        db = Database(tmp_path / "na.db", migrate=True)
        try:
            assert db.rowcount("SELECT COUNT(*) FROM datasets WHERE dataset_id='x'") == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# reconstruction
# ---------------------------------------------------------------------------

class TestReconstruction:
    def test_classify_maps_canonical_codes(self) -> None:
        assert _classify("SIDEWAYS_MARKET") == ("NO TRADE", "SIDEWAYS_MARKET")
        assert _classify("WARM_UP") == ("NO TRADE", "WARM_UP")
        assert _classify("HOLDING") == ("HOLD", "HOLDING")
        assert _classify("fast MA crossed above slow MA") == ("HOLD", "fast MA crossed above slow MA")
        assert _classify(None) == ("HOLD", "no signal")

    def test_deterministic_trade_id_stable(self) -> None:
        a = _deterministic_trade_id("r1", "d1_trend_ema", "2025-06-02T09:15:00", 100.0)
        b = _deterministic_trade_id("r1", "d1_trend_ema", "2025-06-02T09:15:00", 100.0)
        c = _deterministic_trade_id("r1", "d1_trend_ema", "2025-06-02T09:15:00", 101.0)
        assert a == b
        assert a != c
        assert a.startswith("TRD-")

    def test_reconstruction_is_deterministic(self) -> None:
        bars = _intraday()
        config = _small_config()
        defn = build_control()
        start, end, train_end = config.first_date, config.last_date, date(2025, 6, 5)
        a = reconstruct_candidate(
            defn, bars, config, start=start, end=end, train_end=train_end,
            run_id="r-determ", dataset_id="ds-1",
            source_artifact="ds.csv", source_artifact_hash="h",
        )
        b = reconstruct_candidate(
            defn, bars, config, start=start, end=end, train_end=train_end,
            run_id="r-determ", dataset_id="ds-1",
            source_artifact="ds.csv", source_artifact_hash="h",
        )
        assert len(a.decisions) == len(b.decisions) == len(bars)
        assert [d["timestamp"] for d in a.decisions] == [d["timestamp"] for d in b.decisions]
        assert a.trades == b.trades
        assert [d["trade_id"] for d in a.decisions] == [d["trade_id"] for d in b.decisions]

    def test_warmup_candles_recorded_as_no_trade_with_raw_diff(self) -> None:
        bars = _intraday()
        config = _small_config()
        rec = reconstruct_candidate(
            build_deck()[1], bars, config,
            start=config.first_date, end=config.last_date, train_end=date(2025, 6, 5),
            run_id="r-warm", dataset_id="ds-1",
            source_artifact="ds.csv", source_artifact_hash="h",
        )
        no_trade = [d for d in rec.decisions if d["final_decision"] == "NO TRADE"]
        nt_codes = {d["final_reason"] for d in no_trade}
        assert any(c in nt_codes for c in FROZEN_NO_TRADE_CODES | MECHANICAL_CODES)
        assert any(d["final_reason"] == "WARM_UP" for d in no_trade)
        finds_filled = [d for d in rec.decisions if d["execution_status"] == "FILLED"]
        assert finds_filled
        # raw signal (provider signal) and final decision differ on NO TRADE days
        assert any(d["raw_signal"] != d["final_decision"] for d in rec.decisions)
        every_row_has_provenance = all(
            d["decision_source"] == "reconstructed" and d["reconstruction_status"] == "exact"
            for d in rec.decisions
        )
        assert every_row_has_provenance

    def test_raw_signal_vs_final_decision_fields_present(self) -> None:
        bars = _intraday()
        config = _small_config()
        rec = reconstruct_candidate(
            build_control(), bars, config,
            start=config.first_date, end=config.last_date, train_end=date(2025, 6, 5),
            run_id="r-fields", dataset_id="ds-1",
            source_artifact="ds.csv", source_artifact_hash="h",
        )
        for d in rec.decisions:
            assert "raw_signal" in d and "final_decision" in d
            assert d["candle_index"] >= 0
            assert d["open"] is not None and d["close"] is not None
        assert all(d["segment"] in ("train", "validation") for d in rec.decisions)