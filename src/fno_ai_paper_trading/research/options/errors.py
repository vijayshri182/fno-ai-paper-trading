"""Typed errors for the options research layer.

All failures inherit the market-data base type so business logic can catch the
vendor-agnostic base class, mirroring ``data/errors.py``.
"""
from __future__ import annotations

from fno_ai_paper_trading.data.errors import MarketDataError


class OptionDataError(MarketDataError):
    """Base class for options research-layer failures."""


class OptionNormalizationError(OptionDataError):
    """A provider payload could not be deterministically normalized.

    Raised for structurally malformed rows (bad timestamp, unknown side,
    non-numeric required identity, unparseable expiry). This is *not* the same
    as a semantic quality verdict: semantic problems are classified by the
    validation layer into ``VALID / INVALID / UNAVAILABLE``.
    """


__all__ = ["OptionDataError", "OptionNormalizationError"]