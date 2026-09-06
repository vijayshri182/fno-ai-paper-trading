"""Instrument model describing a tradable F&O instrument."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.utils.functions import non_negative_decimal, positive_decimal, positive_int


@dataclass(frozen=True)
class Instrument:
    """Metadata about a single tradable instrument.

    For a future, ``strike`` and ``option_type`` are ``None``. For an option,
    ``underlying_symbol``, ``expiry``, ``strike`` and ``option_type`` describe the contract.
    """

    symbol: str
    instrument_type: InstrumentType
    underlying_symbol: str
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: str | None = None  # "CE" or "PE"
    exchange: str = "NSE"
    exchange_token: str | None = None  # vendor/exchange token (e.g. Kite instrument token)
    lot_size: int = 1
    tick_size: Decimal = Decimal("0.05")
    multiplier: int = 1

    def __post_init__(self) -> None:
        symbol = self.symbol.strip()
        underlying = self.underlying_symbol.strip()
        exchange = self.exchange.strip().upper()
        if not symbol:
            raise ValueError("instrument symbol must not be empty")
        if not underlying:
            raise ValueError("underlying_symbol must not be empty")
        if not exchange:
            raise ValueError("exchange must not be empty")

        exchange_token = self.exchange_token.strip() if self.exchange_token else None

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "underlying_symbol", underlying)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "exchange_token", exchange_token)
        object.__setattr__(self, "lot_size", positive_int(self.lot_size, "lot_size"))
        object.__setattr__(self, "multiplier", positive_int(self.multiplier, "multiplier"))
        object.__setattr__(self, "tick_size", positive_decimal(self.tick_size, "tick_size"))

        if self.is_option():
            if self.expiry is None:
                raise ValueError("option instruments require an expiry")
            if self.strike is None:
                raise ValueError("option instruments require a strike")
            if self.option_type not in ("CE", "PE"):
                raise ValueError("option_type must be 'CE' or 'PE'")
            object.__setattr__(self, "strike", positive_decimal(self.strike, "strike"))
        else:
            if self.strike is not None:
                object.__setattr__(self, "strike", non_negative_decimal(self.strike, "strike"))

    def is_option(self) -> bool:
        return self.instrument_type in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE)

    def display_name(self) -> str:
        """Human readable name, e.g. ``NIFTY 2026-12-24 24500 CE``."""
        if not self.is_option():
            return self.symbol
        return f"{self.underlying_symbol} {self.expiry} {self.strike:g} {self.option_type}"