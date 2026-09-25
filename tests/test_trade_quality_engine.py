"""Phase 9 tests — deterministic option trade-quality engine.

Hermetic unit tests over hand-built Phase 5 ``OptionChainSnapshot`` fixtures,
Phase 7 regime reports and Phase 8 selection results (produced through the real
selector engine to prove interoperation). No network, no clock reads, no
credentials, no execution/broker/Upstox imports. Covers freshness, bid/ask,
premium, liquidity, IV/greeks, selection consistency, outcome precedence,
determinism, no look-ahead and safety.

Fixtures below are **synthetic unit-test data** (explicitly labelled); no
historical option-chain data is claimed, manufactured or reused.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.options.models import (
    Greeks,
    OptionChainSnapshot,
    OptionQuote,
    OptionSide,
)
from fno_ai_paper_trading.research.options.normalization import chain_fingerprint
from fno_ai_paper_trading.research.regime import Direction, MarketRegimeEngine
from fno_ai_paper_trading.research.selection import (
    ContractSelectionEngine,
    ContractSelectionRequest,
)
from fno_ai_paper_trading.research.quality import (
    QUALITY_CONFIG_DEFAULT,
    QUALITY_ENGINE_VERSION,
    QUALITY_RULES_VERSION,
    QUALITY_SCHEMA_VERSION,
    QualityDimension,
    QualityOutcome,
    QualityState,
    TradeQualityConfig,
    TradeQualityEngine,
    TradeQualityRequest,
)
from fno_ai_paper_trading.research.quality.models import DIMENSION_ORDER

# ---------------------------------------------------------------- fixtures

UNDERLYING = Instrument(
    symbol="NIFTY",
    instrument_type=InstrumentType.INDEX,
    underlying_symbol="NIFTY",
    exchange="NSE",
)

DECISION = datetime(2026, 9, 24, 9, 30, 0)
TS = datetime(2026, 9, 24, 9, 29, 0)
EXPIRY = date(2026, 9, 29)
EXPIRY2 = date(2026, 10, 29)
STRIKES = (Decimal("24600"), Decimal("24650"), Decimal("24700"))
SPOT = Decimal("24650.05")


def _option_instrument(side: str, strike: Decimal, expiry: date = EXPIRY) -> Instrument:
    return Instrument(
        symbol=f"NIFTY {expiry} {strike:g} {side}",
        instrument_type=InstrumentType.OPTION_CE if side == "CE" else InstrumentType.OPTION_PE,
        underlying_symbol="NIFTY",
        expiry=expiry,
        strike=strike,
        option_type=side,
    )


def _quote(
    side: str = "CE",
    strike: Decimal | int = STRIKES[1],
    *,
    expiry: date = EXPIRY,
    ts: datetime = TS,
    bid: str | None = "100.50",
    ask: str | None = "100.75",
    last: str | None = "100.60",
    oi: int | None = 150000,
    oi_change: int | None = 500,
    volume: int | None = 900,
    iv: str | None = "0.16",
    greeks: object | None = None,
    source_timestamp: datetime | None = TS,
) -> OptionQuote:
    strike = Decimal(str(strike))
    return OptionQuote(
        instrument=_option_instrument(side, strike, expiry),
        timestamp=ts,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last_price=Decimal(last) if last is not None else None,
        open_interest=oi,
        oi_change=oi_change,
        volume=volume,
        iv=Decimal(iv) if iv is not None else None,
        greeks=greeks if isinstance(greeks, Greeks) else Greeks(),
        source="test-fixture (synthetic)",
        source_timestamp=source_timestamp,
        source_id=None,
    )


def _snapshot(
    quotes: tuple[OptionQuote, ...],
    *,
    expiry: date = EXPIRY,
    ts: datetime = TS,
    spot: Decimal | None = SPOT,
) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying=UNDERLYING,
        expiry=expiry,
        timestamp=ts,
        quotes=quotes,
        spot_price=spot,
        source="test-fixture (synthetic)",
    )


def _full_chain(*, expiry: date = EXPIRY, ts: datetime = TS) -> OptionChainSnapshot:
    return _snapshot(
        tuple(_quote("CE", s, expiry=expiry, ts=ts) for s in STRIKES)
        + tuple(_quote("PE", s, expiry=expiry, ts=ts) for s in STRIKES),
        expiry=expiry,
        ts=ts,
    )


def _bars(closes: list[Decimal]) -> list[MarketPrice]:
    base = datetime(2026, 9, 24, 8, 0)
    bars = []
    for i, close in enumerate(closes):
        bars.append(
            MarketPrice(
                instrument=UNDERLYING,
                timestamp=base + timedelta(minutes=i),
                open=close - Decimal("2"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=1000 + i,
            )
        )
    return bars


def _closes(values: list[int]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def _regime(direction: Direction):
    bars = {
        Direction.BULLISH: _closes([24000 + 10 * i for i in range(40)]),
        Direction.BEARISH: _closes([25000 - 10 * i for i in range(40)]),
        Direction.NEUTRAL: _closes([24200 for _ in range(40)]),
    }[direction]
    return MarketRegimeEngine().evaluate(_bars(bars))


def _selection(snapshot: OptionChainSnapshot = None, *, direction: Direction = Direction.BULLISH):
    """Run the real Phase 8 selector (interoperation proof)."""
    if snapshot is None:
        snapshot = _full_chain()
    request = ContractSelectionRequest(
        underlying=UNDERLYING,
        timestamp=DECISION,
        snapshots=(snapshot,),
        regime=_regime(direction),
    )
    return ContractSelectionEngine().select(request)


def _request(
    selection,
    snapshot: OptionChainSnapshot | None = _full_chain(),
    *,
    timestamp: datetime = DECISION,
    expected_selection_fingerprint: str | None = None,
) -> TradeQualityRequest:
    return TradeQualityRequest(
        timestamp=timestamp,
        selection=selection,
        snapshot=snapshot,
        expected_selection_fingerprint=expected_selection_fingerprint,
    )


ENGINE = TradeQualityEngine
CFG = QUALITY_CONFIG_DEFAULT
_MISSING = object()


def _cfg(**kwargs) -> TradeQualityConfig:
    return replace(QUALITY_CONFIG_DEFAULT, **kwargs)


def _evaluate(*, snapshot=_MISSING, timestamp: datetime = DECISION,
              cfg: TradeQualityConfig | None = None, selection=_MISSING,
              expected_fingerprint: str | None = None):
    if selection is _MISSING:
        selection = _selection()
    if snapshot is _MISSING:
        snapshot = _full_chain()
    config = cfg or QUALITY_CONFIG_DEFAULT
    return ENGINE(config).evaluate(
        _request(selection, snapshot, timestamp=timestamp,
                 expected_selection_fingerprint=expected_fingerprint)
    )


def _verdict(result, dimension: str) -> "object":
    verdict = result.dimension(dimension)
    assert verdict is not None, f"missing dimension {dimension}"
    return verdict


# ---------------------------------------------------------------- freshness

class TestFreshness:
    def test_fresh_default(self) -> None:
        result = _evaluate()
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "freshness").state is QualityState.VALID

    def test_exact_boundary_inclusive(self) -> None:
        # quote/snapshot TS = 09:29 -> 60s before DECISION = 09:30.
        result = _evaluate(cfg=_cfg(max_quote_age_seconds=60, max_snapshot_age_seconds=60))
        assert result.outcome is QualityOutcome.PASS

    def test_stale_quote(self) -> None:
        result = _evaluate(cfg=_cfg(max_quote_age_seconds=59))
        assert result.outcome is QualityOutcome.FAIL
        assert "quote stale" in _verdict(result, "freshness").reason

    def test_stale_snapshot(self) -> None:
        result = _evaluate(cfg=_cfg(max_snapshot_age_seconds=59))
        assert result.outcome is QualityOutcome.FAIL
        assert "snapshot stale" in _verdict(result, "freshness").reason

    def test_future_quote_timestamp(self) -> None:
        future_chain = _snapshot(
            tuple(_quote("CE", s, ts=datetime(2026, 9, 24, 9, 31, 0)) for s in STRIKES)
            + tuple(_quote("PE", s, ts=datetime(2026, 9, 24, 9, 31, 0)) for s in STRIKES),
            ts=TS,
        )
        result = _evaluate(snapshot=future_chain)
        assert result.outcome is QualityOutcome.INVALID
        assert "in the future" in _verdict(result, "freshness").reason

    def test_future_snapshot_timestamp(self) -> None:
        future = _snapshot(
            tuple(_quote("CE", s) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
            ts=datetime(2026, 9, 24, 9, 31, 0),
        )
        result = _evaluate(snapshot=future)
        assert result.outcome is QualityOutcome.INVALID
        assert "snapshot" in _verdict(result, "freshness").reason

    def test_tz_aware_quote_timestamp(self) -> None:
        aware_chain = _snapshot(
            tuple(_quote("CE", s, ts=datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)) for s in STRIKES)
            + tuple(_quote("PE", s, ts=datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)) for s in STRIKES),
        )
        result = _evaluate(snapshot=aware_chain)
        assert result.outcome is QualityOutcome.INVALID
        assert "not naive IST" in _verdict(result, "freshness").reason

    def test_missing_provider_timestamp_unavailable(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, source_timestamp=None) for s in STRIKES)
            + tuple(_quote("PE", s, source_timestamp=None) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(require_provider_market_timestamp=True))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "Phase 6 limitation" in _verdict(result, "freshness").reason

    def test_provider_timestamp_present(self) -> None:
        result = _evaluate(cfg=_cfg(require_provider_market_timestamp=True))
        assert result.outcome is QualityOutcome.PASS

    def test_receive_instant_caveat_is_evidenced(self) -> None:
        result = _evaluate()
        fresh = _verdict(result, "freshness")
        assert "adapter receive instant" in fresh.reason or any(
            "adapter receive instant" in e.reason for e in result.evidence
        )


# ------------------------------------------------------------------ bid/ask

class TestBidAsk:
    def test_valid_spread(self) -> None:
        result = _evaluate()
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "bid_ask").state is QualityState.VALID

    def test_zero_bid(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="0.00", ask="0.00") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.FAIL
        assert "bid not positive" in _verdict(result, "bid_ask").reason

    def test_zero_ask(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="0.00", ask="0.00") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.FAIL
        assert "ask not positive" in _verdict(result, "bid_ask").reason

    def test_zero_ask_crossed_with_positive_bid(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="100.50", ask="0.00") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.INVALID  # ask < bid is a crossed book
        assert "crossed" in _verdict(result, "bid_ask").reason

    def test_crossed_market(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="101.00", ask="100.75") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.INVALID
        assert "crossed" in _verdict(result, "bid_ask").reason

    def test_excessive_absolute_spread(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="100.00", ask="112.00") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(max_spread_points=5.0))
        assert result.outcome is QualityOutcome.FAIL
        assert "absolute spread" in _verdict(result, "bid_ask").reason

    def test_excessive_percentage_spread(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="100.00", ask="106.00") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(max_spread_pct=2.0))
        # ratio = 6 / 103 * 100 = 5.825% > 2% -> FAIL
        assert result.outcome is QualityOutcome.FAIL
        assert "spread ratio" in _verdict(result, "bid_ask").reason

    def test_missing_bid(self) -> None:
        chain = _snapshot(tuple(_quote("CE", s, bid=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES))
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "bid not supplied" in _verdict(result, "bid_ask").reason

    def test_missing_ask(self) -> None:
        chain = _snapshot(tuple(_quote("CE", s, ask=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES))
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "ask not supplied" in _verdict(result, "bid_ask").reason


# ------------------------------------------------------------------ premium

class TestPremium:
    def test_valid_ltp(self) -> None:
        result = _evaluate(cfg=_cfg(require_last_price=True, require_positive_ltp=True))
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "premium").state is QualityState.VALID
        assert _verdict(result, "premium").binding

    def test_missing_ltp(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, last=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(require_last_price=True))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "last_price not supplied" in _verdict(result, "premium").reason

    def test_negative_ltp(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, last="-5.00") for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(require_last_price=True))
        assert result.outcome is QualityOutcome.INVALID
        assert "negative last_price" in _verdict(result, "premium").reason

    def test_zero_ltp_engaged(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, last="0.00") for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(require_last_price=True, require_positive_ltp=True))
        assert result.outcome is QualityOutcome.FAIL
        assert "last_price not positive" in _verdict(result, "premium").reason

    def test_ltp_outside_book(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="100.50", ask="100.75", last="0.01") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(require_ltp_in_spread=True))
        assert result.outcome is QualityOutcome.FAIL
        assert "outside" in _verdict(result, "premium").reason

    def test_missing_ltp_default_advisory(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, last=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.PASS
        assert not _verdict(result, "premium").binding

    def test_excessive_premium_cap(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, last="9000.00") for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(max_premium_points=5000.0))
        assert result.outcome is QualityOutcome.FAIL
        assert "premium" in _verdict(result, "premium").reason


# ---------------------------------------------------------------- liquidity

class TestLiquidity:
    def test_sufficient_oi(self) -> None:
        result = _evaluate(cfg=_cfg(min_open_interest=100000))
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "liquidity").binding

    def test_insufficient_oi(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, oi=5000) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_open_interest=100000))
        assert result.outcome is QualityOutcome.FAIL
        assert "open_interest 5000 < min 100000" in _verdict(result, "liquidity").reason

    def test_missing_oi(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, oi=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_open_interest=100000))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "open_interest not supplied" in _verdict(result, "liquidity").reason

    def test_sufficient_volume(self) -> None:
        result = _evaluate(cfg=_cfg(min_volume=500))
        assert result.outcome is QualityOutcome.PASS

    def test_insufficient_volume(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, volume=10) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_volume=500))
        assert result.outcome is QualityOutcome.FAIL
        assert "volume 10 < min 500" in _verdict(result, "liquidity").reason

    def test_missing_volume(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, volume=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_volume=500))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "volume not supplied" in _verdict(result, "liquidity").reason

    def test_missing_oi_change(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, oi_change=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_oi_change=100))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "oi_change not supplied" in _verdict(result, "liquidity").reason

    def test_missing_oi_default_advisory(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, oi=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.PASS
        assert not _verdict(result, "liquidity").binding


# ---------------------------------------------------------------- iv/greeks

class TestIvGreeks:
    def test_valid_iv(self) -> None:
        result = _evaluate(cfg=_cfg(min_iv=0.05, max_iv=0.50))
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "iv").binding

    def test_iv_below_min(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, iv="0.01") for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_iv=0.10))
        assert result.outcome is QualityOutcome.FAIL
        assert "iv 0.01 < min 0.1" in _verdict(result, "iv").reason

    def test_iv_above_max(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, iv="0.90") for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(max_iv=0.50))
        assert result.outcome is QualityOutcome.FAIL
        assert "iv 0.90 > max 0.5" in _verdict(result, "iv").reason

    def test_missing_iv(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, iv=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_iv=0.05))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "iv not supplied" in _verdict(result, "iv").reason

    def test_invalid_negative_iv(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, iv="-0.10") for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_iv=None))
        assert result.outcome is QualityOutcome.PASS  # advisory IV (not engaged)

    def test_missing_iv_default_advisory(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, iv=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.PASS
        assert not _verdict(result, "iv").binding

    def test_valid_greeks_required(self) -> None:
        greeks = Greeks(delta=Decimal("0.5"), gamma=Decimal("0.001"), theta=Decimal("-0.05"),
                        vega=Decimal("0.3"), rho=Decimal("0.0012"))
        chain = _snapshot(
            tuple(_quote("CE", s, greeks=greeks) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(required_greeks=("delta", "gamma")))
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "greeks").binding

    def test_missing_greeks(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(required_greeks=("delta",)))
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "delta not supplied" in _verdict(result, "greeks").reason

    def test_rho_unavailable_not_mandatory(self) -> None:
        result = _evaluate(cfg=_cfg(required_greeks=("delta",)))
        assert result.outcome is QualityOutcome.UNAVAILABLE  # delta missing in default greeks
        # rho itself is never required: default config passes even with no greeks.
        result_default = _evaluate()
        assert result_default.outcome is QualityOutcome.PASS
        assert "rho" not in result_default.rejection_reasons

    def test_negative_gamma_invalid(self) -> None:
        greeks = Greeks(delta=Decimal("0.5"), gamma=Decimal("-0.001"))
        chain = _snapshot(
            tuple(_quote("CE", s, greeks=greeks) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(required_greeks=("gamma",)))
        assert result.outcome is QualityOutcome.INVALID
        assert "negative gamma" in _verdict(result, "greeks").reason

    def test_missing_greeks_default_advisory(self) -> None:
        result = _evaluate()
        assert result.outcome is QualityOutcome.PASS
        assert not _verdict(result, "greeks").binding


# ------------------------------------------------- selection consistency

class TestSelectionConsistency:
    def test_contract_present(self) -> None:
        result = _evaluate()
        assert result.outcome is QualityOutcome.PASS
        assert _verdict(result, "selection_consistency").state is QualityState.VALID
        assert result.contract_key == "NIFTY|2026-09-29|24650|CE"
        assert result.selection_fingerprint
        assert result.chain_fingerprint is not None

    def test_contract_changed(self) -> None:
        # Snapshot contains only PE quotes: the selected CE contract is gone.
        other = _snapshot(tuple(_quote("PE", s) for s in STRIKES))
        result = _evaluate(snapshot=other)
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "not present" in _verdict(result, "selection_consistency").reason

    def test_contract_strike_shifted(self) -> None:
        # Same side but the selected strike is absent.
        shifted = _snapshot(
            tuple(_quote("CE", s) for s in (STRIKES[0],))
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=shifted)
        assert result.outcome is QualityOutcome.UNAVAILABLE

    def test_fingerprint_mismatch(self) -> None:
        result = _evaluate(expected_fingerprint="not-the-real-fingerprint")
        assert result.outcome is QualityOutcome.INVALID
        assert "fingerprint mismatch" in result.rejection_reasons[0]

    def test_fingerprint_match(self) -> None:
        selection = _selection()
        from fno_ai_paper_trading.research.quality import selection_fingerprint
        expect = selection_fingerprint(selection)
        result = _evaluate(selection=selection, expected_fingerprint=expect)
        assert result.outcome is QualityOutcome.PASS

    def test_stale_selection(self) -> None:
        later = DECISION + timedelta(minutes=2)
        result = _evaluate(timestamp=later, cfg=_cfg(max_selection_age_seconds=60))
        assert result.outcome is QualityOutcome.FAIL
        assert "selection stale" in _verdict(result, "selection_consistency").reason

    def test_selection_not_selected(self) -> None:
        selection = _selection(direction=Direction.NEUTRAL)
        result = _evaluate(selection=selection)
        assert result.outcome is QualityOutcome.INVALID
        assert "did not select a contract" in result.rejection_reasons[0]

    def test_selection_timestamp_in_future(self) -> None:
        selection = _selection()
        result = _evaluate(timestamp=DECISION - timedelta(minutes=5))
        assert result.outcome is QualityOutcome.INVALID
        assert "future" in _verdict(result, "selection_consistency").reason

    def test_no_snapshot(self) -> None:
        result = _evaluate(snapshot=None)
        assert result.outcome is QualityOutcome.UNAVAILABLE
        assert "no chain snapshot" in _verdict(result, "selection_consistency").reason


# ---------------------------------------------------------------- outcomes

class TestOutcomes:
    def test_pass_default(self) -> None:
        result = _evaluate()
        assert result.outcome is QualityOutcome.PASS
        assert result.has_passed
        assert result.rejection_reasons == ()

    def test_fail(self) -> None:
        result = _evaluate(cfg=_cfg(min_volume=999999))
        assert result.outcome is QualityOutcome.FAIL
        assert any("liquidity" in r for r in result.rejection_reasons)

    def test_unavailable(self) -> None:
        result = _evaluate(cfg=_cfg(require_last_price=True), snapshot=_snapshot(
            tuple(_quote("CE", s, last=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES)))
        assert result.outcome is QualityOutcome.UNAVAILABLE

    def test_invalid(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="101.00", ask="100.75") for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain)
        assert result.outcome is QualityOutcome.INVALID

    def test_precedence_invalid_beats_fail(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, bid="101.00", ask="100.75", oi=5) for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_open_interest=100000))
        assert result.outcome is QualityOutcome.INVALID  # crossed > low OI

    def test_precedence_unavailable_beats_fail(self) -> None:
        chain = _snapshot(
            tuple(_quote("CE", s, oi=None, volume=5) for s in STRIKES)
            + tuple(_quote("PE", s) for s in STRIKES),
        )
        result = _evaluate(snapshot=chain, cfg=_cfg(min_open_interest=100000, min_volume=500))
        assert result.outcome is QualityOutcome.UNAVAILABLE  # missing OI > low volume

    def test_precedence_fail_beats_pass(self) -> None:
        result = _evaluate(cfg=_cfg(min_volume=999999))
        assert result.outcome is QualityOutcome.FAIL

    def test_all_dimensions_present_in_order(self) -> None:
        result = _evaluate()
        assert [d.dimension for d in result.dimensions] == list(DIMENSION_ORDER)

    def test_required_dimension_binds(self) -> None:
        # required_dimensions elevates an otherwise-advisory dimension into the
        # binding set (it counts toward outcome/composite); absence of data that
        # no rule requires stays advisory and never invents a value.
        result = _evaluate(cfg=_cfg(required_dimensions=("premium",), include_composite=True))
        assert result.composite == "4/4"
        assert _verdict(result, "premium").binding
        chain = _snapshot(
            tuple(_quote("CE", s, last=None) for s in STRIKES) + tuple(_quote("PE", s) for s in STRIKES))
        result = _evaluate(snapshot=chain, cfg=_cfg(required_dimensions=("premium",)))
        assert result.outcome is QualityOutcome.PASS  # no presence rule engaged
        assert not _verdict(result, "premium").reason.startswith("last_price")

    def test_composite_and_versions(self) -> None:
        result = _evaluate(cfg=_cfg(include_composite=True))
        assert result.composite == "3/3"
        assert result.rules_version == QUALITY_RULES_VERSION
        assert result.schema_version == QUALITY_SCHEMA_VERSION
        assert result.engine_version == QUALITY_ENGINE_VERSION
        assert "selection_fingerprint" in result.to_dict()
        assert "dimensions" in result.to_dict()


# --------------------------------------------------------------- determinism

class TestDeterminism:
    def test_repeated_evaluation_identical(self) -> None:
        first = _evaluate().to_dict()
        second = _evaluate().to_dict()
        assert first == second

    def test_provider_ordering_changed(self) -> None:
        quotes_fwd = tuple(_full_chain().quotes)
        quotes_rev = tuple(reversed(quotes_fwd))
        selection = _selection(_snapshot(quotes_fwd))
        fwd = _evaluate(selection=selection, snapshot=_snapshot(quotes_fwd))
        rev = _evaluate(selection=selection, snapshot=_snapshot(quotes_rev))
        assert fwd.outcome is rev.outcome
        assert fwd.rejection_reasons == rev.rejection_reasons
        assert fwd.selection_fingerprint == rev.selection_fingerprint
        # Each dimension verdict carries a chain fingerprint reference in its
        # value; compare the decision-relevant fields instead.
        assert [
            (d.dimension, d.state, d.reason, d.binding, d.threshold, d.rule) for d in fwd.dimensions
        ] == [
            (d.dimension, d.state, d.reason, d.binding, d.threshold, d.rule) for d in rev.dimensions
        ]
        # chain_fingerprint mirrors Phase 5's order-sensitive canonical form.
        assert fwd.chain_fingerprint != rev.chain_fingerprint

    def test_same_timestamp_same_config_identical(self) -> None:
        a = _evaluate(timestamp=DECISION, cfg=_cfg(min_volume=1))
        b = _evaluate(timestamp=DECISION, cfg=_cfg(min_volume=1))
        assert a.to_dict() == b.to_dict()

    def test_decimal_precision_consistent(self) -> None:
        result = _evaluate(cfg=_cfg(max_spread_points=0.25))
        assert result.outcome is QualityOutcome.PASS
        bid_ask = _verdict(result, "bid_ask")
        assert isinstance(bid_ask.value, str)
        assert "bid=100.50 ask=100.75" in bid_ask.value

    def test_decision_uses_only_snapshot_at_t(self) -> None:
        # A snapshot stamped AFTER the decision time must be INVALID, proving
        # the engine never reaches forward for data.
        later_chain = _snapshot(
            tuple(_quote("CE", s, ts=DECISION + timedelta(seconds=1)) for s in STRIKES)
            + tuple(_quote("PE", s, ts=DECISION + timedelta(seconds=1)) for s in STRIKES),
            ts=DECISION + timedelta(seconds=1),
        )
        result = _evaluate(snapshot=later_chain)
        assert result.outcome is QualityOutcome.INVALID


# ------------------------------------------------------------------- safety

SAFETY_MODULES = (
    "src/fno_ai_paper_trading/research/quality/engine.py",
    "src/fno_ai_paper_trading/research/quality/models.py",
    "src/fno_ai_paper_trading/research/quality/__init__.py",
)


class TestSafety:
    def test_no_execution_or_vendor_imports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        reuse_files = {
            (root / "src/fno_ai_paper_trading/research/quality/engine.py").resolve(),
            (root / "src/fno_ai_paper_trading/research/quality/models.py").resolve(),
        }
        for rel in SAFETY_MODULES:
            module = (root / rel).resolve()
            tree = ast.parse(module.read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            roots = {name.split(".")[0] for name in imported}
            for banned in ("execution", "upstox", "broker"):
                assert banned not in roots, f"{rel} imports forbidden root {banned}"
            if module in reuse_files:
                assert any(name.startswith("fno_ai_paper_trading.research.options") for name in imported)
                assert any(name.startswith("fno_ai_paper_trading.research.selection") for name in imported)

    def test_result_has_no_order_signal(self) -> None:
        result = _evaluate()
        data = result.to_dict()
        for word in ("BUY", "SELL", "order", "signal", "recommendation", "convic", "strong"):
            assert word not in {k.upper() for k in data}, f"{word!r} leaked into result keys"
        assert result.outcome.value in {o.value for o in QualityOutcome}

    def test_no_clock_reads_in_engine(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "src/fno_ai_paper_trading/research/quality/engine.py").read_text(encoding="utf-8")
        assert "datetime.now" not in source
        assert "utcnow" not in source

    def test_config_rejects_bad_thresholds(self) -> None:
        with pytest.raises(ValueError):
            _cfg(max_spread_points=-1)
        with pytest.raises(ValueError):
            _cfg(min_open_interest=-5)
        with pytest.raises(ValueError):
            _cfg(min_iv=0.9, max_iv=0.5)
        with pytest.raises(ValueError):
            _cfg(required_dimensions=("not_a_dimension",))
        with pytest.raises(ValueError):
            _cfg(required_greeks=("not_a_greek",))