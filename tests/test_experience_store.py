"""Tests for WS 7.9 durable experience store (evidence-only infrastructure).

Covers the domain records, builders, query/index layer, deterministic
serialization, and the append-only JSONL store with idempotency, strict
recovery, and no-look-ahead guarantees.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fno_ai_paper_trading.experience.enums import (
    AdvisoryUsage,
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
    OutcomeKind,
)
from fno_ai_paper_trading.experience.queries import ExperienceQuery, apply_query, count_by
from fno_ai_paper_trading.experience.records import (
    EXPERIENCE_SCHEMA_VERSION,
    AdvisoryEvidence,
    DecisionContext,
    ExperienceRecord,
    TradeOutcome,
    decision_identity,
    make_experience_id,
)
from fno_ai_paper_trading.experience import builders
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.ai.decision import AIDecision
from fno_ai_paper_trading.persistence.experience_store import (
    ExperienceStore,
    experience_from_dict,
    experience_to_dict,
)


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _ts(hour: int = 10, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def _decision(**overrides) -> DecisionContext:
    payload = dict(
        instrument=_future(),
        decision_timestamp=_ts(),
        timeframe="5m",
        signal=Signal.BUY,
        confidence="0.82",
        strategy_name="ma_cross",
        strategy_version="1.0.0",
        data_reference="upstox/NIFTY50/5m/2026-09-02",
        features={"ma_fast": Decimal("24120.5"), "ma_slow": Decimal("24095.25"), "bars": 370},
        feature_version="1.2",
        decision_status=DecisionStatus.EXECUTED,
        data_quality=DataQualityStatus.VALIDATED,
        regime_label="up_normal",
    )
    payload.update(overrides)
    return builders.build_decision(**payload)


def _outcome(**overrides) -> TradeOutcome:
    payload = dict(
        trade_id="t-1",
        side=OrderSide.BUY,
        entry_price="24100",
        entry_timestamp=_ts(9, 30),
        exit_price="24140",
        exit_timestamp=_ts(10, 30),
        quantity=75,
        realized_pnl="3000",
        total_costs="40",
        stop_loss_price="24020",
    )
    payload.update(overrides)
    return builders.build_outcome(**payload)


def _advisory(usage: AdvisoryUsage = AdvisoryUsage.ACCEPTED) -> AdvisoryEvidence:
    return builders.build_advisory(
        AIDecision(
            action=Signal.BUY,
            confidence=Decimal("0.82"),
            rationale="MA cross aligned with trend",
            features_considered={"ma_fast": Decimal("24120.5")},
            market_regime="up_normal",
            model_name="regime-ma",
            model_version="0.3.1",
            timestamp=_ts(),
            data_reference="upstox/NIFTY50/5m/2026-09-02",
        ),
        usage=usage,
    )


def _record(**overrides) -> ExperienceRecord:
    payload = dict(
        decision=_decision(),
        source=ExperienceSourceType.PAPER_SESSION,
        recorded_at=_ts(11, 0),
        outcome=_outcome(),
        advisory=_advisory(),
        source_detail="session-2026-09-02",
    )
    payload.update(overrides)
    return builders.build_record(**payload)


class TestDecisionContext:
    def test_identity_is_deterministic(self):
        a = _decision()
        b = _decision(confidence="0.90")
        assert decision_identity(a) == decision_identity(
            builders.build_decision(
                instrument=_future(),
                decision_timestamp=_ts(),
                timeframe="5m",
                signal=Signal.BUY,
                confidence="0.82",
                strategy_name="ma_cross",
                strategy_version="1.0.0",
                data_reference="upstox/NIFTY50/5m/2026-09-02",
                features={"ma_fast": Decimal("24120.5"), "ma_slow": Decimal("24095.25"), "bars": 370},
                feature_version="1.2",
                regime_label="up_normal",
            )
        )
        assert a != b

    def test_identity_changes_when_regime_changes(self):
        assert decision_identity(_decision(regime_label="down")) != decision_identity(
            _decision(regime_label="up_normal")
        )

    def test_no_look_ahead_outcome_fields_absent(self):
        payload = _decision().decision_payload()
        for forbidden in ("outcome", "realized_pnl", "exit_price", "win", "result", "is_win"):
            assert forbidden not in payload

    def test_rejects_float_and_bool_features(self):
        with pytest.raises(TypeError):
            _decision(features={"ma_fast": 1.5})
        with pytest.raises(TypeError):
            _decision(features={"flag": True})

    def test_accepts_finite_decimal_int_and_str_features(self):
        decision = _decision(features={"d": Decimal("1.25"), "i": 7, "s": "ok"})
        assert dict(decision.features) == {"d": Decimal("1.25"), "i": 7, "s": "ok"}

    def test_immutable_frozen(self):
        decision = _decision()
        with pytest.raises(Exception):
            decision.decision_timestamp = _ts(12)

    def test_requires_data_reference(self):
        with pytest.raises(ValueError):
            _decision(data_reference="   ")


class TestTradeOutcome:
    @pytest.mark.parametrize(
        ("realized_pnl", "expected"),
        [
            ("3000", OutcomeKind.WIN),
            ("-1500", OutcomeKind.LOSS),
            ("0", OutcomeKind.BREAKEVEN),
            ("0.00", OutcomeKind.BREAKEVEN),
        ],
    )
    def test_outcome_derived_from_pnl(self, realized_pnl, expected):
        assert _outcome(realized_pnl=realized_pnl).outcome is expected

    def test_explicit_outcome_wins_over_derivation(self):
        outcome = _outcome(realized_pnl="3000", outcome=OutcomeKind.LOSS)
        assert outcome.outcome is OutcomeKind.LOSS

    def test_holding_seconds(self):
        outcome = _outcome(entry_timestamp=_ts(9, 30), exit_timestamp=_ts(9, 40))
        assert outcome.holding_seconds == 600
        same_day_zero = _outcome(entry_timestamp=_ts(9, 30), exit_timestamp=_ts(9, 30))
        assert same_day_zero.holding_seconds == 0

    def test_stop_loss_optional(self):
        assert _outcome(stop_loss_price=None).stop_loss_price is None
        assert _outcome(stop_loss_price="24020").stop_loss_price == Decimal("24020")

    def test_rejects_negative_or_zero_quantity(self):
        with pytest.raises(ValueError):
            _outcome(quantity=0)
        with pytest.raises(ValueError):
            _outcome(quantity=-1)

    def test_rejects_non_finite_pnl(self):
        with pytest.raises(ValueError):
            _outcome(realized_pnl="NaN")


class TestAdvisoryEvidence:
    def test_advisory_only_is_hard_positive(self):
        evidence = _advisory()
        assert evidence.advisory_only is True
        assert evidence.present is True

    def test_advisory_only_cannot_be_disabled(self):
        with pytest.raises(ValueError):
            AdvisoryEvidence(present=True, usage=AdvisoryUsage.ACCEPTED, advisory_only=False)

    def test_none_decision_gives_present_false(self):
        evidence = builders.build_advisory(None)
        assert evidence.present is False
        assert evidence.usage is AdvisoryUsage.NONE
        assert evidence.advisory_only is True


class TestExperienceRecord:
    def test_status_complete_when_outcome_present(self):
        assert _record().status == "complete"

    def test_status_pending_outcome_when_executed_without_outcome(self):
        record = _record(outcome=None)
        assert record.status == "pending_outcome"

    def test_status_no_trade_when_not_executed(self):
        record = _record(
            outcome=None,
            decision=_decision(decision_status=DecisionStatus.SKIPPED),
        )
        assert record.status == "no_trade"

    def test_with_outcome_returns_new_record_and_keeps_original_unchanged(self):
        pending = _record(outcome=None)
        filled = pending.with_outcome(_outcome(), recorded_at=_ts(11, 5))
        assert pending.outcome is None
        assert pending.status == "pending_outcome"
        assert filled.outcome is not None
        assert filled.status == "complete"
        assert filled.experience_id == pending.experience_id

    def test_rejects_unsupported_schema_version(self):
        with pytest.raises(ValueError):
            _record(schema_version="999")

    def test_id_is_deterministic_and_prefixed(self):
        record_a = _record()
        record_b = _record(recorded_at=_ts(12))
        assert record_a.experience_id.startswith("exp_")
        assert len(record_a.experience_id) == 4 + 16
        assert record_b.experience_id == record_a.experience_id

    def test_id_differs_for_distinct_occurrence(self):
        record_a = _record()
        record_b = _record(outcome=None)
        record_c = builders.build_record(
            decision=_decision(),
            source=ExperienceSourceType.PAPER_SESSION,
            recorded_at=_ts(11, 0),
            outcome=None,
            advisory=_advisory(),
            occurrence=1,
        )
        assert record_c.experience_id != record_a.experience_id
        # same decision identity + different occurrence still deterministic
        assert record_b.experience_id == record_a.experience_id


class TestBuilders:
    def test_round_trip_through_serialization(self):
        record = _record()
        rebuilt = experience_from_dict(experience_to_dict(record))
        assert rebuilt == record
        assert rebuilt.experience_id == record.experience_id

    def test_serialization_is_deterministic(self):
        assert json.dumps(experience_to_dict(_record()), sort_keys=True) == json.dumps(
            experience_to_dict(_record()), sort_keys=True
        )
        assert experience_to_dict(_record()) == experience_to_dict(_record())

    def test_serialization_uses_strings_for_money(self):
        payload = experience_to_dict(_record())
        assert payload["decision"]["confidence"] == "0.82"
        assert payload["outcome"]["realized_pnl"] == "3000"
        assert payload["outcome"]["quantity"] == 75

    def test_outcome_from_round_trip(self):
        entry = Trade(
            trade_id="t-open",
            instrument=_future(),
            side=OrderSide.BUY,
            quantity=75,
            price=Decimal("24100"),
            commission=Decimal("20"),
            executed_at=_ts(9, 30),
        )
        exit_ = Trade(
            trade_id="t-close",
            instrument=_future(),
            side=OrderSide.SELL,
            quantity=75,
            price=Decimal("24140"),
            commission=Decimal("20"),
            executed_at=_ts(10, 30),
            realized_pnl=Decimal("3000"),
        )
        outcome = builders.build_outcome_from_round_trip(entry, exit_)
        assert outcome.entry_price == Decimal("24100")
        assert outcome.exit_price == Decimal("24140")
        assert outcome.realized_pnl == Decimal("3000")
        assert outcome.total_costs == Decimal("40")
        assert outcome.quantity == 75
        assert outcome.trade_id == "t-close"

    def test_round_trip_rejects_mismatched_instruments(self):
        other = Instrument(
            symbol="BANKNIFTY1",
            instrument_type=InstrumentType.FUTURE,
            underlying_symbol="BANKNIFTY",
        )
        entry = Trade(
            trade_id="a",
            instrument=_future(),
            side=OrderSide.BUY,
            quantity=75,
            price=Decimal("100"),
            commission=Decimal("0"),
            executed_at=_ts(9, 30),
        )
        exit_ = Trade(
            trade_id="b",
            instrument=other,
            side=OrderSide.SELL,
            quantity=75,
            price=Decimal("110"),
            commission=Decimal("0"),
            executed_at=_ts(10, 30),
        )
        with pytest.raises(ValueError):
            builders.build_outcome_from_round_trip(entry, exit_)


class TestQuery:
    def _records(self):
        win = _record()
        loss = _record(
            decision=_decision(
                decision_timestamp=_ts(10, 5),
                signal=Signal.SELL,
                regime_label="down",
                confidence="0.61",
            ),
            outcome=_outcome(realized_pnl="-1800", exit_timestamp=_ts(10, 45)),
        )
        skip = _record(
            decision=_decision(
                decision_timestamp=_ts(10, 20),
                signal=Signal.HOLD,
                decision_status=DecisionStatus.SKIPPED,
                confidence="0.40",
            ),
            outcome=None,
            advisory=builders.build_advisory(None),
        )
        return [win, loss, skip]

    def test_filter_by_outcome(self):
        records = self._records()
        assert [r.experience_id for r in apply_query(records, ExperienceQuery(outcome=OutcomeKind.WIN))] == [
            records[0].experience_id
        ]
        assert apply_query(records, ExperienceQuery(outcome=OutcomeKind.LOSS)) == [records[1]]
        assert apply_query(records, ExperienceQuery(outcome=OutcomeKind.BREAKEVEN)) == []

    def test_filter_by_regime_and_strategy(self):
        records = self._records()
        assert apply_query(records, ExperienceQuery(regime_label="up_normal")) == [
            records[0],
            records[2],
        ]
        assert apply_query(records, ExperienceQuery(strategy_version="1.0.0")) == records
        assert apply_query(records, ExperienceQuery(strategy_name="ma_cross")) == records

    def test_filter_by_has_trade_and_status(self):
        records = self._records()
        assert apply_query(records, ExperienceQuery(has_trade=True)) == [records[0], records[1]]
        assert apply_query(records, ExperienceQuery(has_trade=False)) == [records[2]]
        assert apply_query(records, ExperienceQuery(record_status="no_trade")) == [records[2]]

    def test_filter_by_time_range_sorted(self):
        records = self._records()
        sliced = apply_query(
            records,
            ExperienceQuery(start_time=_ts(10, 0), end_time=_ts(10, 15)),
        )
        assert sliced == [records[0], records[1]]

    def test_count_by_grouping(self):
        records = self._records()
        assert count_by(records, "outcome") == {
            "NO_TRADE": 1,
            "LOSS": 1,
            "WIN": 1,
        }
        assert count_by(records, "regime_label") == {"down": 1, "up_normal": 2}

    def test_unknown_grouping_key_rejected(self):
        with pytest.raises(ValueError):
            count_by([], "bogus")


class TestExperienceStore:
    def test_in_memory_append_get(self):
        store = ExperienceStore()
        record = _record()
        result = store.append(record)
        assert result.duplicate is False
        assert store.get(record.experience_id) == record
        assert store.count == 1
        assert store.ids == [record.experience_id]
        assert store.all() == [record]

    def test_append_rejects_foreign_types(self):
        store = ExperienceStore()
        with pytest.raises(TypeError):
            store.append("not a record")

    def test_duplicate_append_is_idempotent(self):
        store = ExperienceStore()
        record = _record()
        store.append(record)
        result = store.append(record)
        assert result.duplicate is True
        assert result.record is record
        assert store.count == 1

    def test_merge_idempotency(self):
        store = ExperienceStore()
        first = _record()
        second = _record(
            decision=_decision(decision_timestamp=_ts(10, 5, day=3), signal=Signal.SELL),
            outcome=_outcome(realized_pnl="-900", exit_timestamp=_ts(10, 45, day=3)),
        )
        first_pass = store.merge([first, second])
        assert (first_pass.appended, first_pass.duplicates) == (2, 0)
        second_pass = store.merge([first, second])
        assert (second_pass.appended, second_pass.duplicates) == (0, 2)
        assert store.count == 2

    def test_pending_outcome_upgrade_to_complete(self):
        store = ExperienceStore()
        pending = _record(outcome=None)
        assert pending.status == "pending_outcome"
        store.append(pending)
        assert store.get(pending.experience_id).status == "pending_outcome"

        completed = pending.with_outcome(_outcome())
        assert completed.experience_id == pending.experience_id
        result = store.append(completed)
        assert result.duplicate is False
        assert store.get(pending.experience_id).status == "complete"
        assert store.count == 1

    def test_complete_record_cannot_be_replaced(self):
        store = ExperienceStore()
        store.append(_record())
        different = _record(outcome=_outcome(realized_pnl="-555"))
        assert different.experience_id == store.all()[0].experience_id
        result = store.append(different)
        assert result.duplicate is True
        assert store.all()[0].outcome.realized_pnl == Decimal("3000")

    def test_file_backed_round_trip(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        record = _record()
        store.append(record)
        reloaded = ExperienceStore(directory=tmp_path, name="default")
        assert reloaded.count == 1
        assert reloaded.get(record.experience_id) == record

    def test_multiple_names_in_one_directory(self, tmp_path):
        store_a = ExperienceStore(directory=tmp_path, name="a")
        store_a.append(_record())
        store_b = ExperienceStore(directory=tmp_path, name="b")
        store_b.append(_record(decision=_decision(signal=Signal.SELL)))
        assert ExperienceStore(directory=tmp_path, name="a").count == 1
        assert ExperienceStore(directory=tmp_path, name="b").count == 1
        assert ExperienceStore(directory=tmp_path, name="a").ids != ExperienceStore(
            directory=tmp_path, name="b"
        ).ids

    def test_rejects_missing_metadata(self, tmp_path):
        (tmp_path / "default.jsonl").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="metadata"):
            ExperienceStore(directory=tmp_path, name="default")

    def test_rejects_corrupt_log_line(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        store.append(_record())
        with (tmp_path / "default.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{this is not json}\n")
        with pytest.raises(ValueError, match="corrupt"):
            ExperienceStore(directory=tmp_path, name="default")

    def test_rejects_duplicate_experience_id_in_log(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        record = _record()
        store.append(record)
        with (tmp_path / "default.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(experience_to_dict(record)) + "\n")
        with pytest.raises(ValueError, match="append-only"):
            ExperienceStore(directory=tmp_path, name="default")

    def test_rejects_unsupported_schema_in_metadata(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        store.append(_record())
        meta_path = tmp_path / "default.meta.json"
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["schema_version"] = "0"
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="schema version"):
            ExperienceStore(directory=tmp_path, name="default")

    def test_store_query_and_find(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        store.merge(
            [
                _record(),
                _record(
                    decision=_decision(decision_timestamp=_ts(10, 5), signal=Signal.SELL, regime_label="down"),
                    outcome=_outcome(realized_pnl="-1000", exit_timestamp=_ts(10, 45)),
                ),
            ]
        )
        assert len(store.query(ExperienceQuery(outcome=OutcomeKind.WIN))) == 1
        assert len(store.find(outcome=OutcomeKind.LOSS)) == 1
        assert len(store.find(regime_label="down", has_trade=True)) == 1

    def test_restart_preserves_insertion_order(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        first = _record()
        second = _record(decision=_decision(decision_timestamp=_ts(10, 5, day=3), signal=Signal.SELL))
        store.merge([first, second])
        reloaded = ExperienceStore(directory=tmp_path, name="default")
        assert reloaded.all() == [first, second]


class TestCrossStoreBoundaries:
    def test_meta_manifest_carries_its_own_schema_version(self, tmp_path):
        store = ExperienceStore(directory=tmp_path, name="default")
        store.append(_record())
        metadata = json.loads(
            (tmp_path / "default.meta.json").read_text(encoding="utf-8")
        )
        assert metadata["schema_version"] == EXPERIENCE_SCHEMA_VERSION

    def test_payload_contains_no_execution_or_broker_references(self):
        payload = json.dumps(experience_to_dict(_record()), sort_keys=True)
        for forbidden in ("order_submission", "broker", "portfolio", "live", "risk_manager", "execution"):
            assert forbidden not in payload.lower()

    def test_advisory_never_records_an_order_id(self):
        record = _record()
        assert "trade_id" in experience_to_dict(record)["outcome"]
        assert record.advisory.present is True
        # advisory block carries no order/trade reference
        advisory_block = experience_to_dict(record)["advisory"]
        assert "trade_id" not in advisory_block
        assert "order_id" not in advisory_block