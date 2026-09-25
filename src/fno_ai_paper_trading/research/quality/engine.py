"""Phase 9 — deterministic option trade-quality engine.

:class:`TradeQualityEngine` consumes a Phase 8 :class:`ContractSelectionResult`
and the Phase 5/6 :class:`OptionChainSnapshot` available at decision time ``t``
and decides whether the selected contract is of sufficient *observable market
quality* for the next risk layer to consider. It answers only that question —
it never decides that a trade should be taken and never emits a BUY/SELL side.

Design rules (inherit Phases 5-8):

* every threshold lives in the versioned :class:`TradeQualityConfig`;
  engineering defaults are labelled ``ENGINEERING_DEFAULT``, none were tuned on
  any protected-OOS data;
* dimension outcome precedence is ``INVALID > UNAVAILABLE > FAIL > PASS`` and
  is applied only over **binding** dimensions (rule-engaged or explicitly
  required); advisory dimensions are reported but never degrade the outcome;
* *absence* is explicit: missing bid/ask/OI/volume/OI-change/IV/greeks/premium
  stay ``None`` and are never converted to zero (no-fabrication, inherit Phase 5);
* ``greeks_rho`` is never mandatory (Phase 6: not supplied by the provider);
* ``quote.timestamp`` is the adapter receive instant (Phase 6 verified) — the
  freshness evidence records this caveat instead of pretending it is market time;
* no look-ahead: evaluation at ``t`` uses only artifacts stamped at/before ``t``
  and the pre-existing configuration (no clock reads);
* forbidden imports: execution, broker, Upstox order code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from fno_ai_paper_trading.research.options.models import (
    Greeks,
    OptionChainSnapshot,
    OptionQuote,
)
from fno_ai_paper_trading.research.options.normalization import chain_fingerprint
from fno_ai_paper_trading.research.quality.models import (
    DIMENSION_ORDER,
    QualityDimension,
    QualityDimensionVerdict,
    QualityEvidence,
    QualityOutcome,
    QualityState,
    TradeQualityRequest,
    TradeQualityResult,
    _outcome_for,
    _state_priority,
)
from fno_ai_paper_trading.research.selection import ContractSelectionResult
from fno_ai_paper_trading.utils.functions import to_decimal

QUALITY_ENGINE_VERSION = "1.0.0"
QUALITY_SCHEMA_VERSION = "1.0.0"
QUALITY_RULES_VERSION = "1.0.0"

_DECIMAL_100 = Decimal("100")

# Phase 6 verified provider limitation: option quotes carry no provider market
# timestamp; the adapter stamps the receive instant into quote.timestamp.
PROVIDER_TIMESTAMP_CAVEAT = (
    "quote.timestamp is the adapter receive instant, not provider market time "
    "(Phase 6 verified: provider option quotes carry no market timestamp)"
)

_KNOWN_GREEKS = {"delta", "gamma", "theta", "vega", "rho"}


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def selection_fingerprint(selection: ContractSelectionResult) -> str:
    """Deterministic SHA-256 over the selection's audit dict (stable JSON)."""
    return hashlib.sha256(_stable_json(selection.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TradeQualityConfig:
    """Explicit, versioned trade-quality rules.

    A dimension is **binding** (counts toward the overall outcome) when one of
    its rules is engaged, when it is named in ``required_dimensions``, or when
    it is intrinsically mandatory — freshness, bid/ask and selection
    consistency are always engaged. Premium/liquidity/IV/greeks are advisory by
    default and become binding only once a rule is configured or the dimension
    is required. Every threshold is an explicit engineering constant labelled
    ``ENGINEERING_DEFAULT``; none was tuned on any protected-OOS data.
    """

    version: str = QUALITY_RULES_VERSION

    # freshness (always engaged)
    max_quote_age_seconds: int | None = None
    max_snapshot_age_seconds: int | None = None
    max_selection_age_seconds: int | None = None
    require_provider_market_timestamp: bool = False

    # bid/ask (always engaged)
    require_positive_bid: bool = True
    require_positive_ask: bool = True
    max_spread_points: int | float | str | Decimal | None = None
    max_spread_pct: int | float | str | Decimal | None = None

    # premium (advisory by default)
    require_last_price: bool = False
    require_positive_ltp: bool = False
    require_ltp_in_spread: bool = False
    max_premium_points: int | float | str | Decimal | None = None

    # liquidity (advisory by default)
    min_open_interest: int | None = None
    min_volume: int | None = None
    min_oi_change: int | None = None

    # iv (advisory by default)
    min_iv: int | float | str | Decimal | None = None
    max_iv: int | float | str | Decimal | None = None

    # greeks (advisory by default)
    required_greeks: tuple[str, ...] = ()

    # general
    required_dimensions: tuple[str, ...] = ()
    include_composite: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("max_quote_age_seconds", self.max_quote_age_seconds),
            ("max_snapshot_age_seconds", self.max_snapshot_age_seconds),
            ("max_selection_age_seconds", self.max_selection_age_seconds),
            ("min_open_interest", self.min_open_interest),
            ("min_volume", self.min_volume),
            ("min_oi_change", self.min_oi_change),
        ):
            if value is not None:
                if not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative int or None")

        for name in (
            "max_spread_points",
            "max_spread_pct",
            "max_premium_points",
            "min_iv",
            "max_iv",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value))
                if getattr(self, name) < 0:
                    raise ValueError(f"{name} must be >= 0")

        if (
            self.min_iv is not None
            and self.max_iv is not None
            and self.min_iv > self.max_iv
        ):
            raise ValueError("min_iv must not exceed max_iv")

        for required in self.required_dimensions:
            dimension = QualityDimension(str(required))
            if dimension not in DIMENSION_ORDER:
                raise ValueError(f"unknown required dimension {required!r}")
        object.__setattr__(self, "required_dimensions", tuple(str(d) for d in self.required_dimensions))

        for name in self.required_greeks:
            if name not in _KNOWN_GREEKS:
                raise ValueError(f"unknown required greek {name!r}")
        object.__setattr__(self, "required_greeks", tuple(dict.fromkeys(self.required_greeks)))

    # -------------------------------------------------------- engagement

    def dimension_binding(self, dimension: QualityDimension) -> bool:
        if dimension in (
            QualityDimension.FRESHNESS,
            QualityDimension.BID_ASK,
            QualityDimension.SELECTION_CONSISTENCY,
        ):
            return True
        if dimension.value in self.required_dimensions:
            return True
        if dimension is QualityDimension.PREMIUM:
            return (
                self.require_last_price
                or self.require_positive_ltp
                or self.require_ltp_in_spread
                or self.max_premium_points is not None
            )
        if dimension is QualityDimension.LIQUIDITY:
            return any(
                value is not None
                for value in (self.min_open_interest, self.min_volume, self.min_oi_change)
            )
        if dimension is QualityDimension.IV:
            return self.min_iv is not None or self.max_iv is not None
        if dimension is QualityDimension.GREEKS:
            return bool(self.required_greeks)
        return False

    def to_dict(self) -> dict[str, object]:
        return dict(
            sorted(
                {
                    "version": self.version,
                    "max_quote_age_seconds": self.max_quote_age_seconds,
                    "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                    "max_selection_age_seconds": self.max_selection_age_seconds,
                    "require_provider_market_timestamp": self.require_provider_market_timestamp,
                    "require_positive_bid": self.require_positive_bid,
                    "require_positive_ask": self.require_positive_ask,
                    "max_spread_points": str(self.max_spread_points) if self.max_spread_points is not None else None,
                    "max_spread_pct": str(self.max_spread_pct) if self.max_spread_pct is not None else None,
                    "require_last_price": self.require_last_price,
                    "require_positive_ltp": self.require_positive_ltp,
                    "require_ltp_in_spread": self.require_ltp_in_spread,
                    "max_premium_points": str(self.max_premium_points) if self.max_premium_points is not None else None,
                    "min_open_interest": self.min_open_interest,
                    "min_volume": self.min_volume,
                    "min_oi_change": self.min_oi_change,
                    "min_iv": str(self.min_iv) if self.min_iv is not None else None,
                    "max_iv": str(self.max_iv) if self.max_iv is not None else None,
                    "required_greeks": list(self.required_greeks),
                    "required_dimensions": list(self.required_dimensions),
                    "include_composite": self.include_composite,
                    "binding_dimensions": [d.value for d in DIMENSION_ORDER if self.dimension_binding(d)],
                }.items()
            )
        )


