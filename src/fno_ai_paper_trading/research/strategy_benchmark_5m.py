"""Research-only 5M strategy benchmark: candidates vs the frozen Donchian 20/10.

This harness compares the pre-registered candidate signal families from
``strategy_benchmark_5m_candidates.py`` with the **unchanged** Donchian 20/10
baseline through the **same deterministic engine and economic model** used to
record the baseline:

* candidate signals are precomputed causally and fed to a subclass of the real
  ``Directional5MOptionsEngine`` whose only override swaps the signal line of
  ``_process_decision``; every other behaviour (broker, commission / slippage,
  sizing, risk, stop, EOD flatten, decision cadence, fingerprint) is inherited
  unchanged,
* identity is *proved*, not assumed: no tuning is done, and the Donchian stream
  driven through the subclass over the Dev window must reproduce the recorded
  fingerprint ``b1be2188...a8c6`` and the recorded Dev metrics exactly,
* windows: Dev ``2025-05-22..2025-08-14`` (baseline row reuses the recorded
  artifact which the subclass run must reproduce; every candidate is also run
  fresh), Full domain ``2022-01-03..2025-10-03`` (fresh runs for every family
  **including** the Donchian baseline) and the protected OOS
  ``2025-10-06..2026-09-11`` (single pre-registered run, no tuning, no re-runs),
* reporting is classification-only (``baseline reference`` / ``not testable`` /
  ``insufficient evidence`` / ``rejected by gate`` / ``unstable`` /
  ``promising (research-only)``).  Nothing is ranked, nothing is declared a
  winner, ``ALGO READY`` stays ``NO`` and nothing in this module can promote a
  candidate or touch the live/paper execution path.

Project rules preserved:
* the Donchian 20/10 baseline module is imported read-only (never modified),
* all engine stores and artifacts are written under the OS temp directory or
  the research-only ``reports\\forensics`` folder; the repository's experiment
  stores, strategies and live paths are never written,
* candidate robustness perturbations and gate thresholds are constants below
  (pre-registered, never tuned after the OOS run).
"""
from __future__ import annotations

import argparse
import bisect
import dataclasses
import hashlib
import json
import tempfile
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.experiments.directional_15m.contract import (
    Leg,
    Signal15m,
    decide,
)
from fno_ai_paper_trading.experiments.directional_5m.executor import (
    DayAlreadyReported,
    Directional5MOptionsConfig,
    Directional5MOptionsEngine,
)
from fno_ai_paper_trading.experiments.directional_5m.runner import PAPER_ONLY_BANNER
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.paper_track.feed import TRACK_INSTRUMENT
from fno_ai_paper_trading.paper_track.runner import day_ticks
from fno_ai_paper_trading.paper_track.store import TrackStore
from fno_ai_paper_trading.research.strategy_benchmark_5m_candidates import (
    CANDIDATES,
    PARAM_OVERRIDES,
    CandidateSpec,
    apply_overrides,
    build_stream,
)

BAR_MINUTES = 5
NEUTRAL = Signal15m.NEUTRAL
BULLISH = Signal15m.BULLISH
BEARISH = Signal15m.BEARISH

DEV_START = date(2025, 5, 22)
DEV_END = date(2025, 8, 14)
OOS_START = date(2025, 10, 6)

DATASET_PATH = Path("datasets") / "upstox_Nifty_50_5m_20220103_20260911.csv"
DATASET_HASH_PREFIX = "6c400b01"

# Recorded Dev baseline (validation_report.json, Temp/fno_5m_validation). The
# subclass + Donchian stream must reproduce this fingerprint and these metrics.
RECORDED_DEV_FINGERPRINT = (
    "b1be21885bba16b7481a7f1d84fe7b970dd6259b969a84f5187986b20397a8c6"
)
RECORDED_DEV_METRICS: dict[str, object] = {
    "decision_points_total": 4392,
    "signal_counts": {"BULLISH": 127, "BEARISH": 121, "NEUTRAL": 4144},
    "round_trips": 229,
    "wins": 2,
    "losses": 227,
    "win_rate": "0.008733624454148471615720524017",
    "gross_realized_pnl": "-11696.30900",
    "gross_profit": "34.43110",
    "gross_loss": "-11730.74010",
    "commissions": "3500.252708640",
    "slippage_estimate": "11667.50900",
    "net_pnl": "-15196.561708640",
    "ending_cash": "84803.438291360",
    "max_drawdown": "15196.561708640",
    "max_drawdown_pct": "0.1519656170864048782233924394",
    "entries": {"CALL": 115, "PUT": 113},
    "eod_flattens": 3,
    "neutral_exits": 225,
    "stops_fired": 0,
    "reversals": 1,
}

# Classification gate (pre-registered, documented constants).
GATE_MIN_OOS_TRADES = 20
GATE_MAX_FULL_DRAWDOWN_PCT = Decimal("0.25")


def _D(value: object) -> Decimal:
    return Decimal(str(value))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


