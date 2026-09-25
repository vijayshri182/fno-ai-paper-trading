"""Typed errors for the fresh-OOS collector plus exception -> status mapping.

Everything here is network-free and deterministic. Status strings are the
closed set from :mod:`fno_ai_paper_trading.fresh_oos.protocol`; the collector
uses this mapping to translate provider/exceptions into recorded run statuses
so no raw exception detail (and no credential) ever leaks into reports.
"""
from __future__ import annotations

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    InstrumentNotFoundError,
    MarketDataError,
    ProviderConfigurationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.fresh_oos.protocol import (
    STATUS_AUTH_REQUIRED,
    STATUS_DATA_CONFLICT,
    STATUS_DATA_INVALID,
    STATUS_INCOMPLETE,
    STATUS_LOCKED,
    STATUS_NETWORK_ERROR,
    STATUS_PROTOCOL_VIOLATION,
    STATUS_RATE_LIMITED,
    STATUS_SOURCE_ERROR,
)


class FreshOosError(Exception):
    """Base class for all fresh-OOS collector errors."""


class ProtocolViolationError(FreshOosError):
    """A target date or bar lies on/before the fresh-OOS boundary."""


class DataConflictError(FreshOosError):
    """A stored day exists but its content hash differs from the provider's."""


class DataInvalidError(FreshOosError):
    """Stored artifacts failed local integrity verification."""


class IncompleteDataError(FreshOosError):
    """A fetched day is missing bars from the expected session coverage."""


class CredentialsUnavailableError(FreshOosError):
    """No Upstox analytics/data token is configured (credential-gated exit)."""


class LockedError(FreshOosError):
    """Another collector process holds the run lock."""


def status_from_exception(exc: BaseException) -> str:
    """Map any exception to a closed-set fresh-OOS run status."""
    if isinstance(exc, LockedError):
        return STATUS_LOCKED
    if isinstance(exc, ProtocolViolationError):
        return STATUS_PROTOCOL_VIOLATION
    if isinstance(exc, DataConflictError):
        return STATUS_DATA_CONFLICT
    if isinstance(exc, DataInvalidError):
        return STATUS_DATA_INVALID
    if isinstance(exc, IncompleteDataError):
        return STATUS_INCOMPLETE
    if isinstance(exc, (CredentialsUnavailableError, AuthenticationError, ProviderConfigurationError)):
        return STATUS_AUTH_REQUIRED
    if isinstance(exc, RateLimitError):
        return STATUS_RATE_LIMITED
    if isinstance(exc, (UnavailableError, OSError, TimeoutError, ConnectionError)):
        return STATUS_NETWORK_ERROR
    if isinstance(exc, InstrumentNotFoundError):
        return STATUS_SOURCE_ERROR
    if isinstance(exc, MarketDataError):
        return STATUS_SOURCE_ERROR
    return STATUS_SOURCE_ERROR


def safe_message(exc: BaseException) -> str:
    """A human-safe one-line description that never contains credentials."""
    text = str(exc)
    if not text.strip():
        return type(exc).__name__
    lowered = text.lower()
    for secret_marker in ("bearer ", "access_token=", "token=", "secret", "password"):
        if secret_marker in lowered:
            return "error detail redacted (possible credential in message)"
    return text[:300]