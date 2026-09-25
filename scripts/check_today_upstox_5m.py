"""READ-ONLY monitor: is TODAY's NIFTY 50 5-minute Upstox candle feed ready?

Checks whether the Upstox V3 feed has published today's NIFTY 50 5m candles in
the NSE derivative session window (09:15-15:30 IST), and whether the latest
available candle still satisfies the existing production Watchdog freshness
threshold.

Candle-source routing (deterministic, no fallback):

* CURRENT TRADING DAY  -> Intraday Candle V3
  ``/v3/historical-candle/intraday/{key}/minutes/5`` (the dedicated current-day
  endpoint; the dated Historical endpoint returns zero candles for today).
* COMPLETED DAYS       -> Historical Candle V3 (unchanged).

It performs GET-only market-data reads: it reuses the exact production fetch
implementation (:class:`UpstoxHistoricalDataClient.fetch_intraday_day` /
``fetch_5m_day`` plus the shared :class:`UpstoxHistoricalDataProvider` transport
and the runtime analytics credential ``FNO_UPSTOX_ACCESS_TOKEN``) and the
existing :class:`~fno_ai_paper_trading.alerting.health.Watchdog` (default
5-minute ``max_bar_age``) --- no execution/order API is imported or reachable,
LTP is never substituted for historical candles, and nothing is fabricated.

Final states (exactly):

* ``TODAY_5M = READY``     -- today's candles exist AND the latest candle is
  within the Watchdog freshness window.
* ``TODAY_5M = NOT_READY`` -- zero candles, candles not dated today, or the
  latest candle is outside the Watchdog freshness window.
* ``TODAY_5M = ERROR``     -- HTTP/API/credential failure (exit code 2).

Exit codes: 0 = READY, 1 = NOT_READY, 2 = ERROR/CONFIGURATION/API FAILURE.
HTTP 200 with an empty ``candles`` array is NOT an error (exit code 1).
An atomic status file is written to ``reports\\live_readiness\\upstox_today_5m_status.json``
and a human-readable line is appended to ``reports\\live_readiness\\upstox_today_5m.log``.
No credentials are ever printed, logged or written.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.alerting.health import ComponentStatus, Watchdog  # noqa: E402
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.data.intervals import upstox_unit_interval  # noqa: E402
from fno_ai_paper_trading.data.market_hours import NSE_TZ  # noqa: E402
from fno_ai_paper_trading.data.upstox_provider import (  # noqa: E402
    UPSTOX_INTRADAY_CANDLE_PATH,
    UPSTOX_SOURCE_HISTORICAL,
    UPSTOX_SOURCE_INTRADAY,
    upstox_candle_source,
)
from fno_ai_paper_trading.fresh_oos.client import UpstoxHistoricalDataClient  # noqa: E402
from fno_ai_paper_trading.fresh_oos.credential_provider import (  # noqa: E402
    RuntimeCredentialProvider,
    STATE_PRESENT,
)
from fno_ai_paper_trading.fresh_oos.protocol import (  # noqa: E402
    INTERVAL,
    SESSION_END_EXCLUSIVE,
    SESSION_FIRST_TIME,
)

INSTRUMENT_NAME = "NIFTY 50"
DEFAULT_ROOT = REPO_ROOT / "reports" / "live_readiness"
STATUS_FILENAME = "upstox_today_5m_status.json"
LOG_FILENAME = "upstox_today_5m.log"

READY = "READY"
NOT_READY = "NOT_READY"
ERROR = "ERROR"


@dataclass(frozen=True)
class TodayCheck:
    """Result of one readiness probe (all fields are safe to print/write)."""

    checked_at: str
    nse_date: str
    instrument: str
    interval: str
    session_start: str
    session_end: str
    request: str
    source: str
    http_status: int | None
    api_status: str | None
    candle_count: int
    first_candle: str | None
    latest_candle: str | None
    latest_candle_age_seconds: int | None
    watchdog_fresh: bool | None
    ready: bool
    credential: str
    hard_error: bool = False
    blocker: str = ""

    @property
    def state(self) -> str:
        if self.hard_error:
            return ERROR
        return READY if self.ready else NOT_READY

    def exit_code(self) -> int:
        if self.hard_error:
            return 2
        return 0 if self.ready else 1

    def to_status_payload(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at,
            "nse_date": self.nse_date,
            "instrument": self.instrument,
            "interval": self.interval,
            "session_start": self.session_start,
            "session_end": self.session_end,
            "request": self.request,
            "source": self.source,
            "http_status": self.http_status,
            "api_status": self.api_status,
            "candle_count": self.candle_count,
            "first_candle": self.first_candle,
            "latest_candle": self.latest_candle,
            "latest_candle_age_seconds": self.latest_candle_age_seconds,
            "watchdog_fresh": self.watchdog_fresh,
            "credential_present": self.credential == STATE_PRESENT,
            "ready": self.ready,
            "state": self.state,
            "blocker": self.blocker,
        }


def _now_nse() -> datetime:
    """Current time as a naive IST datetime (consistent with bar timestamps)."""
    return datetime.now(NSE_TZ).replace(tzinfo=None)


def _session_window(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, SESSION_FIRST_TIME.hour, SESSION_FIRST_TIME.minute)
    end = datetime(day.year, day.month, day.day, SESSION_END_EXCLUSIVE.hour, SESSION_END_EXCLUSIVE.minute)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _intraday_request_path(instrument) -> str:
    """The exact V3 path for CURRENT trading day candles (Intraday endpoint)."""
    from urllib.parse import quote

    key = (getattr(instrument, "exchange_token", None) or "").strip()
    if not key:
        raise ValueError(f"instrument {instrument.symbol if hasattr(instrument, 'symbol') else instrument!r} "
                         "has no Upstox instrument key (exchange_token)")
    unit, number = upstox_unit_interval(INTERVAL)
    return f"{UPSTOX_INTRADAY_CANDLE_PATH}/{quote(key)}/{unit}/{number}"


def _request_path(instrument, day: date) -> str:
    """The exact V3 path the production provider would call for one COMPLETED day."""
    from urllib.parse import quote

    key = (getattr(instrument, "exchange_token", None) or "").strip()
    if not key:
        raise ValueError(f"instrument {instrument.symbol if hasattr(instrument, 'symbol') else instrument!r} "
                         "has no Upstox instrument key (exchange_token)")
    unit, number = upstox_unit_interval(INTERVAL)
    return f"/v3/historical-candle/{quote(key)}/{unit}/{number}/{day.isoformat()}/{day.isoformat()}"


def _redact(client: UpstoxHistoricalDataClient, text: str) -> str:
    redact = getattr(client, "redact_error_text", None)
    return redact(text) if callable(redact) else text


def run_check(
    client: UpstoxHistoricalDataClient,
    instrument,
    day: date,
    now: datetime,
    *,
    probe: Callable[[date], tuple[int | None, str | None]] | None = None,
    watchdog: Watchdog | None = None,
    source: str | None = None,
) -> TodayCheck:
    """Probe Upstox (HTTP/API status) then fetch bars via the production path.

    ``probe`` performs the raw GET status probe; when ``None`` the script's own
    production probe (via the client's provider transport) is used. ``watchdog``
    defaults to the production ``Watchdog()`` (5-minute ``max_bar_age``).

    Candle source is deterministic: today (``day == now.date()``) uses Intraday
    V3 (:meth:`UpstoxHistoricalDataClient.fetch_intraday_day`); any other day
    uses Historical V3 (:meth:`UpstoxHistoricalDataClient.fetch_5m_day`). There
    is no Intraday -> Historical fallback: today's data must come from Intraday
    V3 or the monitor reports NOT_READY / fail closed.
    """
    checked_at = _now_nse().isoformat(timespec="seconds")
    session_start, session_end = _session_window(day)
    credential = RuntimeCredentialProvider().probe().state
    source = upstox_candle_source(day, now.date()) if source is None else source
    is_intraday = source == UPSTOX_SOURCE_INTRADAY
    request = (
        _intraday_request_path(instrument)
        if is_intraday
        else _request_path(instrument, day)
    )
    instrument_label = instrument.exchange_token or instrument.symbol or str(instrument)

    probe_fn = probe or (lambda _d: _probe_status(client, day, source=source))

    try:
        http_status, api_status = probe_fn(day)
    except Exception as exc:  # noqa: BLE001 - surfaced and redacted in report
        return TodayCheck(
            checked_at=checked_at,
            nse_date=day.isoformat(),
            instrument=instrument_label,
            interval=INTERVAL,
            session_start=session_start,
            session_end=session_end,
            request=request,
            source=source,
            http_status=None,
            api_status=None,
            candle_count=0,
            first_candle=None,
            latest_candle=None,
            latest_candle_age_seconds=None,
            watchdog_fresh=None,
            ready=False,
            credential=credential,
            hard_error=True,
            blocker=_redact(client, f"HTTP/API probe failed: {exc}"),
        )

    seam = "fetch_intraday_day" if is_intraday else "fetch_5m_day"
    try:
        bars = (
            client.fetch_intraday_day(instrument)
            if is_intraday
            else client.fetch_5m_day(instrument, day)
        )
    except Exception as exc:  # noqa: BLE001 - surfaced and redacted in report
        return TodayCheck(
            checked_at=checked_at,
            nse_date=day.isoformat(),
            instrument=instrument_label,
            interval=INTERVAL,
            session_start=session_start,
            session_end=session_end,
            request=request,
            source=source,
            http_status=http_status,
            api_status=api_status,
            candle_count=0,
            first_candle=None,
            latest_candle=None,
            latest_candle_age_seconds=None,
            watchdog_fresh=None,
            ready=False,
            credential=credential,
            hard_error=True,
            blocker=_redact(client, f"{seam} failed: {exc}"),
        )

    candle_count = len(bars)
    first_candle = bars[0].timestamp.isoformat(timespec="seconds") if bars else None
    latest_candle = bars[-1].timestamp.isoformat(timespec="seconds") if bars else None

    watchdog_fresh: bool | None = None
    blocker = ""
    watchdog = watchdog or Watchdog(now=lambda: now)
    if bars:
        latest = bars[-1].timestamp
        age = now - latest
        latest_candle_age_seconds = max(0, int(age.total_seconds()))
        report = watchdog.evaluate(latest_bar_time=latest, components={})
        watchdog_fresh = report.findings[0].status is ComponentStatus.HEALTHY
        if latest.date() != day:
            blocker = (
                f"latest candle {latest.isoformat()} is not dated {day.isoformat()}"
                f" ({latest.date().isoformat()}); candles do not belong to today"
            )
        elif not watchdog_fresh:
            blocker = (
                f"latest candle {latest.isoformat()} age {latest_candle_age_seconds}s "
                f"exceeds Watchdog max_bar_age {watchdog.max_bar_age}"
            )
    else:
        latest_candle_age_seconds = None
        blocker = (
            f"Upstox returned zero candles for {day.isoformat()} in the "
            f"{session_start}->{session_end} IST window via {source} "
            f"(HTTP {http_status}, api {api_status or 'n/a'}); "
            f"current-day data must come from Intraday V3 (no historical fallback)"
        )

    ready = candle_count > 0 and latest_candle is not None and bars[-1].timestamp.date() == day and bool(watchdog_fresh)

    return TodayCheck(
        checked_at=checked_at,
        nse_date=day.isoformat(),
        instrument=instrument_label,
        interval=INTERVAL,
        session_start=session_start,
        session_end=session_end,
        request=request,
        source=source,
        http_status=http_status,
        api_status=api_status,
        candle_count=candle_count,
        first_candle=first_candle,
        latest_candle=latest_candle,
        latest_candle_age_seconds=latest_candle_age_seconds,
        watchdog_fresh=watchdog_fresh,
        ready=ready,
        credential=credential,
        blocker=blocker,
    )


def _probe_status(client: UpstoxHistoricalDataClient, day: date, *, source: str) -> tuple[int, str]:
    """Raw GET via the production provider transport, routed by candle source."""
    provider = client._provider()
    instrument = get_research_instrument(INSTRUMENT_NAME)
    path = (
        _intraday_request_path(instrument)
        if source == UPSTOX_SOURCE_INTRADAY
        else _request_path(instrument, day)
    )
    response = provider._get(path)
    payload = response.json if isinstance(response.json, dict) else {}
    api_status = str(payload.get("status") or "")
    return int(response.status), api_status


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically persist the status JSON (scratch + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".tmp-{path.name}")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with scratch.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(scratch, path)
    except OSError:
        if scratch.exists():
            try:
                scratch.unlink()
            except OSError:
                pass
        raise


def _append_log(path: Path, check: TodayCheck) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"{check.checked_at} | date={check.nse_date} | source={check.source} "
        f"| request={check.request} "
        f"| http={check.http_status} | api={check.api_status or 'n/a'} "
        f"| candles={check.candle_count} | latest={check.latest_candle or 'none'} "
        f"| age_s={check.latest_candle_age_seconds or 'n/a'} "
        f"| freshness={check.watchdog_fresh} | status=({check.state}) "
        f"| blocker={check.blocker or 'none'}"
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def _print_summary(check: TodayCheck) -> None:
    print(f"TODAY_5M = {check.state}")
    print(f"nse_date            = {check.nse_date}")
    print(f"instrument          = {check.instrument}")
    print(f"interval            = {check.interval}")
    print(f"session_window      = {check.session_start} -> {check.session_end} IST")
    print(f"request             = {check.request}")
    print(f"source              = {check.source}")
    print(f"http_status         = {check.http_status}")
    print(f"api_status          = {check.api_status or 'n/a'}")
    print(f"candle_count        = {check.candle_count}")
    print(f"first_candle        = {check.first_candle or 'none'}")
    print(f"latest_candle       = {check.latest_candle or 'none'}")
    print(f"latest_candle_age_s = {check.latest_candle_age_seconds or 'n/a'}")
    print(f"watchdog_fresh      = {check.watchdog_fresh}")
    print(f"credential          = {check.credential}")
    print(f"blocker             = {check.blocker or 'none'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "READ-ONLY monitor: check whether TODAY's Upstox 5m NIFTY 50 "
            "candles (Intraday V3 for today, Historical V3 for completed days) "
            "are available and Watchdog-fresh (no orders, no execution APIs, "
            "no code writes)."
        ),
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="report directory (default: reports/live_readiness)")
    parser.add_argument("--date", help="force NSE date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-write", action="store_true",
                        help="do not write status JSON / log (stdout only)")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args(argv)

    now = _now_nse()
    day = date.fromisoformat(args.date) if args.date else now.date()
    instrument = get_research_instrument(INSTRUMENT_NAME)
    client = UpstoxHistoricalDataClient()

    check = run_check(client, instrument, day, now)
    _print_summary(check)

    if not args.no_write:
        root = args.root
        _atomic_write_json(root / STATUS_FILENAME, check.to_status_payload())
        _append_log(root / LOG_FILENAME, check)
        print(f"wrote               = {root / STATUS_FILENAME}")
    return check.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())