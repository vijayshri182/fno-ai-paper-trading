"""The fresh-OOS collector orchestrator (detect -> acquire -> validate ->
hash -> store -> record -> report readiness).

One :meth:`FreshOosCollector.collect_once` call performs a deterministic,
boundary-enforced acquisition pass:

1. take the cross-process run lock (otherwise ``LOCKED``);
2. resolve the eligible completed trading days strictly after the fresh-OOS
   boundary (2026-09-11) in chronological order -- catch-up of missed dates is
   the normal path, not a special case;
3. for each day: detect already-acquired coverage (dedicated store first, then
   the established ``datasets/`` pool). Present + same SHA-256 -> NOOP;
   present + different hash -> DATA_CONFLICT (never overwrite); absent ->
   fetch (GET-only historical), validate, hash, store atomically;
4. record every day in the manifest pool and append one run record;
5. report deterministic data readiness (``>=20 trading days AND >=1500 bars``)
   as ``DATA_READY_FOR_SINGLE_USE_FRESH_OOS_VALIDATION``. Validation itself is
   never triggered here.

Safety invariants held by construction: GET-only historical data, no orders,
no strategy/execution imports, no writes outside the fresh-OOS store, and no
bar dated on/before the boundary is ever stored (it fails the run instead).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from fno_ai_paper_trading.data.dataset_store import dataset_hash
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument
from fno_ai_paper_trading.data.market_hours import is_trading_day
from fno_ai_paper_trading.fresh_oos.client import HistoricalDataClient
from fno_ai_paper_trading.fresh_oos.errors import (  # noqa: F401  (re-exported for tests)
    CredentialsUnavailableError,
    ProtocolViolationError,
    status_from_exception,
)
from fno_ai_paper_trading.fresh_oos.lock import CollectorLock
from fno_ai_paper_trading.fresh_oos.manifest import FreshOosManifest
from fno_ai_paper_trading.fresh_oos.protocol import (
    EXPECTED_BARS_PER_FULL_DAY,
    FRESH_OOS_BOUNDARY,
    MIN_BARS,
    MIN_TRADES,
    MIN_TRADING_DAYS,
    POOL_ACQUIRED,
    POOL_CONFLICT,
    POOL_NOOP_DEDICATED,
    POOL_NOOP_ESTABLISHED,
    RunOutcome,
    STATUS_DATA_CONFLICT,
    STATUS_DATA_INVALID,
    STATUS_INCOMPLETE,
    STATUS_LOCKED,
    STATUS_NO_DATA,
    STATUS_NO_NEW_DATA,
    STATUS_PROTOCOL_VIOLATION,
    STATUS_SUCCESS,
    new_run_id,
    utc_now_iso,
    worst_status,
)
from fno_ai_paper_trading.fresh_oos.store import (
    DEFAULT_DATASETS_DIR,
    DEFAULT_STORE_ROOT,
    FreshOosStore,
    EstablishedDay,
    scan_established_datasets,
)
from fno_ai_paper_trading.fresh_oos.validation import validate_5m_day
from fno_ai_paper_trading.models.instruments import Instrument

# Run-level statuses that count as a clean pass for a single day.
_OK_RUN_STATUSES = frozenset({STATUS_SUCCESS, STATUS_NO_NEW_DATA, STATUS_NO_DATA})

_MANIFEST_FILENAME = "fresh_oos_manifest.json"


@dataclass
class FreshOosCollectorConfig:
    """Read-only inputs that pin the collector's operating context."""

    boundary: date = FRESH_OOS_BOUNDARY
    instrument: Instrument = field(
        default_factory=lambda: get_research_instrument("NIFTY 50")
    )
    namespace: str = "NIFTY_50_5m"
    root: str | Path = DEFAULT_STORE_ROOT
    datasets_dir: str | Path = DEFAULT_DATASETS_DIR
    min_days: int = MIN_TRADING_DAYS
    min_bars: int = MIN_BARS
    min_trades: int = MIN_TRADES
    expected_bars_per_day: int = EXPECTED_BARS_PER_FULL_DAY
    max_days_per_run: int = 0  # 0 = unlimited
    verify_present: bool = True  # re-fetch present days to detect hash drift
    now_fn: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] | None = None

    def __post_init__(self) -> None:
        if self.boundary <= date(2000, 1, 1):
            raise ValueError("boundary must be a modern date")
        if self.expected_bars_per_day <= 0:
            raise ValueError("expected_bars_per_day must be positive")


