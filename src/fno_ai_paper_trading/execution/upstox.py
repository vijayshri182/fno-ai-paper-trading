"""Upstox execution adapter for the controlled live test (WS 7.9).

This is the *clean execution adapter/interface* the work stream asks for: a
narrow, broker-aware boundary that the manager (and only the manager) uses to
place the single entry/exit, poll status, cancel, and read positions. It is
**not** a ``Broker`` (``broker/base.py`` stays for the paper path and
``PaperBroker.is_live`` stays hard-coded ``False``; PAPER mode never touches
this class).

Safety properties:

* ``dry_run=True`` (default) means order placement is **fabricated locally** —
  no HTTP write is ever attempted in dry-run mode.
* A real write is refused when the caller asks for the unimplementable
  ``ExecutionMode.LIVE``.
* Credentials come from the environment **only** (:class:`UpstoxCredentials`),
  from the ``UPSTOX_*`` execution-side variables; the adapter never receives
  the analytics/data settings object or token (``FNO_UPSTOX_*``) and never
  falls back to it. The token is never logged; ``repr`` redacts it.
* Without ``UPSTOX_ACCESS_TOKEN`` the credentials are simply unconfigured and a
  non-dry-run attempt raises **before any HTTP write**.
* Credential roles (see `.env.example` and ``execution/oauth.py``):
  ``UPSTOX_API_KEY`` identifies the Upstox application (client id) and is sent
  as ``x-api-key``; ``UPSTOX_API_SECRET`` is used **only** by the OAuth token
  exchange and is never sent on API calls; ``UPSTOX_ACCESS_TOKEN`` authenticates
  API requests. The API secret is stripped/normalized like every other value and
  is never included in ``headers()``, ``repr``, audit or logs.
* Non-2xx HTTP failures map to typed errors from ``data.errors``/``execution.errors``.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, Mapping

from dotenv import load_dotenv

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.execution.errors import (
    UpstoxExecutionError,
    UpstoxOrderRejectedError,
)
from fno_ai_paper_trading.execution.gate import ExecutionMode
from fno_ai_paper_trading.models.enums import OrderSide, OrderStatus
from fno_ai_paper_trading.models.order import Order
from fno_ai_paper_trading.utils.http import HttpError, http_request
from fno_ai_paper_trading.utils.functions import new_id

UPSTOX_BASE_URL = "https://api.upstox.com"

#: Upstox order-status tokens -> internal OrderStatus.
_FILLED_TOKENS = frozenset({"completed"})
_REJECTED_TOKENS = frozenset({"rejected"})
_CANCELLED_TOKENS = frozenset({"cancelled"})
_OPEN_TOKENS = frozenset(
    {
        "put order req received",
        "validation pending",
        "pending",
        "open",
        "trigger pending",
        "triggered",
        "transit",
    }
)


@dataclass(frozen=True)
class UpstoxCredentials:
    """Environment-derived credentials; tokens and secrets are never logged.

    Roles:

    * ``access_token`` — authenticates Upstox API requests (``UPSTOX_ACCESS_TOKEN``).
    * ``api_key`` — identifies the Upstox application/API client
      (``UPSTOX_API_KEY``); sent as ``x-api-key`` when present.
    * ``api_secret`` — Upstox application secret (``UPSTOX_API_SECRET``), used
      **only** by the OAuth token exchange (``execution/oauth.py``); it is never
      placed in request headers by this adapter and never logged.
    """

    access_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    base_url: str = UPSTOX_BASE_URL
    timeout_seconds: float = 10.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "access_token", (self.access_token or "").strip())
        object.__setattr__(self, "api_key", (self.api_key or "").strip())
        object.__setattr__(self, "api_secret", (self.api_secret or "").strip())
        object.__setattr__(self, "base_url", (self.base_url or UPSTOX_BASE_URL).rstrip("/"))
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")

    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    def headers(self, json_body: bool = False) -> dict[str, str]:
        if not self.configured:
            raise UpstoxExecutionError(
                "Upstox credentials are not configured; set UPSTOX_ACCESS_TOKEN in .env"
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

    def __repr__(self) -> str:
        return (
            f"UpstoxCredentials(base_url={self.base_url!r}, "
            f"api_key_configured={bool(self.api_key)}, "
            f"api_secret_configured={bool(self.api_secret)}, "
            "access_token=<redacted>)"
        )

    @classmethod
    def from_env(cls) -> "UpstoxCredentials":
        """Build execution credentials from the ``UPSTOX_*`` environment only.

        The execution path **never** consumes the analytics/data token
        (``FNO_UPSTOX_ACCESS_TOKEN``): if ``UPSTOX_ACCESS_TOKEN`` is absent the
        credentials are simply unconfigured, so a non-dry-run attempt fails
        before any HTTP write rather than borrowing the analytics token.
        """
        load_dotenv()
        return cls(
            access_token=os.getenv("UPSTOX_ACCESS_TOKEN", "").strip(),
            api_key=os.getenv("UPSTOX_API_KEY", "").strip(),
            api_secret=os.getenv("UPSTOX_API_SECRET", "").strip(),
            base_url=(os.getenv("UPSTOX_BASE_URL", "") or UPSTOX_BASE_URL),
            timeout_seconds=float(os.getenv("UPSTOX_TIMEOUT_SECONDS", "") or 10.0),
            max_retries=int(os.getenv("UPSTOX_MAX_RETRIES", "") or 3),
        )


@dataclass(frozen=True)
class ExecutionAck:
    """A broker acknowledgement for a submitted order."""

    order_id: str  # internal id (Order.order_id), stable across the run
    provider_order_id: str  # broker-side id
    status: OrderStatus = OrderStatus.SUBMITTED
    timestamp: str = ""
    dry_run: bool = False


@dataclass(frozen=True)
class ExecutionPosition:
    """A broker-reported open position (net quantity zero == flat)."""

    symbol: str
    exchange: str = "NSE"
    quantity: int = 0
    average_price: Decimal | None = None
    pnl: Decimal | None = None

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


def _quote_node(data: dict, symbol: str) -> dict:
    """Locate a market-quote node despite response-key separator differences.

    Upstox quote responses key nodes with ``SEGMENT:symbol`` (colon) even
    though requests use ``SEGMENT|symbol`` (pipe). This looks up the exact key
    first (backward compatible), then the colon variant, then matches purely by
    the symbol/token suffix so numeric and descriptive keys both work.
    """
    if not isinstance(data, dict):
        return {}
    node = data.get(symbol)
    if isinstance(node, dict):
        return node
    colon = symbol.replace("|", ":")
    if colon in data and isinstance(data[colon], dict):
        return data[colon]
    suffix = symbol.rsplit("|", 1)[-1].strip()
    if suffix:
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            if key.rsplit(":", 1)[-1].strip() == suffix or key.rsplit("|", 1)[-1].strip() == suffix:
                if isinstance(value.get("ohlc"), dict) and value["ohlc"].get("close") is not None:
                    return value
    return {}


class ExecutionAdapter(ABC):
    """Broker-agnostic interface for the controlled live test.

    Implementations are created only by the operator/manager. ``dry_run`` must
    be honored by every implementation: order placement never hits a real
    broker in dry-run mode.
    """

    dry_run: bool = False

    @abstractmethod
    def get_account_id(self) -> str:
        """Authenticate and return a public account identifier (never a secret)."""

    @abstractmethod
    def quote(self, symbol: str) -> Decimal:
        """Last tradable price for the option symbol (premium)."""

    @abstractmethod
    def place_order(self, order: Order, mode: ExecutionMode = ExecutionMode.LIVE_EXECUTION_TEST) -> ExecutionAck:
        """Submit the order; raise for rejection; never writes in dry-run."""

    @abstractmethod
    def get_order_status(self, order_id: str, quantity: int) -> tuple[OrderStatus, dict[str, object]]:
        """Current broker status of an order plus its detail snapshot."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Try to cancel an open order; True when cancellation is confirmed."""

    @abstractmethod
    def get_positions(self) -> list[ExecutionPosition]:
        """All broker-reported positions (used for post-exit reconciliation)."""

    def is_flat(self, symbol: str) -> bool:
        return all(
            position.symbol != symbol or position.is_flat for position in self.get_positions()
        )


