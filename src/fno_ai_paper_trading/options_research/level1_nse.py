"""NSE F&O EOD bhavcopy — Level-1 daily research acquisition pipeline (DATA-005).

Implements the controlled milestone for acquiring the **official NSE F&O EOD
bhavcopy** (the publicly distributed, end-of-day derivatives report) as a
Level-1 daily research dataset:

* deterministic parsing of the classic ``fo`` bhavcopy layout (CSV or ZIP);
* normalization to the frozen 12-field ``options_chain_bar`` column *names and
  types* (``ts, instrument_key, asset, expiry, strike, open, high, low, close,
  volume, oi, source``) — **at daily granularity only**;
* explicit label ``LEVEL_1_DAILY_RESEARCH_ONLY`` and a schema version
  (``nse_fo_eod_level1_daily_v1``) that is deliberately distinct from the
  intraday ``options_chain_bar_v1`` contract so the daily output is never
  consumed as an intraday 5-minute chain;
* structured validation (schema, required fields, dates, duplicates, missing
  values, invalid prices/quantities, contract identity, expiry/strike
  consistency, ordering and coverage with NO_DATA sentinels);
* deterministic SHA-256 fingerprinting (LF-normalized, fresh-OOS convention);
* a machine-readable manifest that refuses silent overwrites (idempotent
  re-acquisition returns ALREADY_ACQUIRED, differing bytes return REFUSED);
* a human-readable validation report.

Nothing here downloads, fabricates, imputes or repairs market data: bid/ask,
intraday quotes and missing observations are never invented. The module is
deterministic, read-only and credential-free (no ``execution``/``upstox``/
``credentials`` imports, no wall-clock reads — enforced by the package safety
tests). The retrieval timestamp is supplied by the caller (the runner script).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.options_research.dataset import (
    OPTION_BAR_FIELDS,
    SYNTHETIC_LABEL,
    file_sha256_lf,
    sha256_text,
)

SOURCE_ID = "NSE_FO_EOD_BHAVCOPY"
SCHEMA_VERSION = "nse_fo_eod_level1_daily_v1"
CONTRACT_SCHEMA_VERSION = "options_chain_bar_v1"   # frozen intraday contract
LEVEL_LABEL = "LEVEL_1_DAILY_RESEARCH_ONLY"

SESSION_END_IST = "15:30"                          # daily bar timestamp anchor
IST_TO_UTC_OFFSET = timedelta(hours=5, minutes=30)  # IST is UTC+5:30

# Canonical NSE F&O bhavcopy columns (classic layout, case-insensitive parse).
NSE_CSV_FIELDS = (
    "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
    "OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR",
    "CONTRACTS", "VAL_INLAKH", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP",
)
REQUIRED_NSE_FIELDS = (
    "SYMBOL", "EXPIRY_DT", "STRIKE_PR",
    "OPEN", "HIGH", "LOW", "CLOSE", "CONTRACTS", "OPEN_INT", "TIMESTAMP",
)

# Asset mapping from NSE bhavcopy option-type to the frozen contract assets.
NSE_OPTION_TYPE_CE = "CE"
NSE_OPTION_TYPE_PE = "PE"
NSE_FUTURES_SENTINELS = frozenset({"", "XX", "0", "NA"})

# Issue codes (closed set, structured for tests and callers).
CODE_SOURCE_MISSING = "SOURCE_MISSING"
CODE_TS_PARSE = "TS_PARSE"
CODE_TS_TZ = "TS_TZ"
CODE_DATE_PARSE = "DATE_PARSE"
CODE_NUMERIC = "NUMERIC"
CODE_NEGATIVE = "NEGATIVE"
CODE_OHLC = "OHLC"
CODE_IDENTITY_OPTION = "IDENTITY_OPTION"
CODE_IDENTITY_FUTURES = "IDENTITY_FUTURES"
CODE_IDENTITY_UNDERLYING = "IDENTITY_UNDERLYING"
CODE_EXPIRY = "EXPIRY"
CODE_DUPLICATE = "DUPLICATE"
CODE_ORDER = "ORDER"
CODE_COVERAGE_GAP = "COVERAGE_GAP"
CODE_SCHEMA = "SCHEMA"
CODE_SYNTHETIC_REQUIRED = "SYNTHETIC_REQUIRED"

MAX_DAILY_GAP_CALENDAR_DAYS = 14   # >14 calendar days without a NO_DATA marker

STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_ACQUIRED = "ACQUIRED"
STATUS_ALREADY_ACQUIRED = "ALREADY_ACQUIRED"
STATUS_REFUSED = "REFUSED"


@dataclass(frozen=True)
class RawProvenance:
    """Provenance of one preserved raw NSE bhavcopy file (no bytes stored here)."""

    source_id: str
    dataset_date: str
    original_filename: str
    file_size_bytes: int
    file_sha256: str
    source_url: str = ""                 # documented retrieval location or "MANUAL_PLACEMENT"
    retrieval_timestamp: str = ""        # RFC 3339 UTC, supplied by the caller
    acquisition_status: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "dataset_date": self.dataset_date,
            "original_filename": self.original_filename,
            "file_size_bytes": self.file_size_bytes,
            "file_sha256": self.file_sha256,
            "source_url": self.source_url,
            "retrieval_timestamp": self.retrieval_timestamp,
            "acquisition_status": self.acquisition_status,
        }


@dataclass(frozen=True)
class Level1Issue:
    """One validation problem with a stable machine-readable code."""

    code: str
    message: str
    index: int | None = None

    def __str__(self) -> str:
        where = "" if self.index is None else f" (row {self.index})"
        return f"[{self.code}]{where} {self.message}"


@dataclass(frozen=True)
class Level1Validation:
    """All issues found for a Level-1 daily dataset plus an overall verdict."""

    num_rows: int
    issues: tuple[Level1Issue, ...] = ()
    per_file: Mapping[str, "Level1Validation"] = field(default_factory=dict)
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
        return STATUS_VALID if self.ok else STATUS_INVALID


# --------------------------------------------------------------------------- #
# Raw file discovery and hashing
# --------------------------------------------------------------------------- #

def raw_byte_sha256(path: Path) -> str:
    """SHA-256 over the raw bytes of a preserved source file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def file_size_bytes(path: Path) -> int:
    return path.stat().st_size


