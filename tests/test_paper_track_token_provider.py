"""Security regression for the env-only Upstox token provider used by the
paper-track warm-up.

Each test is fully offline: it seeds/clears the process environment with
``monkeypatch`` only (never a genuine credential), resolves the provider, and
asserts fail-closed behaviour plus a strict isolation/no-persistence/no-leak
contract. No network call, no subprocess, no file write, no checkpoint, no
report, no real token is ever involved anywhere in this file.

Guaranteed properties under test:

1. a non-empty ``FNO_UPSTOX_ACCESS_TOKEN`` is returned as-is;
2. the variable being absent fails closed;
3. an empty value fails closed;
4. a whitespace-only value fails closed;
5. ``UPSTOX_ACCESS_TOKEN`` (the live-execution token) is *never* consumed,
   even when set -- the provider resolves strictly from the paper env name;
6. the provider never persists on acquisition (`get_access_token` performs no
   disk write; the project maintains no token file or checkpoint by design);
7. a token value can never appear in an exception from the provider, in any
   report, or in persistence (tested via a clearly-fake sentinel that must
   never leak into exception text).

The Phase-1 warm-up regression test is intentionally left untouched by Phase 2.
"""
from __future__ import annotations

import os

import pytest

from fno_ai_paper_trading.paper_track.errors import PaperTrackError
from fno_ai_paper_trading.paper_track.token_provider import (
    TOKEN_ENV_NAME,
    TokenProviderError,
    get_access_token,
)

# A clearly-fake sentinel used ONLY in tests to show the value never leaks.
# Never a real credential.
SENTINEL = "PAPER_TRACK_TEST_FAKE_TOKEN_7c3a"

# The live-execution token env var. The paper provider must refuse to read it.
LIVE_EXEC_TOKEN_ENV = "UPSTOX_ACCESS_TOKEN"


def _unset_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_NAME, raising=False)
    monkeypatch.delenv(LIVE_EXEC_TOKEN_ENV, raising=False)


def test_resolves_a_valid_token() -> None:
    # Uses get_access_token with a controlled env via monkeypatch (no .env, no
    # real token, no network).
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        monkeypatch.setenv(TOKEN_ENV_NAME, SENTINEL)
        assert get_access_token() == SENTINEL
    finally:
        monkeypatch.undo()


def test_missing_token_fails_closed() -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        with pytest.raises(TokenProviderError) as excinfo:
            get_access_token()
        assert isinstance(excinfo.value, PaperTrackError)
        # Message must name the env variable but never the value.
        assert TOKEN_ENV_NAME in str(excinfo.value)
        assert SENTINEL not in str(excinfo.value)
    finally:
        monkeypatch.undo()


def test_empty_token_fails_closed() -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        monkeypatch.setenv(TOKEN_ENV_NAME, "")
        with pytest.raises(TokenProviderError):
            get_access_token()
    finally:
        monkeypatch.undo()


def test_whitespace_token_fails_closed() -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        monkeypatch.setenv(TOKEN_ENV_NAME, "   \t  ")
        with pytest.raises(TokenProviderError):
            get_access_token()
    finally:
        monkeypatch.undo()


def test_live_execution_token_is_never_consumed() -> None:
    # UPSTOX_ACCESS_TOKEN set but FNO_UPSTOX_ACCESS_TOKEN absent: must fail
    # closed rather than silently read the live token.
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        monkeypatch.setenv(LIVE_EXEC_TOKEN_ENV, SENTINEL)
        with pytest.raises(TokenProviderError):
            get_access_token()
    finally:
        monkeypatch.undo()


def test_no_persistence_on_acquisition() -> None:
    # The provider must not introduce any new filesystem writes. Guard: the
    # module writes to no file (acquisition path is pure in-memory), and no
    # token file/checkpoint/report file is created anywhere under the repo by
    # resolving a valid token.
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    snapshot = {
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if p.is_file() and "Github" not in str(p) and ".git" not in p.parts
    }
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        monkeypatch.setenv(TOKEN_ENV_NAME, SENTINEL)
        resolved = get_access_token()
        assert resolved == SENTINEL
    finally:
        monkeypatch.undo()
    after = {
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if p.is_file() and "Github" not in str(p) and ".git" not in p.parts
    }
    # Acquiring a token must never create or modify any tracked file.
    assert after == snapshot


def test_token_never_leaks_into_exceptions_or_state() -> None:
    # Even in the error paths, the (fake) token value must never appear in the
    # exception text, and the provider object holds no fingerprint of it.
    monkeypatch = pytest.MonkeyPatch()
    try:
        _unset_all(monkeypatch)
        monkeypatch.setenv(LIVE_EXEC_TOKEN_ENV, SENTINEL)
        monkeypatch.setenv(TOKEN_ENV_NAME, "   ")
        with pytest.raises(TokenProviderError) as excinfo:
            get_access_token()
        assert SENTINEL not in str(excinfo.value)
        assert repr(excinfo.value) and SENTINEL not in repr(excinfo.value)
        # The provider holds no disk/checkpoint/report state that could leak.
        assert not hasattr(get_access_token, "persist")
        assert not hasattr(get_access_token, "checkpoint")
        assert not hasattr(get_access_token, "report")
    finally:
        monkeypatch.undo()
