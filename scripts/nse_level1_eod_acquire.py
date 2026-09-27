"""DATA-005 runner — acquire the official NSE F&O EOD bhavcopy as Level-1 daily data.

Pipeline (deterministic, credential-free, no orders, no posts):
  1. source discovery: a locally placed CSV/ZIP bhavcopy (``--source-path``) or,
     when ``--fetch`` is passed, a read-only GET of the documented NSE URL for
     the requested trading date (market-data GET only).
  2. parse + normalize to the frozen 12-field layout at *daily* granularity
     (``level1_nse.normalize_rows``).
  3. validate with Level-1 daily semantics (``validate_level1_rows``).
  4. preserve the raw bytes untouched (no silent overwrite; identical bytes are
     ALREADY_ACQUIRED, differing bytes are REFUSED).
  5. write the normalized CSV (LF, SHA-256 fingerprinted), merge the manifest,
     and render human + machine reports under ``datasets/`` and ``reports/``.

Data artifacts live in git-ignored directories only; nothing is auto-committed.

A blocked or failed network fetch is recorded honestly as ``ACCESS_BLOCKED`` in
the report (status code 3) — it is never replaced by synthetic material.

Output is labeled ``LEVEL_1_DAILY_RESEARCH_ONLY``; synthetic fixtures carry the
``SYNTHETIC_FIXTURE`` source marker and are refused as market evidence.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.options_research import level1_nse as m  # noqa: E402

# Documented, official NSE archive location pattern for F&O EOD bhavcopy.
NSE_ARCHIVE_BASE = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{date}"

DOWNLOADED = "DOWNLOADED"
NOT_FOUND = "NOT_FOUND"
ACCESS_BLOCKED = "ACCESS_BLOCKED"
DOWNLOAD_ERROR = "DOWNLOAD_ERROR"


def _build_nse_url(trade_date: date) -> str:
    stamp = trade_date.strftime("%Y/%b/%d").upper()
    return f"{NSE_ARCHIVE_BASE.format(date=stamp)}/fo{date_str(trade_date)}bhav.csv.zip"


def date_str(d: date) -> str:
    return d.strftime("%d%m%y")


def classify_fetch(status_code: int, exc: BaseException | None = None) -> tuple[str, int]:
    """Honest classification of an opt-in market-data GET.

    HTTP 404 names a genuinely-absent archive (``NOT_FOUND``); 401/403 and
    challenge pages are ``ACCESS_BLOCKED`` (never retried, never fabricated
    over); transient network/server failures are ``DOWNLOAD_ERROR`` so a later
    run may legitimately retry. No status is ever replaced by synthetic data.
    """
    if status_code == 404:
        return NOT_FOUND, status_code
    if status_code in (401, 403):
        return ACCESS_BLOCKED, status_code
    if status_code in (429, 408) or status_code >= 500 or status_code == 0:
        return DOWNLOAD_ERROR, status_code
    return ACCESS_BLOCKED, status_code


def _download(url: str, dest: Path) -> tuple[str, int]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fno-ai-paper-trading-data-acquisition/1.0 (research, read-only)",
            "Accept-Encoding": "identity",
            "Referer": "https://www.nseindia.com/",
        },
    )
    status = ACCESS_BLOCKED
    code = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status or 200
            if status != 200:
                return classify_fetch(status)
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "html" in content_type:
                # a login/interstitial page, never the bhavcopy bytes
                return ACCESS_BLOCKED, 403
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        return classify_fetch(getattr(exc, "code", 0), exc)
    except (urllib.error.URLError, TimeoutError) as exc:
        return classify_fetch(0, exc)
    dest.write_bytes(payload)
    return DOWNLOADED, status


def _read_or_create_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire official NSE F&O EOD bhavcopy as Level-1 daily research data.",
        epilog=(
            "READ-ONLY: no orders, no posts. Network use requires --fetch and is a "
            "single market-data GET against the documented NSE archive URL."
        ),
    )
    parser.add_argument("--source-path", help="local NSE bhavcopy CSV or ZIP (recommended)")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="attempt a read-only GET of the documented NSE archive URL (opt-in)",
    )
    parser.add_argument("--trade-date", help="trading date YYYY-MM-DD (defaults to file TIMESTAMP)")
    parser.add_argument("--source-url", default="", help="documented retrieval URL (provenance)")
    parser.add_argument("--retrieval-timestamp", default="", help="RFC 3339 UTC retrieval timestamp")
    parser.add_argument(
        "--dataset-root",
        default=str(REPO_ROOT / "datasets" / "options" / "level1_daily"),
        help="root for raw + normalized Level-1 output (git-ignored)",
    )
    parser.add_argument(
        "--report-root",
        default=str(REPO_ROOT / "reports" / "level1_daily"),
        help="root for JSON/MD reports (git-ignored)",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="flag the input as a fixture; output source becomes SYNTHETIC_FIXTURE",
    )
    parser.add_argument("--no-save", action="store_true", help="validate only, write nothing")
    args = parser.parse_args(argv)

    dataset_root = Path(args.dataset_root)
    report_root = Path(args.report_root)

    raw_path = Path(args.source_path).resolve() if args.source_path else None
    if raw_path is None and not args.fetch:
        parser.error("provide --source-path (or pass --fetch for the documented NSE URL)")

    retrieval_ts = args.retrieval_timestamp or datetime.now().astimezone().isoformat()
    source_url = args.source_url
    download_status = ""

    # 1. obtain the raw bytes (local file or opt-in network GET).
    if raw_path is not None:
        if not raw_path.exists():
            print(f"ERROR: source file not found: {raw_path}")
            return 2
        raw_bytes = raw_path.read_bytes()
        original_filename = raw_path.name
    else:
        if not args.trade_date:
            parser.error("--fetch requires --trade-date YYYY-MM-DD")
        trade_date = date.fromisoformat(args.trade_date)
        source_url = source_url or _build_nse_url(trade_date)
        dest = dataset_root / "raw" / f"fo{date_str(trade_date)}bhav.csv.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        download_status, code = _download(source_url, dest)
        if download_status != DOWNLOADED:
            status_label = f"{download_status} (HTTP/code {code})"
            report = m.render_report(
                manifesto=m.build_manifest(
                    level=m.LEVEL_LABEL, source=m.SOURCE_ID, coverage={}, files={}
                ),
                validation=m.Level1Validation(num_rows=0, issues=(), profiles={}),
                provenance=m.RawProvenance(
                    source_id=m.SOURCE_ID,
                    dataset_date=args.trade_date,
                    original_filename=dest.name,
                    file_size_bytes=0,
                    file_sha256="",
                    source_url=source_url,
                    retrieval_timestamp=retrieval_ts,
                    acquisition_status=status_label,
                ),
                normalized_path="",
            )
            out_dir = (
                report_root / "errors"
                if download_status == DOWNLOAD_ERROR
                else report_root / "absent"
                if download_status == NOT_FOUND
                else report_root / "blocked"
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"nse_fo_eod_{args.trade_date}.md").write_text(
                report + "\n", encoding="utf-8"
            )
            print(
                f"{download_status} for {args.trade_date} (HTTP/code {code}); "
                "no data fabricated."
            )
            return 3 if download_status != DOWNLOAD_ERROR else 4
        raw_path = dest
        raw_bytes = dest.read_bytes()
        original_filename = dest.name
        print(f"DOWNLOADED {source_url} -> {dest} ({len(raw_bytes)} bytes)")

    # 2. preserve the raw bytes (no silent overwrite).
    raw_hash = m.raw_byte_sha256(raw_path)
    if not args.trade_date:
        parser.error("--trade-date YYYY-MM-DD is required for acquisition")
    try:
        trade_date = date.fromisoformat(args.trade_date)
    except ValueError:
        parser.error(f"--trade-date must be an ISO date, got {args.trade_date!r}")
    dataset_date = trade_date.isoformat()

    raw_dir = dataset_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest_raw = raw_dir / original_filename
    if dest_raw.exists():
        existing_hash = m.raw_byte_sha256(dest_raw)
        if existing_hash != raw_hash:
            print(f"REFUSED: {dest_raw.name} already stored with a different sha256; not overwritten.")
            return 3
        print(f"ALREADY_ACQUIRED: {dest_raw.name} raw bytes already present (sha256 {raw_hash[:12]}).")
    else:
        shutil.copyfile(raw_path, dest_raw)
        print(f"raw preserved -> {dest_raw} ({raw_hash[:12]})")

    # 3. parse -> normalize -> validate.
    parse_rows, parse_issues = m.parse_bhavcopy(raw_path)
    if parse_issues:
        for issue in parse_issues:
            print(f"REFUSED: {issue}")
        return 3
    source = m.SYNTHETIC_LABEL if args.synthetic else m.SOURCE_ID
    normalized = m.normalize_rows(parse_rows, source=source, trade_date=trade_date)
    validation = m.validate_level1_rows(normalized)
    for issue in validation.issues:
        print(f"  {issue}")

    if args.no_save:
        print(f"VALIDATION status: {validation.status()} ({len(validation.issues)} issues)")
        return 0 if validation.ok else 1

    # 4. fields recorded before writing: standard ones.
    profiles = validation.profiles or {}
    date_span = profiles.get("date_span") or [dataset_date, dataset_date]
    normalized_name = f"nse_fo_eod_{dataset_date}_level1_daily.csv"
    normalized_path = dataset_root / "normalized" / normalized_name
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    fprint = m.write_normalized_csv(normalized, normalized_path)

    cov_dict = {}
    coverage_profile = {"start": date_span[0], "end": date_span[1]}
    if profiles.get("identities") is not None:
        coverage_profile["identities"] = sorted(profiles["identities"])
    cov_dict[dataset_date] = coverage_profile

    entry = {
        "filename": normalized_name,
        "file": {
            "sha256": fprint,
            "rows": validation.num_rows,
            "validation_status": validation.status(),
            "issue_counts": validation.code_counts,
            "raw": {
                "original_filename": original_filename,
                "sha256": raw_hash,
                "size_bytes": m.file_size_bytes(dest_raw),
                "source_url": source_url,
                "retrieval_timestamp": retrieval_ts,
                "acquisition_status": download_status or "MANUAL_PLACEMENT",
            },
            "synthetic": args.synthetic,
        },
        "synthetic": args.synthetic,
        "schema_version": m.SCHEMA_VERSION,
        "level": m.LEVEL_LABEL,
        "source": source,
        "coverage": {"start": date_span[0], "end": date_span[1]},
    }

    manifest_path = dataset_root / "manifest.json"
    merged = m.merge_manifest(_read_or_create_manifest(manifest_path), entry)

    provenance = m.RawProvenance(
        source_id=m.SOURCE_ID,
        dataset_date=dataset_date,
        original_filename=original_filename,
        file_size_bytes=m.file_size_bytes(dest_raw),
        file_sha256=raw_hash,
        source_url=source_url,
        retrieval_timestamp=retrieval_ts,
        acquisition_status=download_status or "MANUAL_PLACEMENT",
    )
    manifesto = m.build_manifest(
        level=m.LEVEL_LABEL,
        schema_version=m.SCHEMA_VERSION,
        source=source,
        synthetic=args.synthetic,
        coverage=merged.get("coverage", cov_dict),
        files=merged.get("files", {}),
    )
    report_md = m.render_report(
        manifesto=manifesto,
        validation=validation,
        provenance=provenance,
        normalized_path=str(normalized_path),
    )

    # 5. persist manifest + reports (git-ignored).
    if not args.no_save:
        manifest_path.write_text(json.dumps(manifesto, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report_root.mkdir(parents=True, exist_ok=True)
        (report_root / f"nse_fo_eod_{dataset_date}_level1_daily.md").write_text(report_md, encoding="utf-8")
        (report_root / f"nse_fo_eod_{dataset_date}_level1_daily.json").write_text(
            json.dumps(
                {
                    "level": m.LEVEL_LABEL,
                    "schema_version": m.SCHEMA_VERSION,
                    "status": validation.status(),
                    "num_issues": len(validation.issues),
                    "scan": True,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    print(f"normalized -> {normalized_path} (sha256 {fprint[:12]})")
    print(f"VALIDATION status: {validation.status()} ({len(validation.issues)} issues)")
    print(f"manifest -> {manifest_path}")
    print(f"report  -> {report_root}")
    return 0 if validation.ok else 1


if __name__ == "__main__":
    sys.exit(main())