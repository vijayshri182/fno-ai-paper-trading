"""Options research domain models (Phase 5).

Provider-neutral, deterministic, replay-safe building blocks for the options
research pipeline: **NIFTY spot -> market regime -> option-chain snapshot ->
data-quality validation -> contract selection -> trade quality -> risk**.

Design rules inherited from the codebase:

* naive IST timestamps everywhere;
* ``Decimal`` prices and canonical ``str(Decimal)`` forms;
* frozen dataclasses;
* *absence* means explicitly unavailable — the no-fabrication rule: a field the
  provider did not supply stays ``None`` and is never invented, never
  substituted with the underlying spot, and never copied from another quote.

Nothing here places orders, touches credentials, or imports execution/paper
layers. The module is read-only research tooling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.utils.functions import to_decimal

_OPTION_SIDES = frozenset({"CE", "PE"})


class OptionSide(str, Enum):
    """Call/Put leg identifier (provider-neutral)."""

    CE = "CE"
    PE = "PE"

    def to_instrument_type(self) -> InstrumentType:
        """Map the leg to the shared domain instrument type."""
        if self is OptionSide.CE:
            return InstrumentType.OPTION_CE
        return InstrumentType.OPTION_PE

    @classmethod
    def from_instrument_type(cls, value) -> "OptionSide | None":
        """Inverse of :meth:`to_instrument_type`; ``None`` for non-options."""
        if value is InstrumentType.OPTION_CE:
            return OptionSide.CE
        if value is InstrumentType.OPTION_PE:
            return OptionSide.PE
        return None

    @classmethod
    def parse(cls, value) -> "OptionSide | None":
        """Parse a raw value (``"CE"`` / ``"ce"`` / ``"PE"`` ...). ``None`` if absent/unknown."""
        if value is None:
            return None
        text = str(value).strip().upper()
        if text in _OPTION_SIDES:
            return cls(text)
        return None


@dataclass(frozen=True)
class Greeks:
    """Option greeks. Each field is ``None`` when the provider did not supply it.

    Sign conventions used by the quality layer: ``gamma``/``vega`` must be
    non-negative when present; ``delta``/``theta``/``rho`` may be negative.
    """

    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("delta", "gamma", "theta", "vega", "rho"):
            value = getattr(self, name)
            if value is None:
                continue
            object.__setattr__(self, name, to_decimal(value))

    @property
    def available(self) -> tuple[str, ...]:
        """Names of the greeks the provider actually supplied (ordered)."""
        return tuple(name for name in ("delta", "gamma", "theta", "vega", "rho") if getattr(self, name) is not None)

    @property
    def all_available(self) -> bool:
        return len(self.available) == 5


def _non_negative_side(option_type: str) -> str:
    text = (option_type or "").strip().upper()
    if text not in _OPTION_SIDES:
        raise ValueError("option_type must be 'CE' or 'PE'")
    return text


def _int_or_none(value, name: str) -> int | None:
    """Coerce an integer field; ``None`` stays ``None``; non-integer raises."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
    raise ValueError(f"{name} must be an integer")


