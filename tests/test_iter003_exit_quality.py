"""Iteration-003 tests: exit-quality overlay (shadow replica) + canonical metrics.

Every expected value below is hand-verified against the documented engine
semantics used by the Iteration-003 pre-registration (WS 6.4 / Research-Quality
Audit §10):

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

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.metrics import canonical
from fno_ai_paper_trading.evaluation.records import EvaluationConfig
from fno_ai_paper_trading.models.enums import InstrumentType, Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.day_batch_overlays import (
    _Shadow,
    _allow_exit,
    _closing,
    confirm_exits,
    run_exit_quality_variant,
    verify_execution_sync,
    verify_state_machine,
)
from fno_ai_paper_trading.strategies.base import SignalResult
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


def _signal(s: Signal, iso: str) -> SignalResult:
    return SignalResult(signal=s, instrument=_future(), timestamp=datetime.fromisoformat(iso))


def _domain_bars(n: int):
    stored = load_dataset(DATASET)
    return [b for b in stored.bars if b.timestamp.date().isoformat() < "2025-10-06"][:n]


# ---------------------------------------------------------------------------
# Canonical reporting taxonomy (audit Medium 1 + 2 remediation)
# ---------------------------------------------------------------------------

def test_canonical_metrics_terminology_maps_to_engine_counts() -> None:
    bars = _domain_bars(2500)
    strat = MultiIndicatorStrategy(mode="trend")
    config = EvaluationConfig().backtest()
    journal = []
    result = BacktestEngine().run(bars, strat, config, signals=bulk_signals(bars, strat, latch=True), journal=journal)
    s = canonical(result)
    assert s.fills == result.orders_filled            # executed fills (entries + exits)
    assert s.round_trips == result.num_trades          # completed entry+exit lifecycles
    assert s.wins == result.winning_trades
    assert s.losses == result.losing_trades
    assert s.wins + s.losses == s.round_trips          # wins/losses are closed RTs only
    assert s.round_trips <= s.fills                    # a RT needs >= 2 fills
    assert s.net == result.total_pnl                   # NET is the canonical figure


def test_canonical_net_identity_realized_plus_carry_minus_commissions() -> None:
    bars = _domain_bars(2500)
    strat = MultiIndicatorStrategy(mode="trend")
    config = EvaluationConfig().backtest()
    journal = []
    result = BacktestEngine().run(bars, strat, config, signals=bulk_signals(bars, strat, latch=True), journal=journal)
    s = canonical(result)
    # NET == REALIZED + CARRY MTM - COMMISSIONS (carry computed as the residual).
    assert s.realized_pnl + s.carry_mtm - s.commissions == s.net


def test_canonical_net_differs_from_realized_minus_commissions_when_position_carries() -> None:
    # Medium-2 regression guard: with a position still open at the final bar the
    # canonical NET includes carry MTM, so the legacy figure differs. Hand-built:
    # BUY at close 100 -> fill 100.100 (adverse slippage 0.001), commission
    # 100.100 * 0.0003 = 0.03003; final native close 115 -> unrealized 14.9.
    bars = [_bar("2025-01-02T09:15:00", 98, 101, 98, 100), _bar("2025-01-02T09:20:00", 114, 116, 113, 115)]
    signals = [_signal(Signal.BUY, "2025-01-02T09:15:00"), _signal(Signal.HOLD, "2025-01-02T09:20:00")]
    config = EvaluationConfig().backtest()
    result = BacktestEngine().run(bars, None, config, signals=signals, journal=[])
    s = canonical(result)
    assert s.fills == 1                      # the single opening fill
    assert s.round_trips == 0                # nothing closed
    assert s.wins == 0 and s.losses == 0
    assert s.realized_pnl == Decimal("0")    # no realized P&L at all
    assert s.carry_mtm == Decimal("14.9")    # final open position mark (115 - 100.100)
    assert s.net == result.total_pnl == Decimal("14.86997")
    assert s.net - s.commissions == Decimal("14.83994")
    # The legacy figure (realized - commissions) is -0.03003 here; NET differs by the carry.
    assert s.realized_pnl - s.commissions != s.net


# ---------------------------------------------------------------------------
# Overlay primitives: exit gate + shadow replica arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("adx", "expected"),
    [
        (None, True),            # ADX unavailable: allow, no trap
        (Decimal("27.999"), True),
        (Decimal("28"), False),  # pre-registered threshold: >= 28 defers
        (Decimal("44"), False),
    ],
)
def test_allow_exit_gate(adx, expected) -> None:
    assert _allow_exit(adx, Decimal("28")) is expected


def test_shadow_slippage_adjusted_entry_and_long_stop_exact() -> None:
    sh = _Shadow()
    bar0 = _bar("2025-01-02T09:15:00", 98, 101, 98, 100)
    f = sh.apply_fill(bar=bar0, side="BUY", idx=0)
    assert f["action"] == "entry"
    assert f["fill_price"] == Decimal("100.100")   # 100 * (1 + 0.001), adverse
    assert sh.pos == 1
    assert sh.entry == Decimal("100.100")
    assert sh.opened_idx == 0
    # Entry candle is excluded from the protective stop even on a trigger bar.
    assert sh.apply_stop(bar=_bar("2025-01-02T09:15:00", 50, 50, 45, 50), idx=0) is None
    # Next bar far from the stop: nothing fires.
    assert sh.apply_stop(bar=_bar("2025-01-02T09:20:00", 120, 122, 119, 121), idx=1) is None
    # low <= stop (98.098): fill at stop * (1 - 0.001) = 97.999902.
    stop = sh.apply_stop(bar=_bar("2025-01-02T09:25:00", 120, 120, 97, 118), idx=2)
    assert stop is not None
    assert stop["action"] == "stop"
    assert stop["fill_price"] == Decimal("97.999902")
    assert stop["realized"] == Decimal("-2.100098")
    assert sh.pos == 0 and sh.entry is None
    assert sh.day_realized["2025-01-02"] == Decimal("-2.100098")


def test_shadow_open_below_stop_fills_at_open_adverse() -> None:
    sh = _Shadow()
    sh.apply_fill(bar=_bar("2025-01-02T09:15:00", 98, 101, 98, 100), side="BUY", idx=0)
    stop = sh.apply_stop(bar=_bar("2025-01-02T09:20:00", 97, 98, 94, 97), idx=1)
    assert stop is not None
    assert stop["fill_price"] == Decimal("96.903")   # 97 * (1 - 0.001)
    assert stop["realized"] == Decimal("-3.197")     # 96.903 - 100.100


def test_shadow_risk_daily_loss_gate() -> None:
    sh = _Shadow()
    sh.day_realized["2025-01-02"] = Decimal("-9999.00")
    assert sh.risk_blocked(_bar("2025-01-02T09:20:00", 100, 101, 99, 100)) is False
    sh.day_realized["2025-01-02"] = Decimal("-10000.00")
    assert sh.risk_blocked(_bar("2025-01-02T09:20:00", 100, 101, 99, 100)) is True
    # One day's loss never blocks the next day.
    assert sh.risk_blocked(_bar("2025-01-03T09:15:00", 100, 101, 99, 100)) is False


def test_shadow_same_direction_fill_is_a_contract_bug() -> None:
    sh = _Shadow()
    sh.apply_fill(bar=_bar("2025-01-02T09:15:00", 98, 101, 98, 100), side="BUY", idx=0)
    with pytest.raises(AssertionError):
        sh.apply_fill(bar=_bar("2025-01-02T09:20:00", 100, 101, 99, 100), side="BUY", idx=1)


def test_shadow_flip_close_realized_exact() -> None:
    sh = _Shadow()
    sh.apply_fill(bar=_bar("2025-01-02T09:15:00", 98, 101, 98, 100), side="BUY", idx=0)
    f = sh.apply_fill(bar=_bar("2025-01-02T09:20:00", 108, 110, 107, 109), side="SELL", idx=1)
    assert f["action"] == "exit"
    assert f["fill_price"] == Decimal("108.891")     # 109 * (1 - 0.001)
    assert f["realized"] == Decimal("8.791")         # 108.891 - 100.100
    assert sh.pos == 0


def test_closing_includes_protective_stop_exits() -> None:
    journal = [
        {"timestamp": "T1", "order_side": "SELL", "fill_price": "99.5", "entry_trade_id": None,
         "exit_trade_id": "X1", "realized_pnl": "-0.6", "stop_fill": False},
        {"timestamp": "T2", "stop_fill": True, "stop_price": "98.09", "exit_trade_id": "X2",
         "realized_pnl": "-2.01"},
        {"timestamp": "T3", "order_side": "BUY", "fill_price": "100.1", "entry_trade_id": "E1",
         "exit_trade_id": None, "stop_fill": False},
        {"timestamp": "T4", "stop_fill": True, "stop_price": "97", "exit_trade_id": "X4",
         "realized_pnl": "-3.1"},
    ]
    rows = _closing(journal)
    assert [(r["ts"], r["side"], r["stop"]) for r in rows] == [
        ("T1", "SELL", False),
        ("T2", "SELL", True),
        ("T4", "SELL", True),
    ]


# ---------------------------------------------------------------------------
# Execution-sync (shadow vs real engine) — includes the SHORT-qty-0 quirk
# ---------------------------------------------------------------------------

def test_execution_sync_quantity_zero_reported_as_short_is_not_a_mismatch() -> None:
    # Regression guard: _pos_state reports a closed (qty 0) position as SHORT.
    shadow_rows = [{"ts": "T1", "emit": "SELL", "risk_blocked": False, "stop_fill": False,
                    "pos_after": 0}]
    journal = [{"timestamp": "T1", "order_side": "SELL", "fill_price": "99.0",
                "risk_approved": True, "position_after": {"side": "SHORT", "quantity": 0}}]
    sync = verify_execution_sync(journal, shadow_rows)
    assert sync["ok"] is True
    assert sync["mismatches"] == []
    assert (sync["fills_shadow"], sync["fills_engine"]) == (1, 1)


def test_execution_sync_detects_a_real_position_mismatch() -> None:
    shadow_rows = [{"ts": "T1", "emit": "SELL", "risk_blocked": False, "stop_fill": False,
                    "pos_after": 0}]
    journal = [{"timestamp": "T1", "order_side": "SELL", "fill_price": "99.0",
                "risk_approved": True, "position_after": {"side": "LONG", "quantity": 1}}]
    sync = verify_execution_sync(journal, shadow_rows)
    assert sync["ok"] is False
    assert sync["mismatches"][0]["detail"] == "position_after mismatch"


# ---------------------------------------------------------------------------
# Overlay on a real, deterministic data slice (entry preservation + course)
# ---------------------------------------------------------------------------

def run_slice_overlay(n=3000):
    bars = _domain_bars(n)
    strategy = MultiIndicatorStrategy(mode="trend")
    config = EvaluationConfig().backtest()
    engine = BacktestEngine()
    return bars, strategy, config, engine


def test_overlay_preserves_entry_sequence_and_syncs_on_data_slice() -> None:
    bars, strategy, config, engine = run_slice_overlay()
    var = run_exit_quality_variant(bars, strategy, config, engine, exit_adx=Decimal("28"))
    base_signals = bulk_signals(bars, strategy, latch=True)
    base_journal = []
    base_result = engine.run(bars, strategy, config, signals=base_signals, journal=base_journal)
    proof = verify_state_machine(
        bars, strategy, base_signals, var["series"], base_journal, var["journal"], var["shadow"]
    )
    assert proof["ok"] is True
    assert proof["raw_divergences"] == 0
    assert proof["entry_divergences_added"] == 0
    assert proof["entry_divergences_omitted"] == 0
    assert proof["exit_divergences_deferred"] >= 0
    assert proof["outside_intended_exits"] == []
    assert var["sync"]["ok"] is True
    assert var["sync"]["fills_shadow"] == var["sync"]["fills_engine"]
    # The overlay never changes the engine fill P&L model: a deferred exit means
    # fewer variant fills, never more.
    assert var["result"].orders_filled <= base_result.orders_filled


def test_overlay_deterministic_across_two_runs() -> None:
    bars, strategy, config, engine = run_slice_overlay()

    def _stable(journal):
        # Trade ids are random across runs; everything else must be identical.
        drop = {"entry_trade_id", "exit_trade_id"}
        return [
            {k: v for k, v in e.items() if k not in drop}
            for e in journal
        ]

    a = run_exit_quality_variant(bars, strategy, config, engine, exit_adx=Decimal("28"))
    b = run_exit_quality_variant(bars, strategy, config, engine, exit_adx=Decimal("28"))
    assert a["result"].total_pnl == b["result"].total_pnl
    assert a["result"].num_trades == b["result"].num_trades
    assert _stable(a["journal"]) == _stable(b["journal"])
    assert a["series"] == b["series"]
    assert a["sync"]["ok"] is b["sync"]["ok"] is True


def test_overlay_engine_run_is_identical_to_plain_engine_for_allowed_exits() -> None:
    bars, strategy, config, engine = run_slice_overlay(n=1500)
    # On a short window with ADX allowed everywhere (threshold far above any ADX),
    # every original exit is allowed and the variant must reproduce the baseline.
    var = run_exit_quality_variant(bars, strategy, config, engine, exit_adx=Decimal("999"))
    base_signals = bulk_signals(bars, strategy, latch=True)
    base_journal = []
    base_result = engine.run(bars, strategy, config, signals=base_signals, journal=base_journal)
    assert var["result"].total_pnl == base_result.total_pnl
    assert var["result"].num_trades == base_result.num_trades
    assert var["sync"]["ok"] is True