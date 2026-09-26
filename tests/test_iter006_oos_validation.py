"""Iteration-006 protected-OOS validation tests.

Discipline: these tests NEVER re-execute the protected-OOS engine (no second
look at the results for tuning).  They validate (a) the immutable fingerprint
against the frozen Iteration-005 literals, (b) the persisted result artifact's
mandatory-metric keys and reporting identities, (c) the pure pre-registered
verdict logic, and (d) the OOS slicing predicate -- all against the recorded
JSON artifacts or synthetic inputs.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import json

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.instruments import Instrument, InstrumentType
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration005_discovery import EnsembleParams, _hold
from fno_ai_paper_trading.research.iteration006_oos_validation import (
    FROZEN_PARAMS,
    FROZEN_WARMUP,
    INITIAL_CAPITAL,
    OOS_START,
    _sha256_file,
    oos_verdict,
    slice_oos,
)

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs" / "research" / "day_batch"
FINGERPRINT = RUNS / "iteration_006_n3_fingerprint.json"
RESULT = RUNS / "iteration_006_protected_oos_n3.json"


def _load_js(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dec(value) -> Decimal:
    return Decimal(value if isinstance(value, str) else str(value))


# ---------------------------------------------------------------------------
# frozen candidate immutability
# ---------------------------------------------------------------------------


def test_frozen_params_defaults_must_match_recorded_literals() -> None:
    p = EnsembleParams()
    assert {
        "fast": p.fast, "slow": p.slow, "slope_window": p.slope_window,
        "lookback": p.lookback, "stop_atr_mult": p.stop_atr_mult,
        "max_hold_days": p.max_hold_days,
    } == FROZEN_PARAMS
    assert p.slow + p.slope_window + p.lookback + 3 == FROZEN_WARMUP == 54


def test_fingerprint_is_frozen_and_matches_literals() -> None:
    fp = _load_js(FINGERPRINT)
    assert fp["recorded_before_oos_run"] is True
    assert fp["oos_run_status"] == "COMPLETED"
    freeze = fp["candidate_freeze"]
    assert freeze["candidate_id"] == "n3_ensemble"
    assert freeze["parameters"] == FROZEN_PARAMS
    assert freeze["warmup_sessions"] == FROZEN_WARMUP
    assert freeze["discovered_in"] == "ITERATION_005"
    assert fp["boundary"]["protected_oos_start"] == "2025-10-06"
    assert fp["boundary"]["oos_used_for_selection"] is False
    assert fp["evaluation_config"]["quantity"] == 1
    assert fp["evaluation_config"]["initial_capital"] == 100000
    assert fp["evaluation_config"]["commission_rate"] == "0.0003"
    assert fp["evaluation_config"]["slippage_rate"] == "0.001"
    assert fp["evaluation_config"]["stop_loss_pct"] == "0.02"
    assert fp["dataset"]["data_hash_sha256"].startswith("6c400b01")
    assert fp["dataset"]["name"] == "upstox_Nifty_50_5m_20220103_20260911.csv"


# ---------------------------------------------------------------------------
# persisted mandatory metrics + identities
# ---------------------------------------------------------------------------


MANDATORY_KEYS = [
    "starting_capital", "ending_equity", "net_pnl", "return_pct",
    "gross_profit", "gross_loss", "gross_pnl", "gross_edge",
    "commissions", "slippage", "total_costs", "cost_coverage_pct",
    "profit_factor_gross", "profit_factor_net", "round_trips", "fills",
    "wins", "losses", "win_rate_pct", "avg_winning_trade", "avg_losing_trade",
    "expectancy_per_rt", "max_drawdown", "max_drawdown_pct",
    "longest_winning_streak", "longest_losing_streak",
    "trading_days", "active_trading_days", "no_trade_days", "trades_per_day",
    "avg_holding_period_minutes", "median_holding_period_minutes",
]


def test_result_artifact_all_mandatory_keys_present() -> None:
    out = _load_js(RESULT)
    assert set(MANDATORY_KEYS) <= set(out["mandatory_metrics"])
    assert out["oos_run"]["engine_runs"] == 1
    assert out["verdict"]["code"] in {
        "INCONCLUSIVE_LOW_TRADES", "FAIL_NO_GROSS_EDGE_OOS", "FAIL_COSTS_OOS",
        "PARTIAL_NEEDS_REPLICATION", "PASS_SURVIVES_PROTECTED_OOS",
    }


def test_result_reporting_identities_hold() -> None:
    out = _load_js(RESULT)
    m = out["mandatory_metrics"]
    ec = out["economics"]
    assert m["starting_capital"] == str(INITIAL_CAPITAL)
    assert round(_dec(m["ending_equity"]), 6) == round(
        _dec(m["starting_capital"]) + _dec(m["net_pnl"]), 6)
    assert round(_dec(m["gross_pnl"]), 6) == round(
        _dec(m["gross_profit"]) - _dec(m["gross_loss"]), 6)
    assert round(_dec(m["total_costs"]), 6) == round(
        _dec(m["slippage"]) + _dec(m["commissions"]), 6)
    assert round(_dec(ec["net_closed_rts"]) + _dec(ec["carry_mtm"]), 6) == round(
        _dec(ec["total_pnl"]), 6)
    assert ec["reconcile"]["economic_identity"] is True
    assert m["wins"] + m["losses"] == m["round_trips"]
    expected_wr = round(m["wins"] / m["round_trips"] * 100, 2) if m["round_trips"] else 0.0
    assert m["win_rate_pct"] == expected_wr
    assert m["no_trade_days"] == m["trading_days"] - m["active_trading_days"]


def test_result_reconciles_and_fingerprint_hash_is_linked() -> None:
    out = _load_js(RESULT)
    assert out["economics"]["reconcile_all"] is True
    fp = out["fingerprint"]
    assert fp["frozen_matches_iteration_005"] is True
    assert fp["sha256"] == _sha256_file(REPO / fp["path"])


def test_reference_to_iteration_005_is_recorded() -> None:
    out = _load_js(RESULT)
    ref = out["reference_iteration_005"]["research_domain_reserved"]
    assert ref["round_trips"] == 108
    assert float(ref["net_at_1x"]) == float(6648.798750230)


# ---------------------------------------------------------------------------
# pre-registered verdict (pure logic, synthetic inputs only)
# ---------------------------------------------------------------------------


def _m(rts: int, gross: str, net: str, coverage) -> dict:
    return {
        "round_trips": rts, "gross_edge": gross, "net_pnl": net,
        "cost_coverage_pct": coverage,
    }


def test_verdict_maps_all_codes() -> None:
    assert oos_verdict(_m(5, "1000", "500", 150.0), {"t_gross_per_rt": 2.0, "carry_share_pct": 90.0})["code"] == "INCONCLUSIVE_LOW_TRADES"
    assert oos_verdict(_m(20, "-100", "-500", None), {"t_gross_per_rt": -1.0, "carry_share_pct": 90.0})["code"] == "FAIL_NO_GROSS_EDGE_OOS"
    assert oos_verdict(_m(20, "1000", "-100", 50.0), {"t_gross_per_rt": 2.0, "carry_share_pct": 90.0})["code"] == "FAIL_COSTS_OOS"
    assert oos_verdict(_m(20, "1000", "200", 150.0), {"t_gross_per_rt": 0.5, "carry_share_pct": 100.0})["code"] == "PARTIAL_NEEDS_REPLICATION"
    assert oos_verdict(_m(31, "3703.85", "1710.00", 185.76), {"t_gross_per_rt": 3.9, "carry_share_pct": 100.0})["code"] == "PASS_SURVIVES_PROTECTED_OOS"


def _synthetic_bars() -> tuple[list[MarketPrice], list]:
    inst = Instrument(
        symbol="NIFTY", instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY", exchange="NSE", lot_size=1,
    )
    bars: list[MarketPrice] = []
    t = datetime(2025, 10, 3, 9, 15)
    for _ in range(8):
        bars.append(MarketPrice(
            instrument=inst, timestamp=t, open=Decimal("100"), high=Decimal("102"),
            low=Decimal("99"), close=Decimal("101"), volume=0,
        ))
        t += timedelta(days=1)
    sigs = [_hold(b, "test", {}) for b in bars]
    return bars, sigs


def test_oos_slice_takes_only_protected_window() -> None:
    bars, sigs = _synthetic_bars()
    ob, os_ = slice_oos(bars, sigs)
    assert ob and os_
    assert len(ob) == len(os_)
    assert all(b.timestamp.date() >= OOS_START for b in ob)
    expected = sum(1 for b in bars if b.timestamp.date() >= OOS_START)
    assert len(ob) == expected
    assert expected == 5  # Oct 6..10 2025 (synthetic daily bars incl. weekend days)


def test_exported_constants() -> None:
    assert OOS_START == date(2025, 10, 6)
    assert Signal.BUY is not None
    assert not FROZEN_PARAMS["stop_atr_mult"] <= 0