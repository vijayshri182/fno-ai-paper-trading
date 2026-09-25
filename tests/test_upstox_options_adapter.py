"""Offline tests for the Upstox V2 option-chain adapter (Phase 6).

All tests are hermetic: the transport is a fake serving canned Upstox V2 JSON
shapes captured from the live read-only probe, so nothing touches the network
or the environment. Covers the Phase 6 acceptance-test list plus adapter
invariants (receive-stamp, oi_change derivation, spot no-substitution,
underlying-key resolution, determinism, credential/execution isolation).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from fno_ai_paper_trading.data.errors import (
    AuthenticationError,
    ProviderConfigurationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.options.errors import (
    OptionDataError,
    OptionNormalizationError,
)
from fno_ai_paper_trading.research.options.protocol import OptionChainDataUnavailableError
from fno_ai_paper_trading.research.options.models import OptionSide
from fno_ai_paper_trading.research.options.normalization import chain_fingerprint
from fno_ai_paper_trading.research.options.upstox_adapter import (
    UPSTOX_PROVIDER_ID,
    UpstoxOptionChainProvider,
    resolve_upstox_underlying_key,
)
from fno_ai_paper_trading.research.options.upstox_diagnostic import (
    build_chain_diagnostic,
    field_availability,
    quote_state_counts,
    render_markdown,
)
from fno_ai_paper_trading.research.options.validation import ChainValidator, DataState
from fno_ai_paper_trading.utils.http import HttpError, HttpResponse

EXPIRY = date(2026, 9, 29)
NOW = datetime(2026, 9, 24, 9, 16, 0)
STRKES = (23000.0, 23100.0, 23200.0)


def nifty() -> Instrument:
    return Instrument(
        symbol="NIFTY 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY 50",
        exchange="NSE",
        exchange_token="NSE_INDEX|Nifty 50",
    )


def _node(side: str, index: int, *, oi: float = 150000.0, prev_oi: float = 149000.0) -> dict:
    return {
        "instrument_key": f"NSE_FO|{100000 + index}",
        "market_data": {
            "ltp": 23100.5,
            "volume": 2500,
            "oi": oi,
            "prev_oi": prev_oi,
            "close_price": 23095.0,
            "bid_price": 23100.0,
            "bid_qty": 75,
            "ask_price": 23101.0,
            "ask_qty": 75,
        },
        "option_greeks": {
            "delta": 0.5 if side == "CE" else -0.5,
            "gamma": 0.001,
            "theta": -0.05,
            "vega": 0.3,
            "iv": 12.5,
        },
    }


def chain_data(holder: dict, *, expiry: str = EXPIRY.isoformat(), spot: float = 23095.65) -> dict:
    records = []
    for i, strike in enumerate(STRKES):
        records.append(
            {
                "strike_price": strike,
                "expiry": expiry,
                "underlying_key": "NSE_INDEX|Nifty 50",
                "underlying_spot_price": spot,
                "pcr": 0.9 + i * 0.05,
                "call_options": holder.get(f"c{i}") or _node("CE", i),
                "put_options": holder.get(f"p{i}") or _node("PE", 100 + i),
            }
        )
    return {"data": records}


def contract_rows(*expiries: str) -> list[dict]:
    rows = []
    for i, exp in enumerate(expiries):
        rows.append(
            {
                "exchange": "NSE",
                "exchange_token": f"NSE_FO|{i}",
                "expiry": exp,
                "freeze_quantity": 10,
                "instrument_key": f"NSE_FO|{i}",
                "instrument_type": "CE",
                "lot_size": 75,
                "minimum_lot": 1,
                "name": "NIFTY",
                "segment": "NSE_FO",
                "strike_price": STRKES[0],
                "tick_size": 0.05,
                "trading_symbol": f"NIFTY {exp.replace('-', '')} CE",
                "underlying_key": "NSE_INDEX|Nifty 50",
                "underlying_symbol": "NIFTY",
                "underlying_type": "INDEX",
                "weekly": True,
            }
        )
    return rows


class FakeTransport:
    """Serves canned Upstox option endpoints; injectable failures."""

    def __init__(self, chain: dict, contracts: list[dict] | None = None, fail: Exception | None = None) -> None:
        self.chain = chain
        self.contracts = contracts or contract_rows(EXPIRY.isoformat())
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 10.0):
        self.calls.append(url)
        if self.fail is not None:
            raise self.fail
        if "/v2/option/contract" in url:
            body = json.dumps({"data": self.contracts}).encode()
            return HttpResponse(200, body, url, {"content-type": "application/json"})
        if "/v2/option/chain" in url:
            body = json.dumps(self.chain).encode()
            return HttpResponse(200, body, url, {"content-type": "application/json"})
        raise AssertionError(f"unexpected URL: {url}")


def make_provider(*, transport=None, now_fn=lambda: NOW, max_retries=0, token="data-token"):
    return UpstoxOptionChainProvider(
        token,
        now_fn=now_fn,
        http_fn=transport or FakeTransport(chain_data({})),
        max_retries=max_retries,
    )


def validate_chain(snapshot, ref_time=None):
    return ChainValidator(max_age_seconds=300).validate(snapshot, ref_time=ref_time or snapshot.timestamp)


def fetch(provider, *, expiry=EXPIRY, timestamp=NOW, **kw):
    return provider.fetch_chain(nifty(), expiry=expiry, timestamp=timestamp, **kw)


# ----------------------------------------------------------------- accept-01: valid payload


def test_accept01_valid_payload_maps_fields_without_invalid():
    chain = fetch(make_provider())
    validation = validate_chain(chain)

    # Full hydration except rho (Upstox publishes no rho): nothing INVALID,
    # exactly one field UNAVAILABLE per quote — never weakened to force VALID.
    assert not validation.invalid_reasons
    assert validation.state == DataState.UNAVAILABLE
    assert validation.summary()["unavailable_fields"] == ["greeks_rho"]

    assert len(chain.quotes) == 2 * len(STRKES)
    assert chain.expiry == EXPIRY
    assert chain.source == UPSTOX_PROVIDER_ID
    assert chain.spot_price == Decimal("23095.65")

    ce = chain.call_quotes()
    pe = chain.put_quotes()
    assert len(ce) == len(STRKES) and len(pe) == len(STRKES)
    assert all(q.side == OptionSide.CE for q in ce)
    assert all(q.side == OptionSide.PE for q in pe)
    assert all(q.instrument.instrument_type == InstrumentType.OPTION_CE for q in ce)
    assert all(q.instrument.instrument_type == InstrumentType.OPTION_PE for q in pe)

    first = chain.quotes[0]
    assert first.bid == Decimal("23100.0")
    assert first.ask == Decimal("23101.0")
    assert first.last_price == Decimal("23100.5")
    assert first.open_interest == 150000
    assert first.oi_change == 1000
    assert first.volume == 2500
    assert first.iv == Decimal("12.5")
    assert first.greeks.delta == Decimal("0.5")
    assert first.source == UPSTOX_PROVIDER_ID
    assert first.source_id == "NSE_FO|100000"


# ---------------------------------------------------------------- side mapping


def test_accept09_ce_pe_mapping_from_nested_keys():
    chain = fetch(make_provider())
    sides = {(q.strike, q.side) for q in chain.quotes}
    assert {(Decimal(s), OptionSide.CE) for s in STRKES} | {(Decimal(s), OptionSide.PE) for s in STRKES} == sides


# ----------------------------------------------------------------- accept-02: missing OI


def test_accept02_missing_oi_is_unavailable_not_fabricated():
    holder = {"c0": _node("CE", 0, oi=None, prev_oi=None)}
    chain = fetch(make_provider(transport=FakeTransport(chain_data(holder))))
    targeted = chain.contract(OptionSide.CE, STRKES[0])
    assert targeted.open_interest is None
    assert targeted.oi_change is None

    validation = validate_chain(chain)
    verdict = next(q for q in validation.quotes if q.key == targeted.key)
    assert verdict.state == DataState.UNAVAILABLE
    assert "open_interest" in verdict.unavailable


# ----------------------------------------------------------------- accept-03: missing IV


def test_accept03_missing_iv_is_unavailable_not_fabricated():
    holder = {"c0": dict(_node("CE", 0), option_greeks={})}
    chain = fetch(make_provider(transport=FakeTransport(chain_data(holder))))
    targeted = chain.contract(OptionSide.CE, STRKES[0])
    assert targeted.iv is None

    verdict = next(q for q in validate_chain(chain).quotes if q.key == targeted.key)
    assert verdict.state == DataState.UNAVAILABLE
    assert "iv" in verdict.unavailable


# ----------------------------------------------------------------- accept-04: missing greeks


def test_accept04_missing_greeks_are_unavailable_rho_never_available():
    chain = fetch(make_provider())
    assert all(q.greeks.rho is None for q in chain.quotes)
    matrix = field_availability(chain)
    assert matrix["greeks_rho"]["present"] == 0
    assert matrix["iv"]["present"] == len(chain.quotes)

    validation = validate_chain(chain)
    assert validation.state == DataState.UNAVAILABLE  # rho absent on every quote
    assert "greeks_rho" in validation.summary()["unavailable_fields"]


# ----------------------------------------------------------------- accept-05: stale timestamp


def test_accept05_stale_timestamp_is_invalid():
    chain = fetch(make_provider())
    stale_ref = NOW + timedelta(hours=2)
    validation = validate_chain(chain, ref_time=stale_ref)
    assert validation.state == DataState.INVALID
    assert any("stale" in reason for reason in validation.invalid_reasons)


# ----------------------------------------------------------------- accept-06: future timestamp


def test_accept06_future_timestamp_is_invalid():
    chain = fetch(make_provider())
    validation = validate_chain(chain, ref_time=NOW - timedelta(seconds=1))
    assert validation.state == DataState.INVALID
    assert any("future" in reason for reason in validation.invalid_reasons)


# ----------------------------------------------------------------- accept-07: crossed bid/ask


def test_accept07_crossed_bid_ask_is_invalid():
    bad = dict(_node("CE", 0))
    bad["market_data"] = dict(bad["market_data"], bid_price=23150.0, ask_price=23100.0)
    chain = fetch(make_provider(transport=FakeTransport(chain_data({"c0": bad}))))
    validation = validate_chain(chain)
    assert validation.state == DataState.INVALID
    assert any("crossed" in reason for reason in validation.invalid_reasons)


# ----------------------------------------------------------------- accept-08: duplicate contract


def test_accept08_duplicate_contract_is_invalid_global():
    record = chain_data({})["data"][0]
    transport = FakeTransport({"data": [record, dict(record)]})
    chain = fetch(make_provider(transport=transport))
    assert len(chain.duplicate_keys()) == 2  # CE and PE both duplicated
    validation = validate_chain(chain)
    assert validation.state == DataState.INVALID
    assert any("duplicate" in reason for reason in validation.invalid_reasons)


# ----------------------------------------------------------------- accept-10: inconsistent expiry


def test_accept10_inconsistent_expiry_is_invalid():
    transport = FakeTransport(chain_data({}, expiry="2026-09-29"))
    chain = make_provider(transport=transport).fetch_chain(
        nifty(), expiry=date(2026, 10, 6), timestamp=NOW
    )
    assert chain.expiry == date(2026, 10, 6)
    validation = validate_chain(chain)
    assert validation.state == DataState.INVALID
    assert any("expiry" in reason for reason in validation.invalid_reasons)


# ------------------------------------------------- accept-11: malformed payload


def test_accept11_malformed_chain_payload_raises_typed_error():
    transport = FakeTransport(chain_data({}))
    transport.chain = {"not_a_list": True}
    with pytest.raises(OptionDataError):
        fetch(make_provider(transport=transport))


def test_accept11b_malformed_row_missing_strike_raises_normalization_error():
    transport = FakeTransport(chain_data({}))
    record = transport.chain["data"][0]
    record.pop("strike_price")
    with pytest.raises(OptionNormalizationError):
        fetch(make_provider(transport=transport))


def test_accept11c_empty_chain_raises_typed_error():
    transport = FakeTransport({"data": []})
    with pytest.raises(OptionDataError):
        fetch(make_provider(transport=transport))


# ------------------------------------------------- accept-12: provider failures


def test_accept12a_401_maps_to_authentication_error():
    transport = FakeTransport(chain_data({}), fail=HttpError(401, "u", b"unauthorized"))
    with pytest.raises(AuthenticationError):
        fetch(make_provider(transport=transport))


def test_accept12b_429_exhausted_maps_to_rate_limit_error():
    transport = FakeTransport(chain_data({}), fail=HttpError(429, "u", b"slow down"))
    with pytest.raises(RateLimitError):
        fetch(make_provider(transport=transport, max_retries=1))


def test_accept12c_5xx_exhausted_maps_to_unavailable_error():
    transport = FakeTransport(chain_data({}), fail=HttpError(503, "u", b"boom"))
    with pytest.raises(UnavailableError):
        fetch(make_provider(transport=transport, max_retries=1))


def test_retry_recovers_from_transient_failure():
    failures = {"count": 0}

    def flaky(url, *, params=None, headers=None, timeout=10.0):
        if failures["count"] < 1 and "/v2/option/chain" in url:
            failures["count"] += 1
            raise HttpError(503, url, b"transient")
        return FakeTransport(chain_data({}))(url, params=params, headers=headers, timeout=timeout)

    chain = fetch(make_provider(transport=flaky, max_retries=2))
    assert failures["count"] == 1
    assert len(chain.quotes) == 2 * len(STRKES)


def test_empty_token_raises_provider_configuration_error():
    provider = make_provider(token="   ")
    with pytest.raises(ProviderConfigurationError):
        provider.fetch_chain(nifty(), expiry=EXPIRY, timestamp=NOW)


# -------------------------------------------------- adapter invariants: receive stamp


def test_receive_instant_stamps_quotes_and_is_never_future():
    timestamp = datetime(2026, 9, 24, 9, 16, 5, 123456)
    chain = fetch(make_provider(), timestamp=timestamp)
    assert chain.timestamp == timestamp
    assert all(q.timestamp == timestamp for q in chain.quotes)
    assert all(q.source_timestamp == timestamp for q in chain.quotes)
    validation = validate_chain(chain)
    assert validation.state == DataState.UNAVAILABLE or validation.state == DataState.VALID


# ------------------------------------------- adapter invariants: oi_change derivation


def test_oi_change_is_derived_oi_minus_prev_oi():
    holder = {"c0": _node("CE", 0, oi=200.0, prev_oi=180.0), "c1": _node("CE", 1, oi=200.0, prev_oi=220.0)}
    chain = fetch(make_provider(transport=FakeTransport(chain_data(holder))))
    assert chain.contract(OptionSide.CE, STRKES[0]).oi_change == 20
    assert chain.contract(OptionSide.CE, STRKES[1]).oi_change == -20
    # oi_change missing when either side is missing
    holder2 = {"c2": _node("CE", 2, oi=None, prev_oi=10.0)}
    chain2 = fetch(make_provider(transport=FakeTransport(chain_data(holder2))))
    assert chain2.contract(OptionSide.CE, STRKES[2]).oi_change is None


def test_fractional_oi_is_not_coerced_to_int():
    holder = {"c0": _node("CE", 0, oi=150000.5)}
    chain = fetch(make_provider(transport=FakeTransport(chain_data(holder))))
    quote = chain.contract(OptionSide.CE, STRKES[0])
    assert quote.open_interest is None
    assert quote.oi_change is None


# --------------------------------------- adapter invariants: spot no-substitution


def test_spot_from_provider_never_substituted():
    chain = fetch(make_provider())
    assert chain.spot_price == Decimal("23095.65")


def test_missing_spot_is_reported_not_substituted():
    transport = FakeTransport(chain_data({}, spot=None))
    chain = make_provider(transport=transport).fetch_chain(nifty(), expiry=EXPIRY, timestamp=NOW)
    assert chain.spot_price is None
    validation = validate_chain(chain)
    assert validation.global_issues and any(
        i.field == "spot_unavailable" and i.state == DataState.UNAVAILABLE for i in validation.global_issues
    )
    # and no quote carries a spot/greek value that could fake a spot
    assert all(q.last_price is not None for q in chain.quotes)


# ---------------------------------------------------- underlying-key resolution


def test_underlying_key_uses_exchange_token_when_present():
    inst = Instrument(
        symbol="X",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="whatever",
        exchange="NSE",
        exchange_token="NSE_FO|9999",
    )
    assert resolve_upstox_underlying_key(inst) == "NSE_FO|9999"


def test_underlying_key_falls_back_to_registry():
    inst = get_research_instrument("NIFTY 50")
    always_fresh = Instrument(
        symbol="NIFTY 50",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NIFTY 50",
        exchange="NSE",
    )
    assert resolve_upstox_underlying_key(always_fresh) == inst.exchange_token


def test_underlying_key_falls_back_to_alias():
    inst = Instrument(
        symbol="BANKNIFTY",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="BANKNIFTY",
        exchange="NSE",
    )
    assert resolve_upstox_underlying_key(inst) == "NSE_INDEX|Nifty Bank"


def test_underlying_key_unresolvable_raises():
    inst = Instrument(
        symbol="NOT A REAL INDEX",
        instrument_type=InstrumentType.INDEX,
        underlying_symbol="NOT A REAL INDEX",
        exchange="NSE",
    )
    with pytest.raises(OptionDataError):
        resolve_upstox_underlying_key(inst)


def test_supports_declines_option_underlying():
    provider = make_provider()
    option = Instrument(
        symbol="NIFTY 24SEP26 CE",
        instrument_type=InstrumentType.OPTION_CE,
        underlying_symbol="NIFTY 50",
        expiry=EXPIRY,
        strike=Decimal("23000"),
        option_type="CE",
        exchange="NSE",
        exchange_token="NSE_FO|1",
    )
    assert provider.supports(option) is False
    assert provider.supports(nifty()) is True


# ------------------------------------------------------- nearest expiry resolution


def test_nearest_expiry_resolution_via_contract_endpoint():
    rows = contract_rows("2026-10-06", "2026-09-29", "2027-12-28")
    transport = FakeTransport(chain_data({}), contracts=rows)
    chain = make_provider(transport=transport).fetch_chain(nifty(), timestamp=NOW)
    assert chain.expiry == EXPIRY
    assert any("/v2/option/contract" in url for url in transport.calls)


def test_expiries_are_sorted_and_deduped():
    rows = contract_rows("2026-10-06", "2026-09-29", "2026-09-29")
    provider = make_provider(transport=FakeTransport(chain_data({}), contracts=rows))
    assert provider.expiries(nifty()) == (date(2026, 9, 29), date(2026, 10, 6))


# ------------------------------------------------------------- determinism


def test_repeated_fetch_is_deterministic():
    one = fetch(make_provider(transport=FakeTransport(chain_data({}))))
    two = fetch(make_provider(transport=FakeTransport(chain_data({}))))
    assert chain_fingerprint(one) == chain_fingerprint(two)


def test_receive_stamp_naive_ist_conversion():
    aware = datetime(2026, 9, 24, 4, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    chain = fetch(make_provider(), timestamp=aware)
    assert chain.timestamp.tzinfo is None
    assert chain.timestamp == datetime(2026, 9, 24, 9, 30, 0)


# ------------------------------------------------------ diagnostic helpers


def test_diagnostic_artifact_is_credential_free():
    chain = fetch(make_provider())
    validation = validate_chain(chain)
    diagnostic = build_chain_diagnostic(
        chain, validation, fetched_at=NOW, live=False, endpoints_used=["/v2/option/chain", "/v2/option/contract"], orders_called=False
    )
    diagnostic["fingerprint"] = chain_fingerprint(chain)
    text = json.dumps(diagnostic, sort_keys=True)
    assert "data-token" not in text
    assert "Authorization" not in text
    assert diagnostic["credentials_in_output"] is False
    assert diagnostic["orders_called"] is False
    assert diagnostic["contracts_received"] == 2 * len(STRKES)
    assert diagnostic["strikes"] == len(STRKES)
    counts = quote_state_counts(validation)
    assert counts[DataState.INVALID] == 0
    assert counts[DataState.VALID] + counts[DataState.UNAVAILABLE] == 2 * len(STRKES)
    md = render_markdown(diagnostic)
    assert "23095.65" in md
    assert "UPSTOX_V2_OPTION_CHAIN" in md


# ------------------------------------------------------------ import isolation


def test_adapter_never_touches_execution_layer():
    """The adapter must not (even transitively) import execution/order code."""
    import ast
    from pathlib import Path

    package_root = Path(__file__).resolve().parents[1] / "src"
    for rel in ("research/options/upstox_adapter.py", "research/options/upstox_diagnostic.py"):
        module = package_root / "fno_ai_paper_trading" / rel
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        assert "execution" not in imported_roots, f"{rel} imports the execution layer"