class WindowHistoryFeed:
    """Read-only 5m bar feed over a window slice, engine-shaped.

    ``delta=True`` returns only the bars that completed since the last query
    (used for the large windows so a full-domain run stays O(n)); ``delta=False``
    mirrors the recorded harness exactly (full completed prefix each tick) and
    is used for the small Dev window whose recorded numbers must be reproduced.
    """

    def __init__(
        self, instrument, bars: list[MarketPrice], *, delta: bool = False
    ) -> None:
        self.instrument = instrument
        self._bars = sorted(bars, key=lambda b: b.timestamp)
        self._ts = [b.timestamp for b in self._bars]
        self._close_by_ts = {b.timestamp: b.close for b in self._bars}
        self._delta = delta
        self._cursor = 0

    def bars_up_to(self, moment: datetime) -> list[MarketPrice]:
        cut = moment - timedelta(minutes=BAR_MINUTES)
        idx = bisect.bisect_right(self._ts, cut)
        if self._delta:
            out = self._bars[self._cursor : idx]
            self._cursor = idx
            return list(out)
        return self._bars[:idx]

    def last_close(self, moment: datetime) -> Decimal | None:
        done = self.bars_up_to(moment)
        return done[-1].close if done else None

    def close_at(self, ts: datetime) -> Decimal | None:
        return self._close_by_ts.get(ts)

    @property
    def window_size(self) -> int:
        return len(self._bars)


# ---------------------------------------------------------------------------
# subclassed engine (the only overridden behaviour is the signal source)
# ---------------------------------------------------------------------------


class StrategizedDirectional5MOptionsEngine(Directional5MOptionsEngine):
    """The base 5M engine with the signal line of ``_process_decision`` swapped
    to a precomputed causal stream; everything else is inherited unchanged.

    ``persist_eod_reports=False`` (large windows only) keeps the protective EOD
    flatten and day-finalization behaviour identical while skipping the per-day
    report/checkpoint disk writes (which would otherwise duplicate the growing
    bar history on every finalize).  Trading economics are unaffected: every
    position change still happens at a decision point or the 15:20 flatten,
    none of which is touched by report persistence.
    """

    def __init__(
        self,
        config,
        *,
        stream: list[Signal15m],
        window_bars: list[MarketPrice],
        store=None,
        clock=None,
        bars_source=None,
        failpoints=None,
        run_id=None,
        persist_eod_reports: bool = True,
    ) -> None:
        super().__init__(
            config,
            store=store,
            clock=clock,
            bars_source=bars_source,
            failpoints=failpoints,
            run_id=run_id,
        )
        self._stream = list(stream)
        self._bar_index = {
            b.timestamp: i
            for i, b in enumerate(sorted(window_bars, key=lambda b: b.timestamp))
        }
        self._persist_eod_reports = persist_eod_reports

    def _benchmark_signal(self, bar: MarketPrice) -> Signal15m:
        idx = self._bar_index.get(bar.timestamp)
        if idx is None:
            self.counters["data_errors"].append(
                f"benchmark stream missing reference bar {bar.timestamp:%H:%M}"
            )
            return NEUTRAL
        return self._stream[idx]

    def _process_decision(self, now: datetime) -> None:  # noqa: D102
        result = self.aggregator.decision_at(now)
        if not result.complete:
            self.counters["data_errors"].append(result.note)
            self._mark_equity(now, self._last_close(now))
            return
        bar = result.reference_bar
        if bar is None:  # pragma: no cover
            return

        stop_filled: list[str] = []
        if self.leg is not Leg.FLAT:
            pos = self.portfolio.position_for(self.symbol)
            if pos is not None and pos.quantity != 0:
                check = self.stop_policy.check(
                    leg=self.leg, entry_price=pos.average_entry_price, bar=bar
                )
                if check.triggered:
                    trade = self._exit_leg(self.leg, bar, now, kind="STOP")
                    if trade is not None:
                        self.counters["stops_fired"] += 1
                        stop_filled.append(self.leg.value)

        signal5m = self._benchmark_signal(bar)
        decision = decide(self.leg, signal5m)
        state_before = self.leg
        new_fill_ids: list[str] = []
        for action in decision.actions:
            new_fill_ids.extend(self._apply_action(action, bar, now))
        state_after = self.leg

        record = {
            "moment": now.isoformat(),
            "signal": signal5m.value,
            "state_before": state_before.value,
            "state_after": state_after.value,
            "actions": [action.value for action in decision.actions],
            "reason": decision.reason,
            "fills": new_fill_ids,
        }
        self.decisions.append(record)
        self.counters["decision_points"] += 1
        self.counters["signal_counts"][signal5m.value] += 1
        self._mark_equity(now, bar.close)

    def _maybe_finalize(self, now: datetime) -> None:
        if self.day is None:
            return
        if self._is_finalized(self.day):
            return
        if now.time() < self.config.policy.window_close:
            return
        if self.eod_status == "NONE" and self.leg is not Leg.FLAT:
            self._maybe_flatten(now)
        if self._persist_eod_reports:
            from fno_ai_paper_trading.experiments.directional_5m.report import (
                build_experiment_report,
            )

            report = build_experiment_report(self)
            self.store.save_report(self.day, report)
            self.store.save_checkpoint(self.day, self.checkpoint_payload())
            self.store.update_run(
                self.run_id,
                last_day=self.day.isoformat(),
                fingerprint=report["fingerprint"],
            )
        self._finalized_days.add(self.day.isoformat())


