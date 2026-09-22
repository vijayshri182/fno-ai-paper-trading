"""Deterministic 15-minute signal adapter over the frozen DonchianBreakout.

The approved signal source is the committed deterministic channel breakout
``strategies/research_candidates.py::DonchianBreakout`` (entry channel 20,
exit channel 10). At each 15-minute decision point it is evaluated over the
full chronological history of *completed* candles up to that moment; the last
emitted signal is mapped BUY -> BULLISH, SELL -> BEARISH, HOLD -> NEUTRAL.

The committed module is used read-only (never modified); the state machine is
entirely re-derived from the pure serial at every decision point so results
are deterministic and replayable.
"""

from __future__ import annotations

from typing import Sequence

from fno_ai_paper_trading.experiments.directional_15m.contract import (
    DONCHIAN_ENTRY_CHANNEL,
    DONCHIAN_EXIT_CHANNEL,
    Signal15m,
    signal_to_15m,
)
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.strategies.base import SignalResult
from fno_ai_paper_trading.strategies.research_candidates import DonchianBreakout

__all__ = ["donchian_signal_15m"]

_SIGNAL_SOURCE = "DonchianBreakout(20,10) from strategies/research_candidates.py (read-only)"


def donchian_signal_15m(
    history: Sequence[MarketPrice],
    *,
    entry_channel: int = DONCHIAN_ENTRY_CHANNEL,
    exit_channel: int = DONCHIAN_EXIT_CHANNEL,
) -> tuple[Signal15m, SignalResult | None]:
    """The 15-minute intent for a decision point, plus the raw signal result.

    An empty history (defensive; never reached at a real decision window)
    evaluates NEUTRAL so the experiment can never crash before the breakout
    warm-up (``max(entry, exit) + 1`` candles) is available.
    """
    if not history:
        return Signal15m.NEUTRAL, None
    strategy = DonchianBreakout(entry_channel=entry_channel, exit_channel=exit_channel)
    result = strategy.analyze(list(history))
    if result is None:
        return Signal15m.NEUTRAL, None
    mapping = _signal_result_mapping[result.signal.value]
    return mapping, result


_signal_result_mapping = {
    "BUY": Signal15m.BULLISH,
    "SELL": Signal15m.BEARISH,
    "HOLD": Signal15m.NEUTRAL,
}