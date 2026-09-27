"""WP-8 — transparent statutory cost schedule and gross-to-net attribution.

The schedule is REPORTING-ONLY: it never changes how commissions are charged,
so the reconciliation identity (cash = initial + gross - total_commission)
stays frozen. These tests pin the schedule's math recomputed from fills.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fno_ai_paper_trading.models.enums import InstrumentType, OrderSide
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.order import Fill
from fno_ai_paper_trading.paper_track.accounting import total_commission
from fno_ai_paper_trading.paper_track.costs import (
    cost_attribution,
    estimate_statutory_costs,
    reference_price,
    slippage_amount,
    turnover_by_side,
)
from tests.paper_track_testkit import DAY0, day_ticks, drive, make_engine

FUT = Instrument(
    symbol="NIFTY26SEPFUT",
    instrument_type=InstrumentType.FUTURE,
    underlying_symbol="NIFTY 50",
    expiry=date(2026, 9, 24),
    multiplier=25,
)


def _fill(price: str, qty: int, side: OrderSide, commission: str) -> Fill:
    return Fill(
        order_id=f"ORD-{side.value}-{qty}",
        instrument=FUT,
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal(commission),
        filled_at=datetime(2026, 9, 21, 10, 0),
    )


def test_reference_price_inverts_paper_broker_slippage():
    rate = Decimal("0.001")
    ref = Decimal("18750.50")
    buy_fill = ref * (Decimal("1") + rate)
    sell_fill = ref * (Decimal("1") - rate)
    assert reference_price(buy_fill, OrderSide.BUY, rate) == ref
    assert reference_price(sell_fill, OrderSide.SELL, rate) == ref


def test_slippage_amount_equals_rate_times_reference_notional():
    rate = Decimal("0.001")
    qty = 2
    ref = Decimal("18750.50")
    fill = _fill(str(ref * (Decimal("1") - rate)), qty, OrderSide.SELL, "0")
    expected = Decimal(qty) * FUT.multiplier * ref * rate
    assert slippage_amount([fill], rate) == expected
    assert slippage_amount([], rate) == Decimal("0")


def test_slippage_zero_when_rate_zero():
    fill = _fill("18750.50", 1, OrderSide.BUY, "0")
    assert slippage_amount([fill], Decimal("0")) == Decimal("0")


def test_turnover_by_side_splits_fills():
    buy = _fill("100.00", 1, OrderSide.BUY, "0")
    sell = _fill("200.00", 2, OrderSide.SELL, "0")
    b, s = turnover_by_side([buy, sell])
    assert b == Decimal(100) * FUT.multiplier
    assert s == Decimal(200) * 2 * FUT.multiplier


def test_statutory_estimate_exact_components():
    # buy = 1 crore, sell = 2 crore
    out = estimate_statutory_costs(Decimal("10000000"), Decimal("20000000"))
    assert out["sebi_turnover_fee"] == Decimal("30.00")  # 3 crore * 0.0001%
    assert out["exchange_transaction_charges"] == Decimal("570.00")  # 3 crore * 1.90/lakh
    assert out["stt_sell_side"] == Decimal("2500.00")  # 2 crore * 0.0125%
    assert out["stamp_duty_buy_side"] == Decimal("200.00")  # 1 crore * 0.002%
    assert out["total"] == Decimal("3300.00")


def test_statutory_estimate_zero_without_turnover():
    out = estimate_statutory_costs(Decimal("0"), Decimal("0"))
    assert all(v == Decimal("0") for v in out.values())


def test_cost_attribution_balance_and_under_modeled():
    fills = [  # buy turnover 1 crore, sell turnover 2 crore (see turnover test)
        _fill("400000.00", 1, OrderSide.BUY, "25.00"),
        _fill("400000.00", 2, OrderSide.SELL, "50.00"),
    ]
    # statutories = 3300.00; commission 6000 -> balance 2700, fully covered
    attr = cost_attribution(fills, commission_total=Decimal("6000.00"), slippage_rate=Decimal("0"))
    assert attr["provisional"] is True
    assert attr["charges"]["modeled_total_commission"] == "6000.00"
    assert attr["charges"]["brokerage_model_balance"] == "2700.00"
    assert attr["charges"]["under_modeled"] is False
    assert attr["charges"]["statutory_estimated"]["total"] == "3300.00"
    assert Decimal(attr["slippage"]["amount"]) == Decimal("0")

    # commission only 1000 -> statutory (3300) exceeds aggregate: clamped, flagged
    attr = cost_attribution(fills, commission_total=Decimal("1000.00"), slippage_rate=Decimal("0"))
    assert attr["charges"]["brokerage_model_balance"] == "0.00"
    assert attr["charges"]["under_modeled"] is True


def test_report_embeds_cost_schedule(tmp_path):
    engine, _, _ = make_engine(tmp_path, days=[DAY0])
    drive(engine, day_ticks(DAY0))
    report = engine.report()
    schedule = report["accounting"]["cost_schedule"]
    assert schedule["provisional"] is True
    assert schedule["charges"]["modeled_total_commission"] == str(
        total_commission(engine.broker.fills)
    )
    assert Decimal(schedule["slippage"]["amount"]) >= Decimal("0")