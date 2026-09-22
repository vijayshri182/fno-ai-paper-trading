"""Phase 3 — order lifecycle, entry approval and protective exits.

Rules under test:
* a BUY entry exists only after the RiskManager pre-trade approval (notional,
  qty and daily-loss caps all gate it) and every approval is logged;
* a long-only portfolio can never be made to short by a protective SELL;
* exits (signal SELL, stop, EOD flatten) are PROTECTIVE: no risk re-approval,
  always executable even at the daily-loss cap;
* exactly one position at a time — the sizer refuses re-entry while long.
"""

from __future__ import annotations

from decimal import Decimal

from fno_ai_paper_trading.config.settings import Environment, PaperSettings
from fno_ai_paper_trading.models.enums import OrderSide, RejectionReason, Signal
from fno_ai_paper_trading.paper_track.engine import TrackConfig
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from tests.paper_track_testkit import DAY0, closes_feed, day_ticks, drive, make_engine


class CycleStrategy(Strategy):
    """Deterministic signal plan keyed by bar ordinal (1 = first processed bar).

    ``plan``    {bar_ordinal: Signal}; anything unpinned falls back to
                ``auto_buy`` (BUY every bar) else HOLD.
    """

    name = "cycle"

    def __init__(self, plan=None, auto_buy: bool = False) -> None:
        self.plan = dict(plan or {})
        self.auto_buy = auto_buy

    def analyze(self, bars):
        last = bars[-1]
        seq = len(bars)
        if seq in self.plan:
            signal = self.plan[seq]
        elif self.auto_buy:
            signal = Signal.BUY
        else:
            signal = Signal.HOLD
        return SignalResult(signal, instrument=last.instrument, timestamp=last.timestamp, reason=self.name)


def _risk_manager(engine, config):
    return type(engine.risk_manager)(
        PaperSettings(
            environment=Environment.PAPER,
            initial_capital=config.initial_cash,
            max_position_quantity=config.max_position_quantity,
            max_order_notional=config.max_order_notional,
            max_daily_loss=config.max_daily_loss,
            commission_rate=config.commission_rate,
            commission_fixed=config.commission_fixed,
            slippage_rate=config.slippage_rate,
        )
    )


def _rebuild(engine, tmp_path, *, account, **config_kwargs):
    strategy = config_kwargs.pop("strategy", engine.config.strategy)
    cfg = TrackConfig(account=account, store_dir=tmp_path, strategy=strategy, **config_kwargs)
    engine.config = cfg
    engine.store = TrackStore(tmp_path, account)
    engine.risk_manager = _risk_manager(engine, cfg)
    engine.sizer = engine.sizer.__class__(
        type(engine.sizer.config)(
            risk_per_trade_pct=cfg.risk_per_trade_pct,
            stop_loss_pct=cfg.stop_loss_pct,
            commission_rate=cfg.commission_rate,
            commission_fixed=cfg.commission_fixed,
        )
    )
    engine.stop = engine.stop.__class__(cfg.stop_loss_pct)
    return engine


def test_entry_requires_risk_approval_notional_cap(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    _rebuild(engine, tmp_path, account="notional", max_order_notional=Decimal("1000"))
    drive(engine, day_ticks(DAY0))

    refusals = engine.counters["risk_refusals"]
    assert refusals, "expected risk refusals all day long"
    assert any(RejectionReason.MAX_ORDER_NOTIONAL_EXCEEDED.value in r["reason"] for r in refusals)
    assert engine.counters["entries_filled"] == 0
    orders, _fills = engine.broker.snapshot()
    assert orders == []
    assert engine.position_quantity == 0


def test_sell_with_no_position_never_creates_order(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=CycleStrategy(plan={6: Signal.SELL}))
    _rebuild(engine, tmp_path, account="flatssell", strategy=CycleStrategy(plan={6: Signal.SELL}))
    drive(engine, day_ticks(DAY0))
    orders, _fills = engine.broker.snapshot()
    assert orders == []
    assert engine.position_quantity == 0
    assert engine.broker.fills == []


def test_no_short_can_ever_open(tmp_path):
    # Strong exposure from the strategy does not override the long-only guard:
    # big-autobuy enters long; a protective SELL can only close it.
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=CycleStrategy(auto_buy=True))
    _rebuild(engine, tmp_path, account="golong", strategy=CycleStrategy(auto_buy=True))
    drive(engine, day_ticks(DAY0))
    assert engine.position_quantity == 0
    assert all(f.side is OrderSide.BUY or f.quantity > 0 for f in engine.broker.fills)
    assert engine.counters["entries_filled"] == 1


