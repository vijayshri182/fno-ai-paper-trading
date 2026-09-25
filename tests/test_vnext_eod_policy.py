"""EOD/overnight policy tests (isolated, deterministic).

No network, no Upstox, no paper/live orders, no credentials. Pins Policy A
(intraday flatten) and Policy B (multi-day) divergence plus the flatten-order
mapping consistency with the pinned transition table.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import pytest

from fno_ai_paper_trading.execution.vnext.broker import OrderRequest
from fno_ai_paper_trading.execution.vnext.eod_policy import (
    EodDecisionKind,
    IntradayFlattenPolicy,
    MultiDayHoldPolicy,
    flatten_orders,
)
from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OptionLeg,
    OrderAction,
    PositionState,
    SignalDirection,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.mapping import plan_orders
from fno_ai_paper_trading.execution.vnext.order_request import build_order_request

from vnext_helpers import make_contract

TUE = datetime(2026, 9, 22)  # Tuesday 2026-09-22


def _t(hour: int, minute: int = 0) -> datetime:
    return TUE.replace(hour=hour, minute=minute)


def _sun() -> datetime:
    return datetime(2026, 9, 20, 10, 0)  # Sunday


class TestIntradayFlattenPolicy:
    def test_cutoff_validation(self):
        with pytest.raises(ValueError):
            IntradayFlattenPolicy(
                session_open=dtime(9, 15),
                session_close=dtime(15, 30),
                flatten_cutoff=dtime(9, 0),
            )

    def test_midday_hold(self):
        policy = IntradayFlattenPolicy()
        decision = policy.decide(PositionState.LONG_CALL, _t(10, 0))
        assert decision.kind is EodDecisionKind.HOLD
        assert decision.position is PositionState.LONG_CALL

    def test_default_cutoff_after_hold_window(self):
        policy = IntradayFlattenPolicy()
        assert policy.decide(PositionState.LONG_PUT, _t(15, 24)).kind is EodDecisionKind.HOLD
        assert policy.decide(PositionState.LONG_PUT, _t(15, 25)).kind is EodDecisionKind.FLATTEN_DUE

    def test_after_close_flatten_due(self):
        policy = IntradayFlattenPolicy()
        for hour, minute in ((15, 30), (16, 0), (20, 0), (23, 30)):
            assert policy.decide(PositionState.LONG_CALL, _t(hour, minute)).kind is EodDecisionKind.FLATTEN_DUE

    def test_before_open_flatten_due(self):
        policy = IntradayFlattenPolicy()
        assert policy.decide(PositionState.LONG_CALL, _t(8, 59)).kind is EodDecisionKind.FLATTEN_DUE

    def test_weekend_flatten_due(self):
        policy = IntradayFlattenPolicy()
        assert policy.decide(PositionState.LONG_CALL, _sun()).kind is EodDecisionKind.FLATTEN_DUE

    def test_flat_position_is_always_hold(self):
        policy = IntradayFlattenPolicy()
        for now in (_t(20, 59), _sun(), _t(10, 0)):
            assert policy.decide(PositionState.FLAT, now).kind is EodDecisionKind.HOLD

    def test_can_open_bounds(self):
        policy = IntradayFlattenPolicy()
        assert policy.can_open(_t(9, 15))
        assert policy.can_open(_t(10, 0))
        assert policy.can_open(_t(14, 0))
        assert policy.can_open(_t(15, 24))
        assert not policy.can_open(_t(15, 25))
        assert not policy.can_open(_t(15, 30))
        assert not policy.can_open(_t(8, 0))
        assert not policy.can_open(_sun())


class TestMultiDayHoldPolicy:
    def test_always_holds(self):
        policy = MultiDayHoldPolicy()
        assert policy.decide(PositionState.LONG_CALL, _t(15, 30)).kind is EodDecisionKind.HOLD
        assert policy.decide(PositionState.LONG_PUT, _sun()).kind is EodDecisionKind.HOLD
        assert policy.decide(PositionState.FLAT, _t(10, 0)).kind is EodDecisionKind.HOLD

    def test_can_open_anytime(self):
        policy = MultiDayHoldPolicy()
        assert policy.can_open(_t(15, 30))
        assert policy.can_open(_sun())

    def test_policies_are_distinct_classes(self):
        assert type(IntradayFlattenPolicy()) is not type(MultiDayHoldPolicy())


class TestFlattenOrders:
    def test_long_call_flatten_sells_call(self):
        steps = flatten_orders(PositionState.LONG_CALL)
        assert len(steps) == 1
        assert steps[0].order_action is OrderAction.CLOSE
        assert steps[0].tx_side is TxSide.SELL
        mapped, target = plan_orders(PositionState.LONG_CALL, SignalDirection.FLAT)
        assert target is PositionState.FLAT
        assert mapped == list(steps)

    def test_long_put_flatten_sells_put(self):
        steps = flatten_orders(PositionState.LONG_PUT)
        assert len(steps) == 1
        assert steps[0].tx_side is TxSide.SELL

    def test_flat_has_no_flatten_orders(self):
        assert flatten_orders(PositionState.FLAT) == ()

    def test_flatten_steps_build_valid_close_requests(self):
        for state, leg in (
            (PositionState.LONG_CALL, OptionLeg.CALL),
            (PositionState.LONG_PUT, OptionLeg.PUT),
        ):
            contract = make_contract(leg)
            assert contract.contract_type is (ContractType.CE if leg is OptionLeg.CALL else ContractType.PE)
            for step in flatten_orders(state):
                request = build_order_request(step, contract, 75)
                assert isinstance(request, OrderRequest)
                assert request.order_action is OrderAction.CLOSE
                assert request.tx_side is TxSide.SELL


class TestEodSemanticsCrossChecks:
    def test_flatten_is_closing_not_opening(self):
        for state in (PositionState.LONG_CALL, PositionState.LONG_PUT):
            assert all(step.order_action is OrderAction.CLOSE for step in flatten_orders(state))

    def test_policy_output_never_attempts_an_open(self):
        policy = IntradayFlattenPolicy()
        allowed = {EodDecisionKind.HOLD, EodDecisionKind.FLATTEN_DUE}
        for state in (PositionState.LONG_CALL, PositionState.LONG_PUT, PositionState.FLAT):
            for now in (_t(10, 0), _t(15, 30), _sun()):
                assert policy.decide(state, now).kind in allowed