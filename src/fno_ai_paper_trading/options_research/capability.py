"""Historical options-data capability gate (Phase 12, first gate).

Deterministically scores whether a legitimate, sufficiently complete historical
options dataset exists for leakage-safe research and evaluation. Nothing here
invents data and nothing upgrades a source's claims: every verdict is computed
from caller-supplied evidence, and synthetic fixtures used in tests are forced
to carry an explicit ``SYNTHETIC_FIXTURE`` source label which can never satisfy
the observed-market requirements (so fixture data is never represented as
observed market data).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# --------------------------------------------------------------------------- #
# The four possible gate verdicts (exact names required by the phase spec).
# --------------------------------------------------------------------------- #
HISTORICAL_OPTIONS_DATA_READY = "HISTORICAL_OPTIONS_DATA_READY"
HISTORICAL_OPTIONS_DATA_PARTIAL = "HISTORICAL_OPTIONS_DATA_PARTIAL"
HISTORICAL_OPTIONS_DATA_UNAVAILABLE = "HISTORICAL_OPTIONS_DATA_UNAVAILABLE"
HISTORICAL_OPTIONS_DATA_INVALID = "HISTORICAL_OPTIONS_DATA_INVALID"

# Per-source classifications used to derive the verdict.
SOURCE_ADEQUATE = "ADEQUATE"
SOURCE_INSUFFICIENT = "INSUFFICIENT"
SOURCE_NOT_HISTORICAL = "NOT_HISTORICAL"
SOURCE_BLOCKED = "BLOCKED"
SOURCE_SYNTHETIC = "SYNTHETIC_FIXTURE"

# Label required on any synthetic fixture evidence so it can never be treated
# as observed market data.
SYNTHETIC_PREFIX = "SYNTHETIC_FIXTURE"

OPTION_BAR_SCHEMA_VERSION = "options_chain_bar_v1"

ALL_SOURCE_VERDICTS = frozenset(
    {SOURCE_ADEQUATE, SOURCE_INSUFFICIENT, SOURCE_NOT_HISTORICAL, SOURCE_BLOCKED, SOURCE_SYNTHETIC}
)


@dataclass(frozen=True)
class SourceFinding:
    """One investigated historical options-data source, exactly as found.

    ``synthetic`` marks evidence built from fixtures for deterministic tests.
    A synthetic source is classified ``SYNTHETIC_FIXTURE`` and can never
    contribute to a READY/PARTIAL verdict.
    """

    source_id: str
    provider: str
    product: str
    date_coverage: str = ""
    granularity: str = ""                 # e.g. "5m", "1d", "snapshot"
    historical_retrieval: bool = False    # false == live-only (no as-of history)
    observed_bid_ask: bool = False
    observed_ltp: bool = False
    observed_volume: bool = False
    observed_oi: bool = False
    observed_iv: bool = False
    observed_greeks: bool = False
    timezone: str = ""
    licensing_access: str = ""
    reproducible: bool = False
    checksum_provenance: bool = False
    supports_realistic_simulation: bool = False
    synthetic: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "product": self.product,
            "date_coverage": self.date_coverage,
            "granularity": self.granularity,
            "historical_retrieval": self.historical_retrieval,
            "observed_bid_ask": self.observed_bid_ask,
            "observed_ltp": self.observed_ltp,
            "observed_volume": self.observed_volume,
            "observed_oi": self.observed_oi,
            "observed_iv": self.observed_iv,
            "observed_greeks": self.observed_greeks,
            "timezone": self.timezone,
            "licensing_access": self.licensing_access,
            "reproducible": self.reproducible,
            "checksum_provenance": self.checksum_provenance,
            "supports_realistic_simulation": self.supports_realistic_simulation,
            "synthetic": self.synthetic,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class DatasetFinding:
    """Whether a materialized historical options dataset exists and passed the
    frozen ``options_chain_bar`` contract."""

    present: bool
    validated: bool
    source_id: str | None = None
    fingerprint: str | None = None
    schema_version: str | None = None
    num_rows: int | None = None
    synthetic: bool = False
    note: str = ""


@dataclass(frozen=True)
class ReadinessAssessment:
    """Deterministic outcome of the capability gate."""

    verdict: str
    reason: str
    source_verdicts: Mapping[str, str] = field(default_factory=dict)
    dataset: DatasetFinding | None = None
    evidence_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "gate": "historical_options_data",
            "verdict": self.verdict,
            "reason": self.reason,
            "sources": dict(self.source_verdicts),
            "dataset": {
                "present": self.dataset.present,
                "validated": self.dataset.validated,
                "source_id": self.dataset.source_id,
                "fingerprint": self.dataset.fingerprint,
                "schema_version": self.dataset.schema_version,
                "num_rows": self.dataset.num_rows,
                "synthetic": self.dataset.synthetic,
            }
            if self.dataset
            else None,
            "evidence_notes": list(self.evidence_notes),
        }

    def render_markdown(self) -> str:
        lines = [
            "# Historical options-data capability gate (Phase 12)",
            "",
            f"- Verdict: **`{self.verdict}`**",
            f"- Reason: {self.reason}",
            "- Sources investigated:",
        ]
        if not self.source_verdicts:
            lines.append("  - (none)")
        for source_id in sorted(self.source_verdicts):
            lines.append(f"  - `{source_id}` -> `{self.source_verdicts[source_id]}`")
        if self.dataset:
            d = self.dataset
            lines += [
                "- Dataset:",
                f"  - present=`{d.present}` validated=`{d.validated}` synthetic=`{d.synthetic}`",
                f"  - source=`{d.source_id}` schema=`{d.schema_version}` rows=`{d.num_rows}`",
                f"  - fingerprint=`{d.fingerprint}`",
            ]
        for note in self.evidence_notes:
            lines.append(f"- Note: {note}")
        return "\n".join(lines) + "\n"


def classify_source(finding: SourceFinding) -> str:
    """Classify one source into the closed set of source verdicts."""
    if finding.synthetic:
        return SOURCE_SYNTHETIC
    if not finding.historical_retrieval:
        return SOURCE_NOT_HISTORICAL
    # Access blocked (failed auth/licensing/paywall, non-retrievable): the
    # source is not usable even though it may theoretically hold the data.
    if not finding.reproducible:
        return SOURCE_BLOCKED
    priced_observed = finding.observed_bid_ask or finding.observed_ltp
    if not (priced_observed and finding.observed_oi):
        return SOURCE_INSUFFICIENT
    if not finding.checksum_provenance:
        return SOURCE_INSUFFICIENT
    if finding.supports_realistic_simulation:
        return SOURCE_ADEQUATE
    return SOURCE_INSUFFICIENT


def assess_readiness(
    *,
    sources: tuple[SourceFinding, ...] = (),
    dataset: DatasetFinding | None = None,
    evidence_notes: tuple[str, ...] = (),
) -> ReadinessAssessment:
    """Score the gate deterministically from the supplied evidence.

    Rules (in priority order):

    * a materialized dataset that fails the frozen contract -> INVALID;
    * a validated, non-synthetic dataset backed by an adequate, matching
      non-synthetic source -> READY;
    * a validated dataset but only insufficient sources -> PARTIAL;
    * no dataset but at least one adequate or insufficient (historical-partial)
      non-synthetic source -> PARTIAL (available, not yet materialized /
      not yet adequate in every required field);
    * otherwise (live-only / blocked sources only) -> UNAVAILABLE.
    """
    if dataset is not None and dataset.present and not dataset.validated:
        return ReadinessAssessment(
            verdict=HISTORICAL_OPTIONS_DATA_INVALID,
            reason=(
                "a materialized historical options dataset exists but failed the "
                "frozen options_chain_bar contract and was quarantined; it is never "
                "used and nothing is repaired"
            ),
            source_verdicts=_source_verdicts(sources),
            dataset=dataset,
            evidence_notes=evidence_notes,
        )

    source_verdicts = _source_verdicts(sources)
    adequate = {k for k, v in source_verdicts.items() if v == SOURCE_ADEQUATE}
    insufficient = {k for k, v in source_verdicts.items() if v == SOURCE_INSUFFICIENT}

    if dataset is not None and dataset.present and dataset.validated:
        non_synthetic = not dataset.synthetic
        if non_synthetic and dataset.source_id in adequate:
            return ReadinessAssessment(
                verdict=HISTORICAL_OPTIONS_DATA_READY,
                reason=(
                    f"validated historical options dataset (source {dataset.source_id!r}, "
                    f"schema {dataset.schema_version}, fingerprint {dataset.fingerprint}) "
                    "backed by an adequate reproducible source with checksum provenance"
                ),
                source_verdicts=source_verdicts,
                dataset=dataset,
                evidence_notes=evidence_notes,
            )
        return ReadinessAssessment(
            verdict=HISTORICAL_OPTIONS_DATA_PARTIAL,
            reason=(
                "a dataset is materialized and contract-valid, but its source is "
                "synthetic or not yet adequate for realistic entry/exit simulation "
                "(missing observed bid/ask or last price, or no reproducibility/checksum)"
            ),
            source_verdicts=source_verdicts,
            dataset=dataset,
            evidence_notes=evidence_notes,
        )

    if adequate or insufficient:
        verdict = (
            HISTORICAL_OPTIONS_DATA_PARTIAL
            if insufficient
            else HISTORICAL_OPTIONS_DATA_PARTIAL
        )
        detail = (
            "adequate historical source(s) exist but no validated dataset was "
            "materialized yet"
            if adequate
            else "only insufficient historical sources exist (e.g. daily-only, "
            "no observed bid/ask, or non-reproducible); they cannot support "
            "realistic 5m entry/exit simulation"
        )
        return ReadinessAssessment(
            verdict=verdict,
            reason=detail,
            source_verdicts=source_verdicts,
            dataset=dataset,
            evidence_notes=evidence_notes,
        )

    return ReadinessAssessment(
        verdict=HISTORICAL_OPTIONS_DATA_UNAVAILABLE,
        reason=(
            "no historical options dataset exists and every investigated source is "
            "live-only, blocked, or synthetic; historical chains for the research "
            "window are not retrievable with current provider access"
        ),
        source_verdicts=source_verdicts,
        dataset=dataset,
        evidence_notes=evidence_notes,
    )


def _source_verdicts(sources: tuple[SourceFinding, ...]) -> dict[str, str]:
    return {f.source_id: classify_source(f) for f in sources}


__all__ = [
    "HISTORICAL_OPTIONS_DATA_READY",
    "HISTORICAL_OPTIONS_DATA_PARTIAL",
    "HISTORICAL_OPTIONS_DATA_UNAVAILABLE",
    "HISTORICAL_OPTIONS_DATA_INVALID",
    "SOURCE_ADEQUATE",
    "SOURCE_INSUFFICIENT",
    "SOURCE_NOT_HISTORICAL",
    "SOURCE_BLOCKED",
    "SOURCE_SYNTHETIC",
    "SYNTHETIC_PREFIX",
    "OPTION_BAR_SCHEMA_VERSION",
    "SourceFinding",
    "DatasetFinding",
    "ReadinessAssessment",
    "classify_source",
    "assess_readiness",
]