class FreshOosCollector:
    """Deterministic, immutable, lock-protected fresh-OOS acquisition."""

    def __init__(
        self,
        config: FreshOosCollectorConfig,
        client: HistoricalDataClient,
        store: FreshOosStore | None = None,
        manifest: FreshOosManifest | None = None,
        lock: CollectorLock | None = None,
    ) -> None:
        self.config = config
        self.client = client
        roots = Path(config.root)
        self.store = store or FreshOosStore(roots, namespace=config.namespace)
        if manifest is not None:
            self.manifest = manifest
        else:
            # Restart-safe: a collector built fresh (CLI, scheduler) must see
            # the days already accepted on disk, never start from an empty pool.
            manifest_path = roots / _MANIFEST_FILENAME
            self.manifest = (
                FreshOosManifest.load(manifest_path)
                if manifest_path.exists()
                else FreshOosManifest(path=manifest_path)
            )
        self.lock = lock or CollectorLock(roots, now_fn=config.now_fn)
        self._established: dict[date, EstablishedDay] = {}

    # ------------------------------------------------------------------ #
    # Eligibility
    # ------------------------------------------------------------------ #

    def eligible_dates(self, now: datetime) -> list[date]:
        """All completed trading days in ``(boundary, today)``, chronological."""
        timestamp = now if now.tzinfo is None else now.astimezone().replace(tzinfo=None)
        today = timestamp.date()
        dates: list[date] = []
        day = self.config.boundary + timedelta(days=1)
        while day < today:
            if is_trading_day(datetime(day.year, day.month, day.day, 12, 0)):
                dates.append(day)
            day += timedelta(days=1)
        limit = self.config.max_days_per_run
        if limit and limit > 0 and len(dates) > limit:
            return dates[:limit]
        return sorted(dates)

    def _established_index(self) -> dict[date, EstablishedDay]:
        if not self._established:
            self._established = scan_established_datasets(
                self.config.datasets_dir, boundary=self.config.boundary
            )
        return self._established

    # ------------------------------------------------------------------ #
    # One acquisition pass
    # ------------------------------------------------------------------ #

    def collect_once(
        self,
        *,
        now: datetime | None = None,
        force_date: date | None = None,
    ) -> RunOutcome:
        now = now or self.config.now_fn()
        run_id = new_run_id(now)
        started = utc_now_iso(now)

        if force_date is not None and force_date <= self.config.boundary:
            return self._outcome(
                run_id,
                started,
                now,
                status=STATUS_PROTOCOL_VIOLATION,
                dates_attempted=(force_date.isoformat(),),
                errors={force_date.isoformat(): STATUS_PROTOCOL_VIOLATION},
                message=(
                    f"refusing to acquire {force_date.isoformat()}: fresh OOS must be "
                    f"strictly after {self.config.boundary.isoformat()}"
                ),
            )

        try:
            self.lock.acquire(run_id=run_id)
        except Exception as exc:
            return self._outcome(
                run_id,
                started,
                now,
                status=status_from_exception(exc),
                dates_attempted=(),
                errors={},
                message="another collector run is active; this pass was skipped",
            )

        try:
            established = self._established_index()
            dates = (
                self._bounded_single([force_date])
                if force_date is not None
                else self.eligible_dates(now)
            )
            status_by_date: dict[str, str] = {}
            for day in dates:
                status_by_date[day.isoformat()] = self._process_day(day, now, established)

            if not status_by_date:
                final = STATUS_NO_NEW_DATA
            else:
                run_statuses = list(status_by_date.values())
                # Every per-date outcome maps to a closed-set run status; absent
                # a LOCKED/PROTOCOL_VIOLATION, the worst determines the run.
                final = worst_status(list(run_statuses))

            accepted = sorted(d for d, s in status_by_date.items() if s == STATUS_SUCCESS)
            noop = sorted(d for d, s in status_by_date.items() if s == STATUS_NO_NEW_DATA)
            conflicts = sorted(d for d, s in status_by_date.items() if s == STATUS_DATA_CONFLICT)
            errors = {d: s for d, s in status_by_date.items() if s not in _OK_RUN_STATUSES}

            outcome = self._outcome(
                run_id,
                started,
                now,
                status=final,
                dates_attempted=tuple(sorted(status_by_date)),
                accepted=tuple(accepted),
                noop=tuple(noop),
                conflicts=tuple(conflicts),
                errors=dict(sorted(errors.items())),
                message=_summarize(final, accepted, noop, conflicts, errors),
            )
            self._save_manifest(now)
            return outcome
        finally:
            self.lock.release()

    def _bounded_single(self, dates: list[date | None]) -> list[date]:
        result = [day for day in dates if day is not None]
        limit = self.config.max_days_per_run
        if limit and limit > 0 and len(result) > limit:
            return result[:limit]
        return result

    # ------------------------------------------------------------------ #
    # Per-day processing
    # ------------------------------------------------------------------ #

    def _process_day(
        self, day: date, now: datetime, established: dict[date, EstablishedDay]
    ) -> str:
        """Acquire one day. Returns the closed-set run status for that day."""
        if day <= self.config.boundary:
            self._record(day, status=STATUS_PROTOCOL_VIOLATION, error="date on/before boundary", now=now)
            return STATUS_PROTOCOL_VIOLATION

        try:
            stored = self.store.find(day)
        except Exception as exc:
            status = status_from_exception(exc)
            self._record(day, status=status, error=str(exc), now=now)
            return status

        if stored is not None:
            return self._resolve_present(
                day,
                existing_hash=stored.data_hash,
                num_bars=stored.num_bars,
                origin="store",
                source=str(stored.csv_path),
                now=now,
            )

        est = established.get(day)
        if est is not None:
            if not est.verified:
                self._record(
                    day,
                    status=STATUS_DATA_CONFLICT,
                    data_hash=est.data_hash,
                    source=str(est.csv_path),
                    error="established dataset hash verification failed",
                    now=now,
                )
                return STATUS_DATA_CONFLICT
            return self._resolve_present(
                day,
                existing_hash=est.data_hash,
                num_bars=_established_num_bars(est),
                origin="datasets",
                source=str(est.csv_path),
                now=now,
            )

        return self._acquire_new(day, now)

    def _resolve_present(
        self,
        day: date,
        *,
        existing_hash: str,
        num_bars: int,
        origin: str,
        source: str,
        now: datetime,
    ) -> str:
        """A day already covered: NOOP, or DATA_CONFLICT on hash drift."""
        if not self.config.verify_present:
            pool = POOL_NOOP_DEDICATED if origin == "store" else POOL_NOOP_ESTABLISHED
            self._record(day, status=pool, data_hash=existing_hash, num_bars=num_bars, source=source, now=now)
            return STATUS_NO_NEW_DATA

        try:
            bars = self.client.fetch_5m_day(self.config.instrument, day)
        except Exception as exc:
            status = status_from_exception(exc)
            self._record(day, status=status, error=str(exc), source=source, now=now)
            return status

        fetched_hash = dataset_hash(bars)
        if fetched_hash == existing_hash:
            pool = POOL_NOOP_DEDICATED if origin == "store" else POOL_NOOP_ESTABLISHED
            self._record(day, status=pool, data_hash=existing_hash, num_bars=num_bars, source=source, now=now)
            return STATUS_NO_NEW_DATA

        self._record(
            day,
            status=POOL_CONFLICT,
            data_hash=existing_hash,
            num_bars=num_bars,
            source=source,
            error=(
                f"already-acquired day no longer matches the provider hash "
                f"({existing_hash[:12]}... vs {fetched_hash[:12]}...); refusing to overwrite"
            ),
            now=now,
        )
        return STATUS_DATA_CONFLICT

    def _acquire_new(self, day: date, now: datetime) -> str:
        try:
            bars = self.client.fetch_5m_day(self.config.instrument, day)
        except Exception as exc:
            status = status_from_exception(exc)
            self._record(day, status=status, error=str(exc), now=now)
            return status

        if not bars:
            self._record(
                day,
                status=STATUS_NO_DATA,
                error="provider returned no bars for the session date",
                now=now,
            )
            return STATUS_NO_DATA

        report = validate_5m_day(
            bars,
            day,
            boundary=self.config.boundary,
            expected_bars=self.config.expected_bars_per_day,
        )
        if not report.ok:
            status = report.status()
            self._record(
                day,
                status=status,
                num_bars=len(bars),
                error="; ".join(str(issue) for issue in report.issues[:3]),
                now=now,
            )
            return status  # INCOMPLETE or DATA_INVALID

        data_hash = dataset_hash(bars)
        try:
            self.store.write_day(
                day,
                bars,
                instrument=self.config.instrument,
                data_hash=data_hash,
                downloaded_at=utc_now_iso(now),
            )
        except FileExistsError as exc:
            self._record(
                day,
                status=POOL_CONFLICT,
                data_hash=data_hash,
                num_bars=len(bars),
                error=str(exc),
                now=now,
            )
            return STATUS_DATA_CONFLICT

        self._record(
            day,
            status=POOL_ACQUIRED,
            data_hash=data_hash,
            num_bars=len(bars),
            source=str(self.store.day_dir(day)),
            now=now,
        )
        return STATUS_SUCCESS

    # ------------------------------------------------------------------ #
    # Manifest + reporting
    # ------------------------------------------------------------------ #

    def _record(self, day: date, *, status: str, data_hash: str = "", num_bars: int = 0,
                source: str = "", error: str = "", now: datetime) -> None:
        self.manifest.record_date(
            day,
            status=status,
            data_hash=data_hash,
            num_bars=num_bars,
            source=source,
            error=self._redact_error_text(error),
            updated_at=utc_now_iso(now),
        )

    def _redact_error_text(self, text: str) -> str:
        """Strip any occurrence of the Upstox token from recorded error text.

        When the client exposes a ``redact_error_text`` hook it is used so a
        token that is accidentally embedded in downstream exception text never
        reaches the manifest. Fakes without the hook pass the text through.
        """
        if not text:
            return text
        redactor = getattr(self.client, "redact_error_text", None)
        if callable(redactor):
            return redactor(text)
        return text

    def _outcome(
        self,
        run_id: str,
        started: str,
        now: datetime,
        *,
        status: str,
        dates_attempted: tuple[str, ...] = (),
        accepted: tuple[str, ...] = (),
        noop: tuple[str, ...] = (),
        conflicts: tuple[str, ...] = (),
        errors: dict[str, str] | str = {},
        message: str = "",
    ) -> RunOutcome:
        error_map = dict(errors) if isinstance(errors, dict) else {}
        outcome = RunOutcome(
            run_id=run_id,
            started_at=started,
            ended_at=utc_now_iso(self.config.now_fn()),
            status=status,
            dates_attempted=dates_attempted,
            accepted=accepted,
            noop=noop,
            conflicts=conflicts,
            errors=error_map,
            message=message,
        )
        self.manifest.add_run(outcome)
        return outcome

    def _save_manifest(self, now: datetime) -> None:
        self.manifest.generated_at = utc_now_iso(now)
        self.manifest.save()

    # ------------------------------------------------------------------ #
    # Status block
    # ------------------------------------------------------------------ #

    def describe_status(self, now: datetime | None = None) -> dict[str, object]:
        now = now or self.config.now_fn()
        readiness = self.manifest.readiness(now=utc_now_iso(now))
        established = scan_established_datasets(
            self.config.datasets_dir, boundary=self.config.boundary
        )
        pool_rows: list[dict[str, object]] = []
        for day in self.manifest.accepted_dates:
            entry = self.manifest.pool.get(day.isoformat(), {})
            pool_rows.append(
                {
                    "day": day.isoformat(),
                    "status": entry.get("status", ""),
                    "num_bars": entry.get("num_bars", 0),
                    "data_hash": entry.get("data_hash", ""),
                    "source": entry.get("source", ""),
                }
            )
        first_date = pool_rows[0]["day"] if pool_rows else ""
        last_date = pool_rows[-1]["day"] if pool_rows else ""
        return {
            "boundary_exclusive": self.config.boundary.isoformat(),
            "instrument": f"{self.config.instrument.symbol} ({self.config.namespace})",
            "store_root": str(self.config.root),
            "datasets_dir": str(self.config.datasets_dir),
            "accepted_days": len(pool_rows),
            "accepted_bars": self.manifest.accepted_bars,
            "first_date": first_date,
            "last_date": last_date,
            "readiness": readiness.to_dict(),
            "validation": "NOT RUN",
            "pool": pool_rows,
            "established_reused": sorted(d.isoformat() for d in established),
            "last_run": self.manifest.last_run().to_dict() if self.manifest.last_run() else None,
        }


def _established_num_bars(est: EstablishedDay) -> int:
    import csv

    try:
        with est.csv_path.open("r", encoding="utf-8", newline="") as handle:
            return sum(1 for _ in csv.reader(handle)) - 1
    except OSError:
        return 0


def _summarize(
    status: str,
    accepted: list[str],
    noop: list[str],
    conflicts: list[str],
    errors: dict[str, str],
) -> str:
    pieces = [f"final status {status}"]
    if accepted:
        pieces.append(f"acquired {len(accepted)} day(s)")
    if noop:
        pieces.append(f"{len(noop)} day(s) already present (NOOP)")
    if conflicts:
        pieces.append(f"{len(conflicts)} day(s) in data conflict")
    if errors:
        pieces.append("failures: " + ", ".join(sorted(set(errors.values()))))
    return "; ".join(pieces) if pieces else "no eligible dates"