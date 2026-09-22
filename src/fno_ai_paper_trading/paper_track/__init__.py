"""Daily Paper Trading Track (isolated fail-closed simulation).

A self-contained, paper-only, 5-minute long-only NIFTY 50 strategy loop with
adversarial data screening, protective exits, atomic checkpointing, crash/restart
safety and deterministic simulations. It is deliberately independent of every
live-execution and validation subsystem in this repository.
"""

from __future__ import annotations

__version__ = "1.0.0"