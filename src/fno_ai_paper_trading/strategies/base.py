"""Strategy contract and signal result.

A strategy consumes a chronological series of :class:`~MarketPrice` bars and
returns a signal. Strategies are pure computation — they never touch a broker,
a portfolio or an order. Executing a signal is the responsibility of
:class:`~fno_ai_paper_trading.services.strategy_service.StrategyService`, which
routes through the risk manager and the paper broker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from fno_ai_paper_trading.models.enums import Signal
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice


@dataclass(frozen=True)
class SignalResult:
    """A strategy's recommendation about a single instrument/bar.

    ``reason`` is a human-readable justification; ``meta`` carries structured
    feature values (e.g. indicator levels) for logging and later analysis.
    """

    signal: Signal
    instrument: Instrument | None = None
    timestamp: datetime | None = None
    reason: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """True when the strategy wants to change a position (BUY/SELL)."""
        return self.signal in (Signal.BUY, Signal.SELL)


class Strategy(ABC):
    """Base class for all strategies. Strategies are stateless and deterministic."""

    name: str = "strategy"

    @abstractmethod
    def analyze(self, bars: list[MarketPrice]) -> SignalResult:
        """Return a signal for the most recent bar given the full history.

        ``bars`` must be chronological (oldest first). Implementations must be
        deterministic: the same input must always produce the same result.
        Invest on insufficient data by returning ``HOLD``.
        """