def replay_strategized(
    *,
    cfg: Directional5MOptionsConfig,
    store: TrackStore,
    feed: WindowHistoryFeed,
    days: list[date],
    stream: list[Signal15m],
    window_bars: list[MarketPrice],
    persist: bool,
    run_id: str,
) -> StrategizedDirectional5MOptionsEngine:
    """Drive the subclassed engine over ``days`` at every session tick (mirrors
    ``runner.replay_experiment`` exactly, incl. the once-per-day guard)."""
    engine = StrategizedDirectional5MOptionsEngine(
        cfg,
        stream=stream,
        window_bars=window_bars,
        store=store,
        bars_source=feed,
        run_id=run_id,
        persist_eod_reports=persist,
    )
    for day in days:
        if store.load_report(day) is not None:
            raise DayAlreadyReported(
                f"{day.isoformat()} trading day already completed; refusing to re-run it"
            )
    for day in days:
        for tick in day_ticks(day):
            engine.clock.set(tick)
            engine.step()
    return engine


# ---------------------------------------------------------------------------
# data loading / window slicing
# ---------------------------------------------------------------------------


def load_dataset_verified(repo: Path):
    ds = load_dataset(repo / DATASET_PATH)
    if not ds.metadata.get("data_hash", "").startswith(DATASET_HASH_PREFIX):
        raise RuntimeError(f"dataset hash prefix mismatch: {ds.metadata.get('data_hash')}")
    return ds


def re_stamp_bars(ds) -> list[MarketPrice]:
    """Re-stamp every bar to the engine's canonical instrument (symbol-safe)."""
    instrument = TRACK_INSTRUMENT()
    out = []
    for bar in ds.bars:
        out.append(
            MarketPrice(
                instrument=instrument,
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                open_interest=bar.open_interest,
            )
        )
    return out


def window_bars(bars: list[MarketPrice], predicate):
    sel = [b for b in bars if predicate(b.timestamp.date())]
    days = sorted({b.timestamp.date() for b in sel})
    return sel, days, len(sel)


def dev_window(bars):
    return window_bars(bars, lambda d: DEV_START <= d <= DEV_END)


def full_window(bars):
    return window_bars(bars, lambda d: d < OOS_START)


def oos_window(bars):
    return window_bars(bars, lambda d: d >= OOS_START)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _slippage_estimate(engine, feed: WindowHistoryFeed):
    """Modeled slippage cost per fill = |fill price - reference candle close|."""
    total = Decimal("0")
    per_side = {"BUY": Decimal("0"), "SELL": Decimal("0")}
    n = 0
    for fill in engine.broker.fills:
        ref_ts = fill.filled_at - timedelta(minutes=BAR_MINUTES)
        close = feed.close_at(ref_ts)
        if close is None:
            continue
        cost = abs(fill.price - close) * fill.quantity  # multiplier == 1 (index)
        total += cost
        per_side[fill.side.value] += cost
        n += 1
    return {
        "fills_mapped": n,
        "total_slippage_estimate": str(total),
        "per_side": {k: str(v) for k, v in per_side.items()},
        "note": "modeled slippage_rate=0.001 already embedded in fill prices.",
    }


def _neutral_exits(engine) -> int:
    return sum(
        1
        for d in engine.decisions
        if d["signal"] == "NEUTRAL" and d["state_before"] != "FLAT"
    )


def next_candle_accuracy(stream: list[Signal15m], bars: list[MarketPrice]) -> dict:
    """Share of directional signals whose same-session next 5m candle closed in
    the signaled direction (bars are already sorted; cross-session pairs are
    excluded so an overnight gap never counts as a 'next candle')."""
    bull_sig = bull_ok = bear_sig = bear_ok = 0
    for i in range(len(stream) - 1):
        if bars[i].timestamp.date() != bars[i + 1].timestamp.date():
            continue
        sig = stream[i]
        if sig == BULLISH:
            bull_sig += 1
            if bars[i + 1].close > bars[i].close:
                bull_ok += 1
        elif sig == BEARISH:
            bear_sig += 1
            if bars[i + 1].close < bars[i].close:
                bear_ok += 1

    def _pct(ok: int, n: int):
        return round(100.0 * ok / n, 4) if n else None

    return {
        "bullish_signals": bull_sig,
        "bullish_next_candle_up": bull_ok,
        "bullish_accuracy_pct": _pct(bull_ok, bull_sig),
        "bearish_signals": bear_sig,
        "bearish_next_candle_down": bear_ok,
        "bearish_accuracy_pct": _pct(bear_ok, bear_sig),
        "note": (
            "share of directional signals whose immediately-following same-session "
            "5m candle closed in the signaled direction."
        ),
    }


def engine_metrics(engine, feed: WindowHistoryFeed, window: str) -> dict:
    report = engine.report()
    acct = report["accounting"]
    perf = report["performance"]
    dec = report["decisions"]
    sig = dec["signal_counts"]
    slippage = _slippage_estimate(engine, feed)
    return {
        "window": window,
        "decision_points_total": dec["decision_points_total"],
        "signal_counts": dict(sig),
        "signals_directional": int(sig["BULLISH"] + sig["BEARISH"]),
        "entries": dict(dec["entries"]),
        "exits": dict(dec["exits"]),
        "reversals": dec["reversals"],
        "stops_fired": dec["stops_fired"],
        "eod_flattens": dec["eod_flattens"],
        "neutral_exits": _neutral_exits(engine),
        "round_trips": perf["round_trips"],
        "wins": perf["wins"],
        "losses": perf["losses"],
        "flat_closures": perf["flat_closures"],
        "win_rate": perf["win_rate"],
        "gross_avg_trade": perf["gross_avg_trade"],
        "best_trade": perf["best_trade"],
        "worst_trade": perf["worst_trade"],
        "consecutive_losses": perf["consecutive_losses"],
        "holding_minutes": perf["time_in_position_minutes"],
        "max_drawdown": perf["max_drawdown"],
        "max_drawdown_pct": perf["max_drawdown_pct"],
        "worst_day": perf["worst_day"],
        "starting_cash": acct["initial_cash"],
        "ending_cash": acct["cash_end"],
        "gross_realized_pnl": acct["realized_pnl"],
        "gross_profit": acct["gross_profit"],
        "gross_loss": acct["gross_loss"],
        "commissions": acct["commissions"],
        "net_pnl": acct["net_pnl"],
        "reconciled": acct["reconciled"],
        "fills": report["execution"]["fills"],
        "data_errors": report["data_quality"]["data_errors"],
        "anomalies": report["data_quality"]["anomalies"],
        "poisoned": report["data_quality"]["poisoned"],
        "slippage_estimate": slippage["total_slippage_estimate"],
        "slippage_fills_mapped": slippage["fills_mapped"],
        "slippage_per_side": slippage["per_side"],
        "fingerprint": engine.stable_fingerprint(),
    }


