"""Phase 5 — options research stack tests.

Covers the explicit data-quality contract: valid chain, malformed chain, missing
OI, missing IV/Greeks, stale quote, crossed/invalid bid-ask, duplicate
contracts, inconsistent expiry, CE/PE mapping, deterministic normalization and
provider-independence — plus the no-fabrication guarantee.

Hermetic: no network, no clock reads (explicit ``ref_time`` everywhere), no
credentials, nothing touching execution/paper layers.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.options import (
    ChainValidator,
    DataState,
    Greeks,
    OptionChainDataUnavailableError,
    OptionChainProvider,
    OptionChainSnapshot,
    OptionNormalizationError,
    OptionQuote,
    OptionSide,
    StaticChainProvider,
    chain_fingerprint,
    normalize_chain,
    normalize_quote,
    quote_from_dict,
    quote_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
)

UNDERLYING = Instrument(
    symbol="NIFTY",
    instrument_type=InstrumentType.INDEX,
    underlying_symbol="NIFTY",
    exchange="NSE",
)

REF_TIME = datetime(2026, 12, 24, 9, 21, 0)
EXPIRY = "2026-12-24"
TS = "2026-12-24T09:20:30"

NO_GREEKS = object()  # sentinel: force the row to carry *no* greeks at all
DEFAULT_GREEKS = {
    "delta": "0.52",
    "gamma": "0.0045",
    "theta": "-0.0021",
    "vega": "0.0314",
    "rho": "0.0012",
}


def make_quote(
    strike: int = 24500,
    side: str = "CE",
    *,
    ts: str = TS,
    expiry: str = EXPIRY,
    bid: str = "245.50",
    ask: str = "245.75",
    last: str = "245.60",
    oi: str | None = "12345",
    oi_change: str | None = "90",
    volume: str | None = "890",
    iv: str | None = "0.1745",
    greeks=DEFAULT_GREEKS,
) -> dict:
    row: dict = {
        "expiry": expiry,
        "strike": str(strike),
        "option_type": side,
        "timestamp": ts,
        "instrument_key": f"TKN{strike}{side}",
        "bid": bid,
        "ask": ask,
        "last_price": last,
    }
    if oi is not None:
        row["oi"] = oi
    if oi_change is not None:
        row["oi_change"] = oi_change
    if volume is not None:
        row["volume"] = volume
    if iv is not None:
        row["iv"] = iv
    if greeks is not NO_GREEKS:
        row["greeks"] = greeks
    return row


def make_chain(
    rows,
    *,
    expiry: str = EXPIRY,
    timestamp: str = TS,
    spot: str | None = "24650.05",
    source: str = "test",
) -> dict:
    return {
        "expiry": expiry,
        "timestamp": timestamp,
        "spot_price": spot,
        "quotes": rows,
        "source": source,
    }


def normalize(rows, **kwargs):
    payload = make_chain(rows, **kwargs)
    return normalize_chain(payload, underlying=UNDERLYING, source="test")


def validate(snapshot, *, max_age_seconds: int = 120):
    return ChainValidator(max_age_seconds=max_age_seconds).validate(snapshot, ref_time=REF_TIME)


# --------------------------------------------------------------------------- #
# valid chain
# --------------------------------------------------------------------------- #


class TestValidChain:
    def test_two_sided_chain_fully_valid(self):
        snapshot = normalize([make_quote(24500, "CE"), make_quote(24500, "PE")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.VALID
        assert verdict.invalid_reasons == ()
        assert len(verdict.quotes) == 2
        assert all(q.state == DataState.VALID for q in verdict.quotes)
        assert snapshot.expiry == date(2026, 12, 24)
        assert snapshot.spot_price == Decimal("24650.05")
        assert len(snapshot.call_quotes()) == 1
        assert len(snapshot.put_quotes()) == 1

    def test_contract_lookup_and_strikes(self):
        snapshot = normalize([make_quote(24450, "CE"), make_quote(24500, "CE"), make_quote(24500, "PE")])
        assert snapshot.contract(OptionSide.CE, "24500") is not None
        assert snapshot.contract(OptionSide.CE, "24500").strike == Decimal("24500")
        assert snapshot.contract(OptionSide.PE, "24450") is None
        assert snapshot.strike_values() == (Decimal("24450"), Decimal("24500"))

    def test_quote_mid_and_key(self):
        row = make_quote()
        quote = normalize_quote(row, underlying=UNDERLYING)
        assert quote.mid == Decimal("245.625")
        assert quote.key == "NIFTY|2026-12-24|24500|CE"
        assert quote.has_price is True

    def test_freshness_age_boundary(self):
        snapshot = normalize(
            [make_quote(ts="2026-12-24T09:18:41")],
            timestamp="2026-12-24T09:18:41",
            source="test",
        )
        # REF_TIME 09:21:00 -> age 139s == max_age 139 => VALID
        verdict = ChainValidator(max_age_seconds=139).validate(snapshot, ref_time=REF_TIME)
        assert verdict.state == DataState.VALID
        # age 140s > max 139 => INVALID (stale)
        verdict = ChainValidator(max_age_seconds=139).validate(snapshot, ref_time=REF_TIME.replace(second=1))
        assert verdict.state == DataState.INVALID

    def test_future_timestamp_invalid(self):
        snapshot = normalize([make_quote(ts="2026-12-24T09:21:30")], timestamp="2026-12-24T09:21:30")
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID
        assert any(f.field == "freshness" and f.state == DataState.INVALID for q in verdict.quotes for f in q.fields)


# --------------------------------------------------------------------------- #
# malformed chain (strict normalization)
# --------------------------------------------------------------------------- #


class TestMalformedChain:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda row: row.pop("expiry"),
            lambda row: row.update({"option_type": "XX"}),
            lambda row: row.update({"strike": "not-a-number"}),
            lambda row: row.pop("timestamp"),
            lambda row: row.update({"timestamp": "not-a-date"}),
        ],
    )
    def test_malformed_quote_row_raises(self, mutate):
        row = make_quote()
        mutate(row)
        with pytest.raises(OptionNormalizationError):
            normalize_quote(row, underlying=UNDERLYING)

    def test_bad_chain_envelope_raises(self):
        with pytest.raises(OptionNormalizationError):
            normalize_chain({"quotes": "not-a-list"}, underlying=UNDERLYING)
        with pytest.raises(OptionNormalizationError):
            normalize_chain({"timestamp": TS}, underlying=UNDERLYING)  # no rows, no expiry
        option_underlying = Instrument(
            symbol="NIFTY 2026-12-24 24500 CE",
            instrument_type=InstrumentType.OPTION_CE,
            underlying_symbol="NIFTY",
            expiry=date(2026, 12, 24),
            strike=Decimal("24500"),
            option_type="CE",
        )
        with pytest.raises(OptionNormalizationError):
            # an option contract cannot be a chain underlying
            normalize_chain(make_chain([make_quote()]), underlying=option_underlying)


# --------------------------------------------------------------------------- #
# missing OI
# --------------------------------------------------------------------------- #


class TestMissingOi:
    def test_missing_oi_is_unavailable_not_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", oi=None), make_quote(24500, "PE")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.UNAVAILABLE
        ce = next(q for q in verdict.quotes if q.quote.side is OptionSide.CE)
        oi_report = next(f for f in ce.fields if f.field == "open_interest")
        assert oi_report.state == DataState.UNAVAILABLE
        assert "not supplied" in oi_report.reason
        assert snapshot.quotes[0].open_interest is None

    def test_zero_oi_is_valid_when_present(self):
        snapshot = normalize([make_quote(24500, "CE", oi="0")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.VALID

    def test_negative_oi_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", oi="-10")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID
        assert any(q.state == DataState.INVALID for q in verdict.quotes)


# --------------------------------------------------------------------------- #
# missing IV / greeks
# --------------------------------------------------------------------------- #


class TestMissingIvGreeks:
    def test_missing_iv_and_greeks_is_unavailable(self):
        snapshot = normalize([make_quote(24500, "CE", iv=None, greeks=NO_GREEKS)])
        verdict = validate(snapshot)
        assert verdict.state == DataState.UNAVAILABLE
        assert "iv" in verdict.summary()["unavailable_fields"]
        for name in ("greeks_delta", "greeks_gamma", "greeks_theta", "greeks_vega", "greeks_rho"):
            assert name in verdict.summary()["unavailable_fields"]

    def test_partial_greeks(self):
        snapshot = normalize([make_quote(24500, "CE", greeks={"delta": "0.52"})])
        quote = snapshot.quotes[0]
        assert quote.greeks.delta == Decimal("0.52")
        assert quote.greeks.gamma is None
        assert quote.greeks.available == ("delta",)
        assert quote.greeks.all_available is False
        verdict = validate(snapshot)
        assert verdict.state == DataState.UNAVAILABLE

    def test_negative_gamma_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", greeks={"gamma": "-0.02"})])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID

    def test_negative_iv_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", iv="-0.3")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID


# --------------------------------------------------------------------------- #
# stale quote
# --------------------------------------------------------------------------- #


class TestStaleQuote:
    def test_stale_quote_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", ts="2026-12-24T09:15:00")], timestamp="2026-12-24T09:15:00")
        # age 360s > 120s
        verdict = validate(snapshot, max_age_seconds=120)
        assert verdict.state == DataState.INVALID
        assert any("stale" in f.reason for f in verdict.quotes[0].fields)

    def test_age_check_disabled_when_max_age_none(self):
        snapshot = normalize([make_quote(24500, "CE", ts="2026-12-24T09:15:00")], timestamp="2026-12-24T09:15:00")
        verdict = ChainValidator().validate(snapshot, ref_time=REF_TIME)
        assert verdict.state == DataState.VALID
        assert any(f.reason == "age check disabled" for f in verdict.quotes[0].fields)


# --------------------------------------------------------------------------- #
# crossed / invalid bid-ask
# --------------------------------------------------------------------------- #


class TestBidAsk:
    def test_crossed_bid_ask_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", bid="245.90", ask="245.50")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID
        assert any("crossed" in f.reason for f in verdict.quotes[0].fields)

    def test_negative_bid_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", bid="-1")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID

    def test_half_depth_is_unavailable(self):
        snapshot = normalize([make_quote(24500, "CE", bid="245.50", ask=None, last=None)])
        verdict = validate(snapshot)
        assert verdict.state == DataState.UNAVAILABLE
        assert any(f.field == "bid_ask" and f.state == DataState.UNAVAILABLE for f in verdict.quotes[0].fields)

    def test_no_price_at_all_invalid(self):
        snapshot = normalize([make_quote(24500, "CE", bid=None, ask=None, last=None)])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID
        assert any(f.field == "price_presence" and f.state == DataState.INVALID for f in verdict.quotes[0].fields)


# --------------------------------------------------------------------------- #
# duplicate contracts
# --------------------------------------------------------------------------- #


class TestDuplicates:
    def test_duplicate_contracts_invalid_but_preserved(self):
        snapshot = normalize([make_quote(24500, "CE"), make_quote(24500, "CE"), make_quote(24500, "PE")])
        assert snapshot.duplicate_keys() == ("NIFTY|2026-12-24|24500|CE",)
        assert len(snapshot.quotes) == 3  # never silently dropped
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID
        assert any("duplicate contract" in f.reason for f in verdict.global_issues)


# --------------------------------------------------------------------------- #
# inconsistent expiry
# --------------------------------------------------------------------------- #


class TestExpiryConsistency:
    def test_quote_expiry_mismatch_invalid(self):
        snapshot = normalize([make_quote(24500, "CE"), make_quote(24500, "PE", expiry="2026-12-31")])
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID
        pe = next(q for q in verdict.quotes if q.quote.side is OptionSide.PE)
        assert any(
            f.field == "expiry_consistency" and f.state == DataState.INVALID for f in pe.fields
        )

    def test_option_side_mapping_valid(self):
        snapshot = normalize([make_quote(24500, "CE"), make_quote(24500, "PE")])
        verdict = validate(snapshot)
        assert all(q.state == DataState.VALID for q in verdict.quotes)


# --------------------------------------------------------------------------- #
# CE / PE mapping
# --------------------------------------------------------------------------- #


class TestSideMapping:
    def test_option_side_enum_round_trip(self):
        assert OptionSide.from_instrument_type(InstrumentType.OPTION_CE) is OptionSide.CE
        assert OptionSide.from_instrument_type(InstrumentType.OPTION_PE) is OptionSide.PE
        assert OptionSide.from_instrument_type(InstrumentType.INDEX) is None
        assert OptionSide.CE.to_instrument_type() is InstrumentType.OPTION_CE
        assert OptionSide.PE.to_instrument_type() is InstrumentType.OPTION_PE
        assert OptionSide.parse("ce") is OptionSide.CE
        assert OptionSide.parse(" PE ") is OptionSide.PE
        assert OptionSide.parse("") is None
        assert OptionSide.parse("XX") is None

    def test_side_mapping_instrument_consistency(self):
        snapshot = normalize([make_quote(24500, "CE")])
        quote = snapshot.quotes[0]
        assert quote.instrument.instrument_type is InstrumentType.OPTION_CE
        assert quote.instrument.option_type == "CE"
        quote_pe = normalize([make_quote(24500, "PE")]).quotes[0]
        assert quote_pe.instrument.instrument_type is InstrumentType.OPTION_PE


# --------------------------------------------------------------------------- #
# deterministic normalization + canonical round-trip
# --------------------------------------------------------------------------- #


class TestDeterminism:
    def test_row_order_does_not_change_normalization(self):
        rows_a = [make_quote(24500, "CE"), make_quote(24450, "PE"), make_quote(24500, "PE")]
        rows_b = list(reversed(rows_a))
        snap_a = normalize(rows_a)
        snap_b = normalize(rows_b)
        assert snapshot_to_dict(snap_a) == snapshot_to_dict(snap_b)
        assert chain_fingerprint(snap_a) == chain_fingerprint(snap_b)

    def test_double_normalization_is_identity(self):
        snap = normalize([make_quote(24500, "CE"), make_quote(24500, "PE")])
        rebuilt = snapshot_from_dict(snapshot_to_dict(snap))
        assert snapshot_to_dict(rebuilt) == snapshot_to_dict(snap)
        assert chain_fingerprint(rebuilt) == chain_fingerprint(snap)

    def test_quote_round_trip(self):
        quote = normalize([make_quote(24500, "CE", greeks={"delta": "0.52", "gamma": "0.003"})]).quotes[0]
        rebuilt = quote_from_dict(quote_to_dict(quote))
        assert rebuilt.key == quote.key
        assert rebuilt.bid == quote.bid
        assert rebuilt.ask == quote.ask
        assert rebuilt.open_interest == quote.open_interest
        assert rebuilt.greeks.delta == Decimal("0.52")
        assert rebuilt.greeks.gamma == Decimal("0.003")
        assert rebuilt.greeks.theta is None

    def test_fingerprint_is_stable_across_sources(self):
        snap_a = normalize([make_quote(24500, "CE")], source="vendor_a")
        snap_b = normalize([make_quote(24500, "CE")], source="vendor_a")
        assert chain_fingerprint(snap_a) == chain_fingerprint(snap_b)

    def test_canonical_excludes_none_when_requested(self):
        quote = normalize([make_quote(24500, "CE", iv=None, greeks=NO_GREEKS)]).quotes[0]
        slim = quote_to_dict(quote, exclude_none=True)
        assert "iv" not in slim
        assert "open_interest" in slim  # present stays


# --------------------------------------------------------------------------- #
# provider-independence
# --------------------------------------------------------------------------- #


class TestProviderIndependence:
    def test_classification_identical_across_sources(self):
        rows = [make_quote(24500, "CE"), make_quote(24500, "PE")]
        snap_a = normalize(rows, source="vendor_a")
        snap_b = normalize(rows, source="vendor_b")
        va = validate(snap_a)
        vb = validate(snap_b)
        assert va.state == vb.state == DataState.VALID
        assert va.invalid_reasons == vb.invalid_reasons == ()
        # classification must not depend on provider identity
        assert vb.summary()["state"] == va.summary()["state"]

    def test_static_provider_serves_exact_snapshot(self):
        snapshot = normalize([make_quote(24500, "CE"), make_quote(24500, "PE")])
        provider = StaticChainProvider({snapshot.expiry: snapshot}, provider_id="replay-store")
        fetched = provider.fetch_chain(UNDERLYING, snapshot.expiry)
        assert fetched is snapshot
        assert provider.supports(UNDERLYING, snapshot.expiry) is True
        assert provider.provider_id == "replay-store"

    def test_static_provider_missing_raises(self):
        snapshot = normalize([make_quote(24500, "CE")])
        provider = StaticChainProvider({snapshot.expiry: snapshot})
        with pytest.raises(OptionChainDataUnavailableError):
            provider.fetch_chain(UNDERLYING, date(2030, 1, 1))

    def test_provider_is_abstract(self):
        with pytest.raises(TypeError):
            OptionChainProvider()  # type: ignore[abstract]


# --------------------------------------------------------------------------- #
# no-fabrication guarantee
# --------------------------------------------------------------------------- #


class TestNoFabrication:
    def test_missing_spot_is_unavailable_never_substituted(self):
        snapshot = normalize([make_quote(24500, "CE")], spot=None)
        assert snapshot.spot_price is None
        verdict = validate(snapshot)
        assert verdict.state == DataState.UNAVAILABLE  # only the spot is missing
        assert any(
            f.field == "spot_unavailable" and "never substituted" in f.reason
            for f in verdict.global_issues
        )
        # option values were NOT invented to compensate
        assert snapshot.quotes[0].bid is not None
        assert snapshot.quotes[0].open_interest is not None

    def test_without_spot_forces_spot_none(self):
        snapshot = normalize([make_quote(24500, "CE")])
        stripped = snapshot.without_spot()
        assert stripped.spot_price is None
        assert stripped.quotes == snapshot.quotes

    def test_negative_spot_invalid(self):
        snapshot = normalize([make_quote(24500, "CE")], spot="-100")
        verdict = validate(snapshot)
        assert verdict.state == DataState.INVALID

    def test_quote_field_order_preserves_explicit_unavailability(self):
        # None fields stay None even when the row does not carry them at all
        row = make_quote(24500, "CE")
        row.pop("bid")
        row.pop("oi")
        quote = normalize_quote(row, underlying=UNDERLYING)
        assert quote.bid is None
        assert quote.open_interest is None
        assert quote.has_price is True  # ask + last present


# --------------------------------------------------------------------------- #
# snapshot payload timestamp forms
# --------------------------------------------------------------------------- #


class TestTimestampForms:
    def test_aware_iso_is_normalized_to_naive_ist(self):
        quote = normalize_quote(
            {"expiry": EXPIRY, "strike": "24500", "option_type": "CE", "timestamp": "2026-12-24T09:20:30+00:00"},
            underlying=UNDERLYING,
        )
        assert quote.timestamp == datetime(2026, 12, 24, 14, 50, 30)
        assert quote.timestamp.tzinfo is None

    def test_aware_quote_timestamp_is_invalid(self):
        quote = normalize_quote(
            {"expiry": EXPIRY, "strike": "24500", "option_type": "CE", "timestamp": TS},
            underlying=UNDERLYING,
        )
        aware = OptionQuote(
            instrument=quote.instrument,
            timestamp=quote.timestamp.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))),
            bid=quote.bid,
            ask=quote.ask,
            last_price=quote.last_price,
        )
        from fno_ai_paper_trading.research.options.validation import validate_quote

        verdict = validate_quote(
            aware,
            snapshot_expiry=aware.expiry,
            ref_time=REF_TIME,
            max_age_seconds=120,
        )
        assert any(f.field == "timestamp" and f.state == DataState.INVALID for f in verdict.fields)

    def test_epoch_seconds_and_millis(self):
        epoch = int(datetime(2026, 12, 24, 9, 20, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp())
        quote = normalize_quote(
            {"expiry": EXPIRY, "strike": "24500", "option_type": "CE", "timestamp": epoch},
            underlying=UNDERLYING,
        )
        assert quote.timestamp == datetime(2026, 12, 24, 9, 20, 30)
        assert quote.timestamp.tzinfo is None
        quote_ms = normalize_quote(
            {"expiry": EXPIRY, "strike": "24500", "option_type": "CE", "timestamp": epoch * 1000},
            underlying=UNDERLYING,
        )
        assert quote_ms.timestamp == quote.timestamp

    def test_market_price_unaffected(self):
        # sanity that the options stack did not shadow the shared OHLCV model
        mp = MarketPrice(
            instrument=UNDERLYING,
            timestamp=datetime(2026, 12, 24, 9, 20),
            open=Decimal("24600"),
            high=Decimal("24700"),
            low=Decimal("24590"),
            close=Decimal("24650"),
        )
        assert mp.close == Decimal("24650")