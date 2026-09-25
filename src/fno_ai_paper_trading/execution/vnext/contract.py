"""Contract resolution abstraction for the vNext layer.

The machine depends on this abstraction, never on Upstox. In production a real
resolver would be injected; the deterministic ``TableContractResolver`` is used
by the isolated test-suite of this workstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from fno_ai_paper_trading.execution.vnext.enums import ContractType, OptionLeg
from fno_ai_paper_trading.execution.vnext.errors import (
    ContractResolutionError,
    InvalidOrderSemanticsError,
)


@dataclass(frozen=True)
class MarketContext:
    """Opaque market context handed to a resolver (expiry/strike free).

    No expiry, strike or contract is hardcoded here — production logic never
    hardcodes a trading day. The resolver decides from context.
    """

    underlying: str = "NIFTY"
    as_of: date | None = None


@dataclass(frozen=True)
class OptionContract:
    """A fully-resolved option contract (leg + CE/PE + instrument identity)."""

    option_leg: OptionLeg
    contract_type: ContractType
    instrument_key: str
    expiry: date
    strike: Decimal
    lot_size: int
    tick_size: Decimal

    def __post_init__(self) -> None:
        if self.option_leg is OptionLeg.NONE:
            raise InvalidOrderSemanticsError("NONE leg cannot carry a contract")
        if self.contract_type is ContractType.CE and self.option_leg is OptionLeg.PUT:
            raise InvalidOrderSemanticsError("PUT must resolve to PE, got CE")
        if self.contract_type is ContractType.PE and self.option_leg is OptionLeg.CALL:
            raise InvalidOrderSemanticsError("CALL must resolve to CE, got PE")
        if not self.instrument_key or not self.instrument_key.strip():
            raise ContractResolutionError("instrument_key is required")
        if self.lot_size <= 0:
            raise ContractResolutionError("lot_size must be positive")
        if self.tick_size <= 0:
            raise ContractResolutionError("tick_size must be positive")


@runtime_checkable
class ContractResolver(Protocol):
    """Resolve an option leg into a concrete, validated contract."""

    def resolve(
        self, option_leg: OptionLeg, market_context: MarketContext
    ) -> OptionContract:
        ...


@dataclass(frozen=True)
class OptionLegResolution:
    """Resolver companion — returns a contract plus its idempotency identity.

    Contains everything required to verify the resolved instrument: option_leg,
    CE/PE, instrument_key, expiry, strike, lot_size, tick_size.
    """

    contract: OptionContract
    resolution_key: str = ""


class TableContractResolver:
    """Deterministic in-memory resolver backed by a user-supplied table.

    Used to keep the vNext suite deterministic (no network, no Upstox). A real
    production resolver would implement :class:`ContractResolver` instead.
    """

    def __init__(
        self,
        contracts: dict[OptionLeg, OptionContract],
        market_context: MarketContext = MarketContext(),
    ) -> None:
        self._contracts = dict(contracts)
        self._context = market_context

    def resolve(
        self, option_leg: OptionLeg, market_context: MarketContext | None = None
    ) -> OptionContract:
        if option_leg is OptionLeg.NONE:
            raise ContractResolutionError("NONE leg has no contract to resolve")
        contract = self._contracts.get(option_leg)
        if contract is None:
            raise ContractResolutionError(
                f"no contract available for {option_leg.value}"
            )
        return contract