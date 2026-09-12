"""Tests for WS 7.13 continuous feedback / learning loop.

The loop is paper-only: capture builds experience records from completed
replay round trips, generation emits inert hypotheses, and promotion (when it
happens) is bookkeeping in the version registry. Nothing here places orders,
touches risk, or enables live trading.
"""
from __future__ import annotations

import ast
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
from fno_ai_paper_trading.experience.enums import DecisionStatus, ExperienceSourceType, OutcomeKind
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.learning.capture import (
    CaptureResult,
    capture_from_day_bars,
    pair_round_trips,
)
from fno_ai_paper_trading.learning.loop import (
    LOOP_DISCLAIMER,
    LearningLoop,
    LearningLoopConfig,
    cycle_result_to_html,
    default_strategy_factories,
    resolve_active_champion,
)
from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.persistence.experience_store import ExperienceStore
from fno_ai_paper_trading.promotion.gate import PromotionCriteria
from fno_ai_paper_trading.promotion.registry import VersionRegistry
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.strategies import (
    MovingAverageCrossStrategy,
    RegimeFilteredMovingAverageCross,
)

FLAT_PRICE = Decimal("25000")


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _bar(close: Decimal, ts: datetime) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=ts,
        open=close - Decimal("2"),
        high=close + Decimal("5"),
        low=close - Decimal("5"),
        close=close,
        volume=1000,
    )


def _series(steps: list[Decimal], length: int, start: datetime) -> list[MarketPrice]:
    bars: list[MarketPrice] = []
    price = FLAT_PRICE
    for i in range(length):
        if i >= 30:
            price += steps[i - 30]
        bars.append(_bar(price, start + timedelta(minutes=1 * i)))
    return bars


def _rise_day(day: date) -> DayBars:
    return DayBars(
        day=day,
        bars=tuple(_series([Decimal("100")] * 30, 60, datetime.combine(day, time(9, 15)))),
        source_hash=f"rise-{day.isoformat()}",
    )


def _fall_day(day: date) -> DayBars:
    return DayBars(
        day=day,
        bars=tuple(_series([Decimal("-100")] * 30, 60, datetime.combine(day, time(9, 15)))),
        source_hash=f"fall-{day.isoformat()}",
    )


def _hill_day(day: date) -> DayBars:
    rise = [Decimal("10")] * 10
    fall = [Decimal("-100")] * 20
    return DayBars(
        day=day,
        bars=tuple(_series(rise + fall, 60, datetime.combine(day, time(9, 15)))),
        source_hash=f"hill-{day.isoformat()}",
    )


def _five_day_set() -> list[DayBars]:
    """training (3) / validation (1) / out-of-sample (1) with an OOS steep rise."""
    return [
        _rise_day(date(2026, 9, 1)),
        _hill_day(date(2026, 9, 2)),
        _rise_day(date(2026, 9, 3)),
        _fall_day(date(2026, 9, 4)),
        _rise_day(date(2026, 9, 5)),
    ]


def _loop(
    *,
    store=None,
    registry=None,
    oos_min_days: int = 1,
) -> LearningLoop:
    return LearningLoop(
        store=store,
        registry=registry,
        detector=RegimeDetector(),
        config=LearningLoopConfig(criteria=PromotionCriteria(oos_min_days=oos_min_days)),
    )


class TestPairRoundTrips:
    def _trade(self, side: OrderSide, minute: int) -> Trade:
        return Trade(
            trade_id=f"t-{minute}",
            instrument=_future(),
            side=side,
            quantity=1,
            price=Decimal("25000"),
            commission=Decimal("0"),
            executed_at=datetime(2026, 9, 1, 9, 30 + minute),
        )

    def test_pairs_fifo_per_instrument(self) -> None:
        trades = [
            self._trade(OrderSide.BUY, 0),
            self._trade(OrderSide.SELL, 15),
        ]
        trips, open_count = pair_round_trips(trades)
        assert len(trips) == 1
        assert open_count == 0

    def test_open_positions_counted_not_paired(self) -> None:
        trades = [
            self._trade(OrderSide.BUY, 0),
            self._trade(OrderSide.BUY, 5),
            self._trade(OrderSide.SELL, 10),
        ]
        trips, open_count = pair_round_trips(trades)
        assert len(trips) == 1
        assert open_count == 1