def _decimal_or_none(value, name: str) -> Decimal | None:
    """Coerce an optional numeric field; ``None``/empty stays ``None``; non-numeric raises."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return to_decimal(value)


@dataclass(frozen=True)
class OptionQuote:
    """A single point-in-time option quote (one contract, one timestamp).

    ``bid``/``ask``/``last_price``/``open_interest``/``oi_change``/``volume``/
    ``iv``/``greeks`` are ``None`` when the provider did not supply them —
    that is *explicitly unavailable*, never fabricated and never substituted
    with the underlying spot.

    Identity comes from the shared :class:`Instrument` (expiry+strike+CE/PE).
    ``source`` is the provider identifier; ``source_id`` preserves the
    provider's contract key (e.g. ``NSE_FO|NIFTY 24400 CE 26 SEP 2026``);
    ``source_timestamp`` preserves the provider's received-at instant (naive
    IST).
    """

    instrument: Instrument
    timestamp: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last_price: Decimal | None = None
    open_interest: int | None = None
    oi_change: int | None = None
    volume: int | None = None
    iv: Decimal | None = None
    greeks: Greeks = field(default_factory=Greeks)
    source: str = "UNKNOWN"
    source_timestamp: datetime | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if not self.instrument.is_option():
            raise ValueError("an option quote requires an option instrument (CE/PE)")
        if self.instrument.option_type not in _OPTION_SIDES:
            raise ValueError("instrument.option_type must be 'CE' or 'PE'")

        object.__setattr__(self, "bid", _decimal_or_none(self.bid, "bid"))
        object.__setattr__(self, "ask", _decimal_or_none(self.ask, "ask"))
        object.__setattr__(self, "last_price", _decimal_or_none(self.last_price, "last_price"))
        object.__setattr__(self, "iv", _decimal_or_none(self.iv, "iv"))
        object.__setattr__(self, "open_interest", _int_or_none(self.open_interest, "open_interest"))
        object.__setattr__(self, "oi_change", _int_or_none(self.oi_change, "oi_change"))
        object.__setattr__(self, "volume", _int_or_none(self.volume, "volume"))
        object.__setattr__(self, "source", (self.source or "").strip() or "UNKNOWN")
        if isinstance(self.greeks, Greeks):
            return
        raise ValueError("greeks must be a Greeks instance")

    # ------------------------------------------------------------------ view

    @property
    def side(self) -> OptionSide:
        side = OptionSide.parse(self.instrument.option_type)
        if side is None:  # guarded in __post_init__
            raise ValueError("invalid option_type")
        return side

    @property
    def expiry(self) -> date:
        return self.instrument.expiry  # type: ignore[return-value]  # guaranteed for options

    @property
    def strike(self) -> Decimal:
        return self.instrument.strike  # type: ignore[return-value]  # guaranteed for options

    @property
    def key(self) -> str:
        """Canonical internal contract key: ``UNDERLYING|EXPIRY|STRIKE|SIDE``."""
        return f"{self.instrument.underlying_symbol}|{self.expiry.isoformat()}|{self.strike:g}|{self.side.value}"

    @property
    def mid(self) -> Decimal | None:
        """Mid of a clean two-sided book; ``None`` when either side is missing."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def has_price(self) -> bool:
        """True when at least one of bid/ask/last is available."""
        return self.bid is not None or self.ask is not None or self.last_price is not None


@dataclass(frozen=True)
class OptionChainSnapshot:
    """One option-chain snapshot for one underlying + expiry at one instant.

    ``quotes`` is an immutable tuple. Rows with identical canonical keys are
    **preserved** (never silently dropped) and the quality layer flags them.
    ``spot_price`` is the underlying's point-in-time price (``None`` = not
    supplied — never invented).
    """

    underlying: Instrument
    expiry: date
    timestamp: datetime
    quotes: tuple[OptionQuote, ...]
    spot_price: Decimal | None = None
    source: str = "UNKNOWN"
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.underlying.is_option():
            raise ValueError("the chain underlying must be a non-option instrument (index/futures)")
        object.__setattr__(self, "quotes", tuple(self.quotes))
        object.__setattr__(self, "spot_price", _decimal_or_none(self.spot_price, "spot_price"))
        object.__setattr__(self, "source", (self.source or "").strip() or "UNKNOWN")

    # ------------------------------------------------------------------ view

    def call_quotes(self) -> tuple[OptionQuote, ...]:
        return tuple(q for q in self.quotes if q.side is OptionSide.CE)

    def put_quotes(self) -> tuple[OptionQuote, ...]:
        return tuple(q for q in self.quotes if q.side is OptionSide.PE)

    def contract(self, side: OptionSide, strike: float | int | str | Decimal) -> OptionQuote | None:
        """Return the first quote matching ``side`` + ``strike``, else ``None``."""
        wanted = _decimal_or_none(strike, "strike")
        for quote in self.quotes:
            if quote.side is side and quote.strike == wanted:
                return quote
        return None

    def strike_values(self) -> tuple[Decimal, ...]:
        return tuple(sorted({q.strike for q in self.quotes}))

    def duplicate_keys(self) -> tuple[str, ...]:
        """Canonical keys that appear more than once (sorted, deduplicated)."""
        counts: dict[str, int] = {}
        for quote in self.quotes:
            counts[quote.key] = counts.get(quote.key, 0) + 1
        return tuple(sorted(key for key, count in counts.items() if count > 1))

    def contracts(self) -> tuple[tuple[str, OptionSide, Decimal], ...]:
        """Deterministic (key, side, strike) listing of every quote."""
        return tuple(
            (quote.key, quote.side, quote.strike) for quote in sorted(self.quotes, key=lambda q: (q.expiry, q.strike, q.side.value))
        )

    def without_spot(self) -> "OptionChainSnapshot":
        """Return a copy with ``spot_price`` forced to ``None`` (never substituted)."""
        return OptionChainSnapshot(
            underlying=self.underlying,
            expiry=self.expiry,
            timestamp=self.timestamp,
            quotes=self.quotes,
            spot_price=None,
            source=self.source,
            source_timestamp=self.source_timestamp,
        )


__all__ = [
    "Greeks",
    "OptionChainSnapshot",
    "OptionQuote",
    "OptionSide",
]