"""Iteration-004 tests: combined ADX entry + exit confirmation overlay.

Every expected value below is hand-verified against the documented engine
semantics used by the Iteration-004 pre-registration (Iteration-002 entry
confirmation + Iteration-003 exit confirmation, fused on a single shadow
replica and executed through the UNCHANGED BacktestEngine):

* FILL = one executed entry/exit fill; ROUND TRIP = one completed entry+exit
  lifecycle (``BacktestResult.num_trades``). WINS/LOSSES are closed round
  trips by sign of ``Trade.realized_pnl``.
* Slippage is adverse: BUY = close * (1 + 0.001), SELL = close * (1 - 0.001);
  commission = notional * 0.0003. Realized P&L excludes commission.
* Protective stop is LONG-only, anchored below the slippage-adjusted entry
  fill, entry candle excluded: open <= stop -> fill at open*(1-sl); low <= stop
  -> fill at stop*(1-sl).
* Risk gate: an order is rejected when the date's realized P&L <= -10000.
* NET (canonical) == ``BacktestResult.total_pnl`` == realized + carry MTM
  - commissions. This is the report taxonomy fixed by the audit (Medium 1 + 2).
* ``_pos_state`` reports a quantity-0 closed position as side="SHORT" (an
  observability quirk); execution sync must compare SIGNED quantity, never the
  side label.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.day_batch_overlays import (
    _INTENDED_COMBINED_ACTIONS,
    _Shadow,
    run_combined_variant,
    run_exit_quality_variant,
    verify_combined_state_machine,
    verify_state_machine,
)
from fno_ai_paper_trading.strategies.composite import MultiIndicatorStrategy, bulk_signals

REPO = Path(__file__).resolve().parent.parent
DATASET = REPO / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


def _bar(iso: str, o, h, l, c) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=datetime.fromisoformat(iso),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=0,
        open_interest=0,
    )


def _domain_bars(n: int):
    stored = load_dataset(DATASET)
    return [b for b in stored.bars if b.timestamp.date().isoformat() < "2025-10-06"][:n]


# ---------------------------------------------------------------------------
# Combined overlay primitives: shadow replica arithmetic
# ---------------------------------------------------------------------------

def test_combined_shadow_fill_slippage_exact() -> None:
    sh = _Shadow()
    bar0 = _bar("2025-01-02T09:15:00", 98, 101, 98, 100)
    f = sh.apply_fill(bar=bar0, side="BUY", idx=0)
    assert f["action"] == "entry"
    assert f["fill_price"] == Decimal("100.100")
    assert sh.pos == 1
    assert sh.entry == Decimal("100.100")


def test_combined_shadow_exit_closes_long() -> None:
    sh = _Shadow()
    sh.apply_fill(bar=_bar("2025-01-02T09:15:00", 98, 101, 98, 100), side="BUY", idx=0)
    f = sh.apply_fill(bar=_bar("2025-01-02T09:20:00", 108, 110, 107, 109), side="SELL", idx=1)
    assert f["action"] == "exit"
    assert f["fill_price"] == Decimal("108.891")
    assert f["realized"] == Decimal("8.791")
    assert sh.pos == 0


def test_combined_shadow_protective_stop_exact() -> None:
    sh = _Shadow()
    sh.apply_fill(bar=_bar("2025-01-02T09:15:00", 98, 101, 98, 100), side="BUY", idx=0)
    stop = sh.apply_stop(bar=_bar("2025-01-02T09:25:00", 120, 120, 97, 118), idx=2)
    assert stop is not None
    assert stop["fill_price"] == Decimal("97.999902")
    assert stop["realized"] == Decimal("-2.100098")
    assert sh.pos == 0


def test_combined_shadow_risk_gate() -> None:
    sh = _Shadow()
    sh.day_realized["2025-01-02"] = Decimal("-9999.00")
    assert sh.risk_blocked(_bar("2025-01-02T09:20:00", 100, 101, 99, 100)) is False
    sh.day_realized["2025-01-02"] = Decimal("-10000.00")
    assert sh.risk_blocked(_bar("2025-01-02T09:20:00", 100, 101, 99, 100)) is True
    assert sh.risk_blocked(_bar("2025-01-03T09:15:00", 100, 101, 99, 100)) is False


def test_combined_same_direction_fill_is_a_contract_bug() -> None:
    sh = _Shadow()
    sh.apply_fill(bar=_bar("2025-01-02T09:15:00", 98, 101, 98, 100), side="BUY", idx=0)
    with pytest.raises(AssertionError):
        sh.apply_fill(bar=_bar("2025-01-02T09:20:00", 100, 101, 99, 100), side="BUY", idx=1)


# ---------------------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------------------

def test_combined_action_vocabulary() -> None:
    assert _INTENDED_COMBINED_ACTIONS == {
        "entry",
        "entry_reconfirmed",
        "entry_deferred",
        "entry_still_deferred",
        "dropped_pending",
        "exit",
        "exit_deferred",
        "hold_carry",
        "hold_aligned",
        "hold_deferred",
        "hold_flat",
        "noop_exit_ignored",
        "risk_blocked",
    }


# ---------------------------------------------------------------------------
# Integration on real data: proof + sync + determinism + gate edges
# ---------------------------------------------------------------------------

def run_slice():
    bars = _domain_bars(3000)
    strat = MultiIndicatorStrategy(mode="trend")
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    return bars, strat, config, engine


def test_combined_proof_and_sync_on_data_slice() -> None:
    bars, strat, config, engine = run_slice()
    base_signals = bulk_signals(bars, strat, latch=True)
    base_journal = []
    base_result = engine.run(bars, strat, config, signals=base_signals, journal=base_journal)
    var = run_combined_variant(bars, strat, config, engine)
    proof = verify_combined_state_machine(
        bars, strat, base_signals, var["series"], base_journal, var["journal"], var["shadow"]
    )
    assert proof["ok"] is True
    assert proof["raw_divergences"] == 0
    assert proof["bad_actions"] == []
    assert proof["entry_sanction_violations"] == []
    assert proof["direct_reversals"] == []
    assert var["sync"]["ok"] is True
    assert var["sync"]["fills_shadow"] == var["sync"]["fills_engine"]
    assert var["result"].orders_filled <= base_result.orders_filled


def test_combined_deterministic_across_two_runs() -> None:
    bars, strat, config, engine = run_slice()

    def _stable(journal):
        drop = {"entry_trade_id", "exit_trade_id"}
        return [{k: v for k, v in e.items() if k not in drop} for e in journal]

    a = run_combined_variant(bars, strat, config, engine)
    b = run_combined_variant(bars, strat, config, engine)
    assert a["result"].total_pnl == b["result"].total_pnl
    assert a["result"].num_trades == b["result"].num_trades
    assert _stable(a["journal"]) == _stable(b["journal"])
    assert a["series"] == b["series"]
    assert a["sync"]["ok"] is b["sync"]["ok"] is True


def test_combined_reduces_to_baseline_when_gates_open() -> None:
    bars, strat, config, engine = run_slice()
    base_signals = bulk_signals(bars, strat, latch=True)
    base_journal = []
    base_result = engine.run(bars, strat, config, signals=base_signals, journal=base_journal)
    var = run_combined_variant(
        bars, strat, config, engine, entry_adx=Decimal("0"), exit_adx=Decimal("999")
    )
    assert var["result"].total_pnl == base_result.total_pnl
    assert var["result"].num_trades == base_result.num_trades
    assert var["result"].orders_filled == base_result.orders_filled


def test_combined_no_trades_when_entry_gate_impossible() -> None:
    bars, strat, config, engine = run_slice()
    var = run_combined_variant(
        bars, strat, config, engine, entry_adx=Decimal("999"), exit_adx=Decimal("28")
    )
    assert var["result"].num_trades == 0
    assert var["result"].orders_filled == 0
    assert var["sync"]["ok"] is True
    assert var["result"].total_pnl == Decimal("0")


def test_combined_exit_gate_alone_reduces_round_trips() -> None:
    bars, strat, config, engine = run_slice()
    base_signals = bulk_signals(bars, strat, latch=True)
    base_journal = []
    base_result = engine.run(bars, strat, config, signals=base_signals, journal=base_journal)
    var = run_combined_variant(
        bars, strat, config, engine, entry_adx=Decimal("0"), exit_adx=Decimal("28")
    )
    assert var["result"].num_trades <= base_result.num_trades
    assert var["sync"]["ok"] is True


# ---------------------------------------------------------------------------
# Exit-only variant (C) still verified in isolation
# ---------------------------------------------------------------------------

def test_exit_only_variant_proof_ok() -> None:
    bars, strat, config, engine = run_slice()
    base_signals = bulk_signals(bars, strat, latch=True)
    base_journal = []
    base_result = engine.run(bars, strat, config, signals=base_signals, journal=base_journal)
    var = run_exit_quality_variant(bars, strat, config, engine, exit_adx=Decimal("28"))
    proof = verify_state_machine(
        bars, strat, base_signals, var["series"], base_journal, var["journal"], var["shadow"]
    )
    assert proof["ok"] is True
    assert var["sync"]["ok"] is True
    assert var["result"].num_trades < base_result.num_trades