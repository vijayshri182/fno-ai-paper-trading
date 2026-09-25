"""Human decision gate for the autonomous research/validation loop.

The gate never decides for the user. Whenever the loop reaches a terminal point
that needs a person (no autonomous protocol-compliant path remains, or a GREEN
candidate requires approval), it emits a structured, deterministic report that
ends with the canonical prompt ``Choose A / B / C``.

Safety: the report always restates what the system will NOT do (no contaminated
OOS reuse, no gate bypass, no fabricated evidence, no auto promotion, no real
order), regardless of the state it was built for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from fno_ai_paper_trading.autonomous.states import LoopState, PathKind, ValidPath

DECISION_PROMPT = "Choose A / B / C"
_OPTION_LETTERS = ("A", "B", "C")

_SYSTEM_WILL_NOT = (
    "reuse contaminated or already-consumed out-of-sample data",
    "bypass or weaken any promotion/validation/live gate",
    "fabricate or extrapolate evidence that was not recorded",
    "auto-promote a candidate version",
    "send or simulate a real (live) order",
    "enable live trading or touch broker credentials",
)


@dataclass(frozen=True)
class HumanDecisionReport:
    """Structured human-decision-required report."""

    current_state: LoopState
    candidate: str
    blocking_condition: str
    evidence: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    options: tuple[tuple[str, ValidPath], ...]
    data_required: str
    system_will_not: tuple[str, ...]
    requires_human_approval: bool
    generated_at: str

    def to_markdown(self) -> str:
        lines = ["## HUMAN DECISION REQUIRED", ""]
        lines.append(f"- **Current state**: {self.current_state.value}")
        lines.append(f"- **Candidate**: {self.candidate}")
        lines.append(f"- **Blocking condition**: {self.blocking_condition}")
        if self.evidence:
            lines.append("")
            lines.append("- **Evidence**:")
            lines.extend(f"  - {item}" for item in self.evidence)
        if self.prohibited_actions:
            lines.append("")
            lines.append("- **What is prohibited**:")
            lines.extend(f"  - {item}" for item in self.prohibited_actions)
        lines.append("")
        lines.append("- **Valid next paths**:")
        for letter, path in self.options:
            lines.append(f"  - **{letter}** — {path.label}: {path.description}")
        lines.append("")
        lines.append(f"- **Data required**: {self.data_required}")
        lines.append("")
        lines.append("- **What the system will NOT do**:")
        lines.extend(f"  - {item}" for item in self.system_will_not)
        lines.append("")
        lines.append(DECISION_PROMPT)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "current_state": self.current_state.value,
            "candidate": self.candidate,
            "blocking_condition": self.blocking_condition,
            "evidence": list(self.evidence),
            "prohibited_actions": list(self.prohibited_actions),
            "options": [
                {"letter": letter, "path": path.to_dict()} for letter, path in self.options
            ],
            "data_required": self.data_required,
            "system_will_not": list(self.system_will_not),
            "requires_human_approval": self.requires_human_approval,
            "generated_at": self.generated_at,
        }


class HumanDecisionGate:
    """Builds deterministic human-decision reports with A/B/C options."""

    def build(
        self,
        *,
        current_state: LoopState,
        candidate: str,
        blocking_condition: str,
        evidence: Sequence[str] = (),
        prohibited_actions: Sequence[str] = (),
        paths: Sequence[ValidPath] = (),
        data_required: str = "",
        requires_human_approval: bool = False,
        now: str = "",
    ) -> HumanDecisionReport:
        """Assemble the report; guarantees exactly one A/B/C prompt."""
        if not blocking_condition.strip():
            raise ValueError("blocking_condition is required")
        if not candidate.strip():
            raise ValueError("candidate is required")
        options = self._options(paths)
        generated_at = now or _utc_now()
        return HumanDecisionReport(
            current_state=current_state,
            candidate=candidate,
            blocking_condition=blocking_condition,
            evidence=tuple(evidence),
            prohibited_actions=tuple(prohibited_actions),
            options=options,
            data_required=data_required or "none",
            system_will_not=_SYSTEM_WILL_NOT,
            requires_human_approval=requires_human_approval,
            generated_at=generated_at,
        )

    @staticmethod
    def _options(paths: Sequence[ValidPath]) -> tuple[tuple[str, ValidPath], ...]:
        fallbacks = (
            ValidPath(
                kind=PathKind.AWAIT_HUMAN_DECISION,
                label="Human-defined alternative",
                description="Provide a different protocol-compliant next step.",
                autonomous=False,
                ready=False,
            ),
            ValidPath(
                kind=PathKind.AWAIT_HUMAN_DECISION,
                label="Decline / no action",
                description="Keep the candidate pending; no further autonomous action.",
                autonomous=False,
                ready=False,
            ),
            ValidPath(
                kind=PathKind.AWAIT_HUMAN_DECISION,
                label="None of the above",
                description="Stop the loop; record the state in the decision ledger.",
                autonomous=False,
                ready=False,
            ),
        )
        filled: list[ValidPath] = list(paths[:3])
        for fallback in fallbacks:
            if len(filled) >= 3:
                break
            filled.append(fallback)
        return tuple((_OPTION_LETTERS[i], filled[i]) for i in range(3))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")