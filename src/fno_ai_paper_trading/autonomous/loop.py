"""Autonomous research/validation loop -- orchestration core.

The loop turns a loop state (plus an optional blockage) into exactly one
decision: a terminal token, a next action, the set of protocol-compliant paths it
considered, and (when a person is required) a human-decision report.

Key behaviour -- a protocol ``BLOCKED`` condition never silently terminates the
workflow:

* if any rescquently autonomous, protocol-compliant path is ready, the loop
  ``CONTINUE_AUTONOMOUSLY`` on it;
* if the blocked candidate is waiting on fresh untouched OOS data that cannot
  yet satisfy validation coverage, the loop enters
  ``WAITING_FOR_FRESH_OOS_DATA`` and states exactly what data remains required;
* only when no protocol-compliant path exists does the loop raise
  ``HUMAN_DECISION_REQUIRED`` and ask the human to ``Choose A / B / C``.

Safety invariants are fixed for every decision: LIVE GATE CLOSED, no real order,
no promotion, ALGO READY NO, algorithm health RED. Nothing here places orders or
enables live trading.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Sequence

from fno_ai_paper_trading.autonomous.fresh_data import (
    FreshDataPool,
    FreshOosRequirement,
)
from fno_ai_paper_trading.autonomous.gate import HumanDecisionGate, HumanDecisionReport
from fno_ai_paper_trading.autonomous.states import (
    Blockage,
    BlockageType,
    LoopState,
    PathKind,
    STATE_POLICY,
    ValidPath,
)

TERMINAL_CONTINUE_AUTONOMOUSLY = "CONTINUE_AUTONOMOUSLY"
TERMINAL_WAITING_FOR_FRESH_OOS_DATA = "WAITING_FOR_FRESH_OOS_DATA"
TERMINAL_HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL = "GREEN_REQUIRES_HUMAN_APPROVAL"

ALL_TERMINAL_TOKENS = (
    TERMINAL_CONTINUE_AUTONOMOUSLY,
    TERMINAL_WAITING_FOR_FRESH_OOS_DATA,
    TERMINAL_HUMAN_DECISION_REQUIRED,
    TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL,
)

ALLOWED_LOOP_ACTIONS = frozenset(path.value for path in PathKind)

_CREDENTIAL_NAME = "FNO_UPSTOX_ACCESS_TOKEN"


@dataclass(frozen=True)
class AutonomousLoopConfig:
    """Read-only inputs that pin the loop's operating context."""

    candidate: str = "OUR-ALGO-004"
    stage: str = "protected_oos_validation"
    consumed_oos_until: date = date(2026, 9, 11)
    fresh_data_acquisition_permitted: bool = True
    data_acquisition_available: bool = False
    allow_research_families: bool = True
    pending_research_task: bool = False
    pending_preregistered_hypothesis: bool = False
    min_days: int = 20
    min_bars: int = 1500
    min_trades: int = 30
    now: str = ""

    def __post_init__(self) -> None:
        if not self.candidate.strip():
            raise ValueError("candidate is required")
        if not self.stage.strip():
            raise ValueError("stage is required")

    @property
    def fresh_boundary(self) -> date:
        """Fresh OOS must start strictly after the consumed window."""
        return self.consumed_oos_until

    def to_requirement(self) -> FreshOosRequirement:
        return FreshOosRequirement(
            first_bar_after=self.fresh_boundary,
            min_days=self.min_days,
            min_bars=self.min_bars,
            min_trades=self.min_trades,
        )


