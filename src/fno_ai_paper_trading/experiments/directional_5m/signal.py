"""Deterministic 5-minute signal adapter over the frozen DonchianBreakout.

The approved signal source is the committed deterministic channel breakout
``strategies/research_candidates.py::DonchianBreakout`` (entry channel 20,
exit channel 10) - the same frozen, unchanged contract the 15-minute
directional experiment uses. At each decision instant (every completed
5-minute candle) it is evaluated over the full chronological history of
*completed* candles up to that moment; the last emitted signal is mapped
BUY -> BULLISH, SELL -> BEARISH, HOLD -> NEUTRAL.

The 15M signal adapter is reused read-only (never modified): the only thing
that differs here is the name under which the caller receives it, so the 5M
namespace stays self-documenting.
"""

from __future__ import annotations

from fno_ai_paper_trading.experiments.directional_15m.signal import (
    donchian_signal_15m as donchian_signal_5m,
)

__all__ = ["donchian_signal_5m"]

_SIGNAL_SOURCE = (
    f"DonchianBreakout({20},{10}) from strategies/research_candidates.py "
    "(read-only, frozen, unchanged from the 15M experiment)"
)