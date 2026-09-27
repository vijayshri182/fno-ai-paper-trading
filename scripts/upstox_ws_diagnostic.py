"""Isolated READ-ONLY diagnostic for the Upstox Market Data Feed V3 WebSocket.

PURPOSE
-------
Determine whether TRUE CURRENT NIFTY 50 market data can be obtained from the
Upstox Market Data Feed V3 WebSocket (the REST historical feed was found to be
stale through 2026-09-11 while NSE is open on 2026-09-15).

This script is a DIAGNOSTIC ONLY. It:
  * never places an order and never touches the order/execution path;
  * subscribes ONLY to the instrument key passed via ``--key``
    (default ``NSE_INDEX|Nifty 50``);
  * emits read-only ``GET`` calls and a read-only market-data WebSocket
    subscription (mode ``ltpc``);
  * writes one evidence JSON file under ``reports/`` when ``--out`` is set;
  * never runs any algorithm, never trades, and never launches the CRON loop.

CREDENTIALS
-----------
Uses ONLY ``FNO_UPSTOX_ACCESS_TOKEN`` (the analytics/data-layer token). The
execution-layer token ``UPSTOX_ACCESS_TOKEN`` is deliberately never read.

NO DEPENDENCIES
---------------
Runs on a bare stdlib interpreter: token is parsed from ``.env`` directly
(no ``dotenv``), the WebSocket client is a minimal RFC 6455 implementation,
and the V3 ``FeedResponse`` protobuf payload is decoded with a minimal wire
parser. The authorize REST call shells out to ``curl.exe`` exactly like
``src/fno_ai_paper_trading/utils/http.py`` (urllib is blocked by the
Cloudflare WAF in front of api.upstox.com).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:  # best-effort use of the project's market-hours logic; stdlib fallback below
    from fno_ai_paper_trading.data.market_hours import NSE_TZ, market_phase  # type: ignore
except Exception:  # pragma: no cover - defensive fallback on broken import graph
    from datetime import time as _time

    NSE_TZ = timezone(timedelta(hours=5, minutes=30))

    def market_phase(dt):  # type: ignore[misc]
        if 9 <= dt.hour < 15 or (dt.hour == 15 and dt.minute <= 30):
            if (dt.hour, dt.minute) >= (9, 15):
                return "OPEN"
        return "CLOSED"


AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_KEY = "NSE_INDEX|Nifty 50"
DEFAULT_TOKEN_ENV = "FNO_UPSTOX_ACCESS_TOKEN"

VERDICT_VERIFIED = "LIVE WEBSOCKET VERIFIED"
VERDICT_NO_NIFTY = "WEBSOCKET CONNECTS BUT NO NIFTY DATA"
VERDICT_AUTH_FAILED = "WEBSOCKET AUTHENTICATION FAILED"
VERDICT_SUB_FAILED = "WEBSOCKET SUBSCRIPTION FAILED"
VERDICT_IMPL_BLOCKED = "WEBSOCKET IMPLEMENTATION BLOCKED"
VERDICT_STALE = "WEBSOCKET CONNECTS BUT FEED STALE"

_TIMEOUT = object()  # recv-frame sentinel
_CLOSED = object()  # recv-frame sentinel (peer closed / transport lost)


class DiagnosticError(Exception):
    pass


def _ist(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=NSE_TZ)


def _now_utc_ms() -> int:
    return int(time.time() * 1000)


def _redact_ws_uri(uri: str) -> str:
    """Display form of the socket URI without the single-use ``code`` param."""
    p = urlparse(uri)
    params = [x for x in (p.query.split("&") if p.query else []) if not x.startswith("code=")]
    query = ("?" + "&".join(params)) if params else ""
    return f"{p.scheme}://{p.hostname}{p.path}{query}"


def read_token(env_name: str) -> str | None:
    """Read ``env_name`` from the environment, else from ``<repo>/.env``."""
    value = os.getenv(env_name)
    if value and value.strip():
        return value.strip()
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == env_name:
                return val.strip().strip('"').strip("'")
    return None


# --------------------------------------------------------------------------- #
# Authorize REST call (curl.exe backend, mirroring utils/http.py)
# --------------------------------------------------------------------------- #

def authorize_ws_url(token: str, timeout: float = 25.0) -> str:
    """GET the authorized wss:// URI (read-only). Raises DiagnosticError."""
    with tempfile.TemporaryDirectory(prefix="fno_ws_auth_") as tmp:
        header_file = os.path.join(tmp, "request_headers.txt")
        dump_file = os.path.join(tmp, "response_headers.txt")
        with open(header_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"Authorization: Bearer {token}\n")
            fh.write("Accept: application/json\n")

        cmd = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--max-time",
            str(int(timeout)),
            "-X",
            "GET",
            "-H",
            f"@{header_file}",
            "-D",
            dump_file,
            "-o",
            "-",
            AUTHORIZE_URL,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise DiagnosticError(
                f"authorize curl failed (exit {result.returncode}): "
                f"{result.stderr.decode('utf-8', errors='replace').strip()}"
            )
        status, _headers = _curl_status(dump_file)
        if status is None:
            raise DiagnosticError("authorize returned no HTTP status line")
        if status >= 400:
            raise DiagnosticError(
                f"authorize HTTP {status}: "
                f"{result.stdout.decode('utf-8', errors='replace')[:400]}"
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError:
            raise DiagnosticError("authorize returned non-JSON body")
    data = payload.get("data") if payload.get("status") == "success" else None
    uri = (data or {}).get("authorized_redirect_uri") if isinstance(data, dict) else None
    if not uri:
        raise DiagnosticError(f"authorize unexpected payload: status={payload.get('status')}")
    return uri


def _curl_status(dump_path: str) -> tuple[int | None, dict[str, str]]:
    try:
        raw = Path(dump_path).read_bytes()
    except OSError:
        return None, {}
    status = None
    headers: dict[str, str] = {}
    for line in raw.split(b"\r\n"):
        text = line.strip(b"\r\n").decode("utf-8", errors="replace")
        if not text:
            continue
        if text.startswith("HTTP/"):
            parts = text.split(" ", 2)
            try:
                status = int(parts[1])
            except (IndexError, ValueError):
                status = None
            continue
        if ":" in text:
            name, _, value = text.partition(":")
            headers[name.strip().lower()] = value.strip()
    return status, headers


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 WebSocket client (stdlib only)
# --------------------------------------------------------------------------- #

class WSConnectionError(Exception):
    pass


class MiniWebSocket:
    """A minimal RFC 6455 WebSocket client speaking to the wss:// URI."""

    def __init__(self, uri: str, timeout: float = 15.0, verify_ssl: bool = True):
        self.uri = uri
        self.timeout = timeout  # per-frame read timeout used by recv_frame
        self.buf = bytearray()
        self._sock: socket.socket | None = None
        self.peer = _redact_ws_uri(uri)

        p = urlparse(uri)
        if p.scheme != "wss":
            raise WSConnectionError(f"unsupported scheme {p.scheme!r} (wss required)")
        self.host = p.hostname or ""
        self.port = p.port or 443
        self.path = p.path or "/"
        if p.query:
            self.path += "?" + p.query

        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        context = ssl.create_default_context()
        if not verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._sock = context.wrap_socket(raw, server_hostname=self.host)
        self._handshake(timeout)
        print(f"[ws] connected TLS={self._sock.version()} to {p.hostname}:{self.port}")

    # -- handshake --------------------------------------------------------- #
    def _handshake(self, timeout: float) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        assert self._sock is not None
        self._sock.sendall(request.encode("ascii"))

        response = bytearray()
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in response:
            if time.time() > deadline:
                raise WSConnectionError("handshake timed out")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSConnectionError("handshake closed by peer")
            response += chunk
        head, _, rest = bytes(response).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        if " 101 " not in lines[0]:
            raise WSConnectionError(f"handshake failed: {lines[0]}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1(f"{key}{WS_GUID}".encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept", "").strip() != expected:
            raise WSConnectionError("Sec-WebSocket-Accept mismatch")
        self.buf = bytearray(rest)

    # -- frames ------------------------------------------------------------- #
    def _ensure(self, needed: int) -> None:
        while len(self.buf) < needed:
            assert self._sock is not None
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSConnectionError("connection closed while reading frame")
            self.buf += chunk

    def _read_exact_frame(self) -> tuple[int, bytes]:
        """Read one complete frame; caller toggles the socket timeout."""
        self._ensure(2)
        b0, b1 = self.buf[0], self.buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            self._ensure(offset + 2)
            length = struct.unpack(">H", bytes(self.buf[offset:offset + 2]))[0]
            offset += 2
        elif length == 127:
            self._ensure(offset + 8)
            length = struct.unpack(">Q", bytes(self.buf[offset:offset + 8]))[0]
            offset += 8
        mask_key = b""
        if masked:
            self._ensure(offset + 4)
            mask_key = bytes(self.buf[offset:offset + 4])
            offset += 4
        self._ensure(offset + length)
        payload = bytes(self.buf[offset:offset + length])
        del self.buf[:offset + length]
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if not fin:
            raise WSConnectionError(
                "fragmented data frame received (not supported by this minimal diagnostic client)"
            )
        return opcode, payload

    def recv_frame(self) -> object | tuple[int, bytes]:
        """Blocks up to ``self.timeout``; returns (opcode, payload) | sentinel."""
        assert self._sock is not None
        self._sock.settimeout(self.timeout)
        try:
            return self._read_exact_frame()
        except socket.timeout:
            return _TIMEOUT
        except (OSError, WSConnectionError):
            return _CLOSED

    def __send(self, first: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([first])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        assert self._sock is not None
        self._sock.sendall(bytes(header + mask + masked))

    def send_text(self, text: str) -> None:
        self.__send(0x81, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self.__send(0x82, data)

    def send_pong(self, payload: bytes) -> None:
        self.__send(0x8A, payload[:125])

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._sock is not None:
            try:
                payload = struct.pack(">H", code) + reason.encode("utf-8")
                mask = os.urandom(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                self._sock.sendall(bytes([0x88, 0x80 | len(payload)]) + mask + masked)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# --------------------------------------------------------------------------- #
# Minimal protobuf wire decoder for the V3 FeedResponse schema
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


def _fields(buf: bytes):
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        num = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 1:
            val = buf[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            val = buf[i:i + ln]
            i += ln
        elif wt == 5:
            val = buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield num, wt, val


def _as_double(b: bytes) -> float | None:
    return struct.unpack("<d", b)[0] if len(b) == 8 else None


def decode_ltpc(buf: bytes) -> dict:
    out: dict = {}
    for num, wt, val in _fields(buf):
        if num == 1 and wt == 1:
            out["ltp"] = _as_double(val)
        elif num == 2 and wt == 0:
            out["ltt"] = val
        elif num == 3 and wt == 0:
            out["ltq"] = val
        elif num == 4 and wt == 1:
            out["cp"] = _as_double(val)
    return out


def decode_feed(buf: bytes) -> dict:
    out: dict = {"request_mode": None}
    for num, wt, val in _fields(buf):
        if num == 1 and wt == 2:
            out["ltpc"] = decode_ltpc(val)
        elif num == 4 and wt == 0:
            out["request_mode"] = val
    return out


def _map_entry(buf: bytes) -> tuple[str, bytes, int | None]:
    key = ""
    value = b""
    varint_value = None
    for num, wt, val in _fields(buf):
        if num == 1 and wt == 2:
            key = val.decode("utf-8", errors="replace")
        elif num == 2 and wt == 2:
            value = val
        elif num == 2 and wt == 0:
            varint_value = val
    return key, value, varint_value


def decode_market_info(buf: bytes) -> dict:
    out: dict = {"segment_status": {}, "pre_open_session_status": {}}
    for num, wt, val in _fields(buf):
        if num == 1 and wt == 2:  # map<string, MarketStatus>
            key, _value, enum = _map_entry(val)
            if key:
                out["segment_status"][key] = enum
        elif num == 3 and wt == 2:  # map<string, StatusInfo>
            key, value, _enum = _map_entry(val)
            if key:
                status = {"status": None, "updated_time": None}
                for n2, wt2, v2 in _fields(value):
                    if n2 == 1 and wt2 == 2:
                        status["status"] = v2.decode("utf-8", errors="replace")
                    elif n2 == 2 and wt2 == 0:
                        status["updated_time"] = v2
                out["pre_open_session_status"][key] = status
    return out


def decode_feed_response(buf: bytes) -> dict:
    out: dict = {"type": None, "feeds": {}, "current_ts": None, "market_info": None}
    for num, wt, val in _fields(buf):
        if num == 1 and wt == 0:
            out["type"] = val  # 0=initial_feed, 1=live_feed, 2=market_info
        elif num == 2 and wt == 2:  # map<string, Feed>
            key, value, _enum = _map_entry(val)
            if key:
                out["feeds"][key] = decode_feed(value)
        elif num == 3 and wt == 0:
            out["current_ts"] = val
        elif num == 4 and wt == 2:
            out["market_info"] = decode_market_info(val)
    return out


# --------------------------------------------------------------------------- #
# Aggregation + freshness + verdict
# --------------------------------------------------------------------------- #

def aggregate_5m(ticks: list[dict]) -> list[dict]:
    buckets: dict[datetime, dict] = {}
    for tk in ticks:
        dt = _ist(tk["ltt"])
        minute = (dt.minute // 5) * 5
        key = dt.replace(minute=minute, second=0, microsecond=0)
        b = buckets.get(key)
        if b is None:
            b = {
                "bucket_ist": key.isoformat(),
                "open": tk["ltp"],
                "high": tk["ltp"],
                "low": tk["ltp"],
                "close": tk["ltp"],
                "volume": 0,
                "ticks": 0,
                "first_ltt_ms": tk["ltt"],
                "last_ltt_ms": tk["ltt"],
            }
            buckets[key] = b
        b["high"] = max(b["high"], tk["ltp"])
        b["low"] = min(b["low"], tk["ltp"])
        b["close"] = tk["ltp"]
        b["volume"] += tk.get("ltq") or 0
        b["ticks"] += 1
        b["last_ltt_ms"] = max(b["last_ltt_ms"], tk["ltt"])
    return sorted(buckets.values(), key=lambda c: c["bucket_ist"])


def classify_verdict(
    *,
    auth_ok: bool,
    connected: bool,
    saw_frame: bool,
    tick_count: int,
    is_open: bool,
    last_tick: dict | None,
    fresh_tol_s: int,
    lag_tol_s: int,
) -> tuple[str, str]:
    """Return (verdict, freshness_label)."""
    if not auth_ok:
        return VERDICT_AUTH_FAILED, "N/A"
    if not connected:
        return VERDICT_IMPL_BLOCKED, "N/A"
    if not saw_frame:
        return VERDICT_SUB_FAILED, "N/A"
    if tick_count == 0:
        return VERDICT_NO_NIFTY, "N/A"
    assert last_tick is not None
    if not is_open:
        return VERDICT_STALE, "MARKET CLOSED"
    now = _now_utc_ms()
    age = (now - last_tick["ltt"]) / 1000.0
    lag = (last_tick["recv_ms"] - last_tick["ltt"]) / 1000.0
    if age <= fresh_tol_s and lag <= lag_tol_s:
        return VERDICT_VERIFIED, "FRESH"
    return VERDICT_STALE, f"STALE (ltt_age={age:.0f}s lag={lag:.0f}s)"


def _fmt_ist(ms: int) -> str:
    return _ist(ms).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------- #
# Main diagnostic run
# --------------------------------------------------------------------------- #

def run_diagnostic(args: argparse.Namespace) -> dict:
    started_ms = _now_utc_ms()
    report: dict = {
        "tool": "upstox_ws_diagnostic",
        "run_id": f"WS_DIAG_{int(started_ms)}",
        "started_ist": _fmt_ist(started_ms),
        "instrument_key": args.key,
        "mode": args.mode,
        "duration_s": args.duration,
        "auth": {"url": AUTHORIZE_URL, "status": None, "redirect_uri_redacted": None},
        "connection": {"ok": False, "host": None, "error": None},
        "subscription": {"sent_at_ist": None, "guid": None},
        "frames": {"total": 0, "text": 0, "binary": 0, "ping": 0, "pong": 0},
        "feed_types": {"initial_feed": 0, "live_feed": 0, "market_info": 0},
        "market": {"phase": None, "is_open": None, "segment_status": None},
        "ticks": [],
        "errors": [],
        "verdict": None,
        "freshness": None,
    }

    ist_now = datetime.now(NSE_TZ).replace(tzinfo=None)
    report["market"]["phase"] = str(market_phase(ist_now))
    report["market"]["is_open"] = report["market"]["phase"] == "OPEN" or "OPEN" in str(
        report["market"]["phase"]
    )

    # 1) authorize ---------------------------------------------------------- #
    token = read_token(args.token_env if args.token_env else DEFAULT_TOKEN_ENV)
    if not token:
        report["errors"].append("no data-layer token found")
        report["verdict"] = VERDICT_IMPL_BLOCKED
        return report
    print(f"[auth] token source: {args.token_env or DEFAULT_TOKEN_ENV} (data layer, read-only)")

    try:
        ws_uri = authorize_ws_url(token, timeout=args.connect_timeout)
        report["auth"]["status"] = "success"
        report["auth"]["redirect_uri_redacted"] = _redact_ws_uri(ws_uri)
        print(f"[auth] authorize OK -> {report['auth']['redirect_uri_redacted']}")
    except DiagnosticError as exc:
        report["auth"]["status"] = "failed"
        report["errors"].append(f"authorize: {exc}")
        report["verdict"] = VERDICT_AUTH_FAILED
        print(f"[auth] FAILED: {exc}")
        return report
    auth_ok = True

    # 2) connect ------------------------------------------------------------ #
    ws: MiniWebSocket | None = None
    try:
        ws = MiniWebSocket(ws_uri, timeout=args.frame_timeout, verify_ssl=not args.insecure)
        report["connection"]["ok"] = True
        report["connection"]["host"] = ws.peer
    except (WSConnectionError, OSError, ssl.SSLError, socket.gaierror) as exc:
        report["connection"]["error"] = str(exc)
        report["errors"].append(f"websocket connect: {exc}")
        report["verdict"] = VERDICT_IMPL_BLOCKED
        print(f"[ws] connect FAILED: {exc}")
        return report
    connected = True

    # 3) subscribe ---------------------------------------------------------- #
    # V3 spec: "The WebSocket request message should be sent in binary format,
    # not as a text message."
    guid = str(uuid.uuid4())
    sub_msg = {
        "guid": guid,
        "method": "sub",
        "data": {"mode": args.mode, "instrumentKeys": [args.key]},
    }
    report["subscription"]["guid"] = guid
    report["subscription"]["sent_as"] = "binary"
    try:
        ws.send_binary(json.dumps(sub_msg).encode("utf-8"))
        report["subscription"]["sent_at_ist"] = _fmt_ist(_now_utc_ms())
        print(f"[sub] sent {args.mode} subscribe for {args.key} as BINARY (guid={guid[:8]}...)")
    except OSError as exc:
        report["errors"].append(f"subscribe send: {exc}")
        report["verdict"] = VERDICT_IMPL_BLOCKED
        return report
    sent_sub = True

    # 4) capture window ------------------------------------------------------ #
    saw_frame = False
    error_hint = None
    close_code = None
    ticks: list[dict] = []
    printed_ticks = 0
    deadline = time.time() + args.duration
    last_log = time.time()
    while time.time() < deadline and ws is not None:
        frame = ws.recv_frame()
        if frame is _TIMEOUT:
            if time.time() - last_log >= 10:
                print(f"[ws] ... no frame for {int(time.time() - last_log)}s "
                      f"(ticks so far: {len(ticks)})")
                last_log = time.time()
            continue
        if frame is _CLOSED:
            report["errors"].append("websocket transport lost mid-stream")
            print("[ws] transport lost mid-stream")
            break
        opcode, payload = frame
        assert isinstance(opcode, int)
        report["frames"]["total"] += 1

        if opcode == 0x8:  # close
            close_code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
            report["errors"].append(f"server sent close frame (code={close_code})")
            print(f"[ws] server closed (code={close_code})")
            break
        if opcode == 0x9:  # ping -> pong
            report["frames"]["ping"] += 1
            ws.send_pong(payload)
            continue
        if opcode == 0xA:  # pong
            report["frames"]["pong"] += 1
            continue
        if opcode == 0x1:  # text
            report["frames"]["text"] += 1
            saw_frame = True
            text = payload.decode("utf-8", errors="replace")
            low = text.lower()
            if error_hint is None and ("error" in low or "failed" in low or "invalid" in low):
                error_hint = text[:300]
            report.setdefault("text_frames", []).append(text[:500])
            print(f"[ws] TEXT frame ({len(payload)}B): {text[:300]}")
            continue
        if opcode != 0x2:  # binary expected for FeedResponse
            continue

        report["frames"]["binary"] += 1
        saw_frame = True
        try:
            parsed = decode_feed_response(payload)
        except (ValueError, struct.error, IndexError) as exc:
            report["errors"].append(f"protobuf decode error: {exc}")
            continue
        ftype = parsed.get("type")
        if ftype == 0:
            report["feed_types"]["initial_feed"] += 1
        elif ftype == 1:
            report["feed_types"]["live_feed"] += 1
        elif ftype == 2:
            report["feed_types"]["market_info"] += 1
            mi = parsed.get("market_info") or {}
            seg = mi.get("segment_status")
            if seg:
                report["market"]["segment_status"] = seg
                print(f"[ws] market_info segment_status: {seg}")
            continue
        else:
            continue

        feed = parsed.get("feeds", {}).get(args.key)
        if not feed:
            continue
        ltpc = feed.get("ltpc")
        if not ltpc or ltpc.get("ltp") is None or ltpc.get("ltt") is None:
            continue
        recv_ms = _now_utc_ms()
        tick = {
            "recv_ms": recv_ms,
            "recv_ist": _fmt_ist(recv_ms),
            "ltt": ltpc["ltt"],
            "ltt_ist": _fmt_ist(ltpc["ltt"]),
            "ltp": ltpc["ltp"],
            "cp": ltpc.get("cp"),
            "ltq": ltpc.get("ltq"),
            "current_ts": parsed.get("current_ts"),
            "feed_type": ftype,
        }
        ticks.append(tick)
        if printed_ticks < 5:
            print(
                f"[tick#{len(ticks)}] LTP={tick['ltp']:.2f} "
                f"ltt={tick['ltt_ist']} recv={tick['recv_ist']} "
                f"lag={(tick['recv_ms'] - tick['ltt']) / 1000.0:.1f}s"
            )
            printed_ticks += 1
    # end loop

    # 5) close + stats ------------------------------------------------------ #
    if ws is not None:
        try:
            ws.close()
        except OSError:
            pass

    report["ended_ist"] = _fmt_ist(_now_utc_ms())
    report["duration_s_actual"] = round((_now_utc_ms() - started_ms) / 1000.0, 1)
    report["market"]["close_code"] = close_code
    report["ticks"] = ticks[:2000]  # cap raw evidence in the JSON report

    last_tick = ticks[-1] if ticks else None
    interval_stats = {}
    if len(ticks) >= 2:
        gaps = [ticks[i]["ltt"] - ticks[i - 1]["ltt"] for i in range(1, len(ticks))]
        interval_stats = {
            "avg_ms": round(sum(gaps) / len(gaps), 1),
            "max_ms": max(gaps),
            "min_ms": min(gaps),
            "deciles": {
                "p10_ms": sorted(gaps)[max(0, int(len(gaps) * 0.1) - 1)],
                "p50_ms": sorted(gaps)[int(len(gaps) * 0.5)],
                "p90_ms": sorted(gaps)[min(len(gaps) - 1, int(len(gaps) * 0.9))],
            },
        }
    summary = {
        "tick_count": len(ticks),
        "first_ltt_ist": _fmt_ist(ticks[0]["ltt"]) if ticks else None,
        "last_ltt_ist": _fmt_ist(ticks[-1]["ltt"]) if ticks else None,
        "first_recv_ist": ticks[0]["recv_ist"] if ticks else None,
        "last_recv_ist": ticks[-1]["recv_ist"] if ticks else None,
        "ltp_first": ticks[0]["ltp"] if ticks else None,
        "ltp_last": ticks[-1]["ltp"] if ticks else None,
        "interval_stats": interval_stats,
    }
    report["summary"] = summary
    report["candles_5m"] = aggregate_5m(ticks)

    verdict, freshness = classify_verdict(
        auth_ok=auth_ok,
        connected=connected,
        saw_frame=saw_frame,
        tick_count=len(ticks),
        is_open=bool(report["market"]["is_open"]),
        last_tick=last_tick,
        fresh_tol_s=args.fresh_tolerance,
        lag_tol_s=args.lag_tolerance,
    )
    if error_hint and len(ticks) == 0 and verdict == VERDICT_NO_NIFTY:
        verdict = VERDICT_SUB_FAILED
        report["errors"].append(f"server error frame: {error_hint}")
    report["verdict"] = verdict
    report["freshness"] = freshness

    # 6) evidence file -------------------------------------------------------
    if args.out:
        out_path = REPO_ROOT / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["evidence_file"] = str(out_path)

    _print_summary(report, summary, verdict, freshness, sent_sub)
    return report


def _print_summary(report: dict, summary: dict, verdict: str, freshness: str, sent_sub: bool) -> None:
    print("\n" + "=" * 72)
    print(f"INSTRUMENT   : {report['instrument_key']} (mode {report['mode']})")
    print(f"MARKET PHASE : {report['market']['phase']}  is_open={report['market']['is_open']}")
    print(f"DRIVER       : read-only Upstox Market Data Feed V3 WebSocket diagnostic")
    print(f"AUTH         : {report['auth']['status']}")
    print(f"CONNECT      : {'OK ' + str(report['connection']['host']) if report['connection']['ok'] else report['connection']['error']}")
    print(f"SUBSCRIBE    : {'sent' if sent_sub else 'N/A'} (ltpc)")
    print(f"FRAMES       : total={report['frames']['total']} text={report['frames']['text']} "
          f"binary={report['frames']['binary']}")
    print(f"FEED TYPES   : {report['feed_types']}")
    print(f"TICKS        : {summary['tick_count']}")
    if summary["tick_count"]:
        print(f"  first ltt  : {summary['first_ltt_ist']}  LTP={summary['ltp_first']}")
        print(f"  last  ltt  : {summary['last_ltt_ist']}  LTP={summary['ltp_last']}")
        print(f"  window     : first rcv {summary['first_recv_ist']} -> last rcv {summary['last_recv_ist']}")
        iv = summary.get("interval_stats") or {}
        if iv:
            print(f"  intervals  : avg={iv.get('avg_ms')}ms p50={iv.get('deciles', {}).get('p50_ms')}ms "
                  f"max={iv.get('max_ms')}ms")
    candles = report.get("candles_5m", [])
    if candles:
        print("  5m candles (from LTPC ticks):")
        for c in candles:
            print(f"    {c['bucket_ist']} O={c['open']:.2f} H={c['high']:.2f} "
                  f"L={c['low']:.2f} C={c['close']:.2f} V={c['volume']} ticks={c['ticks']}")
    print(f"FRESHNESS    : {freshness}")
    print(f"VERDICT      : {verdict}")
    for err in report["errors"]:
        print(f"ERROR        : {err}")
    if report.get("evidence_file"):
        print(f"EVIDENCE     : {report['evidence_file']}")
    print(f"SAFETY       : READ-ONLY diagnostic only - no orders, no paper trades, "
          f"no algorithm, no execution path.")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upstox V3 WebSocket market-data diagnostic (READ-ONLY).",
        epilog="Subscribes only to NSE_INDEX|Nifty 50 in ltpc mode. Never places orders.",
    )
    parser.add_argument("--key", default=DEFAULT_KEY, help="instrument key to subscribe")
    parser.add_argument("--mode", choices=["ltpc", "full"], default="ltpc")
    parser.add_argument("--duration", type=int, default=90, help="capture window in seconds")
    parser.add_argument("--frame-timeout", type=float, default=10.0, help="per-frame read timeout")
    parser.add_argument("--connect-timeout", type=float, default=25.0, help="authorize/connect timeout")
    parser.add_argument("--fresh-tolerance", type=int, default=90,
                        help="max age of last ltt for FRESH (seconds)")
    parser.add_argument("--lag-tolerance", type=int, default=15,
                        help="max receive-vs-ltt lag for FRESH (seconds)")
    parser.add_argument("--token-env", default="",
                        help="env-var name holding the data-layer token")
    parser.add_argument("--out", default="", help="evidence JSON path under the repo")
    parser.add_argument("--insecure", action="store_true",
                        help="disable TLS verification for the WS hop (diagnostic only)")
    args = parser.parse_args(argv)

    report = run_diagnostic(args)
    ok = report.get("verdict") == VERDICT_VERIFIED
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())