"""Phase 12 tests — leakage-safe options research and OOS evaluation framework.

Hermetic unit tests for the capability gate, the frozen options_chain_bar
dataset contract, leakage/OOS protection, preregistered protocol pinning, and
ledger evaluation metrics (Phase 11 ``build_daily_report`` ledger integration).
No network, no wall-clock reads, no credentials, no ``execution.*``/Upstox
imports in the module under test, and synthetic fixtures carry an explicit
``SYNTHETIC_FIXTURE`` label so they can never be presented as observed market
data.
"""
from __future__ import annotations

import ast
import csv
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fno_ai_paper_trading.options_research import (
    HISTORICAL_OPTIONS_DATA_INVALID,
    HISTORICAL_OPTIONS_DATA_PARTIAL,
    HISTORICAL_OPTIONS_DATA_READY,
    HISTORICAL_OPTIONS_DATA_UNAVAILABLE,
    STRATEGY_SPECIFICATION_REQUIRED,
    ConsumedWindowRegistry,
    DatasetFinding,
    DatasetValidation,
    LedgerRow,
    LedgerSample,
    ProtectedOosRefusal,
    ReadinessAssessment,
    ResearchProtocol,
    SourceFinding,
    SplitPlan,
    assess_readiness,
    check_no_lookahead,
    classify_day,
    classify_source,
    compute_metrics,
    default_protocol,
    freeze,
    protocol_fingerprint,
    sample_from_daily_report,
    validate_dataset_directory,
    validate_rows,
    verify_no_protected_reuse,
)
from fno_ai_paper_trading.options_research.capability import (
    SOURCE_ADEQUATE,
    SOURCE_BLOCKED,
    SOURCE_INSUFFICIENT,
    SOURCE_NOT_HISTORICAL,
    SOURCE_SYNTHETIC,
)
from fno_ai_paper_trading.options_research.dataset import (
    CODE_COVERAGE_GAP,
    CODE_IDENTITY_FUTURES,
    CODE_IDENTITY_OPTION,
    CODE_IDENTITY_UNDERLYING,
    CODE_MANIFEST_HASH,
    CODE_MANIFEST_MISSING,
    CODE_NEGATIVE,
    CODE_OHLC,
    CODE_PREMIUM_UNDERLYING,
    CODE_SCHEMA,
    CODE_TS_ALIGN,
    CODE_TS_DUPLICATE,
    CODE_TS_ORDER,
    CODE_TS_PARSE,
    CODE_TS_TZ,
    STATUS_INVALID,
    STATUS_QUARANTINED,
    STATUS_VALID,
    OPTION_BAR_FIELDS,
    manifest_synthetic_ok,
    sha256_text,
    file_sha256_lf,
)
from fno_ai_paper_trading.options_research.windows import (
    PROTECTED_OOS_END,
    PROTECTED_OOS_START,
    WINDOW_FRESH,
    WINDOW_PROTECTED,
    WINDOW_RESEARCH,
    collect_protected_days,
    validate_split,
)
from fno_ai_paper_trading.paper_track.options_paper import (
    FILL_MODEL_VERSION,
    LifecyclePhase,
    LifecycleRecord,
    build_daily_report,
)
from fno_ai_paper_trading.paper_track.options_paper.models import (
    EntryFill,
    ExitFill,
    Financials,
    Reconciliation,
    Step,
)

ROOT = Path(__file__).resolve().parents[1]
DEC = Decimal

# ---------------------------------------------------------------- capability gate

ADEQUATE = SourceFinding(
    source_id="vendor-hx-options-5m",
    provider="vendor-historical",
    product="index-options-chain",
    date_coverage="2022-01-03..2025-10-03",
    granularity="5m",
    historical_retrieval=True,
    observed_bid_ask=True,
    observed_oi=True,
    timezone="Asia/Kolkata",
    reproducible=True,
    checksum_provenance=True,
    supports_realistic_simulation=True,
)

NOT_HISTORICAL_UPSTOX = SourceFinding(
    source_id="upstox-live-chain",
    provider="upstox",
    product="option-chain",
    granularity="snapshot",
    historical_retrieval=False,
    observed_bid_ask=True,
    observed_ltp=True,
    observed_oi=True,
    timezone="Asia/Kolkata",
    reproducible=False,
    notes="live-snapshot only (GET /v2/option/chain + /v2/option/contract)",
)

