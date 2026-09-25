"""Construct validated broker-ready order requests from mapped steps.

Bridges the pure ``OrderStep`` (action+leg+side+type) and the resolved
``OptionContract`` into an ``OrderRequest``. The broker adapter must NEVER
infer the contract from the transaction side — the contract is always supplied
in full.
"""
from __future__ import annotations

from fno_ai_paper_trading.execution.vnext.broker import OrderRequest
from fno_ai_paper_trading.execution.vnext.contract import OptionContract
from fno_ai_paper_trading.execution.vnext.mapping import OrderStep


def build_order_request(
    step: OrderStep, contract: OptionContract, quantity: int
) -> OrderRequest:
    """Combine a mapped step with its resolved contract into one order."""
    return OrderRequest(
        order_action=step.order_action,
        contract=contract,
        tx_side=step.tx_side,
        quantity=quantity,
    )