"""Typed errors for the market-data layer.

All provider/vendor failures are surfaced as :class:`MarketDataError` subtypes so
business logic can catch one base type and remain vendor-agnostic.
"""
from __future__ import annotations


class MarketDataError(Exception):
    """Base class for all market-data layer failures."""


class ProviderConfigurationError(MarketDataError):
    """The provider is not configured correctly (e.g. missing credentials)."""


class AuthenticationError(MarketDataError):
    """The provider rejected the supplied credentials (401/403)."""


class RateLimitError(MarketDataError):
    """The provider rate-limited the request (429) and retries were exhausted."""


class InstrumentNotFoundError(MarketDataError):
    """The requested instrument has no data at the provider (e.g. bad token)."""


class MarketClosedError(MarketDataError):
    """An operation was attempted while the market session is closed."""


class UnavailableError(MarketDataError):
    """The provider service failed (network or 5xx) and retries were exhausted."""