BLOCKED_EXPIRED = SourceFinding(
    source_id="upstox-expired-contracts",
    provider="upstox",
    product="expired-instruments",
    date_coverage="2022-01-01..",
    historical_retrieval=True,
    observed_bid_ask=True,
    observed_oi=True,
    reproducible=False,
    checksum_provenance=False,
    notes="HTTP 401 UDAPI1149: Upstox Plus plan required",
)

SYNTHETIC = replace(ADEQUATE, source_id="synthetic-001", synthetic=True)


class TestCapabilityGate:
    def test_unavailable_when_only_live_sources(self) -> None:
        assessment = assess_readiness(sources=(NOT_HISTORICAL_UPSTOX,))
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_UNAVAILABLE

    def test_unavailable_when_sources_blocked(self) -> None:
        assessment = assess_readiness(sources=(NOT_HISTORICAL_UPSTOX, BLOCKED_EXPIRED))
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_UNAVAILABLE

    def test_ready_when_validated_dataset_adequate_source(self) -> None:
        dataset = DatasetFinding(
            present=True,
            validated=True,
            source_id=ADEQUATE.source_id,
            fingerprint="a" * 64,
            schema_version="options_chain_bar_v1",
            num_rows=150000,
        )
        assessment = assess_readiness(sources=(ADEQUATE,), dataset=dataset)
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_READY

    def test_partial_when_adequate_source_but_no_dataset(self) -> None:
        assessment = assess_readiness(sources=(ADEQUATE,))
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_PARTIAL

    def test_partial_when_insufficient_source(self) -> None:
        daily_only = replace(ADEQUATE, granularity="1d", observed_bid_ask=False)
        assessment = assess_readiness(sources=(daily_only,))
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_PARTIAL

    def test_invalid_when_dataset_quarantined(self) -> None:
        dataset = DatasetFinding(present=True, validated=False, num_rows=10)
        assessment = assess_readiness(sources=(ADEQUATE,), dataset=dataset)
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_INVALID

    def test_synthetic_source_and_dataset_never_satisfy_gate(self) -> None:
        dataset = DatasetFinding(
            present=True,
            validated=True,
            source_id=SYNTHETIC.source_id,
            synthetic=True,
            fingerprint="f" * 64,
            schema_version="options_chain_bar_v1",
            num_rows=100,
        )
        assessment = assess_readiness(sources=(SYNTHETIC,), dataset=dataset)
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_PARTIAL
        assert SOURCE_SYNTHETIC in assessment.source_verdicts.values()

    def test_classify_source_codes(self) -> None:
        assert classify_source(SYNTHETIC) == SOURCE_SYNTHETIC
        assert classify_source(NOT_HISTORICAL_UPSTOX) == SOURCE_NOT_HISTORICAL
        assert classify_source(BLOCKED_EXPIRED) == SOURCE_BLOCKED
        assert classify_source(replace(ADEQUATE, supports_realistic_simulation=False)) == SOURCE_INSUFFICIENT
        assert classify_source(ADEQUATE) == SOURCE_ADEQUATE

    def test_no_claimed_verdict_without_evidence(self) -> None:
        assessment = assess_readiness()
        assert assessment.verdict == HISTORICAL_OPTIONS_DATA_UNAVAILABLE


# ---------------------------------------------------------------- dataset contract

def _option_row(
    ts: str = "2024-01-15T09:15:00",
    key: str = "OPT|NIFTY 25JAN2024 24600 CE",
    asset: str = "CE",
    expiry: str = "2024-01-25",
    strike: str = "24600",
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "ts": ts,
        "instrument_key": key,
        "asset": asset,
        "expiry": expiry,
        "strike": strike,
        "open": "100.50",
        "high": "103.00",
        "low": "99.75",
        "close": "102.25",
        "volume": "1200",
        "oi": "45000",
        "source": "SYNTHETIC_FIXTURE",
    }
    row.update(overrides)
    return row


