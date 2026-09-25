"""Tests for the authoritative Upstox F&O instrument resolver (read-only).

Everything is offline and deterministic: fixture rows mirror the real NSE.json.gz
BOD master schema (verified against the live file), and the execution-adapter
checks use a fake transport. No network, no credentials, no order writes.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.data.errors import InstrumentNotFoundError, MarketDataError
from fno_ai_paper_trading.data.upstox_instruments import (
    EXPIRY_BUCKETS,
    contracts_from_rows,
    load_nse_rows,
    resolve_fno_contract,
)
from fno_ai_paper_trading.execution.upstox import (
    UpstoxCredentials,
    UpstoxExecutionAdapter,
)
from fno_ai_paper_trading.models.enums import InstrumentType

TODAY = date(2026, 9, 15)


def _row(under, strike, oe, day, mon, yy, token, lot=65, tick="5", segment="NSE_FO", instrument_type=None):
    return {
        "exchange": "NSE",
        "segment": segment,
        "instrument_type": oe if instrument_type is None else instrument_type,
        "instrument_key": f"NSE_FO|{token}",
        "exchange_token": str(token),
        "trading_symbol": f"{under} {strike} {oe} {day} {mon} {yy}",
        "lot_size": lot,
        "tick_size": tick,
        "freeze_quantity": 1000,
        "name": "",
        "isin": "",
        "qty_multiplier": 1.0,
        "security_type": "NORMAL",
    }


def _fixture_rows():
    rows = [
        # NIFTY weekly Thursdays: 17 SEP 26 and 24 SEP 26; monthly 29 SEP 26; 27 OCT 26.
        _row("NIFTY", "24500", "CE", "17", "SEP", "26", 101),
        _row("NIFTY", "24500", "CE", "17", "SEP", "26", 1020, segment="NSE_FO"),
        _row("NIFTY", "24550", "CE", "17", "SEP", "26", 102, tick="5.0"),
        _row("NIFTY", "24500", "PE", "17", "SEP", "26", 103),
        _row("NIFTY", "24500", "CE", "24", "SEP", "26", 200),
        _row("NIFTY", "24600", "CE", "29", "SEP", "26", 300),
        _row("NIFTY", "24600", "PE", "29", "SEP", "26", 301),
        _row("NIFTY", "24600", "CE", "27", "OCT", "26", 400),
        _row("NIFTY", "24500", "FUT", "29", "SEP", "26", 500, instrument_type="FUT"),
        _row("BANKNIFTY", "60000", "CE", "24", "SEP", "26", 700),
        _row("NIFTY", "24500", "CE", "17", "SEP", "26", 900, lot=0),  # malformed: zero lot
        # Non-FO rows must be ignored.
        {"exchange": "NSE", "segment": "NSE_EQ", "instrument_type": "EQ",
         "instrument_key": "NSE_EQ|123", "exchange_token": "123",
         "trading_symbol": "TATA", "lot_size": 1, "tick_size": 1.0},
    ]
    # Deduplicate the duplicated weekly row id (same expiry/strike/oe twice).
    del rows[1]
    return rows


def _contracts():
    return contracts_from_rows(_fixture_rows())


def _resolve(**kwargs):
    kwargs.setdefault("contracts", _contracts())
    kwargs.setdefault("underlying", "NIFTY 50")
    kwargs.setdefault("today", TODAY)
    return resolve_fno_contract(**kwargs)


class TestMasterParsing:
    def test_parses_nfo_options_only(self):
        contracts = _contracts()
        assert len(contracts) == 8  # NIFTY CE/PE + BANKNIFTY CE; FUT, EQ, zero-lot skipped
        assert {c.option_type for c in contracts} == {"CE", "PE"}

    def test_fields_parsed_from_rows(self):
        contract = next(c for c in _contracts() if c.instrument_key == "NSE_FO|200")
        assert contract is not None
        assert contract.strike == Decimal("24500")
        assert contract.expiry == date(2026, 9, 24)
        assert contract.option_type == "CE"

    def test_numeric_token_and_symbol(self):
        contract = _resolve(expiry="2026-09-24", strike="24500")
        assert contract.instrument_key == "NSE_FO|200"
        assert contract.instrument_token == 200
        assert contract.trading_symbol == "NIFTY 24500 CE 24 SEP 26"
        assert contract.lot_size == 65
        assert contract.tick_size == Decimal("5")

    def test_non_json_master_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(MarketDataError):
            load_nse_rows(path)

    def test_missing_master_file_rejected(self, tmp_path):
        with pytest.raises(MarketDataError):
            load_nse_rows(tmp_path / "missing.json")

    def test_load_plain_json_master(self, tmp_path):
        path = tmp_path / "NSE.json"
        path.write_text(json.dumps(_fixture_rows()), encoding="utf-8")
        rows = load_nse_rows(path)
        assert len(contracts_from_rows(rows)) == 8


class TestResolution:
    def test_exact_expiry_and_strike(self):
        contract = _resolve(expiry="2026-09-17", strike="24500")
        assert contract.expiry == date(2026, 9, 17)
        assert contract.strike == Decimal("24500")
        assert contract.option_type == "CE"
        assert contract.instrument_token == 101

    def test_pe_selection(self):
        contract = _resolve(expiry="2026-09-17", strike="24500", option_type="PE")
        assert contract.option_type == "PE"
        assert contract.instrument_token == 103

    def test_expiry_buckets(self):
        assert _resolve(expiry_bucket="current_week", spot_price="24525").expiry == date(2026, 9, 17)
        assert _resolve(expiry_bucket="next_week", spot_price="24525").expiry == date(2026, 9, 24)
        assert _resolve(expiry_bucket="current_month", spot_price="24600").expiry == date(2026, 9, 29)
        assert _resolve(expiry_bucket="next_month", spot_price="24600").expiry == date(2026, 10, 27)

    def test_default_bucket_is_current_week(self):
        assert _resolve(spot_price="24525").expiry == date(2026, 9, 17)

    def test_atm_strike_tie_prefers_lower(self):
        # Spot exactly between 24500 and 24550 -> the lower strike wins.
        contract = _resolve(expiry="2026-09-17", strike=None, spot_price="24525")
        assert contract.strike == Decimal("24500")

    def test_atm_strike_nearest(self):
        contract = _resolve(expiry="2026-09-17", strike=None, spot_price="24549")
        assert contract.strike == Decimal("24550")

    def test_spot_required_without_strike(self):
        with pytest.raises(ValueError):
            _resolve(expiry="2026-09-17", strike=None)

    def test_expired_contract_rejected(self):
        with pytest.raises(InstrumentNotFoundError):
            _resolve(expiry="2026-09-14", strike="24500")

    def test_future_only_for_bucket(self):
        # 17 SEP 26 kept as future relative to today; past expiry only -> error.
        rows = contracts_from_rows([_row("NIFTY", "24500", "CE", "08", "SEP", "26", 999)])
        with pytest.raises(InstrumentNotFoundError):
            resolve_fno_contract(rows, underlying="NIFTY 50", option_type="CE",
                                 expiry_bucket="current_week", today=date(2026, 9, 15))

    def test_unknown_underlying_not_found(self):
        with pytest.raises(InstrumentNotFoundError):
            _resolve(expiry="2026-09-17", strike="24500", underlying="SENSEX")

    def test_unknown_strike_lists_diagnostic(self):
        with pytest.raises(InstrumentNotFoundError) as exc:
            _resolve(expiry="2026-09-17", strike="999999")
        assert "strikes" in str(exc.value)

    def test_bad_bucket_rejected(self):
        with pytest.raises(ValueError):
            _resolve(expiry_bucket="far_month")

    def test_bucket_names_validated(self):
        assert set(EXPIRY_BUCKETS) == {"current_week", "next_week", "current_month", "next_month"}

    def test_bad_option_type_rejected(self):
        with pytest.raises(ValueError):
            _resolve(expiry="2026-09-17", strike="24500", option_type="XX")


class TestContractModel:
    def test_to_instrument(self):
        contract = _resolve(expiry="2026-09-17", strike="24500")
        instrument = contract.to_instrument()
        assert instrument.is_option()
        assert instrument.instrument_type is InstrumentType.OPTION_CE
        assert instrument.expiry == date(2026, 9, 17)
        assert instrument.exchange_token == contract.instrument_key
        assert instrument.lot_size == 65

    def test_to_adapter_tokens(self):
        contract = _resolve(expiry="2026-09-17", strike="24500")
        assert contract.to_adapter_tokens() == {contract.instrument_key: contract.instrument_token}

    def test_describe_is_secret_free(self):
        contract = _resolve(expiry="2026-09-17", strike="24500")
        summary = contract.describe()
        assert summary["instrument_key"] == "NSE_FO|101"
        assert "token" not in " ".join(str(v) for k, v in summary.items() if "token" not in k)


# ------------------------------------------------------------- adapter wiring


class _FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []
        self.queue = list(self.responses)

    def __call__(self, method, url, *, headers=None, timeout=10.0, data=None, **kwargs):
        self.calls.append((method, url))
        if self.queue:
            return self.queue.pop(0)
        from fno_ai_paper_trading.utils.http import HttpResponse
        return HttpResponse(status=200, body=b"{}", url=url, headers={})


def _response(status, payload, url="https://api.upstox.com/x"):
    from fno_ai_paper_trading.utils.http import HttpResponse
    return HttpResponse(status=status, body=json.dumps(payload).encode(), url=url, headers={})


class TestAdapterTokenWiring:
    def _adapter(self, transport, tokens=None, dry_run=True):
        return UpstoxExecutionAdapter(
            UpstoxCredentials(access_token="tok"),
            dry_run=dry_run,
            instrument_tokens=tokens or {},
            request=transport,
            now_fn=lambda: __import__("datetime").datetime(2026, 9, 14, 12, 0),
        )

    def test_numeric_suffix_token_used_without_map(self):
        adapter = self._adapter(_FakeTransport())
        assert adapter._token_for("NSE_FO|57617") == 57617

    def test_map_used_for_descriptive_key(self):
        adapter = self._adapter(_FakeTransport(), tokens={"NSE_FO|24500" : 57617})
        assert adapter._token_for("NSE_FO|24500") == 57617

    def test_non_numeric_key_without_map_refused(self):
        adapter = self._adapter(_FakeTransport())
        with pytest.raises(Exception, match="refuses to guess"):
            adapter._token_for("NSE_FO|NIFTY 24500 CE 17 SEP 26")

    def test_quote_accepts_colon_keyed_response(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:57617": {"ohlc": {"close": "247.50"}}}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|57617") == Decimal("247.50")

    def test_quote_accepts_suffix_match(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 24500 CE 17 SEP 26": {"ohlc": {"close": "245.25"}}}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|NIFTY 24500 CE 17 SEP 26") == Decimal("245.25")

    def test_quote_still_accepts_pipe_keyed_response(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO|24500": {"ohlc": {"close": "250.00"}}}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|24500") == Decimal("250.00")

    def test_quote_missing_close_raises(self):
        transport = _FakeTransport([_response(200, {"data": {"NSE_FO:57617": {"ohlc": {}}}})])
        with pytest.raises(Exception, match="no close price"):
            self._adapter(transport).quote("NSE_FO|57617")

    def test_quote_uses_query_form_url_with_interval(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"close": "24550.00"},
                "last_price": "24560.00",
                "instrument_token": "NSE_FO|48704",
            }}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|48704") == Decimal("24550.00")
        (method, url) = transport.calls[-1]
        assert method == "GET"
        assert url == "https://api.upstox.com/v2/market-quote/ohlc?instrument_key=NSE_FO%7C48704&interval=1d"

    def test_quote_uses_ohlc_close_ahead_of_last_price(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"close": "24550.00"},
                "last_price": "99999.00",
                "instrument_token": "NSE_FO|48704",
            }}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|48704") == Decimal("24550.00")

    def test_quote_falls_back_to_last_price_when_close_absent(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"open": "24300.00", "high": "24600.00", "low": "24200.00"},
                "last_price": "24560.00",
                "instrument_token": "NSE_FO|48704",
            }}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|48704") == Decimal("24560.00")

    def test_quote_realistic_descriptive_key_via_instrument_token(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"close": "24550.00"},
                "last_price": "24560.00",
                "instrument_token": "NSE_FO|48704",
            }}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO|48704") == Decimal("24550.00")

    def test_quote_matches_instrument_token_with_colon_symbol(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"close": "24550.00"},
                "last_price": "24560.00",
                "instrument_token": "NSE_FO|48704",
            }}}),
        ])
        adapter = self._adapter(transport)
        assert adapter.quote("NSE_FO:48704") == Decimal("24550.00")

    def test_quote_unknown_instrument_stays_unavailable(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"close": "24550.00"},
                "last_price": "24560.00",
                "instrument_token": "NSE_FO|99999",
            }}}),
        ])
        adapter = self._adapter(transport)
        with pytest.raises(Exception, match="no close price"):
            adapter.quote("NSE_FO|48704")

    def test_quote_no_match_when_token_field_missing(self):
        transport = _FakeTransport([
            _response(200, {"data": {"NSE_FO:NIFTY 27 OCT 26": {
                "ohlc": {"close": "24550.00"},
                "last_price": "24560.00",
            }}}),
        ])
        adapter = self._adapter(transport)
        with pytest.raises(Exception, match="no close price"):
            adapter.quote("NSE_FO|48704")

    def test_positions_map_token_back_to_key(self):
        transport = _FakeTransport([
            _response(200, {"data": [
                {"trading_symbol": "NIFTY 24500 CE 17 SEP 26", "instrument_token": 57617,
                 "quantity": 65, "average_price": "249.00", "pnl": "5.00"},
            ]}),
        ])
        adapter = self._adapter(transport, tokens={"NSE_FO|57617": 57617}, dry_run=False)
        positions = adapter.get_positions()
        assert positions[0].symbol == "NSE_FO|57617"
        assert positions[0].quantity == 65

    def test_positions_falls_back_to_net_quantity_and_display_symbol(self):
        transport = _FakeTransport([
            _response(200, {"data": [
                {"trading_symbol": "SOME OTHER", "net_quantity": 10},
            ]}),
        ])
        adapter = self._adapter(transport, dry_run=False)
        positions = adapter.get_positions()
        assert positions[0].symbol == "SOME OTHER"
        assert positions[0].quantity == 10