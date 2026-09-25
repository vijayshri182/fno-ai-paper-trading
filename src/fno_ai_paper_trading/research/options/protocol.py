"""Provider-neutral option-chain source interface (Phase 5).

Business/research code depends only on :class:`OptionChainProvider`; concrete
adapters (Kite, Upstox, future brokers, replay stores) implement it. Nothing
here can reach a live order path — it is a read-only data boundary.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Mapping

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.options.errors import OptionDataError
from fno_ai_paper_trading.research.options.models import OptionChainSnapshot


class OptionChainDataUnavailableError(OptionDataError):
    """The provider has nothing for the requested (underlying, expiry, time)."""


class OptionChainProvider(ABC):
    """Contract any option-chain source must satisfy (read-only)."""

    provider_id: str = "UNKNOWN"

    @abstractmethod
    def supports(self, underlying: Instrument, expiry: date | None = None) -> bool:
        """Whether this provider can serve ``underlying`` (optionally one expiry)."""

    @abstractmethod
    def fetch_chain(
        self,
        underlying: Instrument,
        expiry: date | None = None,
        *,
        timestamp: datetime | None = None,
    ) -> OptionChainSnapshot:
        """Return the point-in-time chain snapshot for ``underlying``.

        Raises :class:`OptionChainDataUnavailableError` when nothing is held.
        Returned snapshots are deterministic for identical inputs (replay-safe).
        """


class StaticChainProvider(OptionChainProvider):
    """Replay/static adapter: serves pre-built snapshots from memory.

    Never fabricates anything: the returned snapshot is exactly what was given
    at construction (a reader may still call ``without_spot()`` itself if it
    wants to force spot absent).
    """

    def __init__(
        self,
        snapshots: Mapping[date, OptionChainSnapshot],
        *,
        provider_id: str = "static",
    ) -> None:
        self._snapshots = dict(snapshots)
        self.provider_id = provider_id

    def supports(self, underlying: Instrument, expiry: date | None = None) -> bool:
        if expiry is not None:
            return expiry in self._snapshots
        return len(self._snapshots) > 0

    def fetch_chain(
        self,
        underlying: Instrument,
        expiry: date | None = None,
        *,
        timestamp: datetime | None = None,
    ) -> OptionChainSnapshot:
        expiry = expiry if expiry is not None else (max(self._snapshots) if self._snapshots else None)
        if expiry is None or expiry not in self._snapshots:
            raise OptionChainDataUnavailableError(
                f"static provider holds no chain for {underlying.symbol} / {expiry}"
            )
        return self._snapshots[expiry]


__all__ = ["OptionChainDataUnavailableError", "OptionChainProvider", "StaticChainProvider"]