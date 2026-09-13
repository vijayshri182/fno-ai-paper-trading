"""Generic, decision-time evaluation harness for challenger candidates (WS 7.16).

This module runs any candidate *signal provider* through the deterministic
``BacktestEngine`` (paper fills, portfolio, risk manager, stop-loss) and produces
the same style of analytics as the champion's ``evaluate_continuous``, but for an
arbitrary signal stream — one entry per bar, computed strictly from ``bars[:i+1]``.

Research protocol enforced here:
    * **Protected OOS.** Segments are `design` (training/insight),
      `validation`, and `protected_oos` (single-use, untouched during selection).
      Callers must not use ``protected_oos`` results to select candidates.
    * **No-look-ahead.** Providers only get ``bars[:i+1]`` per bar index.
    * **Determinism.** Same inputs -> same outputs (Decimal everywhere).

Nothing here places orders or changes risk/execution code. PAPER ONLY.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from fno_ai_paper_trading.backtest.config import BacktestConfig
from fno_ai_paper_trading.backtest.engine import BacktestEngine
from fno_ai_paper_trading.evaluation.model_performance import (
    _build_round_trips,
    bars_per_year_for,
)
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.promotion.gate import DeltaView, PromotionGate
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.research.metrics import compute_metrics
from fno_ai_paper_trading.strategies.base import SignalResult


@dataclass(frozen=True)
class CandidateRun:
    """Analytics for one strategy replay over one bar segment."""

    name: str
    bars: list[MarketPrice]
    config: BacktestConfig
    result: object
    metrics: object
    round_trips: list
    open_trades: int
    reconciled_pnl: Decimal | None
    reconciliation_ok: bool
    min_equity: Decimal

    def summary_dict(self) -> dict[str, object]:
        m = self.metrics
        price_pnls = [trip.price_pnl for trip in self.round_trips]
        avg_price = (
            sum(price_pnls, Decimal("0")) / Decimal(len(price_pnls))
            if price_pnls
            else Decimal("0")
        )
        cost = m.transaction_costs / Decimal(m.num_trades) if m.num_trades else Decimal("0")
        gross = m.net_pnl + m.transaction_costs if m.num_trades else Decimal("0")
        per_trade_gross = gross / Decimal(m.num_trades) if m.num_trades else Decimal("0")
        return {
            "name": self.name,
            "start": self.bars[0].timestamp.isoformat(),
            "end": self.bars[-1].timestamp.isoformat(),
            "bars": len(self.bars),
            "num_trades": m.num_trades,
            "winning": m.winning_trades,
            "losing": m.losing_trades,
            "win_rate_pct": str(m.win_rate),
            "net_pnl": str(m.net_pnl),
            "net_return_pct": str(m.net_return_pct),
            "max_drawdown_pct": str(m.max_drawdown_pct),
            "max_drawdown": str(m.max_drawdown),
            "transaction_costs": str(m.transaction_costs),
            "commission": str(m.total_commission),
            "slippage": str(m.slippage_cost),
            "gross_profit": str(m.gross_profit),
            "gross_loss": str(m.gross_loss),
            "profit_factor": str(m.profit_factor),
            "expectancy_net": str(m.expectancy) if m.expectancy is not None else "n/a",
            "avg_price_pnl_per_trade": str(avg_price),
            "per_trade_cost": str(cost),
            "per_trade_gross": str(per_trade_gross),
            "exposure_pct": str(m.exposure_pct),
            "sharpe": str(m.sharpe_ratio) if m.sharpe_ratio is not None else "n/a",
            "min_equity": str(self.min_equity),
            "open_trades": self.open_trades,
            "reconciliation_ok": self.reconciliation_ok,
        }


def evaluate_strategy(
    bars: Sequence[MarketPrice],
    signals: Sequence[SignalResult],
    *,
    name: str,
    config: BacktestConfig | None = None,
    engine: BacktestEngine | None = None,
    strategy=None,
    detector: RegimeDetector | None = None,
) -> CandidateRun:
    """Replay ``signals`` over ``bars`` and build full analytics.

    ``strategy`` is only used to satisfy the engine's interface; when ``signals``
    is not None the engine never calls ``strategy.analyze``. The engine still
    enforces ``len(signals) == len(bars)`` and the no-look-ahead contract.
    """
    engine = engine or BacktestEngine()
    config = config or BacktestConfig()
    detector = detector or RegimeDetector(fast=5, slow=21)

    sequence = list(bars)
    if len(signals) != len(sequence):
        raise ValueError(f"signals ({len(signals)}) must match bars ({len(sequence)})")
    result = engine.run(sequence, strategy if strategy is not None else object(), config, signals=signals)
    metrics = compute_metrics(result, bars_per_year=bars_per_year_for(sequence))
    trips, open_count = _build_round_trips(sequence, result.trades, detector)
    if open_count == 0 and trips:
        reconciled = sum((trip.net_pnl for trip in trips), Decimal("0"))
        reconciliation_ok = reconciled == result.total_pnl
    else:
        reconciled = None
        reconciliation_ok = False
    min_equity = min((p.equity for p in result.equity_curve), default=Decimal("0"))
    return CandidateRun(
        name=name,
        bars=sequence,
        config=config,
        result=result,
        metrics=metrics,
        round_trips=trips,
        open_trades=open_count,
        reconciled_pnl=reconciled,
        reconciliation_ok=reconciliation_ok,
        min_equity=min_equity,
    )


def date_split_bars(
    bars: Sequence[MarketPrice],
    *,
    design_end: datetime,
    validation_end: datetime,
) -> tuple[list[MarketPrice], list[MarketPrice], list[MarketPrice]]:
    """Chronological split into design / validation / protected-OOS by timestamp."""
    design: list[MarketPrice] = []
    validation: list[MarketPrice] = []
    protected: list[MarketPrice] = []
    for bar in bars:
        if bar.timestamp < design_end:
            design.append(bar)
        elif bar.timestamp < validation_end:
            validation.append(bar)
        else:
            protected.append(bar)
    return design, validation, protected


def run_candidate_plan(
    bars: Sequence[MarketPrice],
    *,
    name: str,
    provider,
    params: Mapping[str, object],
    config: BacktestConfig | None = None,
    design_end: datetime,
    validation_end: datetime,
    detector: RegimeDetector | None = None,
) -> dict[str, object]:
    """Evaluate one candidate over all four segments (full/design/validation/OOS).

    ``protected_oos`` is included for completeness but must not be used for
    selection — see module docstring.
    """
    config = config or BacktestConfig()
    design, validation, protected = date_split_bars(
        bars, design_end=design_end, validation_end=validation_end
    )
    segments: dict[str, list[MarketPrice]] = {
        "design": design,
        "validation": validation,
        "protected_oos": protected,
        "full": list(bars),
    }
    out: dict[str, object] = {
        "candidate": name,
        "params": dict(params),
        "segments": {},
    }
    segment_results: dict[str, CandidateRun] = {}
    for seg_name, segment in segments.items():
        if not segment:
            continue
        signals = provider(list(segment), **dict(params))
        run = evaluate_strategy(
            segment,
            signals,
            name=name,
            config=config,
            detector=detector,
        )
        segment_results[seg_name] = run
        out["segments"][seg_name] = run.summary_dict()
    out["runs"] = segment_results
    return out


def trading_days(bars: Sequence[MarketPrice]) -> int:
    return len({bar.timestamp.date().isoformat() for bar in bars})


def build_delta_views(
    champion_plan: dict[str, object],
    candidate_plan: dict[str, object],
) -> tuple[dict[str, dict[str, DeltaView]], dict[str, int]]:
    """Period -> challenger -> DeltaView, plus trading-day counts, for the gate.

    Period keys follow the gate contract: ``validation`` and ``out_of_sample``
    (the protected OOS segment).
    """
    deltas: dict[str, dict[str, DeltaView]] = {}
    counts: dict[str, int] = {}
    period_map = {"validation": "validation", "protected_oos": "out_of_sample"}
    for seg_period, gate_period in period_map.items():
        seg = champion_plan["segments"].get(seg_period)
        cand_seg = candidate_plan["segments"].get(seg_period)
        if not seg or not cand_seg:
            continue
        champ_run: CandidateRun = champion_plan["runs"][seg_period]
        cand_run: CandidateRun = candidate_plan["runs"][seg_period]
        deltas[gate_period] = {
            candidate_plan["candidate"]: DeltaView(
                challenger_name=str(candidate_plan["candidate"]),
                period=gate_period,
                champion_net_pnl=champ_run.metrics.net_pnl,
                challenger_net_pnl=cand_run.metrics.net_pnl,
                champion_max_drawdown_pct=champ_run.metrics.max_drawdown_pct,
                challenger_max_drawdown_pct=cand_run.metrics.max_drawdown_pct,
                beats_champion=cand_run.metrics.net_pnl > champ_run.metrics.net_pnl,
            )
        }
    for seg_period, gate_period in period_map.items():
        if seg_period in champion_plan["runs"]:
            counts[gate_period] = trading_days(champion_plan["runs"][seg_period].bars)
    return deltas, counts


def promotion_verdict(
    champion_plan: dict[str, object],
    candidate_plan: dict[str, object],
    *,
    criteria=None,
) -> object:
    """Apply the existing :class:`PromotionGate` to candidate vs champion."""
    deltas, counts = build_delta_views(champion_plan, candidate_plan)
    return PromotionGate().evaluate(deltas, counts, str(candidate_plan["candidate"]), criteria=criteria)


def cost_scan(
    bars: Sequence[MarketPrice],
    *,
    name: str,
    provider,
    params: Mapping[str, object],
    base_config: BacktestConfig,
    detector: RegimeDetector | None = None,
) -> list[dict[str, object]]:
    """Bounded cost scenarios around the configured friction for one strategy."""
    scenarios: dict[str, tuple[Decimal, Decimal]] = {
        "zero_cost": (Decimal("0"), Decimal("0")),
        "low_cost": (base_config.commission_rate / 2, base_config.slippage_rate / 2),
        "base": (base_config.commission_rate, base_config.slippage_rate),
        "high_cost": (base_config.commission_rate * 2, base_config.slippage_rate * 2),
    }
    rows: list[dict[str, object]] = []
    base_trades: int | None = None
    for scenario, (commission, slippage) in scenarios.items():
        config = BacktestConfig(
            initial_capital=base_config.initial_capital,
            quantity=base_config.quantity,
            commission_rate=commission,
            commission_fixed=base_config.commission_fixed,
            slippage_rate=slippage,
            cost_schedule=base_config.cost_schedule,
            execution=base_config.execution,
            enable_risk_manager=base_config.enable_risk_manager,
            max_position_quantity=base_config.max_position_quantity,
            max_order_notional=base_config.max_order_notional,
            max_daily_loss=base_config.max_daily_loss,
            enable_stop_loss=base_config.enable_stop_loss,
            stop_loss_pct=base_config.stop_loss_pct,
        )
        signals = provider(list(bars), **dict(params))
        run = evaluate_strategy(bars, signals, name=name, config=config, detector=detector)
        m = run.metrics
        if base_trades is None:
            base_trades = m.num_trades
        rows.append(
            {
                "scenario": scenario,
                "num_trades": m.num_trades,
                "net_pnl": str(m.net_pnl),
                "net_return_pct": str(m.net_return_pct),
                "transaction_costs": str(m.transaction_costs),
                "max_drawdown_pct": str(m.max_drawdown_pct),
                "final_equity": str(m.final_equity),
                "same_trades_as_base": m.num_trades == (base_trades or -1),
            }
        )
    return rows


def robustness_grid(
    bars: Sequence[MarketPrice],
    *,
    name: str,
    provider,
    base_params: Mapping[str, object],
    perturb: Mapping[str, Sequence[object]],
    config: BacktestConfig | None = None,
) -> list[dict[str, object]]:
    """Bounded one-at-a-time parameter perturbations around the canonical values.

    Each row is one variant evaluated over the full ``bars`` argument (the caller
    decides the segment; selection must keep protected OOS out). Row includes a
    ``coarse`` verdict: net-return sign and consistency vs the canonical variant.
    """
    config = config or BacktestConfig()
    rows: list[dict[str, object]] = []
    canonical: dict[str, object] = dict(base_params)
    canon_sig = provider(list(bars), **canonical)
    canon_run = evaluate_strategy(bars, canon_sig, name=name, config=config)
    canon_ret = canon_run.metrics.net_return_pct
    for param, values in perturb.items():
        for value in values:
            params = dict(base_params)
            params[param] = value
            signals = provider(list(bars), **params)
            run = evaluate_strategy(bars, signals, name=name, config=config)
            m = run.metrics
            rows.append(
                {
                    "variant": f"{param}={value}",
                    "num_trades": m.num_trades,
                    "net_return_pct": str(m.net_return_pct),
                    "net_pnl": str(m.net_pnl),
                    "max_drawdown_pct": str(m.max_drawdown_pct),
                    "win_rate_pct": str(m.win_rate),
                    "transaction_costs": str(m.transaction_costs),
                    "canonical_return": str(canon_ret),
                    "generous_sign_match": (m.net_return_pct >= 0) == (canon_ret >= 0),
                }
            )
    return rows