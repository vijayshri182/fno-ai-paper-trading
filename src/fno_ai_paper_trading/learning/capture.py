"""Experience capture bridge for the continuous learning loop (WS 7.13).

Turns a completed paper replay into deterministic :class:`ExperienceRecord`
evidence (the "Paper Trade -> Capture Decision/Execution/Outcome -> Store
Experience" stage of §17f.3). Capture is a pure constructor: it reads finished
replay results and builds frozen records — it never submits an order, never
touches a portfolio, and never triggers execution.

Only *completed* round trips become outcome records (reused WS 7.9 builders);
positions still open at the end of a day are counted but not recorded, so no
unrealized P&L ever leaks into the experience evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from fno_ai_paper_trading.evaluation.five_year import DayBars
from fno_ai_paper_trading.evaluation.historical import HistoricalEvaluator
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
from fno_ai_paper_trading.models.enums import OrderSide, Signal
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.models.position import Trade
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.strategies.base import Strategy


@dataclass(frozen=True)
class CaptureResult:
    """What a capture pass produced."""

    records: tuple[ExperienceRecord, ...]
    round_trips: int
    open_position_trades: int
    sessions: int


def pair_round_trips(trades: Sequence[Trade]) -> tuple[list[tuple[Trade, Trade]], int]:
    """Pair opening/closing trades FIFO per instrument into completed round trips.

    Same-side trades accumulate as open entries; the first opposite-side trade
    closes the oldest entry. Unmatched trades (still open at day end) are
    reported via the returned ``open_count`` and are never turned into records.
    """
    open_entries: dict[str, list[Trade]] = {}
    round_trips: list[tuple[Trade, Trade]] = []
    for trade in trades:
        stack = open_entries.setdefault(trade.instrument.symbol, [])
        if not stack or stack[0].side is trade.side:
            stack.append(trade)
            continue
        entry = stack.pop(0)
        round_trips.append((entry, trade))
    open_count = sum(len(stack) for stack in open_entries.values())
    return round_trips, open_count


def _bar_index_by_timestamp(bars: Sequence[MarketPrice], timestamp) -> int | None:
    for index, bar in enumerate(bars):
        if bar.timestamp == timestamp:
            return index
    return None


def _regime_label_at(
    detector: RegimeDetector, bars: Sequence[MarketPrice], timestamp
) -> str | None:
    index = _bar_index_by_timestamp(bars, timestamp)
    if index is None:
        return None
    regime = detector.detect_prefix(bars, index)
    return regime.label if regime is not None else None


def capture_from_day_bars(
    days: Sequence[DayBars],
    strategy: Strategy,
    evaluator: HistoricalEvaluator,
    *,
    strategy_version: str = "1.0",
    timeframe: str = "1m",
    feature_version: str = "1.0",
    source: ExperienceSourceType = ExperienceSourceType.HISTORICAL_REPLAY,
    detector: RegimeDetector | None = None,
) -> CaptureResult:
    """Capture experience records from completed champion replays over days.

    Each day is replayed with the *same* evaluator (identical deterministic
    assumptions as the champion/challenger runs), so the captured trades are
    exactly the paper trades that the evaluation counted.
    """
    records: list[ExperienceRecord] = []
    open_positions = 0
    sessions = 0
    for day_bars in days:
        bars = list(day_bars.bars)
        replay = evaluator.replay_bars(
            bars,
            strategy,
            dataset_name=day_bars.day.isoformat(),
            dataset_hash=day_bars.source_hash,
        )
        sessions += 1
        round_trips, open_count = pair_round_trips(replay.result.trades)
        open_positions += open_count
        for occurrence, (entry, exit_) in enumerate(round_trips):
            regime_label = (
                _regime_label_at(detector, bars, entry.executed_at)
                if detector is not None
                else None
            )
            decision = build_decision(
                instrument=entry.instrument,
                decision_timestamp=entry.executed_at,
                timeframe=timeframe,
                signal=Signal.BUY if entry.side is OrderSide.BUY else Signal.SELL,
                confidence="1.0",
                strategy_name=strategy.name,
                strategy_version=strategy_version,
                data_reference=str(day_bars.day.isoformat()),
                feature_version=feature_version,
                decision_status=DecisionStatus.EXECUTED,
                data_quality=DataQualityStatus.VALIDATED,
                regime_label=regime_label,
            )
            outcome = build_outcome_from_round_trip(entry, exit_)
            record = build_record(
                decision=decision,
                source=source,
                recorded_at=exit_.executed_at,
                occurrence=occurrence,
                outcome=outcome,
                advisory=build_advisory(None),
                source_detail="learning-loop capture",
            )
            records.append(record)
    return CaptureResult(
        records=tuple(records),
        round_trips=len(records),
        open_position_trades=open_positions,
        sessions=sessions,
    )