class TestCapture:
    def test_capture_produces_completed_round_trips(self) -> None:
        evaluator = HistoricalEvaluator()
        result: CaptureResult = capture_from_day_bars(
            [_hill_day(date(2026, 9, 1))],
            MovingAverageCrossStrategy(),
            evaluator,
            detector=RegimeDetector(),
        )
        assert result.sessions == 1
        assert result.round_trips >= 1
        assert len(result.records) == result.round_trips
        record = result.records[0]
        assert record.status == "complete"
        assert record.outcome is not None
        assert record.outcome.outcome in (
            OutcomeKind.WIN,
            OutcomeKind.LOSS,
            OutcomeKind.BREAKEVEN,
        )
        assert record.decision.signal is Signal.BUY
        assert record.decision.decision_status is DecisionStatus.EXECUTED
        assert record.source is ExperienceSourceType.HISTORICAL_REPLAY
        assert record.decision.strategy_name == "moving_average_cross"

    def test_capture_is_deterministic(self) -> None:
        evaluator = HistoricalEvaluator()
        days = [_hill_day(date(2026, 9, 1))]
        first = capture_from_day_bars(days, MovingAverageCrossStrategy(), evaluator)
        second = capture_from_day_bars(days, MovingAverageCrossStrategy(), evaluator)
        assert len(first.records) == len(second.records)
        assert [r.experience_id for r in first.records] == [
            r.experience_id for r in second.records
        ]

    def test_merge_is_idempotent_via_store(self) -> None:
        store = ExperienceStore()
        evaluator = HistoricalEvaluator()
        records = capture_from_day_bars(
            [_hill_day(date(2026, 9, 1))], MovingAverageCrossStrategy(), evaluator
        ).records
        first = store.merge(records)
        second = store.merge(records)
        assert first.appended == len(records)
        assert first.duplicates == 0
        assert second.appended == 0
        assert second.duplicates == len(records)

    def test_open_trades_never_become_records(self) -> None:
        evaluator = HistoricalEvaluator()
        result = capture_from_day_bars(
            [_rise_day(date(2026, 9, 1))],
            MovingAverageCrossStrategy(),
            evaluator,
        )
        assert result.round_trips == 0
        assert result.records == ()
        assert result.open_position_trades >= 1

    def test_regime_label_attached_from_decision_prefix(self) -> None:
        evaluator = HistoricalEvaluator()
        result = capture_from_day_bars(
            [_rise_day(date(2026, 9, 1)), _hill_day(date(2026, 9, 2))],
            MovingAverageCrossStrategy(),
            evaluator,
            detector=RegimeDetector(),
        )
        assert len(result.records) >= 1
        assert any(r.decision.regime_label is not None for r in result.records)


