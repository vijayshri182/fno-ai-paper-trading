"""Adversarial bar validation for the Daily Paper Trading Track.

The engine is fail-closed: before a single bar can influence a signal or an
order, the whole incoming batch is screened. Individual bad bars are *poisoned*
(carried to the ledger as data-skips and never consumed); structural breakage
of the usable stream (non-chronological, duplicated timestamps) is a hard error
that stops the tick. Nothing is ever tradeable that has not passed this screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from fno_ai_paper_trading.data.intervals import interval_minutes
from fno_ai_paper_trading.data.market_hours import NSE_TZ, is_trading_day
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.errors import TrackValidationError
from fno_ai_paper_trading.paper_track.policy import SessionPolicy

__all__ = ["ValidatedBars", "BarValidator"]


@dataclass(frozen=True)
class ValidatedBars:
    """Result of screening one provider batch.

    ``valid``       usable bars, strictly chronological, each completed (same day);
    ``prior``       bars whose date is before ``now`` (legitimate older history —
                    never tradeable today, but not a data-quality error);
    ``poisoned``    bars excluded today with the reasons that excluded them;
    ``anomalies``   soft observations (gaps) that do not halt processing;
    ``ok``          False only for a *structural* failure (``error`` set).
    """

    ok: bool
    error: str | None = None
    valid: tuple[MarketPrice, ...] = ()
    prior: tuple[MarketPrice, ...] = ()
    poisoned: tuple[MarketPrice, ...] = ()
    poison_reasons: tuple[str, ...] = ()
    anomalies: tuple[str, ...] = ()

    @property
    def poison_count(self) -> int:
        return len(self.poisoned)


class BarValidator:
    """Screens incoming bars for the track's operating window and integrity."""

    def __init__(
        self,
        interval: str = "5m",
        policy: SessionPolicy | None = None,
        expected_symbol: str | None = None,
    ) -> None:
        minutes = interval_minutes(interval)
        if minutes is None:
            raise TrackValidationError(
                f"track interval {interval!r} must have a fixed minute length"
            )
        self.interval = interval
        self._bar_len = timedelta(minutes=minutes)
        self.policy = policy if policy is not None else SessionPolicy()
        self.expected_symbol = expected_symbol

    # -- helpers ---------------------------------------------------------

    def _session_window_ok(self, ts: datetime) -> bool:
        if not is_trading_day(ts, tz=NSE_TZ):
            return False
        return self.policy.window_open <= ts.time() < self.policy.window_close

    def _price_ok(self, value) -> tuple[bool, str]:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return False, "non-numeric price"
        if not dec.is_finite() or dec <= 0:
            return False, "non-positive or non-finite price"
        return True, ""

    # -- screen ----------------------------------------------------------

    def validate(self, bars, *, now: datetime, last_processed: datetime | None = None) -> ValidatedBars:
        """Screen ``bars`` (completed chronological list) for ``now``."""
        if not isinstance(bars, list):
            raise TrackValidationError(
                f"bars must be a list of MarketPrice, got {type(bars).__name__}"
            )

        anomalies: list[str] = []
        valid: list[MarketPrice] = []
        prior: list[MarketPrice] = []
        poisoned: list[MarketPrice] = []
        reasons: list[str] = []

        for bar in bars:
            outcome = self._classify(bar, now=now)
            if outcome == "prior":
                prior.append(bar)
            elif outcome == "poison":
                reason = self._bar_reason(bar, now=now)
                poisoned.append(bar)
                reasons.append(reason or "unknown reason")
            else:
                valid.append(bar)

        # Structural safety of the usable stream.
        if not valid:
            return ValidatedBars(
                ok=True,
                valid=(),
                prior=tuple(prior),
                poisoned=tuple(poisoned),
                poison_reasons=tuple(reasons),
                anomalies=tuple(anomalies),
            )

        timestamps = [b.timestamp for b in valid]
        if len(set(timestamps)) != len(timestamps):
            raise TrackValidationError("duplicate timestamps in the valid bar stream")
        if any(prev >= nxt for prev, nxt in zip(timestamps, timestamps[1:])):
            raise TrackValidationError("non-chronological valid bar stream")

        # Gap observation (anomaly, not an error): a missing intraday bar.
        for prev, nxt in zip(timestamps, timestamps[1:]):
            if nxt.date() == prev.date() and (nxt - prev) > self._bar_len:
                anomalies.append(
                    f"gap between {prev:%H:%M} and {nxt:%H:%M} on {prev.date()} "
                    f"(expected {self._bar_len} per bar)"
                )

        # Nothing stale should reach this point; documented for forensics.
        if last_processed is not None:
            stale = [b for b in valid if b.timestamp <= last_processed]
            if stale:
                anomalies.append(f"{len(stale)} bar(s) repeated before {last_processed:%Y-%m-%d %H:%M}")

        return ValidatedBars(
            ok=True,
            valid=tuple(valid),
            prior=tuple(prior),
            poisoned=tuple(poisoned),
            poison_reasons=tuple(reasons),
            anomalies=tuple(anomalies),
        )

    def _classify(self, bar, *, now: datetime) -> str:
        """Classify a batch element: ``valid``, ``prior`` or ``poison``."""
        if not isinstance(bar, MarketPrice) or not isinstance(bar.timestamp, datetime):
            return "poison"
        if bar.timestamp.date() != now.date():
            return "prior" if bar.timestamp < now else "poison"
        return "poison" if self._bar_reason(bar, now=now) is not None else "valid"

    def _bar_reason(self, bar, *, now: datetime) -> str | None:
        if not isinstance(bar, MarketPrice):
            return "not a MarketPrice"
        ts = bar.timestamp
        if not isinstance(ts, datetime):
            return "timestamp is not a datetime"
        if ts.date() != now.date():
            return f"bar dated {ts.date()} != today {now.date()}"
        if not self._session_window_ok(ts):
            return f"bar timestamp {ts:%Y-%m-%d %H:%M} outside session window"
        if ts + self._bar_len > now:
            return f"bar {ts:%H:%M} not completed yet (opens at {ts:%H:%M}, completes {ts + self._bar_len:%H:%M})"
        if ts > now:
            return "bar timestamp is in the future"
        if self.expected_symbol is not None and bar.instrument.symbol != self.expected_symbol:
            return f"unexpected instrument {bar.instrument.symbol!r}"

        for name in ("open", "high", "low", "close"):
            ok, msg = self._price_ok(getattr(bar, name))
            if not ok:
                return f"{name} {msg}"
        if bar.high < max(bar.open, bar.close):
            return "high below open/close"
        if bar.low > min(bar.open, bar.close):
            return "low above open/close"
        if bar.high < bar.low:
            return "high below low"
        if bar.volume < 0:
            return "negative volume"
        return None