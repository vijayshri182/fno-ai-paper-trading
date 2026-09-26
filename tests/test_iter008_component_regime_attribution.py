"""Tests for ITERATION 008 - component + regime attribution (n3_ensemble).

Pure-logic + artifact-integrity tests only.  No engine re-run for tuning;
the attribution artifact is produced once by the module itself and these
tests verify its provenance, guardrails, and the frozen-loop equivalence of
the component-toggle variant against the authoritative ensemble_signals.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fno_ai_paper_trading.models.enums import InstrumentType
from fno_ai_paper_trading.models.instruments import Instrument
from fno_ai_paper_trading.models.market import MarketPrice
from fno_ai_paper_trading.research.iteration005_discovery import EnsembleParams, ensemble_signals
from fno_ai_paper_trading.research.iteration008_component_regime_attribution import (
    ABLATION_DEFS,
    ABLATION_ORDER,
    FINGERPRINT_SHA256,
    ITER6_RESULT_SHA256,
    OUT_FILE,
    _trend_variant_signs,
    build_entry_context,
    condition_pivot,
    enrich_trades,
    ensemble_variant,
)

REPO = Path(__file__).resolve().parents[1]


def _future() -> Instrument:
    return Instrument(
        symbol="NIFTY1",
        instrument_type=InstrumentType.FUTURE,
        underlying_symbol="NIFTY",
        lot_size=1,
        multiplier=1,
    )


def _bar(iso: str, o, h, l, c) -> MarketPrice:
    return MarketPrice(
        instrument=_future(),
        timestamp=datetime.fromisoformat(iso),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=1,
        open_interest=0,
    )


def synth_bars(sessions: int = 120, bars_per_day: int = 76) -> list[MarketPrice]:
    """Deterministic synthetic prices: trend legs + vol bursts + chop."""
    from datetime import timedelta

    out: list[MarketPrice] = []
    price = 18000.0
    d0 = date(2024, 1, 2)
    cur = d0
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    for s in range(sessions):
        phase = s % 36
        drift = 0.0
        vol = 34.0
        if phase < 10:
            drift = 22.0
        elif phase < 16:
            drift = 0.0
        elif phase < 26:
            drift = -18.0
        else:
            drift = 0.0
        if s in (40, 41, 42, 101, 102):
            vol = 90.0
        open_ = price
        for b in range(bars_per_day):
            close = open_ + drift / bars_per_day
            hi = max(open_, close) + vol * 0.4
            lo = min(open_, close) - vol * 0.4
            out.append(_bar(f"{cur.isoformat()}T{9 + b // 60:02d}:{b % 60:02d}:00",
                            open_, hi, lo, close))
            open_ = close
        price = open_
        cur += timedelta(days=1)
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
    return out


def _actionable_profile(signals):
    return [(s.actionable, s.signal.value if s else None) for s in signals]


# ---------------------------------------------------------------------------
# component-toggle variant
# ---------------------------------------------------------------------------


def test_variant_all_on_is_byte_identical_to_authoritative():
    bars = synth_bars()
    params = EnsembleParams()
    base = ensemble_signals(bars, params)
    variant = ensemble_variant(bars, params, use_trend=True, use_vol=True,
                               use_level=True, use_slope=True)
    assert _actionable_profile(variant) == _actionable_profile(base)
    assert len(variant) == len(bars)


def test_variant_vol_off_relaxes_the_gate():
    bars = synth_bars()
    params = EnsembleParams()
    base = ensemble_signals(bars, params)
    vol_off = ensemble_variant(bars, params, use_trend=True, use_vol=False)
    base_entries = sum(1 for s in base if s is not None and s.actionable)
    vol_entries = sum(1 for s in vol_off if s is not None and s.actionable)
    assert vol_entries > 0
    assert vol_entries >= base_entries


def test_variant_trend_off_keeps_scaling():
    bars = synth_bars()
    params = EnsembleParams()
    trend_off = ensemble_variant(bars, params, use_trend=False, use_vol=True)
    entries = sum(1 for s in trend_off if s is not None and s.actionable)
    assert entries > 0


def test_trend_variant_signs_core_logic():
    assert _trend_variant_signs(5.0, 4.0, 4.8, True, True) == 1
    assert _trend_variant_signs(3.0, 4.0, 3.2, True, True) == -1
    assert _trend_variant_signs(5.0, 4.0, 5.2, True, True) == 0
    assert _trend_variant_signs(5.0, 4.0, None, True, False) == 1
    assert _trend_variant_signs(3.0, 4.0, None, True, False) == -1
    assert _trend_variant_signs(5.0, 5.0, None, True, False) == 0
    assert _trend_variant_signs(5.0, 4.0, 5.2, False, True) == -1
    assert _trend_variant_signs(5.0, 4.0, 4.6, False, True) == 1
    assert _trend_variant_signs(None, 4.0, 5.0, True, True) == 0
    assert _trend_variant_signs(5.0, 4.0, None, False, True) == 0


def test_registry_pre_registered_and_structure_only():
    assert list(ABLATION_DEFS) == list(ABLATION_ORDER)
    base = ABLATION_DEFS["base"]
    assert all(base[k] for k in ("use_trend", "use_vol", "use_level", "use_slope"))
    for name, d in ABLATION_DEFS.items():
        if name == "base":
            continue
        toggled = [k for k in ("use_trend", "use_vol", "use_level", "use_slope") if not d[k]]
        assert len(toggled) == 1, f"{name} must toggle exactly one component: {toggled}"
        for k in ("use_trend", "use_vol", "use_level", "use_slope"):
            assert base[k]
    assert ABLATION_DEFS["vol_off"]["use_vol"] is False
    assert ABLATION_DEFS["trend_off"]["use_trend"] is False
    assert ABLATION_DEFS["slope_off"]["use_slope"] is False


# ---------------------------------------------------------------------------
# attribution helpers
# ---------------------------------------------------------------------------


def test_enrich_trades_maps_journal_offset_to_full_series():
    bars = synth_bars(sessions=12)
    params = EnsembleParams()
    ctx = build_entry_context(bars, params)
    oos_start = 0
    fake_rts = [{
        "entry": {"entry_index": 300}, "exit_day": "2024-01-09",
        "entry_day": "2024-01-08", "side": "LONG", "net": Decimal("10"),
        "gross_close": Decimal("12"), "exit_reason": "x",
    }]
    enriched = enrich_trades(fake_rts, ctx, oos_start, [])
    assert enriched[0]["trend_sign"] == ctx[300]["trend"]
    assert enriched[0]["vol_sign"] == ctx[300]["volmove"]


def test_condition_pivot_aggregates():
    rows = [
        {"k": "a", "net": 10.0, "gross_close": 12.0},
        {"k": "a", "net": -3.0, "gross_close": 5.0},
        {"k": "b", "net": 7.0, "gross_close": 9.0},
    ]
    piv = condition_pivot(rows, "k")
    a = next(r for r in piv if r["k"] == "a")
    b = next(r for r in piv if r["k"] == "b")
    assert a["count"] == 2 and a["wins"] == 1 and a["losses"] == 1
    assert a["net"] == 7.0 and a["gross"] == 17.0
    assert b["count"] == 1 and b["wins"] == 1 and b["net"] == 7.0


# ---------------------------------------------------------------------------
# artifact provenance + guardrails
# ---------------------------------------------------------------------------


def test_artifact_exists_and_provenance_hashes():
    assert OUT_FILE.exists()
    data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    assert data["experiment"] == "ITERATION_008_COMPONENT_REGIME_ATTRIBUTION_n3_ensemble"
    it6 = REPO / "runs" / "research" / "day_batch" / "iteration_006_protected_oos_n3.json"
    fp = REPO / "runs" / "research" / "day_batch" / "iteration_006_n3_fingerprint.json"
    assert hashlib.sha256(it6.read_bytes()).hexdigest() == ITER6_RESULT_SHA256
    assert hashlib.sha256(fp.read_bytes()).hexdigest() == FINGERPRINT_SHA256


def test_attribution_guardrails_in_artifact():
    data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    assert data["attribution_only"] is True
    method = data["method"]
    assert method["diagnostics_only"] is True
    assert method["ablations_are_not_candidates"] is True
    assert method["no_parameter_changed"] is True
    assert method["base_consistency_guard"] is True
    assert method["base_sha256"] == ITER6_RESULT_SHA256
    assert data["candidate_freeze"]["parameters"]["fast"] == 9
    assert data["candidate_freeze"]["parameters"]["max_hold_days"] == 25
    assert "ATTRIBUTION" in data["promotion_notice"].upper()
    assert "NO" in data["promotion_notice"].upper()


def test_ablation_rows_engine_authoritative():
    data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    assert [r["name"] for r in data["ablation_rows"]] == list(ABLATION_ORDER)
    base = data["ablation_rows"][0]["engine_authoritative"]
    assert base["basis"] == "engine_authoritative"
    assert base["round_trips"] == 31
    assert base["net"] == "1710.004256155"
    assert base["reconciled_journal"] is True
    for name in ("vol_off", "trend_off", "slope_off"):
        row = next(r for r in data["ablation_rows"] if r["name"] == name)
        e = row["engine_authoritative"]
        assert e["round_trips"] > 0
        assert e["net"] not in ("", "0")


def test_component_edge_attribution_question_answered():
    data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    src = data["primary_edge_source"]
    assert src in ("VOL_GATE", "TREND_GATE", "JOINT_TREND_AND_VOL_GATES", "JOINT_INTERACTION_ONLY")
    assert "vol_gate_value_net" in data["component_marginal_attribution"]
    assert "trend_gate_value_net" in data["component_marginal_attribution"]
    assert "slope_gate_value_net" in data["component_marginal_attribution"]
    sc = data["sign_consistency"]
    assert sc["trend_sign_matches_side"] == 31
    assert sc["vol_sign_matches_side"] == 31
    assert sc["level_sign_matches_side"] == 31
    assert sc["slope_sign_matches_side"] == 31
    assert len(data["trades_enriched"]) == 31


def test_losses_attribution_answer_present():
    data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    losses = data["losses"]
    assert losses["count"] == 11
    assert losses["net"] == round(-1082.328616, 6)
    by_regime = {r["entry_regime"]: r["count"] for r in losses["by_regime"]}
    assert by_regime.get("sideways", -1) == 5
    assert by_regime.get("mixed", -1) == 6
    assert len(losses["rows"]) == 11
    pivots = data["pivots"]
    assert any(r["entry_regime"] == "sideways" and r["net"] < 0 for r in pivots["by_entry_regime"])
    ans = data["attribution_answer"]
    assert isinstance(ans["edge_conditions"], list)
    assert isinstance(ans["loss_conditions"]["exit_reasons_net_negative"], list)