class TestLearningLoop:
    def test_cycle_without_store_or_registry_returns_verdicts(self) -> None:
        loop = _loop()
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
            cycle=1,
        )
        assert result.cycle == 1
        assert result.day_count == 5
        assert result.period_counts["out_of_sample"] == 1
        assert len(result.verdicts) == 1
        assert result.verdicts[0].decision == "REJECT"
        assert result.promotion is False
        assert not result.promoted_versions

    def test_cycle_rejects_regime_suppressed_candidate_on_oos(self) -> None:
        loop = _loop()
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
        )
        verdict = result.verdicts[0]
        assert verdict.decision == "REJECT"
        assert any("does not beat the champion" in r for r in verdict.reasons)

    def test_cycle_promotes_twin_and_updates_registry(self) -> None:
        registry = VersionRegistry()
        registry.start()
        loop = _loop(registry=registry)
        twin = RegimeFilteredMovingAverageCross(
            allowed_trends=("UP", "DOWN", "SIDEWAYS")
        )
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[twin],
        )
        assert result.promotion is True
        verdict = result.verdicts[0]
        assert verdict.decision == "PROMOTE"
        assert verdict.promoted_version is not None
        assert registry.active is not None
        assert registry.active.strategy_name == "regime_filtered_ma_cross"
        assert registry.active.version_id == verdict.promoted_version

    def test_rejected_candidate_never_reaches_registry(self) -> None:
        registry = VersionRegistry()
        registry.start()
        loop = _loop(registry=registry)
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
        )
        assert result.promotion is False
        assert len(registry.versions) == 1  # baseline only

    def test_repeat_uses_active_champion_from_registry(self) -> None:
        registry = VersionRegistry()
        registry.start()
        loop = _loop(registry=registry)
        twin = RegimeFilteredMovingAverageCross(
            allowed_trends=("UP", "DOWN", "SIDEWAYS")
        )
        loop.run_cycle(_five_day_set(), champion=MovingAverageCrossStrategy(), candidates=[twin])
        resolved = resolve_active_champion(registry, default_strategy_factories())
        assert resolved.name == "regime_filtered_ma_cross"
        cycle_two = _loop(
            registry=registry, oos_min_days=1
        ).run_cycle(
            _five_day_set(),
            champion=resolved,
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
            cycle=2,
        )
        assert cycle_two.champion_name == "regime_filtered_ma_cross"

    def test_captured_trades_accumulate_in_store(self) -> None:
        store = ExperienceStore()
        loop = _loop(store=store)
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
        )
        assert result.captured_records >= 1
        assert result.appended >= 1
        assert store.count == result.appended

    def test_hypothesis_generation_runs_over_captured_evidence(self) -> None:
        store = ExperienceStore()
        loop = _loop(store=store)
        result = loop.run_cycle(
            [_hill_day(date(2026, 9, 2))],
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
        )
        # single hill day -> no period splits, so no promotion, but capture/hypotheses run
        assert result.captured_records >= 1
        assert isinstance(result.hypotheses, tuple)


class TestCycleSerialization:
    def test_cycle_to_dict_is_json_serializable(self) -> None:
        loop = _loop()
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
        )
        payload = result.to_dict()
        dumped = json.dumps(payload)
        assert isinstance(dumped, str)
        assert payload["promotion"] is False
        assert payload["verdicts"][0]["decision"] == "REJECT"

    def test_cycle_to_html_contains_disclaimer(self) -> None:
        loop = _loop()
        result = loop.run_cycle(
            _five_day_set(),
            champion=MovingAverageCrossStrategy(),
            candidates=[RegimeFilteredMovingAverageCross(allowed_trends=("DOWN",))],
        )
        html = cycle_result_to_html(result)
        assert LOOP_DISCLAIMER in html
        assert "never enables live trading" in html


class TestImportBoundary:
    FORBIDDEN_ROOTS = {
        "broker",
        "portfolio",
        "risk",
        "services",
        "paper_session",
        "sizing",
        "stop_loss",
    }

    def test_loop_module_has_no_execution_imports(self) -> None:
        source = Path(
            "src/fno_ai_paper_trading/learning/loop.py"
        ).read_text(encoding="utf-8")
        self._assert_no_forbidden_imports(source)

    def test_capture_module_has_no_execution_imports(self) -> None:
        source = Path(
            "src/fno_ai_paper_trading/learning/capture.py"
        ).read_text(encoding="utf-8")
        self._assert_no_forbidden_imports(source)

    def _assert_no_forbidden_imports(self, source: str) -> None:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in self.FORBIDDEN_ROOTS, (
                    f"forbidden execution import {node.module!r}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in self.FORBIDDEN_ROOTS, (
                        f"forbidden execution import {alias.name!r}"
                    )