"""WS 7.13 CLI: continuous feedback / learning loop over day batches.

Each batch (--chunk-days) is one learning cycle over the §17f.3 loop:

    Paper Trade (champion replay) -> capture experience -> analyze/generate
    hypotheses -> champion vs challenger -> promotion gate -> (promotion is
    recorded in the version registry, becoming the next cycle's champion).

The loop is fully paper-only: capture, hypotheses, comparison and promotion are
evidence/bookkeeping only — no order is ever placed, no risk control is ever
changed, and promotion never enables live trading.

    python scripts/run_learning_loop.py --datasets-dir datasets --out-dir reports \
        --chunk-days 6 --registry model_registry --store experience_store
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from fno_ai_paper_trading.data.dataset_store import load_dataset
from fno_ai_paper_trading.evaluation.five_year import DayBars, PeriodSplitConfig
from fno_ai_paper_trading.learning.loop import (
    LearningLoop,
    LearningLoopConfig,
    cycle_result_to_html,
    default_strategy_factories,
    resolve_active_champion,
)
from fno_ai_paper_trading.persistence.experience_store import (
    DEFAULT_EXPERIENCE_DIR,
    ExperienceStore,
)
from fno_ai_paper_trading.promotion.gate import PromotionCriteria
from fno_ai_paper_trading.promotion.registry import (
    DEFAULT_MODEL_REGISTRY_DIR,
    VersionRegistry,
)
from fno_ai_paper_trading.regime.detector import RegimeDetector
from fno_ai_paper_trading.strategies import (
    MovingAverageCrossStrategy,
    RegimeFilteredMovingAverageCross,
)


def chunk_by_day(path: Path) -> list[DayBars]:
    dataset = load_dataset(path)
    grouped: dict[str, list] = {}
    for bar in dataset.bars:
        grouped.setdefault(bar.timestamp.date().isoformat(), []).append(bar)
    return [
        DayBars(
            day=date.fromisoformat(day_key),
            bars=tuple(grouped[day_key]),
            source_hash=dataset.data_hash,
        )
        for day_key in sorted(grouped)
    ]


def _candidates(selection: list[str]) -> list[RegimeFilteredMovingAverageCross]:
    choices = set(selection)
    candidates: list[RegimeFilteredMovingAverageCross] = []
    if "up" in choices:
        candidates.append(
            RegimeFilteredMovingAverageCross(allowed_trends=("UP",), trend_threshold_pct="0.05")
        )
    if "up_sideways" in choices:
        candidates.append(
            RegimeFilteredMovingAverageCross(
                allowed_trends=("UP", "SIDEWAYS"), trend_threshold_pct="0.05"
            )
        )
    if "twin" in choices:
        candidates.append(
            RegimeFilteredMovingAverageCross(
                allowed_trends=("UP", "DOWN", "SIDEWAYS"), trend_threshold_pct="0.05"
            )
        )
    if not candidates:
        raise ValueError("choose from candidate kinds: up, up_sideways, twin")
    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Continuous feedback loop (WS 7.13)")
    parser.add_argument("--datasets-dir", default="datasets")
    parser.add_argument("--out-dir", default="reports/learning_loop")
    parser.add_argument("--name", default="learning_loop")
    parser.add_argument("--start", default=None, help="inclusive start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="inclusive end date YYYY-MM-DD")
    parser.add_argument("--chunk-days", type=int, default=6, help="days per cycle")
    parser.add_argument("--training-ratio", type=float, default=0.60)
    parser.add_argument("--validation-ratio", type=float, default=0.20)
    parser.add_argument("--oos-min-days", type=int, default=1)
    parser.add_argument("--registry", default=DEFAULT_MODEL_REGISTRY_DIR)
    parser.add_argument("--store", default=DEFAULT_EXPERIENCE_DIR)
    parser.add_argument(
        "--candidates",
        default="up,twin",
        help="comma-separated kinds: up, up_sideways, twin",
    )
    args = parser.parse_args(argv)

    datasets_dir = Path(args.datasets_dir)
    days: list[DayBars] = []
    for path in sorted(datasets_dir.glob("*.csv")):
        days.extend(chunk_by_day(path))
    if not days:
        print(f"no day-chunked data found under {datasets_dir} (no *.csv)")
        return 1

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if start or end:
        days = [d for d in days if (start is None or d.day >= start) and (end is None or d.day <= end)]

    if args.chunk_days < 1:
        raise ValueError("--chunk-days must be >= 1")

    store_dir = Path(args.store)
    store = ExperienceStore(directory=store_dir, name="experiences")

    registry = VersionRegistry(args.registry)
    if registry.active is None:
        baseline = registry.start(
            strategy_name="moving_average_cross",
            strategy_params={"fast": 5, "slow": 21},
            feature_version="features-1",
            description="frozen V1 baseline MA(5,21): learning loop entry point",
        )
        print(f"started registry champion {baseline.version_id} {baseline.strategy_name}")
    else:
        print(f"registry active champion {registry.active.strategy_name}")

    factories = default_strategy_factories()
    champion = (
        resolve_active_champion(registry, factories)
        if registry.active.strategy_name in factories
        else MovingAverageCrossStrategy(fast=5, slow=21)
    )
    candidates = _candidates([token.strip() for token in args.candidates.split(",") if token.strip()])

    config = LearningLoopConfig(
        split=PeriodSplitConfig(
            training_ratio=args.training_ratio, validation_ratio=args.validation_ratio
        ),
        criteria=PromotionCriteria(oos_min_days=args.oos_min_days),
    )
    loop = LearningLoop(store=store, registry=registry, config=config, detector=RegimeDetector())

    chunks = [days[i : i + args.chunk_days] for i in range(0, len(days), args.chunk_days)]
    results = []
    for cycle, chunk in enumerate(chunks):
        result = loop.run_cycle(
            chunk, champion=champion, candidates=candidates, cycle=cycle, title=args.name
        )
        results.append(result)
        for verdict in result.verdicts:
            print(
                f"cycle {cycle}: {verdict.challenger_name} -> {verdict.decision}"
                + (f" ({verdict.promoted_version})" if verdict.promoted_version else "")
            )
        print(f"  captured {result.captured_records} trade(s), appended {result.appended}")
        champion = (
            resolve_active_champion(registry, factories)
            if registry.active is not None and registry.active.strategy_name in factories
            else champion
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.name}.json"
    html_path = out_dir / f"{args.name}.html"
    json_path.write_text(
        json.dumps([result.to_dict() for result in results], indent=2), encoding="utf-8"
    )
    html_path.write_text(
        "".join(cycle_result_to_html(result) for result in results), encoding="utf-8"
    )

    print(
        f"ran {len(results)} cycle(s) over {len(days)} day(s); champion now "
        f"{registry.active.strategy_name if registry.active else 'none'}"
    )
    print(f"wrote {json_path} and {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())