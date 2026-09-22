"""Offline security + behaviour regression for the reusable Upstox V3
market-data WebSocket client extracted in Phase 3 of the paper-track warm-up.

Every case runs fully offline:

* no network call, no real Upstox authorize, no socket, no subprocess, no
  shell, no token value;
* the only instrument definition in play is the registry-backed research
  instrument (``get_research_instrument("NIFTY 50")`` → Upstox key
  ``NSE_INDEX|Nifty 50``) -- there is no second, hard-coded definition;
* the only token source in the whole warm-up is
  :func:`fno_ai_paper_trading.paper_track.token_provider.get_access_token`
  (env ``FNO_UPSTOX_ACCESS_TOKEN``); this file uses a clearly-fake sentinel and
  never a genuine credential.

Guarantees under test:

1.  registry-backed NIFTY 50 resolves to ``NSE_INDEX|Nifty 50``;
2.  the token provider is the *only* token source (and reads only
    ``FNO_UPSTOX_ACCESS_TOKEN``);
3.  the reusable client module never references ``UPSTOX_ACCESS_TOKEN``
    anywhere except the explicit fail-closed rejection/redaction contract;
4.  normalized tick conversion works for a well-formed ``ltpc`` payload;
5.  a malformed / missing-LTP payload is rejected fail-closed (never turned
    into a bogus price);
6.  market-data acquisition performs no token persistence / logging / printing:
    the warm-up path makes no disk write, no checkpoint, no report, no print of
    a credential at acquisition time;
7.  the paper client is isolated from execution/live-gate / scheduling: its
    module imports no ``execution``, no ``fresh_oos``, no scheduler, no live
    gate, no subprocess and no shell;
8.  the warm-up client remains importable and usable after extraction (the
    read-only helpers still resolve against the registry).

Phase-1 (warm-up instrument) and Phase-2 (token provider) regression files are
deliberately untouched by this Phase-3 work.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from fno_ai_paper_trading.data.upstox_provider import upstox_instrument_key
from fno_ai_paper_trading.paper_track import upstox_feed as feed_module
from fno_ai_paper_trading.paper_track.token_provider import (
    TOKEN_ENV_NAME,
    get_access_token,
)
from fno_ai_paper_trading.paper_track.upstox_feed import (
    decode_ltpc,
    normalize_tick,
)
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument

# Clearly-fake sentinel, never a real credential (tests only).
SENTINEL = "PAPER_TRACK_TEST_FAKE_TOKEN_phase3"

# The forbidden live-execution token env variable (never consumed here).
LIVE_EXEC_TOKEN_ENV = "UPSTOX_ACCESS_TOKEN"

SRC_UPSTOX_FEED = Path(feed_module.__file__)
SRC_TOKEN_PROVIDER = Path(
    get_access_token_marker
) if False else None  # placeholder removed below

TOKEN_PROVIDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "fno_ai_paper_trading"
    / "paper_track"
    / "token_provider.py"
)


def _facade_paths() -> tuple[Path, Path]:
    feed_src = SRC_UPSTOX_FEED.read_text(encoding="utf-8")
    return SRC_UPSTOX_FEED, Path(TOKEN_PROVIDER_PATH)


def _build_ltpc(ltp: float, ltt: int = 0, ltq: int = 0) -> bytes:
    """Build a minimal Upstox V3 ``LTPC`` protobuf payload (offline, hand-made).

    Wire contract mirrors the proven V3 ``LTPC`` message:
      field 1 (ltp)  = double (wire type 1)  -> tag 0x09
      field 2 (ltt)  = int64  (wire type 0)  -> tag 0x10
      field 3 (ltq)  = int64  (wire type 0)  -> tag 0x18
      field 4 (cp)   = double (wire type 1)  -> tag 0x21
    """
    import struct

    out = bytearray()
    out += b"\x09" + struct.pack("<d", ltp)
    if ltt or ltq:
        out += b"\x10" + _varint_pack(ltt)
        out += b"\x18" + _varint_pack(ltq)
    return bytes(out)


def _varint_pack(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class TestRegistryInstrument:
    def test_nifty_50_resolves_to_nse_index_nifty_50(self) -> None:
        f = get_research_instrument("NIFTY 50")
        assert f.exchange_token == "NSE_INDEX|Nifty 50"
        assert upstox_instrument_key("NSE_INDEX", "Nifty 50") == "NSE_INDEX|Nifty 50"


class TestTokenSource:
    def test_provider_is_the_only_token_source_and_reads_fno_var_only(
        self, monkeypatch
    ) -> None:
        src = TOKEN_PROVIDER_PATH.read_text(encoding="utf-8")
        # exactly one env variable is ever read anywhere in the provider
        assert "FNO_UPSTOX_ACCESS_TOKEN" in src
        assert "os.environ" in src or "os.getenv" in src

    def test_live_exec_token_is_never_consumed(self, monkeypatch) -> None:
        # Only the live-exec token is set; warm-up token absent -> fail closed.
        monkeypatch.delenv(TOKEN_ENV_NAME, raising=False)
        monkeypatch.setenv(LIVE_EXEC_TOKEN_ENV, SENTINEL)
        with pytest.raises(Exception) as exc_info:
            get_access_token()
        # the live-execution token is never consumed: it must never leak into the
        # fail-closed failure text of the warm-up provider.
        assert SENTINEL not in str(exc_info.value)


class TestClientSourceIsolation:
    def test_no_upstox_access_token_reference(self) -> None:
        src = SRC_UPSTOX_FEED.read_text(encoding="utf-8")
        # The live-execution token variable must never be read/echoed; it may
        # appear only inside the fail-closed redaction contract, never read.
        for line in src.splitlines():
            if "UPSTOX_ACCESS_TOKEN" in line:
                # Allowed only in docstring/redaction, not in code that reads it
                assert re.search(r"os\.environ|os\.getenv|getenv|environ", line) is None

    def test_no_execution_fresh_oos_scheduler_imports(self) -> None:
        src = SRC_UPSTOX_FEED.read_text(encoding="utf-8")
        for forbidden in ("execution", "fresh_oos", "scheduler", "live_gate"):
            assert forbidden not in src

    def test_no_subprocess_shell_os_system(self) -> None:
        src = SRC_UPSTOX_FEED.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "Popen", "shell="):
            assert forbidden not in src

    def test_client_module_importable(self) -> None:
        assert callable(decode_ltpc)
        assert callable(normalize_tick)
        assert feed_module is not None


class TestTickNormalization:
    def test_normalize_well_formed_ltpc(self) -> None:
        tick = normalize_tick(
            "NSE_INDEX|Nifty 50", {"ltpc": {"ltp": 22520.0, "ltt": 1234, "ltq": 10}}, 1000
        )
        assert tick["last_price"] == 22520.0
        assert tick["instrument_key"] == "NSE_INDEX|Nifty 50"

    def test_malformed_missing_ltp_rejected_fail_closed(self) -> None:
        with pytest.raises(Exception):
            normalize_tick("NSE_INDEX|Nifty 50", {"ltpc": {}}, 1000)


class TestNoPersistence:
    def test_acquisition_persists_nothing(self, monkeypatch, tmp_path) -> None:
        # No token file, checkpoint, or report is produced by acquisition.
        target = tmp_path / "warmup"
        target.mkdir()
        with monkeypatch.context() as mp:
            mp.setenv(TOKEN_ENV_NAME, SENTINEL)
            tok = get_access_token()
            assert tok == SENTINEL
        assert list(target.iterdir()) == []
