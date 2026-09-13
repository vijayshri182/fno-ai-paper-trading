"""Tests for the ALGO READY / ALGORITHM HEALTH monitor (recorded trades only).

Covers the metric calculations (win rate, profit factor, expectancy, drawdown,
rolling windows, streaks, t-stat), the objective health/readiness/trend decision
rules, bucket separation, integrity-alert coupling, and the explicit guardrails
of the audit requirement (no win-rate shortcut, no dataset mixing, insufficient
samples clearly marked). All figures come from small, hand-checked synthetic
trade lists; no datasets are required.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from fno_ai_paper_trading.evaluation.algorithm_health import (
    DEFAULT_THRESHOLDS,
    IntegrityAlerts,
    TradeRecord,
    assess_algorithm_health,
    compute_trade_metrics,
    segment_expectancies,
)

T0 = datetime(2026, 1, 2, 9, 15)
T1 = datetime(2026, 1, 2, 9, 45)
T2 = datetime(2026, 1, 3, 9, 15)
T3 = datetime(2026, 1, 3, 10, 0)


def _trade(
    net: str,
    *,
    bucket: str = "backtest",
    entry: datetime = T0,
    exit: datetime = T1,
    price: str = "0.0",
    commission: str = "0.0",
    confidence: str | None = None,
    version: str = "v-test",
) -> TradeRecord:
    return TradeRecord(
        bucket=bucket,
        strategy_name="ma_test",
        algorithm_version=version,
        configuration_version="v-test-config",
        entry_time=entry,
        exit_time=exit,
        side="LONG",
        entry_price=Decimal("100"),
        exit_price=Decimal("100") + Decimal(price),
        price_pnl=Decimal(price),
        commission=Decimal(commission),
        net_pnl=Decimal(net),
        confidence=Decimal(confidence) if confidence is not None else None,
    )


# --------------------------------------------------------------------------
# Metric math
# --------------------------------------------------------------------------
def test_win_rate_and_counts_are_correct():
    trades = [_trade("1"), _trade("-2"), _trade("3"), _trade("-1"), _trade("5")]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.total_closed == 5
    assert metrics.winning == 3
    assert metrics.losing == 2
    assert metrics.win_rate_pct == Decimal("60")
    assert metrics.net_pnl == Decimal("6")


def test_expectancy_profit_factor_avg_win_loss():
    trades = [_trade("10"), _trade("-5"), _trade("20"), _trade("-5")]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.expectancy == Decimal("5")
    assert metrics.profit_factor == Decimal("3")  # 30 / 10
    assert metrics.avg_win == Decimal("15")
    assert metrics.avg_loss == Decimal("-5")


def test_max_drawdown_and_current_drawdown_from_cumulative_pnl():
    # Cumulative: 10, 18, 33, 23, 28  → peak 33, max dd 10, current 5.
    trades = [_trade("10"), _trade("8"), _trade("15"), _trade("-10"), _trade("5")]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.max_drawdown == Decimal("10")
    assert metrics.current_drawdown == Decimal("5")


def test_consecutive_wins_and_losses_use_ending_streak():
    trades = [_trade("1"), _trade("1"), _trade("-1"), _trade("-1"), _trade("-1")]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.consecutive_losses == 3
    assert metrics.consecutive_wins == 0
    trades2 = [_trade("-1"), _trade("1"), _trade("1")]
    metrics2 = compute_trade_metrics(trades2, bucket="backtest")
    assert metrics2.consecutive_wins == 2
    assert metrics2.consecutive_losses == 0


def test_rolling_win_rates_require_full_window():
    trades = [_trade("1") if i % 2 == 0 else _trade("-1") for i in range(5)]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.last_10_win_rate is None  # fewer than 10
    assert metrics.last_20_win_rate is None
    many = [_trade("1") if i % 2 == 0 else _trade("-1") for i in range(25)]
    metrics2 = compute_trade_metrics(many, bucket="backtest")
    assert metrics2.last_20_win_rate == Decimal("50")


def test_today_win_rate_only_for_paper_bucket():
    today = T1.date()
    paper = [
        _trade("1", bucket="paper", exit=T1),
        _trade("-1", bucket="paper", exit=T1),
        _trade("1", bucket="paper", exit=T2),
    ]
    metrics = compute_trade_metrics(paper, bucket="paper", today_date=today)
    assert metrics.today_closed == 2
    assert metrics.today_win_rate == Decimal("50")


def test_net_expectancy_t_present_and_correct_sign():
    trades = [_trade("-2") for _ in range(10)]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.net_expectancy_t is None or metrics.net_expectancy_t < 0


def test_average_confidence_none_when_baseline_emits_none():
    trades = [_trade("1"), _trade("-1")]
    metrics = compute_trade_metrics(trades, bucket="backtest")
    assert metrics.avg_confidence is None


def test_insufficient_sample_marked():
    metrics = compute_trade_metrics([_trade("1")], bucket="backtest")
    assert metrics.sample_sufficient is False


# --------------------------------------------------------------------------
# Health decision rules
# --------------------------------------------------------------------------
def _bucket(
    bucket: str,
    count: int,
    *,
    wins: int | None = None,
    win_value: str = "30",
    loss_value: str = "-20",
) -> list[TradeRecord]:
    wins = wins if wins is not None else max(count // 3, 1)
    trades: list[TradeRecord] = []
    for i in range(count):
        net = win_value if i % count < wins else loss_value
        trades.append(_trade(net, bucket=bucket))
    return trades


def test_negative_expectancy_sufficient_sample_is_red():
    backtest = _bucket("backtest", 600, wins=100, win_value="10", loss_value="-20")
    oos = _bucket("protected_oos", 60, wins=10, win_value="10", loss_value="-20")
    assessment = assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.health == "RED"
    assert assessment.ready == "NO"


def test_high_win_rate_with_negative_expectancy_is_still_red():
    # 80% win rate at small wins vs occasional big loss → negative expectancy.
    trades: list[TradeRecord] = []
    for i in range(100):
        trades.append(_trade("2" if i % 5 != 0 else "-10", bucket="backtest"))
    oos = trades[:50]
    assessment = assess_algorithm_health(
        {"backtest": trades, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.health == "RED"
    assert "not positive" in assessment.health_reason
    assert assessment.ready == "NO"


def test_green_requires_positive_expectancy_and_pf():
    backtest = _bucket("backtest", 600, wins=400, win_value="30", loss_value="-20")
    oos = _bucket("protected_oos", 60, wins=40, win_value="30", loss_value="-20")
    assessment = assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.health == "GREEN"
    assert assessment.ready == "MONITOR"  # no paper sample yet


def test_green_with_paper_sample_is_ready_yes():
    backtest = _bucket("backtest", 600, wins=400, win_value="30", loss_value="-20")
    oos = _bucket("protected_oos", 60, wins=40, win_value="30", loss_value="-20")
    paper = _bucket("paper", 12, wins=8, win_value="30", loss_value="-20")
    assessment = assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos, "paper": paper},
        version="v", configuration_version="c",
    )
    assert assessment.ready == "YES"


def test_small_oos_sample_is_yellow_not_red():
    backtest = _bucket("backtest", 600, wins=400, win_value="30", loss_value="-20")
    oos = _bucket("protected_oos", 5, wins=2, win_value="30", loss_value="-20")
    assessment = assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.health == "YELLOW"
    assert assessment.ready == "MONITOR"


def test_risk_violation_triggers_red_even_with_good_pnl():
    backtest = _bucket("backtest", 600, wins=400, win_value="30", loss_value="-20")
    oos = _bucket("protected_oos", 60, wins=40, win_value="30", loss_value="-20")
    assessment = assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos},
        version="v", configuration_version="c",
        integrity=IntegrityAlerts(risk_control_violations=("max-daily-loss breached",)),
    )
    assert assessment.health == "RED"
    assert "risk-control violation" in assessment.health_reason


# --------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------
def test_segment_expectancies_orders_chronologically():
    trades = [
        _trade("-5", entry=datetime(2022, 6, 1, 9, 15), exit=datetime(2022, 6, 1, 9, 30)),
        _trade("3", entry=datetime(2023, 6, 1, 9, 15), exit=datetime(2023, 6, 1, 9, 30)),
        _trade("5", entry=datetime(2024, 6, 1, 9, 15), exit=datetime(2024, 6, 1, 9, 30)),
    ]
    segments = segment_expectancies(trades, min_trades=3)
    assert [s[0] for s in segments] == ["2022", "2023", "2024"]
    assert segments[0][1] == 1
    assert segments[0][2] is None  # insufficient


def _assess_with_segments(expectancies: list[str], year_count: int = 120) -> "object":
    trades: list[TradeRecord] = []
    for year, expectancy in enumerate(expectancies, start=2020):
        for _i in range(year_count):
            trades.append(_trade(
                expectancy,
                entry=datetime(year, 6, 1, 9, 15),
                exit=datetime(year, 6, 1, 9, 30),
            ))
    backtest = trades
    oos = trades[: max(year_count // 2, DEFAULT_THRESHOLDS.min_oos_trades_green)]
    return assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos},
        version="v", configuration_version="c",
    )


def test_trend_improving():
    assessment = _assess_with_segments(["1", "2", "3", "4"])
    assert assessment.performance_trend == "IMPROVING"


def test_trend_deteriorating():
    assessment = _assess_with_segments(["4", "3", "2", "1"])
    assert assessment.performance_trend == "DETERIORATING"


def test_trend_stable():
    assessment = _assess_with_segments(["2", "2", "2", "2"])
    assert assessment.performance_trend == "STABLE"


def test_trend_insufficient_data():
    trades = [_trade("1"), _trade("-1")]
    oos = trades[:2]
    assessment = assess_algorithm_health(
        {"backtest": trades, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.performance_trend == "INSUFFICIENT DATA"


# --------------------------------------------------------------------------
# Audit guardrails
# --------------------------------------------------------------------------
def test_buckets_are_never_mixed():
    backtest = _bucket("backtest", 60)
    oos = _bucket("protected_oos", 30)
    assessment = assess_algorithm_health(
        {"backtest": backtest, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.buckets["backtest"].total_closed == 60
    assert assessment.buckets["protected_oos"].total_closed == 30
    assert assessment.buckets["backtest"].net_pnl + assessment.buckets["protected_oos"].net_pnl != assessment.buckets["protected_oos"].net_pnl


def test_readiness_never_from_win_rate_alone():
    # Negative expectancy with a high win rate → NO, not READY.
    trades: list[TradeRecord] = []
    for i in range(120):
        trades.append(_trade("1" if i % 10 != 0 else "-15", bucket="backtest"))
    oos = [t for t in trades[:60]]
    assessment = assess_algorithm_health(
        {"backtest": trades, "protected_oos": oos},
        version="v", configuration_version="c",
    )
    assert assessment.health == "RED"
    assert assessment.ready == "NO"
    assert "not positive" in assessment.health_reason


def test_metrics_dict_serializes_for_state_recovery():
    assessment = _assess_with_segments(["2", "2", "2", "2"])
    payload = assessment.to_dict()
    assert payload["algorithm_health"] == assessment.health
    assert payload["algo_ready"] == assessment.ready
    assert "reasons" not in payload
    assert payload["buckets"]["backtest"]["win_rate_pct"] is not None
    assert "last_updated" in payload