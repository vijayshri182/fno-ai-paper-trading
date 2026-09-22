"""15M_DIRECTIONAL_OPTIONS_EXPERIMENT - an isolated paper experiment.

Approved contract (human-approved design decisions):

* **Signal source** - committed deterministic ``DonchianBreakout(20, 10)``
  (``strategies/research_candidates.py``). BUY -> BULLISH, SELL -> BEARISH,
  HOLD -> NEUTRAL at 15-minute decision points.
* **Neutral rule** - NEUTRAL always closes the open leg back to FLAT at the
  15-minute point (approved rule, no invented exit).
* **Option execution** - label-ONLY position intents on the NIFTY 50 index.
  ``CALL`` == long the index, ``PUT`` == short the index, under a documented
  zero-premium simplification: no strike, expiry or option premium is ever
  fabricated. Every fill/report carries ``zero_premium=True`` so the intent
  cannot be misread as a priced options trade.

Isolation guarantees: live_trading is never enabled, no scheduler, no token
or credential pathway, no shared store with the frozen MA(5,21) baseline, and
no modification of any committed baseline/OOS component.
"""

from __future__ import annotations

__all__ = ["EXPERIMENT_ID", "DirectionalOptionsConfig", "DirectionalOptionsEngine"]

from fno_ai_paper_trading.experiments.directional_15m.executor import (
    DirectionalOptionsConfig,
    DirectionalOptionsEngine,
)

EXPERIMENT_ID: str = "15M_DIRECTIONAL_OPTIONS_EXPERIMENT"