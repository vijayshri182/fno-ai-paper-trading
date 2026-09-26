"""Historical options dataset validation per the frozen ``options_data_contract``.

This implements the immutable 12-field ``OPTION_CHAIN_BAR`` schema contract
(`docs/options_data_contract.md` §3-§6) as a deterministic, report-only
validator plus a manifest-aware directory loader:

* SHA-256 provenance (LF-normalized hash, the same convention as the fresh-OOS
  pool) — any file whose hash mismatches its manifest entry is REFUSED;
* schema exactness, identity consistency (CE/PE strike+expiry, futures expiry,
  underlying without strike/expiry), OHLC integrity, price/volume/OI checks;
* explicit timezone normalization (naive timestamps are interpreted as IST;
  aware UTC timestamps are converted to IST; any other zone is rejected);
* 5-minute timestamp alignment, duplicates, ordering and session-gap coverage
  (a missing intraday session must be declared with a NO_DATA marker row);
* option premiums require an option identity — underlying index prices can
  never masquerade as option premiums (mis-labelled rows are INVALID).

Nothing here repairs, filters, imputes or fabricates market fields: invalid
rows are reported with structured codes and the dataset is quarantined.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.data.market_hours import NSE_TZ

OPTION_BAR_FIELDS = (
    "ts", "instrument_key", "asset", "expiry", "strike",
    "open", "high", "low", "close", "volume", "oi", "source",
)
OPTION_ASSETS = frozenset({"UNDERLYING_INDEX", "FUTURES", "CE", "PE"})
FUTURES = "FUTURES"
CE = "CE"
PE = "PE"
UNDERLYING_INDEX = "UNDERLYING_INDEX"

BAR_INTERVAL_SECONDS = 300                     # 5-minute research bar
MAX_MISSING_BARS_WITHOUT_SENTINEL = 3          # > 3 consecutive missing bars -> gap
SESSION_FIRST = time(9, 15)
SESSION_LAST = time(15, 25)
SESSION_END_EXCLUSIVE = time(15, 30)

SYNTHETIC_LABEL = "SYNTHETIC_FIXTURE"

# Issue codes (closed set, structured for tests and callers).
CODE_SCHEMA = "SCHEMA"
CODE_IDENTITY_OPTION = "IDENTITY_OPTION"
CODE_IDENTITY_FUTURES = "IDENTITY_FUTURES"
CODE_IDENTITY_UNDERLYING = "IDENTITY_UNDERLYING"
CODE_IDENTITY_SIDE = "IDENTITY_SIDE"
CODE_PREMIUM_UNDERLYING = "PREMIUM_UNDERLYING"
CODE_OHLC = "OHLC"
CODE_NEGATIVE = "NEGATIVE"
CODE_TS_PARSE = "TS_PARSE"
CODE_TS_TZ = "TS_TZ"
CODE_TS_ALIGN = "TS_ALIGN"
CODE_TS_DUPLICATE = "TS_DUPLICATE"
CODE_TS_ORDER = "TS_ORDER"
CODE_COVERAGE_GAP = "COVERAGE_GAP"
CODE_MANIFEST_MISSING = "MANIFEST_MISSING"
CODE_MANIFEST_SCHEMA = "MANIFEST_SCHEMA"
CODE_MANIFEST_HASH = "MANIFEST_HASH"
CODE_DATASET_MISSING = "DATASET_MISSING"
CODE_SYNTHETIC_REQUIRED = "SYNTHETIC_REQUIRED"

STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class DatasetIssue:
    """One validation problem with a stable machine-readable code."""

    code: str
    message: str
    index: int | None = None

    def __str__(self) -> str:
        where = "" if self.index is None else f" (row {self.index})"
        return f"[{self.code}]{where} {self.message}"


@dataclass(frozen=True)
class DatasetValidation:
    """All issues found for a dataset plus an overall verdict (never repairs)."""

    num_rows: int
    issues: tuple[DatasetIssue, ...] = ()
    per_file: Mapping[str, "DatasetValidation"] = field(default_factory=dict)
    profiles: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def code_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.code] = counts.get(issue.code, 0) + 1
        return counts

    def status(self) -> str:
        """VALID / INVALID / QUARANTINED based on issue severity."""
        if self.ok:
            return STATUS_VALID
        if any(
            code in (CODE_MANIFEST_MISSING, CODE_MANIFEST_HASH, CODE_MANIFEST_SCHEMA)
            for code in self.codes
        ):
            return STATUS_QUARANTINED
        return STATUS_INVALID


# --------------------------------------------------------------------------- #
# Timezone normalisation and file hashing (mirrors the fresh-OOS conventions).
# --------------------------------------------------------------------------- #

def parse_normalized_ts(value: Any, *, index: int) -> tuple[datetime | None, str | None]:
    """Parse a bar timestamp into the naive-IST convention.

    Naive timestamps are interpreted as IST. Aware UTC timestamps are converted
    to IST. Any other timezone is rejected (``CODE_TS_TZ``). Returns
    ``(naive_ist_datetime, None)`` on success or ``(None, issue_code)``.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, CODE_TS_PARSE
        try:
            if text.endswith("Z"):
                dt = datetime.fromisoformat(text[:-1] + "+00:00")
            else:
                dt = datetime.fromisoformat(text)
        except ValueError:
            return None, CODE_TS_PARSE
    else:
        return None, CODE_TS_PARSE
    if dt.tzinfo is not None:
        offset = dt.utcoffset()
        if offset is None or offset != timedelta(0):
            return None, CODE_TS_TZ
        dt = dt.astimezone(NSE_TZ).replace(tzinfo=None)
    return dt, None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    if isinstance(value, str):
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def file_sha256_lf(path: Path) -> str:
    """SHA-256 over the LF-normalised text content (fresh-OOS convention)."""
    text = path.read_text(encoding="utf-8")          # universal newline: CRLF -> LF
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_no_data_row(raw: Mapping[str, Any]) -> bool:
    """A NO_DATA sentinel row: identity only, no market fields.

    Declares session absence for an identity. The row may carry
    ``ts`` / ``instrument_key`` / ``asset`` / ``expiry`` / ``strike`` /
    ``source`` but must not populate any of ``open``/``high``/``low``/``close``/
    ``volume``/``oi``. Suppresses the OHLC/gap checks for that identity.
    """
    identity_only = frozenset({"ts", "instrument_key", "asset", "expiry", "strike", "source"})
    market_fields = frozenset({"open", "high", "low", "close", "volume", "oi"})
    populated = {k for k in OPTION_BAR_FIELDS if _present(raw.get(k))}
    if populated & market_fields:
        return False
    return {"ts", "instrument_key"} <= populated <= identity_only


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