def cost_stress(metrics: dict) -> dict:
    """Gross-close edge and net at 0x/1x/3x/5x modeled cost multipliers.

    net(m) = (realized_gross + slippage_est) - m * (slippage_est + commissions);
    m == 1 recovers the engine's accounting net_pnl exactly.
    """
    gross = _D(metrics["gross_realized_pnl"])
    s = _D(metrics["slippage_estimate"])
    comm = _D(metrics["commissions"])
    gross_close = gross + s
    costs1 = s + comm
    edge = abs(gross_close)

    def net(m: Decimal) -> Decimal:
        return gross_close - m * costs1

    return {
        "gross_close_pnl": str(gross_close),
        "costs_1x": str(costs1),
        "net_at_0x": str(net(Decimal("0"))),
        "net_at_1x": str(net(Decimal("1"))),
        "net_at_3x": str(net(Decimal("3"))),
        "net_at_5x": str(net(Decimal("5"))),
        "cost_coverage_pct": round(100.0 * float(costs1) / float(edge), 6)
        if edge != 0
        else None,
        "formula": (
            "net(m) = (realized_gross + slippage_est) - m*(slippage_est + "
            "commissions); 1x == engine net_pnl"
        ),
    }


# ---------------------------------------------------------------------------
# classification gate (pre-registered, pure, unit-tested)
# ---------------------------------------------------------------------------


def classify_candidate(spec: CandidateSpec, metrics: dict[str, dict]) -> dict:
    """Pre-registered verdict logic over the recorded window rows only."""
    if spec.not_testable_reason:
        return {
            "verdict": "not testable",
            "reason": spec.not_testable_reason,
            "checks": {},
        }
    if spec.baseline:
        return {
            "verdict": "baseline reference",
            "reason": "frozen reference row; never classified as a candidate.",
            "checks": {},
        }
    dev = metrics.get("dev")
    full = metrics.get("full")
    oos = metrics.get("oos")
    if oos is None or full is None or dev is None:
        return {
            "verdict": "insufficient evidence",
            "reason": "missing Dev / Full / OOS rows.",
            "checks": {},
        }
    trades = oos["round_trips"]
    oos_gross_close = _D(oos["gross_realized_pnl"]) + _D(oos["slippage_estimate"])
    oos_net = _D(oos["net_pnl"])
    dev_net = _D(dev["net_pnl"])
    full_net = _D(full["net_pnl"])
    full_dd_pct = _D(full["max_drawdown_pct"] or "0")
    checks = {
        "oos_round_trips": int(trades),
        "oos_min_trades_met": trades >= GATE_MIN_OOS_TRADES,
        "oos_gross_close_edge": str(oos_gross_close),
        "oos_gross_edge_positive": oos_gross_close > 0,
        "oos_net_1x": str(oos_net),
        "oos_economic_positive": oos_net > 0,
        "full_domain_drawdown_pct": str(full_dd_pct),
        "full_domain_drawdown_below_25pct": full_dd_pct < GATE_MAX_FULL_DRAWDOWN_PCT,
        "dev_net_1x": str(dev_net),
        "full_net_1x": str(full_net),
        "dev_oos_sign_consistent": (dev_net >= 0) == (oos_net >= 0),
        "full_oos_sign_consistent": (full_net >= 0) == (oos_net >= 0),
    }
    if not checks["oos_min_trades_met"]:
        verdict = "insufficient evidence"
        reason = "too few OOS round trips for a statistically usable sample."
    elif not (checks["oos_gross_edge_positive"] and checks["oos_economic_positive"]):
        verdict = "rejected by gate"
        reason = "no positive OOS gross edge, or costs destroy the edge at 1x."
    elif not (
        checks["full_domain_drawdown_below_25pct"]
        and checks["dev_oos_sign_consistent"]
        and checks["full_oos_sign_consistent"]
    ):
        verdict = "unstable"
        reason = "blow-up drawdown or direction-flip Dev/Full vs OOS."
    else:
        verdict = "promising (research-only)"
        reason = (
            "positive OOS economic result within the pre-registered gate; this is "
            "research evidence only and does NOT lift ALGO READY / promotion."
        )
    return {"verdict": verdict, "reason": reason, "checks": checks}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _store_dir(root: Path, *parts: str) -> Path:
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_candidate_window(
    *,
    spec: CandidateSpec,
    window: str,
    sel_bars: list[MarketPrice],
    days: list[date],
    persist: bool,
    run_tag: str,
    store_root: Path,
    delta_feed: bool,
    params: object | None = None,
    run_id: str | None = None,
) -> dict:
    actual_params = params if params is not None else spec.params
    stream, diagnostics = spec.builder(sel_bars, actual_params)
    cfg = Directional5MOptionsConfig(
        account="5m_directional_options",
        store_dir=_store_dir(store_root, window, spec.candidate_id, run_tag),
        initial_cash=Decimal("100000"),
    )
    feed = WindowHistoryFeed(cfg.instrument, sel_bars, delta=delta_feed)
    store = TrackStore(cfg.store_dir, cfg.account)
    rid = run_id or f"bench-{spec.candidate_id}-{window}-{run_tag}"
    t0 = time.time()
    engine = replay_strategized(
        cfg=cfg,
        store=store,
        feed=feed,
        days=days,
        stream=stream,
        window_bars=sel_bars,
        persist=persist,
        run_id=rid,
    )
    elapsed = time.time() - t0
    metrics = engine_metrics(engine, feed, window)
    metrics["run_id"] = rid
    metrics["elapsed_seconds"] = round(elapsed, 2)
    return {
        "stream": stream,
        "engine": engine,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "closures": list(engine.closures),
    }


