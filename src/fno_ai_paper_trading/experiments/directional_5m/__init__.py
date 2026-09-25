"""5M_DIRECTIONAL_OPTIONS_EXPERIMENT - an isolated paper experiment.

Approved contract (human-approved design decisions):

* **Signal source** - committed deterministic ``DonchianBreakout(20, 10)``
  (``strategies/research_candidates.py``). BUY -> BULLISH, SELL -> BEARISH,
  HOLD -> NEUTRAL. The signal source contract is unchanged from the 15M
  experiment (read-only reuse).
* **Decision cadence** - decision after **every completed 5-minute candle**
  (single-candle cadence, no 3-bar window). Decision instants ``09:20..15:15``
  (72/day), never at/after the ``15:20`` EOD flatten (the three candles
  completing at 15:20/15:25/15:30 are never decision candles, which preserves
  the hard always-flat-at-close safety invariant - parallel to the 15M rule
  that the final triple is never a decision window).
* **Neutral rule** - NEUTRAL always closes the open leg back to FLAT at the
  decision point (approved rule, no invented exit).
* **Option execution** - label-ONLY position intents on the NIFTY 50 index.
  ``CALL`` == long the index, ``PUT`` == short the index, under a documented
  zero-premium simplification: no strike, expiry or option premium is ever
  fabricated. Every fill/report carries ``zero_premium=True`` so the intent
  cannot be misread as a priced options trade.

Isolation guarantees: live_trading is never enabled, no scheduler, no token
or credential pathway, no shared store with the frozen MA(5,21) baseline, no
shared store with the 15M experiment, and no modification of any committed
baseline/OOS/15M component.
"""

from __future__ import annotations

__all__ = ["EXPERIMENT_ID", "Directional5MOptionsConfig", "Directional5MOptionsEngine"]

from fno_ai_paper_trading.experiments.directional_5m.executor import (
    Directional5MOptionsConfig,
    Directional5MOptionsEngine,
)

EXPERIMENT_ID: str = "5M_DIRECTIONAL_OPTIONS_EXPERIMENT"