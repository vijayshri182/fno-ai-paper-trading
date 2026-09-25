"""Phase 8 â€” deterministic option contract selection engine (provider-neutral).

:class:`ContractSelectionEngine` implements *eligibility selection*: which
contract, among validated Phase 5 ``OptionChainSnapshot``(s), satisfies the
versioned rules in :class:`ContractSelectionConfig` given a Phase 7
``MarketRegimeReport`` at the same instant. It answers only "which contract(s)
meet the configured eligibility rules?" â€” it never decides that a trade should
be taken and it never emits a BUY/SELL side.

Design rules (inherit Phases 5-7):

* all rules live in the versioned :class:`ContractSelectionConfig`; no
  thresholds are buried in code; defaults are engineering constants, not
  tuned on any protected-OOS data;
* *absence* is explicit: missing OI/volume/IV/greeks never become zero;
  ``greeks_rho`` is deliberately not used anywhere (Phase 6: unavailable);
* no look-ahead: decision at time ``t`` uses only the snapshot at ``t`` and the
  regime computed at ``t``;
* the resolved strike is an actual strike present in the chain â€” never a
  synthetic one;
* deterministic tie-breaking is explicit and tested (module constant
  :data:`TIE_BREAK_ORDER`);
* forbidden imports: execution, broker, Upstox order code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping, Sequence

from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.research.options.models import (
    OptionChainSnapshot,
    OptionQuote,
    OptionSide,
)
from fno_ai_paper_trading.research.options.normalization import chain_fingerprint
from fno_ai_paper_trading.research.regime import Direction, MarketRegimeReport
from fno_ai_paper_trading.research.selection.models import (
    CandidateState,
    ContractCandidateVerdict,
    ContractSelectionRequest,
    ContractSelectionResult,
    ExpiryPolicy,
    OffsetUnits,
    SelectionEvidence,
    SelectionOutcome,
    SideSource,
    StrikePolicy,
)
from fno_ai_paper_trading.utils.functions import to_decimal

ENGINE_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"
SELECTOR_RULES_VERSION = "1.0.0"

# Geographic default in phase order: BULLISH -> CE, BEARISH -> PE. NEUTRAL is
# handled by ``neutral_side`` (configurable; ``None`` => no selection).
REGIME_DIRECTION_SIDE: Mapping[str, OptionSide] = {
    Direction.BULLISH.value: OptionSide.CE,
    Direction.BEARISH.value: OptionSide.PE,
}

# Documented deterministic tie-break precedence (first key wins; missing
# liquidity data sorts last, never ahead of a present value).
TIE_BREAK_ORDER = (
    "expiry_policy_match",
    "minimum_strike_distance",
    "tighter_spread_ratio",
    "higher_open_interest",
    "stable_contract_key",
)

_DECIMAL_100 = Decimal("100")


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_date(value: date | str) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def pool_fingerprint(snapshots: Sequence[OptionChainSnapshot]) -> str:
    """Deterministic fingerprint over the whole expiry pool (audit/evidence)."""
    payload = sorted(
        (s.expiry.isoformat(), chain_fingerprint(s)) for s in snapshots
    )
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractSelectionConfig:
    """Explicit, versioned selection rules.

    All market-quality thresholds default to **disabled** (``None``/``False``)
    except ``require_two_sided_book`` / ``require_positive_bid`` /
    ``require_positive_ask`` which default on so a default selection is a
    clean, tradeable two-sided quote. Every threshold here is an engineering
    assumption labelled as such; none was tuned on any protected-OOS data.
    """

    version: str = SELECTOR_RULES_VERSION
    expiry_policy: ExpiryPolicy = ExpiryPolicy.NEAREST
    strike_policy: StrikePolicy = StrikePolicy.ATM
    atm_offset: int | Decimal = 0
    offset_units: OffsetUnits = OffsetUnits.STRIKES
    side_source: SideSource = SideSource.REGIME
    neutral_side: OptionSide | str | None = None
    max_snapshot_age_seconds: int | None = None
    require_two_sided_book: bool = True
    require_positive_bid: bool = True
    require_positive_ask: bool = True
    require_last_price: bool = False
    max_spread_points: int | float | str | Decimal | None = None
    max_spread_pct: int | float | str | Decimal | None = None
    min_open_interest: int | None = None
    min_open_interest_change: int | None = None
    min_volume: int | None = None
    min_iv: int | float | str | Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expiry_policy, ExpiryPolicy):
            raise ValueError("expiry_policy must be an ExpiryPolicy")
        if not isinstance(self.strike_policy, StrikePolicy):
            raise ValueError("strike_policy must be a StrikePolicy")
        if not isinstance(self.offset_units, OffsetUnits):
            raise ValueError("offset_units must be an OffsetUnits")
        if not isinstance(self.side_source, SideSource):
            raise ValueError("side_source must be a SideSource")

        offset = Decimal(str(self.atm_offset))
        if self.offset_units is OffsetUnits.STRIKES and offset != int(offset):
            raise ValueError("atm_offset must be an integer number of strikes in STRIKES mode")
        object.__setattr__(self, "atm_offset", offset)

        neutral = self.neutral_side
        parsed = neutral if isinstance(neutral, OptionSide) else OptionSide.parse(neutral)
        if neutral is not None and parsed is None:
            raise ValueError("neutral_side must be an OptionSide (CE/PE) or None")
        object.__setattr__(self, "neutral_side", parsed)

        if self.max_snapshot_age_seconds is not None and self.max_snapshot_age_seconds < 0:
            raise ValueError("max_snapshot_age_seconds must be >= 0")
        if self.max_spread_points is not None:
            object.__setattr__(self, "max_spread_points", to_decimal(self.max_spread_points))
            if self.max_spread_points < 0:
                raise ValueError("max_spread_points must be >= 0")
        if self.max_spread_pct is not None:
            object.__setattr__(self, "max_spread_pct", to_decimal(self.max_spread_pct))
            if self.max_spread_pct < 0:
                raise ValueError("max_spread_pct must be >= 0")
        if self.min_iv is not None:
            object.__setattr__(self, "min_iv", to_decimal(self.min_iv))
            if self.min_iv < 0:
                raise ValueError("min_iv must be >= 0")

    def to_dict(self) -> dict[str, object]:
        return dict(
            sorted(
                {
                    "version": self.version,
                    "expiry_policy": self.expiry_policy.value,
                    "strike_policy": self.strike_policy.value,
                    "atm_offset": str(self.atm_offset),
                    "offset_units": self.offset_units.value,
                    "side_source": self.side_source.value,
                    "neutral_side": self.neutral_side.value if self.neutral_side else None,
                    "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                    "require_two_sided_book": self.require_two_sided_book,
                    "require_positive_bid": self.require_positive_bid,
                    "require_positive_ask": self.require_positive_ask,
                    "require_last_price": self.require_last_price,
                    "max_spread_points": str(self.max_spread_points) if self.max_spread_points is not None else None,
                    "max_spread_pct": str(self.max_spread_pct) if self.max_spread_pct is not None else None,
                    "min_open_interest": self.min_open_interest,
                    "min_open_interest_change": self.min_open_interest_change,
                    "min_volume": self.min_volume,
                    "min_iv": str(self.min_iv) if self.min_iv is not None else None,
                    "tie_break_order": list(TIE_BREAK_ORDER),
                }.items()
            )
        )


def stable_candidate_key(
    quote: OptionQuote,
    *,
    target_strike: Decimal,
    spread_ratio: Decimal,
    open_interest: int | None,
) -> tuple[object, ...]:
    """Deterministic tie-break key; see :data:`TIE_BREAK_ORDER`."""
    strike_distance = abs(quote.strike - target_strike)
    oi_rank = -open_interest if open_interest is not None else 0  # missing OI sorts last
    oi_present = 0 if open_interest is not None else 1
    return (strike_distance, spread_ratio, oi_present, oi_rank, quote.key)


def _option_side(value: OptionSide | str | None) -> OptionSide | None:
    return OptionSide.parse(value)


class ContractSelectionEngine:
    """Stateless deterministic contract-eligibility selector."""

    def __init__(self, config: ContractSelectionConfig | None = None) -> None:
        self.config = config or ContractSelectionConfig()
        self.engine_version = ENGINE_VERSION
        self.schema_version = SCHEMA_VERSION

    # ---------------------------------------------------------------- select

    def select(self, request: ContractSelectionRequest) -> ContractSelectionResult:
        evidence: list[SelectionEvidence] = []
        config = self.config

        if request.timestamp.tzinfo is not None:
            return self._plain_result(
                request, SelectionOutcome.INVALID_DATA, "decision timestamp must be naive IST"
            )
        evidence.append(
            SelectionEvidence("request", "timestamp", request.timestamp.isoformat(), "decision time")
        )

        if not request.snapshots:
            return self._plain_result(
                request, SelectionOutcome.UNAVAILABLE_DATA, "no option-chain snapshots supplied"
            )
        if config.side_source is SideSource.REGIME and not self._regime_usable(request.regime):
            return self._plain_result(
                request, SelectionOutcome.UNAVAILABLE_DATA, "market regime not available/actionable"
            )
        if request.regime is not None and request.regime.timestamp is not None:
            if request.regime.timestamp > request.timestamp:
                return self._plain_result(
                    request,
                    SelectionOutcome.INVALID_DATA,
                    "regime timestamp is in the future relative to the decision time",
                )
            evidence.append(
                SelectionEvidence(
                    "regime", "timestamp", request.regime.timestamp.isoformat(),
                    f"regime {request.regime.regime} (no look-ahead: must not exceed decision time)",
                )
            )

        side = self._resolve_side(config, request, evidence)
        if isinstance(side, ContractSelectionResult):
            return side

        ordered = tuple(sorted(request.snapshots, key=lambda s: (s.expiry.isoformat(), chain_fingerprint(s))))
        fingerprints = tuple(chain_fingerprint(s) for s in ordered)
        evidence.append(
            SelectionEvidence("snapshots", "count", str(len(ordered)), "expiry pool sorted deterministically")
        )
        for snap in ordered:
            evidence.append(
                SelectionEvidence("snapshots", "expiry", snap.expiry.isoformat(), snap.source)
            )

        resolved = self._resolve_snapshot(ordered, request, evidence)
        if isinstance(resolved, tuple):
            snapshot, reject_reasons, terminal_outcome = resolved
        else:
            return resolved  # ContractSelectionResult terminal
        if snapshot is None:
            outcome = terminal_outcome or SelectionOutcome.UNAVAILABLE_DATA
            return self._plain_result(
                request, outcome, "; ".join(reject_reasons), evidence=evidence
            )

        candidates = [q for q in snapshot.quotes if q.side is side]
        verdicts, considered = self._gate_candidates(
            snapshot.quotes, side, candidates, snapshot, request, evidence
        )
        if not candidates:
            return self._plain_result(
                request,
                SelectionOutcome.UNAVAILABLE_DATA,
                f"no {side.value} contracts present in the chain",
                evidence=evidence,
                verdicts=verdicts.by_key.values(),
                fingerprints=fingerprints,
                resolved_snapshot=snapshot,
            )

        spot = snapshot.spot_price
        if spot is None:
            return self._plain_result(
                request,
                SelectionOutcome.UNAVAILABLE_DATA,
                "spot not supplied by snapshot; strike cannot be resolved (never fabricated)",
                evidence=evidence,
                verdicts=verdicts.by_key.values(),
                fingerprints=fingerprints,
                resolved_snapshot=snapshot,
            )

        strikes = tuple(sorted({q.strike for q in candidates}))
        resolved_strike, strike_failure, strike_unavailable = self._resolve_strike(
            config, spot, strikes, evidence
        )
        if resolved_strike is None:
            return self._plain_result(
                request,
                SelectionOutcome.UNAVAILABLE_DATA if strike_unavailable else SelectionOutcome.NO_ELIGIBLE_CONTRACT,
                strike_failure or "no strike resolvable",
                evidence=evidence,
                verdicts=verdicts.by_key.values(),
                fingerprints=fingerprints,
                resolved_snapshot=snapshot,
            )

        eligible: list[OptionQuote] = []
        final_verdicts: list[ContractCandidateVerdict] = []
        for quote in considered:
            verdict = verdicts.by_key[quote.key]
            reasons = list(verdict.reasons)
            state = verdict.state
            if quote.strike != resolved_strike:
                reasons.append(
                    f"strike {quote.strike:g} != configured strike {resolved_strike:g} "
                    f"(policy {config.strike_policy.value})"
                )
            market_reasons, market_state = self._market_gate(quote, config)
            reasons.extend(market_reasons)
            state = self._merge_state(state, market_state)
            eligible_ok = quote.strike == resolved_strike and state is CandidateState.VALID and not market_reasons
            final_verdicts.append(
                ContractCandidateVerdict(key=quote.key, state=state, reasons=tuple(reasons), eligible=eligible_ok)
            )
            if eligible_ok:
                eligible.append(quote)

        if eligible:
            spread_of = {q.key: self._spread_ratio(q) for q in eligible}
            chosen = min(
                eligible,
                key=lambda q: stable_candidate_key(
                    q,
                    target_strike=resolved_strike,
                    spread_ratio=spread_of[q.key],
                    open_interest=q.open_interest,
                ),
            )
            evidence.append(
                SelectionEvidence(
                    "tiebreak", "order", "|".join(TIE_BREAK_ORDER), f"chosen {chosen.key}"
                )
            )
            return self._result(
                request,
                SelectionOutcome.SELECTED,
                selected=chosen,
                resolved_snapshot=snapshot,
                resolved_strike=resolved_strike,
                resolved_side=side,
                verdicts=final_verdicts,
                fingerprints=fingerprints,
                evidence=evidence,
            )

        outcome = self._empty_outcome(considered_verdicts=final_verdicts)
        return self._result(
            request,
            outcome,
            selected=None,
            resolved_snapshot=snapshot,
            resolved_strike=resolved_strike,
            resolved_side=side,
            verdicts=final_verdicts,
            fingerprints=fingerprints,
            evidence=evidence,
        )

    # -------------------------------------------------------------- helpers

    @property
    def rules_version(self) -> str:
        return self.config.version

    def _regime_usable(self, regime: MarketRegimeReport | None) -> bool:
        return regime is not None and regime.has_regime and regime.direction is not None

    def _resolve_side(
        self, config: ContractSelectionConfig, request: ContractSelectionRequest, evidence: list[SelectionEvidence]
    ) -> OptionSide | ContractSelectionResult:
        if config.side_source is SideSource.FIXED:
            side = _option_side(request.option_side)
            if side is None:
                evidence.append(
                    SelectionEvidence("side", "fixed", str(request.option_side), "malformed or missing")
                )
                return self._plain_result(
                    request, SelectionOutcome.INVALID_DATA, "malformed or missing fixed option side"
                )
            evidence.append(SelectionEvidence("side", "value", side.value, "FIXED side source"))
            return side

        assert request.regime is not None and request.regime.direction is not None
        direction = request.regime.direction
        evidence.append(
            SelectionEvidence("side", "regime", direction.value, request.regime.regime)
        )
        if direction is Direction.NEUTRAL:
            if config.neutral_side is not None:
                evidence.append(
                    SelectionEvidence("side", "neutral_side", config.neutral_side.value, "configured continuation")
                )
                return config.neutral_side
            evidence.append(
                SelectionEvidence("side", "neutral", "NO_SELECTION", "no directional mapping for NEUTRAL")
            )
            return self._plain_result(
                request, SelectionOutcome.NO_ELIGIBLE_CONTRACT, "NEUTRAL regime has no directional mapping"
            )
        side = REGIME_DIRECTION_SIDE[direction.value]
        evidence.append(SelectionEvidence("side", "value", side.value, "regime direction mapping"))
        return side

    def _resolve_snapshot(
        self,
        ordered: tuple[OptionChainSnapshot, ...],
        request: ContractSelectionRequest,
        evidence: list[SelectionEvidence],
    ) -> tuple[OptionChainSnapshot | None, list[str], SelectionOutcome | None] | ContractSelectionResult:
        config = self.config
        decision_date = request.timestamp.date()
        reject: list[str] = []

        if config.expiry_policy is ExpiryPolicy.EXPLICIT:
            explicit = _parse_date(request.expiry)
            if explicit is None:
                evidence.append(
                    SelectionEvidence("expiry", "explicit", str(request.expiry), "malformed expiry")
                )
                return self._plain_result(
                    request, SelectionOutcome.INVALID_DATA, "malformed explicit expiry", evidence=evidence
                )
            match = next((s for s in ordered if s.expiry == explicit), None)
            if match is None:
                reject.append(f"no snapshot for explicit expiry {explicit.isoformat()}")
                return (None, reject, None)
            if explicit < decision_date:
                reject.append(f"expiry {explicit.isoformat()} < decision date {decision_date.isoformat()} (expired)")
                return (None, reject, SelectionOutcome.INVALID_DATA)
            evidence.append(SelectionEvidence("expiry", "policy", ExpiryPolicy.EXPLICIT.value, explicit.isoformat()))
            return (match, reject, None)

        # NEAREST / NEXT resolve over non-expired, structurally valid snapshots.
        usable: list[OptionChainSnapshot] = []
        for snap in ordered:
            if snap.timestamp.tzinfo is not None:
                reject.append(f"{snap.expiry.isoformat()}: snapshot timestamp not naive IST")
                continue
            if snap.underlying != request.underlying:
                reject.append(f"{snap.expiry.isoformat()}: underlying mismatch")
                continue
            evidence.append(
                SelectionEvidence("expiry", "freshness", snap.timestamp.isoformat(), f"expiry {snap.expiry.isoformat()}")
            )
            if snap.timestamp > request.timestamp:
                reject.append(f"{snap.expiry.isoformat()}: snapshot timestamp is in the future")
                continue
            if config.max_snapshot_age_seconds is not None:
                age = int((request.timestamp - snap.timestamp).total_seconds())
                if age > config.max_snapshot_age_seconds:
                    reject.append(
                        f"{snap.expiry.isoformat()}: stale snapshot (age {age}s > max {config.max_snapshot_age_seconds}s)"
                    )
                    continue
            usable.append(snap)

        valid_expiries = sorted(s.expiry for s in usable if s.expiry >= decision_date)
        if not valid_expiries:
            if usable:
                reject.append("all candidate expiries expired (expiry < decision date)")
                return (None, reject, SelectionOutcome.INVALID_DATA)
            return (None, reject, None)
        if config.expiry_policy is ExpiryPolicy.NEAREST:
            chosen_expiry = valid_expiries[0]
            if len(valid_expiries) > 1:
                evidence.append(
                    SelectionEvidence(
                        "expiry", "policy", ExpiryPolicy.NEAREST.value,
                        f"chose {chosen_expiry.isoformat()} from " + ",".join(d.isoformat() for d in valid_expiries),
                    )
                )
        elif config.expiry_policy is ExpiryPolicy.NEXT:
            if len(valid_expiries) < 2:
                reject.append("no NEXT expiry (fewer than two non-expired expiries under NEXT policy)")
                return (None, reject, None)
            chosen_expiry = valid_expiries[1]
            evidence.append(
                SelectionEvidence(
                    "expiry", "policy", ExpiryPolicy.NEXT.value,
                    f"chose next {chosen_expiry.isoformat()} after nearest {valid_expiries[0].isoformat()}",
                )
            )
        else:  # pragma: no cover â€” guarded by config validation
            raise AssertionError("unreachable expiry policy")

        snapshot = next(s for s in usable if s.expiry == chosen_expiry)
        return (snapshot, reject, None)

    def _gate_candidates(
        self,
        all_quotes: tuple[OptionQuote, ...],
        side: OptionSide,
        candidates: list[OptionQuote],
        snapshot: OptionChainSnapshot,
        request: ContractSelectionRequest,
        evidence: list[SelectionEvidence],
    ) -> tuple["_VerdictTable", list[OptionQuote]]:
        verdicts: dict[str, ContractCandidateVerdict] = {}
        by_key: dict[str, list[OptionQuote]] = {}
        for quote in all_quotes:
            by_key.setdefault(quote.key, []).append(quote)

        considered: list[OptionQuote] = []
        for quote in sorted(candidates, key=lambda q: q.key):
            considered.append(quote)
            reasons: list[str] = []
            state = CandidateState.VALID
            if not quote.instrument.is_option():
                reasons.append("instrument is not an option")
                state = CandidateState.INVALID
            if quote.instrument.underlying_symbol != request.underlying.symbol:
                reasons.append("quote underlying mismatch")
                state = CandidateState.INVALID
            if quote.timestamp.tzinfo is not None:
                reasons.append("quote timestamp not naive IST")
                state = CandidateState.INVALID
            duplicates = by_key[quote.key]
            if len(duplicates) > 1:
                reasons.append(f"duplicate canonical contract ({len(duplicates)} quotes)")
                state = CandidateState.INVALID
            verdicts[quote.key] = ContractCandidateVerdict(
                key=quote.key, state=state, reasons=tuple(reasons), eligible=False
            )

        evidence.append(
            SelectionEvidence(
                "candidates", "count", str(len(considered)),
                f"resolved side {side.value} over {snapshot.expiry.isoformat()}",
            )
        )
        return _VerdictTable(by_key=verdicts), considered

    def _resolve_strike(
        self,
        config: ContractSelectionConfig,
        spot: Decimal,
        strikes: tuple[Decimal, ...],
        evidence: list[SelectionEvidence],
    ) -> tuple[Decimal | None, str | None, bool]:
        """Return ``(strike, failure_reason, unavailable_data)``."""
        if not strikes:
            evidence.append(SelectionEvidence("strike", "policy", config.strike_policy.value, "no strikes in chain"))
            return (None, "no strikes present in chain for the resolved side", True)

        atm_index, atm_strike = min(
            enumerate(strikes), key=lambda pair: (abs(pair[1] - spot), pair[1])
        )
        if config.strike_policy is StrikePolicy.ATM:
            resolved = atm_strike
            evidence.append(
                SelectionEvidence("strike", "target", str(spot), f"ATM -> {resolved:g}")
            )
            return (resolved, None, False)

        offset = config.atm_offset
        if config.offset_units is OffsetUnits.STRIKES:
            index = atm_index + int(offset)
            if index < 0 or index >= len(strikes):
                evidence.append(
                    SelectionEvidence(
                        "strike", "offset", str(offset),
                        f"index {index} out of [{0},{len(strikes) - 1}]",
                    )
                )
                return (
                    None,
                    f"configured strike offset is unavailable in the chain (needs strike index {index})",
                    False,
                )
            resolved = strikes[index]
            evidence.append(
                SelectionEvidence("strike", "target", str(spot), f"ATM_OFFSET(strikes) -> {resolved:g}")
            )
            return (resolved, None, False)

        target = atm_strike + offset
        resolved = min(strikes, key=lambda s: (abs(s - target), s))
        evidence.append(
            SelectionEvidence("strike", "target", str(target), f"ATM_OFFSET(points) -> {resolved:g}")
        )
        return (resolved, None, False)

    def _market_gate(self, quote: OptionQuote, config: ContractSelectionConfig) -> tuple[list[str], CandidateState]:
        reasons: list[str] = []
        state = CandidateState.VALID

        bid = quote.bid
        ask = quote.ask

        if config.require_two_sided_book or config.require_positive_bid or config.require_positive_ask:
            if bid is None:
                reasons.append("bid not supplied; never assumed available")
                state = CandidateState.UNAVAILABLE
            if ask is None:
                reasons.append("ask not supplied; never assumed available")
                state = CandidateState.UNAVAILABLE
            if bid is not None and ask is not None:
                if bid > ask:
                    reasons.append(f"crossed bid/ask (bid {bid} > ask {ask})")
                    state = CandidateState.INVALID
                else:
                    if config.require_positive_bid and bid <= 0:
                        reasons.append("bid not positive")
                        state = CandidateState.UNAVAILABLE
                    if config.require_positive_ask and ask <= 0:
                        reasons.append("ask not positive")
                        state = CandidateState.UNAVAILABLE
                    mid = (bid + ask) / 2
                    spread = ask - bid
                    if mid > 0:
                        if config.max_spread_points is not None and spread > config.max_spread_points:
                            reasons.append(f"spread {spread:g} > max {config.max_spread_points:g} pts")
                        if config.max_spread_pct is not None:
                            pct = (spread / mid) * _DECIMAL_100
                            if pct > config.max_spread_pct:
                                reasons.append(f"spread ratio {pct:.4f}% > max {config.max_spread_pct:g}%")
                    else:
                        reasons.append("zero mid cannot compute spread ratio")
                        state = CandidateState.UNAVAILABLE

        if config.require_last_price:
            if quote.last_price is None:
                reasons.append("last_price not supplied; never assumed available")
                state = CandidateState.UNAVAILABLE

        if config.min_open_interest is not None:
            if quote.open_interest is None:
                reasons.append("open_interest not supplied; never assumed zero")
                state = CandidateState.UNAVAILABLE
            elif quote.open_interest < config.min_open_interest:
                reasons.append(f"open_interest {quote.open_interest} < min {config.min_open_interest}")
        if config.min_open_interest_change is not None:
            if quote.oi_change is None:
                reasons.append("oi_change not supplied; never assumed zero")
                state = CandidateState.UNAVAILABLE
            elif quote.oi_change < config.min_open_interest_change:
                reasons.append(f"oi_change {quote.oi_change} < min {config.min_open_interest_change}")
        if config.min_volume is not None:
            if quote.volume is None:
                reasons.append("volume not supplied; never assumed zero")
                state = CandidateState.UNAVAILABLE
            elif quote.volume < config.min_volume:
                reasons.append(f"volume {quote.volume} < min {config.min_volume}")
        if config.min_iv is not None:
            if quote.iv is None:
                reasons.append("iv not supplied; never assumed zero")
                state = CandidateState.UNAVAILABLE
            elif quote.iv < config.min_iv:
                reasons.append(f"iv {quote.iv:g} < min {config.min_iv:g}")

        return reasons, state

    @staticmethod
    def _spread_ratio(quote: OptionQuote) -> Decimal:
        if quote.bid is None or quote.ask is None or quote.bid + quote.ask == 0:
            return Decimal("0")
        return (quote.ask - quote.bid) / ((quote.bid + quote.ask) / 2)

    @staticmethod
    def _merge_state(current: CandidateState, market: CandidateState) -> CandidateState:
        order = {CandidateState.VALID: 0, CandidateState.UNAVAILABLE: 1, CandidateState.INVALID: 2}
        return max((current, market), key=lambda s: order[s])

    def _empty_outcome(self, considered_verdicts: Sequence[ContractCandidateVerdict]) -> SelectionOutcome:
        states = [v.state for v in considered_verdicts]
        if CandidateState.INVALID in states:
            return SelectionOutcome.INVALID_DATA
        if CandidateState.UNAVAILABLE in states:
            return SelectionOutcome.UNAVAILABLE_DATA
        return SelectionOutcome.NO_ELIGIBLE_CONTRACT

    # -------------------------------------------------------------- results

    def _plain_result(
        self,
        request: ContractSelectionRequest,
        outcome: SelectionOutcome,
        reason: str,
        *,
        evidence: list[SelectionEvidence] | None = None,
        verdicts: Sequence[ContractCandidateVerdict] = (),
        fingerprints: tuple[str, ...] | None = None,
        resolved_snapshot: OptionChainSnapshot | None = None,
    ) -> ContractSelectionResult:
        return self._result(
            request,
            outcome,
            selected=None,
            resolved_snapshot=resolved_snapshot,
            resolved_strike=None,
            resolved_side=None,
            verdicts=verdicts,
            fingerprints=fingerprints
            if fingerprints is not None
            else tuple(
                chain_fingerprint(s)
                for s in sorted(request.snapshots, key=lambda s: (s.expiry.isoformat(), chain_fingerprint(s)))
            ),
            evidence=(evidence or []) + [SelectionEvidence("outcome", "reason", outcome.value, reason)],
            extra_reasons=(reason,),
        )

    def _result(
        self,
        request: ContractSelectionRequest,
        outcome: SelectionOutcome,
        *,
        selected: OptionQuote | None,
        resolved_snapshot: OptionChainSnapshot | None,
        resolved_strike: Decimal | None,
        resolved_side: OptionSide | None,
        verdicts: Sequence[ContractCandidateVerdict],
        fingerprints: tuple[str, ...],
        evidence: list[SelectionEvidence],
        extra_reasons: Sequence[str] = (),
    ) -> ContractSelectionResult:
        ordered_verdicts = tuple(sorted(verdicts, key=lambda v: v.key))
        considered = tuple(v.key for v in ordered_verdicts)
        reasons: list[str] = list(extra_reasons)
        for verdict in ordered_verdicts:
            if not verdict.eligible and verdict.reasons:
                reasons.append(f"{verdict.key}: {verdict.reasons[0]}")
        if selected is not None:
            reasons.append(f"selected {selected.key}")
        final_evidence = tuple(evidence) + (
            SelectionEvidence("outcome", "state", outcome.value, "; ".join(reasons[:1]) or outcome.value),
        )
        return ContractSelectionResult(
            outcome=outcome,
            timestamp=request.timestamp,
            underlying=request.underlying,
            selected_contract=selected,
            resolved_expiry=resolved_snapshot.expiry if resolved_snapshot else None,
            resolved_strike=resolved_strike,
            resolved_side=resolved_side,
            spot_price=resolved_snapshot.spot_price if resolved_snapshot else None,
            regime_direction=request.regime.direction.value if request.regime and request.regime.direction else None,
            regime_headline=request.regime.regime if request.regime else None,
            expiry_policy=self.config.expiry_policy.value,
            strike_policy=self.config.strike_policy.value,
            candidates_considered=considered,
            candidate_verdicts=ordered_verdicts,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            chain_fingerprints=fingerprints,
            evidence=final_evidence,
            rules_version=self.config.version,
            schema_version=self.schema_version,
            engine_version=self.engine_version,
        )


@dataclass(frozen=True)
class _VerdictTable:
    by_key: dict[str, ContractCandidateVerdict] = field(default_factory=dict)


__all__ = [
    "CONTRACT_CONFIG_DEFAULT",
    "ContractSelectionConfig",
    "ContractSelectionEngine",
    "ENGINE_VERSION",
    "SCHEMA_VERSION",
    "SELECTOR_RULES_VERSION",
    "TIE_BREAK_ORDER",
    "pool_fingerprint",
    "stable_candidate_key",
]

CONTRACT_CONFIG_DEFAULT = ContractSelectionConfig()