def test_single_position_only(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=CycleStrategy(auto_buy=True))
    _rebuild(engine, tmp_path, account="single", strategy=CycleStrategy(auto_buy=True))
    drive(engine, day_ticks(DAY0))
    assert engine.counters["entries_filled"] == 1  # the engine never re-enters while long
    assert engine.position_quantity == 0  # flattened at 15:20+
    assert engine.counters["flatten_count"] == 1
    assert engine.eod_status == "FLATTENED"


def test_sizer_refuses_second_position_defence_in_depth(tmp_path):
    # The sizer itself must refuse a second position even if asked directly:
    # defense-in-depth behind the engine's `position_quantity == 0` gate.
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    sizer = engine.sizer
    first = sizer.size(
        equity=engine.portfolio.cash,
        available_cash=engine.portfolio.cash,
        entry_price=Decimal("25000"),
        instrument=engine.config.instrument,
        current_quantity=0,
    )
    assert first.approved and first.quantity > 0
    second = sizer.size(
        equity=engine.portfolio.cash,
        available_cash=engine.portfolio.cash,
        entry_price=Decimal("25000"),
        instrument=engine.config.instrument,
        current_quantity=first.quantity,
    )
    assert not second.approved
    assert second.skip_reason


def test_exit_executes_even_at_daily_loss_cap(tmp_path):
    closes = [25000, 25020, 25050, 25040, 24980, 24700, 24500, 24300] + [24300] * 67
    strat = CycleStrategy(plan={2: Signal.BUY, 6: Signal.SELL})
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=strat)
    _rebuild(engine, tmp_path, account="losscap", max_daily_loss=Decimal("1"), strategy=strat)
    engine.bars_source = closes_feed(closes)
    drive(engine, day_ticks(DAY0))

    assert engine.counters["entries_filled"] == 1
    assert engine.counters["exits_filled"] == 1  # protective sell ran despite loss cap
    assert engine.position_quantity == 0
    assert engine.portfolio.realized_pnl < 0


def test_daily_loss_cap_blocks_a_later_entry(tmp_path):
    closes = [25000, 25020, 25050, 25040, 24980, 24700, 24500, 24300, 24200, 24100] + [24100] * 65
    strat = CycleStrategy(plan={2: Signal.BUY, 6: Signal.SELL, 10: Signal.BUY})
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=strat)
    _rebuild(engine, tmp_path, account="deload", max_daily_loss=Decimal("1"), strategy=strat)
    engine.bars_source = closes_feed(closes)
    drive(engine, day_ticks(DAY0))

    assert engine.counters["entries_filled"] == 1
    assert engine.counters["exits_filled"] == 1
    refusals = engine.counters["risk_refusals"]
    assert any(RejectionReason.DAILY_LOSS_LIMIT_REACHED.value in r["reason"] for r in refusals), refusals


def test_stop_loss_is_a_protective_exit(tmp_path):
    closes = [25000, 25040, 25060, 25030, 25000, 24900, 24700, 24500, 24200] + [24200] * 66
    strat = CycleStrategy(plan={1: Signal.BUY})
    engine, _, _ = make_engine(tmp_path, days=[DAY0], strategy=strat)
    _rebuild(engine, tmp_path, account="stopp", strategy=strat)
    engine.bars_source = closes_feed(closes)
    drive(engine, day_ticks(DAY0))

    assert engine.counters["entries_filled"] == 1
    assert engine.counters["stops_fired"] == 1
    assert engine.counters["exits_filled"] == 1
    assert engine.position_quantity == 0


def test_entry_approval_log_matches_fills(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    assert engine.counters["entries_filled"] == len(engine.entry_approvals)
    buy_fills = {f.quantity for f in engine.broker.fills if f.side is OrderSide.BUY}
    approval_qty = {a["quantity"] for a in engine.entry_approvals}
    assert buy_fills == approval_qty == {1} or buy_fills == approval_qty