# --------------------------------------------------------------------------- #
# Row-level validation (pure, indexed for stable diagnostics).
# --------------------------------------------------------------------------- #

def validate_rows(rows: Sequence[Mapping[str, Any]]) -> DatasetValidation:
    """Validate a sequence of provider rows against the frozen contract."""
    issues: list[DatasetIssue] = []
    seen: set[tuple[str, datetime]] = set()
    last_by_identity: dict[str, tuple[datetime, int]] = {}
    profiles: dict[str, Any] = {"families": {}, "identities": set(), "date_span": None}

    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            issues.append(DatasetIssue(CODE_SCHEMA, "row is not a mapping", index))
            continue
        known = set(raw.keys())
        if not known <= set(OPTION_BAR_FIELDS):
            extra = sorted(known - set(OPTION_BAR_FIELDS))
            issues.append(DatasetIssue(CODE_SCHEMA, f"unknown field(s): {extra}", index))
            continue
        missing = [f for f in ("ts", "instrument_key", "open", "high", "low", "close") if not _present(raw.get(f))]
        if missing and not _is_no_data_row(raw):
            issues.append(DatasetIssue(CODE_SCHEMA, f"required field(s) missing: {missing}", index))

        ts_value = raw.get("ts")
        ts_norm, ts_code = parse_normalized_ts(ts_value, index=index)
        if ts_code:
            issues.append(DatasetIssue(ts_code, f"invalid timestamp {ts_value!r}", index))
            continue
        assert ts_norm is not None
        if (ts_norm.minute % 5 != 0) or ts_norm.second != 0 or ts_norm.microsecond != 0:
            issues.append(
                DatasetIssue(CODE_TS_ALIGN, f"{ts_norm.isoformat()} is not 5-minute aligned", index)
            )

        key = str(raw.get("instrument_key", "")).strip()
        if not key:
            issues.append(DatasetIssue(CODE_SCHEMA, "empty instrument_key", index))
            continue

        asset = str(raw.get("asset") or "").strip()
        if asset not in OPTION_ASSETS:
            issues.append(
                DatasetIssue(CODE_SCHEMA, f"asset {asset!r} not in {sorted(OPTION_ASSETS)}", index)
            )
        _profile(profiles, key, asset, ts_norm)

        # identity + no-underlying-as-premium rules
        strike = _to_decimal(raw.get("strike"))
        expiry = str(raw.get("expiry") or "").strip()
        if asset in (CE, PE):
            if strike is None or strike <= 0:
                issues.append(DatasetIssue(CODE_IDENTITY_OPTION, f"option row {key} has no positive strike", index))
            if not expiry:
                issues.append(DatasetIssue(CODE_IDENTITY_OPTION, f"option row {key} has no expiry", index))
            if not asset_ok(strike, expiry, key, asset):
                issues.append(
                    DatasetIssue(CODE_PREMIUM_UNDERLYING, f"option row {key} carries an underlying-style identity", index)
                )
        elif asset == FUTURES:
            if not expiry:
                issues.append(DatasetIssue(CODE_IDENTITY_FUTURES, f"futures row {key} has no expiry", index))
        elif asset == UNDERLYING_INDEX:
            if strike is not None or expiry:
                issues.append(
                    DatasetIssue(CODE_IDENTITY_UNDERLYING, f"underlying row {key} carries strike/expiry", index)
                )

        # duplicate + ordering + session gap per identity (runs for NO_DATA
        # sentinels too, so a sentinel participates in the coverage chain and
        # suppresses a gap across its declared absence)
        dup = (key, ts_norm)
        if dup in seen:
            issues.append(DatasetIssue(CODE_TS_DUPLICATE, f"duplicate (identity, ts) {key} {ts_norm.isoformat()}", index))
        seen.add(dup)
        prior = last_by_identity.get(key)
        if prior is not None:
            prev_ts, prev_index = prior
            if ts_norm < prev_ts:
                issues.append(
                    DatasetIssue(CODE_TS_ORDER, f"out-of-order timestamp for {key} at {ts_norm.isoformat()}", index)
                )
            elif ts_norm > prev_ts:
                gap_code = _session_gap(prev_ts, ts_norm)
                if gap_code:
                    issues.append(DatasetIssue(gap_code, f"missing session coverage between {prev_ts.isoformat()} and {ts_norm.isoformat()} for {key}", index))
        last_by_identity[key] = (ts_norm, index)

        # OHLC / volume / oi (sentinel rows carry no market fields)
        if _is_no_data_row(raw):
            continue
        ohlc = {f: _to_decimal(raw.get(f)) for f in ("open", "high", "low", "close")}
        if any(v is None for v in ohlc.values()):
            missing = [f for f, v in ohlc.items() if v is None]
            issues.append(DatasetIssue(CODE_SCHEMA, f"non-numeric OHLC in {missing} for {key}", index))
            continue
        o, h, l, c = ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]
        for name, value in (("open", o), ("high", h), ("low", l), ("close", c)):
            if value < 0:
                issues.append(DatasetIssue(CODE_NEGATIVE, f"{name} is negative for {key}", index))
        if h < max(o, c):
            issues.append(DatasetIssue(CODE_OHLC, f"high < max(open, close) for {key}", index))
        if l > min(o, c):
            issues.append(DatasetIssue(CODE_OHLC, f"low > min(open, close) for {key}", index))
        if h < l:
            issues.append(DatasetIssue(CODE_OHLC, f"high < low for {key}", index))

        volume = _to_decimal(raw.get("volume"))
        oi = _to_decimal(raw.get("oi"))
        if volume is not None and volume < 0:
            issues.append(DatasetIssue(CODE_NEGATIVE, f"negative volume for {key}", index))
        if oi is not None and oi < 0:
            issues.append(DatasetIssue(CODE_NEGATIVE, f"negative oi for {key}", index))

    return DatasetValidation(
        num_rows=len(rows),
        issues=tuple(issues),
        profiles=dict(profiles),
    )


