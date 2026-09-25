"""Phase 8 tests — deterministic option contract selector.

Hermetic unit tests over hand-built Phase 5 ``OptionChainSnapshot`` fixtures and
Phase 7 regime reports. No network, no clock reads, no credentials, no
execution/broker/Upstox imports. Covers expiry, strike, CE/PE mapping,
liquidity/market-quality filters, determinism, tie-breaking, integrity/no
look-ahead and safety.

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
from fno_ai_paper_trading.research.regime import (
    Direction,
    MarketRegimeEngine,
)
from fno_ai_paper_trading.research.selection import (
    CONTRACT_CONFIG_DEFAULT,
    ExpiryPolicy,
    OffsetUnits,
    SelectionOutcome,
    SideSource,
    StrikePolicy,
    ContractSelectionConfig,
    ContractSelectionEngine,
    pool_fingerprint,
    stable_candidate_key,
)

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
    bid: str = "100.50",
    ask: str = "100.75",
    last: str = "100.60",
    oi: int | None = 150000,
    oi_change: int | None = 500,
    volume: int | None = 900,
    iv: str | None = "0.16",
    greeks: object | None = None,
    source_id: str | None = None,
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
        source_timestamp=ts,
        source_id=source_id,
    )


def _snapshot(
    quotes: tuple[OptionQuote, ...],
    *,
    expiry: date = EXPIRY,
    ts: datetime = TS,
    spot: Decimal | None = SPOT,
    source: str = "test-fixture (synthetic)",
) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying=UNDERLYING,
        expiry=expiry,
        timestamp=ts,
        quotes=quotes,
        spot_price=spot,
        source=source,
    )


def _full_chain(*, expiry: date = EXPIRY, ts: datetime = TS, spot: Decimal | None = SPOT) -> OptionChainSnapshot:
    return _snapshot(
        tuple(_quote("CE", s, expiry=expiry, ts=ts) for s in STRIKES)
        + tuple(_quote("PE", s, expiry=expiry, ts=ts) for s in STRIKES),
        expiry=expiry,
        ts=ts,
        spot=spot,
    )


# Phase 7 regime fixtures (deterministic bar builders, mirrored from Phase 7).


def _bars(closes: list[Decimal]) -> list[MarketPrice]:
    base = datetime(2026, 9, 24, 8, 0)  # last bar must end at/before DECISION (no future regime)
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


def _insufficient_regime():
    return MarketRegimeEngine().evaluate(_bars(_closes([24000 + 10 * i for i in range(10)])))


def _request(
    snapshots: tuple[OptionChainSnapshot, ...],
    *,
    regime=None,
    expiry=None,
    option_side=None,
    timestamp: datetime = DECISION,
) -> "object":
    from fno_ai_paper_trading.research.selection import ContractSelectionRequest

    return ContractSelectionRequest(
        underlying=UNDERLYING,
        timestamp=timestamp,
        snapshots=snapshots,
        regime=regime,
        expiry=expiry,
        option_side=option_side,
    )


ENGINE = ContractSelectionEngine


def _cfg(**kwargs) -> ContractSelectionConfig:
    return replace(CONTRACT_CONFIG_DEFAULT, **kwargs)


# ------------------------------------------------------------------ expiry

class TestExpiryPolicy:
    def test_nearest_valid_expiry(self) -> None:
        result = ENGINE().select(
            _request((_full_chain(expiry=EXPIRY2, ts=TS), _full_chain(expiry=EXPIRY, ts=TS)), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_expiry == EXPIRY
        assert result.has_selection

    def test_next_expiry(self) -> None:
        result = ENGINE(_cfg(expiry_policy=ExpiryPolicy.NEXT)).select(
            _request((_full_chain(expiry=EXPIRY), _full_chain(expiry=EXPIRY2)), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_expiry == EXPIRY2

    def test_explicit_expiry_matches(self) -> None:
        result = ENGINE(_cfg(expiry_policy=ExpiryPolicy.EXPLICIT)).select(
            _request((_full_chain(expiry=EXPIRY), _full_chain(expiry=EXPIRY2)), expiry=EXPIRY2.isoformat(), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_expiry == EXPIRY2

    def test_explicit_expiry_not_in_pool(self) -> None:
        result = ENGINE(_cfg(expiry_policy=ExpiryPolicy.EXPLICIT)).select(
            _request((_full_chain(expiry=EXPIRY),), expiry="2026-11-26", regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA

    def test_malformed_expiry(self) -> None:
        result = ENGINE(_cfg(expiry_policy=ExpiryPolicy.EXPLICIT)).select(
            _request((_full_chain(expiry=EXPIRY),), expiry="not-a-date", regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("malformed" in r for r in result.rejection_reasons)

    def test_expired_contracts_only(self) -> None:
        expired = date(2026, 9, 20)  # before decision date 2026-09-24
        result = ENGINE().select(
            _request((_full_chain(expiry=expired, ts=TS),), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("expired" in r for r in result.rejection_reasons)

    def test_expired_ignored_when_valid_exists(self) -> None:
        expired = date(2026, 9, 20)
        result = ENGINE().select(
            _request(
                (_full_chain(expiry=expired, ts=TS), _full_chain(expiry=EXPIRY, ts=TS)),
                regime=_regime(Direction.BULLISH),
            )
        )
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_expiry == EXPIRY

    def test_unavailable_no_snapshots(self) -> None:
        result = ENGINE().select(_request((), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("no option-chain snapshots" in r for r in result.rejection_reasons)

    def test_next_unavailable_with_single_expiry(self) -> None:
        result = ENGINE(_cfg(expiry_policy=ExpiryPolicy.NEXT)).select(
            _request((_full_chain(expiry=EXPIRY),), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("no NEXT expiry" in r for r in result.rejection_reasons)


# ------------------------------------------------------------------- strike

class TestStrikePolicy:
    def test_atm_exact(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_strike == Decimal("24650")

    def test_atm_nearest_available(self) -> None:
        snap = _snapshot(
            (_quote("CE", 24600), _quote("CE", 24700), _quote("PE", 24600), _quote("PE", 24700)),
            spot=Decimal("24625.5"),
        )
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_strike == Decimal("24600")

    def test_atm_between_strikes_ties_lower(self) -> None:
        snap = _snapshot(
            (_quote("CE", 24600), _quote("CE", 24700), _quote("PE", 24600), _quote("PE", 24700)),
            spot=Decimal("24650"),
        )
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_strike == Decimal("24600")

    def test_offset_positive_strike(self) -> None:
        cfg = _cfg(strike_policy=StrikePolicy.ATM_OFFSET, atm_offset=1, offset_units=OffsetUnits.STRIKES)
        result = ENGINE(cfg).select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_strike == Decimal("24700")

    def test_offset_negative_strike(self) -> None:
        cfg = _cfg(strike_policy=StrikePolicy.ATM_OFFSET, atm_offset=-1, offset_units=OffsetUnits.STRIKES)
        result = ENGINE(cfg).select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_strike == Decimal("24600")

    def test_offset_points(self) -> None:
        cfg = _cfg(strike_policy=StrikePolicy.ATM_OFFSET, atm_offset=50, offset_units=OffsetUnits.POINTS)
        result = ENGINE(cfg).select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_strike == Decimal("24700")

    def test_missing_spot_no_fabrication(self) -> None:
        result = ENGINE().select(
            _request((_full_chain(spot=None),), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("spot not supplied" in r for r in result.rejection_reasons)
        assert result.spot_price is None

    def test_strike_offset_unavailable_in_chain(self) -> None:
        cfg = _cfg(strike_policy=StrikePolicy.ATM_OFFSET, atm_offset=5, offset_units=OffsetUnits.STRIKES)
        result = ENGINE(cfg).select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.NO_ELIGIBLE_CONTRACT
        assert any("strike offset is unavailable" in r for r in result.rejection_reasons)

    def test_no_quotes_for_resolved_side(self) -> None:
        snap = _snapshot(tuple(_quote("PE", s) for s in STRIKES))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("no CE contracts" in r for r in result.rejection_reasons)


# ------------------------------------------------------------------ side

class TestSideMapping:
    def test_bullish_maps_ce(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_side is OptionSide.CE
        assert result.selected_contract is not None and result.selected_contract.side is OptionSide.CE

    def test_bearish_maps_pe(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BEARISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_side is OptionSide.PE
        assert result.selected_contract is not None and result.selected_contract.side is OptionSide.PE

    def test_neutral_default_no_selection(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.NEUTRAL)))
        assert result.outcome is SelectionOutcome.NO_ELIGIBLE_CONTRACT
        assert result.selected_contract is None
        assert any("NEUTRAL regime" in r for r in result.rejection_reasons)

    def test_neutral_with_configured_side(self) -> None:
        cfg = _cfg(neutral_side=OptionSide.CE)
        result = ENGINE(cfg).select(_request((_full_chain(),), regime=_regime(Direction.NEUTRAL)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_side is OptionSide.CE

    def test_fixed_side(self) -> None:
        cfg = _cfg(side_source=SideSource.FIXED)
        result = ENGINE(cfg).select(
            _request((_full_chain(),), regime=None, option_side="PE")
        )
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.resolved_side is OptionSide.PE

    def test_malformed_fixed_side(self) -> None:
        cfg = _cfg(side_source=SideSource.FIXED)
        result = ENGINE(cfg).select(_request((_full_chain(),), option_side="XX"))
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("malformed" in r for r in result.rejection_reasons)

    def test_regime_required_by_regime_source(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=None))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA

    def test_regime_not_actionable(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_insufficient_regime()))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("regime" in r for r in result.rejection_reasons)


# --------------------------------------------------------------- liquidity

class TestLiquidity:
    def test_valid_two_sided_book(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.selected_contract is not None
        assert result.selected_contract.bid is not None and result.selected_contract.ask is not None

    def test_crossed_bid_ask(self) -> None:
        crossed = _quote("CE", STRIKES[1], bid="101.00", ask="100.50")
        snap = _snapshot((crossed,))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("crossed bid/ask" in r for r in result.rejection_reasons)

    def test_excessive_spread_points(self) -> None:
        wide = _quote("CE", STRIKES[1], bid="100.00", ask="102.00")
        snap = _snapshot((wide,))
        cfg = _cfg(max_spread_points="1")
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.NO_ELIGIBLE_CONTRACT
        assert any("spread" in r for r in result.rejection_reasons)

    def test_excessive_spread_pct(self) -> None:
        wide = _quote("CE", STRIKES[1], bid="100.00", ask="102.00")
        snap = _snapshot((wide,))
        cfg = _cfg(max_spread_pct="0.5")  # 2 / 101 ~ 1.98% > 0.5%
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.NO_ELIGIBLE_CONTRACT

    def test_missing_bid(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], bid=None),))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("bid not supplied" in r for r in result.rejection_reasons)

    def test_missing_ask(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], ask=None),))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("ask not supplied" in r for r in result.rejection_reasons)

    def test_missing_last_price_allowed_by_default(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], last=None),))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED

    def test_missing_last_price_required(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], last=None),))
        cfg = _cfg(require_last_price=True)
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("last_price not supplied" in r for r in result.rejection_reasons)

    def test_missing_oi_never_zero_with_min(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], oi=None),))
        cfg = _cfg(min_open_interest=100)
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("open_interest not supplied" in r for r in result.rejection_reasons)

    def test_oi_below_min(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], oi=10),))
        cfg = _cfg(min_open_interest=100)
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.NO_ELIGIBLE_CONTRACT
        assert any("open_interest 10" in r for r in result.rejection_reasons)

    def test_missing_volume_never_zero_with_min(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], volume=None),))
        cfg = _cfg(min_volume=50)
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("volume not supplied" in r for r in result.rejection_reasons)

    def test_missing_iv_never_zero_with_min(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], iv=None),))
        cfg = _cfg(min_iv="0.10")
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("iv not supplied" in r for r in result.rejection_reasons)

    def test_missing_iv_allowed_by_default(self) -> None:
        snap = _snapshot((_quote("CE", STRIKES[1], iv=None),))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED

    def test_missing_greeks_allowed_rho_not_required(self) -> None:
        no_greeks = _quote("CE", STRIKES[1], greeks=Greeks())
        snap = _snapshot((no_greeks,))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.selected_contract is not None
        assert result.selected_contract.greeks.rho is None  # Phase 6: rho unavailable is fine

    def test_partial_greeks_ok(self) -> None:
        partial = _quote("CE", STRIKES[1], greeks=Greeks(delta=Decimal("0.5"), vega=Decimal("0.3")))
        snap = _snapshot((partial,))
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.SELECTED


# --------------------------------------------------------------- determinism

class TestDeterminism:
    def test_provider_ordering_changed(self) -> None:
        quotes_fwd = tuple(_full_chain().quotes)
        quotes_rev = tuple(reversed(quotes_fwd))
        fwd = ENGINE().select(_request((_snapshot(quotes_fwd),), regime=_regime(Direction.BULLISH)))
        rev = ENGINE().select(_request((_snapshot(quotes_rev),), regime=_regime(Direction.BULLISH)))
        assert fwd.outcome is SelectionOutcome.SELECTED
        assert fwd.selected_key == rev.selected_key
        assert fwd.candidate_verdicts == rev.candidate_verdicts
        assert fwd.rejection_reasons == rev.rejection_reasons
        assert fwd.resolved_strike == rev.resolved_strike
        assert fwd.to_dict()["evidence"] == rev.to_dict()["evidence"]

    def test_duplicate_candidates_rejected_deterministically(self) -> None:
        dup = _quote("CE", STRIKES[1], source_id="dup")
        snap = _snapshot(
            (
                _quote("CE", STRIKES[0]),
                dup,
                replace(dup, source_id="dup2"),
                _quote("PE", STRIKES[1]),
            )
        )
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("duplicate canonical contract" in r for r in result.rejection_reasons)
        repeated = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.to_dict() == repeated.to_dict()

    def test_identical_input_repeated(self) -> None:
        first = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        second = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert first.to_dict() == second.to_dict()
        assert first.selected_key == second.selected_key

    def test_stable_tiebreak_order(self) -> None:
        target = Decimal("25000")
        far = _quote("CE", 25100)
        far_addr = stable_candidate_key(far, target_strike=target, spread_ratio=Decimal("0.10"), open_interest=100)
        close = _quote("CE", 25000)
        close_key = stable_candidate_key(close, target_strike=target, spread_ratio=Decimal("0.20"), open_interest=100)
        # distance dominates spread
        assert close_key < far_addr
        # tighter spread dominates liquidity
        wide = stable_candidate_key(_quote("CE", 25000), target_strike=target, spread_ratio=Decimal("0.30"), open_interest=900)
        tight = stable_candidate_key(_quote("CE", 25000), target_strike=target, spread_ratio=Decimal("0.10"), open_interest=100)
        assert tight < wide
        # higher OI beats lower OI (both present)
        small_oi = stable_candidate_key(_quote("CE", 25000), target_strike=target, spread_ratio=Decimal("0.10"), open_interest=10)
        big_oi = stable_candidate_key(_quote("CE", 25000), target_strike=target, spread_ratio=Decimal("0.10"), open_interest=99999)
        assert big_oi < small_oi
        # missing OI sorts last
        missing_oi = stable_candidate_key(_quote("CE", 25000, oi=None), target_strike=target, spread_ratio=Decimal("0.10"), open_interest=None)
        assert small_oi < missing_oi

    def test_pool_fingerprint_order_independent(self) -> None:
        a = _full_chain(expiry=EXPIRY)
        b = _full_chain(expiry=EXPIRY2)
        assert pool_fingerprint((a, b)) == pool_fingerprint((b, a))


# ------------------------------------------------------------------ integrity

class TestIntegrity:
    def test_stale_snapshot(self) -> None:
        old = datetime(2026, 9, 24, 9, 27, 30)  # 150 s before DECISION
        snap = _full_chain(ts=old)
        cfg = _cfg(max_snapshot_age_seconds=60)
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("stale snapshot" in r for r in result.rejection_reasons)

    def test_future_snapshot(self) -> None:
        future = datetime(2026, 9, 24, 9, 45, 0)
        result = ENGINE().select(
            _request((_full_chain(ts=future),), regime=_regime(Direction.BULLISH))
        )
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("future" in r for r in result.rejection_reasons)

    def test_tz_aware_decision_timestamp_invalid(self) -> None:
        result = ENGINE().select(
            _request((_full_chain(),), timestamp=datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc))
        )
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("naive" in r for r in result.rejection_reasons)

    def test_underlying_mismatch(self) -> None:
        other = replace(UNDERLYING, symbol="BANKNIFTY", underlying_symbol="BANKNIFTY")
        snap = replace(_full_chain(), underlying=other)
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA
        assert any("underlying mismatch" in r for r in result.rejection_reasons)

    def test_no_look_ahead_uses_snapshot_at_decision(self) -> None:
        # Both the snapshot and every quote are stamped exactly at the decision
        # instant; nothing from after t can enter the result.
        snap = _full_chain(ts=DECISION)
        for q in snap.quotes:
            assert q.timestamp == DECISION
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH), timestamp=DECISION))
        assert result.outcome is SelectionOutcome.SELECTED
        assert result.selected_contract is not None
        assert result.selected_contract.timestamp == DECISION

    def test_regime_from_future_rejected(self) -> None:
        bullish = _regime(Direction.BULLISH)
        future_regime = replace(bullish, timestamp=DECISION + timedelta(minutes=5))
        result = ENGINE().select(_request((_full_chain(),), regime=future_regime))
        assert result.outcome is SelectionOutcome.INVALID_DATA
        assert any("future" in r for r in result.rejection_reasons)

    def test_empty_chain_snapshot(self) -> None:
        snap = _snapshot(())
        result = ENGINE().select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is SelectionOutcome.UNAVAILABLE_DATA

    def test_audit_fields_present(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        assert len(result.chain_fingerprints) == 1
        assert result.chain_fingerprints[0] == chain_fingerprint(_full_chain())
        assert result.engine_version and result.schema_version and result.rules_version
        assert len(result.candidate_verdicts) == 3  # the 3 CE strikes
        assert any(e.dimension == "tiebreak" for e in result.evidence)


# ------------------------------------------------------------------- safety

SAFETY_MODULES = (
    "src/fno_ai_paper_trading/research/selection/selector.py",
    "src/fno_ai_paper_trading/research/selection/models.py",
    "src/fno_ai_paper_trading/research/selection/__init__.py",
)


class TestSafety:
    def test_no_execution_or_vendor_imports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        reuse_files = {
            (root / "src/fno_ai_paper_trading/research/selection/selector.py").resolve(),
            (root / "src/fno_ai_paper_trading/research/selection/models.py").resolve(),
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
                assert any(name.startswith("fno_ai_paper_trading.research.regime") for name in imported)

    def test_result_has_no_order_signal(self) -> None:
        result = ENGINE().select(_request((_full_chain(),), regime=_regime(Direction.BULLISH)))
        data = result.to_dict()
        assert "selected_contract" in data
        for word in ("BUY", "SELL", "order", "signal", "recommendation"):
            assert word not in {k.upper() for k in data}
        assert result.selected_contract is not None
        # The resolved side is an eligibility label, never a trade side.
        assert result.resolved_side in (OptionSide.CE, OptionSide.PE)

    def test_all_verdicts_have_explicit_reasons(self) -> None:
        snap = _snapshot(
            (
                _quote("CE", STRIKES[0], bid="90.00", ask="92.00"),
                _quote("CE", STRIKES[1], oi=5),
                _quote("CE", STRIKES[2], bid=None),
            )
        )
        cfg = _cfg(min_open_interest=100)
        result = ENGINE(cfg).select(_request((snap,), regime=_regime(Direction.BULLISH)))
        assert result.outcome is not SelectionOutcome.SELECTED
        assert len(result.rejection_reasons) >= 2
        for verdict in result.candidate_verdicts:
            assert verdict.reasons or verdict.eligible