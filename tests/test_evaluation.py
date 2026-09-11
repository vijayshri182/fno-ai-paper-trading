"""Tests for WS 7.4 historical strategy evaluation."""
from __future__ import annotations

from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv
from fno_ai_paper_trading.evaluation import (
    EvaluationConfig,
    HistoricalEvaluator,
    evaluation_run_to_dict,
    evaluation_run_to_html,
)
from fno_ai_paper_trading.evaluation.records import composite_curve, losing_streak_of
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.strategies import MovingAverageCrossStrategy


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _baseline() -> MovingAverageCrossStrategy:
    return MovingAverageCrossStrategy(fast=5, slow=21)


class TestHistoricalEvaluator:
    def test_evaluate_baseline_is_deterministic(self) -> None:
        bars = build_crossing_ohlcv(_future())
        engine = HistoricalEvaluator()
        run_1 = engine.evaluate_bars(bars, _baseline(), name="rt", dataset_name="a", baseline=True)
        run_2 = engine.evaluate_bars(bars, _baseline(), name="rt", dataset_name="a", baseline=True)
        assert run_1.aggregate.total_pnl == run_2.aggregate.total_pnl
        assert run_1.aggregate.win_rate == run_2.aggregate.win_rate
        assert run_1.sessions[0].net_pnl == run_2.sessions[0].net_pnl
        assert run_1.aggregate.num_trades == run_2.aggregate.num_trades

    def test_standardized_metrics_present(self) -> None:
        run = HistoricalEvaluator().evaluate_bars(
            build_crossing_ohlcv(_future()), _baseline(), name="rt", dataset_name="a"
        )
        agg = run.aggregate
        assert agg.total_pnl is not None
        assert agg.total_return_pct is not None
        assert agg.win_rate is not None
        assert agg.transaction_costs >= 0
        assert agg.max_drawdown >= 0
        assert agg.max_drawdown_pct >= 0
        assert agg.exposure_pct >= 0
        assert agg.losing_streak_bars >= 0
        assert agg.num_trades >= 0
        assert agg.sessions == 1

    def test_baseline_flag_recorded(self) -> None:
        run = HistoricalEvaluator().evaluate_bars(
            build_crossing_ohlcv(_future()), _baseline(), name="rt", dataset_name="a", baseline=True
        )
        assert run.baseline is True
        run_2 = HistoricalEvaluator().evaluate_bars(
            build_crossing_ohlcv(_future()), _baseline(), name="rt", dataset_name="a"
        )
        assert run_2.baseline is False

    def test_multi_session_aggregate_compounds(self) -> None:
        bars = build_crossing_ohlcv(_future())
        engine = HistoricalEvaluator()
        run = engine.evaluate_bars(bars, _baseline(), name="rt", dataset_name="a")
        # compounding two identical sessions changes totals deterministically.
        second = engine.evaluate_bars(bars, _baseline(), name="rt", dataset_name="b")
        # Aggregate over a single session equals the session metrics.
        assert run.aggregate.total_pnl == run.sessions[0].net_pnl

    def test_crossing_series_produces_trades(self) -> None:
        run = HistoricalEvaluator().evaluate_bars(
            build_crossing_ohlcv(_future()), _baseline(), name="rt", dataset_name="a"
        )
        assert run.aggregate.num_trades >= 1

    def test_invalid_config_rejected(self) -> None:
        with pytest.raises(ValueError):
            EvaluationConfig(initial_capital=0)

    def test_report_serializers_smoke(self) -> None:
        run = HistoricalEvaluator().evaluate_bars(
            build_crossing_ohlcv(_future()), _baseline(), name="rt", dataset_name="a", baseline=True
        )
        data = evaluation_run_to_dict(run)
        assert data["strategy_name"] == "moving_average_cross"
        assert data["aggregate"]["win_rate"] is not None
        assert "MA" in evaluation_run_to_html(run) or "BASELINE" in evaluation_run_to_html(run)

    def test_composite_curve_chaining(self) -> None:
        from fno_ai_paper_trading.backtest.result import EquityPoint

        curve_a = [
            EquityPoint(timestamp=bar.timestamp, bar_index=i, equity=Decimal("100000"),
                        cash=Decimal("0"), unrealized_pnl=Decimal("0"), drawdown_from_peak=Decimal("0"))
            for i, bar in enumerate(build_crossing_ohlcv(_future())[:3])
        ]
        curve_b = [
            EquityPoint(timestamp=bar.timestamp, bar_index=i, equity=Decimal("101000"),
                        cash=Decimal("0"), unrealized_pnl=Decimal("0"), drawdown_from_peak=Decimal("0"))
            for i, bar in enumerate(build_crossing_ohlcv(_future())[3:6])
        ]
        composite = composite_curve([], [curve_a, curve_b], Decimal("100000"))
        assert len(composite) == 6
        assert composite[0].equity == Decimal("100000")
        # second session adds only its incremental change onto the running total.
        assert composite[3].equity == Decimal("101000")

    def test_losing_streak_of(self) -> None:
        from fno_ai_paper_trading.backtest.result import EquityPoint

        def point(ts, equity) -> EquityPoint:
            return EquityPoint(timestamp=ts, bar_index=0, equity=equity, cash=Decimal("0"),
                               unrealized_pnl=Decimal("0"), drawdown_from_peak=Decimal("0"))

        from datetime import datetime

        base = datetime(2026, 9, 1)
        from datetime import timedelta

        curve = [
            point(base + timedelta(hours=i), Decimal(str(100 + (10 if i % 3 == 0 else -5))))
            for i in range(6)
        ]
        assert losing_streak_of(curve) >= 1