def _underlying_row(ts: str = "2024-01-15T09:15:00", **overrides: object) -> dict[str, object]:
    row = _option_row(
        ts=ts,
        key="NSE_INDEX|Nifty 50",
        asset="UNDERLYING_INDEX",
        expiry="",
        strike="",
        open="24650.05",
        high="24680.00",
        low="24620.00",
        close="24660.00",
        volume="500000",
        oi="",
    )
    row.update(overrides)
    return row


class TestDatasetRowValidation:
    def test_valid_rows_pass(self) -> None:
        result = validate_rows(
            [
                _underlying_row("2024-01-15T09:15:00"),
                _underlying_row("2024-01-15T09:20:00"),
                _option_row("2024-01-15T09:15:00"),
                _option_row("2024-01-15T09:20:00"),
            ]
        )
        assert result.ok
        assert result.status() == STATUS_VALID
        assert result.num_rows == 4

    def test_schema_extra_field_rejected(self) -> None:
        result = validate_rows([_option_row(extra_field="x")])
        assert CODE_SCHEMA in result.codes

    def test_ohlc_violation_rejected(self) -> None:
        result = validate_rows([_option_row(high="98.00")])
        assert CODE_OHLC in result.codes

    def test_negative_price_rejected(self) -> None:
        result = validate_rows([_option_row(close="-1.00")])
        assert CODE_NEGATIVE in result.codes

    def test_option_without_strike_rejected(self) -> None:
        result = validate_rows([_option_row(strike="")])
        assert CODE_IDENTITY_OPTION in result.codes

    def test_option_without_expiry_rejected(self) -> None:
        result = validate_rows([_option_row(expiry="")])
        assert CODE_IDENTITY_OPTION in result.codes

    def test_underlying_row_with_strike_rejected(self) -> None:
        result = validate_rows([_underlying_row(strike="24600")])
        assert CODE_IDENTITY_UNDERLYING in result.codes

    def test_underlying_substituted_as_option_rejected(self) -> None:
        """NIFTY index price relabelled as an option premium must be caught."""
        result = validate_rows(
            [_option_row(key="NSE_INDEX|Nifty 50", asset="CE", strike="", expiry="")]
        )
        assert CODE_PREMIUM_UNDERLYING in result.codes

    def test_futures_without_expiry_rejected(self) -> None:
        result = validate_rows(
            [_option_row(key="FUT|NIFTY", asset="FUTURES", strike="", expiry="")]
        )
        assert CODE_IDENTITY_FUTURES in result.codes

    def test_duplicate_timestamp_rejected(self) -> None:
        result = validate_rows([_option_row(), _option_row()])
        assert CODE_TS_DUPLICATE in result.codes

    def test_out_of_order_timestamp_rejected(self) -> None:
        result = validate_rows([_option_row("2024-01-15T09:20:00"), _option_row("2024-01-15T09:15:00")])
        assert CODE_TS_ORDER in result.codes

    def test_non_five_minute_alignment_rejected(self) -> None:
        result = validate_rows([_option_row("2024-01-15T09:17:00")])
        assert CODE_TS_ALIGN in result.codes

    def test_aware_utc_timestamp_normalized_to_ist(self) -> None:
        result = validate_rows(
            [_option_row("2024-01-15T04:15:00+00:00"), _option_row("2024-01-15T04:20:00+00:00")]
        )
        assert result.ok
        assert result.profiles["date_span"] == [date(2024, 1, 15), date(2024, 1, 15)]

    def test_non_utc_aware_timestamp_rejected(self) -> None:
        result = validate_rows([_option_row("2024-01-15T09:15:00+05:30")])
        assert CODE_TS_TZ in result.codes

    def test_unparseable_timestamp_rejected(self) -> None:
        result = validate_rows([_option_row("not-a-date")])
        assert CODE_TS_PARSE in result.codes

    def test_missing_intrasession_coverage_is_gap(self) -> None:
        result = validate_rows([_option_row("2024-01-15T09:15:00"), _option_row("2024-01-15T09:40:00")])
        assert CODE_COVERAGE_GAP in result.codes

    def test_three_missing_bars_ok(self) -> None:
        result = validate_rows([_option_row("2024-01-15T09:15:00"), _option_row("2024-01-15T09:35:00")])
        assert result.ok

    def test_overnight_boundary_not_reported_as_gap(self) -> None:
        result = validate_rows([_option_row("2024-01-15T15:25:00"), _option_row("2024-01-16T09:15:00")])
        assert result.ok

    def test_silent_skipped_session_day_is_gap(self) -> None:
        result = validate_rows([_option_row("2024-01-15T15:25:00"), _option_row("2024-01-17T09:15:00")])
        assert CODE_COVERAGE_GAP in result.codes

    def test_no_data_sentinel_suppresses_gap(self) -> None:
        rows = [
            _option_row("2024-01-15T09:15:00"),
            _option_row("2024-01-15T09:25:00", open="", high="", low="", close="", volume="", oi=""),
            _option_row("2024-01-15T09:40:00"),
        ]
        result = validate_rows(rows)
        assert CODE_COVERAGE_GAP not in result.codes