@dataclass(frozen=True)
class LoopDecision:
    """The loop's single deterministic decision for one cycle."""

    current_state: LoopState
    terminal_token: str
    next_action: str
    blockage: Blockage | None
    paths: tuple[ValidPath, ...]
    human_decision_report: HumanDecisionReport | None
    requirements_message: str
    narrative: tuple[str, ...]
    pool_summary: str

    # Safety invariants -- fixed for every decision, never changeable.
    live_gate_closed: bool = True
    promotion: bool = False
    algo_ready: str = "NO"
    algorithm_health: str = "RED"
    real_order_sent: bool = False

    def __post_init__(self) -> None:
        if self.terminal_token not in ALL_TERMINAL_TOKENS:
            raise ValueError(f"unknown terminal token {self.terminal_token!r}")
        if self.next_action not in ALLOWED_LOOP_ACTIONS:
            raise ValueError(f"next_action {self.next_action!r} is not a loop action")
        if self.live_gate_closed is not True:
            raise ValueError("the LIVE GATE must stay CLOSED")
        if self.promotion is not False:
            raise ValueError("a decision can never promote a version")
        if self.real_order_sent is not False:
            raise ValueError("a decision can never send a real order")
        if self.algo_ready != "NO":
            raise ValueError("ALGO READY must stay NO")
        if self.algorithm_health != "RED":
            raise ValueError("algorithm health must stay RED")

    def to_dict(self) -> dict[str, object]:
        return {
            "current_state": self.current_state.value,
            "terminal_token": self.terminal_token,
            "next_action": self.next_action,
            "blockage": self.blockage.to_dict() if self.blockage else None,
            "paths": [path.to_dict() for path in self.paths],
            "human_decision_report": (
                self.human_decision_report.to_dict()
                if self.human_decision_report
                else None
            ),
            "requirements_message": self.requirements_message,
            "narrative": list(self.narrative),
            "pool_summary": self.pool_summary,
            "live_gate_closed": self.live_gate_closed,
            "promotion": self.promotion,
            "algo_ready": self.algo_ready,
            "algorithm_health": self.algorithm_health,
            "real_order_sent": self.real_order_sent,
        }

    def report_markdown(self) -> str:
        """Render the ``## AUTONOMOUS LOOP UPDATE`` section owned by the loop."""
        lines = ["## AUTONOMOUS LOOP UPDATE", ""]
        if self.blockage is not None:
            lines.append(f"- **Previous BLOCKED reason**: {self.blockage.reason}")
        lines.append(
            "- **Protocol interpretation**: a BLOCKED operation may not be retried,"
            " bypassed or run on prohibited data; the loop finds another"
            " protocol-compliant path or waits for fresh data before it escalates"
            " to a human."
        )
        behavior = (
            "BLOCKED no longer terminates research: the loop now branches to a"
            " permitted path, or enters WAITING_FOR_FRESH_OOS_DATA with an explicit"
            " data-requirements statement, or raises HUMAN_DECISION_REQUIRED with an"
            " A/B/C report."
        )
        lines.append(f"- **Loop behavior changed**: {behavior}")
        lines.append(f"- **Current state**: {self.current_state.value}")
        lines.append(f"- **Next action**: {self.next_action}")
        lines.append(f"- **Terminal token**: {self.terminal_token}")
        availability = "available" if self.blockage is not None else "engaged"
        if self.human_decision_report is not None:
            lines.append(
                f"- **Human decision gate**: engaged (report {self.human_decision_report.generated_at})"
            )
        else:
            lines.append(f"- **Human decision gate**: not engaged ({availability})")
        lines.append(f"- **Fresh-data capability**: {self.pool_summary}")
        lines.append(f"- **Fresh-data requirements**: {self.requirements_message}")
        lines.append(f"- **Algorithm Health**: {self.algorithm_health} — color does not determine autonomous continuation or promotion")
        lines.append("- **ALGO READY**: NO")
        lines.append("- **Promotion**: NO")
        lines.append("- **LIVE GATE**: CLOSED")
        lines.append("- **Real orders**: NONE SENT")
        if self.human_decision_report is not None:
            body = self.human_decision_report.to_markdown()
            body = body.replace("## HUMAN DECISION REQUIRED", "### Human decision report", 1)
            lines.append("")
            lines.append(body)
        return "\n".join(lines)


