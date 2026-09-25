"""Runtime, in-memory-only Upstox credential provider for the fresh-OOS collector.

The analytics/data token is supplied by the account's **process environment**.
On Windows this includes the user-level environment variables
(``HKCU\\Environment``), which every process launched under that account
inherits -- including Task Scheduler tasks such as ``FNO_FreshOosCollector``
(``python.exe -m fno_ai_paper_trading.fresh_oos.scheduler --once``). The
secret therefore lives *outside* the repository, the task command line/
arguments/XML/working directory, the manifest, logs, reports and command
history.

Guarantees:

* resolved only at acquisition time and returned to the caller in memory only;
* never persisted, printed, logged or written to any project file;
* reported to operators exclusively as ``PRESENT`` / ``ABSENT``;
* fails closed with :class:`CredentialsUnavailableError` (``AUTH_REQUIRED``)
  when the token is unavailable;
* :meth:`RuntimeCredentialProvider.redact_text` strips the resolved token from
  any downstream exception text before it can reach the manifest.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from fno_ai_paper_trading.fresh_oos.errors import CredentialsUnavailableError

CREDENTIAL_ENV = "FNO_UPSTOX_ACCESS_TOKEN"
REDACTED = "<token-redacted>"

STATE_PRESENT = "PRESENT"
STATE_ABSENT = "ABSENT"


def redact_text(text: str, token: str, replacement: str = REDACTED) -> str:
    """Replace every occurrence of ``token`` in ``text`` (no-op if either is empty)."""
    if not text or not token:
        return text
    return text.replace(token, replacement)


@dataclass(frozen=True)
class CredentialState:
    """Operator-facing status: one of ``PRESENT``/``ABSENT`` plus a safe message.

    The token value and any fingerprint derived from it are never part of this
    object; ``str()`` and ``repr()`` therefore expose nothing more than the
    status string.
    """

    state: str
    message: str

    @property
    def is_present(self) -> bool:
        return self.state == STATE_PRESENT

    def __str__(self) -> str:
        return self.state


class RuntimeCredentialProvider:
    """Resolves the Upstox data token from the process environment only.

    ``environ`` is injectable for tests (defaults to the real process
    environment, which on Windows includes the account's user-level variables
    when launched by Task Scheduler under the same account).
    """

    def __init__(
        self,
        env_name: str = CREDENTIAL_ENV,
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.env_name = env_name
        self._environ = os.environ if environ is None else environ

    def probe(self) -> CredentialState:
        """PRESENT or ABSENT only -- never the value, never a fingerprint."""
        if (self._environ.get(self.env_name) or "").strip():
            return CredentialState(
                state=STATE_PRESENT,
                message=f"{self.env_name} is configured for this account",
            )
        return CredentialState(
            state=STATE_ABSENT,
            message=f"{self.env_name} is not set (acquisition is credential-gated)",
        )

    def resolve(self) -> str:
        """Return the token to the caller in memory only.

        Fails closed with :class:`CredentialsUnavailableError` (recorded as
        ``AUTH_REQUIRED``) when the token is absent; the error text names only
        the environment variable, never the value.
        """
        value = (self._environ.get(self.env_name) or "").strip()
        if not value:
            raise CredentialsUnavailableError(
                f"fresh-OOS acquisition is credential-gated: {self.env_name} is not set "
                "(the Upstox analytics/data token is required; it is never printed or stored)"
            )
        return value

    def redact_text(self, text: str) -> str:
        """Redact any occurrence of the resolved token from ``text``.

        When the token is absent there is nothing to redact and ``text`` is
        returned unchanged.
        """
        try:
            token = self.resolve()
        except CredentialsUnavailableError:
            return text
        return redact_text(text, token)