# ---------------------------------------------------------------- dataset directory

def _write_dataset(tmp_path: Path, rows, *, source="SYNTHETIC_FIXTURE", synthetic=True) -> Path:
    directory = tmp_path / "options_data"
    directory.mkdir(exist_ok=True)
    path = directory / "chain.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(OPTION_BAR_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    digest = file_sha256_lf(path)
    manifest = {
        "schema_version": "options_chain_bar_v1",
        "source": source,
        "synthetic": synthetic,
        "coverage": "2024-01-15",
        "files": {"chain.csv": {"sha256": digest}},
    }
    (directory / "options_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return directory


class TestDatasetDirectory:
    def test_valid_directory_passes(self, tmp_path: Path) -> None:
        directory = _write_dataset(
            tmp_path, [_underlying_row(), _underlying_row("2024-01-15T09:20:00")]
        )
        result = validate_dataset_directory(directory)
        assert result.ok
        assert result.status() == STATUS_VALID
        assert result.profiles["synthetic"] is True

    def test_manifest_hash_mismatch_quarantines(self, tmp_path: Path) -> None:
        directory = _write_dataset(tmp_path, [_underlying_row()])
        manifest_path = directory / "options_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["chain.csv"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = validate_dataset_directory(directory)
        assert not result.ok
        assert result.status() == STATUS_QUARANTINED
        assert CODE_MANIFEST_HASH in result.codes

    def test_missing_manifest_quarantines(self, tmp_path: Path) -> None:
        directory = _write_dataset(tmp_path, [_underlying_row()])
        (directory / "options_manifest.json").unlink()
        result = validate_dataset_directory(directory)
        assert CODE_MANIFEST_MISSING in result.codes
        assert result.status() == STATUS_QUARANTINED

    def test_missing_directory(self, tmp_path: Path) -> None:
        result = validate_dataset_directory(tmp_path / "does-not-exist")
        assert result.codes == ("DATASET_MISSING",)

    def test_synthetic_manifest_must_declare_itself(self) -> None:
        assert manifest_synthetic_ok({"synthetic": True, "source": "SYNTHETIC_FIXTURE"})
        assert not manifest_synthetic_ok({"synthetic": True, "source": "vendor-live"})
        assert manifest_synthetic_ok({"synthetic": False, "source": "vendor-live"})

    def test_sha256_lf_stable_across_newlines(self, tmp_path: Path) -> None:
        content = "ts,instrument_key\n2024-01-15T09:15:00,OPT|X\n"
        lf_path = tmp_path / "lf.csv"
        crlf_path = tmp_path / "crlf.csv"
        lf_path.write_text(content, encoding="utf-8", newline="")
        crlf_path.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
        assert file_sha256_lf(lf_path) == file_sha256_lf(crlf_path)


# ---------------------------------------------------------------- protocol

class TestResearchProtocol:
    def test_default_protocol_requires_strategy(self) -> None:
        protocol = default_protocol()
        assert protocol.strategy_specification_status == STRATEGY_SPECIFICATION_REQUIRED
        assert protocol.candidate_strategy is None
        assert protocol.protected_oos_period is None

    def test_fingerprint_stable_and_mutation_sensitive(self) -> None:
        protocol = default_protocol()
        assert freeze(protocol) == protocol_fingerprint(protocol)
        mutated = replace(protocol, hypothesis="changed")
        assert protocol_fingerprint(mutated) != protocol_fingerprint(protocol)

    def test_fingerprint_sorted_sequence_stable(self) -> None:
        a = replace(default_protocol(), evaluation_metrics=("win_rate", "net_pnl"))
        b = default_protocol()
        assert protocol_fingerprint(a) != protocol_fingerprint(b)


# ---------------------------------------------------------------- windows / OOS

class TestOosWindows:
    def test_valid_split_passes(self) -> None:
        plan = SplitPlan(
            development_start=date(2022, 1, 3),
            development_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2025, 10, 3),
        )
        assessment = validate_split(plan)
        assert assessment.ok

    def test_split_order_violation(self) -> None:
        assessment = validate_split(
            SplitPlan(
                development_start=date(2022, 1, 3),
                development_end=date(2024, 6, 1),
                validation_start=date(2023, 1, 2),
                validation_end=date(2025, 1, 1),
            )
        )
        assert not assessment.ok
        assert "SPLIT_ORDER" in assessment.codes

    def test_split_touching_protected_refused(self) -> None:
        assessment = validate_split(
            SplitPlan(
                development_start=date(2022, 1, 3),
                development_end=date(2023, 12, 29),
                validation_start=date(2025, 9, 1),
                validation_end=date(2026, 1, 1),
            )
        )
        assert "SPLIT_PROTECTED" in assessment.codes

    def test_classify_day_windows(self) -> None:
        assert classify_day(date(2025, 10, 3)) == WINDOW_RESEARCH
        assert classify_day(PROTECTED_OOS_START) == WINDOW_PROTECTED
        assert classify_day(PROTECTED_OOS_END) == WINDOW_PROTECTED
        assert classify_day(date(2026, 9, 12)) == WINDOW_FRESH

    def test_collect_and_verify_protected_days(self) -> None:
        days = (date(2025, 9, 1), date(2025, 10, 20), date(2026, 9, 12))
        assert collect_protected_days(days) == (date(2025, 10, 20),)
        assert verify_no_protected_reuse(days) == (date(2025, 10, 20),)

    def test_protected_window_reuse_refused(self) -> None:
        registry = ConsumedWindowRegistry()
        with pytest.raises(ProtectedOosRefusal):
            registry.assert_unconsumed_fresh(date(2025, 11, 5))

    def test_consumed_fresh_day_refused(self) -> None:
        registry = ConsumedWindowRegistry(consumed={"day_2026-09-24": {"fingerprint": "x"}})
        with pytest.raises(ProtectedOosRefusal):
            registry.assert_unconsumed_fresh(date(2026, 9, 24))

    def test_unconsumed_fresh_day_ok(self) -> None:
        ConsumedWindowRegistry().assert_unconsumed_fresh(date(2026, 9, 24))

    def test_no_lookahead_ok_and_violation(self) -> None:
        decision = datetime(2024, 1, 15, 9, 30, 0)
        ok = check_no_lookahead(decision, {"snapshot": datetime(2024, 1, 15, 9, 30, 0)})
        assert ok.ok
        bad = check_no_lookahead(decision, {"snapshot": datetime(2024, 1, 15, 9, 45, 0)})
        assert not bad.ok
        (label, ts), = bad.violations
        assert label == "snapshot" and ts > decision