def equivalence_check(*, sel_bars: list[MarketPrice], days: list[date], store_root: Path):
    """Prove subclass == base: run the Donchian stream twice through the subclassed
    engine on the Dev window and require the recorded fingerprint and metrics."""
    spec = CANDIDATES["donchian_20_10_baseline"]
    runs = []
    for tag in ("run_A", "run_B"):
        runs.append(
            run_candidate_window(
                spec=spec,
                window="dev",
                sel_bars=sel_bars,
                days=days,
                persist=True,
                run_tag=tag,
                store_root=store_root,
                delta_feed=False,
            )
        )
    fp_a = runs[0]["metrics"]["fingerprint"]
    fp_b = runs[1]["metrics"]["fingerprint"]
    m = runs[0]["metrics"]
    matched = fp_a == RECORDED_DEV_FINGERPRINT
    diffs = {}
    # Integer/count metrics must match exactly; money and percent metrics are
    # compared within a tiny tolerance because Decimal-hygiene artefacts can
    # differ at the ~1e-15 level between otherwise identical runs.
    exact_keys = {
        "decision_points_total",
        "round_trips",
        "wins",
        "losses",
        "eod_flattens",
        "neutral_exits",
        "stops_fired",
        "reversals",
    }
    tolerance_keys = {
        "win_rate",
        "gross_realized_pnl",
        "gross_profit",
        "gross_loss",
        "commissions",
        "slippage_estimate",
        "net_pnl",
        "ending_cash",
        "max_drawdown",
        "max_drawdown_pct",
    }
    for key in sorted(exact_keys | tolerance_keys):
        recorded = _D(RECORDED_DEV_METRICS[key])
        run = _D(m[key])
        if key in exact_keys:
            equal = run == recorded
        else:
            tol = Decimal("1e-9") * max(abs(recorded), abs(run), Decimal("1"))
            equal = abs(run - recorded) <= tol
        if not equal:
            diffs[key] = {
                "recorded": str(recorded),
                "run": str(run),
            }
    if m["entries"] != RECORDED_DEV_METRICS["entries"]:
        diffs["entries"] = {
            "recorded": str(RECORDED_DEV_METRICS["entries"]),
            "run": str(m["entries"]),
        }
    signal_ok = m["signal_counts"] == RECORDED_DEV_METRICS["signal_counts"]
    ok = matched and not diffs and signal_ok and (fp_a == fp_b)
    return {
        "ok": ok,
        "fingerprint_run_A": fp_a,
        "fingerprint_run_B": fp_b,
        "fingerprints_identical": fp_a == fp_b,
        "matches_recorded": matched,
        "recorded_fingerprint": RECORDED_DEV_FINGERPRINT,
        "signals_match_recorded": signal_ok,
        "metric_diffs": diffs,
        "note": (
            "subclass + Donchian stream over the Dev window must reproduce the "
            "recorded baseline fingerprint and metrics exactly."
        ),
    }


def _window_summary(days: list[date], n_bars: int) -> dict:
    return {
        "bars": n_bars,
        "sessions": len(days),
        "first": days[0].isoformat() if days else None,
        "last": days[-1].isoformat() if days else None,
    }