def read_bhavcopy_text(path: Path) -> str:
    """Return the bhavcopy CSV text, transparently reading a CSV or a ZIP that
    contains exactly one CSV member."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError(
                    f"expected exactly one CSV inside {path.name}, found {len(members)}"
                )
            with zf.open(members[0]) as fh:
                data = fh.read()
        text = data.decode("utf-8-sig", errors="replace")
    else:
        text = path.read_text(encoding="utf-8-sig")
    return text


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _normalize_header(name: str) -> str:
    cleaned = "".join(ch for ch in name.upper() if ch.isalnum())
    aliases = {
        "OPENINT": "OPEN_INT",
"CHGINOI": "CHG_IN_OI",
    "SETTLEPR": "SETTLE_PR",
    "STRIKEPR": "STRIKE_PR",
    "EXPIRYDT": "EXPIRY_DT",
        "OPTIONTYP": "OPTION_TYP",
        "OPTTYP": "OPTION_TYP",
        "VALUEINLAKH": "VAL_INLAKH",
        "CONTRACTS": "CONTRACTS",
        "TOTTRDQTY": "CONTRACTS",
    }
    return aliases.get(cleaned, cleaned)


def parse_bhavcopy(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Parse a classic NSE F&O EOD bhavcopy into canonical row dicts.

    Returns ``(rows, issues)``. Column matching is case- and whitespace-
    insensitive; unknown columns are ignored (not an error) but a missing
    required column is recorded as a parse issue so the file is refused.
    """
    text = read_bhavcopy_text(path)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["no header row found"]
    header = {_normalize_header(h): h for h in reader.fieldnames}
    issues: list[str] = []
    for req in REQUIRED_NSE_FIELDS:
        if req not in header:
            issues.append(f"required column missing: {req}")
    if issues:
        return [], issues

    rows: list[dict[str, str]] = []
    for raw in reader:
        if raw is None:
            continue
        row: dict[str, str] = {}
        blank = True
        for req in REQUIRED_NSE_FIELDS:
            value = str(raw.get(header.get(req, "")) or "").strip()
            row[req] = value
            if value:
                blank = False
        if blank:
            continue
        for optional in ("OPTION_TYP", "SETTLE_PR", "VAL_INLAKH", "CHG_IN_OI"):
            src = header.get(optional)
            row[optional] = str(raw.get(src, "") if src else "").strip()
        rows.append(row)
    return rows, []


