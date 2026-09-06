"""In-memory market-data provider with deterministic sample data.

Used by the demo and the test suite. It never connects to any network source
and is intentionally the only provider shipped in Phase 1.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.utils.functions import non_negative_int


def build_sample_instruments() -> list[Instrument]:
    """Deterministic sample instruments: a future plus one CE and one PE."""
    expiry = date(2026, 12, 24)
    return [
        Instrument(
            symbol="NIFTY1",
            instrument_type=InstrumentType.FUTURE,
            underlying_symbol="NIFTY",
            lot_size=75,
            multiplier=1,
        ),
        Instrument(
            symbol="NIFTY1_24500_CE",
            instrument_type=InstrumentType.OPTION_CE,
            underlying_symbol="NIFTY",
            expiry=expiry,
            strike=Decimal("24500"),
            option_type="CE",
            lot_size=75,
            multiplier=1,
        ),
        Instrument(
            symbol="NIFTY1_24500_PE",
            instrument_type=InstrumentType.OPTION_PE,
            underlying_symbol="NIFTY",
            expiry=expiry,
            strike=Decimal("24500"),
            option_type="PE",
            lot_size=75,
            multiplier=1,
        ),
    ]


def build_sample_ohlcv(instrument: Instrument, bars: int = 5) -> list[MarketPrice]:
    """Deterministic OHLCV bars whose close rises by 10 each bar."""
    non_negative_int(bars, "bars")
    base_time = datetime(2026, 9, 1, 9, 15)
    prices: list[MarketPrice] = []
    start = Decimal("24000") if instrument.is_option() else Decimal("24200")
    for i in range(bars):
        close = start + Decimal(i * 10)
        ts = base_time + timedelta(minutes=15 * i)
        prices.append(
            MarketPrice(
                instrument=instrument,
                timestamp=ts,
                open=close - Decimal("15"),
                high=close + Decimal("20"),
                low=close - Decimal("25"),
                close=close,
                volume=1000 + i * 100,
            )
        )
    return prices


class InMemoryMarketDataProvider(MarketDataProvider):
    """Provider backed by a pre-built in-memory bar store.

    Constructed with ``instruments`` and an optional mapping of
    ``symbol -> list[MarketPrice]``. If a mapping is omitted, sample data is
    generated deterministically.
    """

    def __init__(
        self,
        instruments: list[Instrument] | None = None,
        history: dict[str, list[MarketPrice]] | None = None,
    ) -> None:
        self._instruments = instruments if instruments is not None else build_sample_instruments()
        self._history: dict[str, list[MarketPrice]] = {}
        for instrument in self._instruments:
            bars = history.get(instrument.symbol) if history else None
            bars = bars if bars else build_sample_ohlcv(instrument)
            self._history[instrument.symbol] = list(bars)

    def get_instruments(self) -> list[Instrument]:
        return list(self._instruments)

    def get_instrument(self, symbol: str) -> Instrument | None:
        for instrument in self._instruments:
            if instrument.symbol == symbol:
                return instrument
        return None

    def get_market_price(self, instrument: Instrument) -> MarketPrice:
        bars = self._history.get(instrument.symbol)
        if not bars:
            raise KeyError(f"no market data for instrument '{instrument.symbol}'")
        return bars[-1]

    def get_last_price(self, instrument: Instrument) -> Decimal:
        return self.get_market_price(instrument).close

    def get_ohlcv(self, instrument: Instrument, limit: int | None = None) -> list[MarketPrice]:
        bars = self._history.get(instrument.symbol)
        if not bars:
            raise KeyError(f"no market data for instrument '{instrument.symbol}'")
        if limit is None:
            return list(bars)
        return list(bars[-non_negative_int(limit, "limit") :])