class AutonomousResearchLoop:
    """Resolves loop states into terminal decisions, never touching live."""

    def __init__(
        self,
        config: AutonomousLoopConfig,
        pool: FreshDataPool,
        gate: HumanDecisionGate | None = None,
    ) -> None:
        self.config = config
        self.pool = pool
        self.gate = gate or HumanDecisionGate()

    def run(
        self,
        state: LoopState,
        blockage: Blockage | None = None,
    ) -> LoopDecision:
        """Evaluate one cycle and return exactly one terminal decision.

        Continuation is determined solely by the protocol: every ready autonomous
        protocol-compliant path is taken first, then waiting for fresh OOS data,
        then a human step.  Algorithm Health color is never consulted and never a
        terminal condition; ``LoopState.GREEN`` is a protocol signal whose first
        effect is to escalate to human approval *only* when no autonomous path
        remains.
        """
        if state is LoopState.HUMAN_DECISION_REQUIRED:
            return self._decision(
                state=state,
                token=TERMINAL_HUMAN_DECISION_REQUIRED,
                action=PathKind.AWAIT_HUMAN_DECISION,
                blockage=blockage,
                human=True,
            )

        paths = self._paths()
        ready = [path for path in paths if path.ready and path.autonomous]
        if ready:
            chosen = ready[0]
            return self._decision(
                state=state,
                token=TERMINAL_CONTINUE_AUTONOMOUSLY,
                action=chosen.kind,
                blockage=blockage,
                human=False,
                paths=paths,
            )

        fresh = self._path_of(paths, PathKind.FRESH_OOS_ACQUISITION)
        if state is LoopState.BLOCKED and fresh is not None:
            return self._decision(
                state=state,
                token=TERMINAL_WAITING_FOR_FRESH_OOS_DATA,
                action=PathKind.AWAIT_FRESH_OOS_DATA,
                blockage=blockage,
                human=False,
                paths=paths,
                requirements=fresh.reason or fresh.description,
            )

        if state is LoopState.GREEN:
            report = self.gate.build(
                current_state=state,
                candidate=self.config.candidate,
                blocking_condition=(
                    "candidate evidence satisfies the current stage and no"
                    " autonomous protocol-compliant path remains; explicit human"
                    " approval is required before any next stage"
                ),
                evidence=(),
                prohibited_actions=self._prohibited(),
                paths=paths,
                data_required="none -- approval decision requested",
                requires_human_approval=True,
                now=self.config.now,
            )
            return self._decision(
                state=state,
                token=TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL,
                action=PathKind.AWAIT_HUMAN_APPROVAL,
                blockage=blockage,
                human=False,
                human_report=report,
            )

        return self._decision(
            state=state,
            token=TERMINAL_HUMAN_DECISION_REQUIRED,
            action=PathKind.AWAIT_HUMAN_DECISION,
            blockage=blockage,
            human=True,
            paths=paths,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _paths(self) -> tuple[ValidPath, ...]:
        paths: list[ValidPath] = []
        readiness = self.pool.readiness()
        if self.config.fresh_data_acquisition_permitted:
            base = ValidPath(
                kind=PathKind.FRESH_OOS_ACQUISITION,
                label="Fresh untouched OOS data",
                description=(
                    "Collect or consume a fresh, untouched, single-use OOS window"
                    " strictly after the consumed protected window."
                ),
                autonomous=True,
                ready=False,
                reason=self._fresh_reason(readiness.ready_message()),
            )
            if readiness.ready:
                base = replace(
                    base,
                    ready=True,
                    description=(
                        "Run single-use validation on the registered fresh untouched OOS window."
                    ),
                    reason=readiness.ready_message(),
                )
            elif self.config.data_acquisition_available:
                base = replace(
                    base,
                    ready=True,
                    description=(
                        "Continue automated acquisition of fresh untouched OOS data (GET-only)."
                    ),
                )
            paths.append(base)
        if self.config.allow_research_families:
            if self.config.pending_research_task:
                paths.append(
                    ValidPath(
                        kind=PathKind.OTHER_FAMILY_RESEARCH,
                        label="Separately permitted family",
                        description=(
                            "Run the queued pre-registered research task on a permitted"
                            " family over the pre-OOS domain (no protected OOS touched)."
                        ),
                        autonomous=True,
                        ready=True,
                    )
                )
            else:
                paths.append(
                    ValidPath(
                        kind=PathKind.OTHER_FAMILY_RESEARCH,
                        label="Separately permitted family",
                        description=(
                            "Research a separately permitted family over the pre-OOS domain."
                        ),
                        autonomous=False,
                        ready=False,
                        reason="no pre-registered new-family task is queued; human selection required",
                    )
                )
        if self.config.pending_preregistered_hypothesis:
            paths.append(
                ValidPath(
                    kind=PathKind.PREREGISTERED_HYPOTHESIS,
                    label="Pre-registered hypothesis set",
                    description=(
                        "Run the queued pre-registered hypothesis set on the research"
                        " domain; protected OOS stays untouched."
                    ),
                    autonomous=True,
                    ready=True,
                )
            )
        return tuple(paths)

    def _fresh_reason(self, readiness_message: str) -> str:
        if self.config.data_acquisition_available:
            return readiness_message
        return (
            f"{readiness_message}; automated acquisition is credential-gated"
            f" ({_CREDENTIAL_NAME}) until that dependency is satisfied"
        )

    @staticmethod
    def _path_of(paths: Sequence[ValidPath], kind: PathKind) -> ValidPath | None:
        for path in paths:
            if path.kind is kind:
                return path
        return None

    def _prohibited(self) -> tuple[str, ...]:
        return (
            "re-run the candidate on the already-consumed protected OOS window",
            "relabel the consumed 2025-10-06..2026-09-11 interval as fresh OOS",
            "weaken or bypass PROJECT_PLAN §17e.11 / WS 7.16 promotion gates",
            "change algorithm health to GREEN or ALGO READY to YES without evidence",
        )

    def _decision(
        self,
        *,
        state: LoopState,
        token: str,
        action: PathKind,
        blockage: Blockage | None,
        human: bool,
        paths: Sequence[ValidPath] | None = None,
        human_report: HumanDecisionReport | None = None,
        requirements: str = "",
    ) -> LoopDecision:
        paths = tuple(paths) if paths is not None else self._paths()
        frame = blockage or default_blockage(
            candidate=self.config.candidate, stage=self.config.stage, now=self.config.now
        )
        report = human_report
        if human and human_report is None:
            report = self.gate.build(
                current_state=state,
                candidate=self.config.candidate,
                blocking_condition=frame.reason,
                evidence=frame.evidence,
                prohibited_actions=frame.prohibited or self._prohibited(),
                paths=paths,
                data_required=self._data_required(),
                now=self.config.now,
            )
        requirements = requirements or self._data_required()
        narrative = self._narrative(state, token, action)
        return LoopDecision(
            current_state=state,
            terminal_token=token,
            next_action=action.value,
            blockage=frame,
            paths=paths,
            human_decision_report=report,
            requirements_message=requirements,
            narrative=narrative,
            pool_summary=self.pool.describe(),
        )

    def _data_required(self) -> str:
        req = self.config.to_requirement()
        return (
            f"fresh untouched OOS 5m NIFTY 50 bars strictly after"
            f" {self.config.fresh_boundary.isoformat()}; require at least"
            f" {req.min_days} trading days / {req.min_bars} bars / target"
            f" {req.min_trades} closed trades in one contiguous window, recorded with"
            f" a SHA-256 fingerprint and untouched until registration."
        )

    def _narrative(self, state: LoopState, token: str, action: PathKind) -> tuple[str, ...]:
        policy = STATE_POLICY[state]
        if token == TERMINAL_CONTINUE_AUTONOMOUSLY:
            return (
                f"state {state.value}: {policy}",
                f"protocol-compliant path found ({action.value}); continuing autonomously",
                "no human input required for this step",
            )
        if token == TERMINAL_WAITING_FOR_FRESH_OOS_DATA:
            return (
                f"state {state.value}: {policy}",
                "operating path is fresh untouched OOS data; coverage is insufficient",
                "waiting explicitly; acquisition or sizes will be re-evaluated on the next cycle",
            )
        if token == TERMINAL_GREEN_REQUIRES_HUMAN_APPROVAL:
            return (
                "state GREEN: autonomous work stops",
                "explicit human approval required before any next stage",
                "no real order; LIVE GATE stays CLOSED",
            )
        return (
            f"state {state.value}: no autonomous protocol-compliant path remains",
            "human decision requested; the loop never chooses for the user",
        )


def default_blockage(
    candidate: str = "OUR-ALGO-004",
    stage: str = "protected_oos_validation",
    now: str = "",
) -> Blockage:
    """The canonical OOS-consumed blockage for the OUR-ALGO-004 lineage."""
    return Blockage(
        candidate=candidate,
        stage=stage,
        blockage_type=BlockageType.OOS_ALREADY_CONSUMED,
        reason=(
            "the protected out-of-sample window 2025-10-06..2026-09-11 has already"
            " been consumed exactly once for this lineage (iteration006 and the"
            " WS 7.16 walk-forward confirmation); the protocol forbids re-running"
            " OUR-ALGO-004-B on that window and forbids relabelling it as fresh data"
        ),
        evidence=(
            "runs/research/day_batch/iteration_006_protected_oos_n3.json — window"
            " [2025-10-06, 2026-09-11], 17,412 bars / 233 days, engine_runs=1",
            "reports/model_performance/oos_confirmation.json — protected_oos 12,975"
            " bars / 173 days, validation 9,387 bars, conclusion B",
            "reports/walkforward/summary.json — protected_oos_start=2025-10-06,",
            "docs/project_state.json — fresh-OOS consequence recorded",
        ),
        prohibited=(
            "re-run OUR-ALGO-004-B on 2025-10-06..2026-09-11",
            "relabel 2025-10-06..2026-09-11 as fresh OOS",
            "weaken PROJECT_PLAN §17e.11 / WS 7.16 gates to advance the candidate",
        ),
        detected_at=now,
    )