# --------------------------------------------------------------------------- #
# Normalization to the frozen 12-field layout (daily granularity only)
# --------------------------------------------------------------------------- #

def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _fmt_decimal(value: Decimal) -> str:
    """Render a Decimal without scientific notation and without trailing noise."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_trade_ts(ds: date) -> str:
    """Daily bar timestamp as RFC 3339 UTC at the session end (15:30 IST == 10:00 UTC).

    The minute/second are pinned to a 5-minute boundary (10:00:00Z) and the
    value is deterministic from the trading date alone — matching the frozen
    contract's timestamp *boundary* rule while staying explicitly daily.
    """
    ist_end = datetime.combine(ds, datetime.min.time()) + timedelta(hours=15, minutes=30)
    utc = ist_end - IST_TO_UTC_OFFSET
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_instrument_key(symbol: str, expiry_dt: str, strike: Decimal | None, opt_type: str) -> str:
    """Deterministic Level-1 identity key derived from the bhavcopy row.

    This is a documented *derived* research identity (``NSE_FO|SYM|EXP|STK|OPT``),
    NOT an exchange or Upstox instrument token; it is stable and reproducible so
    duplicate/ordering/coverage checks can run per contract identity.
    """
    sym = symbol.strip().upper() or "UNKNOWN"
    exp = expiry_dt.strip()
    stk = "" if strike is None else _fmt_decimal(strike)
    opt = opt_type.strip().upper()
    return f"NSE_FO|{sym}|{exp}|{stk}|{opt}"


def normalize_rows(
    raw_rows: Sequence[Mapping[str, str]], *, source: str, trade_date: date | None = None
) -> list[dict[str, str]]:
    """Map classic NSE bhavcopy rows onto the frozen 12-field layout.

    ``source`` becomes the ``source`` column. When ``trade_date`` is supplied the
    ``ts`` column is anchored deterministically to the session end (15:30 IST ==
    10:00Z) of that trading date; otherwise the raw ``TIMESTAMP`` string is kept
    for validation to inspect. No market field is invented: blank contracts/
    volumes stay blank (reported by validation); CE/PE/futures identity is
    derived only from the bhavcopy's own OPTION_TYP + STRIKE_PR.
    """
    normalized: list[dict[str, str]] = []
    for raw in raw_rows:
        symbol = str(raw.get("SYMBOL") or "").strip()
        expiry_dt = str(raw.get("EXPIRY_DT") or "").strip()
        opt_type = str(raw.get("OPTION_TYP") or "").strip().upper()
        strike = _to_decimal(raw.get("STRIKE_PR"))

        is_option = opt_type in (NSE_OPTION_TYPE_CE, NSE_OPTION_TYPE_PE) or (
            strike is not None and strike > 0
        )
        if opt_type in (NSE_OPTION_TYPE_CE, NSE_OPTION_TYPE_PE):
            asset = opt_type
        elif is_option:
            asset = opt_type if opt_type in (NSE_OPTION_TYPE_CE, NSE_OPTION_TYPE_PE) else ""
        else:
            asset = "FUTURES"

        ts_raw = str(raw.get("TIMESTAMP") or "").strip()
        ts_value = normalize_trade_ts(trade_date) if trade_date is not None else ts_raw
        normalized.append(
            {
                "ts": ts_value,
                "instrument_key": build_instrument_key(symbol, expiry_dt, strike, opt_type),
                "asset": asset,
                "expiry": expiry_dt,
                "strike": "" if strike is None else _fmt_decimal(strike),
                "open": str(raw.get("OPEN") or "").strip(),
                "high": str(raw.get("HIGH") or "").strip(),
                "low": str(raw.get("LOW") or "").strip(),
                "close": str(raw.get("CLOSE") or "").strip(),
                "volume": str(raw.get("CONTRACTS") or "").strip(),
                "oi": str(raw.get("OPEN_INT") or "").strip(),
                "source": str(source or "").strip(),
            }
        )
    return normalized


# --------------------------------------------------------------------------- #
# Level-1 validation (daily semantics; NO_DATA sentinels supported)
# --------------------------------------------------------------------------- #

def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _parse_ts(value: str) -> tuple[datetime | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        dt: datetime
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
            dt = dt.replace(tzinfo=None)   # normalize to naive UTC representation
        else:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                offset = dt.utcoffset()
                if offset is None or offset != timedelta(0):
                    return None, CODE_TS_TZ
    except ValueError:
        return None, CODE_TS_PARSE
    return dt, None


def _is_no_data_row(raw: Mapping[str, Any]) -> bool:
    """A NO_DATA sentinel row: identity only (no market fields).

    Declares a day of absence for an identity. The row may carry ``ts`` /
    ``instrument_key`` / ``asset`` / ``expiry`` / ``strike`` / ``source``;
    identity rules still apply (an option marker needs its strike/expiry) but
    OHLC/volume checks and the coverage-gap trigger are suppressed.
    """
    identity_only = frozenset({"ts", "instrument_key", "asset", "expiry", "strike", "source"})
    populated = {k for k in OPTION_BAR_FIELDS if _present(raw.get(k))}
    return {"ts", "instrument_key", "asset"} <= populated <= identity_only


def validate_level1_rows(rows: Sequence[Mapping[str, Any]]) -> Level1Validation:
    """Validate a sequence of Level-1 normalized rows (daily semantics)."""
    issues: list[Level1Issue] = []
    seen: set[tuple[str, datetime]] = set()
    last_by_identity: dict[str, tuple[datetime, int]] = {}
    profiles: dict[str, Any] = {"families": {}, "identities": set(), "date_span": None}

    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            issues.append(Level1Issue(CODE_SCHEMA, "row is not a mapping", index))
            continue
        known = set(raw.keys())
        if not known <= set(OPTION_BAR_FIELDS):
            extra = sorted(known - set(OPTION_BAR_FIELDS))
            issues.append(Level1Issue(CODE_SCHEMA, f"unknown field(s): {extra}", index))
            continue

        source = str(raw.get("source") or "").strip()
        if not source:
            issues.append(Level1Issue(CODE_SOURCE_MISSING, "empty source", index))
        if "SYNTHETIC" in source.upper() and not source.startswith(SYNTHETIC_LABEL):
            issues.append(
                Level1Issue(
                    CODE_SYNTHETIC_REQUIRED,
                    f"synthetic source must start with {SYNTHETIC_LABEL}",
                    index,
                )
            )

        required = ["ts", "instrument_key", "asset"]
        if not _is_no_data_row(raw):
            required += ["open", "high", "low", "close"]
        missing = [f for f in required if not _present(raw.get(f))]
        if missing:
            issues.append(Level1Issue(CODE_SCHEMA, f"required field(s) missing: {missing}", index))

        ts_value = str(raw.get("ts") or "").strip()
        ts_norm, ts_code = _parse_ts(ts_value)
        if ts_code:
            issues.append(Level1Issue(ts_code, f"invalid timestamp {ts_value!r}", index))
            continue
        if ts_norm is None:
            issues.append(Level1Issue(CODE_TS_PARSE, f"empty ts {ts_value!r}", index))
            continue
        if (ts_norm.minute % 5 != 0) or ts_norm.second != 0 or ts_norm.microsecond != 0:
            issues.append(
                Level1Issue(
                    CODE_TS_PARSE,
                    f"{ts_norm.isoformat()} is not on a 5-minute boundary (daily anchor)",
                    index,
                )
            )

        key = str(raw.get("instrument_key", "")).strip()
        if not key:
            issues.append(Level1Issue(CODE_SCHEMA, "empty instrument_key", index))
            continue

        asset = str(raw.get("asset") or "").strip()
        _profile(profiles, key, asset, ts_norm)

        strike = _to_decimal(raw.get("strike"))
        expiry = str(raw.get("expiry") or "").strip()
        if asset in (NSE_OPTION_TYPE_CE, NSE_OPTION_TYPE_PE, "CE", "PE"):
            if strike is None or strike <= 0:
                issues.append(Level1Issue(CODE_IDENTITY_OPTION, f"option row {key} has no positive strike", index))
            if not expiry:
                issues.append(Level1Issue(CODE_IDENTITY_OPTION, f"option row {key} has no expiry", index))
            if ts_norm.date() and expiry:
                exp_date = _parse_iso_date(expiry)
                if exp_date is None:
                    issues.append(Level1Issue(CODE_EXPIRY, f"unparseable expiry {expiry!r}", index))
                elif exp_date < ts_norm.date():
                    issues.append(Level1Issue(CODE_EXPIRY, f"expiry {expiry} before data date {ts_norm.date()}", index))
        elif asset == "FUTURES":
            if strike is not None and strike > 0:
                issues.append(Level1Issue(CODE_IDENTITY_FUTURES, f"futures row {key} carries a strike", index))
        else:
            issues.append(
                Level1Issue(CODE_IDENTITY_UNDERLYING, f"asset {asset!r} not CE/PE/FUTURES", index)
            )

        dup = (key, ts_norm)
        if dup in seen:
            issues.append(Level1Issue(CODE_DUPLICATE, f"duplicate (identity, ts) {key} {ts_norm.isoformat()}", index))
        seen.add(dup)
        prior = last_by_identity.get(key)
        if prior is not None:
            prev_ts, prev_index = prior
            if ts_norm < prev_ts:
                issues.append(Level1Issue(CODE_ORDER, f"out-of-order timestamp for {key}", index))
            elif ts_norm > prev_ts:
                delta = (ts_norm.date() - prev_ts.date()).days
                if delta > MAX_DAILY_GAP_CALENDAR_DAYS and not _is_no_data_row(raw):
                    issues.append(
                        Level1Issue(
                            CODE_COVERAGE_GAP,
                            f"daily coverage gap of {delta} calendar days for {key} "
                            f"between {prev_ts.date()} and {ts_norm.date()} without a NO_DATA marker",
                            index,
                        )
                    )
        last_by_identity[key] = (ts_norm, index)

        if _is_no_data_row(raw):
            continue

        if _is_blank_optional(asset, raw):
            # futures/rows where bhavcopy blanks legitimate columns (e.g. zero
            # volume) are validated but never repaired.
            pass
        ohlc = {f: _to_decimal(raw.get(f)) for f in ("open", "high", "low", "close")}
        if any(v is None for v in ohlc.values()):
            missing = [f for f, v in ohlc.items() if v is None]
            issues.append(Level1Issue(CODE_NUMERIC, f"non-numeric OHLC in {missing} for {key}", index))
            continue
        o, h, l, c = ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]
        for name, value in (("open", o), ("high", h), ("low", l), ("close", c)):
            if value < 0:
                issues.append(Level1Issue(CODE_NEGATIVE, f"{name} is negative for {key}", index))
        if h < max(o, c):
            issues.append(Level1Issue(CODE_OHLC, f"high < max(open, close) for {key}", index))
        if l > min(o, c):
            issues.append(Level1Issue(CODE_OHLC, f"low > min(open, close) for {key}", index))
        if h < l:
            issues.append(Level1Issue(CODE_OHLC, f"high < low for {key}", index))

        volume = _to_decimal(raw.get("volume"))
        oi = _to_decimal(raw.get("oi"))
        if volume is not None and volume < 0:
            issues.append(Level1Issue(CODE_NEGATIVE, f"negative volume for {key}", index))
        if oi is not None and oi < 0:
            issues.append(Level1Issue(CODE_NEGATIVE, f"negative oi for {key}", index))

    return Level1Validation(
        num_rows=len(rows),
        issues=tuple(issues),
        profiles=dict(profiles),
    )


def _is_blank_optional(asset: str, raw: Mapping[str, Any]) -> bool:
    # an identity for which bhavcopy legitimately may leave quantity / OI blank
    # is still validated for negative values when present; nothing is repaired.
    return asset in ("FUTURES", "CE", "PE")


def _parse_iso_date(value: str) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _profile(profiles: dict[str, Any], key: str, asset: str, ts: datetime) -> None:
    families = profiles["families"]
    families[asset] = families.get(asset, 0) + 1
    profiles["identities"].add(key)
    if profiles["date_span"] is None:
        profiles["date_span"] = [ts.date().isoformat(), ts.date().isoformat()]
    else:
        span = profiles["date_span"]
        if ts.date().isoformat() < span[0]:
            span[0] = ts.date().isoformat()
        if ts.date().isoformat() > span[1]:
            span[1] = ts.date().isoformat()


# --------------------------------------------------------------------------- #
# Fingerprinting, manifest and report
# --------------------------------------------------------------------------- #

def write_normalized_csv(rows: Sequence[Mapping[str, Any]], path: Path) -> str:
    """Write the normalized 12-field file (LF newlines) and return its hash."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(OPTION_BAR_FIELDS), lineterminator="\n")
        writer.writeheader()
        for raw in rows:
            writer.writerow({f: ("" if raw.get(f) is None else str(raw.get(f))) for f in OPTION_BAR_FIELDS})
    return file_sha256_lf(path)


