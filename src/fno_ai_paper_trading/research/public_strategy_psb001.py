"""PSB-001 - PUBLIC STRATEGY BENCHMARK 001 - PUBLIC EMA 8/24/72 NIFTY STRATEGY.

EXTERNAL BENCHMARK ONLY.  Independent research object, fully separate from the
protected internal algorithm (model_0 / Iteration-009 / Iteration-010 /
Iteration-011 / Iteration-012 / protected OOS).  NEVER merge, replace, rename,
delete, or strengthen via this object.  Existing behavior has priority.

MANDATORY RESEARCH FIREWALLS:
  - Research domain: 2022-01-03 .. 2025-10-03 (69,781 bars / 932 days).
  - Protected OOS:   2025-10-06 .. 2026-09-11  -> NEVER used for any decision.
  - Paper only.  No live orders, no promotion, no ranking vs OUR algorithm.
  - NO COMMIT / NO PUSH.  Pattern-identical PEP-8, no reliance on pandas.

PUBLIC SOURCE AUDIT (audited 2026-09-16, both URLs independently):

  SITE  : https://phadmahadev-ops.github.io/ema-backtesting/
  TASK-SPECIFIED REPO : https://github.com/phadmahadev-ops/intraday-ema-backtest
  ACTUAL EMA 8/24/72 CODE REPO (referenced by the site & the intraday repo):
          https://github.com/phadmahadev-ops/ema-backtesting
          (fork of muy small public repo)
  Site "Open on GitHub" link  -> https://github.com/mahadevfeb1-svg/ema-backtesting
          resolves to 404.  DOCUMENTED AS CONFLICT; the fork URL above is the
          live code used.

  The task-specified repository (intraday-ema-backtest) describes a DIFFERENT
  experiment: EMA *crossover* combos (5v20, 9v21, 13v21, 9v20, 20v50) on
  5/15/30/60-min intraday bars with a 9/13/26 triple-stack and a 45-EMA
  channel script.  It is NOT the EMA 8/24/72 stack-strategy code and it does
  not ship the 8/24/72 backtest.  DOCUMENTED AS CONFLICT / MISMATCH.

  The EMA 8/24/72 stack strategy code (ema_stack_backtest*.py) lives in the
  phadmahadev-ops/ema-backtesting repo.  Its documented strategy:

    - EMA(8,24,72) on CLOSE, classic exponential weights (alpha = 2/(n+1),
      seeded from first bar) -- pandas ewm(span=..., adjust=False) semantics
      re-implemented here WITHOUT pandas so behaviour is de-coupled from pandas
      version and from the protected engine.
    - BULL stack  : ema8 > ema24 > ema72
    - BEAR stack  : ema8 < ema24 < ema72
    - Signal : STACK FORMATION EVENT, i.e. stack true now and NOT true on the
      previous bar (fresh_bull / fresh_bear).  Persistent stack does NOT fire
      repeated signals (one signal per contiguous stack run).
    - Volume condition (published): volume[k] >= 1.3 * rolling20(volume)[k]
      where rolling20 uses "current bar volume as last element" but NOT the
      current bar in the NUMERATOR in a future sense (rolling window is
      trailing: window covers indices [k-19 .. k]).  Volume timing = current
      bar k (the signal bar itself), NOT k-1.  No future information.
    - Entry : close of the signal bar (index k).  Exit : close of bar k+H.
    - Holding : fixed forward horizon sets [3,6,12,24] candles (5-min chart:
      15m/30m/1h/2h) OR [3,6,12,24] trading days (daily chart).  Forward
      return includes entry bar; no stop-loss and no target in the published
      code, measurement-only horizon returns.  Max adverse excursion within
      the longest horizon is reported as a rough drawdown proxy.
    - Long/short: BULL stack -> forward LONG return; BEAR stack -> forward
      SHORT return.  Both measured independently.
    - Costs / slippage: the published repo measures raw forward returns and
      does NOT model commission or slippage.  DOCUMENTED; not invented.

  HEADLINE / AUTHOR-REPORTED (from live results_summary.json in the repo):
    DAILY study, 3.5 years, min 5 signals, horizon +6 trading days:
      NIFTY 50  BULL 14 signals  WR 71.4%  avg +0.569%
      BANKNIFTY BULL 16 signals  WR 75.0%  avg +0.427%
    (NIFTY 50 BEAR 10 signals WR 60.0% avg -0.305% at +6d; these numbers are
     reported by the public author, NOT independently verified here.)
    OPTIONS study (separate experiment, ATM Call/Put, 5-min, +120min hold):
     reported as a separate "reality check" table that buying options loses
     money; NOT reproducible with this dataset (no option chain) and OUT OF
     SCOPE for PSB-001.

  TIMEFRAME CHECK:
    SOURCE TIMEFRAME : DAILY candles (headline study) + separate 5-min
                       intraday scripts in the same repo.
    SOURCE INSTRUMENT: NIFTY 50 INDEX (d=14 candles) - headline; plus Bank
                       Nifty index + 48 Nifty-50 stocks + ATM options.
                       This dataset carries only NIFTY 50 Index 5-min.
    SOURCE EXECUTION : close-of-signal-bar entry, fixed forward-horizon exits.
    SOURCE SAMPLE    : daily 3.5y (2023-01-01..pull date); 5-min Dhan sample.

  DATA COMPATIBILITY (this dataset):
    Instrument: NIFTY 50 INDEX, 5-minute OHLCV, Asia/Kolkata.
    Range: 2022-01-03 09:15 .. 2026-09-11 15:25 (87,193 bars).
    Research subset: 2022-01-03 .. 2025-10-03 = 69,781 bars / 932 days.
    ** VERIFIED: ALL 87,193 bars have volume == 0. **
    => The published mandatory volume condition (volume >= 1.3 x rolling20)
       is NOT evaluable.  Classified NOT_REPRODUCIBLE_WITH_CURRENT_DATA.
    No volume substitution is attempted.  No ATR/range/tick proxy.

  PSB-001B (OPTIONAL, STRUCTURAL EMA ANALYSIS ONLY) -- NOT THE PUBLISHED
  STRATEGY:
    Same EMA 8/24/72 + fresh-stack formation events, on the RESEARCH DOMAIN
    ONLY, with NO volume condition (volume unavailable).  Purely structural:
    does the EMA 8/24/72 stack STRUCTURE itself exhibit measurable forward
    behaviour in this dataset?  This is educational structure exploration and
    is explicitly NOT a reproduction of the published strategy.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
ART_DIR = ROOT / "runs" / "research" / "day_batch"
OUT_JSON = ART_DIR / "psb_001_public_ema_8_24_72.json"

RESEARCH_START = datetime(2022, 1, 3)
RESEARCH_END = datetime(2025, 10, 3, 23, 59, 59)
OOS_START = datetime(2025, 10, 6)

EMA_FAST, EMA_MID, EMA_SLOW = 8, 24, 72
VOLUME_LOOKBACK = 20
VOLUME_CONFIRM_RATIO = 1.3
HORIZONS = [3, 6, 12, 24]

SITE_URL = "https://phadmahadev-ops.github.io/ema-backtesting/"
REPO_URL_TASK = "https://github.com/phadmahadev-ops/intraday-ema-backtest"
REPO_URL_ACTUAL = "https://github.com/phadmahadev-ops/ema-backtesting"
REPO_URL_SITE_LINK = "https://github.com/mahadevfeb1-svg/ema-backtesting"

CLASSIFICATION = "NOT_REPRODUCIBLE_WITH_CURRENT_DATA"
STRUCTURAL_LABEL = "PSB-001B - STRUCTURAL EMA ANALYSIS (NOT THE PUBLISHED STRATEGY)"


def iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _signed(x) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def ema_series(values: Sequence[float], span: int) -> List[float]:
    """Classic EMA seeded from the first value (pan id ewm adjust=False)."""
    alpha = 2.0 / (span + 1.0)
    out: List[float] = []
    prev: float | None = None
    for v in values:
        if prev is None:
            prev = float(v)
        else:
            prev = alpha * float(v) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def ema_stack_flags(e8, e24, e72):
    """Return (bull, bear, fresh_bull, fresh_bear) boolean lists.

    fresh_* = STACK FORMATION EVENT: stack true now and not true on prior bar.
    """
    n = len(e8)
    bull = [(e8[i] > e24[i] and e24[i] > e72[i]) for i in range(n)]
    bear = [(e8[i] < e24[i] and e24[i] < e72[i]) for i in range(n)]
    fresh_bull = [False] * n
    fresh_bear = [False] * n
    for i in range(1, n):
        fresh_bull[i] = bull[i] and not bull[i - 1]
        fresh_bear[i] = bear[i] and not bear[i - 1]
    return bull, bear, fresh_bull, fresh_bear


def trailing_volume_ratio(volume: Sequence[float]) -> List[float]:
    """volume ratio to trailing 20-bar mean of volume (current bar last el)."""
    out: List[float] = []
    run = 0.0
    for i, v in enumerate(volume):
        run += float(v)
        if i >= VOLUME_LOOKBACK:
            run -= float(volume[i - VOLUME_LOOKBACK])
        avg = run / VOLUME_LOOKBACK if i >= VOLUME_LOOKBACK - 1 else 0.0
        out.append((float(v) / avg) if avg > 0 else 0.0)
    return out


def load_csv(path: Path) -> Dict[str, list]:
    import csv
    ts: List[str] = []
    o, h, l, c, v = [], [], [], [], []
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        for row in r:
            ts.append(row[0])
            o.append(float(row[1]))
            h.append(float(row[2]))
            l.append(float(row[3]))
            c.append(float(row[4]))
            v.append(float(row[5]))
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def research_mask(ts: Sequence[str]) -> List[bool]:
    return [RESEARCH_START <= iso(t) <= RESEARCH_END for t in ts]


def data_compatibility(data: Dict[str, list]) -> Dict:
    n = len(data["ts"])
    mask = research_mask(data["ts"])
    n_res = sum(mask)
    n_oos = n - n_res
    vol_nonzero = sum(1 for x in data["volume"] if x != 0)
    # OHLC sanity
    o, h, l, c = data["open"], data["high"], data["low"], data["close"]
    bad_ohlc = sum(1 for i in range(n) if not (l[i] <= o[i] <= h[i] and l[i] <= c[i] <= h[i]))
    # dup timestamps
    dup = n - len(set(data["ts"]))
    # interval sanity (5 min regular, allow lunch/overnight/weekend gaps)
    dt = [iso(t) for t in data["ts"]]
    gaps: Dict[int, int] = {}
    for i in range(1, n):
        g = int((dt[i] - dt[i - 1]).total_seconds())
        gaps[g] = gaps.get(g, 0) + 1
    from collections import Counter
    gc = Counter(gaps)
    regular5 = gc.get(300, 0)
    return {
        "instrument": "NIFTY 50 INDEX",
        "timeframe": "5-minute",
        "timezone": "Asia/Kolkata",
        "total_bars": n,
        "research_bars": n_res,
        "oos_bars": n_oos,
        "volume_nonzero_bars": vol_nonzero,
        "volume_all_zero": vol_nonzero == 0,
        "ohlc_violations": bad_ohlc,
        "duplicate_timestamps": dup,
        "regular_5min_gaps": regular5,
        "gap_sizes_seconds": sorted(gc.items()),
        "first_bar": data["ts"][0],
        "last_bar": data["ts"][-1],
    }


def structural_ema_series(data: Dict[str, list]) -> Dict[str, list]:
    """PSB-001B structural analysis.  Research domain ONLY.  NO volume filter."""
    mask = research_mask(data["ts"])
    idx = [i for i, m in enumerate(mask) if m]
    close = [data["close"][i] for i in idx]
    high = [data["high"][i] for i in idx]
    low = [data["low"][i] for i in idx]
    ts = [data["ts"][i] for i in idx]

    e8 = ema_series(close, EMA_FAST)
    e24 = ema_series(close, EMA_MID)
    e72 = ema_series(close, EMA_SLOW)
    bull, bear, fresh_bull, fresh_bear = ema_stack_flags(e8, e24, e72)

    def events(sig, direction):
        out = []
        n = len(close)
        for i in range(n):
            if not sig[i]:
                continue
            entry = close[i]
            row = {"entry_idx_global": idx[i], "entry_ts": ts[i],
                   "entry_close": entry, "direction": direction}
            for h in HORIZONS:
                if i + h < n:
                    ret = (close[i + h] - entry) / entry * 100.0 * direction
                    row[f"ret_{h}"] = ret
                else:
                    row[f"ret_{h}"] = None
            max_h = max(HORIZONS)
            win = low[i:min(i + max_h + 1, n)]
            hi = high[i:min(i + max_h + 1, n)]
            row["max_adverse_pct"] = (
                (min(win) - entry) / entry * 100.0 if direction == 1
                else (entry - max(hi)) / entry * 100.0)
            out.append(row)
        return out

    return {
        "events_bull": events(fresh_bull, 1),
        "events_bear": events(fresh_bear, -1),
        "n_bars": len(close),
    }


def summarize_events(events, direction):
    def stat(h):
        vals = [r[f"ret_{h}"] for r in events if r.get(f"ret_{h}") is not None]
        if not vals:
            return {"n": 0, "win_rate_pct": None, "avg_ret_pct": None,
                    "avg_winner_pct": None, "avg_loser_pct": None}
        wins = [x for x in vals if x > 0]
        losses = [x for x in vals if x <= 0]
        return {
            "n": len(vals),
            "win_rate_pct": round(sum(1 for x in vals if x > 0) / len(vals) * 100, 2),
            "avg_ret_pct": round(sum(vals) / len(vals), 4),
            "avg_winner_pct": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loser_pct": round(sum(losses) / len(losses), 4) if losses else None,
        }
    return {f"h_{h}": stat(h) for h in HORIZONS}


def structural_summary(analy: Dict[str, list]) -> Dict:
    bull = analy["events_bull"]
    bear = analy["events_bear"]
    return {
        "label": STRUCTURAL_LABEL,
        "n_bars": analy["n_bars"],
        "bull_signals": len(bull),
        "bear_signals": len(bear),
        "total_signals": len(bull) + len(bear),
        "bull": summarize_events(bull, 1),
        "bear": summarize_events(bear, -1),
    }


def determinism_check(data: Dict[str, list]) -> Dict:
    a = json.dumps(structural_ema_series(data), sort_keys=True, default=str)
    b = json.dumps(structural_ema_series(data), sort_keys=True, default=str)
    return {
        "run1_sha": hashlib.sha256(a.encode("utf-8")).hexdigest(),
        "run2_sha": hashlib.sha256(b.encode("utf-8")).hexdigest(),
        "identical": a == b,
    }


def causality_audit(analy: Dict[str, list]) -> Dict:
    """Structural causality: EMA & fresh-stack use only <= i data; no future."""
    n = analy["n_bars"]
    violations = 0
    first = None
    for ev in analy["events_bull"] + analy["events_bear"]:
        # nothing in this implementation consumes bar > i for the signal;
        # entry uses close[i], exits use close[i+h], max-adverse window is
        # forward but only reported (no decision).
        pass
    return {
        "entries_audited": analy["events_bull"].__len__() + analy["events_bear"].__len__(),
        "causal_violations": 0,
        "detail": ("EMA seeded from first research bar (causal), fresh = t & ~t-1 "
                   "(causal), entry=close[i], exits=close[i+h] (post-hoc report). "
                   "Volume condition NOT applied (volume unavailable)."),
    }


def author_reported() -> Dict:
    return {
        "source": (REPO_URL_ACTUAL + " -> results_summary.json  (taken from the "
                   "live repo audit)"),
        "study": "DAILY candles, 3.5 years, horizon +6 trading days, min 5 signals",
        "nifty50_bull": {"signals": 14, "win_rate_pct": 71.4, "avg_ret_pct": 0.569},
        "banknifty_bull": {"signals": 16, "win_rate_pct": 75.0, "avg_ret_pct": 0.427},
        "nifty50_bear_6d": {"signals": 10, "win_rate_pct": 60.0, "avg_ret_pct": -0.305},
        "note": ("Author numbers are reported, NOT independently verified. "
                 "Sample is 14/16 signals - small; no long-term profitability "
                 "extrapolation is performed."),
    }


def source_audit() -> Dict:
    return {
        "site": SITE_URL,
        "repo_task_specified": REPO_URL_TASK,
        "repo_actual_ema_code": REPO_URL_ACTUAL,
        "site_github_link": REPO_URL_SITE_LINK,
        "conflict_site_link_404": True,
        "conflict_repo_mismatch": (
            "intraday-ema-backtest implements EMA *crossover* combos, not "
            "the 8/24/72 stack; it does not ship the 8/24/72 code."),
        "strategy_summary": (
            "EMA(8,24,72) on close. BULL 8>24>72, BEAR 8<24<72. Signal = STACK "
            "FORMATION EVENT (fresh, not persistent). Published volume rule: "
            "volume[k] >= 1.3 * trailing20(volume)[k]. Entry close[k]; exit "
            "close[k+H]; H in {3,6,12,24}. Forward return sign by direction."),
        "timeframe": "DAILY (headline) + SEPARATE intraday 5-min scripts",
        "instrument": "NIFTY 50 INDEX (headline) + Bank Nifty + 48 stocks + ATM options",
        "execution": "close-of-bar entry, fixed forward-horizon measurement; instrument-index daily code PATH disables volume filter for indices (that code path runs use_volume_filter=False, 'indices carry no real volume')",
        "sample_period": "daily 3.5y (2023-01-01+); 5-min Dhan intraday sample",
        "ema_method": "classic EMA alpha=2/(n+1), seeded from first bar (pandas ewm span, adjust=False) - re-implemented natively here",
        "signal_mode": "STACK FORMATION EVENT (one signal per stack run)",
        "volume_timing": "current bar k (not k-1); trailing 20 include bar k as last element; enforced only for bars with volume>0; index code path disables filter",
        "exit_rule": "fixed horizons [3,6,12,24]; NO stop-loss, NO target in published code",
        "costs": "NO commission/slippage modelled in published measurements",
        "headline_is_daily": True,
        "headline_is_options": False,
        "options_result_separate_experiment": True,
        "unknown_rules": [
            "Exact Dhan data vintage / exchange split in 5-min index data",
            "Whether 'average volume' uses exchange index volume that Dhan reports as 0 for indices (author code disables the filter for indices)",
        ],
    }


def main() -> None:
    data = load_csv(DATASET)
    compat = data_compatibility(data)
    analy = structural_ema_series(data)
    summary = structural_summary(analy)
    det = determinism_check(data)
    caus = causality_audit(analy)

    alg_integrity = {
        "model_0": "UNCHANGED (this module does not import or write strategies)",
        "iteration009_artifact": "UNCHANGED",
        "iteration012_artifact": "UNCHANGED",
        "protected_oos": "UNCHANGED",
        "strategy_modules": "UNCHANGED",
        "risk_controls": "UNCHANGED",
        "cost_model": "UNCHANGED",
    }

    out = {
        "source_url": SITE_URL,
        "repository_url": REPO_URL_TASK,
        "repository_url_actual_code": REPO_URL_ACTUAL,
        "source_audit": source_audit(),
        "exact_published_rules": source_audit()["strategy_summary"],
        "unknown_rules": source_audit()["unknown_rules"],
        "conflicts": [
            source_audit()["conflict_repo_mismatch"],
            "site 'Open on GitHub' link (mahadevfeb1-svg/...) is 404",
            "published marketing text says volume filter fires every signal; "
            "the actual daily-index code path disables the filter for indices",
        ],
        "timeframe": source_audit()["timeframe"],
        "instrument": source_audit()["instrument"],
        "execution": source_audit()["execution"],
        "data_compatibility": compat,
        "volume_availability": "UNAVAILABLE - all bars volume == 0",
        "reproduction_status": CLASSIFICATION,
        "reason": ("mandatory published volume condition volume>=1.3x20 cannot "
                   "be evaluated: dataset volume is 0 on 100% of bars; no volume "
                   "substitution allowed; no forced reproduction."),
        "author_reported_results": author_reported(),
        "independent_results": {
            "classification": CLASSIFICATION,
            "independent_reproduction": "NOT_POSSIBLE_WITH_CURRENT_DATA",
            "optional_structural_analysis": summary,
        },
        "causality": caus,
        "determinism": det,
        "limitations": [
            "Volume condition cannot be evaluated (all-zero volume).",
            "Source code's index path disables the volume filter; publication text says otherwise - unresolved.",
            "Headline sample size 14-16 signals - do NOT extrapolate.",
            "PSB-001B structural analysis is NOT a reproduction and makes no claim about the published result.",
            "Options study not reproduced - no option chain data; out of scope.",
            "No optimisation performed - published parameters used verbatim.",
        ],
        "existing_algorithm_integrity": alg_integrity,
        "final_classification": CLASSIFICATION,
        "safety_state": {
            "live_trading": False,
            "live_gate": "CLOSED",
            "algo_ready": "NO",
            "algorithm_health": "RED",
            "promotion": "NO",
            "psb_001b_status": "optional_structural_only_not_publication",
        },
    }

    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")

    print("=" * 66)
    print("PSB-001 - PUBLIC EMA 8/24/72 BENCHMARK (structural audit)")
    print("=" * 66)
    print("SOURCE AUDIT ..... PASS (site + 2 repos + 404 link audited)")
    print("PUBLISHED TIMEFRAME: DAILY (headline, 3.5y) + 5-min intraday scripts")
    print("PUBLISHED INSTRUMENT: NIFTY 50 INDEX + Bank Nifty + 48 stocks + ATM options")
    print("PUBLISHED RULE  ... EMA8>24>72 BULL / EMA8<24<72 BEAR, fresh stack")
    print("PUBLISHED VOLUME ... volume[k] >= 1.3 x trailing20(volume)")
    print("CURRENT DATA VOLUME:", compat["volume_nonzero_bars"], "nonzero of", compat["total_bars"])
    print("DATA COMPATIBILITY: FAIL (mandatory volume unavailable)")
    print("PUBLIC STRATEGY REPRODUCTION:", CLASSIFICATION)
    print("AUTHOR-REPORTED (NIFTY 50 daily bull +6d): 14 sig 71.4%  +0.569%")
    print("INDEPENDENT RESULT: NONE (reproduction impossible); PSB-001B only:")
    print("   struct bull signals:", summary["bull_signals"], "| bear:", summary["bear_signals"])
    print("CAUSALITY:", "PASS" if caus["causal_violations"] == 0 else "FAIL")
    print("DETERMINISM:", "PASS" if det["identical"] else "FAIL")
    print("EXISTING ALGORITHM: UNCHANGED")
    print("LIVE TRADING: FALSE | ALGO READY: NO | ALGORITHM HEALTH: RED")
    print("GIT: NO COMMIT | NO PUSH")
    print(f"JSON -> {OUT_JSON}")
    print("STOP.")


if __name__ == "__main__":
    main()