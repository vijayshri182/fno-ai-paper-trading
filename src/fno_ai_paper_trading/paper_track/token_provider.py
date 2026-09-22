"""Environment-only, fail-closed Upstox access-token provider for the
paper-only daily track.

Security contract (paper-only warm-up):
* Reads **only** ``FNO_UPSTOX_ACCESS_TOKEN`` from the process environment.
* **Never** reads ``UPSTOX_ACCESS_TOKEN`` (the live-execution token): the two
  credential layers are deliberately separated, and this provider can therefore
  never accidentally consume the execution token.
* **Never** accepts a token through any CLI argument.
* **Never** hard-codes, prints, logs, fingerprints, or persists a token.
* **Never** writes the token to disk, to a checkpoint, or into a report.
* **Never** exposes the token in an exception message.
* Fails closed (raises :class:`TokenProviderError`) when the variable is
  absent, empty, or whitespace-only.
* Makes no network call and spawns no subprocess.

The token exists in Python memory only for as long as it is needed by the
warm-up and is never handed to any persistence layer. The variable name here
is the exact one the paper-track runner already reads
(``paper_track/runner.py`` → ``cmd_upstox``), so the warm-up token resolution
can never diverge from the acquisition path.
"""
from __future__ import annotations

import os

from fno_ai_paper_trading.paper_track.errors import PaperTrackError

# Env-only credential for the read-only research/data warm-up. The live
# execution token (``UPSTOX_ACCESS_TOKEN``) belongs to a different layer and is
# deliberately never read here.
TOKEN_ENV_NAME = "FNO_UPSTOX_ACCESS_TOKEN"


class TokenProviderError(PaperTrackError):
    """Fail-closed error raised when the Upstox warm-up token is unavailable.

    The message names the environment variable but never echoes any token
    value, so a secret can never leak through an exception path.
    """


def _read_token() -> str:
    """Read and trim the token without ever exposing its value.

    :raises TokenProviderError: when the variable is absent, empty, or
        whitespace-only.
    """
    value = os.environ.get(TOKEN_ENV_NAME, "").strip()
    if not value:
        raise TokenProviderError(
            f"Upstox access token unavailable: set {TOKEN_ENV_NAME} in .env "
            "(never pass it on any command line)"
        )
    return value


def get_access_token() -> str:
    """Return the Upstox read-only data token from the process environment.

    Failure is always closed (:class:`TokenProviderError`); the resolved token
    is returned in memory only and is never printed, logged, fingerprinted,
    checkpointed, or written to disk/report.
    """
    return _read_token()