def build_manifest(
    *,
    level: str = LEVEL_LABEL,
    schema_version: str = SCHEMA_VERSION,
    source: str = SOURCE_ID,
    synthetic: bool = False,
    coverage: Mapping[str, Any] | None = None,
    files: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "level": level,
        "source": source,
        "synthetic": synthetic,
        "coverage": dict(coverage) if coverage else {},
        "files": dict(files) if files else {},
    }
    if synthetic:
        manifest["source"] = SYNTHETIC_LABEL
    return manifest


def merge_manifest(existing: Mapping[str, Any] | None, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Add one file entry to a manifest without silent overwrites.

    Raises ``ValueError`` (REFUSED) when the same filename already exists with a
    different SHA-256; returns an unchanged-document marker (ALREADY_ACQUIRED)
    when the bytes are identical.
    """
    base = dict(existing) if existing else {
        "schema_version": entry.get("schema_version", SCHEMA_VERSION),
        "level": entry.get("level", LEVEL_LABEL),
        "source": entry.get("source", SOURCE_ID),
        "synthetic": bool(entry.get("synthetic", False)),
        "coverage": {},
        "files": {},
    }
    files = dict(base.get("files", {}))
    name = entry.get("filename", "")
    meta = entry.get("file", {})
    if name:
        existing_meta = files.get(name)
        if existing_meta is not None:
            existing_hash = existing_meta.get("sha256") if isinstance(existing_meta, Mapping) else None
            new_hash = meta.get("sha256")
            if new_hash is not None and existing_hash == new_hash:
                return base          # identical re-acquisition: no change
            raise ValueError(
                f"REFUSED: {name} already recorded with a different sha256 "
                f"({existing_hash} != {new_hash}); no silent overwrite"
            )
        files[name] = meta
    base["files"] = files
    if entry.get("coverage"):
        base["coverage"] = dict(entry["coverage"])
    return base


def render_report(
    *,
    manifesto: Mapping[str, Any],
    validation: Level1Validation,
    provenance: RawProvenance,
    normalized_path: str = "",
    client: str = "predicted",
) -> str:
    """Human-readable DATA-005 report for one acquired dataset file."""
    is_synthetic = bool(manifesto.get("synthetic", False))
    source = str(manifesto.get("source") or "")
    banner = (
        "!!! SYNTHETIC FIXTURE — NOT OBSERVED MARKET DATA. DO NOT consume as "
        "market evidence."
        if is_synthetic
        else "!!! LEVEL_1_DAILY_RESEARCH_ONLY — daily EOD data. NOT sufficient for "
        "intraday 5-minute options execution simulation; contains NO intraday "
        "quotes and NO historical bid/ask."
    )
    lines = [
        "# DATA-005 — NSE F&O EOD Level-1 Daily Research Acquisition",
        "",
        f"- Level: `{LEVEL_LABEL}`",
        f"- Schema version: `{manifesto.get('schema_version')}` (frozen intraday contract: `{CONTRACT_SCHEMA_VERSION}`)",
        f"- Source: `{source}`",
        f"- Banner: {banner}",
        "",
        "## Provenance",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Source ID | `{provenance.source_id}` |",
        f"| Dataset date | `{provenance.dataset_date}` |",
        f"| Original filename | `{provenance.original_filename}` |",
        f"| File size (bytes) | {provenance.file_size_bytes} |",
        f"| Raw SHA-256 | `{provenance.file_sha256}` |",
        f"| Source URL / location | `{provenance.source_url or 'MANUAL_PLACEMENT'}` |",
        f"| Retrieval timestamp | `{provenance.retrieval_timestamp or 'not supplied'}` |",
        f"| Acquisition status | `{provenance.acquisition_status or 'not supplied'}` |",
        "",
        "## Normalized file",
        "",
        f"- Path: `{normalized_path or 'not written'}`",
        "",
        "## Validation",
        "",
        f"- Rows: {validation.num_rows}",
        f"- Status: `{validation.status()}`",
        f"- Issues: {len(validation.issues)}",
    ]
    for issue in validation.issues:
        lines.append(f"  - {issue}")
    profiles = validation.profiles or {}
    lines += [
        "",
        "## Coverage profile",
        "",
        f"- Date span: {profiles.get('date_span')}",
        f"- Identities: {len(profiles.get('identities') or set())}",
        f"- Family counts: {profiles.get('families')}",
        "",
        "## Contract-compliance note",
        "",
        f"- `{manifesto.get('schema_version')}` is a **daily** Level-1 research "
        "schema; it is deliberately distinct from the frozen intraday "
        f"`{CONTRACT_SCHEMA_VERSION}` so daily output is never consumed as an "
        "intraday chain. No bid/ask, intraday quote or missing observation is "
        "fabricated.",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "SOURCE_ID",
    "SCHEMA_VERSION",
    "CONTRACT_SCHEMA_VERSION",
    "LEVEL_LABEL",
    "STATUS_VALID",
    "STATUS_INVALID",
    "STATUS_ACQUIRED",
    "STATUS_ALREADY_ACQUIRED",
    "STATUS_REFUSED",
    "SYNTHETIC_LABEL",
    "RawProvenance",
    "Level1Issue",
    "Level1Validation",
    "raw_byte_sha256",
    "file_size_bytes",
    "read_bhavcopy_text",
    "parse_bhavcopy",
    "normalize_trade_ts",
    "build_instrument_key",
    "normalize_rows",
    "validate_level1_rows",
    "write_normalized_csv",
    "build_manifest",
    "merge_manifest",
    "render_report",
]