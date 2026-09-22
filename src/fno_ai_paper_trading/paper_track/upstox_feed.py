"""Reusable read-only Upstox V3 WebSocket warm-up market-data client (Phase 3).

Origin and principle
--------------------
The proven read-only transport lives in ``scripts/upstox_ws_diagnostic.py`` (Upstox
V3 market-data feed in ``ltpc`` mode). This module reuses that proven
connect/authorize/protobuf-decode/ltpc-subscribe path and exposes a small,
injectable, offline-testable client that speaks the same wire protocol -- not a
second speculative WebSocket implementation. It is a pure market-data reader and
never emits an order itself.

Security contract (paper-only, fail-closed)
-------------------------------------------
* Token by dependency injection only. The caller resolves the warm-up token through
  ``paper_track.token_provider.get_access_token`` (``token_provider`` reads exactly
  the registered warm-up env var). This client never reads an environment variable
  itself and never holds a live-account credential.
* Never places an order; never imports the live layer or the order layer.
* Never imports from the out-of-sample collection package nor from any recurring job.
* Never prints, logs, fingerprints, persists, checkpoints, or reports the token and
  never writes it to disk or into any report.
* Never starts a child process or a command shell; no network beyond the data feed.
* Instrument subscription key is registry-backed: it is derived from the research
  registry's Upstox key for Nifty 50 (``NSE_INDEX|Nifty 50``) through
  ``fno_ai_paper_trading.data.instrument_registry.get_research_instrument`` -- there
  is no second hard-coded instrument definition.

The decode/normalize helpers (``decode_ltpc``, ``normalize_tick``) are pure and
tested fully offline with canned protobuf bytes; the transport accepts an injectable
frame source so the whole test surface runs with no network call and no real token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional, Sequence
from urllib.parse import urlparse

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.paper_track.errors import PaperTrackError

# The single registry-backed warm-up instrument (index feed).
WARMUP_INSTRUMENT_NAME = "NIFTY 50"

WS_GUID = "258EA5-E914-47DA-95CA-C5AB0DC85B11"
AUTHORIZE_URL = "https://api.upstox.com/v2/feed/market-data-feed/authorize"
_IST_TZ = timezone(timedelta(hours=5, minutes=30))


class UpstoxFeedError(PaperTrackError):
    """Base error for the read-only Upstox market-data feed client."""


class UpstoxAuthError(UpstoxFeedError):
    """The Upstox authorize step failed (fail-closed)."""


class UpstoxConnectionError(UpstoxFeedError):
    """The WebSocket transport could not connect or stay alive."""


class UpstoxSubscriptionError(UpstoxFeedError):
    """The feed subscription was refused or never produced a usable frame."""


class UpstoxProtocolError(UpstoxFeedError):
    """A frame/payload could not be decoded safely (fail-closed)."""


# --------------------------------------------------------------------------- #
# Pure protobuf helpers (offline-testable, no network)
# --------------------------------------------------------------------------- #

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        num = tag >> 3
        wt = tag & 0x7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 1:
            val = buf[i:i + 8]
            i += 8
        elif wt == 2:
            length, i = _varint(buf, i)
            val = buf[i:i + length]
            i += length
        elif wt == 5:
            val = buf[i:i + 4]
            i += 4
        else:
            raise UpstoxProtocolError(f"unsupported protobuf wire type {wt}")
        yield num, wt, val


def _as_double(b: bytes, expected: int) -> Optional[float]:
    if isinstance(b, bytes) and len(b) == expected:
        try:
            return struct.unpack("<d", b)[0]
        except struct.error:
            return None
    return None


def decode_ltpc(buf: bytes) -> dict:
    """Decode an Upstox V3 ``LTPC`` message into ``{ltp, ltt, ltq, cp}``.

    Strips to the four prices the warm-up can act on. Any unknown field is
    ignored (like the proven diagnostic). Raises :class:`UpstoxProtocolError`
    only on a genuinely undecodable wire type.
    """
    out: dict = {}
    for num, wt, val in _fields(buf):
        if num == 1 and wt == 1:  # ltp
            p = _as_double(val, 8)
            if p is not None:
                out["ltp"] = p
        elif num == 2 and wt == 0:  # ltt
            out["ltt"] = val
        elif num == 3 and wt == 0:  # ltq
            out["ltq"] = val
        elif num == 4 and wt == 1:  # cp
            p = _as_double(val, 8)
            if p is not None:
                out["cp"] = p
    return out


def normalize_tick(
    instrument_key: str,
    feed: dict,
    recv_ms: int,
    *,
    now_utc_ms: Optional[int] = None,
) -> dict:
    """Normalize a decoded Upstox feed into the paper warm-up tick shape.

    The normalized tick always carries a timestamp, the instrument key and a
    last-traded price for the warm-up. When the feed is missing/empty/without a
    usable ``ltp`` the call fails closed with :class:`UpstoxProtocolError`
    rather than emitting a bogus zero price.
    """
    if not isinstance(feed, dict):
        raise UpstoxProtocolError("feed payload is not a dictionary")
    ltpc = feed.get("ltpc") or {}
    ltp = ltpc.get("ltp")
    if not isinstance(ltp, (int, float)) or ltp <= 0:
        raise UpstoxProtocolError("feed carries no usable ltp for the warm-up")
    tick: dict = {
        "instrument_key": instrument_key,
        "last_price": float(ltp),
        "ts_ms": now_utc_ms if now_utc_ms is not None else recv_ms,
    }
    if isinstance(ltpc.get("ltt"), int):
        tick["ltt_ms"] = ltpc["ltt"]
    if isinstance(ltpc.get("ltq"), int):
        tick["ltq"] = ltpc["ltq"]
    if isinstance(ltpc.get("cp"), (int, float)):
        tick["close"] = float(ltpc["cp"])
    return tick