def run_benchmark(
    repo: Path,
    *,
    skip_full: bool = False,
    skip_oos: bool = False,
    temp_root: Path | None = None,
) -> tuple[dict, dict]:
    ds = load_dataset_verified(repo)
    bars = re_stamp_bars(ds)
    dev_bars, dev_days, dev_n = dev_window(bars)
    full_bars, full_days, full_n = full_window(bars)
    oos_bars, oos_days, oos_n = oos_window(bars)
    store_root = temp_root or Path(
        tempfile.mkdtemp(prefix="fno_5m_strategy_benchmark_")
    )

    windows_out = {
        "dev": _window_summary(dev_days, dev_n),
        "full": _window_summary(full_days, full_n),
        "oos": _window_summary(oos_days, oos_n),
    }

    print(PAPER_ONLY_BANNER)
    print(f"dataset: {DATASET_PATH}  hash={ds.metadata['data_hash']}")
    print(
        f"windows: dev {windows_out['dev']['first']}..{windows_out['dev']['last']} "
        f"(bars {dev_n}) | full {windows_out['full']['first']}..{windows_out['full']['last']} "
        f"(bars {full_n}) | oos {windows_out['oos']['first']}..{windows_out['oos']['last']} "
        f"(bars {oos_n})"
    )

    equivalence = equivalence_check(
        sel_bars=dev_bars, days=dev_days, store_root=store_root
    )
    print(
        f"equivalence: {'PASS' if equivalence['ok'] else 'FAIL'} "
        f"fp_runA={equivalence['fingerprint_run_A'][:16]}.. "
        f"fp_runB={equivalence['fingerprint_run_B'][:16]}.."
    )

    candidates_out: dict[str, dict] = {}
    closures_out: dict[str, dict[str, list[dict]]] = {}
    for cid, spec in CANDIDATES.items():
        row: dict = {
            "candidate_id": cid,
            "family": spec.family,
            "label": spec.label,
            "note": spec.note,
            "not_testable_reason": spec.not_testable_reason,
            "params": _params_json(spec.params),
            "windows": {},
            "cost_stress": {},
            "next_candle_accuracy": {},
            "robustness": [],
        }
        candidates_out[cid] = row
        closures_out[cid] = {}
        if spec.not_testable_reason:
            continue

        dev_runs = []
        for tag in ("run_1", "run_2"):
            dev_runs.append(
                run_candidate_window(
                    spec=spec, window="dev", sel_bars=dev_bars, days=dev_days,
                    persist=True, run_tag=tag, store_root=store_root, delta_feed=False,
                )
            )
        dev_metrics = dev_runs[0]["metrics"]
        row["dev_determinism"] = {
            "fingerprints_identical": dev_metrics["fingerprint"]
            == dev_runs[1]["metrics"]["fingerprint"],
            "fingerprint_run_1": dev_metrics["fingerprint"],
            "fingerprint_run_2": dev_runs[1]["metrics"]["fingerprint"],
        }
        row["windows"]["dev"] = dev_metrics
        row["cost_stress"]["dev"] = cost_stress(dev_metrics)
        row["next_candle_accuracy"]["dev"] = next_candle_accuracy(dev_runs[0]["stream"], dev_bars)
        row["regime_bucket_dev"] = dev_runs[0]["diagnostics"].get("regime_bucket")
        closures_out[cid]["dev"] = dev_runs[0]["closures"]

        if not skip_full:
            res_full = run_candidate_window(
                spec=spec, window="full", sel_bars=full_bars, days=full_days,
                persist=False, run_tag="run", store_root=store_root, delta_feed=True,
            )
            row["windows"]["full"] = res_full["metrics"]
            row["cost_stress"]["full"] = cost_stress(res_full["metrics"])
            row["next_candle_accuracy"]["full"] = next_candle_accuracy(
                res_full["stream"], full_bars
            )
            row["regime_bucket_full"] = res_full["diagnostics"].get("regime_bucket")
            closures_out[cid]["full"] = res_full["closures"]

        if not skip_oos:
            res_oos = run_candidate_window(
                spec=spec, window="oos", sel_bars=oos_bars, days=oos_days,
                persist=False, run_tag="run", store_root=store_root, delta_feed=True,
            )
            row["windows"]["oos"] = res_oos["metrics"]
            row["cost_stress"]["oos"] = cost_stress(res_oos["metrics"])
            row["next_candle_accuracy"]["oos"] = next_candle_accuracy(
                res_oos["stream"], oos_bars
            )
            row["regime_bucket_oos"] = res_oos["diagnostics"].get("regime_bucket")
            closures_out[cid]["oos"] = res_oos["closures"]

        if (
            not spec.baseline
            and not spec.not_testable_reason
            and spec.candidate_id in PARAM_OVERRIDES
        ):
            for i, ov in enumerate(PARAM_OVERRIDES[spec.candidate_id]):
                alt_params = apply_overrides(spec.params, ov)
                res = run_candidate_window(
                    spec=spec, window="dev", sel_bars=dev_bars, days=dev_days,
                    persist=False, run_tag=f"rob{i}", store_root=store_root,
                    delta_feed=False, params=alt_params,
                )
                row["robustness"].append(
                    {
                        "variant": i + 1,
                        "name": json.dumps(_params_json(alt_params), sort_keys=True),
                        "net_pnl": res["metrics"]["net_pnl"],
                        "round_trips": res["metrics"]["round_trips"],
                        "win_rate": res["metrics"]["win_rate"],
                        "max_drawdown": res["metrics"]["max_drawdown"],
                        "net_at_1x": cost_stress(res["metrics"])["net_at_1x"],
                    }
                )

    classification = {
        cid: classify_candidate(spec, candidates_out[cid].get("windows", {}))
        for cid, spec in CANDIDATES.items()
    }

    payload = {
        "report_version": "5M_STRATEGY_BENCHMARK",
        "dataset": {
            "name": DATASET_PATH.name,
            "data_hash": ds.metadata["data_hash"],
            "bars_total": len(bars),
            "hash_verified": ds.metadata.get("data_hash", "").startswith(DATASET_HASH_PREFIX),
        },
        "windows": windows_out,
        "equivalence": equivalence,
        "candidates": candidates_out,
        "classification": classification,
        "required_table": build_required_table(candidates_out),
        "algo_ready": "NO",
        "no_promotion": True,
        "declared_winner": None,
        "paper_only_banner": PAPER_ONLY_BANNER,
        "store_root": str(store_root),
    }
    return payload, closures_out