def _profile(profiles: dict[str, Any], key: str, asset: str, ts: datetime) -> None:
    families = profiles["families"]
    families[asset] = families.get(asset, 0) + 1
    profiles["identities"].add(key)
    if profiles["date_span"] is None:
        profiles["date_span"] = [ts.date(), ts.date()]
    else:
        span = profiles["date_span"]
        span[0] = min(span[0], ts.date())
        span[1] = max(span[1], ts.date())


def asset_ok(strike: Decimal | None, expiry: str, key: str, asset: str) -> bool:
    """Option identity sanity: a CE/PE row must look like an option contract.

    A row whose ``instrument_key`` matches the underlying index key, or which
    carries no expiry, cannot be an option premium (guards the documented
    prohibition on substituting underlying NIFTY prices for option premiums).
    """
    upper = key.upper()
    if "NSE_INDEX" in upper or "NIFTY 50" in upper:
        return False
    if strike is None or strike <= 0 or not expiry:
        return False
    if asset.upper() not in (CE, PE):
        return False
    return True


def _session_gap(prev_ts: datetime, next_ts: datetime) -> str | None:
    """Coverage-gap check between two consecutive timestamps of one identity.

    An overnight boundary (last session bar -> first session bar of a later
    day) is allowed. Any other delta greater than
    ``(MAX_MISSING_BARS_WITHOUT_SENTINEL + 1) * 5m``, or a silent multi-day
    jump without a NO_DATA marker, is reported as a coverage gap.
    """
    if prev_ts >= next_ts:
        return None
    delta = next_ts - prev_ts
    if delta.total_seconds() <= (MAX_MISSING_BARS_WITHOUT_SENTINEL + 1) * BAR_INTERVAL_SECONDS:
        return None
    prev_day_over = prev_ts.time() >= SESSION_LAST
    next_day_start = next_ts.time() <= SESSION_FIRST
    if prev_day_over and next_day_start and next_ts.date() > prev_ts.date():
        if (next_ts.date() - prev_ts.date()).days == 1:
            return None
        return CODE_COVERAGE_GAP  # silently skipped whole session day(s)
    return CODE_COVERAGE_GAP