# ---------------------------------------------------------------- metrics

class TestMetrics:
    def _one(self, *, pnl: str, day: str = "2026-09-23", **kw) -> dict[str, object]:
        gross = pnl
        net = kw.pop("net", pnl)
        row: dict[str, object] = {
            "record_id": f"r-{day}-{pnl}",
            "contract_key": "OPT|NIFTY CE",
            "quantity": 1,
            "entry_fill": "100.00",
            "exit_fill": "101.00",
            "gross_realized_pnl": gross,
            "net_realized_pnl": net,
            "close_day": day,
        }
        row.update(kw)
        return row

    def test_empty_sample_metrics_undefined(self) -> None:
        metrics = compute_metrics(LedgerSample())
        assert metrics.executed == 0
        assert metrics.net_realized_pnl == DEC("0")
        assert metrics.win_rate_pct is None
        assert metrics.profit_factor is None
        assert metrics.max_drawdown is None
        assert "win_rate_pct" in metrics.reasons
        assert "profit_factor" in metrics.reasons
        assert "rejection_rate_pct" in metrics.reasons

    def test_win_rate_denominator(self) -> None:
        sample = LedgerSample(
            rows=(
                LedgerRow.from_dict(self._one(pnl="10.00")),
                LedgerRow.from_dict(self._one(pnl="20.00", day="2026-09-24")),
                LedgerRow.from_dict(self._one(pnl="-5.00", day="2026-09-24", net="-5.00")),
            )
        )
        metrics = compute_metrics(sample)
        assert metrics.executed == 3
        assert metrics.wins == 2 and metrics.losses == 1 and metrics.flat == 0
        assert metrics.win_rate_pct == DEC("66.66666666666666666666666667")

    def test_profit_factor_undefined_without_losses(self) -> None:
        sample = LedgerSample(rows=(LedgerRow.from_dict(self._one(pnl="10.00")),))
        metrics = compute_metrics(sample)
        assert metrics.profit_factor is None
        assert "profit_factor" in metrics.reasons
        assert metrics.avg_loss is None

    def test_profit_factor_computed(self) -> None:
        sample = LedgerSample(
            rows=(
                LedgerRow.from_dict(self._one(pnl="150.00")),
                LedgerRow.from_dict(self._one(pnl="-50.00", net="-50.00", day="2026-09-24")),
            )
        )
        metrics = compute_metrics(sample)
        assert metrics.gross_profit == DEC("150")
        assert metrics.gross_loss == DEC("-50")
        assert metrics.profit_factor == DEC("3")

    def test_max_drawdown_on_cumulative_daily_net(self) -> None:
        sample = LedgerSample(
            rows=(
                LedgerRow.from_dict(self._one(pnl="330.00", day="2026-09-23", net="330.00")),
                LedgerRow.from_dict(self._one(pnl="-465.00", day="2026-09-24", net="-465.00")),
            )
        )
        metrics = compute_metrics(sample)
        assert metrics.max_drawdown == DEC("465")

    def test_commissions_and_slippage(self) -> None:
        sample = LedgerSample(
            rows=(
                LedgerRow.from_dict(
                    self._one(pnl="10.00", entry_commission="1.25", exit_commission="1.25")
                ),
                LedgerRow.from_dict(
                    self._one(pnl="-5.00", net="-5.00", day="2026-09-24", slippage="-0.40")
                ),
            )
        )
        metrics = compute_metrics(sample)
        assert metrics.commissions == DEC("2.50")
        assert metrics.total_slippage == DEC("-0.40")

    def test_capital_utilization(self) -> None:
        sample = LedgerSample(
            rows=(
                LedgerRow.from_dict(
                    self._one(pnl="10.00", premium_exposure="7500.00")
                ),
                LedgerRow.from_dict(
                    self._one(pnl="-5.00", net="-5.00", day="2026-09-24", premium_exposure="15000.00")
                ),
            ),
            capital=DEC("100000"),
        )
        metrics = compute_metrics(sample)
        assert metrics.total_premium_exposure == DEC("22500")
        assert metrics.capital_utilization_pct == DEC("22.5")

    def test_rejection_rate_denominator(self) -> None:
        sample = LedgerSample(opportunities=10, rejected_by_reason={"quality": 3})
        metrics = compute_metrics(sample)
        assert metrics.rejected_total == 3
        assert metrics.rejection_rate_pct == DEC("30")

    def test_distributions(self) -> None:
        rows = (
            LedgerRow.from_dict(
                self._one(pnl="10.00", regime="RANGE", expiry="2026-10-01", strike="24600", holding_duration_seconds=7200)
            ),
            LedgerRow.from_dict(
                self._one(pnl="-5.00", net="-5.00", day="2026-09-24", regime="TREND", holding_duration_seconds=5400)
            ),
        )
        metrics = compute_metrics(LedgerSample(rows=rows))
        assert metrics.regime_distribution == {"RANGE": 1, "TREND": 1}
        assert metrics.expiry_buckets == {"2026-10-01": 1}
        assert metrics.expiry_unknown == 1
        assert metrics.avg_holding_seconds == 6300
        assert metrics.profitable_days == 1 and metrics.losing_days == 1 and metrics.trading_days == 2

    def test_ledger_row_requires_identity_and_pnl(self) -> None:
        with pytest.raises(ValueError):
            LedgerRow.from_dict({})
        with pytest.raises(ValueError):
            LedgerRow.from_dict({"record_id": "r1", "close_day": "2026-09-23"})
        with pytest.raises(ValueError):
            LedgerRow.from_dict(
                self._one(pnl=None)  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------- Phase 11 paper integration

def _entry_fill(price: str, contract: str, filled_at: datetime) -> EntryFill:
    p = DEC(price)
    return EntryFill(
        order_id=f"po-{contract}-in",
        reference_used="bid",
        reference_price=p,
        observed_bid=p,
        observed_ask=None,
        observed_last=None,
        fill_price=p,
        slippage_rate=DEC("0"),
        commission=p / DEC("100") * DEC("0.03"),  # 3 bps synthetic fee
        filled_at=filled_at,
        premium_value=p * 75,
        premium_exposure=p * 75,
        candidate_premium_exposure=None,
        max_premium_exposure=None,
    )


def _exit_fill(price: str, contract: str, filled_at: datetime) -> ExitFill:
    p = DEC(price)
    return ExitFill(
        order_id=f"px-{contract}-out",
        reference_used="ask",
        reference_price=p,
        observed_bid=None,
        observed_ask=p,
        observed_last=None,
        fill_price=p,
        slippage_rate=DEC("0"),
        commission=p / DEC("100") * DEC("0.03"),
        filled_at=filled_at,
        exit_reason="EXPLICIT",
    )


class TestPhase11LedgerIntegration:
    def test_daily_report_ledger_drives_metrics(self) -> None:
        day_a = date(2026, 9, 23)
        day_b = date(2026, 9, 24)
        rec_a = LifecycleRecord(
            event_id="evt-a",
            record_id="rec-a",
            decision_timestamp=datetime(2026, 9, 23, 9, 30, 0),
            instrument_key="OPT|NIFTY 01OCT2026 24600 CE",
            contract_key="OPT|NIFTY 01OCT2026 24600 CE",
            underlying_symbol="NIFTY",
            option_side="CE",
            strike=DEC("24600"),
            expiry="2026-10-01",
            quantity=1,
            lot_size=75,
            multiplier=1,
            direction="LONG",
            fingerprint="f-a",
            phase=LifecyclePhase.RECONCILED,
            history=(
                Step(LifecyclePhase.CANDIDATE, datetime(2026, 9, 23, 9, 30, 0)),
                Step(LifecyclePhase.POSITION_CLOSED, datetime(2026, 9, 23, 14, 0, 0)),
                Step(LifecyclePhase.RECONCILED, datetime(2026, 9, 23, 14, 1, 0)),
            ),
            entry=_entry_fill("100.00", "a", datetime(2026, 9, 23, 9, 31, 0)),
            exit=_exit_fill("105.00", "a", datetime(2026, 9, 23, 14, 0, 0)),
            financials=Financials(
                entry_commission=DEC("2.25"),
                exit_commission=DEC("2.3625"),
                gross_realized_pnl=DEC("375.00"),
                net_realized_pnl=DEC("370.3875"),
                holding_duration_seconds=16140,
                close_day=day_a.isoformat(),
            ),
            reconciliation=Reconciliation(datetime(2026, 9, 23, 14, 1, 0), ok=True, violations=()),
        )
        rec_b = LifecycleRecord(
            event_id="evt-b",
            record_id="rec-b",
            decision_timestamp=datetime(2026, 9, 24, 9, 30, 0),
            instrument_key="OPT|NIFTY 01OCT2026 24700 CE",
            contract_key="OPT|NIFTY 01OCT2026 24700 CE",
            underlying_symbol="NIFTY",
            option_side="CE",
            strike=DEC("24700"),
            expiry="2026-10-01",
            quantity=1,
            lot_size=75,
            multiplier=1,
            direction="LONG",
            fingerprint="f-b",
            phase=LifecyclePhase.RECONCILED,
            history=(
                Step(LifecyclePhase.CANDIDATE, datetime(2026, 9, 24, 9, 30, 0)),
                Step(LifecyclePhase.POSITION_CLOSED, datetime(2026, 9, 24, 14, 0, 0)),
                Step(LifecyclePhase.RECONCILED, datetime(2026, 9, 24, 14, 1, 0)),
            ),
            entry=_entry_fill("200.00", "b", datetime(2026, 9, 24, 9, 31, 0)),
            exit=_exit_fill("195.00", "b", datetime(2026, 9, 24, 14, 0, 0)),
            financials=Financials(
                entry_commission=DEC("4.50"),
                exit_commission=DEC("4.3875"),
                gross_realized_pnl=DEC("-375.00"),
                net_realized_pnl=DEC("-383.8875"),
                holding_duration_seconds=16140,
                close_day=day_b.isoformat(),
            ),
            reconciliation=Reconciliation(datetime(2026, 9, 24, 14, 1, 0), ok=True, violations=()),
        )

        report_a = build_daily_report((rec_a,), day_a)
        report_b = build_daily_report((rec_b,), day_b)
        assert report_a["aggregate"]["paper_only"] is True
        assert report_b["aggregate"]["paper_only"] is True
        assert len(report_a["aggregate"]["ledger"]) == 1
        assert len(report_b["aggregate"]["ledger"]) == 1

        # Merge the two daily ledgers into one sample (rows still originate from
        # the real Phase 11 build_daily_report ledger emission).
        merged = dict(report_b["aggregate"])
        merged["ledger"] = list(report_b["aggregate"]["ledger"]) + list(report_a["aggregate"]["ledger"])
        merged["counts"] = {"closed": 2, "rejected": 0, "open": 0, "reconciled": 2}
        for row in merged["ledger"]:
            row["premium_exposure"] = str(DEC(row["entry_fill"]) * 75)

        sample = sample_from_daily_report(
            {"aggregate": merged, "fingerprint": merged.get("fingerprint", "")},
            capital=DEC("100000"),
            data_label="SYNTHETIC_FIXTURE",
        )
        metrics = compute_metrics(sample)
        assert metrics.data_label == "SYNTHETIC_FIXTURE"
        assert metrics.fill_model_version == FILL_MODEL_VERSION
        assert metrics.executed == 2
        assert metrics.wins == 1 and metrics.losses == 1
        assert metrics.net_realized_pnl == DEC("370.3875") - DEC("383.8875")
        assert metrics.profit_factor == DEC("1")
        assert metrics.max_drawdown == DEC("383.8875")
        assert metrics.capital_utilization_pct == DEC("22.5")


# ---------------------------------------------------------------- safety

class TestSafety:
    def test_import_safety_no_execution_upstox_credentials(self) -> None:
        package = ROOT / "src/fno_ai_paper_trading/options_research"
        assert package.is_dir()
        for module in package.glob("*.py"):
            source = module.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            roots = {name.split(".")[0] for name in imported}
            for banned in ("execution", "upstox", "credentials"):
                assert banned not in roots, f"{module.name} imports forbidden root {banned}"
            for banned_module in (
                "fno_ai_paper_trading.execution",
                "fno_ai_paper_trading.broker",
                "fno_ai_paper_trading.credentials",
            ):
                assert not any(name.startswith(banned_module) for name in imported), (
                    f"{module.name} imports forbidden module {banned_module}"
                )

    def test_no_wall_clock_reads_in_package(self) -> None:
        package = ROOT / "src/fno_ai_paper_trading/options_research"
        for module in package.glob("*.py"):
            source = module.read_text(encoding="utf-8")
            assert "datetime.now" not in source, f"{module.name} reads the clock"
            assert "datetime.utcnow" not in source and "utcnow" not in source
            assert "import time" not in source and "from time import" not in source

    def test_fixture_evidence_never_observed(self) -> None:
        assert classify_source(SYNTHETIC) == SOURCE_SYNTHETIC
        READY = assess_readiness(
            sources=(ADEQUATE,),
            dataset=DatasetFinding(present=True, validated=True, source_id=ADEQUATE.source_id, num_rows=10),
        )
        assert READY.verdict == HISTORICAL_OPTIONS_DATA_READY
        fixtures = assess_readiness(sources=(SYNTHETIC,))
        assert fixtures.verdict != HISTORICAL_OPTIONS_DATA_READY