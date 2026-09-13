"""Walk-forward adaptive research & learning engine (WS 7.18).

Chronological day iteration. The algorithm used for day D is frozen *before* D
is evaluated; all learning from D (evidence, hypotheses, challengers) only
affects D + 1 or a later day, and only after the explicit challenger/promotion
gate permits it. History is never rewritten (the ledger is append-only) and the
protected out-of-sample period is never loaded or read.

Paper/historical only: this orchestration imports no live-execution code, never
enables live trading, keeps the RiskManager / sizing / stop-loss / PaperBroker /
Portfolio safety layers intact, and never tunes strategy parameters (challenger
parameter sets are frozen catalog constants). All execution during the walk runs
through the deterministic :class:`BacktestEngine` (PaperBroker fills stamped
with the bars' timestamps; the strategy sees only ``bars[:i+1]`` at bar ``i``
via :class:`StrategyEngine`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.backtest.result import BacktestResult
from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.experience.builders import (
    build_advisory,
    build_decision,
    build_outcome_from_round_trip,
    build_record,
)
from fno_ai_paper_trading.experience.enums import (
    DataQualityStatus,
    DecisionStatus,
    ExperienceSourceType,
)
from fno_ai_paper_trading.experience.records import ExperienceRecord
from fno_ai_paper_trading.learning.capture import pair_round_trips
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.strategies import (
    MovingAverageCrossStrategy,
    RegimeFilteredMovingAverageCross,
    Strategy,
)
from fno_ai_paper_trading.strategies.engine import StrategyEngine

from fno_ai_paper_trading.walkforward.catalog import ChallengerSpec
from fno_ai_paper_trading.walkforward.config import FRAMEWORK_VERSION, WalkForwardConfig
from fno_ai_paper_trading.walkforward.gate import (
    WalkForwardGate,
    WalkForwardGateCriteria,
    agg_window_metrics,
)
from fno_ai_paper_trading.walkforward.learning import (
    EvidenceAggregator,
    detect_problem,
    experience_to_slices,
    select_challengers,
)
from fno_ai_paper_trading.walkforward.records import (
    DailyEvolutionRecord,
    EvolutionLedger,
    PromotionRecord,
    SignalOccurrence,
    TradeDetail,
    make_baseline_version,
    make_promoted_version,
)

_ZERO = Decimal("0")

BASELINE_STRATEGY = "moving_average_cross"
CHALLENGER_STRATEGY = "regime_filtered_ma_cross"


def _money(value: Decimal) -> str:
    return format(value, "f")


def _map_signal(signal: Signal) -> str:
    """Map a cash-equity BUY/SELL/HOLD signal to F&O semantics."""
    return {"BUY": "CALL", "SELL": "PUT", "HOLD": "NO_TRADE"}[signal.value]


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


@dataclass(frozen=True)
class DayRun:
    """Deterministic result of replaying one strategy over one trading day."""

    day: date
    signal: str
    occurrences: tuple[SignalOccurrence, ...]
    trades: tuple[TradeDetail, ...]
    pnl: Decimal
    costs: Decimal
    slippage: Decimal
    outcome: str
    win_count: int
    loss_count: int
    open_position_count: int
    max_drawdown_pct: Decimal
    exposure_pct: Decimal
    worst_loss: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    per_regime: Mapping[str, Mapping[str, object]]
    regime: str
    regime_features: Mapping[str, str]

    def metrics(self) -> dict[str, object]:
        """Per-day metrics used by window aggregation (incl. PF components)."""
        return {
            "net_pnl": _money(self.pnl),
            "costs": _money(self.costs),
            "slippage": _money(self.slippage),
            "trades": len(self.trades),
            "wins": self.win_count,
            "losses": self.loss_count,
            "gross_profit": _money(self.gross_profit),
            "gross_loss": _money(self.gross_loss),
            "max_drawdown_pct": _money(self.max_drawdown_pct),
            "exposure_pct": _money(self.exposure_pct),
            "worst_loss": _money(self.worst_loss) if self.worst_loss is not None else None,
            "per_regime": {
                label: dict(chunk) for label, chunk in self.per_regime.items()
            },
        }


@dataclass(frozen=True)
class WalkForwardResult:
    """Everything a run produced, ready for reports and machine consumers."""

    run_id: str
    config: WalkForwardConfig
    config_hash: str
    framework_version: str
    first_day: date | None
    last_day: date | None
    days_available: int
    days_processed: int
    versions: tuple[dict[str, Any], ...]
    parent_of: Mapping[str, str]
    promotions: tuple[dict[str, object], ...]
    challengers: tuple[dict[str, object], ...]
    ledger_records: tuple[dict[str, object], ...]
    champion_totals: Mapping[str, object]
    benchmark: Mapping[str, object]


def _version_by_id(
    versions: Sequence[Mapping[str, Any]], version_id: str
) -> Mapping[str, Any]:
    for version in versions:
        if version["version_id"] == version_id:
            return version
    raise ValueError(f"unknown champion version {version_id!r}")


def _build_version_strategy(version: Mapping[str, Any]) -> Strategy:
    name = str(version.get("strategy_name"))
    params = dict(version.get("strategy_params", {}))
    if name == BASELINE_STRATEGY:
        return MovingAverageCrossStrategy(
            fast=int(params.get("fast", 5)), slow=int(params.get("slow", 21))
        )
    if name == CHALLENGER_STRATEGY:
        return RegimeFilteredMovingAverageCross(**params)
    raise ValueError(f"unsupported champion strategy {name!r}")


def _bar_index_by_timestamp(
    bars: Sequence[MarketPrice], timestamp
) -> int | None:
    for index, bar in enumerate(bars):
        if bar.timestamp == timestamp:
            return index
    return None


class WalkForwardEngine:
    """Day-by-day adaptive research walk with a full evolution ledger.

    Deterministic end to end: no wall-clock time enters any artifact (promotions
    are stamped with the trading day at ``T00:00:00``), resume restores the exact
    checkpoint (config-hash guarded) and a ``stop_after_days`` bound is a
    runtime-only limit that does not participate in the config hash, so a fresh
    full run and a resumed run produce byte-identical ledgers and results.
    """

    def __init__(
        self,
        config: WalkForwardConfig | None = None,
        *,
        detector: RegimeDetector | None = None,
    ) -> None:
        self.config = config or WalkForwardConfig()
        self._bt = self.config.backtest_config()
        self._detector = detector or RegimeDetector(fast=5, slow=21)
        self._backtester = BacktestEngine()
        self._gate = WalkForwardGate(WalkForwardGateCriteria.from_config(self.config))
        self._aggregator = EvidenceAggregator()

    # ------------------------------------------------------------------ run

    def run(
        self,
        days: Sequence[DayBars],
        *,
        out_dir: Path | str | None = None,
        run_name: str = "walkforward",
        resume: bool = False,
        stop_after_days: int | None = None,
    ) -> WalkForwardResult:
        """Run the walk over ``days`` (chronological, strictly before OOS)."""
        cfg = self.config
        ordered = sorted(days, key=lambda d: d.day)
        if cfg.first_date is not None:
            ordered = [d for d in ordered if d.day >= cfg.first_date]
        if cfg.last_date is not None:
            ordered = [d for d in ordered if d.day <= cfg.last_date]
        if cfg.max_research_days is not None:
            ordered = ordered[: cfg.max_research_days]
        if cfg.protected_oos_start is not None:
            ordered = [d for d in ordered if d.day < cfg.protected_oos_start]
        if not ordered:
            raise ValueError(
                "walk-forward domain is empty after applying bounds "
                "(and dropping protected out-of-sample days)"
            )

        out: Path | None = None
        if out_dir is not None:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
        state_path = out / f"{run_name}.state.json" if out is not None else None
        ledger = EvolutionLedger(
            out / f"{run_name}.ledger.jsonl" if out is not None else None
        )

        state = self._load_state(state_path) if resume else None
        if state is None:
            state = self._fresh_state(run_name, cfg)
        elif state.get("config_hash") != cfg.config_hash:
            raise ValueError(
                "resume checkpoint config_hash does not match the current "
                "config; refusing to mix configurations"
            )

        self._aggregator.restore_days(state.get("evidence_days", {}))

        champion_totals = state["champion_totals"]
        domain = len(ordered)
        effective_last = stop_after_days if stop_after_days is not None else domain
        effective_last = min(effective_last, domain)
        start_index = int(state.get("processed_days", 0))
        for index in range(start_index, effective_last):
            day_bars = ordered[index]
            is_last_day = index + 1 >= effective_last
            self._process_day(
                index, day_bars, state, ledger, champion_totals,
                is_last_day=is_last_day,
            )
            if out is not None and state_path is not None:
                self._write_state(state_path, state)

        processed = int(state["processed_days"])
        benchmark = self._benchmark(ordered)
        return WalkForwardResult(
            run_id=run_name,
            config=cfg,
            config_hash=cfg.config_hash,
            framework_version=FRAMEWORK_VERSION,
            first_day=ordered[0].day,
            last_day=ordered[-1].day,
            days_available=domain,
            days_processed=processed,
            versions=tuple(dict(v) for v in state["versions"]),
            parent_of=dict(state.get("parent_of", {})),
            promotions=tuple(dict(p) for p in state.get("promotions", [])),
            challengers=tuple(dict(c) for c in state["challengers"].values()),
            ledger_records=ledger.records(),
            champion_totals=dict(champion_totals),
            benchmark=benchmark,
        )

    # ----------------------------------------------------------- day scope

    def _process_day(
        self,
        index: int,
        day_bars: DayBars,
        state: dict[str, Any],
        ledger: EvolutionLedger,
        champion_totals: dict[str, Any],
        *,
        is_last_day: bool,
    ) -> None:
        cfg = self.config
        day = day_bars.day
        if not day_bars.bars:
            raise ValueError(f"day {day} has no bars to evaluate")

        champion_version_id = str(state["active_version_id"])
        champion_version = _version_by_id(state["versions"], champion_version_id)
        champion_strategy = _build_version_strategy(champion_version)

        # Algorithm used for this day is FROZEN before the day is evaluated.
        champion_run, champion_trips = self._run_day(
            day_bars.bars, champion_strategy
        )
        self._accumulate_champion(champion_totals, champion_run)

        records = self._build_champion_records(
            day_bars.bars, champion_strategy, day, champion_version_id, champion_trips
        )
        slices = experience_to_slices(records)
        self._aggregator.add_day(day, slices)
        state.setdefault("evidence_days", {})[day.isoformat()] = [
            s.to_dict() for s in slices
        ]

        # ---- nightly review (uses ONLY up-to-day evidence, active version) --
        window_stats, window_costs = self._aggregator.window_stats(
            day, cfg.nightly_review_window_days, version=champion_version_id
        )
        totals_stats, totals_costs = self._aggregator.totals()
        problem = detect_problem(
            window_stats, window_costs, totals_stats, totals_costs, cfg
        )
        evidence_snapshot = self._evidence_snapshot(
            day, totals_stats, totals_costs, window_stats
        )

        # ---- challenger day accrual (future-only validation window) --------
        validation_status: list[Mapping[str, object]] = []
        for cid, challenger in state["challengers"].items():
            status = str(challenger.get("status", "VALIDATING"))
            validation_status.append(
                self._validation_status_row(challenger, cfg, day, status)
            )
            if status not in ("VALIDATING", "WINDOW_COMPLETE"):
                continue
            start = date.fromisoformat(str(challenger["validation_start"]))
            end = date.fromisoformat(str(challenger["validation_end"]))
            if day < start:
                continue
            challenger_strategy = _build_version_strategy(
                {
                    "strategy_name": challenger["strategy_name"],
                    "strategy_params": challenger["strategy_params"],
                }
            )
            challenger_run, _ = self._run_day(day_bars.bars, challenger_strategy)
            challenger.setdefault("day_metrics", {})[day.isoformat()] = (
                challenger_run.metrics()
            )
            challenger.setdefault("champion_day_metrics", {})[
                day.isoformat()
            ] = champion_run.metrics()
            if day >= end:
                challenger["status"] = "WINDOW_COMPLETE"

        # ---- research round (evidence-gated challenger generation) ---------
        challenger_generated: list[Mapping[str, object]] = []
        modification_proposed: list[Mapping[str, object]] = []
        hypothesis_generated: list[Mapping[str, object]] = []
        validation_period_assigned: list[Mapping[str, object]] = []
        evidence_supporting: Mapping[str, object] = {}
        is_research_day = (
            (index + 1) >= cfg.min_evidence_days
            and (index + 1 - cfg.min_evidence_days) % cfg.research_cadence_days == 0
        )
        open_keys = {
            str(ch["key"])
            for ch in state["challengers"].values()
            if ch.get("status") in ("VALIDATING", "WINDOW_COMPLETE")
        }
        if is_research_day and len(state["challengers"]) < cfg.max_challengers_total:
            research_stats, research_costs = self._aggregator.window_stats(
                day, cfg.research_window_days, version=champion_version_id
            )
            chosen = select_challengers(
                research_stats, cfg, unavailable_keys=open_keys
            )
            remaining = cfg.max_challengers_total - len(state["challengers"])
            chosen = chosen[:max(remaining, 0)]
            state["research_round"] = int(state.get("research_round", 0)) + 1
            round_no = int(state["research_round"])
            for spec in chosen:
                cid = f"wfc-{spec.key}-r{round_no:03d}"
                validation_start = day + timedelta(days=1)
                validation_end = day + timedelta(days=cfg.validation_window_days)
                if cfg.last_date is not None:
                    validation_end = min(validation_end, cfg.last_date)
                fired, predicate_reasons = spec.predicate(research_stats, cfg)
                self._spawn_challenger(
                    state,
                    cid,
                    spec,
                    day,
                    validation_start,
                    validation_end,
                    champion_version_id,
                )
                challenger_generated.append(
                    {
                        "challenger_id": cid,
                        "key": spec.key,
                        "title": spec.title,
                        "strategy_name": spec.strategy_name,
                        "strategy_params": dict(spec.strategy_params),
                        "generated_on": day.isoformat(),
                    }
                )
                hypothesis_generated.append(
                    {
                        "hypothesis_id": cid,
                        "title": spec.title,
                        "hypothesis": spec.hypothesis,
                        "evidence": {
                            "window_end": day.isoformat(),
                            "window_days": cfg.research_window_days,
                            "reason": "; ".join(predicate_reasons) if fired else "",
                        },
                    }
                )
                modification_proposed.append(
                    {
                        "challenger_id": cid,
                        "title": spec.title,
                        "modification": (
                            "regime-filtered variant of the frozen MA(5,21) "
                            "champion with fixed parameters "
                            f"({', '.join(f'{k}={v}' for k, v in sorted(spec.strategy_params.items()))})"
                        ),
                        "hypothesis": spec.hypothesis,
                    }
                )
                evidence_supporting = {
                    "window_end": day.isoformat(),
                    "window_days": cfg.research_window_days,
                    "reason": "; ".join(predicate_reasons),
                    "evidence": research_stats.to_dict(),
                    "window_costs": _money(research_costs),
                }
                validation_period_assigned.append(
                    {
                        "challenger_id": cid,
                        "window_start": validation_start.isoformat(),
                        "window_end": validation_end.isoformat(),
                        "days_assigned": cfg.validation_window_days,
                        "policy": "future-only validation; the challenger never "
                        "influences the algorithm on or before its creation day",
                    }
                )

        # ---- promotion gate (cadence-bound, evidence-only) -----------------
        boundary = (index + 1) % cfg.promotion_cadence_days == 0
        for cid, challenger in state["challengers"].items():
            if challenger.get("status") != "WINDOW_COMPLETE":
                continue
            self._maybe_gate(
                state, cid, challenger, day,
                boundary=boundary, final_boundary=is_last_day,
            )

        # ---- decide the next trading day's algorithm -----------------------
        next_version_id = str(state["active_version_id"])
        promotions_today = [
            p for p in state["promotions"] if p.get("day") == day.isoformat()
        ]
        gate_decisions_today: list[Mapping[str, object]] = []
        gated_rejections: list[Mapping[str, object]] = []
        for cid, challenger in state["challengers"].items():
            verdict = challenger.get("verdict")
            if verdict is None or verdict.get("day") != day.isoformat():
                continue
            row: Mapping[str, object] = {
                "challenger_id": cid,
                "decision": challenger.get("decision"),
                "reasons": list(verdict.get("reasons", [])),
            }
            gate_decisions_today.append(row)
            if challenger.get("decision") != "PROMOTE":
                gated_rejections.append(row)
        why = self._why_next_day(
            day, next_version_id, promotions_today, gated_rejections, state
        )

        # ---- write the day's ledger row (append-only) ----------------------
        record = DailyEvolutionRecord(
            day=day.isoformat(),
            algorithm_used=champion_version_id,
            parent_algorithm=state.get("parent_of", {}).get(champion_version_id) or None,
            regime=champion_run.regime,
            regime_features=dict(champion_run.regime_features),
            signal=champion_run.signal,
            signal_occurrences=champion_run.occurrences,
            trades=champion_run.trades,
            pnl=_money(champion_run.pnl),
            transaction_costs=_money(champion_run.costs),
            slippage=_money(champion_run.slippage),
            outcome=champion_run.outcome,
            win_count=champion_run.win_count,
            loss_count=champion_run.loss_count,
            open_position_count=champion_run.open_position_count,
            evidence=evidence_snapshot,
            problem_identified=dict(problem),
            hypothesis_generated=list(hypothesis_generated),
            challenger_generated=list(challenger_generated),
            modification_proposed=list(modification_proposed),
            evidence_supporting=dict(evidence_supporting),
            validation_period_assigned=list(validation_period_assigned),
            validation_status=list(validation_status),
            promotion_decision=list(gate_decisions_today),
            next_day_algorithm=next_version_id,
            why_next_day=why,
        )
        ledger.append(record)
        state["processed_days"] = index + 1

    # -------------------------------------------------------------- helpers

    def _fresh_state(self, run_name: str, cfg: WalkForwardConfig) -> dict[str, Any]:
        day = cfg.first_date or date(1970, 1, 1)
        return {
            "run_id": run_name,
            "framework_version": FRAMEWORK_VERSION,
            "config_hash": cfg.config_hash,
            "active_version_id": "model_0",
            "processed_days": 0,
            "research_round": 0,
            "versions": [make_baseline_version(day=day)],
            "parent_of": {"model_0": ""},
            "challengers": {},
            "evidence_days": {},
            "promotions": [],
            "champion_totals": {
                "days": 0,
                "net_pnl": _ZERO,
                "costs": _ZERO,
                "slippage": _ZERO,
                "round_trips": 0,
                "wins": 0,
                "losses": 0,
                "exposure_units": _ZERO,
                "max_drawdown_pct": _ZERO,
            },
        }

    def _load_state(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def _write_state(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(json.dumps(state, sort_keys=True, default=_json_default))
        path.write_text(
            json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
        )

    def _run_day(
        self, bars: Sequence[MarketPrice], strategy: Strategy
    ) -> tuple[DayRun, list[tuple[Trade, Trade]]]:
        """Replay one day and return its run summary plus completed round trips."""
        bars_list = list(bars)
        signals = StrategyEngine(strategy).evaluate(bars_list)
        result = self._backtester.run(bars_list, strategy, self._bt, signals=signals)
        trips, _open_count = pair_round_trips(result.trades)
        run = self._build_day_run(bars_list, result, signals, trips)
        return run, trips

    def _build_day_run(
        self,
        bars: Sequence[MarketPrice],
        result: BacktestResult,
        signals: Sequence[Any],
        trips: Sequence[tuple[Trade, Trade]],
    ) -> DayRun:
        day = bars[0].timestamp.date()
        occurrences: list[SignalOccurrence] = []
        for signal in signals:
            side = _map_signal(signal.signal)
            if side != "NO_TRADE":
                occurrences.append(
                    SignalOccurrence(
                        timestamp=signal.timestamp.isoformat(), side=side
                    )
                )
        last_signal = signals[-1].signal if signals else Signal.HOLD

        trade_lines: list[TradeDetail] = []
        win_count = 0
        loss_count = 0
        gross_profit = _ZERO
        gross_loss = _ZERO
        worst: Decimal | None = None
        per_regime: dict[str, dict[str, object]] = {}
        for entry, exit_ in trips:
            side = "CALL" if entry.side is OrderSide.BUY else "PUT"
            realized = exit_.realized_pnl
            regime = self._regime_label_at(bars, entry.executed_at)
            trade_costs = entry.commission + exit_.commission
            if realized > _ZERO:
                win_count += 1
                gross_profit += realized
            elif realized < _ZERO:
                loss_count += 1
                gross_loss += -realized
                if worst is None or realized < worst:
                    worst = realized
            bucket = per_regime.setdefault(
                regime, {"trades": 0, "wins": 0, "realized_pnl": _ZERO}
            )
            bucket["trades"] = int(bucket["trades"]) + 1
            bucket["wins"] = int(bucket["wins"]) + (1 if realized > _ZERO else 0)
            bucket["realized_pnl"] = Decimal(str(bucket["realized_pnl"])) + realized
            trade_lines.append(
                TradeDetail(
                    entry_time=entry.executed_at.isoformat(),
                    exit_time=exit_.executed_at.isoformat(),
                    side=side,
                    entry_price=_money(entry.price),
                    exit_price=_money(exit_.price),
                    quantity=int(entry.quantity),
                    realized_pnl=_money(realized),
                    commission=_money(trade_costs),
                    costs=_money(trade_costs),
                    entry_regime=regime,
                )
            )

        per_regime_json = {
            label: {
                "trades": int(chunk["trades"]),
                "wins": int(chunk["wins"]),
                "realized_pnl": _money(Decimal(str(chunk["realized_pnl"]))),
            }
            for label, chunk in per_regime.items()
        }

        equity = result.equity_curve
        if equity:
            positions = sum(1 for ep in equity if ep.unrealized_pnl != _ZERO)
            exposure = (
                Decimal(positions) * Decimal("100") / Decimal(len(equity))
            )
        else:
            exposure = _ZERO

        return DayRun(
            day=day,
            signal=_map_signal(last_signal),
            occurrences=tuple(occurrences),
            trades=tuple(trade_lines),
            pnl=result.total_pnl,
            costs=result.total_commission + result.slippage_cost,
            slippage=result.slippage_cost,
            outcome=(
                "NO_TRADE"
                if not trips
                else ("WIN" if result.total_pnl > _ZERO else "LOSS")
            ),
            win_count=win_count,
            loss_count=loss_count,
            open_position_count=0,
            max_drawdown_pct=result.max_drawdown_pct,
            exposure_pct=exposure,
            worst_loss=worst,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            per_regime=per_regime_json,
            regime=self._regime_label(bars),
            regime_features=self._regime_features(bars),
        )

    # ------------------------------------------------------------- evidence

    def _build_champion_records(
        self,
        bars: Sequence[MarketPrice],
        strategy: Strategy,
        day: date,
        version_id: str,
        trips: Sequence[tuple[Trade, Trade]],
    ) -> list[ExperienceRecord]:
        """Mirror :func:`capture.capture_from_day_bars` for this day's trades.

        Only completed round trips become evidence; positions still open at the
        end of the day are counted but never recorded.
        """
        records: list[ExperienceRecord] = []
        bars_list = list(bars)
        for occurrence, (entry, exit_) in enumerate(trips):
            regime_label = self._regime_label_at(bars_list, entry.executed_at)
            decision = build_decision(
                instrument=entry.instrument,
                decision_timestamp=entry.executed_at,
                timeframe="5m",
                signal=Signal.BUY if entry.side is OrderSide.BUY else Signal.SELL,
                confidence="1.0",
                strategy_name=strategy.name,
                strategy_version=version_id,
                data_reference=day.isoformat(),
                feature_version=FRAMEWORK_VERSION,
                decision_status=DecisionStatus.EXECUTED,
                data_quality=DataQualityStatus.VALIDATED,
                regime_label=regime_label,
            )
            outcome = build_outcome_from_round_trip(entry, exit_)
            record = build_record(
                decision=decision,
                source=ExperienceSourceType.HISTORICAL_REPLAY,
                recorded_at=exit_.executed_at,
                occurrence=occurrence,
                outcome=outcome,
                advisory=build_advisory(None),
                source_detail="walkforward-day-capture",
            )
            records.append(record)
        return records

    def _evidence_snapshot(
        self,
        day: date,
        totals: Any,
        totals_costs: Decimal,
        window: Any,
    ) -> dict[str, object]:
        completed = totals.total.trades
        wins = totals.total.wins
        win_rate = Decimal(wins) / Decimal(completed) if completed > 0 else None
        return {
            "day": day.isoformat(),
            "completed_total": completed,
            "wins_total": wins,
            "losses_total": completed - wins,
            "net_pnl_total": _money(totals.total.realized_pnl),
            "transaction_costs_total": _money(totals_costs),
            "win_rate": _money(win_rate) if win_rate is not None else None,
            "trailing_window": window.to_dict(),
        }

    def _accumulate_champion(
        self, totals: dict[str, Any], run: DayRun
    ) -> None:
        totals["days"] = int(totals.get("days", 0)) + 1
        totals["net_pnl"] = Decimal(str(totals.get("net_pnl", "0"))) + run.pnl
        totals["costs"] = Decimal(str(totals.get("costs", "0"))) + run.costs
        totals["slippage"] = Decimal(str(totals.get("slippage", "0"))) + run.slippage
        totals["round_trips"] = int(totals.get("round_trips", 0)) + len(run.trades)
        totals["wins"] = int(totals.get("wins", 0)) + run.win_count
        totals["losses"] = int(totals.get("losses", 0)) + run.loss_count
        totals["exposure_units"] = (
            Decimal(str(totals.get("exposure_units", "0"))) + run.exposure_pct
        )
        totals["max_drawdown_pct"] = max(
            Decimal(str(totals.get("max_drawdown_pct", "0"))),
            run.max_drawdown_pct,
        )

    # ------------------------------------------------------------ challengers

    def _spawn_challenger(
        self,
        state: dict[str, Any],
        cid: str,
        spec: ChallengerSpec,
        created_on: date,
        validation_start: date,
        validation_end: date,
        parent_version_id: str,
    ) -> None:
        state["challengers"][cid] = {
            "challenger_id": cid,
            "key": spec.key,
            "title": spec.title,
            "hypothesis": spec.hypothesis,
            "strategy_name": spec.strategy_name,
            "strategy_params": dict(spec.strategy_params),
            "created_on": created_on.isoformat(),
            "validation_start": validation_start.isoformat(),
            "validation_end": validation_end.isoformat(),
            "parent_version_id": parent_version_id,
            "status": "VALIDATING",
            "decision": None,
            "verdict": None,
            "promoted_version_id": None,
            "day_metrics": {},
            "champion_day_metrics": {},
        }

    def _maybe_gate(
        self,
        state: dict[str, Any],
        cid: str,
        challenger: dict[str, Any],
        day: date,
        *,
        boundary: bool,
        final_boundary: bool,
    ) -> None:
        if challenger.get("decision") is not None:
            return
        if not boundary and not final_boundary:
            return
        challenger_metrics = agg_window_metrics(challenger.get("day_metrics", {}))
        champion_metrics = agg_window_metrics(
            challenger.get("champion_day_metrics", {})
        )
        verdict = self._gate.evaluate(
            challenger_metrics,
            champion_metrics,
            cid,
            base_equity=self.config.initial_capital,
        )
        challenger["decision"] = verdict.decision
        challenger["verdict"] = {
            "day": day.isoformat(),
            "decision": verdict.decision,
            "reasons": list(verdict.reasons),
            "metrics": dict(verdict.metrics),
        }

        promoted_version = None
        if verdict.promoted:
            version_id = f"model_{len(state['versions'])}"
            promoted_version = make_promoted_version(
                version_id=version_id,
                day=day,
                strategy_name=str(challenger["strategy_name"]),
                strategy_params=challenger["strategy_params"],
                description=str(challenger["title"]),
                evidence=dict(verdict.metrics),
            )
            previous = _version_by_id(state["versions"], state["active_version_id"])
            previous["status"] = "RETIRED"
            previous["retired_at"] = f"{day.isoformat()}T00:00:00"
            state["versions"].append(promoted_version)
            state["parent_of"][version_id] = str(challenger["parent_version_id"])
            state["active_version_id"] = version_id
            challenger["status"] = "PROMOTED"
            challenger["promoted_version_id"] = version_id
        elif verdict.decision == "INSUFFICIENT_EVIDENCE":
            challenger["status"] = "INSUFFICIENT_EVIDENCE"
        else:
            challenger["status"] = "REJECTED"

        state["promotions"].append(
            PromotionRecord(
                challenger_id=cid,
                day=day.isoformat(),
                decision=str(verdict.decision),
                promoted_version=(
                    str(promoted_version["version_id"]) if promoted_version else None
                ),
                reasons=tuple(verdict.reasons),
                metrics=dict(verdict.metrics),
            ).to_dict()
        )

    def _validation_status_row(
        self,
        challenger: Mapping[str, Any],
        cfg: WalkForwardConfig,
        day: date,
        status: str,
    ) -> Mapping[str, object]:
        return {
            "challenger_id": challenger["challenger_id"],
            "key": challenger["key"],
            "status": status,
            "validated_days": len(challenger.get("day_metrics", {})),
            "required_days": cfg.validation_window_days,
            "window_start": challenger["validation_start"],
            "window_end": challenger["validation_end"],
        }

    def _why_next_day(
        self,
        day: date,
        next_version_id: str,
        promotions_today: Sequence[Mapping[str, object]],
        gated_rejections: Sequence[Mapping[str, object]],
        state: Mapping[str, Any],
    ) -> str:
        if promotions_today:
            promoted = [
                f"{p.get('challenger_id')} -> {p.get('promoted_version')}"
                for p in promotions_today
            ]
            return (
                f"promotion approved at the end of {day.isoformat()}: "
                f"{', '.join(promoted)}; from the next trading day the champion "
                f"becomes {next_version_id}"
            )
        if gated_rejections:
            parts = [
                f"{g.get('challenger_id')} {g.get('decision')}: "
                f"{'; '.join(str(r) for r in g.get('reasons', []))}"
                for g in gated_rejections
            ]
            return (
                f"no promotion at the end of {day.isoformat()}: " + " | ".join(parts)
            )
        return (
            f"no challenger was gated today; the algorithm for the next trading "
            f"day stays {next_version_id} unchanged"
        )

    # ---------------------------------------------------------------- util

    def _regime_label_at(
        self, bars: Sequence[MarketPrice], timestamp
    ) -> str:
        index = _bar_index_by_timestamp(bars, timestamp)
        if index is None:
            return "UNKNOWN"
        regime = self._detector.detect_prefix(bars, index)
        return regime.label if regime is not None else "UNKNOWN"

    def _regime_label(self, bars: Sequence[MarketPrice]) -> str:
        if not bars:
            return "UNKNOWN"
        regime = self._detector.detect_prefix(bars, len(bars) - 1)
        return regime.label if regime is not None else "UNKNOWN"

    def _regime_features(self, bars: Sequence[MarketPrice]) -> Mapping[str, str]:
        if not bars:
            return {}
        regime = self._detector.detect_prefix(bars, len(bars) - 1)
        if regime is None:
            return {"regime": "unknown"}
        return {key: str(value) for key, value in regime.features.items()}

    def _benchmark(self, days: Sequence[DayBars]) -> Mapping[str, object]:
        from fno_ai_paper_trading.research.benchmark import buy_and_hold

        closes: list[MarketPrice] = []
        for day_bars in days:
            if not day_bars.bars:
                continue
            first = day_bars.bars[0]
            last = day_bars.bars[-1]
            closes.append(
                MarketPrice(
                    instrument=first.instrument,
                    timestamp=last.timestamp,
                    open=last.close,
                    high=last.close,
                    low=last.close,
                    close=last.close,
                    volume=last.volume,
                )
            )
        if not closes:
            return {"note": "no bars available for the buy-and-hold reference"}
        benchmark = buy_and_hold(
            closes,
            self.config.initial_capital,
            self.config.quantity,
            name="buy_and_hold",
        )
        return {
            "name": benchmark.name,
            "quantity": benchmark.quantity,
            "entry_close": _money(benchmark.entry_price),
            "final_close": _money(benchmark.final_price),
            "net_pnl": _money(
                benchmark.final_equity - self.config.initial_capital
            ),
            "total_return_pct": _money(benchmark.total_return_pct),
            "max_drawdown_pct": _money(benchmark.max_drawdown_pct),
            "cost_adjusted": False,
            "note": "buy-and-hold of the day-close series, gross of costs and "
            "slippage (reference only)",
        }