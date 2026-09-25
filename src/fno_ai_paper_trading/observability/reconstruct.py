"""Deterministic decision-journal reconstruction from discovery artifacts.

Replays the SAME dataset, definition, configuration and engine the discovery
cycle used (decision-time signals over the full series, per-day fresh-portfolio
replays) while attaching the engine's observability journal. The replay is
validated against the cycle's *persisted* train scorecard net P&L; only a
bit-exact match yields ``reconstruction_status == "exact"``.

``decision_source`` is always ``reconstructed`` for these research journals —
per-candle decisions were never originally persisted. When the net matches the
authoritative aggregate the reconstruction is ``exact``; otherwise the journal
is still written but marked ``exact=False`` so the GUI can surface it honestly.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.discovery.backtest import _trade_records
from fno_ai_paper_trading.discovery.catalog import (
    CandidateDefinition,
    build_params,
    resolve_signal_fn,
)
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.strategies.base import SignalResult, Strategy
from fno_ai_paper_trading.walkforward.config import WalkForwardConfig

from fno_ai_paper_trading.observability.schemas import (
    ALL_KNOWN_REASONS,
    MECHANICAL_CODES,
)

EXIT_REASON_CODE = "STOP_LOSS (protective 2%)"


class _ReplayStrategy(Strategy):
    """Analyze is never called: the engine receives precomputed signals."""

    name = "discovery.replay"

    def analyze(self, bars):  # pragma: no cover - disabled by design
        raise RuntimeError("analyze() disabled; engine must receive signals=")


def _group_days_by_offsets(bars, start: date, end: date) -> tuple[list[list], dict[date, int]]:
    """Chronological day-bar groups in [start, end] plus global offsets per day."""
    offsets: dict[date, int] = {}
    by_day: dict[date, list] = {}
    prev: date | None = None
    for idx, bar in enumerate(bars):
        day = bar.timestamp.date()
        if day != prev:
            offsets[day] = idx
            prev = day
        if start <= day <= end:
            by_day.setdefault(day, []).append(bar)
    return [by_day[d] for d in sorted(by_day)], offsets


def _classify(reason: str) -> tuple[str, str]:
    """Map a provider reason to (final_decision, canonical_code).

    Providers already emit the frozen code set; anything unrecognised is a
    plain HOLD. HOLDING is a mechanical hold (position open, no exit signal).
    """
    code = (reason or "").strip()
    if code in MECHANICAL_CODES:
        if code == "HOLDING":
            return "HOLD", code
        return "NO TRADE", code
    if code in ALL_KNOWN_REASONS:
        return "NO TRADE", code
    return "HOLD", (code or "no signal")


def _target_from_meta(meta: dict[str, Any]) -> float | None:
    for key in ("target_price", "target"):
        value = meta.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _deterministic_trade_id(run_id: str, candidate_id: str, entry_time: str, entry_price: float) -> str:
    """Stable trade identity across re-runs (independent of random engine ids)."""
    canonical = f"{run_id}|{candidate_id}|{entry_time}|{entry_price}"
    return f"TRD-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


@dataclass
class ReconstructedCandidate:
    """All per-candle decisions + reconstructed round trips for one candidate."""

    candidate_id: str
    version: str
    decisions: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    day_nets: dict[str, str] = field(default_factory=dict)
    train_net: Decimal = Decimal("0")
    train_net_ok: bool = False


def reconstruct_candidate(
    defn: CandidateDefinition,
    bars: list,
    config: WalkForwardConfig,
    *,
    start: date,
    end: date,
    train_end: date,
    run_id: str,
    dataset_id: str,
    source_artifact: str,
    source_artifact_hash: str,
    initial_capital: Decimal = Decimal("100000"),
) -> ReconstructedCandidate:
    """Replay a candidate over [start, end] exactly like the discovery cycle,
    capturing a deterministic per-candle decision journal."""
    signal_fn = resolve_signal_fn(defn)
    params = build_params(defn)
    engine = BacktestEngine()
    backtest_config = config.backtest_config()
    strategy = _ReplayStrategy()

    full_bars = list(bars)
    full_signals = signal_fn(full_bars, params)
    if len(full_signals) != len(full_bars):
        raise ValueError(
            f"{defn.candidate_id}: provider returned {len(full_signals)} signals for {len(full_bars)} bars"
        )
    sig_by_ts = {
        (s.timestamp.isoformat() if s.timestamp is not None else None): s
        for s in full_signals
    }
    day_groups, offsets = _group_days_by_offsets(full_bars, start, end)

    ts_to_index: dict[str, int] = {}
    for idx, bar in enumerate(full_bars):
        if start <= bar.timestamp.date() <= end:
            ts_to_index[bar.timestamp.isoformat()] = idx

    decisions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    day_nets: dict[str, str] = {}
    train_net = Decimal("0")
    id_map: dict[str, str] = {}

    for day_bars in day_groups:
        day = day_bars[0].timestamp.date()
        offset = offsets[day]
        day_signals = full_signals[offset:offset + len(day_bars)]
        journal: list[dict[str, Any]] = []
        result = engine.run(
            day_bars,
            strategy,
            config=backtest_config,
            signals=day_signals,
            journal=journal,
        )
        day_net = result.total_pnl
        day_nets[day.isoformat()] = format(day_net, "f")
        if day <= train_end:
            train_net += day_net

        segment = "train" if day <= train_end else "validation"

        records, _open = _trade_records(day, day_signals, result.trades)
        round_trips, _open2 = pair_round_trips(result.trades)
        for entry_trade, exit_trade in round_trips:
            det = _deterministic_trade_id(
                run_id, defn.candidate_id,
                entry_trade.executed_at.isoformat(), _opt_float(entry_trade.price),
            )
            id_map[entry_trade.trade_id] = det
            id_map[exit_trade.trade_id] = det

        for local_idx, (bar, sig, entry) in enumerate(
            zip(day_bars, day_signals, journal)
        ):
            ts = bar.timestamp.isoformat()
            frame = ts_to_index[ts]
            meta = dict(sig.meta) if isinstance(sig.meta, dict) else {}
            features = {
                k: v for k, v in meta.items()
                if k not in ("regime", "volatility_bucket")
            }
            stop_price = None
            pos = entry.get("position_after") or {}
            if pos.get("side") not in (None, "FLAT") and pos.get("entry_price"):
                try:
                    stop_price = round(
                        float(pos["entry_price"]) * (1.0 - float(config.stop_loss_pct)), 2
                    )
                except (TypeError, ValueError):
                    stop_price = None

            if entry.get("stop_fill"):
                final, final_reason = "SELL", EXIT_REASON_CODE
                exit_price_val = _opt_float(entry.get("stop_price"))
                status = "FILLED"
            elif entry.get("order_side"):
                final = entry["order_side"]
                final_reason = sig.reason or final
                exit_price_val = _opt_float(entry.get("fill_price"))
                status = "FILLED"
            elif entry.get("actionable") and entry.get("risk_approved") is False:
                final, final_reason = "NO TRADE", "; ".join(entry.get("risk_reasons") or ["RISK_LIMIT"])
                exit_price_val = None
                status = "BLOCKED"
            else:
                final, code = _classify(sig.reason)
                final_reason = sig.reason or code
                exit_price_val = None
                status = "NO_ORDER"

            raw_rid = entry.get("entry_trade_id") or entry.get("exit_trade_id")
            trade_id = id_map.get(raw_rid) if raw_rid else None
            decisions.append(
                {
                    "run_id": run_id,
                    "run_type": "discovery",
                    "timestamp": ts,
                    "candle_index": frame,
                    "dataset_id": dataset_id,
                    "candidate_id": defn.candidate_id,
                    "algorithm_id": f"{defn.candidate_id}.{defn.version}",
                    "strategy_version": defn.version,
                    "configuration_version": defn.version,
                    "config_hash": config.config_hash,
                    "segment": segment,
                    "open": _num(bar.open),
                    "high": _num(bar.high),
                    "low": _num(bar.low),
                    "close": _num(bar.close),
                    "volume": _num(bar.volume),
                    "regime": meta.get("regime"),
                    "volatility": meta.get("volatility_bucket", meta.get("volatility")),
                    "features": features,
                    "raw_signal": entry.get("raw_signal"),
                    "final_decision": final,
                    "final_reason": final_reason,
                    "position_before": entry.get("position_before"),
                    "position_after": entry.get("position_after"),
                    "risk_checks": entry.get("risk_checks"),
                    "execution_status": status,
                    "trade_id": trade_id,
                    "entry_price": _opt_float(entry.get("fill_price")) if trade_id and not entry.get("exit_trade_id") else None,
                    "exit_price": exit_price_val,
                    "stop_price": stop_price,
                    "target_price": _target_from_meta(meta),
                    "realized_pnl": _opt_float(entry.get("realized_pnl")),
                    "unrealized_pnl": _opt_float(entry.get("unrealized_pnl")),
                    "decision_source": "reconstructed",
                    "reconstruction_status": "exact",
                    "source_artifact": source_artifact,
                    "source_artifact_hash": source_artifact_hash,
                }
            )

        for record, (entry_trade, exit_trade) in zip(records, round_trips):
            det = id_map[entry_trade.trade_id]
            trades.append(
                {
                    "trade_id": det,
                    "run_id": run_id,
                    "candidate_id": defn.candidate_id,
                    "algorithm_id": f"{defn.candidate_id}.{defn.version}",
                    "entry_time": entry_trade.executed_at.isoformat(),
                    "exit_time": exit_trade.executed_at.isoformat(),
                    "side": record.side,
                    "entry_price": _opt_float(entry_trade.price),
                    "exit_price": _opt_float(exit_trade.price),
                    "quantity": entry_trade.quantity,
                    "commission": _opt_float(record.costs),
                    "slippage": None,
                    "gross_pnl": _opt_float(record.gross),
                    "net_pnl": _opt_float(record.net_pnl),
                    "exit_reason": record.exit_reason,
                    "metadata": {
                        "day": record.day.isoformat(),
                        "entry_meta": record.entry_meta,
                        "entry_trade_id": det,
                        "exit_trade_id": det,
                    },
                }
            )

    status = ReconstructedCandidate(
        candidate_id=defn.candidate_id,
        version=defn.version,
        decisions=decisions,
        trades=trades,
        day_nets=day_nets,
        train_net=train_net,
        train_net_ok=False,
    )
    return status


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None