def _map_order_status(raw: str, filled_quantity: int, quantity: int) -> OrderStatus:
    token = (raw or "").strip().lower()
    if token in _FILLED_TOKENS:
        if quantity > 0 and 0 < filled_quantity < quantity:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.FILLED
    if token in _REJECTED_TOKENS:
        return OrderStatus.REJECTED
    if token in _CANCELLED_TOKENS:
        return OrderStatus.CANCELLED
    if token in _OPEN_TOKENS:
        return OrderStatus.SUBMITTED
    return OrderStatus.SUBMITTED


class UpstoxExecutionAdapter(ExecutionAdapter):
    """REST adapter over the Upstox V2 trading endpoints.

    ``instrument_tokens`` maps a local ``exchange_token`` (``SEGMENT|SYMBOL``
    or the numeric-key form ``SEGMENT|TOKEN``) to the numeric Upstox instrument
    token the order API expects. When absent for a symbol, the adapter refuses
    to place the order (no guessing) *unless* the ``SEGMENT|TOKEN`` key already
    carries the numeric token — that suffix is the identity, not a guess.
    """

    def __init__(
        self,
        credentials: UpstoxCredentials,
        *,
        dry_run: bool = True,
        instrument_tokens: Mapping[str, int] | None = None,
        request: Callable = http_request,
        now_fn: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] | None = None,
        instrument_type_for: Callable[[Order], str] | None = None,
    ) -> None:
        self.credentials = credentials
        self.dry_run = bool(dry_run)
        self.instrument_tokens: dict[str, int] = dict(instrument_tokens or {})
        self._request = request
        self._now = now_fn
        self._sleep = sleep
        self._instrument_type_for = instrument_type_for or _instrument_type_of

    # ------------------------------------------------------------ transport

    def _url(self, path: str) -> str:
        return f"{self.credentials.base_url}{path}"

    def _get(self, path: str):
        try:
            return self._request("GET", self._url(path), headers=self.credentials.headers(), timeout=self.credentials.timeout_seconds)
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        except OSError as exc:
            raise UnavailableError(f"Upstox execution unavailable: {exc}") from exc

    def _post(self, path: str, body: dict[str, object]):
        data = _json_bytes(body)
        try:
            return self._request(
                "POST",
                self._url(path),
                headers=self.credentials.headers(json_body=True),
                timeout=self.credentials.timeout_seconds,
                data=data,
            )
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        except OSError as exc:
            raise UnavailableError(f"Upstox execution unavailable: {exc}") from exc

    def _delete(self, path: str):
        try:
            return self._request("DELETE", self._url(path), headers=self.credentials.headers(), timeout=self.credentials.timeout_seconds)
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        except OSError as exc:
            raise UnavailableError(f"Upstox execution unavailable: {exc}") from exc

    @staticmethod
    def _map_http_error(exc: HttpError) -> Exception:
        if exc.status == 401:
            return AuthenticationError("Upstox rejected the access token (HTTP 401)")
        if exc.status == 429:
            return RateLimitError("Upstox rate limit hit (HTTP 429)")
        if exc.status == 400:
            return UpstoxExecutionError(f"Upstox rejected the request (HTTP 400): {exc.text[:240]}")
        return UpstoxExecutionError(f"Upstox execution error HTTP {exc.status}: {exc.text[:240]}")

    # ------------------------------------------------------------- interface

    def get_account_id(self) -> str:
        response = self._get("/v2/user/profile")
        payload = response.json
        data = payload.get("data") or {}
        user_id = str(data.get("user_id") or data.get("email") or "")
        if not user_id:
            raise AuthenticationError("Upstox profile returned no user identifier")
        return user_id

    def quote(self, symbol: str) -> Decimal:
        from urllib.parse import quote as _quote

        key = _quote(symbol)
        response = self._get(f"/v2/market-quote/ohlc/{key}")
        payload = response.json
        data = payload.get("data") or {}
        node = _quote_node(data, symbol)
        ohlc = node.get("ohlc") or {}
        close = ohlc.get("close")
        if close is None:
            raise UpstoxExecutionError(f"Upstox returned no close price for {symbol!r}")
        return Decimal(str(close))

    def place_order(self, order: Order, mode: ExecutionMode = ExecutionMode.LIVE_EXECUTION_TEST) -> ExecutionAck:
        if mode is ExecutionMode.LIVE:
            raise UpstoxExecutionError(
                "normal LIVE order placement is not implemented; only LIVE_EXECUTION_TEST may be enabled"
            )
        if order.side not in (OrderSide.BUY, OrderSide.SELL):
            raise UpstoxExecutionError(f"unsupported order side {order.side!r}")
        if order.instrument is None:
            raise UpstoxExecutionError("an instrument is required to place an order")

        if self.dry_run:
            return self._dry_ack(order)

        token = self._token_for(order.instrument.exchange_token)
        body = {
            "instrument_token": token,
            "quantity": int(order.quantity),
            "product": "M",
            "validity": "DAY",
            "price": 0,
            "tag": "fno-ai-controlled-live-execution-test",
            "instrument_type": self._instrument_type_for(order),
            "transaction_type": order.side.value,
            "order_type": "MARKET",
            "is_amo": False,
        }
        response = self._post("/v2/order/place", body)
        data = (response.json or {}).get("data") or {}
        provider_id = str(data.get("order_id") or "").strip()
        if not provider_id:
            raise UpstoxExecutionError("Upstox order ack contained no order_id")
        if not order.order_id:
            order.order_id = new_id("ORD")
        return ExecutionAck(
            order_id=order.order_id,
            provider_order_id=provider_id,
            status=OrderStatus.SUBMITTED,
            timestamp=str(data.get("timestamp") or self._now().isoformat()),
            dry_run=False,
        )

    def get_order_status(self, order_id: str, quantity: int) -> tuple[OrderStatus, dict[str, object]]:
        if self.dry_run:
            raise UpstoxExecutionError("get_order_status is not available in dry-run mode")
        response = self._get(f"/v2/order/history/{order_id}")
        payload = response.json
        entries = (payload.get("data") or []) if isinstance(payload.get("data"), list) else []
        if not entries:
            raise UpstoxExecutionError(f"Upstox returned no order history for {order_id!r}")
        latest = entries[-1]
        raw = str(latest.get("status") or "")
        filled = int(latest.get("filled_quantity") or 0)
        average = latest.get("average_price")
        detail: dict[str, object] = {
            "status": raw,
            "filled_quantity": filled,
            "average_price": average,
            "exchange_order_id": latest.get("exchange_order_id"),
            "rejection_reason": latest.get("rejected_at") or latest.get("rejection_reason") or "",
        }
        status = _map_order_status(raw, filled, int(quantity))
        if status is OrderStatus.REJECTED:
            raise UpstoxOrderRejectedError(
                f"Upstox rejected order {order_id}: {detail['rejection_reason'] or raw}"
            )
        return status, detail

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            return True
        try:
            self._delete(f"/v2/order/cancel/{order_id}")
        except UpstoxExecutionError:
            return False
        return True

    def get_positions(self) -> list[ExecutionPosition]:
        if self.dry_run:
            return []
        response = self._get("/v2/positions")
        payload = response.json
        rows = (payload.get("data") or []) if isinstance(payload.get("data"), list) else []
        # Map broker-reported numeric instrument tokens back to our registered
        # exchange_token keys so FLAT reconciliation matches by identity, not by
        # display symbol (Upstox reports the descriptive trading_symbol).
        reverse_map = {token: key for key, token in self.instrument_tokens.items()}
        positions: list[ExecutionPosition] = []
        for row in rows:
            symbol = str(row.get("trading_symbol") or "")
            reported_token = row.get("instrument_token")
            if reported_token is not None:
                try:
                    symbol = reverse_map.get(int(reported_token), symbol)
                except (TypeError, ValueError):
                    pass
            raw_quantity = row.get("net_quantity")
            if raw_quantity is None:
                raw_quantity = row.get("quantity")
            positions.append(
                ExecutionPosition(
                    symbol=symbol,
                    exchange=str(row.get("exchange") or "NSE"),
                    quantity=int(raw_quantity or 0),
                    average_price=(
                        Decimal(str(row["average_price"])) if row.get("average_price") is not None else None
                    ),
                    pnl=Decimal(str(row["pnl"])) if row.get("pnl") is not None else None,
                )
            )
        return positions

    # ---------------------------------------------------------------- internals

    def _token_for(self, exchange_token: str | None) -> int:
        token = self.instrument_tokens.get(exchange_token)
        if token is None and exchange_token:
            # A SEGMENT|TOKEN key already carries the numeric Upstox token in
            # its suffix (e.g. "NSE_FO|57617"); that is the identity, not a guess.
            suffix = exchange_token.rsplit("|", 1)[-1].strip()
            if suffix.isdigit():
                token = int(suffix)
        if token is None:
            raise UpstoxExecutionError(
                f"no numeric Upstox instrument token registered for {exchange_token!r}; "
                "the adapter refuses to guess"
            )
        return int(token)

    def _dry_ack(self, order: Order) -> ExecutionAck:
        order_id = order.order_id or new_id("ORD")
        provider_id = f"DRYRUN-{order_id}"
        return ExecutionAck(
            order_id=order_id,
            provider_order_id=provider_id,
            status=OrderStatus.SUBMITTED,
            timestamp=self._now().isoformat(),
            dry_run=True,
        )


def _json_bytes(body: dict[str, object]) -> bytes:
    import json

    def _default(value: object):
        if isinstance(value, (Decimal, int, float)):
            return value
        return str(value)

    return json.dumps(body, default=_default).encode("utf-8")


def _instrument_type_of(order: Order) -> str:
    if order.instrument is None:
        return "EQ"
    name = order.instrument.instrument_type.value
    if name == "FUTURE":
        return "FUT"
    if name in ("OPTION_CE", "OPTION_PE"):
        return "OPT"
    return "EQ"