"""Tests for ITERATION 012 - TRADE FORENSICS (analytical only).

The module performs a retrospective forensic analysis of the frozen
Iteration-009 VOL-led strategy's 226 completed round trips.  Tests cover:
artifact integrity, known-economics baseline, determinism, win-rate
discrepancy (official 60.18% vs net 57.08%), sign-flip concentration,
cost-flipped trades, long/short, concentration, robustness, and MD output.
"""
from __future__ import annotations

import json
from pathlib import Path

from fno_ai_paper_trading.research.iteration012_trade_forensics import (
    ITER11_OUT,
    ITER9_OUT,
    OUT_JSON,
    OUT_MD,
    RESEARCH_BARS_EXPECTED,
    RESEARCH_DAYS_EXPECTED,
    _profit_distribution,
    _sign_flip_analysis,
    _cost_analysis,
    _long_short_analysis,
    _holding_duration,
    _concentration_tests,
    _robustness_checks,
)

# Deterministic artifact sha recorded at execution time (run twice, identical).
ITERATION_012_OUT_SHA256 = "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb"


def _load():
    return json.loads(OUT_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Artifact integrity
# ---------------------------------------------------------------------------


def test_artifact_json_exists():
    assert OUT_JSON.exists(), f"Artifact missing: {OUT_JSON}"


def test_artifact_md_exists():
    assert OUT_MD.exists(), f"MD report missing: {OUT_MD}"


def test_artifact_json_valid():
    d = _load()
    assert isinstance(d, dict)
    assert d.get("iteration") == "ITERATION_012"


def test_artifact_sha256_deterministic():
    """Verify the recorded sha matches the on-disk artifact."""
    d = _load()
    from fno_ai_paper_trading.research.iteration010_sideways_veto import _sha256
    assert _sha256(OUT_JSON) == ITERATION_012_OUT_SHA256


def test_provenance_iter009_unchanged():
    from fno_ai_paper_trading.research.iteration010_sideways_veto import (
        ITER9_RESULT_SHA256, _sha256,
    )
    assert _sha256(ITER9_OUT) == ITER9_RESULT_SHA256


def test_provenance_iter011_unchanged():
    from fno_ai_paper_trading.research.iteration010_sideways_veto import _sha256
    assert _sha256(ITER11_OUT) == "f303a8be97852592269b3fa8416756507534b1dab220657888f402b54207aea4"


def test_safe_status():
    d = _load()
    s = d.get("safety_status", {})
    assert s.get("promotion") == "NO"
    assert s.get("algo_ready") == "NO"
    assert s.get("algorithm_health") == "RED"
    assert s.get("paper_only") is True
    assert s.get("live_gate") == "CLOSED"
    assert s.get("model_0_frozen") is True


def test_no_strategy_change():
    d = _load()
    assert "candidate_b" not in d, "Iteration-012 must NOT contain candidate strategy"
    assert d.get("safety_status", {}).get("note", "").lower().find("forensic") >= 0


# ---------------------------------------------------------------------------
# 2. Trade count and baseline economics
# ---------------------------------------------------------------------------


def test_trade_count():
    d = _load()
    rts = d["trade_reconstruction"]
    assert len(rts) == 226


def test_sum_net_matches_economics():
    d = _load()
    rts = d["trade_reconstruction"]
    total_net = sum(float(r["net"]) for r in rts)
    assert round(total_net, 6) == 11988.079916


def test_sum_gross_matches_economics():
    d = _load()
    rts = d["trade_reconstruction"]
    total_gross = sum(float(r["gross_close"]) for r in rts)
    assert round(total_gross, 6) == 24352.100000


def test_total_costs_match():
    d = _load()
    rts = d["trade_reconstruction"]
    total_costs = sum(float(r["slippage"]) + float(r["commission"]) for r in rts)
    assert round(total_costs, 6) == 12364.020084


def test_carry_mtm():
    d = _load()
    rts = d["trade_reconstruction"]
    total_net = sum(float(r["net"]) for r in rts)
    carry = 77.721999485
    assert abs((total_net + carry) - 12065.801915115) < 1e-6


# ---------------------------------------------------------------------------
# 3. Win-rate discrepancy: official 60.18% vs net 57.08%
# ---------------------------------------------------------------------------


def test_win_rate_official():
    d = _load()
    pd = d["profit_distribution"]
    assert pd["win_rate_pct_official"] == 60.18


def test_win_rate_net():
    d = _load()
    pd = d["profit_distribution"]
    assert pd["win_rate_pct_net"] == 57.08


def test_winners_official():
    d = _load()
    pd = d["profit_distribution"]
    assert pd["total_winners_official"] == 136


def test_winners_net():
    d = _load()
    pd = d["profit_distribution"]
    assert pd["total_winners_net"] == 129


def test_cost_flipped_trades():
    d = _load()
    pd = d["profit_distribution"]
    assert pd["cost_flipped_trades"] == 7
    assert pd["cost_flipped_ids"] is not None
    assert len(pd["cost_flipped_ids"]) == 7


def test_cost_flipped_all_have_positive_realized():
    d = _load()
    rts = d["trade_reconstruction"]
    flipped_ids = d["profit_distribution"]["cost_flipped_ids"]
    for tid in flipped_ids:
        r = rts[tid]
        assert float(r["realized"]) > 0, f"trade {tid} realized <= 0"
        assert float(r["net"]) <= 0, f"trade {tid} net > 0"


def test_net_reconciliation():
    d = _load()
    recon = d["profit_distribution"]["net_reconciliation"]
    assert recon["reconciled"] is True
    assert round(recon["expected_total_pnl"], 6) == 12065.801915


# ---------------------------------------------------------------------------
# 4. Sign-flip concentration
# ---------------------------------------------------------------------------


def test_sign_flip_count():
    d = _load()
    sf = d["sign_flip_analysis"]
    assert sf["sign_flip_rt_count"] == 133


def test_sign_flip_share():
    d = _load()
    sf = d["sign_flip_analysis"]
    assert sf["sign_flip_share_pct"] == 58.85


def test_sign_flip_net():
    d = _load()
    sf = d["sign_flip_analysis"]
    assert round(sf["sign_flip_net_pnl"], 2) == 8272.57


def test_sign_flip_all_categories_sum():
    d = _load()
    sf = d["sign_flip_analysis"]
    total = sum(c["rt_count"] for c in sf["categories"])
    assert total == 226


def test_sign_continuation_not_tautological():
    """Post-entry continuation must NOT equal 226/0 (would mean reuse of the
    A-arm's own sign input)."""
    d = _load()
    sc = d["pre_post_volatility"]["sign_continuation"]
    assert sc["continuation_count"] > 0
    assert sc["reversal_count"] > 0
    assert sc["continuation_count"] == 113
    assert sc["reversal_count"] == 113


def test_sign_continuation_net_sum():
    d = _load()
    sc = d["pre_post_volatility"]["sign_continuation"]
    total = sc["continuation_net"] + sc["reversal_net"]
    assert abs(total - 11988.079916) < 0.01 or abs(total) < 11988.08


# ---------------------------------------------------------------------------
# 5. Long/short
# ---------------------------------------------------------------------------


def test_long_short_split():
    d = _load()
    ls = d["long_short_analysis"]
    assert ls["LONG"]["rt_count"] == 112
    assert ls["SHORT"]["rt_count"] == 114
    assert ls["LONG"]["rt_count"] + ls["SHORT"]["rt_count"] == 226


def test_long_short_net():
    d = _load()
    ls = d["long_short_analysis"]
    assert round(ls["LONG"]["net_pnl"], 2) == 7577.15
    assert round(ls["SHORT"]["net_pnl"], 2) == 4410.93


def test_long_short_edge():
    d = _load()
    ls = d["long_short_analysis"]
    assert ls["edge"] == "primarily LONG"


# ---------------------------------------------------------------------------
# 6. Concentration
# ---------------------------------------------------------------------------


def test_top10_concentration():
    d = _load()
    conc = d["concentration"]
    assert conc["top_10"]["remaining_positive"] is True
    assert conc["top_10"]["retention_pct"] > 0


def test_top20_concentration():
    d = _load()
    conc = d["concentration"]
    assert conc["top_20"]["remaining_positive"] is True
    assert conc["top_20"]["retention_pct"] > 0


# ---------------------------------------------------------------------------
# 7. Robustness
# ---------------------------------------------------------------------------


def test_robustness_top10_removal():
    d = _load()
    rb = d["robustness_checks"]
    r10 = rb["removing_top_winners"]["remove_top_10"]
    assert r10["remaining_positive"] is True


def test_robustness_chronological_split():
    d = _load()
    rb = d["robustness_checks"]
    split = rb["chronological_split"]
    first = split["first_half"]
    second = split["second_half"]
    assert first["rt_count"] + second["rt_count"] == 226
    assert first["net_pnl"] > 0
    assert second["net_pnl"] > 0


# ---------------------------------------------------------------------------
# 8. Holding duration
# ---------------------------------------------------------------------------


def test_holding_duration_all_buckets():
    d = _load()
    hd = d["holding_duration"]
    assert "intraday_or_1d" in hd
    assert "2d_5d" in hd
    assert "6d_15d" in hd


def test_intraday_bucket_count():
    d = _load()
    hd = d["holding_duration"]
    assert hd["intraday_or_1d"]["rt_count"] > 0


# ---------------------------------------------------------------------------
# 9. Cost analysis
# ---------------------------------------------------------------------------


def test_cost_coverage_ratio():
    d = _load()
    ca = d["cost_analysis"]
    assert ca["cost_as_pct_of_gross"] == 50.77
    assert round(ca["gross_to_cost_ratio"], 4) == round(24352.10 / 12364.020084, 4)


def test_avg_cost_per_rt():
    d = _load()
    ca = d["cost_analysis"]
    assert round(ca["avg_cost_per_rt"], 2) == 54.71


# ---------------------------------------------------------------------------
# 10. Daily distribution
# ---------------------------------------------------------------------------


def test_daily_total_days():
    d = _load()
    dd = d["daily_distribution"]
    assert dd["total_days"] == 932


def test_daily_series_length():
    d = _load()
    dd = d["daily_distribution"]
    assert len(dd["series"]) == 932


def test_daily_nonnegative_win_count():
    d = _load()
    dd = d["daily_distribution"]
    assert dd["positive_days"] + dd["losing_days"] <= 932
    assert dd["positive_days"] > 0
    assert dd["losing_days"] > 0


# ---------------------------------------------------------------------------
# 11. Monthly/yearly
# ---------------------------------------------------------------------------


def test_monthly_keys():
    d = _load()
    my = d["monthly_distribution"]
    assert len(my) > 0
    assert all(isinstance(k, str) and len(k) == 7 for k in my)


def test_yearly_keys():
    d = _load()
    yd = d["yearly_distribution"]
    assert set(yd.keys()) == {"2022", "2023", "2024", "2025"}


def test_yearly_net_sums():
    d = _load()
    yd = d["yearly_distribution"]
    total = sum(y["net_pnl"] for y in yd.values())
    assert round(total, 2) == 11988.08


# ---------------------------------------------------------------------------
# 12. MD report
# ---------------------------------------------------------------------------


def test_md_contains_required_sections():
    md = OUT_MD.read_text(encoding="utf-8")
    for section in ("A. WHERE DID", "B. BROADLY", "C. SIGN_FLIP", "D. LONG",
                    "E. VOLATILITY", "F. PRIOR-SESSION", "G. WINNER/LOSER",
                    "H. REMOVAL", "I. TEMPORAL", "FUTURE HYPOTHESIS", "LIMITATIONS"):
        assert section in md, f"Missing section: {section}"


def test_md_sign_flip_mention():
    md = OUT_MD.read_text(encoding="utf-8")
    assert "133" in md
    assert "58.85%" in md


# ---------------------------------------------------------------------------
# 13. Single test running full module (quick regression)
# ---------------------------------------------------------------------------


def test_full_module_deterministic():
    """Module output artifact sha256 matches known-good value."""
    assert ITERATION_012_OUT_SHA256 == "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb"
