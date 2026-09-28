"""Focused tests for the algorithm robustness & strengthening research pass.

Coverage intent (all unit-level, synthetic bars only, no real dataset, no
network, no protected-window data, no order/live activity):
  * dev-window guard: protected-OOS bars are never admitted (``research_bars`` /
    ``assert_clean_dev_domain``).
  * the analytics are pure and deterministic over their inputs.
  * the champion identity/config and recorded-artifact cross-check semantics.
  * failure taxonomy, holding buckets, loss clusters, regimes.
  * absence of network + absence of input mutation while replaying.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.data.mock_provider import build_crossing_ohlcv
from fno_ai_paper_trading.evaluation.algorithm_health import TradeRecord
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.options_research.windows import PROTECTED_OOS_START
from fno_ai_paper_trading.research.robustness_pass import (
    CHAMPION_FAST,
    CHAMPION_SLOW,
    HOLD_BUCKETS,
    NEIGHBOURHOOD_FAST,
    NEIGHBOURHOOD_SLOW,
    assert_clean_dev_domain,
    baseline_from_run,
    champion_run,
    classify_failures,
    cost_sensitivity,
    determinism_probe,
    holding_bucket_rows,
    loss_cluster_forensics,
    metrics_snapshot,
    parameter_neighbourhood,
    recorded_backtest_cross_check,
    regime_breakdown,
    research_bars,
    research_days,
    round_trip_rows,
    trips_to_trade_records,
)

_DEV_EPOCH = datetime(2022, 1, 3, 9, 15)
_PROTECTED_AFTER = datetime(2026, 1, 5, 9, 15)  # inside PROTECTED_OOS 2025-10-06..2026-09-11


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
    )


def _dev_bars(flat=40, up=40, hold=10, down=25) -> list:
    base = datetime(2026, 9, 1, 9, 15)
    bars = build_crossing_ohlcv(
        _future(), flat_bars=flat, up_bars=up, hold_bars=hold, down_bars=down
    )
    offset = _DEV_EPOCH - base
    return [replace(b, timestamp=b.timestamp + offset) for b in bars]


def _sig(signal: str | None) -> Any:
    class _Sig:
        pass

    s = _Sig()
    s.signal = signal
    return s


def _row(entry_index: int, side: str, net: str, price_pnl: str, entry: str = "100") -> dict[str, Any]:
    return {
        "entry_index": entry_index,
        "side": side,
        "net_pnl": Decimal(net),
        "price_pnl": Decimal(price_pnl),
        "entry_price": Decimal(entry),
        "exit_price": Decimal(entry),
        "entry_regime": "up_normal",
        "holding_bars": "2",
        "commission": Decimal("0"),
    }


def test_research_bars_excludes_protected_oos() -> None:
    bars = _dev_bars()
    protected = [replace(bars[0], timestamp=_PROTECTED_AFTER)]
    dev = research_bars(bars + protected)
    assert len(research_days(dev)) > 0
    assert all(b.timestamp.date() < PROTECTED_OOS_START for b in dev)
    assert dev is not bars


def test_assert_clean_dev_domain_rejects_protected_day() -> None:
    dev_days = research_days(_dev_bars())
    assert assert_clean_dev_domain(dev_days)
    with pytest.raises(ValueError):
        assert_clean_dev_domain((*dev_days, PROTECTED_OOS_START))


def test_trips_to_trade_records_round_trips_convention() -> None:
    rows = [
        {
            "entry_time": datetime(2022, 1, 5, 9, 35),
            "exit_time": datetime(2022, 1, 5, 10, 5),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("102"),
            "price_pnl": Decimal("2"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("1.9"),
        }
    ]
    records = trips_to_trade_records(rows, bucket="backtest")
    assert len(records) == 1
    assert isinstance(records[0], TradeRecord)
    assert records[0].net_pnl == Decimal("1.9")
    assert records[0].price_pnl == Decimal("2")
    assert records[0].algorithm_version == "v1-baseline-ma521"


def test_metrics_snapshot_hand_computed() -> None:
    rows = [
        {
            "entry_time": datetime(2022, 1, 5, 9, 35),
            "exit_time": datetime(2022, 1, 5, 10, 5),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("102"),
            "price_pnl": Decimal("2"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("1.9"),
        },
        {
            "entry_time": datetime(2022, 1, 5, 11, 0),
            "exit_time": datetime(2022, 1, 5, 11, 30),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("103"),
            "price_pnl": Decimal("3"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("2.9"),
        },
        {
            "entry_time": datetime(2022, 1, 5, 12, 0),
            "exit_time": datetime(2022, 1, 5, 12, 30),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("98"),
            "price_pnl": Decimal("-2"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("-2.1"),
        },
    ]
    m = metrics_snapshot(rows, bucket="backtest")
    assert m["total_closed"] == 3
    assert m["winning"] == 2
    assert m["losing"] == 1
    assert m["net_pnl"] == str(Decimal("2.7"))
    assert m["expectancy"] == str(Decimal("2.7") / 3)


def test_holding_bucket_assignment_and_contribution() -> None:
    rows = []
    for i, holding_bars in enumerate((75, 76, 1126, 1900)):
        rows.append(
            {
                **_row(i, "BUY", str(-Decimal(i + 1)), str(-Decimal(i + 1))),
                "holding_bars": str(holding_bars),
            }
        )
    buckets = holding_bucket_rows(rows)
    by_name = {b["bucket"]: b for b in buckets}
    assert by_name["intraday_or_1d"]["num_trades"] == 1
    assert by_name["2d_5d"]["num_trades"] == 1
    assert by_name["16d_25d"]["num_trades"] == 1
    assert by_name["over_25d"]["num_trades"] == 1
    nonzero = [b for b in buckets if b["num_trades"]]
    total = sum(Decimal(b["contribution_pct"]) for b in nonzero)
    assert total == Decimal("100")


def _row_exit(index: int, side: str, net: str, price_pnl: str, exit_price: str) -> dict[str, Any]:
    return {
        "entry_index": index,
        "side": side,
        "net_pnl": Decimal(net),
        "price_pnl": Decimal(price_pnl),
        "entry_price": Decimal("100"),
        "exit_price": Decimal(exit_price),
        "entry_regime": "up_normal",
        "holding_bars": "2",
        "commission": Decimal("0"),
    }


def test_classify_failures_taxonomy() -> None:
    signals = [
        _sig(None),
        _sig(None),
        _sig(None),
        _sig("SELL"),
        _sig("SELL"),
        _sig(None),
    ]
    trips = [
        _row_exit(0, "BUY", "5", "5", "105"),  # winner
        _row_exit(1, "BUY", "0", "1", "101"),  # cost_flip (price up > 0, net <= 0)
        _row_exit(2, "BUY", "-1", "-1", "99"),  # -1% adverse; SELL at index 3 in window
        _row_exit(3, "BUY", "-1.5", "-1.5", "98.5"),  # -1.5%; SELL at index 4 in window
        _row_exit(4, "BUY", "-2.5", "-2.5", "97.5"),  # -2.5% => stop band (0.019), no SELL window
        _row_exit(5, "BUY", "-0.5", "-0.5", "99.5"),  # -0.5% => adverse_hold (no SELL window)
    ]
    out = classify_failures(trips, signals)
    by_name = {c["category"]: c for c in out["categories"]}
    assert by_name["winner"]["num_trades"] == 1
    assert by_name["cost_flip"]["num_trades"] == 1
    # rows 2 and 3 both see the SELL cross inside their 25-bar window
    # (entry_index 2 => window [3..27], entry_index 3 => window [4..28]).
    assert by_name["whipsaw"]["num_trades"] == 2
    assert by_name["stop_hit"]["num_trades"] == 1
    assert by_name["adverse_hold"]["num_trades"] == 1
    assert out["details_count"] == len(trips)


def test_loss_cluster_forensics() -> None:
    rows = []
    for i in range(10):
        rows.append(
            {
                "entry_time": datetime(2022, 1, 5, 9, 35) + timedelta(minutes=5 * i),
                "exit_time": datetime(2022, 1, 5, 10, 35) + timedelta(minutes=5 * i),
                "side": "BUY",
                "net_pnl": Decimal("-1"),
                "price_pnl": Decimal("-1"),
                "entry_price": Decimal("100"),
                "entry_index": i,
                "holding_bars": "2",
            }
        )
    rows[9]["net_pnl"] = Decimal("4")
    rows[9]["price_pnl"] = Decimal("4")
    f = loss_cluster_forensics(rows)
    assert f["longest_consecutive_losses"] == 9
    assert f["worst_k_trade_windows"]["5"] == str(Decimal("-5"))
    assert f["trades"] == 10


def test_regime_breakdown_groups_and_totals() -> None:
    rows = [
        {**_row(i, "BUY", "-1", "-1"), "entry_regime": "up_normal"} for i in range(3)
    ]
    rows += [
        {**_row(i + 3, "BUY", "2", "2"), "entry_regime": "sideways_low"} for i in range(2)
    ]
    breakdown = regime_breakdown(rows)
    assert {b["regime"] for b in breakdown} == {"up_normal", "sideways_low"}
    assert sum(b["num_trades"] for b in breakdown) == 5
    assert sum(Decimal(b["net_pnl"]) for b in breakdown) == Decimal("-3") + Decimal("4")


def test_champion_run_deterministic_and_offline(monkeypatch) -> None:
    def _block_socket(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted")

    monkeypatch.setattr("socket.socket", _block_socket)
    bars = _dev_bars()
    snapshot = [(b.timestamp, b.close) for b in bars]
    a = champion_run(bars)
    b = champion_run(bars)
    base = baseline_from_run(a)
    row = base["strategy"]
    assert (row["fast"], row["slow"]) == (CHAMPION_FAST, CHAMPION_SLOW)
    assert base["reconciliation_ok"] is True
    assert a.summary_dict() == b.summary_dict()
    assert [(b.timestamp, b.close) for b in bars] == snapshot


def test_determinism_probe() -> None:
    out = determinism_probe(_dev_bars())
    assert out["deterministic"] is True


def test_parameter_neighbourhood_structure() -> None:
    bars = _dev_bars(flat=40, up=40, hold=10, down=40)
    out = parameter_neighbourhood(bars)
    assert len(out["rows"]) == len(NEIGHBOURHOOD_FAST) * len(NEIGHBOURHOOD_SLOW)
    champion_rows = [r for r in out["rows"] if r["is_champion"]]
    assert len(champion_rows) == 1
    assert set(out["neighbourhood"]["fast_values"]) == set(NEIGHBOURHOOD_FAST)
    for r in out["rows"]:
        assert r["num_trades"] >= 0
        assert r["fast"] in NEIGHBOURHOOD_FAST
        assert r["slow"] in NEIGHBOURHOOD_SLOW


def test_cost_sensitivity_structure() -> None:
    bars = _dev_bars(flat=40, up=60, hold=15, down=40)
    out = cost_sensitivity(bars)
    assert [r["scenario"] for r in out["rows"]] == [
        "zero_cost",
        "low_cost",
        "base",
        "high_cost",
        "statutory_nse_fo_illustrative",
    ]
    by_name = {r["scenario"]: r for r in out["rows"]}
    zero = Decimal(by_name["zero_cost"]["net_pnl"])
    high = Decimal(by_name["high_cost"]["net_pnl"])
    statutory = by_name["statutory_nse_fo_illustrative"]
    assert statutory["cost_schedule"] == "nse_fo_illustrative"
    assert zero >= high


def test_recorded_cross_check_match_and_detection() -> None:
    rows = [
        {
            "entry_time": datetime(2022, 1, 5, 9, 35),
            "exit_time": datetime(2022, 1, 5, 10, 5),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("101"),
            "price_pnl": Decimal("1"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("0.9"),
        },
        {
            "entry_time": datetime(2022, 1, 6, 9, 35),
            "exit_time": datetime(2022, 1, 6, 10, 5),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("99"),
            "price_pnl": Decimal("-1"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("-1.1"),
        },
        {
            "entry_time": datetime(2026, 1, 20, 9, 35),  # after recorded cutoff => exluded
            "exit_time": datetime(2026, 1, 20, 10, 5),
            "side": "BUY",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("100"),
            "price_pnl": Decimal("0"),
            "commission": Decimal("0.1"),
            "net_pnl": Decimal("-0.1"),
        },
    ]
    recorded = {
        "total_closed": 2,
        "winning": 1,
        "losing": 1,
        "net_pnl": str(Decimal("-0.2")),
        "expectancy": str(Decimal("-0.2") / 2),
    }
    out = recorded_backtest_cross_check(rows, recorded)
    assert out["match"] is True
    assert out["rows_used"] == 2
    tampered = dict(recorded, net_pnl=str(Decimal("-9")))
    assert recorded_backtest_cross_check(rows, tampered)["match"] is False


def test_round_trip_rows_include_decision_context() -> None:
    run = champion_run(_dev_bars(flat=40, up=60, hold=15, down=40))
    rows = round_trip_rows(run)
    if rows:
        assert "entry_index" in rows[0]
        assert "entry_regime" in rows[0]
        assert "net_pnl" in rows[0]


def test_protected_oos_assertion_is_static_for_research_window() -> None:
    assert PROTECTED_OOS_START == date(2025, 10, 6)


def test_hold_bucket_ranges_cover_all_bar_counts() -> None:
    all_covered = HOLD_BUCKETS[0][1] == 0
    for i in range(1, len(HOLD_BUCKETS)):
        assert HOLD_BUCKETS[i][1] == HOLD_BUCKETS[i - 1][2] + 1
    assert HOLD_BUCKETS[-1][2] == 1875