class TradeQualityEngine:
    """Stateless deterministic trade-quality engine over one selection + chain."""

    def __init__(self, config: TradeQualityConfig | None = None) -> None:
        self.config = config or TradeQualityConfig()
        self.engine_version = QUALITY_ENGINE_VERSION
        self.schema_version = QUALITY_SCHEMA_VERSION

    # ------------------------------------------------------------- evaluate

    def evaluate(self, request: TradeQualityRequest) -> TradeQualityResult:
        evidence: list[QualityEvidence] = []
        config = self.config

        if request.timestamp.tzinfo is not None:
            return self._terminal(
                request,
                QualityOutcome.INVALID,
                "decision timestamp must be naive IST",
                evidence,
            )
        evidence.append(
            QualityEvidence("request", "timestamp", request.timestamp.isoformat(), "decision time")
        )

        selection = request.selection
        snapshot = request.snapshot

        fingerprint = selection_fingerprint(selection)
        evidence.append(
            QualityEvidence(
                "selection", "fingerprint", fingerprint,
                "audit fingerprint of the Phase 8 selection result",
            )
        )
        if (
            request.expected_selection_fingerprint is not None
            and request.expected_selection_fingerprint != fingerprint
        ):
            return self._terminal(
                request,
                QualityOutcome.INVALID,
                "selection fingerprint mismatch (expected "
                f"{request.expected_selection_fingerprint}, got {fingerprint})",
                evidence,
            )

        if not selection.has_selection or selection.selected_contract is None:
            evidence.append(
                QualityEvidence("selection", "has_selection", "False",
                                "selection did not select a contract")
            )
            return self._terminal(
                request,
                QualityOutcome.INVALID,
                "selection did not select a contract (outcome "
                f"{selection.outcome.value}); nothing to assess",
                evidence,
            )

        contract = selection.selected_contract
        key = contract.key
        evidence.append(QualityEvidence("contract", "key", key, "assessed contract"))

        chain_fp = chain_fingerprint(snapshot) if snapshot is not None else None
        if snapshot is not None:
            evidence.append(
                QualityEvidence("chain", "fingerprint", chain_fp, "normalized chain snapshot")
            )

        verdicts: list[QualityDimensionVerdict] = []
        reasons: list[str] = []

        # selection consistency (always binding)
        sc_reasons: list[str] = []
        sc_state = QualityState.VALID
        if selection.timestamp is None:
            sc_reasons.append("selection has no timestamp")
            sc_state = QualityState.UNAVAILABLE
        elif selection.timestamp.tzinfo is not None:
            sc_reasons.append("selection timestamp not naive IST")
            sc_state = QualityState.INVALID
        elif selection.timestamp > request.timestamp:
            sc_reasons.append("selection timestamp is in the future relative to decision time")
            sc_state = QualityState.INVALID
        elif (
            config.max_selection_age_seconds is not None
            and (request.timestamp - selection.timestamp).total_seconds()
            > config.max_selection_age_seconds
        ):
            age = int((request.timestamp - selection.timestamp).total_seconds())
            sc_reasons.append(f"selection stale: age {age}s > max {config.max_selection_age_seconds}s")
            sc_state = QualityState.FAIL

        if contract.expiry is not None and contract.expiry < request.timestamp.date():
            sc_reasons.append("contract expiry is before the decision date (expired)")
            sc_state = QualityState.INVALID

        if snapshot is None:
            sc_reasons.append("no chain snapshot supplied")
            sc_state = max((sc_state, QualityState.UNAVAILABLE), key=_state_priority)
        elif snapshot.timestamp.tzinfo is not None:
            sc_reasons.append("snapshot timestamp not naive IST")
            sc_state = QualityState.INVALID
        else:
            quote = next(
                (q for q in snapshot.quotes if q.key == key), None
            )
            if quote is None:
                sc_reasons.append("selected contract not present in the supplied chain snapshot")
                sc_state = max((sc_state, QualityState.UNAVAILABLE), key=_state_priority)
            else:
                evidence.append(
                    QualityEvidence("contract", "present_in_chain", "true",
                                    "selected contract located in the current snapshot")
                )

        verdicts.append(
            QualityDimensionVerdict(
                dimension=QualityDimension.SELECTION_CONSISTENCY,
                state=sc_state,
                field="selection_consistency",
                value=f"fingerprint={fingerprint}" + (f" chain={chain_fp}" if chain_fp else " chain=None"),
                threshold=(
                    f"max_selection_age_seconds={config.max_selection_age_seconds}"
                    if config.max_selection_age_seconds is not None
                    else None
                ),
                rule="contract identity / timestamp relation / fingerprint / presence",
                reason="; ".join(sc_reasons) or "selection consistent with the current chain",
                binding=True,
            )
        )

        # Assess quote-level dimensions only when the contract is present and
        # the snapshot is usable; otherwise the dimension verdicts are
        # UNAVAILABLE (no data) or INVALID (broken snapshot timestamp).
        quote: OptionQuote | None
        usable = (
            snapshot is not None
            and snapshot.timestamp.tzinfo is None
            and next((q for q in snapshot.quotes if q.key == key), None) is not None
        )
        quote = (
            next((q for q in snapshot.quotes if q.key == key), None) if usable else None
        )

        if not usable:
            for dimension in (
                QualityDimension.FRESHNESS,
                QualityDimension.BID_ASK,
                QualityDimension.PREMIUM,
                QualityDimension.LIQUIDITY,
                QualityDimension.IV,
                QualityDimension.GREEKS,
            ):
                verdicts.append(self._unavailable_data_only(
                    dimension,
                    snapshot=snapshot,
                    evidence=evidence,
                ))
        else:
            assert quote is not None and snapshot is not None
            verdicts.append(
                self._evaluate_freshness(quote, snapshot, request, config, evidence)
            )
            verdicts.append(
                self._evaluate_bid_ask(quote, request, config, evidence)
            )
            verdicts.append(
                self._evaluate_premium(quote, config, evidence)
            )
            verdicts.append(
                self._evaluate_liquidity(quote, config, evidence)
            )
            verdicts.append(
                self._evaluate_iv(quote, config, evidence)
            )
            verdicts.append(
                self._evaluate_greeks(quote, config, evidence)
            )

        verdicts = tuple(sorted(verdicts, key=lambda v: DIMENSION_ORDER.index(v.dimension)))
        binding = [v for v in verdicts if v.binding]
        if binding:
            worst = max(binding, key=lambda v: _state_priority(v.state))
            outcome = _outcome_for(worst.state)
        else:
            outcome = QualityOutcome.PASS

        for verdict in binding:
            if verdict.state is not QualityState.VALID and verdict.reason:
                reasons.append(f"{verdict.dimension.value}: {verdict.reason}")

        composite = None
        if config.include_composite:
            composite = f"{sum(1 for v in binding if v.state is QualityState.VALID)}/{len(binding)}"

        final_evidence = tuple(evidence) + (
            QualityEvidence("outcome", "state", outcome.value,
                            "; ".join(reasons[:1]) or outcome.value),
        )
        return TradeQualityResult(
            outcome=outcome,
            timestamp=request.timestamp,
            contract_key=key,
            underlying_symbol=contract.instrument.underlying_symbol,
            selection_outcome=selection.outcome.value,
            selection_timestamp=selection.timestamp,
            selection_fingerprint=fingerprint,
            chain_fingerprint=chain_fp,
            dimensions=verdicts,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            evidence=final_evidence,
            composite=composite,
            rules_version=config.version,
            schema_version=self.schema_version,
            engine_version=self.engine_version,
        )

    # ------------------------------------------------- dimension evaluation

    def _terminal(
        self,
        request: TradeQualityRequest,
        outcome: QualityOutcome,
        reason: str,
        evidence: list[QualityEvidence],
    ) -> TradeQualityResult:
        return TradeQualityResult(
            outcome=outcome,
            timestamp=request.timestamp,
            contract_key=None,
            underlying_symbol=None,
            selection_outcome=request.selection.outcome.value,
            selection_timestamp=request.selection.timestamp,
            selection_fingerprint=None,
            chain_fingerprint=None,
            dimensions=(),
            rejection_reasons=(reason,),
            evidence=tuple(evidence) + (
                QualityEvidence("outcome", "state", outcome.value, reason),
            ),
            composite=None,
            rules_version=self.config.version,
            schema_version=self.schema_version,
            engine_version=self.engine_version,
        )

    def _unavailable_data_only(
        self,
        dimension: QualityDimension,
        *,
        snapshot: OptionChainSnapshot | None,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        reason = (
            "snapshot timestamp not naive IST" if snapshot is not None and snapshot.timestamp.tzinfo is not None
            else "contract not present in the supplied chain snapshot; its quality cannot be assessed"
        )
        evidence.append(
            QualityEvidence(dimension.value, "state", "UNAVAILABLE", reason)
        )
        return QualityDimensionVerdict(
            dimension=dimension,
            state=QualityState.UNAVAILABLE,
            field=dimension.value,
            reason=reason,
            binding=self.config.dimension_binding(dimension),
        )

    def _evaluate_freshness(
        self,
        quote: OptionQuote,
        snapshot: OptionChainSnapshot,
        request: TradeQualityRequest,
        config: TradeQualityConfig,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        states: list[tuple[str, QualityState, str]] = []
        threshold_parts: list[str] = []

        for label, ts in (("quote", quote.timestamp), ("snapshot", snapshot.timestamp)):
            if ts.tzinfo is not None:
                states.append((label, QualityState.INVALID, f"{label} timestamp not naive IST"))
                continue
            age = int((request.timestamp - ts).total_seconds())
            if age < 0:
                states.append((label, QualityState.INVALID, f"{label} timestamp in the future"))
                continue
            cap = config.max_quote_age_seconds if label == "quote" else config.max_snapshot_age_seconds
            if cap is not None:
                threshold_parts.append(f"max_{label}_age_seconds={cap}")
                if age > cap:
                    states.append(
                        (label, QualityState.FAIL, f"{label} stale: age {age}s > max {cap}s")
                    )
                else:
                    states.append((label, QualityState.VALID, f"{label} age {age}s <= {cap}s"))
            else:
                states.append((label, QualityState.VALID, f"{label} age {age}s (no ceiling configured)"))
            evidence.append(
                QualityEvidence("freshness", f"{label}_timestamp", ts.isoformat(),
                                PROVIDER_TIMESTAMP_CAVEAT if label == "quote" else "snapshot timestamp")
            )

        if config.require_provider_market_timestamp:
            threshold_parts.append("require_provider_market_timestamp=True")
            if quote.source_timestamp is None:
                states.append(
                    ("quote", QualityState.UNAVAILABLE,
                     "provider supplied no quote timestamp (Phase 6 limitation); receive instant is all we have")
                )
            else:
                states.append(("quote_market_time", QualityState.VALID, "provider timestamp present"))

        state = max((s for _, s, _ in states), key=_state_priority)
        reason = "; ".join(r for _, _, r in states) or "fresh"
        evidence.append(QualityEvidence("freshness", "state", state.value, reason))
        return QualityDimensionVerdict(
            dimension=QualityDimension.FRESHNESS,
            state=state,
            field="freshness",
            value=f"quote_ts={quote.timestamp.isoformat()} snapshot_ts={snapshot.timestamp.isoformat()}",
            threshold=(", ".join(threshold_parts)) or None,
            rule="quote/snapshot age vs decision time; provider timestamp representation",
            reason=reason,
            binding=True,
        )

    def _evaluate_bid_ask(
        self,
        quote: OptionQuote,
        request: TradeQualityRequest,
        config: TradeQualityConfig,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        reasons: list[str] = []
        state = QualityState.VALID
        threshold_parts: list[str] = []

        bid, ask = quote.bid, quote.ask
        if bid is None or ask is None:
            state = QualityState.UNAVAILABLE
            if bid is None:
                reasons.append("bid not supplied; never assumed available")
            if ask is None:
                reasons.append("ask not supplied; never assumed available")
            if (bid is not None) != (ask is not None):
                reasons.append("half-depth book (only one side supplied)")
        else:
            if bid > ask:
                state = QualityState.INVALID
                reasons.append(f"crossed bid/ask (bid {bid:g} > ask {ask:g})")
            if config.require_positive_bid:
                threshold_parts.append("require_positive_bid=True")
                if bid <= 0:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append("bid not positive")
            if config.require_positive_ask:
                threshold_parts.append("require_positive_ask=True")
                if ask <= 0:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append("ask not positive")

            spread = ask - bid
            mid = (bid + ask) / 2
            if config.max_spread_points is not None:
                threshold_parts.append(f"max_spread_points={config.max_spread_points:g}")
                if spread > config.max_spread_points:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append(
                        f"absolute spread {spread:g} > max {config.max_spread_points:g} pts"
                    )
            if config.max_spread_pct is not None:
                threshold_parts.append(f"max_spread_pct={config.max_spread_pct:g}")
                if mid <= 0:
                    state = max((state, QualityState.UNAVAILABLE), key=_state_priority)
                    reasons.append("zero mid cannot compute spread ratio")
                else:
                    pct = (spread / mid) * _DECIMAL_100
                    if pct > config.max_spread_pct:
                        state = max((state, QualityState.FAIL), key=_state_priority)
                        reasons.append(
                            f"spread ratio {pct:.4f}% > max {config.max_spread_pct:g}%"
                        )

        evidence.append(QualityEvidence("bid_ask", "state", state.value, "; ".join(reasons) or "ok"))
        return QualityDimensionVerdict(
            dimension=QualityDimension.BID_ASK,
            state=state,
            field="bid_ask",
            value=(
                f"bid={bid:g} ask={ask:g}" if bid is not None and ask is not None
                else f"bid={bid} ask={ask}"
            ),
            threshold=(", ".join(threshold_parts)) or None,
            rule="two-sided book, positive sides, spread vs configured caps",
            reason="; ".join(reasons) or "valid two-sided book",
            binding=True,
        )

    def _evaluate_premium(
        self,
        quote: OptionQuote,
        config: TradeQualityConfig,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        engaged = config.dimension_binding(QualityDimension.PREMIUM)
        reasons: list[str] = []
        state = QualityState.VALID
        threshold_parts: list[str] = []

        if not engaged:
            evidence.append(
                QualityEvidence("premium", "state", "VALID",
                                "premium rules not configured (advisory; no fabrication)")
            )
            return QualityDimensionVerdict(
                dimension=QualityDimension.PREMIUM,
                state=state,
                field="premium",
                value=None,
                reason="advisory: no premium rule engaged",
                binding=False,
            )

        ltp = quote.last_price
        if config.require_last_price:
            threshold_parts.append("require_last_price=True")
            if ltp is None:
                state = QualityState.UNAVAILABLE
                reasons.append("last_price not supplied; never invented")
        if ltp is not None:
            if ltp < 0:
                state = max((state, QualityState.INVALID), key=_state_priority)
                reasons.append(f"negative last_price {ltp:g}")
            if config.require_positive_ltp:
                threshold_parts.append("require_positive_ltp=True")
                if ltp <= 0:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append("last_price not positive")
            if config.require_ltp_in_spread:
                threshold_parts.append("require_ltp_in_spread=True")
                if quote.bid is not None and quote.ask is not None:
                    if ltp < quote.bid or ltp > quote.ask:
                        state = max((state, QualityState.FAIL), key=_state_priority)
                        reasons.append(
                            f"last_price {ltp:g} outside [{quote.bid:g}, {quote.ask:g}]"
                        )
            if config.max_premium_points is not None:
                threshold_parts.append(f"max_premium_points={config.max_premium_points:g}")
                if ltp > config.max_premium_points:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append(
                        f"premium {ltp:g} > configured cap {config.max_premium_points:g}"
                    )

        evidence.append(QualityEvidence("premium", "state", state.value, "; ".join(reasons) or "ok"))
        return QualityDimensionVerdict(
            dimension=QualityDimension.PREMIUM,
            state=state,
            field="premium",
            value=f"ltp={ltp if ltp is None else f'{ltp:g}'}",
            threshold=(", ".join(threshold_parts)) or None,
            rule="last-price presence/positivity/consistency with book and an explicit cap",
            reason="; ".join(reasons) or "premium rule(s) satisfied",
            binding=True,
        )

    def _evaluate_liquidity(
        self,
        quote: OptionQuote,
        config: TradeQualityConfig,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        engaged = config.dimension_binding(QualityDimension.LIQUIDITY)
        reasons: list[str] = []
        state = QualityState.VALID
        threshold_parts: list[str] = []

        if not engaged:
            evidence.append(
                QualityEvidence("liquidity", "state", "VALID",
                                "liquidity rules not configured (advisory; missing OI/volume not invented)")
            )
            return QualityDimensionVerdict(
                dimension=QualityDimension.LIQUIDITY,
                state=state,
                field="liquidity",
                value=None,
                reason="advisory: no liquidity rule engaged",
                binding=False,
            )

        for field_name, threshold, observed in (
            ("open_interest", config.min_open_interest, quote.open_interest),
            ("volume", config.min_volume, quote.volume),
            ("oi_change", config.min_oi_change, quote.oi_change),
        ):
            if threshold is None:
                continue
            threshold_parts.append(f"min_{field_name}={threshold}")
            if observed is None:
                state = max((state, QualityState.UNAVAILABLE), key=_state_priority)
                reasons.append(f"{field_name} not supplied; never assumed zero")
            elif observed < 0:
                state = QualityState.INVALID
                reasons.append(f"negative {field_name} {observed}")
            elif observed < threshold:
                state = max((state, QualityState.FAIL), key=_state_priority)
                reasons.append(f"{field_name} {observed} < min {threshold}")

        evidence.append(QualityEvidence("liquidity", "state", state.value, "; ".join(reasons) or "ok"))
        return QualityDimensionVerdict(
            dimension=QualityDimension.LIQUIDITY,
            state=state,
            field="liquidity",
            value=f"oi={quote.open_interest} oi_change={quote.oi_change} volume={quote.volume}",
            threshold=(", ".join(threshold_parts)) or None,
            rule="real OI/OI-change/volume against configured minimums",
            reason="; ".join(reasons) or "liquidity rule(s) satisfied",
            binding=True,
        )

    def _evaluate_iv(
        self,
        quote: OptionQuote,
        config: TradeQualityConfig,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        engaged = config.dimension_binding(QualityDimension.IV)
        reasons: list[str] = []
        state = QualityState.VALID
        threshold_parts: list[str] = []

        if not engaged:
            evidence.append(
                QualityEvidence("iv", "state", "VALID",
                                "IV rules not configured; missing IV does not fail the contract")
            )
            return QualityDimensionVerdict(
                dimension=QualityDimension.IV,
                state=state,
                field="iv",
                value=None,
                reason="advisory: no IV rule engaged",
                binding=False,
            )

        iv = quote.iv
        if iv is None:
            state = QualityState.UNAVAILABLE
            reasons.append("iv not supplied; never fabricated")
        else:
            if iv < 0:
                state = QualityState.INVALID
                reasons.append(f"negative iv {iv:g}")
            if config.min_iv is not None:
                threshold_parts.append(f"min_iv={config.min_iv:g}")
                if iv < config.min_iv:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append(f"iv {iv:g} < min {config.min_iv:g}")
            if config.max_iv is not None:
                threshold_parts.append(f"max_iv={config.max_iv:g}")
                if iv > config.max_iv:
                    state = max((state, QualityState.FAIL), key=_state_priority)
                    reasons.append(f"iv {iv:g} > max {config.max_iv:g}")

        evidence.append(QualityEvidence("iv", "state", state.value, "; ".join(reasons) or "ok"))
        return QualityDimensionVerdict(
            dimension=QualityDimension.IV,
            state=state,
            field="iv",
            value=f"iv={iv if iv is None else f'{iv:g}'}",
            threshold=(", ".join(threshold_parts)) or None,
            rule="IV presence, non-negativity and configured bounds (if any)",
            reason="; ".join(reasons) or "IV rule(s) satisfied",
            binding=True,
        )

    def _evaluate_greeks(
        self,
        quote: OptionQuote,
        config: TradeQualityConfig,
        evidence: list[QualityEvidence],
    ) -> QualityDimensionVerdict:
        engaged = config.dimension_binding(QualityDimension.GREEKS)
        greeks: Greeks = quote.greeks
        reasons: list[str] = []
        state = QualityState.VALID
        threshold_parts: list[str] = []

        if not engaged:
            evidence.append(
                QualityEvidence("greeks", "state", "VALID",
                                "greeks advisory; rho not mandatory (Phase 6: provider does not supply rho); "
                                "missing greeks not invented")
            )
            return QualityDimensionVerdict(
                dimension=QualityDimension.GREEKS,
                state=state,
                field="greeks",
                value=None,
                reason="advisory: no greek rule engaged; missing greeks remain missing",
                binding=False,
            )

        for name in config.required_greeks:
            threshold_parts.append(f"required_greeks={name}")
            value = getattr(greeks, name)
            if value is None:
                state = max((state, QualityState.UNAVAILABLE), key=_state_priority)
                reasons.append(f"{name} not supplied; never fabricated")
            elif name in ("gamma", "vega") and value < 0:
                state = QualityState.INVALID
                reasons.append(f"negative {name} {value:g}")

        available = greeks.available
        evidence.append(QualityEvidence("greeks", "state", state.value, "; ".join(reasons) or "ok"))
        return QualityDimensionVerdict(
            dimension=QualityDimension.GREEKS,
            state=state,
            field="greeks",
            value=(
                "delta=" + (f"{greeks.delta:g}" if greeks.delta is not None else "None")
                + " gamma=" + (f"{greeks.gamma:g}" if greeks.gamma is not None else "None")
                + " theta=" + (f"{greeks.theta:g}" if greeks.theta is not None else "None")
                + " vega=" + (f"{greeks.vega:g}" if greeks.vega is not None else "None")
                + " rho=" + (f"{greeks.rho:g}" if greeks.rho is not None else "None")
                if available
                else None
            ),
            threshold=(", ".join(threshold_parts)) or None,
            rule="required greek presence and gamma/vega non-negativity",
            reason="; ".join(reasons) or "required greek(s) satisfied",
            binding=True,
        )


__all__ = [
    "PROVIDER_TIMESTAMP_CAVEAT",
    "QUALITY_ENGINE_VERSION",
    "QUALITY_RULES_VERSION",
    "QUALITY_SCHEMA_VERSION",
    "TradeQualityConfig",
    "TradeQualityEngine",
    "selection_fingerprint",
]