def build_required_table(candidates_out: dict) -> list[dict]:
    """The required comparison table (identical methodology for every row)."""
    rows = []
    for cid in CANDIDATES:
        row = candidates_out[cid]
        dev = row.get("windows", {}).get("dev")
        oos = row.get("windows", {}).get("oos")
        if dev is None:
            rows.append(
                {
                    "candidate": cid,
                    "signals": None,
                    "trades": None,
                    "win_rate": None,
                    "gross_pnl": None,
                    "costs": None,
                    "net_pnl": None,
                    "max_dd": None,
                    "oos_net": None,
                    "oos_win_rate": None,
                    "note": row.get("not_testable_reason") or "not run",
                }
            )
            continue
        cs = cost_stress(dev)
        rows.append(
            {
                "candidate": cid,
                "signals": dev["signals_directional"],
                "trades": dev["round_trips"],
                "win_rate": round(100.0 * float(_D(dev["win_rate"])), 4) if dev["win_rate"] is not None else None,
                "gross_pnl": cs["gross_close_pnl"],
                "costs": cs["costs_1x"],
                "net_pnl": dev["net_pnl"],
                "max_dd": dev["max_drawdown"],
                "oos_net": oos["net_pnl"] if oos is not None else None,
                "oos_win_rate": round(100.0 * float(_D(oos["win_rate"])), 4)
                if oos is not None and oos["win_rate"] is not None
                else None,
                "note": row.get("not_testable_reason"),
            }
        )
    return rows


def _params_json(params: object) -> dict:
    if not dataclasses.is_dataclass(params):
        return {"raw": str(params)}
    out = {}
    for field in dataclasses.fields(params):
        v = getattr(params, field.name)
        out[field.name] = str(v) if isinstance(v, Decimal) else v
    return out


# ---------------------------------------------------------------------------
# report writers
# ---------------------------------------------------------------------------


def write_artifacts(repo: Path, payload: dict, closures_out: dict) -> list[Path]:
    out_dir = repo / "reports" / "forensics"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "strategy_benchmark_5m.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_path = out_dir / "strategy_benchmark_5m.md"
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    csv_path = out_dir / "strategy_benchmark_5m_candidates.csv"
    csv_path.write_text(_render_csv(payload, closures_out), encoding="utf-8")
    return [json_path, md_path, csv_path]


