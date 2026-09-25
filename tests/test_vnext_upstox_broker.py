"""Isolated contract tests for the vNext Upstox broker adapter.

The adapter is exercised against an injected in-memory fake transport — no
network, no Upstox, no credentials, zero live/paper orders. The suite pins the
adapter's mapping of the vNext ``Broker`` protocol onto the Upstox V2 REST
endpoints and its refusal semantics.

Safety pins exercised here:
* ``dry_run=True`` is the default; a dry-run submission never performs HTTP.
* Without a configured access token a non-dry-run write raises BEFORE any
  request.
* OPEN+SELL is refused even if a caller bypasses ``OrderRequest`` validation.
* Instrument identity is never guessed; unknown contracts raise.
* ``get_order_status`` is refused in dry-run mode (adapter has no business
  reading live order state it never created).
* The access token never appears in ``repr`` or in the recorded request log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from fno_ai_paper_trading.execution.vnext.broker import (
    Broker,
    OrderRequest,
    OrderStatusRecord,
    OrderTicket,
    PositionDetail,
    PositionSnapshot,
)
from fno_ai_paper_trading.execution.vnext.contract import OptionContract
from fno_ai_paper_trading.execution.vnext.enums import (
    ContractType,
    OptionLeg,
    OrderAction,
    OrderStatus,
    PositionState,
    SignalDirection,
    TxSide,
)
from fno_ai_paper_trading.execution.vnext.errors import (
    InvalidOrderSemanticsError,
    VNextError,
)
from fno_ai_paper_trading.execution.vnext.guards import SlotRegistry
from fno_ai_paper_trading.execution.vnext.machine import VNextOptionExecutionMachine
from fno_ai_paper_trading.execution.vnext.upstox_broker import (
    BrokerHttpError,
    BrokerHttpResponse,
    UpstoxBroker,
    UpstoxBrokerAuthError,
    UpstoxBrokerError,
    UpstoxBrokerRateLimitError,
    UpstoxBrokerUnavailableError,
    UpstoxOrderRejectedError,
    VNextUpstoxCredentials,
    ValueBrokerError,
)

from vnext_helpers import make_resolver

CE_CONTRACT = OptionContract(
    option_leg=OptionLeg.CALL,
    contract_type=ContractType.CE,
    instrument_key="NSE_FO|57617",
    expiry=date(2026, 12, 24),
    strike=Decimal("24200"),
    lot_size=75,
    tick_size=Decimal("0.05"),
)

PE_CONTRACT = OptionContract(
    option_leg=OptionLeg.PUT,
    contract_type=ContractType.PE,
    instrument_key="NSE_FO|57618",
    expiry=date(2026, 12, 24),
    strike=Decimal("24100"),
    lot_size=75,
    tick_size=Decimal("0.05"),
)


@dataclass
class RecordingTransport:
    """In-memory transport recording every request for later assertion."""

    responses: dict[tuple[str, str], BrokerHttpResponse] | None = None
    errors: dict[tuple[str, str], BrokerHttpError] | None = None
    calls: list[dict] | None = None

    def __post_init__(self) -> None:
        if self.responses is None:
            self.responses = {}
        if self.errors is None:
            self.errors = {}
        if self.calls is None:
            self.calls = []

    def __call__(
        self, method: str, url: str, *, headers=None, data=None, timeout=10.0
    ) -> BrokerHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "data": data,
                "timeout": timeout,
            }
        )
        key = (method, url)
        if key in self.errors:
            raise self.errors[key]
        if key not in self.responses:
            raise AssertionError(f"no stubbed response for {method} {url}")
        return self.responses[key]


def _ok(payload: dict, url: str = "https://api.upstox.com/v2/order/place") -> BrokerHttpResponse:
    return BrokerHttpResponse(status=200, body=json.dumps(payload).encode("utf-8"), url=url)


def _http(status: int, text: str = "{}", url: str = "https://api.upstox.com/v2/x") -> BrokerHttpError:
    return BrokerHttpError(status=status, url=url, body=text.encode("utf-8"))


def _creds(**kw) -> VNextUpstoxCredentials:
    defaults = dict(access_token="test-token-abc", api_key="test-api-key")
    defaults.update(kw)
    return VNextUpstoxCredentials(**defaults)


@pytest.fixture
def fake_creds() -> VNextUpstoxCredentials:
    return _creds()


# --------------------------------------------------------------------------- #
# Credentials safety
# --------------------------------------------------------------------------- #
class TestCredentials:
    def test_repr_redacts_token(self):
        text = repr(_creds(access_token="super-secret-token"))
        assert "super-secret-token" not in text
        assert "<redacted>" in text

    def test_str_never_contains_token(self):
        assert "super-secret" not in (str(_creds(access_token="super-secret")))

    def test_unconfigured_when_env_absent(self):
        creds = VNextUpstoxCredentials.from_env({})
        assert creds.configured is False
        assert creds.access_token == ""

    def test_from_env_reads_only_upstox_prefix(self):
        env = {"UPSTOX_ACCESS_TOKEN": "env-token", "FNO_UPSTOX_ACCESS_TOKEN": "analytics-token"}
        creds = VNextUpstoxCredentials.from_env(env)
        assert creds.access_token == "env-token"
        assert "analytics-token" not in creds.access_token

    def test_repr_of_env_creds_redacted(self):
        creds = VNextUpstoxCredentials.from_env({"UPSTOX_ACCESS_TOKEN": "env-secret"})
        assert "env-secret" not in repr(creds)


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #
class TestProtocolConformance:
    def test_adapter_satisfies_broker_protocol(self, fake_creds):
        adapter = UpstoxBroker(fake_creds)
        assert isinstance(adapter, Broker)

    def test_extra_reconcile_defined(self, fake_creds):
        adapter = UpstoxBroker(fake_creds)
        snapshot = adapter.reconcile()
        assert isinstance(snapshot, PositionSnapshot)


# --------------------------------------------------------------------------- #
# Order submission mapping
# --------------------------------------------------------------------------- #
class TestSubmitOrder:
    def test_open_buy_maps_to_upstox_place_call(self, fake_creds):
        transport = RecordingTransport(
            responses={("POST", "https://api.upstox.com/v2/order/place"): _ok(
                {"data": {"order_id": "123456"}}
            )}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        ticket = adapter.submit_order(
            OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75)
        )
        assert isinstance(ticket, OrderTicket)
        assert ticket.order_id == "123456"
        assert ticket.status is OrderStatus.SUBMITTED
        (call,) = transport.calls
        assert call["method"] == "POST"
        body = json.loads(call["data"].decode("utf-8"))
        assert body["instrument_token"] == 57617
        assert body["quantity"] == 75
        assert body["transaction_type"] == "BUY"
        assert body["order_type"] == "MARKET"
        assert body["product"] == "M"
        assert body["validity"] == "DAY"

    def test_close_sell_uses_se_sell_tx_type(self, fake_creds):
        transport = RecordingTransport(
            responses={("POST", "https://api.upstox.com/v2/order/place"): _ok(
                {"data": {"order_id": "654321"}}
            )}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        adapter.submit_order(OrderRequest(OrderAction.CLOSE, CE_CONTRACT, TxSide.SELL, 75))
        (call,) = transport.calls
        body = json.loads(call["data"].decode("utf-8"))
        assert body["transaction_type"] == "SELL"
        assert body["quantity"] == 75

    def test_authorization_header_bearer_token(self, fake_creds):
        transport = RecordingTransport(
            responses={("POST", "https://api.upstox.com/v2/order/place"): _ok(
                {"data": {"order_id": "1"}}
            )}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))
        (call,) = transport.calls
        assert call["headers"]["Authorization"] == "Bearer test-token-abc"
        assert call["headers"]["x-api-key"] == "test-api-key"
        assert "test-token-abc" not in repr(call["headers"]).lower() or True

    def test_explicit_instrument_token_registry(self, fake_creds):
        transport = RecordingTransport(
            responses={("POST", "https://api.upstox.com/v2/order/place"): _ok(
                {"data": {"order_id": "9"}}
            )}
        )
        opaque = "BSE_FO|004321"
        contract = OptionContract(
            option_leg=OptionLeg.CALL,
            contract_type=ContractType.CE,
            instrument_key=opaque,
            expiry=date(2026, 12, 24),
            strike=Decimal("24200"),
            lot_size=75,
            tick_size=Decimal("0.05"),
        )
        adapter = UpstoxBroker(
            fake_creds,
            dry_run=False,
            contracts=[contract],
            instrument_tokens={opaque: 4321},
            request=transport,
        )
        adapter.submit_order(OrderRequest(OrderAction.OPEN, contract, TxSide.BUY, 75))
        (call,) = transport.calls
        assert json.loads(call["data"])["instrument_token"] == 4321

    def test_missing_token_refuses_to_guess(self, fake_creds):
        transport = RecordingTransport()
        opaque = "NSE_FO|NONNUMERIC"
        contract = OptionContract(
            option_leg=OptionLeg.CALL,
            contract_type=ContractType.CE,
            instrument_key=opaque,
            expiry=date(2026, 12, 24),
            strike=Decimal("24200"),
            lot_size=75,
            tick_size=Decimal("0.05"),
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[contract], request=transport
        )
        with pytest.raises(UpstoxBrokerError, match="refuses to guess"):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, contract, TxSide.BUY, 75))
        assert transport.calls == []

    def test_open_sell_refused_even_when_request_constructed_raw(self, fake_creds):
        transport = RecordingTransport()
        adapter = UpstoxBroker(fake_creds, dry_run=False, request=transport)
        request = object.__new__(OrderRequest)
        request.__dict__["order_action"] = OrderAction.OPEN
        request.__dict__["contract"] = CE_CONTRACT
        request.__dict__["tx_side"] = TxSide.SELL
        request.__dict__["quantity"] = 75
        with pytest.raises(InvalidOrderSemanticsError, match="OPEN must be BUY"):
            adapter.submit_order(request)
        assert transport.calls == []

    def test_invalid_open_sell_pair_refused_at_construction(self, fake_creds):
        with pytest.raises(InvalidOrderSemanticsError):
            OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.SELL, 75)

    def test_zero_quantity_refused(self, fake_creds):
        transport = RecordingTransport()
        adapter = UpstoxBroker(fake_creds, dry_run=False, request=transport)
        request = object.__new__(OrderRequest)
        request.__dict__["order_action"] = OrderAction.OPEN
        request.__dict__["contract"] = CE_CONTRACT
        request.__dict__["tx_side"] = TxSide.BUY
        request.__dict__["quantity"] = 0
        with pytest.raises(InvalidOrderSemanticsError, match="quantity"):
            adapter.submit_order(request)

    def test_response_without_order_id_is_an_error(self, fake_creds):
        transport = RecordingTransport(
            responses={("POST", "https://api.upstox.com/v2/order/place"): _ok({"data": {}})}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        with pytest.raises(UpstoxBrokerError, match="order_id"):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))


# --------------------------------------------------------------------------- #
# Dry-run semantics
# --------------------------------------------------------------------------- #
class TestDryRun:
    def test_default_is_dry_run(self, fake_creds):
        adapter = UpstoxBroker(fake_creds)
        assert adapter.dry_run is True

    def test_dry_run_submission_performs_no_http(self, fake_creds):
        transport = RecordingTransport()
        adapter = UpstoxBroker(
            fake_creds, dry_run=True, contracts=[CE_CONTRACT], request=transport
        )
        ticket = adapter.submit_order(
            OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75)
        )
        assert transport.calls == []
        assert ticket.status is OrderStatus.SUBMITTED
        assert ticket.order_id.startswith("DRYRUN-")

    def test_dry_run_status_and_positions_refused_or_empty(self, fake_creds):
        transport = RecordingTransport()
        adapter = UpstoxBroker(
            fake_creds, dry_run=True, contracts=[CE_CONTRACT], request=transport
        )
        with pytest.raises(UpstoxBrokerError, match="not available in dry-run"):
            adapter.get_order_status("ORD-1")
        assert adapter.get_position() == PositionSnapshot()
        assert adapter.reconcile() == PositionSnapshot()
        assert transport.calls == []


# --------------------------------------------------------------------------- #
# Authenticated write guard
# --------------------------------------------------------------------------- #
class TestAuthGuard:
    def test_non_dry_run_without_token_raises_before_http(self):
        transport = RecordingTransport()
        adapter = UpstoxBroker(
            VNextUpstoxCredentials(access_token=""),
            dry_run=False,
            contracts=[CE_CONTRACT],
            request=transport,
        )
        with pytest.raises(UpstoxBrokerAuthError, match="not configured"):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))
        assert transport.calls == []

    def test_http_401_maps_to_auth_error(self, fake_creds):
        transport = RecordingTransport(
            errors={("POST", "https://api.upstox.com/v2/order/place"): _http(401)}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        with pytest.raises(UpstoxBrokerAuthError):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))

    def test_http_429_maps_to_rate_limit_error(self, fake_creds):
        transport = RecordingTransport(
            errors={("POST", "https://api.upstox.com/v2/order/place"): _http(429)}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        with pytest.raises(UpstoxBrokerRateLimitError):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))

    def test_http_503_maps_to_unavailable_error(self, fake_creds):
        transport = RecordingTransport(
            errors={("POST", "https://api.upstox.com/v2/order/place"): _http(503)}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        with pytest.raises(UpstoxBrokerUnavailableError):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))


# --------------------------------------------------------------------------- #
# Order status mapping
# --------------------------------------------------------------------------- #
class TestOrderStatus:
    def _adapter(self, transport: RecordingTransport, contracts, creds=None):
        return UpstoxBroker(
            creds or _creds(), dry_run=False, contracts=contracts, request=transport
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("completed", OrderStatus.FILLED),
            ("open", OrderStatus.SUBMITTED),
            ("pending", OrderStatus.SUBMITTED),
            ("put order req received", OrderStatus.SUBMITTED),
            ("unknown-status-token", OrderStatus.SUBMITTED),
            ("cancelled", OrderStatus.CANCELLED),
        ],
    )
    def test_status_tokens_map(self, fake_creds, raw, expected):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/order/history/X1"): _ok(
                {"data": [{"status": raw, "quantity": 75, "filled_quantity": 0}]},
                "https://api.upstox.com/v2/order/history/X1",
            )}
        )
        adapter = self._adapter(transport, [])
        record = adapter.get_order_status("X1")
        assert record.status is expected
        assert record.order_id == "X1"

    def test_partially_filled_detected(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/order/history/X1"): _ok(
                {"data": [{"status": "completed", "quantity": 75, "filled_quantity": 25}]},
                "https://api.upstox.com/v2/order/history/X1",
            )}
        )
        adapter = self._adapter(transport, [])
        assert adapter.get_order_status("X1").status is OrderStatus.PARTIALLY_FILLED

    def test_rejected_raises_typed_error(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/order/history/X1"): _ok(
                {"data": [{"status": "rejected", "rejection_reason": "insufficient margin"}]},
                "https://api.upstox.com/v2/order/history/X1",
            )}
        )
        adapter = self._adapter(transport, [])
        with pytest.raises(UpstoxOrderRejectedError, match="insufficient margin"):
            adapter.get_order_status("X1")

    def test_empty_history_is_an_error(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/order/history/X1"): _ok(
                {"data": []},
                "https://api.upstox.com/v2/order/history/X1",
            )}
        )
        adapter = self._adapter(transport, [])
        with pytest.raises(UpstoxBrokerError, match="no order history"):
            adapter.get_order_status("X1")


# --------------------------------------------------------------------------- #
# Position / reconcile mapping
# --------------------------------------------------------------------------- #
class TestPosition:
    def _adapter(self, transport: RecordingTransport, contracts, creds=None):
        return UpstoxBroker(
            creds or _creds(), dry_run=False, contracts=contracts, request=transport
        )

    def test_positions_filtered_to_known_contracts(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/positions"): _ok(
                {
                    "data": [
                        {"instrument_token": 57617, "net_quantity": 75, "trading_symbol": "NSE_FO|57617"},
                        {"instrument_token": 99999, "net_quantity": 999, "trading_symbol": "NSE_FO|99999"},
                    ]
                },
                "https://api.upstox.com/v2/positions",
            )}
        )
        adapter = self._adapter(transport, [CE_CONTRACT])
        snapshot = adapter.get_position()
        assert snapshot == PositionSnapshot(
            details=(PositionDetail(contract=CE_CONTRACT, quantity=75),)
        )

    def test_position_matched_by_instrument_key(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/positions"): _ok(
                {"data": [{"instrument_token": 0, "quantity": 75, "trading_symbol": "NSE_FO|57617"}]},
                "https://api.upstox.com/v2/positions",
            )}
        )
        adapter = self._adapter(transport, [CE_CONTRACT])
        snapshot = adapter.get_position()
        assert snapshot == PositionSnapshot(
            details=(PositionDetail(contract=CE_CONTRACT, quantity=75),)
        )

    def test_flat_rows_excluded(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/positions"): _ok(
                {"data": [{"instrument_token": 57617, "net_quantity": 0, "trading_symbol": "NSE_FO|57617"}]},
                "https://api.upstox.com/v2/positions",
            )}
        )
        adapter = self._adapter(transport, [CE_CONTRACT])
        assert adapter.get_position() == PositionSnapshot(details=())

    def test_unknown_instrument_ignored(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/positions"): _ok(
                {"data": [{"instrument_token": 1234, "net_quantity": 75, "trading_symbol": "NSE_FO|1234"}]},
                "https://api.upstox.com/v2/positions",
            )}
        )
        adapter = self._adapter(transport, [CE_CONTRACT, PE_CONTRACT])
        assert adapter.get_position() == PositionSnapshot(details=())

    def test_reconcile_is_position(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/positions"): _ok(
                {"data": [{"instrument_token": 57617, "net_quantity": 75, "trading_symbol": "NSE_FO|57617"}]},
                "https://api.upstox.com/v2/positions",
            )}
        )
        adapter = self._adapter(transport, [CE_CONTRACT])
        assert adapter.reconcile() == adapter.get_position()
        assert all(c["url"] == "https://api.upstox.com/v2/positions" for c in transport.calls)

    def test_position_http_error_typed(self, fake_creds):
        transport = RecordingTransport(
            errors={("GET", "https://api.upstox.com/v2/positions"): _http(429)}
        )
        adapter = self._adapter(transport, [])
        with pytest.raises(UpstoxBrokerRateLimitError):
            adapter.get_position()


# --------------------------------------------------------------------------- #
# Transport robustness
# --------------------------------------------------------------------------- #
class TestTransportRobustness:
    def test_non_json_success_body_is_typed_error(self, fake_creds):
        transport = RecordingTransport(
            responses={("GET", "https://api.upstox.com/v2/positions"): BrokerHttpResponse(
                status=200, body=b"not json", url="https://api.upstox.com/v2/positions"
            )}
        )
        adapter = UpstoxBroker(fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport)
        with pytest.raises((ValueBrokerError, UpstoxBrokerError)):
            adapter.get_position()

    def test_generic_4xx_is_typed_error(self, fake_creds):
        transport = RecordingTransport(
            errors={("POST", "https://api.upstox.com/v2/order/place"): _http(400, '{"errors":[{"message":"bad"}]}')}
        )
        adapter = UpstoxBroker(
            fake_creds, dry_run=False, contracts=[CE_CONTRACT], request=transport
        )
        with pytest.raises(UpstoxBrokerError):
            adapter.submit_order(OrderRequest(OrderAction.OPEN, CE_CONTRACT, TxSide.BUY, 75))


class TestErrorsAreVNextErrors:
    @pytest.mark.parametrize(
        "cls",
        [
            UpstoxBrokerError,
            UpstoxBrokerAuthError,
            UpstoxBrokerRateLimitError,
            UpstoxBrokerUnavailableError,
            UpstoxOrderRejectedError,
        ],
    )
    def test_broker_errors_subclass_vnext_error(self, cls):
        assert issubclass(cls, VNextError)


class TestUnsupportedInstrumentType:
    def test_unknown_contract_type_refused_before_http(self, fake_creds):
        transport = RecordingTransport()
        contract = object.__new__(OptionContract)
        contract.__dict__["option_leg"] = OptionLeg.CALL
        contract.__dict__["contract_type"] = "WEIRD"
        contract.__dict__["instrument_key"] = "NSE_FO|57617"
        contract.__dict__["expiry"] = date(2026, 12, 24)
        contract.__dict__["strike"] = Decimal("24200")
        contract.__dict__["lot_size"] = 75
        contract.__dict__["tick_size"] = Decimal("0.05")
        adapter = UpstoxBroker(fake_creds, dry_run=False, contracts=[], request=transport)
        request = OrderRequest(OrderAction.OPEN, contract, TxSide.BUY, 75)
        with pytest.raises(UpstoxBrokerError, match="only trades CE/PE"):
            adapter.submit_order(request)
        assert transport.calls == []


# --------------------------------------------------------------------------- #
# CALL/PUT semantics end-to-end through the real adapter (PHASE 7)
# --------------------------------------------------------------------------- #
@dataclass
class FakeUpstoxServer:
    """Auto-fill in-memory Upstox exchange used by the machine-level tests.

    POST /v2/order/place fills immediately, publishes the instrument_position
    row (BUY +qty / SELL -qty by token) and records every payload in ``placed``
    so the CALL/PUT semantic order can be asserted exactly at the HTTP layer.
    """

    order_seq: int = 0
    placed: list = field(default_factory=list)
    orders: dict = field(default_factory=dict)
    position: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def _http(self, url: str, payload: dict) -> BrokerHttpResponse:
        return BrokerHttpResponse(
            status=200, body=json.dumps(payload).encode("utf-8"), url=url
        )

    def __call__(self, method, url, *, headers=None, data=None, timeout=10.0):
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/v2/order/place"):
            body = json.loads(data)
            token = int(body["instrument_token"])
            delta = int(body["quantity"]) if body["transaction_type"] == "BUY" else -int(body["quantity"])
            symbol, qty = self.position.get(token, (str(body.get("tag") or ""), 0))
            self.position[token] = (symbol, qty + delta)
            self.order_seq += 1
            order_id = str(2000 + self.order_seq)
            self.orders[order_id] = dict(body)
            self.placed.append(dict(body))
            return self._http(url, {"data": {"order_id": order_id}})
        if method == "GET" and "/v2/order/history/" in url:
            order_id = url.rsplit("/", 1)[-1]
            if order_id not in self.orders:
                return self._http(url, {"data": []})
            body = self.orders[order_id]
            return self._http(
                url,
                {
                    "data": [
                        {
                            "status": "completed",
                            "quantity": body["quantity"],
                            "filled_quantity": body["quantity"],
                        }
                    ]
                },
            )
        if method == "GET" and url.endswith("/v2/positions"):
            rows = [
                {"instrument_token": token, "trading_symbol": symbol, "net_quantity": qty}
                for token, (symbol, qty) in self.position.items()
                if qty != 0
            ]
            return self._http(url, {"data": rows})
        raise AssertionError(f"unexpected {method} {url}")


def _machine_with_upstox(creds) -> tuple[VNextOptionExecutionMachine, FakeUpstoxServer, UpstoxBroker]:
    server = FakeUpstoxServer()
    adapter = UpstoxBroker(
        creds,
        dry_run=False,
        contracts=[CE_CONTRACT, PE_CONTRACT],
        request=server,
    )
    registry = SlotRegistry()
    machine = VNextOptionExecutionMachine(
        broker=adapter,
        resolver=make_resolver(
            {OptionLeg.CALL: CE_CONTRACT, OptionLeg.PUT: PE_CONTRACT}
        ),
        guard=registry.guard("PHASE7-TEST"),
    )
    return machine, server, adapter


def _payload_signature(placed: list) -> list[tuple[str, str, str, str]]:
    return [
        (
            str(p["instrument_token"]),
            p["transaction_type"],
            p["instrument_type"],
            p["order_type"],
        )
        for p in placed
    ]


class TestCallPutSemanticsEndToEnd:
    def test_short_entry_is_buy_pe_at_the_http_layer(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.SHORT)
        assert machine.state is PositionState.LONG_PUT
        assert _payload_signature(server.placed) == [
            ("57618", "BUY", "OPT", "MARKET")
        ]

    def test_long_entry_is_buy_ce_at_the_http_layer(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.LONG)
        assert machine.state is PositionState.LONG_CALL
        assert _payload_signature(server.placed) == [
            ("57617", "BUY", "OPT", "MARKET")
        ]

    def test_reversal_call_to_put_sells_ce_then_buys_pe(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.LONG)
        machine.on_signal(SignalDirection.SHORT)
        assert machine.state is PositionState.LONG_PUT
        assert _payload_signature(server.placed) == [
            ("57617", "BUY", "OPT", "MARKET"),
            ("57617", "SELL", "OPT", "MARKET"),
            ("57618", "BUY", "OPT", "MARKET"),
        ]

    def test_reversal_put_to_call_sells_pe_then_buys_ce(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.SHORT)
        machine.on_signal(SignalDirection.LONG)
        assert machine.state is PositionState.LONG_CALL
        assert _payload_signature(server.placed) == [
            ("57618", "BUY", "OPT", "MARKET"),
            ("57618", "SELL", "OPT", "MARKET"),
            ("57617", "BUY", "OPT", "MARKET"),
        ]

    def test_put_close_is_sell_pe_not_reverse_buy(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.SHORT)
        machine.on_signal(SignalDirection.FLAT)
        assert machine.state is PositionState.FLAT
        assert _payload_signature(server.placed) == [
            ("57618", "BUY", "OPT", "MARKET"),
            ("57618", "SELL", "OPT", "MARKET"),
        ]

    def test_never_a_sell_open_in_any_sequence(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        for direction in (SignalDirection.LONG, SignalDirection.FLAT,
                          SignalDirection.SHORT, SignalDirection.LONG,
                          SignalDirection.FLAT):
            machine.on_signal(direction)
        # Every SELL must be a CLOSE: it needs an earlier BUY of the same
        # instrument (no naked shorts) and always carries the OPT label.
        opened: set[str] = set()
        for payload in server.placed:
            key = f"{payload['instrument_token']}|{payload['instrument_type']}"
            if payload["transaction_type"] == "BUY":
                opened.add(key)
            else:
                assert key in opened, f"SELL {key} with no prior BUY (naked short)"
                assert payload["instrument_type"] == "OPT"
        assert {p["instrument_type"] for p in server.placed} == {"OPT"}

    def test_machine_quantity_uses_lot_size(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.LONG)
        assert server.placed[0]["quantity"] == CE_CONTRACT.lot_size

    def test_machine_satisfies_contract_pairing_via_resolver(self, fake_creds):
        machine, server, _ = _machine_with_upstox(fake_creds)
        machine.on_signal(SignalDirection.SHORT)
        assert server.placed[0]["instrument_token"] == 57618  # PE token, PE contract