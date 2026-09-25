"""Algorithm Discovery & Competition — one research cycle.

Flow: fresh Upstox fetch (validated, provenance recorded) -> walk-forward window
(train/validation enforced, OOS protected) -> deck replay (control + six
families) -> full metrics -> bounded robustness grid + cost sensitivity scan ->
transparent competition score / ranking -> promotion gate wiring -> failure
pattern learning -> 19-section report (md + json + html + history).

SAFETY
------
* READ-ONLY Upstox data fetch only; no orders, no live trading, no real tokens.
* ALGO READY stays NO unless real promotion-grade evidence passes; live_trading
  is always False. No commit/push.
* If the API is unreachable the run reports DATA FETCH BLOCKED and only then may
  fall back to the cached dataset, explicitly marked STALE. It never silently
  reuses stale data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from fno_ai_paper_trading.data.dataset_store import load_dataset, save_dataset  # noqa: E402
from fno_ai_paper_trading.data.instrument_registry import (  # noqa: E402
    get_research_instrument,
    instrument_from_upstox_key,
)
from fno_ai_paper_trading.data.upstox_provider import UpstoxHistoricalDataProvider  # noqa: E402
from fno_ai_paper_trading.data.validation import (  # noqa: E402
    format_report,
    validate_bars,
    validation_to_dict,
)
from fno_ai_paper_trading.discovery import catalog as cat  # noqa: E402
from fno_ai_paper_trading.discovery import report as report_mod  # noqa: E402
from fno_ai_paper_trading.discovery.backtest import (  # noqa: E402
    analyze,
    replay_candidate,
)
from fno_ai_paper_trading.discovery.competition import (  # noqa: E402
    promotion_verdict,
    rank,
    score_candidate,
)
from fno_ai_paper_trading.discovery.learning import analyze_failures  # noqa: E402
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig  # noqa: E402

DEFAULT_INSTRUMENT = "NIFTY 50"
DEFAULT_INTERVAL = "5m"
COST_MULTIPLIERS = (1, 3, 5)
SEGMENTS = 4


def _cache_meta(path: Path) -> dict[str, Any]:
    meta_path = path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _pick_cached(outdir: Path, interval: str, train_start: date, val_end: date) -> Path | None:
    """Pick the cached dataset that best overlaps the research window (or None).

    A cached file is only eligible if its recorded range actually overlaps
    [train_start, val_end]; anything that starts after the validation end is
    ignored (it would be run-of-2026 data — that is post-OOS and must never be
    used for research).
    """
    best: Path | None = None
    best_overlap = -1
    for path in sorted(outdir.glob("*.csv")):
        meta = _cache_meta(path)
        if meta.get("interval") not in (None, interval):
            continue
        start_s = meta.get("start_date")
        end_s = meta.get("end_date")
        if not start_s:
            continue
        try:
            start = date.fromisoformat(start_s)
            end = date.fromisoformat(end_s) if end_s else val_end
        except ValueError:
            continue
        if start > val_end or end < train_start:
            continue
        overlap = min(val_end, end).toordinal() - max(train_start, start).toordinal()
        if overlap > best_overlap:
            best_overlap = overlap
            best = path
    return best


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_bars(bars: list) -> list:
    """Sort by timestamp and drop duplicate timestamps (fuses bisect/merge edges)."""
    ordered = sorted(bars, key=lambda b: b.timestamp)
    out: list = []
    seen = set()
    for b in ordered:
        if b.timestamp in seen:
            continue
        seen.add(b.timestamp)
        out.append(b)
    return out


def _resolve_instrument(literal: str):
    if "|" in (literal or ""):
        return instrument_from_upstox_key(literal)
    return get_research_instrument(literal)


def _bisect_fetch(provider, instrument, interval, w0: date, w1: date, depth: int = 0):
    """Fetch dates [w0, w1], bisecting on Upstox 400 'Invalid date range' oddities.

    Upstox intermittently rejects some 30-day 5m windows (observed mid-Mar 2025)
    with HTTP 400 while every sub-day range inside them fetches fine. We bisect
    such windows and merge only the pieces the API actually serves, recording any
    gap so coverage stays transparent. Provider behaviour is not modified.
    """
    from fno_ai_paper_trading.data.errors import MarketDataError  # local import

    if depth > 7:
        raise MarketDataError(f"bisect fetch exhausted at {w0}..{w1}")
    try:
        return provider.get_historical_ohlcv(
            instrument, interval,
            datetime.combine(w0, datetime.min.time()),
            datetime.combine(w1, datetime.min.time()),
        )
    except MarketDataError as exc:
        text = str(exc).lower()
        if "date range" not in text and "invalid date" not in text:
            raise
        if (w1 - w0).days <= 1:
            raise
        mid = w0 + (w1 - w0) / 2
        mid = w0 + timedelta(days=int((w1 - w0).days / 2))
        left = _bisect_fetch(provider, instrument, interval, w0, mid, depth + 1)
        right = _bisect_fetch(provider, instrument, interval, mid + timedelta(days=1), w1, depth + 1)
        bars = sorted(left + right, key=lambda b: b.timestamp)
        deduped = []
        seen = set()
        for b in bars:
            key = b.timestamp
            if key in seen:
                continue
            seen.add(key)
            deduped.append(b)
        return deduped


def fetch_fresh(instrument, interval: str, r_from: datetime, r_to: datetime, token: str,
                outdir: Path, name: str) -> tuple[list, dict[str, Any]]:
    """Fresh Upstox fetch -> validate -> persist. Returns (bars, provenance)."""
    provenance: dict[str, Any] = {
        "provider": "upstox",
        "instrument": instrument.symbol,
        "instrument_key": f"{instrument.instrument_type.value}|{instrument.symbol}".replace("index|", "INDEX|"),
        "interval": interval,
        "requested_from": r_from.date().isoformat(),
        "requested_to": r_to.date().isoformat(),
        "retrieved_at": _utcnow(),
        "freshness": "FRESH",
        "blocked": None,
    }
    bars: list = []
    partial_failures: list[str] = []
    try:
        provider = UpstoxHistoricalDataProvider(access_token=token)
        # Fetch in <=30-day windows (Upstox per-request cap) and bisect any window
        # the API refuses with a spurious 'Invalid date range' (see _bisect_fetch).
        from fno_ai_paper_trading.data.errors import MarketDataError  # local import

        cursor = r_from.date()
        r_to_date = r_to.date()
        while cursor <= r_to_date:
            stop = min(r_to_date, cursor + timedelta(days=30))
            try:
                bars.extend(provider.get_historical_ohlcv(
                    instrument, interval,
                    datetime.combine(cursor, datetime.min.time()),
                    datetime.combine(stop, datetime.min.time()),
                ))
            except MarketDataError as exc:
                text = str(exc).lower()
                if "date range" not in text and "invalid date" not in text:
                    raise
                try:
                    bars.extend(_bisect_fetch(provider, instrument, interval, cursor, stop, depth=1))
                except MarketDataError as inner:
                    partial_failures.append(f"{cursor}..{stop} not servable ({inner})")
            cursor = stop + timedelta(days=1)
        bars = _dedupe_bars(bars)
        report = validate_bars(bars, allow_empty=False)
    except Exception as exc:  # noqa: BLE001 - report every blocker explicitly
        provenance["blocked"] = f"{type(exc).__name__}: {exc}"
        provenance["freshness"] = "STALE"
        return [], provenance

    if not report.ok:
        provenance["blocked"] = f"data failed validation: {format_report(report)}"
        provenance["freshness"] = "STALE"
        return [], provenance
    if partial_failures:
        provenance["blocked"] = "PARTIAL FRESH FETCH - Upstox refused some sub-windows: " + "; ".join(partial_failures)
        provenance["freshness"] = "STALE (partial)"

    provenance["actual_from"] = bars[0].timestamp.date().isoformat()
    provenance["actual_to"] = bars[-1].timestamp.date().isoformat()
    provenance["candle_count"] = len(bars)
    provenance["data_quality"] = validation_to_dict(report)
    saved = save_dataset(
        bars,
        instrument=instrument,
        provider="upstox",
        interval=interval,
        directory=str(outdir),
        name=name,
    )
    meta = saved.metadata
    provenance["dataset_path"] = str(outdir / f"{name}.csv")
    provenance["data_hash"] = meta.get("data_hash")
    provenance["schema_version"] = meta.get("schema_version")
    provenance["quality_result"] = "OK"
    return bars, provenance


def load_cached(path: Path) -> tuple[list, dict[str, Any]]:
    """Fallback loader (only used when fresh fetch is blocked): marks STALE."""
    meta_path = path.with_suffix(".meta.json")
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    record = load_dataset(str(path))
    bars = list(record.bars)
    provenance = {
        "provider": "upstox",
        "instrument": meta.get("instrument", "NIFTY 50"),
        "instrument_key": meta.get("instrument_key", "unknown"),
        "interval": "5m",
        "requested_from": bars[0].timestamp.date().isoformat() if bars else None,
        "requested_to": bars[-1].timestamp.date().isoformat() if bars else None,
        "actual_from": bars[0].timestamp.date().isoformat() if bars else None,
        "actual_to": bars[-1].timestamp.date().isoformat() if bars else None,
        "candle_count": len(bars),
        "retrieved_at": meta.get("retrieved_at") or _utcnow(),
        "data_hash": meta.get("data_hash"),
        "schema_version": meta.get("schema_version"),
        "dataset_path": str(path),
        "freshness": "STALE",
        "blocked": "FRESH FETCH BLOCKED - using previously cached dataset explicitly marked STALE",
    }
    return bars, provenance


def _market_environment(bars: list, start: date, end: date) -> dict[str, Any]:
    window = [b for b in bars if start <= b.timestamp.date() <= end]
    days: dict[date, list] = {}
    for b in window:
        days.setdefault(b.timestamp.date(), []).append(b)
    if not window:
        return {
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "days": 0, "bars": 0, "avg_close": 0.0, "min_low": 0.0,
            "max_high": 0.0, "avg_day_range": 0.0, "median_atr": 0.0,
            "regime_estimate": "NO DATA", "note": "no bars in analysis window",
        }
    closes = [float(b.close) for b in window]
    ranges = []
    up_days = 0
    for day_bars in days.values():
        ranges.append(float(max(b.high for b in day_bars) - min(b.low for b in day_bars)))
        if float(day_bars[-1].close) > float(day_bars[0].open):
            up_days += 1
    total_days = len(days)
    net_move = (float(window[-1].close) / float(window[0].close) - 1.0) * 100.0
    regime = f"{up_days}/{total_days} up days; net move over window {net_move:+.2f}%"
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "days": total_days,
        "bars": len(window),
        "avg_close": sum(closes) / len(closes),
        "min_low": min(float(b.low) for b in window),
        "max_high": max(float(b.high) for b in window),
        "avg_day_range": avg_range,
        "median_atr": avg_range,
        "regime_estimate": regime,
        "note": "indicative environment only; actual tradable regime is bucketed per trade",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--train-start", default="2025-01-02")
    parser.add_argument("--train-end", default="2025-06-30")
    parser.add_argument("--val-start", default="2025-07-01")
    parser.add_argument("--val-end", default="2025-10-03")
    parser.add_argument("--protected-oos-start", default="2025-10-06")
    parser.add_argument("--lookback-days", type=int, default=45,
                        help="calendar-day warm-up reach before train start")
    parser.add_argument("--outdir", default="datasets")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--token", default="", help="FNO_UPSTOX_ACCESS_TOKEN (default: env)")
    args = parser.parse_args(argv)

    load_dotenv()
    token = (args.token or os.getenv("FNO_UPSTOX_ACCESS_TOKEN", "") or "").strip()

    train_start = date.fromisoformat(args.train_start)
    train_end = date.fromisoformat(args.train_end)
    val_start = date.fromisoformat(args.val_start)
    val_end = date.fromisoformat(args.val_end)
    oos_start = date.fromisoformat(args.protected_oos_start)
    if val_end >= oos_start:
        parser.error(f"val-end {val_end} must be strictly before protected OOS start {oos_start}")
    if train_end < train_start or val_start <= train_end:
        parser.error("train and validation windows must be contiguous and ordered")

    outdir = Path(args.outdir)
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    instrument = _resolve_instrument(args.instrument)
    run_id = f"cycle_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # ------------------------------------------------------------------ fetch
    request_from = train_start - timedelta(days=args.lookback_days)
    request_to = val_end
    bars: list = []
    provenance: dict[str, Any] = {}
    blocked = None
    if not token:
        blocked = "FNO_UPSTOX_ACCESS_TOKEN not set - fresh fetch is opt-in"
    else:
        bars, provenance = fetch_fresh(
            instrument, args.interval,
            datetime.combine(request_from, datetime.min.time()),
            datetime.combine(request_to, datetime.min.time()),
            token, outdir, f"nifty50_5m_discovery_{run_id}",
        )
        blocked = provenance.get("blocked")
    if blocked:
        print(f"[discovery] DATA FETCH BLOCKED: {blocked}", flush=True)
        cached = _pick_cached(outdir, args.interval, train_start, val_end)
        if cached is not None:
            bars, provenance = load_cached(cached)
            print(f"[discovery] falling back to STALE cached dataset: {cached}", flush=True)
        else:
            print("[discovery] no cached dataset overlaps the research window; continuing with a report-only run.", flush=True)

    if not bars:
        print("[discovery] no bars available for research.", flush=True)
        payload = _minimal_payload(run_id, provenance, blocked, args, oos_start)
        paths = report_mod.write_report(payload, str(reports_dir))
        print(json.dumps(paths, indent=2, sort_keys=True))
        return 3

    # ----------------------------------------------------- research windows
    # HARD OOS GUARD: nothing on/after the protected boundary may ever reach a
    # provider, a replay, or the metrics engine.
    before = len(bars)
    bars = [b for b in bars if b.timestamp.date() < oos_start]
    dropped = before - len(bars)
    if dropped:
        print(f"[discovery] OOS GUARD: dropped {dropped} bars at/after {oos_start} before research", flush=True)
    actual_first = bars[0].timestamp.date()
    actual_last = bars[-1].timestamp.date()
    train_start_eff = max(train_start, actual_first)
    val_end_eff = min(val_end, actual_last)

    config = WalkForwardConfig(
        first_date=train_start_eff,
        last_date=val_end_eff,
        protected_oos_start=oos_start,
    )

    print(
        f"[discovery] window train {train_start_eff}..{train_end} "
        f"validation {val_start}..{val_end_eff} "
        f"(OOS protected >= {oos_start}) bars={len(bars)}", flush=True)

    # ------------------------------------------------------------- per-run
    definitions = cat.build_deck(version="1.0")
    control_defn = next(d for d in definitions if d.control)
    deck = [d for d in definitions if not d.control]

    candidates: dict[str, dict[str, Any]] = {}
    challenger_runs: list[tuple[Any, Any, Any]] = []  # (defn, train_run, val_run)
    control_runs: dict[str, Any] = {}
    scorecards = []
    robustness_out: dict[str, dict[str, Any]] = {}
    cost_scan_out: dict[str, dict[str, Any]] = {}
    promotion_out: dict[str, dict[str, Any]] = {}

    for defn in [control_defn] + deck:
        cid = defn.candidate_id
        cat.persist_definition(defn, str(reports_dir))
        t0 = time.time()
        day_replays = replay_candidate(
            defn, bars, config, start=train_start_eff, end=val_end_eff
        )
        train_replays = [r for r in day_replays if r.day <= train_end]
        val_replays = [r for r in day_replays if r.day > train_end]
        train_run = analyze(
            train_replays, cid, defn.version,
            window_start=train_start_eff, window_end=train_end,
            segments=SEGMENTS,
        )
        val_run = analyze(
            val_replays, cid, defn.version,
            window_start=val_start, window_end=val_end_eff,
            segments=SEGMENTS,
        )
        print(f"[discovery] {cid}: train net {train_run.net_pnl} ({train_run.trades} tr), "
              f"val net {val_run.net_pnl} ({val_run.trades} tr) in {time.time() - t0:.1f}s", flush=True)

        # robustness grid
        variants = cat.perturbations_for(defn)
        nets: dict[str, float] = {}
        for variant in variants:
            cat.persist_definition(variant, str(reports_dir))
            v_replays = replay_candidate(variant, bars, config, start=train_start_eff, end=val_end_eff)
            nets[variant.version] = float(sum((r.net_pnl for r in v_replays), Decimal("0")))
        positive = sum(1 for v in nets.values() if v > 0)
        robustness_out[cid] = {
            "n": len(nets),
            "positive": positive,
            "positive_fraction": (positive / len(nets)) if nets else 1.0,
            "net_by_variant": {k: format(v, ".2f") for k, v in nets.items()},
        }

        # cost sensitivity at 1x / 3x / 5x on the full research window
        cost_nets: dict[int, float] = {}
        for mult in COST_MULTIPLIERS:
            if mult == 1:
                full_net = sum((r.net_pnl for r in day_replays), Decimal("0"))
                cost_nets[mult] = float(full_net)
                continue
            cfg_m = replace(
                config,
                commission_rate=config.commission_rate * mult,
                slippage_rate=config.slippage_rate * mult,
            )
            m_replays = replay_candidate(defn, bars, cfg_m, start=train_start_eff, end=val_end_eff)
            cost_nets[mult] = float(sum((r.net_pnl for r in m_replays), Decimal("0")))
        cost_pos = sum(1 for v in cost_nets.values() if v > 0)
        cost_scan_out[cid] = {
            "1x": format(cost_nets[1], ".2f"),
            "3x": format(cost_nets[3], ".2f"),
            "5x": format(cost_nets[5], ".2f"),
            "positive_fraction": cost_pos / len(COST_MULTIPLIERS),
        }

        sc = score_candidate(
            train_run,
            robustness_positive_fraction=robustness_out[cid]["positive_fraction"],
            cost_positive_fraction=cost_scan_out[cid]["positive_fraction"],
        )
        scorecards.append(sc)
        candidates[cid] = {
            "definition": defn.to_dict(),
            "train": train_run.to_dict(),
            "validation": val_run.to_dict(),
            "robustness": robustness_out[cid],
            "cost_scan": cost_scan_out[cid],
            "scorecard": sc.to_dict(),
        }
        if cid == control_defn.candidate_id:
            control_runs = {"train": train_run, "validation": val_run}
        else:
            challenger_runs.append((defn, train_run, val_run))

    # ------------------------------------------------- promotion + ranking
    control_val_run = control_runs["validation"]
    training_runs = [r for _, r, _ in challenger_runs]
    validation_runs = [r for _, _, r in challenger_runs]
    for defn, _, val_run in challenger_runs:
        verdict = promotion_verdict(
            val_run,
            control_val_run,
            challenger_name=defn.candidate_id,
            config=config,
        )
        promotion_out[defn.candidate_id] = {
            "decision": verdict.decision,
            "reasons": list(verdict.reasons),
            "evidence": dict(verdict.evidence),
        }

    ranking = rank(scorecards)

    # ---------------------------------------------------- best + learning
    eligible = [
        sc for sc in scorecards
        if not sc.candidate_id.startswith("model_0")
        and sc.net_pnl > 0
        and "insufficient_evidence" not in sc.flags
        and "no_trades" not in sc.flags
        and sc.robustness_positive_fraction >= 0.5
        and _beats_control_on_val(validation_runs, control_val_run, sc.candidate_id)
    ]
    best = max(eligible, key=lambda s: s.score) if eligible else None

    failure = analyze_failures(training_runs, validation_runs)

    families: dict[str, list[str]] = {}
    for d in deck:
        families.setdefault(d.family, []).append(d.candidate_id)
    families_tested = [{"family": "CONTROL (frozen)", "candidate_ids": [control_defn.candidate_id]}]
    families_tested.extend(
        {"family": family, "candidate_ids": ids} for family, ids in sorted(families.items())
    )

    if best is not None:
        best_train = candidates[best.candidate_id]["train"]
        best_val = candidates[best.candidate_id]["validation"]
        why_best = (
            f"{best.candidate_id}@{best.version} is the top competition-ranked candidate with "
            f"train net {best_train.get('net_pnl')} over {best_train.get('trades')} trades "
            f"(WR {best_train.get('win_rate')}%, PF {best_train.get('profit_factor')}, "
            f"maxDD {best_train.get('max_drawdown_pct')}%) and validation net {best_val.get('net_pnl')} "
            f"over {best_val.get('trades')} trades; robustness {best.robustness_positive_fraction:.2f} "
            f"perturbation-positive, cost sensitivity {best.cost_positive_fraction:.2f}. "
            f"Promotion is still REJECTED (no OOS evidence)."
        )
    else:
        why_best = (
            "NO ROBUST PROFITABLE CANDIDATE FOUND on this window: no challenger simultaneously "
            "has positive train net, positive-cost-adjusted, robustness >= 0.5, and beats the "
            "frozen control on validation. Being 'less bad' is not a promotion."
        )

    unproven = [
        "Protected out-of-sample (>= 2025-10-06) — never evaluated during discovery",
        "Options economics — expiry/strike/premium/volume/OI/IV/bid-ask data NOT AVAILABLE; option profitability is NOT claimed",
        "Live fill behavior, latency and market-impact slippage",
        "Regime persistence across other market cycles (retest under the same discipline once OOS unlocks)",
        "Multi-timeframe and India VIX regime layer (architecturally ready, not exercised without data)",
    ]

    payload: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": _utcnow(),
        "generation": cat.GENERATION,
        "provenance": provenance,
        "config": config.to_dict(),
        "config_hash": config.config_hash,
        "watch_window": {
            "train_start": train_start_eff.isoformat(),
            "train_end": train_end.isoformat(),
            "validation_start": val_start.isoformat(),
            "validation_end": val_end_eff.isoformat(),
            "protected_oos_start": oos_start.isoformat(),
        },
        "market_environment": _market_environment(bars, train_start_eff, val_end_eff),
        "families_tested": families_tested,
        "candidates_generated": len(candidates),
        "candidates": candidates,
        "robustness": robustness_out,
        "cost_scan": cost_scan_out,
        "scorecards": [sc.to_dict() for sc in scorecards],
        "ranking": [
            {
                "rank": i + 1,
                "candidate_id": sc.candidate_id,
                "version": sc.version,
                "score": round(sc.score, 4),
                "net_pnl": format(sc.net_pnl, "f"),
                "trades": sc.trades,
                "flags": list(sc.flags),
            }
            for i, sc in enumerate(ranking)
        ],
        "promotion": promotion_out,
        "current_best": {
            "candidate_id": best.candidate_id,
            "version": best.version,
            "score": round(best.score, 4),
        } if best else None,
        "why_best": why_best,
        "unproven": unproven,
        "next_hypothesis": failure.next_hypothesis,
        "learning": failure.to_dict(),
        "algo_ready": "NO",
        "live_trading": False,
        "next_action": f"run hypothesis {failure.next_hypothesis.get('family', '?')}: {failure.next_hypothesis.get('mechanism', '?')}",
        "links": {
            "paper trading dashboard": "../../docs/paper_trading_dashboard.html",
            "paper dashboard audit json": "../../reports/algorithm_state/paper_dashboard.json",
            "paper trade ledger": "../../reports/algorithm_state/paper_trades.json",
            "bucketed trade ledger": "../../reports/algorithm_state/trade_ledger.json",
            "candidate definitions": "../../reports/discovery_definitions/",
            "data provenance": "../../reports/discovery_cycle_{}.json provenance section".format(run_id),
        },
    }

    paths = report_mod.write_report(payload, str(reports_dir))
    print(json.dumps({"run_id": run_id, "algo_ready": "NO",
                      "best": payload["current_best"],
                      "next_hypothesis": failure.next_hypothesis.get("family"),
                      "reports": paths}, indent=2, sort_keys=True, default=str))
    return 3 if blocked else 0


def _beats_control_on_val(validation_runs, control_val_run, candidate_id) -> bool:
    control_net = float(control_val_run.net_pnl)
    for run in validation_runs:
        if run.candidate_id == candidate_id:
            return float(run.net_pnl) > control_net
    return False


def _minimal_payload(run_id: str, provenance: dict[str, Any], blocked: str,
                     args: argparse.Namespace, oos_start: date) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp": _utcnow(),
        "generation": cat.GENERATION,
        "provenance": provenance,
        "config": {},
        "config_hash": "N/A",
        "watch_window": {"protected_oos_start": oos_start.isoformat()},
        "market_environment": _market_environment([], date.today(), date.today()),
        "families_tested": [],
        "candidates_generated": 0,
        "candidates": {},
        "robustness": {},
        "cost_scan": {},
        "scorecards": [],
        "ranking": [],
        "promotion": {},
        "current_best": None,
        "why_best": "DATA FETCH BLOCKED — no research was possible without fresh bars.",
        "unproven": ["everything (no data)"],
        "next_hypothesis": {"family": "DATA_ACQUISITION", "mechanism": "restore the read-only Upstox token and rerun",
                            "read": "no valid research can occur without fresh data", "trigger": blocked},
        "learning": {"bullet": "no candidates replayed", "worst_buckets": [],
                     "gross_edge_exists": False, "cost_kill": False,
                     "next_hypothesis": {"family": "DATA_ACQUISITION"}},
        "algo_ready": "NO",
        "live_trading": False,
        "next_action": "acquire the FNO_UPSTOX_ACCESS_TOKEN and rerun the cycle",
        "links": {},
    }


if __name__ == "__main__":
    raise SystemExit(main())