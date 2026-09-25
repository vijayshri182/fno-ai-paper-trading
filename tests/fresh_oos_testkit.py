"""Shared fake client and bar factories for the fresh-OOS test suite.

All tests are deterministic and network-free: bars are synthesised from a
full-session grid, and :class:`FakeHistoricalDataClient` lets each test stage
responses, failures and hash-drift per trading day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.data.market_hours import is_trading_day
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider
from fno_ai_paper_trading.fresh_oos.errors import (
    AuthenticationError,
    RateLimitError,
    UnavailableError,
)
from fno_ai_paper_trading.fresh_oos.protocol import (
    EXPECTED_BARS_PER_FULL_DAY,
    SESSION_FIRST_TIME,
)
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice

BOUNDARY = date(2026, 9, 11)


def make_instrument(name: str = "NIFTY 50") -> Instrument:
    return get_research_instrument(name)


def session_times(day: date, count: int = EXPECTED_BARS_PER_FULL_DAY) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, SESSION_FIRST_TIME.hour, SESSION_FIRST_TIME.minute)
    return [start + timedelta(minutes=5 * i) for i in range(count)]


def generate_5m_bars(day: date, count: int = EXPECTED_BARS_PER_FULL_DAY) -> list[MarketPrice]:
    """Deterministic full-session bars for ``day`` (identical on every call)."""
    instrument = make_instrument()
    seed = (day.toordinal() % 40) * 10
    bars: list[MarketPrice] = []
    for i, ts in enumerate(session_times(day, count)):
        open_ = Decimal(seed) + Decimal(1000) + Decimal(i)
        close = open_ + Decimal("0.25")
        high = max(open_, close) + Decimal("1.0")
        low = min(open_, close) - Decimal("1.0")
        bars.append(
            MarketPrice(
                instrument=instrument,
                timestamp=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000 + i,
                open_interest=0,
            )
        )
    return bars


def mutate_bars(bars: list[MarketPrice]) -> list[MarketPrice]:
    """Return a semantics-preserving variant of ``bars`` (hash will differ)."""
    from dataclasses import replace

    bar = bars[0]
    new_close = bar.close + Decimal("1.5")
    new_high = bar.high if bar.high >= new_close else new_close + Decimal("1.0")
    return [replace(bar, close=new_close, high=new_high)] + bars[1:]


class FakeHistoricalDataClient:
    """Configurable stand-in for the narrow :class:`HistoricalDataClient`."""

    def __init__(self, default_factory=generate_5m_bars) -> None:
        self.default_factory = default_factory
        self.responses: dict[date, list[MarketPrice] | Exception] = {}
        self.failures: dict[date, Exception] = {}
        self.altered: set[date] = set()  # serve mutated bars after the first fetch
        self.calls: list[date] = []
        self.instrument_seen: list[Instrument] = []

    def fetch_5m_day(self, instrument: Instrument, day: date) -> list[MarketPrice]:
        self.calls.append(day)
        self.instrument_seen.append(instrument)
        if day in self.failures:
            raise self.failures[day]
        if day in self.altered and self.calls.count(day) > 1:
            return mutate_bars(self.responses.get(day) or self.default_factory(day))
        if day in self.responses:
            value = self.responses[day]
            if isinstance(value, Exception):
                raise value
            return list(value)
        bars = self.default_factory(day)
        self.responses[day] = list(bars)
        return list(bars)


def trading_days_from(boundary: date, through: date) -> list[date]:
    """Trading days in ``(boundary, through]`` (chronological, calendar-aware)."""
    result: list[date] = []
    day = boundary + timedelta(days=1)
    while day <= through:
        if is_trading_day(datetime(day.year, day.month, day.day, 12, 0)):
            result.append(day)
        day += timedelta(days=1)
    return result


def days_to_today(boundary: date, now: datetime) -> list[date]:
    """Trading days the collector would see for a given ``now``."""
    return trading_days_from(boundary, now.date() - timedelta(days=1))


class FakeProvider(UpstoxHistoricalDataProvider):
    """A network-free provider injected into the real client for isolation tests."""

    def _get(self, path: str):  # pragma: no cover - never invoked in tests
        raise AssertionError("FakeProvider._get must never be called")


COMMON_EXCEPTIONS = {
    "auth": AuthenticationError("Upstox rejected the access token (HTTP 401)"),
    "rate": RateLimitError("Upstox rate limit hit (429) and retries exhausted"),
    "unavailable": UnavailableError("Upstox service unavailable after retries"),
}


def make_context(
    tmp_path,
    *,
    now=None,
    client=None,
    boundary=BOUNDARY,
    expected_bars=EXPECTED_BARS_PER_FULL_DAY,
    verify_present=True,
    max_days=0,
    datasets_dir=None,
):
    """Build a wired collector in a temp dir with a deterministic clock."""
    from pathlib import Path

    from fno_ai_paper_trading.fresh_oos.collector import FreshOosCollector, FreshOosCollectorConfig
    from fno_ai_paper_trading.fresh_oos.lock import CollectorLock
    from fno_ai_paper_trading.fresh_oos.manifest import FreshOosManifest
    from fno_ai_paper_trading.fresh_oos.store import FreshOosStore

    now = now or datetime(2026, 10, 16, 12, 0)
    root = Path(tmp_path) / "root"
    datasets = Path(datasets_dir) if datasets_dir else Path(tmp_path) / "datasets"
    datasets.mkdir(parents=True, exist_ok=True)
    config = FreshOosCollectorConfig(
        root=root,
        datasets_dir=datasets,
        boundary=boundary,
        expected_bars_per_day=expected_bars,
        verify_present=verify_present,
        max_days_per_run=max_days,
        now_fn=lambda: now,
    )
    store = FreshOosStore(root, namespace=config.namespace)
    manifest = FreshOosManifest(path=root / "fresh_oos_manifest.json")
    collector = FreshOosCollector(
        config,
        client or FakeHistoricalDataClient(),
        store=store,
        manifest=manifest,
        lock=CollectorLock(root, now_fn=lambda: now),
    )
    return {
        "collector": collector,
        "config": config,
        "store": store,
        "manifest": manifest,
        "root": root,
        "datasets": datasets,
        "client": collector.client,
    }