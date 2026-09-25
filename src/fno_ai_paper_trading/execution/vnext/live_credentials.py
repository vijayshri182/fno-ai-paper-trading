"""Separate live-trading credential provider for the isolated vNext layer.

The lived-trading access token is a distinct secret from the analytics/data
token used by the data layer (``FNO_UPSTOX_*``). This provider resolves ONLY
the ``UPSTOX_*`` execution credentials from the process environment — it never
reads, falls back to, or shares the data token, and it has no order/network
path of its own (a provider cannot place orders).

Guarantees (mirror the fresh-OOS collector's credential discipline for the
execution secret):

* resolved at use-time and returned to the caller in memory only;
* never persisted, printed, logged or written to any project file;
* reported to operators exclusively as ``PRESENT`` / ``ABSENT``;
* fails closed with :class:`LiveCredentialsUnavailableError` when the token is
  absent — before any HTTP request could be attempted by a consumer;
* :func:`redact_text` strips the resolved token from exception text so it never
  reaches audit payloads, manifests, reasons or logs;
* ``repr``/``str`` of credentials and state expose no token material.

Isolation: this module imports ONLY the vNext tree (no ``fresh_oos``, no
``data.errors``, no ``execution/upstox``), so the static isolation audit keeps
passing and this provider can never be autowired into the WS 7.9 harness.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from fno_ai_paper_trading.execution.vnext.errors import VNextError

LIVE_CREDENTIAL_ENV = "UPSTOX_ACCESS_TOKEN"
LIVE_API_KEY_ENV = "UPSTOX_API_KEY"
LIVE_BASE_URL_ENV = "UPSTOX_BASE_URL"
LIVE_TIMEOUT_ENV = "UPSTOX_TIMEOUT_SECONDS"

DEFAULT_LIVE_BASE_URL = "https://api.upstox.com"
DEFAULT_LIVE_TIMEOUT_SECONDS = 10.0
REDACTED = "<token-redacted>"

STATE_PRESENT = "PRESENT"
STATE_ABSENT = "ABSENT"


def redact_text(text: str, token: str, replacement: str = REDACTED) -> str:
    """Replace every occurrence of ``token`` in ``text`` (no-op if either is empty)."""
    if not text or not token:
        return text
    return text.replace(token, replacement)


class LiveCredentialsUnavailableError(VNextError):
    """The execution token is missing; consumers must fail closed."""


@dataclass(frozen=True)
class LiveCredentialState:
    """Operator-facing status: ``PRESENT``/``ABSENT`` plus a safe message."""

    state: str
    message: str

    @property
    def is_present(self) -> bool:
        return self.state == STATE_PRESENT

    def __str__(self) -> str:
        return self.state


@dataclass(frozen=True)
class LiveTradingCredentials:
    """The immutable execution-credential bundle for the vNext broker adapter.

    Only ``access_token`` authenticates order requests. ``api_key`` is the
    Upstox application/client id (public; optional ``x-api-key`` header). The
    token is never logged: ``repr`` redacts it and ``headers`` never echoes it
    back to the caller.
    """

    access_token: str = ""
    api_key: str = ""
    base_url: str = DEFAULT_LIVE_BASE_URL
    timeout_seconds: float = DEFAULT_LIVE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "access_token", (self.access_token or "").strip())
        object.__setattr__(self, "api_key", (self.api_key or "").strip())
        object.__setattr__(
            self, "base_url", (self.base_url or DEFAULT_LIVE_BASE_URL).rstrip("/")
        )
        scaled = float(self.timeout_seconds)
        if scaled <= 0:
            raise ValueError("timeout_seconds must be > 0")
        object.__setattr__(self, "timeout_seconds", scaled)

    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    def headers(self, json_body: bool = False) -> dict[str, str]:
        """Request headers, failing closed without the token (before any HTTP)."""
        if not self.configured:
            raise LiveCredentialsUnavailableError(
                "live-trading credentials are not configured: "
                f"{LIVE_CREDENTIAL_ENV} is not set "
                "(the execution token is required; it is never printed or stored)"
            )
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "LiveTradingCredentials":
        """Build from the ``UPSTOX_*`` variables (injected env for tests).

        Reads ONLY the live-trading execution names — never
        ``FNO_UPSTOX_ACCESS_TOKEN``. An absent token simply yields
        ``configured=False``; consumers fail closed at use time.
        """
        source = os.environ if environ is None else environ
        raw_timeout = str(source.get(LIVE_TIMEOUT_ENV, "") or DEFAULT_LIVE_TIMEOUT_SECONDS)
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_LIVE_TIMEOUT_SECONDS
        return cls(
            access_token=str(source.get(LIVE_CREDENTIAL_ENV, "")).strip(),
            api_key=str(source.get(LIVE_API_KEY_ENV, "")).strip(),
            base_url=str(source.get(LIVE_BASE_URL_ENV, "") or DEFAULT_LIVE_BASE_URL),
            timeout_seconds=timeout,
        )

    def __repr__(self) -> str:
        return (
            f"LiveTradingCredentials(base_url={self.base_url!r}, "
            f"api_key_configured={bool(self.api_key)}, "
            "access_token=<redacted>)"
        )


class LiveCredentialProvider:
    """Resolves the execution token from the process environment only.

    ``environ`` is injectable for tests. The provider has no order/network
    surface: it can only answer presence and hand the credentials to consumers
    in memory.
    """

    def __init__(
        self, environ: Mapping[str, str] | None = None
    ) -> None:
        self._environ = os.environ if environ is None else environ

    def probe(self) -> LiveCredentialState:
        """PRESENT or ABSENT only — never the value, never a fingerprint."""
        if (self._environ.get(LIVE_CREDENTIAL_ENV) or "").strip():
            return LiveCredentialState(
                state=STATE_PRESENT,
                message=f"{LIVE_CREDENTIAL_ENV} is configured for this account",
            )
        return LiveCredentialState(
            state=STATE_ABSENT,
            message=f"{LIVE_CREDENTIAL_ENV} is not set (live trading is credential-gated)",
        )

    def resolve(self) -> LiveTradingCredentials:
        """Return the credentials to the caller in memory only.

        Fails closed with :class:`LiveCredentialsUnavailableError` when the
        execution token is absent; the error text names only the environment
        variable, never the value, and never falls back to ``FNO_UPSTOX_*``.
        """
        credentials = LiveTradingCredentials.from_env(self._environ)
        if not credentials.configured:
            raise LiveCredentialsUnavailableError(
                "live-trading is credential-gated: "
                f"{LIVE_CREDENTIAL_ENV} is not set "
                "(the execution token is required; it is never printed or stored)"
            )
        return credentials

    def redact_text(self, text: str) -> str:
        """Redact any occurrence of the resolved token from ``text``."""
        try:
            credentials = self.resolve()
        except LiveCredentialsUnavailableError:
            return text
        return redact_text(text, credentials.access_token)


__all__ = [
    "LIVE_CREDENTIAL_ENV",
    "LIVE_API_KEY_ENV",
    "LIVE_BASE_URL_ENV",
    "LIVE_TIMEOUT_ENV",
    "DEFAULT_LIVE_BASE_URL",
    "REDACTED",
    "STATE_PRESENT",
    "STATE_ABSENT",
    "redact_text",
    "LiveCredentialsUnavailableError",
    "LiveCredentialState",
    "LiveTradingCredentials",
    "LiveCredentialProvider",
]