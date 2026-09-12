"""Acquire real historical OHLCV data (Upstox, READ-ONLY) for model-performance work.

Fetches normalized OHLCV bars from the read-only Upstox V3 ``/v3/historical-candle``
endpoint in small, resumable time windows and persists them via the standard
dataset store (CSV + ``.meta.json`` with a SHA-256 ``data_hash``) into the
git-ignored ``datasets/`` directory.

Window size is deliberately small (default 25 calendar days) so the acquisition
robustly navigates provider boundary artifacts (some early-2022 5m ranges return
"Invalid date range" for windows around the 29–30 day mark).

SAFETY: this script never places an order, never posts data, and never mutates a
remote resource. The only HTTP method ever used is ``GET``. Like the other
acquisition scripts it is opt-in: it exits non-zero unless ``UPSTOX_ACCESS_TOKEN``
is set.

Usage::

    python scripts/acquire_model_performance_data.py --interval 5m --start 2021-12-26
    python scripts/acquire_model_performance_data.py --interval 1d --start 2005-01-01

Progress is recorded in a JSON checkpoint (default ``datasets/acquire_progress.json``)
so interrupted runs resume without re-fetching completed windows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fno_ai_paper_trading.data.dataset_store import save_dataset  # noqa: E402
from fno_ai_paper_trading.data.errors import MarketDataError  # noqa: E402
from fno_ai_paper_trading.data.instrument_registry import get_research_instrument  # noqa: E402
from fno_ai_paper_trading.data.upstox_provider import (  # noqa: E402
    UpstoxHistoricalDataProvider,
)
from fno_ai_paper_trading.data.validation import validate_bars  # noqa: E402


def date_windows(start: datetime, end: datetime, window_days: int) -> list[tuple[datetime, datetime]]:
    """Contiguous, non-overlapping ``[from, to]`` windows covering ``[start, end]``."""
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        to = min(cursor + timedelta(days=window_days), end)
        windows.append((cursor, to))
        cursor = to + timedelta(days=1)
    return windows


def load_progress(path: Path) -> dict[str, str]:
    if path.is_file():
        try:
            return {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (ValueError, OSError):
            return {}
    return {}


def save_progress(path: Path, data: dict[str, str]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire real Upstox historical data (read-only).")
    parser.add_argument("--interval", default="5m", help="canonical interval token (5m, 1h, 1d)")
    parser.add_argument("--start", required=True, help="inclusive start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="inclusive end date YYYY-MM-DD (default: today)")
    parser.add_argument("--window-days", type=int, default=25, help="calendar days per request window")
    parser.add_argument("--sleep", type=float, default=0.5, help="seconds between request windows")
    parser.add_argument("--outdir", default="datasets", help="directory for the saved dataset")
    parser.add_argument("--name", default=None, help="dataset name override (default: auto)")
    parser.add_argument("--token", default="", help="Upstox access token (default: env var)")
    parser.add_argument("--progress", default="datasets/acquire_progress.json", help="checkpoint path")
    args = parser.parse_args(argv)

    load_dotenv()
    token = (args.token or os.getenv("UPSTOX_ACCESS_TOKEN", "") or "").strip()
    if not token:
        parser.error("no Upstox access token; set UPSTOX_ACCESS_TOKEN (or pass --token)")

    if args.window_days < 1:
        parser.error("--window-days must be >= 1")

    instrument = get_research_instrument("NIFTY 50")
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end) if args.end else datetime.now()
    if end < start:
        parser.error("--end must not be before --start")

    provider = UpstoxHistoricalDataProvider(access_token=token)
    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = Path(args.progress)
    progress = load_progress(progress_path)
    key_prefix = f"{args.interval}:{args.start}:"

    windows = date_windows(start, end, args.window_days)
    bars: list = []
    failures: list[dict[str, str]] = []
    for index, (from_date, to_date) in enumerate(windows, start=1):
        key = f"{key_prefix}{from_date.date().isoformat()}_{to_date.date().isoformat()}"
        if progress.get(key) in ("ok", "empty"):
            continue
        attempt = 0
        while True:
            attempt += 1
            try:
                window_bars = provider.get_historical_ohlcv(
                    instrument, args.interval, from_date, to_date
                )
                if not window_bars:
                    progress[key] = "empty"
                    save_progress(progress_path, progress)
                    print(f"[{index}/{len(windows)}] {from_date.date()}..{to_date.date()}: empty")
                    break
                bars.extend(window_bars)
                progress[key] = "ok"
                save_progress(progress_path, progress)
                print(
                    f"[{index}/{len(windows)}] {from_date.date()}..{to_date.date()}: "
                    f"{len(window_bars)} bars"
                )
                break
            except (MarketDataError, OSError) as exc:  # noqa: BLE001
                if attempt >= 4:
                    progress[key] = "failed"
                    save_progress(progress_path, progress)
                    failures.append(
                        {
                            "from": from_date.date().isoformat(),
                            "to": to_date.date().isoformat(),
                            "error": str(exc)[:240],
                        }
                    )
                    print(
                        f"[{index}/{len(windows)}] {from_date.date()}..{to_date.date()}: "
                        f"FAILED after {attempt} attempts ({str(exc)[:90]})"
                    )
                    break
                wait = 2.0 * attempt
                print(
                    f"[{index}/{len(windows)}] {from_date.date()}..{to_date.date()}: "
                    f"retry {attempt} in {wait:.0f}s ({str(exc)[:80]})"
                )
                time.sleep(wait)
        time.sleep(args.sleep)

    total = len(bars)
    if total == 0:
        print(f"no bars acquired for {args.interval}; nothing saved", file=sys.stderr)
        print(json.dumps({"interval": args.interval, "num_bars": 0, "failures": failures}, indent=2))
        return 2 if failures else 1

    report = validate_bars(bars, allow_empty=False)
    if not report.ok:
        print(
            f"merged series FAILED validation ({len(report.errors)} error(s)); refusing to save",
            file=sys.stderr,
        )
        for issue in report.errors[:10]:
            print(str(issue), file=sys.stderr)
        print(json.dumps({"interval": args.interval, "num_bars": total, "failures": failures}, indent=2))
        return 2

    saved = save_dataset(
        bars,
        instrument=instrument,
        provider="upstox",
        interval=args.interval,
        directory=str(out_dir),
        name=args.name,
    )
    summary = {
        "interval": args.interval,
        "start_date": saved.metadata["start_date"],
        "end_date": saved.metadata["end_date"],
        "num_bars": len(bars),
        "data_hash": saved.metadata["data_hash"],
        "path": str(saved.path),
        "windows_attempted": len(windows),
        "windows_failed": len(failures),
        "failures": failures,
        "validation": {
            "ok": report.ok,
            "num_errors": len(report.errors),
            "num_warnings": len(report.warnings),
        },
    }
    print(
        f"acquired {saved.metadata['start_date']}..{saved.metadata['end_date']} "
        f"({len(bars)} {args.interval} bars); hash {summary['data_hash'][:12]}"
    )
    if failures:
        print(f"NOTE: {len(failures)} window(s) failed and were excluded from the saved dataset")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())