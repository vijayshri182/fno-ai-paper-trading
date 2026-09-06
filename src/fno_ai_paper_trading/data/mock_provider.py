"""In-memory market-data provider with deterministic sample data.

Used by the demo and the test suite. It never connects to any network source
and is intentionally the only provider a demo run relies on.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.data.provider import MarketDataProvider
from fno_ai_paper_trading.models.enums import InstrumentType, MarketPhase
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice, MarketQuote, MarketSession
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


def build_crossing_ohlcv(
    instrument: Instrument,
    *,
    flat_bars: int = 25,
    up_bars: int = 15,
    hold_bars: int = 5,
    down_bars: int = 10,
) -> list[MarketPrice]:
    """Deterministic daily series that forces a genuine MA fast/slow crossover.

    The series is flat, rises, holds, then falls — so a fast moving average
    climbs above the slow one and later drops below it again. This gives any
    crossover strategy a reproducible BUY signal followed by a SELL signal.
    """
    for name, value in (
        ("flat_bars", flat_bars),
        ("up_bars", up_bars),
        ("hold_bars", hold_bars),
        ("down_bars", down_bars),
    ):
        non_negative_int(value, name)

    base_time = datetime(2026, 9, 1, 9, 15)
    start = Decimal("24200") if not instrument.is_option() else Decimal("24000")

    closes: list[Decimal] = [start] * flat_bars
    closes += [start + Decimal(i * 25) for i in range(1, up_bars + 1)]
    top = closes[-1]
    closes += [top] * hold_bars
    closes += [top - Decimal(i * 25) for i in range(1, down_bars + 1)]

    bars: list[MarketPrice] = []
    for i, close in enumerate(closes):
        bars.append(
            MarketPrice(
                instrument=instrument,
                timestamp=base_time + timedelta(days=i),
                open=close - Decimal("10"),
                high=close + Decimal("20"),
                low=close - Decimal("15"),
                close=close,
                volume=1000 + i * 10,
            )
        )
    return bars


def _session_template() -> MarketSession:
    """A deterministic 'open' session snapshot for the in-memory market."""
    observed = datetime.now()
    return MarketSession(
        is_open=True,
        phase=MarketPhase.OPEN,
        observed_at=observed,
        open_time=datetime(observed.year, 9, 15),
        close_time=datetime(observed.year, 9, 15, 15, 30),
        exchange="NSE",
        label="InMemory sample market",
    )


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

    def get_quote(self, instrument: Instrument) -> MarketQuote:
        bar = self.get_market_price(instrument)
        return MarketQuote(
            instrument=instrument,
            timestamp=bar.timestamp,
            last_price=bar.close,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            previous_close=bar.close - Decimal("10"),
            volume=bar.volume,
            open_interest=bar.open_interest,
            bid=bar.mid - Decimal("0.5"),
            ask=bar.mid + Decimal("0.5"),
            change=bar.close - (bar.close - Decimal("10")),
            change_percent=Decimal("0.0413"),
        )

    def get_market_session(self) -> MarketSession:
        return _session_template()

    def get_ohlcv(self, instrument: Instrument, limit: int | None = None) -> list[MarketPrice]:
        bars = self._history.get(instrument.symbol)
        if not bars:
            raise KeyError(f"no market data for instrument '{instrument.symbol}'")
        if limit is None:
            return list(bars)
        return list(bars[-non_negative_int(limit, "limit") :])

    def get_historical_ohlcv(
        self,
        instrument: Instrument,
        interval: str = "day",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketPrice]:
        bars = self._history.get(instrument.symbol)
        if not bars:
            raise KeyError(f"no market data for instrument '{instrument.symbol}'")
        if start is None and end is None:
            return list(bars)
        result = [
            bar
            for bar in bars
            if (start is None or bar.timestamp >= start) and (end is None or bar.timestamp <= end)
        ]
        return result