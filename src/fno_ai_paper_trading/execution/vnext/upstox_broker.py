"""Upstox REST broker adapter for the isolated vNext execution layer.

Implements the :class:`Broker` protocol (``submit_order`` / ``get_order_status``
/ ``get_position`` / ``reconcile``) over the Upstox V2 trading endpoints,
mirroring the request/response shape the WS 7.9 upgrade path uses
(``/v2/order/place``, ``/v2/order/history/{id}``, ``/v2/positions``).

Isolation guarantees (same discipline as the rest of vNext):

* This module imports ONLY the vNext tree — no ``execution/upstox``, no
  ``utils.http``, no ``data.errors`` — so the static isolation audit still
  passes and this adapter can never be wired into the WS 7.9 harness.
* The transport is injected: ``request`` is a plain callable mirroring the
  ``utils.http.http_request`` shape. Tests inject deterministic in-memory
  fakes; the default ``default_transport`` (urllib) is never used in tests.
* ``dry_run=True`` by default (same as the WS 7.9 adapter): order placement is
  fabricated locally, no HTTP write is ever attempted; ``get_order_status`` is
  refused in dry-run mode and positions come back empty, so a dry-run adapter
  can never "observe" live state it has no business reading.
* Credentials come from explicit construction or ``from_env``
  (``UPSTOX_*``); the token is never logged — ``repr`` redacts it.
* Without a configured access token, a non-dry-run write raises BEFORE any
  HTTP request.
* Adapters NEVER place an OPEN by SELL (vNext forbids naked short options),
  never guess an instrument token, and refuse ``CLOSE`` with an unknown side.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from fno_ai_paper_trading.execution.vnext.broker import (
    Broker,
    OrderRequest,
    OrderStatusRecord,
    OrderTicket,
    PositionDetail,
    PositionSnapshot,
)
from fno_ai_paper_trading.execution.vnext.contract import OptionContract
from fno_ai_paper_trading.execution.vnext.enums import OrderAction, OrderStatus, TxSide
from fno_ai_paper_trading.execution.vnext.errors import (
    InvalidOrderSemanticsError,
    VNextError,
)
from fno_ai_paper_trading.execution.vnext.live_credentials import (
    DEFAULT_LIVE_BASE_URL,
    LiveCredentialsUnavailableError,
    LiveCredentialProvider,
    LiveTradingCredentials,
    redact_text,
)

UPSTOX_BASE_URL = DEFAULT_LIVE_BASE_URL

#: Backward-compatible alias: the vNext credential bundle now lives with the
#: separate live-trading credential provider (live_credentials.py).
VNextUpstoxCredentials = LiveTradingCredentials
_ORDER_TAG = "fno-ai-vnext-isolated-broker-adapter"


# --------------------------------------------------------------------------- #
# Module-local HTTP surface (no dependency on utils.http)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BrokerHttpResponse:
    """Minimal typed HTTP response (mirrors utils.http.HttpResponse)."""

    status: int
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> dict:
        try:
            value = json.loads(self.text)
        except ValueError as exc:
            raise ValueBrokerError(
                f"broker returned non-JSON body for {self.url}: {self.text[:40]!r}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueBrokerError("response is not a JSON object")
        return value


class ValueBrokerError(VNextError):
    """A transport returned an unexpected payload."""


class BrokerHttpError(Exception):
    """A transport completed with a non-2xx status (module-local).

    Kept independent of utils.http so vNext never depends on the WS 7.9 tree;
    the same attribute shapes (``status``, ``text``) make fakes interchangeable.
    """

    def __init__(self, status: int, url: str, body: bytes = b"") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}")

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


TransportRequest = Callable[
    [str, str], BrokerHttpResponse
]  # (method, url) -> response (see default_transport for full signature)


def default_transport(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 10.0,
) -> BrokerHttpResponse:
    """Urllib-backed transport (never exercised by the test-suite; tests inject fakes)."""
    if headers is None:
        headers = {}
    request = urllib.request.Request(
        url,
        data=data,
        headers=dict(headers),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return BrokerHttpResponse(
                status=int(response.status), body=response.read(), url=url
            )
    except urllib.error.HTTPError as exc:
        raise BrokerHttpError(int(exc.code), url, exc.read()) from exc


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #
class UpstoxBrokerError(VNextError):
    """A vNext Upstox broker call failed."""


class UpstoxBrokerAuthError(UpstoxBrokerError, LiveCredentialsUnavailableError):
    """The access token was rejected (HTTP 401) or is missing/unconfigured."""


class UpstoxBrokerRateLimitError(UpstoxBrokerError):
    """Rate limit hit (HTTP 429)."""


class UpstoxBrokerUnavailableError(UpstoxBrokerError):
    """The broker service was unavailable (network/5xx)."""


class UpstoxOrderRejectedError(UpstoxBrokerError):
    """The broker rejected the order ("rejected" order status)."""


# --------------------------------------------------------------------------- #
# Status token mapping (mirrors the WS 7.9 adapter's table)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
def _token_from_instrument_key(key: str) -> int | None:
    """The numeric Upstox token is the identity when the key is ``SEG|TOKEN``."""
    suffix = key.rsplit("|", 1)[-1].strip()
    if suffix.isdigit():
        return int(suffix)
    return None


def _json_bytes(body: Mapping[str, object]) -> bytes:
    def _default(value: object) -> object:
        if isinstance(value, Decimal):
            return str(value)
        return value

    return json.dumps(dict(body), default=_default).encode("utf-8")


class UpstoxBroker:
    """Deterministic, isolated Upstox adapter implementing the vNext Broker protocol.

    Supports the exact vNext one-directional model: OPEN is always BUY,
    CLOSE may be BUY (covering a short, never used) or SELL (closing a long).
    Instrument identity is never guessed: an order requires either an explicit
    ``instrument_tokens`` mapping or a numeric token suffix on
    ``contract.instrument_key``.
    """

    def __init__(
        self,
        credentials: VNextUpstoxCredentials,
        *,
        dry_run: bool = True,
        contracts: Sequence[OptionContract] = (),
        instrument_tokens: Mapping[str, int] | None = None,
        request: TransportRequest = default_transport,
    ) -> None:
        self.credentials = credentials
        self.dry_run = bool(dry_run)
        self._contracts: dict[str, OptionContract] = {
            contract.instrument_key: contract for contract in contracts
        }
        self._instrument_tokens: dict[str, int] = dict(instrument_tokens or {})
        self._reverse_tokens: dict[int, OptionContract] = {}
        for key, contract in self._contracts.items():
            explicit = self._instrument_tokens.get(key)
            token = explicit if explicit is not None else _token_from_instrument_key(key)
            if token is not None:
                self._reverse_tokens[int(token)] = contract
        self._request = request

    # ------------------------------------------------------------- transport

    def _url(self, path: str) -> str:
        return f"{self.credentials.base_url}{path}"

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        # Surface the provider's missing-token refusal as an adapter-typed
        # auth error so callers see a single Upstox-signed exception family.
        try:
            return self.credentials.headers(json_body=json_body)
        except LiveCredentialsUnavailableError as exc:
            raise UpstoxBrokerAuthError(str(exc)) from exc

    def _get(self, path: str):
        try:
            return self._request(
                "GET",
                self._url(path),
                headers=self._headers(),
                timeout=self.credentials.timeout_seconds,
            )
        except BrokerHttpError as exc:
            raise _map_http_error(exc) from exc
        except OSError as exc:
            raise UpstoxBrokerUnavailableError(f"broker unavailable: {exc}") from exc

    def _post(self, path: str, body: Mapping[str, object]):
        try:
            return self._request(
                "POST",
                self._url(path),
                headers=self._headers(json_body=True),
                timeout=self.credentials.timeout_seconds,
                data=_json_bytes(body),
            )
        except BrokerHttpError as exc:
            raise _map_http_error(exc) from exc
        except OSError as exc:
            raise UpstoxBrokerUnavailableError(f"broker unavailable: {exc}") from exc

    # ------------------------------------------------------------------ broker

    def submit_order(self, order: OrderRequest) -> OrderTicket:
        if order.order_action is OrderAction.OPEN and order.tx_side is TxSide.SELL:
            raise InvalidOrderSemanticsError(
                "OPEN must be BUY in vNext (no options selling as an opening transaction)"
            )
        if order.quantity <= 0:
            raise InvalidOrderSemanticsError("quantity must be positive")
        token = self._token_for(order.contract)
        if self.dry_run:
            provider_id = f"DRYRUN-{uuid.uuid4().hex[:8]}"
            return OrderTicket(order_id=provider_id, status=OrderStatus.SUBMITTED)
        body = {
            "instrument_token": token,
            "quantity": int(order.quantity),
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "trigger_price": 0,
            "tag": _ORDER_TAG,
            "instrument_type": _instrument_type_for(order.contract),
            "transaction_type": order.tx_side.value,
            "order_type": "MARKET",
            "is_amo": False,
        }
        response = self._post("/v2/order/place", body)
        data = response.json.get("data") or {}
        provider_id = str(data.get("order_id") or "").strip()
        if not provider_id:
            raise UpstoxBrokerError("Upstox order ack contained no order_id")
        return OrderTicket(order_id=provider_id, status=OrderStatus.SUBMITTED)

    def get_order_status(self, order_id: str) -> OrderStatusRecord:
        if self.dry_run:
            raise UpstoxBrokerError("get_order_status is not available in dry-run mode")
        response = self._get(f"/v2/order/history/{order_id}")
        entries = response.json.get("data") or []
        if not isinstance(entries, list) or not entries:
            raise UpstoxBrokerError(f"Upstox returned no order history for {order_id!r}")
        latest = entries[-1]
        raw = str(latest.get("status") or "")
        filled = int(latest.get("filled_quantity") or 0)
        quantity = int(latest.get("quantity") or filled)
        status = _map_order_status(raw, filled, quantity)
        if status is OrderStatus.REJECTED:
            raise UpstoxOrderRejectedError(
                f"Upstox rejected order {order_id}: {latest.get('rejection_reason') or raw}"
            )
        return OrderStatusRecord(order_id=order_id, status=status, message=raw)

    def get_position(self) -> PositionSnapshot:
        if self.dry_run:
            return PositionSnapshot(details=())
        response = self._get("/v2/positions")
        rows = response.json.get("data") or []
        if not isinstance(rows, list):
            return PositionSnapshot(details=())
        details: list[PositionDetail] = []
        for row in rows:
            contract = self._contract_for_row(row)
            if contract is None:
                continue
            raw_quantity = row.get("net_quantity")
            if raw_quantity is None:
                raw_quantity = row.get("quantity")
            quantity = int(raw_quantity or 0)
            if quantity == 0:
                continue
            details.append(PositionDetail(contract=contract, quantity=quantity))
        return PositionSnapshot(details=tuple(details))

    def reconcile(self) -> PositionSnapshot:
        return self.get_position()

    # ---------------------------------------------------------------- internals

    def _token_for(self, contract: OptionContract) -> int:
        explicit = self._instrument_tokens.get(contract.instrument_key)
        if explicit is not None:
            return int(explicit)
        token = _token_from_instrument_key(contract.instrument_key)
        if token is not None:
            return token
        raise UpstoxBrokerError(
            f"no numeric Upstox instrument token registered for "
            f"{contract.instrument_key!r}; the adapter refuses to guess"
        )

    def _contract_for_row(self, row: Mapping[str, object]) -> OptionContract | None:
        reported_token = row.get("instrument_token")
        if reported_token is not None:
            try:
                found = self._reverse_tokens.get(int(reported_token))
            except (TypeError, ValueError):
                found = None
            if found is not None:
                return found
        symbol = str(row.get("trading_symbol") or "")
        if symbol in self._contracts:
            return self._contracts[symbol]
        token_hint = _token_from_instrument_key(symbol)
        if token_hint is not None:
            return self._reverse_tokens.get(token_hint)
        return None


def _map_http_error(exc: BrokerHttpError) -> UpstoxBrokerError:
    if exc.status == 401:
        return UpstoxBrokerAuthError("Upstox rejected the access token (HTTP 401)")
    if exc.status == 429:
        return UpstoxBrokerRateLimitError("Upstox rate limit hit (HTTP 429)")
    if 500 <= exc.status <= 599:
        return UpstoxBrokerUnavailableError(
            f"Upstox service unavailable (HTTP {exc.status})"
        )
    return UpstoxBrokerError(
        f"Upstox broker error HTTP {exc.status}: {exc.text[:240]}"
    )


def _instrument_type_for(contract: OptionContract) -> str:
    from fno_ai_paper_trading.execution.vnext.enums import ContractType

    if contract.contract_type is ContractType.CE or contract.contract_type is ContractType.PE:
        return "OPT"
    raise UpstoxBrokerError(
        f"unsupported contract type {contract.contract_type!r}; "
        "the adapter only trades CE/PE options"
    )


__all__ = [
    "UPSTOX_BASE_URL",
    "BrokerHttpResponse",
    "BrokerHttpError",
    "TransportRequest",
    "default_transport",
    "VNextUpstoxCredentials",
    "LiveTradingCredentials",
    "LiveCredentialProvider",
    "LiveCredentialsUnavailableError",
    "redact_text",
    "UpstoxBroker",
    "UpstoxBrokerError",
    "UpstoxBrokerAuthError",
    "UpstoxBrokerRateLimitError",
    "UpstoxBrokerUnavailableError",
    "UpstoxOrderRejectedError",
    "ValueBrokerError",
]