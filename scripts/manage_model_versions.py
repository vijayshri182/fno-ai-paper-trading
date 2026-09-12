"""WS 7.12 CLI: controlled model promotion & rollback via the version registry.

A promote is only accepted when the WS 7.11 champion-vs-challenger report shows
a robust out-of-sample win (the promotion gate). Rejected candidates leave the
registry untouched.

Usage:
    python scripts/manage_model_versions.py --registry model_registry list
    python scripts/manage_model_versions.py --registry model_registry start
    python scripts/manage_model_versions.py --registry model_registry promote \
        --report reports/champion_vs_challenger.json --candidate regime_filtered_ma_cross
    python scripts/manage_model_versions.py --registry model_registry rollback --reason "..."

Paper-only evidence management: promotion never enables live trading and never
weakens risk controls.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from fno_ai_paper_trading.promotion.gate import PromotionCriteria, PromotionGate
from fno_ai_paper_trading.promotion.registry import VersionRegistry

BASELINE_STRATEGY = "moving_average_cross"
BASELINE_PARAMS = {"fast": 5, "slow": 21}


def _print_active(registry: VersionRegistry) -> None:
    active = registry.active
    if active is None:
        print("no champion started")
        return
    print(
        f"ACTIVE {active.version_id} {active.strategy_name} "
        f"(promoted {active.promoted_at})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model version registry (WS 7.12)")
    parser.add_argument("--registry", default="model_registry", help="registry JSONL path or directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show champion chain and status")

    start = sub.add_parser("start", help="declare the frozen MA(5,21) baseline")
    start.add_argument("--strategy", default=BASELINE_STRATEGY)
    start.add_argument("--params-json", default=json.dumps(BASELINE_PARAMS))
    start.add_argument("--feature-version", default="features-1")
    start.add_argument("--description", default="frozen V1 baseline MA(5,21)")

    promote = sub.add_parser("promote", help="promote a challenger after gate approval")
    promote.add_argument("--report", required=True, help="champion-vs-challenger report JSON")
    promote.add_argument("--candidate", required=True, help="challenger name inside the report")
    promote.add_argument("--strategy", required=True, help="strategy name to record")
    promote.add_argument("--params-json", default="{}", help="strategy params to record")
    promote.add_argument("--feature-version", default="features-1")
    promote.add_argument("--description", required=True)
    promote.add_argument("--oos-min-days", type=int, default=3)
    promote.add_argument("--no-validation-check", action="store_true")
    promote.add_argument("--require-positive-oos", action="store_true")

    rollback = sub.add_parser("rollback", help="roll the champion back")
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--to", default=None, help="optional specific version_id")

    args = parser.parse_args(argv)

    registry = VersionRegistry(args.registry)

    if args.command == "list":
        if registry.active is None:
            print("registry is empty")
        for version in registry.versions:
            print(
                f"{version.status:<11} {version.version_id:<8} "
                f"{version.strategy_name} {version.promoted_at}"
            )
        return 0

    if args.command == "start":
        if registry.active is not None:
            print("registry already started; refusing to overwrite")
            return 1
        baseline = registry.start(
            strategy_name=args.strategy,
            strategy_params=json.loads(args.params_json),
            feature_version=args.feature_version,
            description=args.description,
        )
        print(f"baseline started: {baseline.version_id} {baseline.strategy_name}")
        return 0

    if args.command == "promote":
        if registry.active is None:
            print("registry not started; run 'start' first")
            return 1
        report_path = Path(args.report)
        if not report_path.is_file():
            print(f"report not found: {report_path}")
            return 1
        report = json.loads(report_path.read_text(encoding="utf-8"))
        criteria = PromotionCriteria(
            oos_min_days=args.oos_min_days,
            require_validation_not_worse=not args.no_validation_check,
            require_positive_oos_pnl=args.require_positive_oos,
        )
        verdict = PromotionGate().evaluate_from_report_dict(
            report, args.candidate, criteria=criteria
        )
        if not verdict.promoted:
            print(f"REJECT {args.candidate}")
            for reason in verdict.reasons:
                print(f"  - {reason}")
            return 1
        created = registry.promote(
            strategy_name=args.strategy,
            strategy_params=json.loads(args.params_json),
            feature_version=args.feature_version,
            description=args.description,
            evidence=dict(verdict.evidence),
        )
        print(f"PROMOTE {args.candidate} -> {created.version_id}")
        for reason in verdict.reasons:
            print(f"  + {reason}")
        return 0

    if args.command == "rollback":
        if registry.active is None:
            print("registry not started")
            return 1
        if args.to:
            restored = registry.rollback_to(args.to, reason=args.reason)
        else:
            restored = registry.rollback(reason=args.reason)
        print(
            f"rollback: active is now {restored.version_id} {restored.strategy_name} "
            f"({args.reason})"
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())