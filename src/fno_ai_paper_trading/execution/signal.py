"""CALL vs PUT decision from the existing trend signal (WS 7.9).

The live experiment must reuse the existing strategy signal — no new strategy,
no algorithm change. The frozen MA(5,21) crossover runs on the *underlying*
index bars; its BUY/SELL recommendation maps deterministically to the F&O leg:

* ``Signal.BUY``  -> CALL (bullish)
* ``Signal.SELL`` -> PUT  (bearish)
* ``Signal.HOLD`` -> NONE (no trade — the manager refuses to place an order)

The mapping is a pure function; nothing here executes anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.strategies.base import SignalResult
from fno_ai_paper_trading.strategies.moving_average_cross import MovingAverageCrossStrategy


class CallPutSignal(str, Enum):
    """Directional leg of the F&O experiment derived from the trend signal."""

    CALL = "CALL"
    PUT = "PUT"
    NONE = "NONE"

    @property
    def order_side(self) -> OrderSide:
        if self is CallPutSignal.NONE:
            raise ValueError("NONE carries no order side")
        return OrderSide.BUY if self is CallPutSignal.CALL else OrderSide.SELL


def signal_to_call_put(signal: Signal) -> CallPutSignal:
    """Map a strategy signal onto the CALL/PUT leg (HOLD -> NONE, no trade)."""
    if signal is Signal.BUY:
        return CallPutSignal.CALL
    if signal is Signal.SELL:
        return CallPutSignal.PUT
    return CallPutSignal.NONE


@dataclass(frozen=True)
class CallPutDecision:
    """The directional decision plus the strategy evidence behind it."""

    leg: CallPutSignal
    signal_result: SignalResult
    bare_signal: Signal

    @property
    def actionable(self) -> bool:
        return self.leg is not CallPutSignal.NONE


def decide_call_put(
    bars,
    strategy: MovingAverageCrossStrategy | None = None,
) -> CallPutDecision:
    """Run the frozen MA(5,21) strategy over chronological bars and map to a leg.

    Deterministic and stateless. ``bars`` must be chronological (oldest first);
    insufficient data degrades to NONE (no trade).
    """
    strategy = strategy if strategy is not None else MovingAverageCrossStrategy()
    result = strategy.analyze(bars)
    return CallPutDecision(
        leg=signal_to_call_put(result.signal),
        signal_result=result,
        bare_signal=result.signal,
    )