def _render_csv(payload: dict, closures_out: dict) -> str:
    lines = ["window,candidate_id,leg,kind,entered_at,exited_at,minutes,quantity,realized"]
    for cid in CANDIDATES:
        for window in ("dev", "full", "oos"):
            for c in closures_out.get(cid, {}).get(window, []) or []:
                lines.append(
                    ",".join(
                        [
                            window,
                            cid,
                            c["leg"],
                            c["kind"],
                            c["entered_at"],
                            c["exited_at"],
                            str(c["minutes"]),
                            str(c["quantity"]),
                            c["realized"],
                        ]
                    )
                )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict) -> str:
    w = payload["windows"]
    eq = payload["equivalence"]
    lines: list[str] = []
    a = lines.append
    a("# 5M Strategy Benchmark — candidate families vs the frozen Donchian 20/10 baseline")
    a("")
    a("**Research-only benchmark. Classification only — never ranking, never promotion.**")
    a("")
    a(f"- `ALGO READY = {payload['algo_ready']}` — this benchmark establishes *evidence*, not readiness.")
    a(f"- `no_promotion = {payload['no_promotion']}`, `declared_winner = {payload['declared_winner']}`")
    a(
        f"- identity proof: subclass + Donchian stream over Dev reproduces the recorded fingerprint "
        f"`{eq['fingerprint_run_A'][:16]}…` — matches recorded `b1be2188…` = "
        f"`{eq['matches_recorded']}`, runs identical = `{eq['fingerprints_identical']}`"
    )
    a("")
    a(
        f"Dataset: `{payload['dataset']['name']}` — bars `{payload['dataset']['bars_total']}`, "
        f"sha256 `{payload['dataset']['data_hash']}` (verified prefix `{DATASET_HASH_PREFIX}`)."
    )
    a("")
    a("## Windows")
    a("")
    a("| window | first day | last day | bars | sessions |")
    a("|---|---:|---:|---:|---:|")
    for name in ("dev", "full", "oos"):
        a(f"| {name} | {w[name]['first']} | {w[name]['last']} | {w[name]['bars']} | {w[name]['sessions']} |")
    a("")
    a("## Methodology (identical economy for every candidate)")
    a("")
    a("- Engine: the real `Directional5MOptionsEngine`; the benchmark subclass overrides only the")
    a("  signal line of `_process_decision` (a precomputed causal stream), mirroring the base body")
    a("  verbatim. Every other behaviour is inherited unchanged (broker, commission 0.0003, slippage")
    a("  0.001 adverse, risk 1%, stop 2%, daily-loss 10000, EOD flatten 15:20, decisions 09:20..15:15).")
    a("- No look-ahead: `stream[i]` is a pure, causal function of `bars[:i+1]` (property-tested).")
    a("- Each window replays from a fresh flat origin, exactly like the recorded baseline run.")
    a("- The Dev baseline row is produced by the subclass run, which the equivalence block proves")
    a("  identical to the recorded artifact; every candidate additionally runs fresh on Dev (twice,")
    a("  determinism), on the full domain, and once on the protected OOS.")
    a("- Candidate P&L is the index-direction label-only P&L (zero premium), the same as the baseline.")
    a("- Protected OOS `2025-10-06..` — pre-registered single run per candidate, no tuning, no re-runs.")
    a("")
    a("## Required table")
    a("")
    a("| Candidate | Signals | Trades | Win Rate | Gross P&L | Costs | Net P&L | Max DD | OOS Net | OOS Win Rate |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in payload["required_table"]:
        a(
            f"| {r['candidate']} | {_fmt(r['signals'])} | {_fmt(r['trades'])} | {_fmt(r['win_rate'])} "
            f"| {_fmt(r['gross_pnl'])} | {_fmt(r['costs'])} | {_fmt(r['net_pnl'])} "
            f"| {_fmt(r['max_dd'])} | {_fmt(r['oos_net'])} | {_fmt(r['oos_win_rate'])} |"
        )
    a("")
    a("Table conventions: Signals = directional signal count (BULLISH + BEARISH); win rates in %;")
    a("Gross P&L = gross-close edge; Costs = slippage estimate + commissions;")
    a("Net P&L = Gross P&L − Costs (== engine 1x net). Identical methodology for every row.")
    a("")
    a("## Classification")
    a("")
    a("| Candidate | verdict | reason |")
    a("|---|---|---|")
    for cid, cls in payload["classification"].items():
        a(f"| {cid} | {cls['verdict']} | {cls['reason']} |")
    a("")
    for cid, row in payload["candidates"].items():
        a(f"## {cid} — {row['label']}")
        a("")
        if row["not_testable_reason"]:
            a(f"**NOT TESTABLE:** {row['not_testable_reason']}")
            a("")
            continue
        a(f"Parameters: `{json.dumps(row['params'], sort_keys=True)}`")
        a("")
        for window in ("dev", "full", "oos"):
            m = row["windows"].get(window)
            if m is None:
                continue
            cs = row["cost_stress"].get(window) or {}
            sig = m["signal_counts"]
            a(
                f"### `{window}` — decision_points `{m['decision_points_total']}`, "
                f"signals BULL `{sig['BULLISH']}` / BEAR `{sig['BEARISH']}` / NEU `{sig['NEUTRAL']}`"
            )
            a("")
            a(
                f"- trades `{m['round_trips']}` win_rate `{m['win_rate']}` wins `{m['wins']}` "
                f"losses `{m['losses']}`"
            )
            a(
                f"- gross_close = `{cs.get('gross_close_pnl')}` costs_1x = `{cs.get('costs_1x')}` "
                f"net = `{m['net_pnl']}` (max DD `{m['max_drawdown_pct']}`)"
            )
            a(
                f"- net at 0x/1x/3x/5x = `{cs.get('net_at_0x')}` / `{cs.get('net_at_1x')}` / "
                f"`{cs.get('net_at_3x')}` / `{cs.get('net_at_5x')}`"
            )
            nca = row["next_candle_accuracy"].get(window)
            if nca:
                a(
                    f"- next-candle accuracy: bull `{nca['bullish_accuracy_pct']}%` "
                    f"({nca['bullish_next_candle_up']}/{nca['bullish_signals']}) "
                    f"bear `{nca['bearish_accuracy_pct']}%` "
                    f"({nca['bearish_next_candle_down']}/{nca['bearish_signals']})"
                )
            a("")
    a("## Robustness (Dev window, pre-registered perturbations)")
    a("")
    a("| candidate | variant | net(1x) | trades | win rate | max DD |")
    a("|---|---:|---:|---:|---:|---:|")
    for cid, row in payload["candidates"].items():
        for rv in row["robustness"]:
            a(f"| {cid} | {rv['name']} | {rv['net_at_1x']} | {rv['round_trips']} | {rv['win_rate']} | {rv['max_drawdown']} |")
    a("")
    a("## Safety")
    a("")
    a("- No live trading; the only broker reachable is `PaperBroker` (`is_live=False`);")
    a("  `PAPER_ONLY_BANNER`: 'paper-only experiment - live_trading disabled'.")
    a("- The Donchian 20/10 baseline modules are imported read-only; nothing adapted or rewritten.")
    a("- All engine stores live under the OS temp directory; only `reports\\forensics` is written.")
    a("- No commits/pushes are performed by the benchmark run itself.")
    return "\n".join(lines)


def _fmt(v) -> str:
    return "" if v is None else str(v)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_5m_strategy_benchmark.py",
        description="Research-only 5M strategy benchmark (never promotes; paper-only).",
    )
    parser.add_argument("--skip-full", action="store_true", help="skip the full-domain window")
    parser.add_argument("--skip-oos", action="store_true", help="skip the protected OOS window")
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parents[3]
    payload, closures_out = run_benchmark(repo, skip_full=args.skip_full, skip_oos=args.skip_oos)
    if not payload["equivalence"]["ok"]:
        print("EQUIVALENCE FAILED — subclass did not reproduce the recorded baseline.")
        return 2
    for path in write_artifacts(repo, payload, closures_out):
        print(f"wrote {path}")
    print("classification:")
    for cid, cls in payload["classification"].items():
        print(f"  {cid}: {cls['verdict']}")
    print("dev determinism (double-run fingerprints identical):")
    for cid, row in payload["candidates"].items():
        det = row.get("dev_determinism")
        if det:
            print(f"  {cid}: {det['fingerprints_identical']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())