# --------------------------------------------------------------------------- #
# Manifest-aware directory validation.
# --------------------------------------------------------------------------- #

def _load_csv(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def validate_dataset_directory(directory: Path, *, require_manifest: bool = True) -> DatasetValidation:
    """Validate an ``options_data/`` directory against its manifest.

    Checks, in order: directory+manifest presence; schema version; per-file
    SHA-256 match (LF-normalised); then row-level validation of every file.
    Never repairs: the first manifest/hash/schema failure quarantines the
    dataset; row issues are aggregated with per-file detail.

    ``directory`` = ``<repo>/options_data`` per the frozen contract layout.
    """
    if not directory.exists() or not directory.is_dir():
        return DatasetValidation(
            num_rows=0,
            issues=(DatasetIssue(CODE_DATASET_MISSING, f"dataset directory {directory} does not exist"),),
        )
    manifest_path = directory / "options_manifest.json"
    if not manifest_path.exists():
        issue = DatasetIssue(CODE_MANIFEST_MISSING, "options_manifest.json is missing")
        if require_manifest:
            return DatasetValidation(num_rows=0, issues=(issue,))
        return DatasetValidation(num_rows=0, issues=(), per_file={}, profiles={"_no_manifest": True})

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return DatasetValidation(
            num_rows=0,
            issues=(DatasetIssue(CODE_MANIFEST_SCHEMA, f"manifest unreadable: {exc}"),),
        )
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), Mapping):
        return DatasetValidation(
            num_rows=0,
            issues=(DatasetIssue(CODE_MANIFEST_SCHEMA, "manifest missing 'files' mapping"),),
        )

    schema = manifest.get("schema_version", "")
    if schema and schema != "options_chain_bar_v1":
        return DatasetValidation(
            num_rows=0,
            issues=(DatasetIssue(CODE_MANIFEST_SCHEMA, f"unsupported schema_version {schema!r}"),),
        )

    files = manifest["files"]
    file_validations: dict[str, DatasetValidation] = {}
    combined: list[DatasetIssue] = []
    num_rows = 0
    for name, meta in sorted(files.items()):
        path = directory / name
        if not path.exists():
            combined.append(DatasetIssue(CODE_MANIFEST_HASH, f"manifest entry {name} has no file"))
            file_validations[name] = DatasetValidation(num_rows=0, issues=(DatasetIssue(CODE_MANIFEST_HASH, "no file"),))
            continue
        if not isinstance(meta, Mapping) or not meta.get("sha256"):
            combined.append(DatasetIssue(CODE_MANIFEST_SCHEMA, f"manifest entry {name} has no sha256"))
            file_validations[name] = DatasetValidation(num_rows=0, issues=(DatasetIssue(CODE_MANIFEST_SCHEMA, "no sha256"),))
            continue
        actual = file_sha256_lf(path)
        if actual != meta["sha256"]:
            combined.append(
                DatasetIssue(CODE_MANIFEST_HASH, f"{name}: manifest sha256 {meta['sha256']} != actual {actual}")
            )
            file_validations[name] = DatasetValidation(
                num_rows=0, issues=(DatasetIssue(CODE_MANIFEST_HASH, "hash mismatch"),)
            )
            continue
        try:
            rows = _load_csv(path)
        except OSError as exc:
            combined.append(DatasetIssue(CODE_MANIFEST_SCHEMA, f"{name} unreadable: {exc}"))
            continue
        result = validate_rows(rows)
        num_rows += result.num_rows
        file_validations[name] = result
        combined.extend(result.issues)

    synthetic = bool(manifest.get("synthetic", False))
    if not synthetic and manifest.get("source") and not str(manifest["source"]).startswith("SYNTHETIC"):
        pass
    return DatasetValidation(
        num_rows=num_rows,
        issues=tuple(combined),
        per_file=file_validations,
        profiles={
            "schema_version": schema,
            "source": manifest.get("source"),
            "synthetic": synthetic,
            "coverage": manifest.get("coverage"),
        },
    )


def manifest_synthetic_ok(manifest: Mapping[str, Any]) -> bool:
    """When a fixture dataset declares synthetic, its source must say so.

    Enforces that synthetic fixture data is explicitly labelled and can never
    be consumed as observed market evidence.
    """
    synthetic = bool(manifest.get("synthetic", False))
    source = str(manifest.get("source") or "")
    if synthetic:
        return source.startswith(SYNTHETIC_LABEL) or source == SYNTHETIC_LABEL
    return True


__all__ = [
    "OPTION_BAR_FIELDS",
    "OPTION_ASSETS",
    "FUTURES",
    "CE",
    "PE",
    "UNDERLYING_INDEX",
    "SYNTHETIC_LABEL",
    "STATUS_VALID",
    "STATUS_INVALID",
    "STATUS_QUARANTINED",
    "DatasetIssue",
    "DatasetValidation",
    "parse_normalized_ts",
    "file_sha256_lf",
    "sha256_text",
    "validate_rows",
    "validate_dataset_directory",
    "manifest_synthetic_ok",
]