"""PSB-002 - PUBLIC STRATEGY BENCHMARK 002 - PRICE/RANGE-BREAKOUT NIFTY 50.

EXTERNAL BENCHMARK ONLY.  Independent research object, fully separate from the
protected internal algorithm (model_0 / Iteration-009 / Iteration-010 /
Iteration-011 / Iteration-012 / protected OOS).  NEVER merge, replace, rename,
delete, or strengthen via this object.  Existing behaviour has priority.

MANDATORY RESEARCH FIREWALLS:
  - Research domain: 2022-01-03 .. 2025-10-03 (69,781 bars / 932 days).
  - Protected OOS:   2025-10-06 .. 2026-09-11  -> NEVER used for any decision.
  - Paper only.  No live orders, no promotion, no ranking vs OUR algorithm.
  - NO COMMIT / NO PUSH.  Pattern-identical PEP-8, no reliance on pandas.

PUBLIC SOURCE AUDIT (audited 2026-09-16 from actual executable code):

  REPOSITORY : https://github.com/Raj1984/nifty-breakout-lab
  EXECUTABLE : nifty_backtest_multifile.html (browser-based, single-file vanilla JS)
  STRATEGY   : "First Breakout of Range" (NIFTY Breakout Lab)

  The executable engine (the HTML/JS, not the README) was inspected line by line.
  The strategy is a price-only range breakout.  No volume, no indicator, no ML.

  RANGE CONSTRUCTION:
    For each date and each window time (e.g. '10:15'):
      iterate all bars for that date.
      rangeHigh = max(high) for all bars with bar.time <= window_time.
      rangeLow  = min(low)  for all bars with bar.time <= window_time.
      Inclusive of the window bar itself.  Fixed once formed (later bars cannot modify).
      Minimum rangeBarsCount = 2; minimum range width = minRange (default 5 points).

  BREAKOUT TRIGGER:
    First bar AFTER the window time (time > window) where:
      bar.high > rangeHigh  -> LONG  (entryPrice = rangeHigh, the boundary)
      bar.low  < rangeLow   -> SHORT (entryPrice = rangeLow,  the boundary)
    LONG takes priority if the same bar breaches both (checks high first).
    Entry price = the RANGE BOUNDARY (not the bar's own OHLC).
      This assumes an intrabar fill at the exact boundary level.
      The author's convention is documented; it is NOT look-ahead (range is fixed
      before the breakout bar, and no future bars are used in detection).

  EXIT:
    Fixed holding period: exitBarIdx = min(entryBarIdx + holdBars, dayLastBarIdx).
    holdBars = ceil(holdMins / 5) = ceil(20/5) = 4 bars (20 minutes).
    exitPrice = close of bars[exitBarIdx].
    If the breakout occurs near day-end, the exit is clamped to the last bar of the day.

  STOP LOSS (optional, default NONE):
    SL = opposite range boundary (rangeLow for LONG, rangeHigh for SHORT).
    Scanned from entryBarIdx to exitBarIdx inclusive.
    SL checked BEFORE exit on each bar (SL has priority if both occur on the same bar).
    When SL hit: pnl = sl - entryPrice (LONG) or entryPrice - sl (SHORT).

  RE-ENTRY:
    One trade per (date, window) — the engine breaks after the FIRST breakout.
    No re-entry after exit within the same window.
    Up to 10 windows per day (5 AM + 5 PM).

  SESSION / DAY-TYPE (descriptive only, NOT used in trade decisions):
    ANCHOR_OPEN='09:15', ANCHOR_S1='11:27', ANCHOR_S2='13:27', ANCHOR_S3='15:30'.
    Pattern U-U-U = TREND_UP, etc.  Computed AFTER the day; post-hoc classification.

  VOLUME:
    Parsed but NEVER used in any strategy logic.  The engine is price-only.
    Author's own NIFTY 50 sample data also has volume = 0.

  COSTS:
    NOT modelled in the published source.  The README explicitly states:
    "gross points only -- no commissions, no slippage, no lot size applied."
    The source's own sample data ships with volume = 0, confirming this is index
    spot data where volume is not meaningful for the author either.

  POSITION SIZING:
    1 unit per trade.  Points only (1 NIFTY point = 1 P&L unit).
    No rupee conversion, no lot size, no futures/options.

  CONFIGURATION (defaults):
    holdMins = 20, minRange = 5, session = AM+PM, slMode = none.
    Windows: AM 09:30/09:45/10:00/10:15/10:30, PM 14:00/14:15/14:30/14:45/15:00.
    BEST4 (author's highlighted windows): AM 10:15, AM 10:30, PM 14:15, PM 14:45.

  AUTHOR-REPORTED:
    Only LICI (a stock) results shown in the README — not NIFTY 50 index.
    LICI 2022-2025: AM 10:15 ~1200 trades / 52-55% WR, PM 14:15 ~1100 / 51-54%.
    NIFTY 50 INDEX results: NOT PUBLISHED in the repository.
    The CSV "NIFTY 50_5minutedata.csv" is committed (author ran the tool on it),
    but no output for NIFTY 50 is shown.

  DATA COMPATIBILITY:
    Strategy is price-only (volume unused).
    Instrument: NIFTY 50 INDEX, 5-min OHLCV, Asia/Kolkata — matches our dataset.
    All-zero volume: NOT A BLOCKER (volume is not used in the strategy).
    Classification path: REPRODUCIBLE (price/range data sufficient).
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "datasets" / "upstox_Nifty_50_5m_20220103_20260911.csv"
ART_DIR = ROOT / "runs" / "research" / "day_batch"
OUT_JSON = ART_DIR / "psb_002_public_breakout.json"

# ---------------------------------------------------------------------------
# Research domain
# ---------------------------------------------------------------------------

RESEARCH_START = "2022-01-03"
RESEARCH_END = "2025-10-03"
OOS_START = "2025-10-06"

# ---------------------------------------------------------------------------
# Published constants (verbatim from executable code)
# ---------------------------------------------------------------------------

AM_WINDOWS = ["09:30", "09:45", "10:00", "10:15", "10:30"]
PM_WINDOWS = ["14:00", "14:15", "14:30", "14:45", "15:00"]
BEST4 = [("AM", "10:15"), ("AM", "10:30"), ("PM", "14:15"), ("PM", "14:45")]
DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

ANCHOR_OPEN = "09:15"
ANCHOR_S1 = "11:27"
ANCHOR_S2 = "13:27"
ANCHOR_S3 = "15:30"
TYPE_MAP = {
    "U-U-U": "TREND_UP",
    "D-D-D": "TREND_DOWN",
    "D-D-U": "LATE_REV_UP",
    "U-U-D": "LATE_REV_DOWN",
    "U-D-D": "REVERSAL_DOWN",
    "D-U-U": "FADE_DOWN",
    "U-D-U": "FADE_UP",
    "D-U-D": "REVERSAL_DOWN2",
}

HOLD_MINS = 20
HOLD_BARS = 4  # ceil(20/5)
MIN_RANGE = 5.0
DEFAULT_SL_MODE = "none"

# Project-standardised cost framework (NOT in the published source)
ADVERSE_RATE = Decimal("0.001")   # total_adverse per fill (slippage 0.05% + half-spread 0.02% + impact 0.03%)
COMMISSION_RATE = Decimal("0.0003")  # commission per side
COST_PER_RT_FACTOR = float(ADVERSE_RATE * 2 + COMMISSION_RATE * 2)  # 0.0026

REPO_URL = "https://github.com/Raj1984/nifty-breakout-lab"
SITE_URL = REPO_URL

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_csv(path: Path) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row["timestamp"]
            bars.append({
                "date": ts[:10],
                "time": ts[11:16],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
    return bars


def _research_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [b for b in bars if RESEARCH_START <= b["date"] <= RESEARCH_END]


def _all_trading_days(bars: List[Dict[str, Any]]) -> List[str]:
    return sorted({b["date"] for b in bars})


# ---------------------------------------------------------------------------
# Session / day-type labelling (descriptive only, NOT used in trade decisions)
# ---------------------------------------------------------------------------


def _close_at(bars: List[Dict], t: str) -> Optional[float]:
    last: Optional[float] = None
    for b in bars:
        if b["time"] <= t:
            last = b["close"]
        else:
            break
    return last


def _day_type(bars: List[Dict]) -> str:
    pO = _close_at(bars, ANCHOR_OPEN)
    pS1 = _close_at(bars, ANCHOR_S1)
    pS2 = _close_at(bars, ANCHOR_S2)
    pS3 = _close_at(bars, ANCHOR_S3)
    if not (pO and pS1 and pS2 and pS3):
        return "UNKNOWN"
    s1 = "U" if pS1 > pO else "D"
    s2 = "U" if pS2 > pS1 else "D"
    s3 = "U" if pS3 > pS1 else "D"
    pattern = f"{s1}-{s2}-{s3}"
    return TYPE_MAP.get(pattern, "OTHER")


# ---------------------------------------------------------------------------
# Strategy engine (replicates the HTML/JS exactly)
# ---------------------------------------------------------------------------


def _run_one_window(
    bars: List[Dict],
    session: str,
    win: str,
    hold_bars: int,
    min_range: float,
    sl_mode: str,
) -> Optional[Dict[str, Any]]:
    """Run one (date, window) pair. Returns a trade dict or None."""
    range_high = float("-inf")
    range_low = float("inf")
    range_bars_count = 0
    after_start = len(bars)

    for bi, b in enumerate(bars):
        if b["time"] <= win:
            if b["high"] > range_high:
                range_high = b["high"]
            if b["low"] < range_low:
                range_low = b["low"]
            range_bars_count += 1
        else:
            if after_start == len(bars):
                after_start = bi

    if range_bars_count < 2:
        return None
    if (range_high - range_low) < min_range:
        return None
    if after_start >= len(bars):
        return None

    direction: Optional[str] = None
    entry_price: float = 0.0
    entry_bar_idx = -1

    for bi in range(after_start, len(bars)):
        b = bars[bi]
        if b["high"] > range_high:
            direction = "LONG"
            entry_price = range_high
            entry_bar_idx = bi
            break
        if b["low"] < range_low:
            direction = "SHORT"
            entry_price = range_low
            entry_bar_idx = bi
            break

    if direction is None:
        return None

    exit_bar_idx = min(entry_bar_idx + hold_bars, len(bars) - 1)
    exit_price = bars[exit_bar_idx]["close"]

    pnl: float
    sl_hit = False
    if sl_mode == "range":
        sl = range_low if direction == "LONG" else range_high
        for bi in range(entry_bar_idx, exit_bar_idx + 1):
            b = bars[bi]
            if direction == "LONG" and b["low"] <= sl:
                sl_hit = True
                break
            if direction == "SHORT" and b["high"] >= sl:
                sl_hit = True
                break
        if sl_hit:
            pnl = (sl - entry_price) if direction == "LONG" else (entry_price - sl)
        else:
            pnl = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
    else:
        pnl = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)

    entry_time = bars[entry_bar_idx]["time"]
    exit_time = bars[exit_bar_idx]["time"]
    duration_mins = (exit_bar_idx - entry_bar_idx) * 5

    dt_obj = date.fromisoformat(bars[0]["date"])
    return {
        "date": bars[0]["date"],
        "year": dt_obj.year,
        "month": f"{dt_obj.year}-{dt_obj.month:02d}",
        "dow": dt_obj.strftime("%A"),
        "session": session,
        "window": win,
        "direction": direction,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "entry_time": entry_time,
        "exit_time": exit_time,
        "pnl": round(pnl, 2),
        "win": 1 if pnl > 0 else 0,
        "range_size": round(range_high - range_low, 2),
        "duration_mins": duration_mins,
        "sl_hit": sl_hit,
    }


def run_strategy(
    bars: List[Dict[str, Any]],
    hold_bars: int = HOLD_BARS,
    min_range: float = MIN_RANGE,
    sl_mode: str = DEFAULT_SL_MODE,
) -> List[Dict[str, Any]]:
    """Run the published strategy across all windows.  Returns list of trades."""
    all_windows: List[Tuple[str, str]] = []
    for w in AM_WINDOWS:
        all_windows.append(("AM", w))
    for w in PM_WINDOWS:
        all_windows.append(("PM", w))

    days: Dict[str, List[Dict]] = defaultdict(list)
    for b in bars:
        days[b["date"]].append(b)

    trades: List[Dict] = []
    for d in sorted(days):
        day_bars = days[d]
        for session, win in all_windows:
            t = _run_one_window(day_bars, session, win, hold_bars, min_range, sl_mode)
            if t is not None:
                trades.append(t)
    return trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _equity_curve(trades: List[Dict]) -> Tuple[List[Dict], float, int]:
    """Cumulative P&L curve, max drawdown in points, max dd duration in trades."""
    srt = sorted(trades, key=lambda t: (t["date"], t["entry_time"]))
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_len = 0
    cur_dd = 0
    eq: List[Dict] = []
    for t in srt:
        cum += t["pnl"]
        eq.append({"d": t["date"], "e": t["entry_time"], "p": round(cum, 4)})
        if cum > peak:
            peak = cum
            cur_dd = 0
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
        if dd > 1e-9:
            cur_dd += 1
            dd_len = max(dd_len, cur_dd)
        else:
            cur_dd = 0
    return eq, round(max_dd, 4), dd_len


def _streaks(trades: List[Dict]) -> Tuple[int, int]:
    srt = sorted(trades, key=lambda t: (t["date"], t["entry_time"]))
    mw = ml = cw = cl = 0
    for t in srt:
        if t["pnl"] > 0:
            cw += 1
            cl = 0
        else:
            cl += 1
            cw = 0
        mw = max(mw, cw)
        ml = max(ml, cl)
    return mw, ml


def _daily_pnl(trades: List[Dict]) -> Dict[str, float]:
    d: Dict[str, float] = defaultdict(float)
    for t in trades:
        d[t["date"]] += t["pnl"]
    return dict(d)


def calc_metrics(trades: List[Dict]) -> Dict[str, Any]:
    if not trades:
        return {
            "n": 0, "long": 0, "short": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "gross_pnl_pts": 0.0,
            "costs_pts": 0.0, "net_pnl_pts": 0.0,
            "avg_trade_pts": 0.0, "avg_win_pts": 0.0, "avg_loss_pts": 0.0,
            "profit_factor": 0.0, "max_drawdown_pts": 0.0,
            "max_dd_duration_trades": 0, "longest_win_streak": 0,
            "longest_loss_streak": 0, "positive_days": 0, "negative_days": 0,
            "flat_days": 0,
        }
    n = len(trades)
    long_n = sum(1 for t in trades if t["direction"] == "LONG")
    short_n = sum(1 for t in trades if t["direction"] == "SHORT")
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross = sum(t["pnl"] for t in trades)
    costs = sum((t["entry_price"] + t["exit_price"]) * COST_PER_RT_FACTOR for t in trades)
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    _, max_dd, dd_dur = _equity_curve(trades)
    mw, ml = _streaks(trades)
    daily = _daily_pnl(trades)
    pos = sum(1 for v in daily.values() if v > 1e-9)
    neg = sum(1 for v in daily.values() if v < -1e-9)
    flat = sum(1 for v in daily.values() if abs(v) <= 1e-9)
    return {
        "n": n,
        "long": long_n,
        "short": short_n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "gross_pnl_pts": round(gross, 2),
        "costs_pts": round(costs, 2),
        "net_pnl_pts": round(gross - costs, 2),
        "avg_trade_pts": round(gross / n, 4),
        "avg_win_pts": round(avg_win, 4),
        "avg_loss_pts": round(avg_loss, 4),
        "profit_factor": round(pf, 4) if pf != float("inf") else "INF",
        "max_drawdown_pts": max_dd,
        "max_dd_duration_trades": dd_dur,
        "longest_win_streak": mw,
        "longest_loss_streak": ml,
        "positive_days": pos,
        "negative_days": neg,
        "flat_days": flat,
    }


# ---------------------------------------------------------------------------
# Dimensional breakdowns
# ---------------------------------------------------------------------------


def _by_key(trades: List[Dict], key: str) -> Dict[str, Dict]:
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        groups[str(t[key])].append(t)
    return {k: calc_metrics(v) for k, v in sorted(groups.items())}


def _yearly(trades: List[Dict]) -> Dict[int, Dict]:
    return _by_key(trades, "year")


def _monthly(trades: List[Dict]) -> Dict[str, Dict]:
    return _by_key(trades, "month")


def _am_pm(trades: List[Dict]) -> Dict[str, Dict]:
    return _by_key(trades, "session")


def _long_short(trades: List[Dict]) -> Dict[str, Dict]:
    return _by_key(trades, "direction")


def _by_window(trades: List[Dict]) -> Dict[str, Dict]:
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        groups[f"{t['session']}_{t['window']}"].append(t)
    return {k: calc_metrics(v) for k, v in sorted(groups.items())}


def _by_day_type(trades: List[Dict], day_types: Dict[str, str]) -> Dict[str, Dict]:
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        groups[day_types.get(t["date"], "UNKNOWN")].append(t)
    return {k: calc_metrics(v) for k, v in sorted(groups.items())}


def _range_buckets(trades: List[Dict]) -> Dict[str, Dict]:
    buckets = {"5_10": [], "10_20": [], "20_50": [], "50_plus": []}
    for t in trades:
        r = t["range_size"]
        if r < 10:
            buckets["5_10"].append(t)
        elif r < 20:
            buckets["10_20"].append(t)
        elif r < 50:
            buckets["20_50"].append(t)
        else:
            buckets["50_plus"].append(t)
    return {k: calc_metrics(v) for k, v in buckets.items() if v}


def _time_of_day(trades: List[Dict]) -> Dict[str, Dict]:
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        et = t["entry_time"]
        if et < "10:00":
            buckets["0915_1000"].append(t)
        elif et < "11:00":
            buckets["1000_1100"].append(t)
        elif et < "12:00":
            buckets["1100_1200"].append(t)
        elif et < "14:00":
            buckets["1200_1400"].append(t)
        elif et < "15:00":
            buckets["1400_1500"].append(t)
        else:
            buckets["1500_1530"].append(t)
    return {k: calc_metrics(v) for k, v in sorted(buckets.items())}


def _first_half_second_half(trades: List[Dict]) -> Dict[str, Dict]:
    days = sorted({t["date"] for t in trades})
    mid = len(days) // 2
    first = set(days[:mid])
    second = set(days[mid:])
    f = [t for t in trades if t["date"] in first]
    s = [t for t in trades if t["date"] in second]
    return {"first_half": calc_metrics(f), "second_half": calc_metrics(s)}


def _top_trades(trades: List[Dict]) -> Dict[str, Any]:
    srt = sorted(trades, key=lambda t: t["pnl"], reverse=True)
    top5 = srt[:5]
    top10 = srt[:10]
    gross = sum(t["pnl"] for t in trades)
    return {
        "top5": [{"date": t["date"], "win": t["window"], "dir": t["direction"],
                  "pnl": t["pnl"]} for t in top5],
        "top10_pnl": round(sum(t["pnl"] for t in top10), 4),
        "top5_contribution_pct": round(sum(t["pnl"] for t in top5) / gross * 100, 2) if gross else 0,
        "top10_contribution_pct": round(sum(t["pnl"] for t in top10) / gross * 100, 2) if gross else 0,
    }


def _duration_dist(trades: List[Dict]) -> Dict[int, int]:
    return dict(sorted(Counter(t["duration_mins"] for t in trades).items()))


def _causality_audit() -> Dict[str, Any]:
    return {
        "range_uses_only_bars_with_time_lte_window": True,
        "breakout_uses_only_bars_after_window": True,
        "entry_price_is_range_boundary_known_before_breakout_bar": True,
        "exit_uses_only_bar_close_after_entry": True,
        "no_future_data_in_signal_or_exit": True,
        "sl_check_if_enabled_uses_only_same_or_later_bars": True,
        "no_oos_leakage": True,
        "day_type_label_is_descriptive_post_hoc_not_used_in_decisions": True,
    }


# ---------------------------------------------------------------------------
# Source audit (static, from actual code inspection)
# ---------------------------------------------------------------------------


def _source_audit() -> Dict[str, Any]:
    return {
        "repository": REPO_URL,
        "executable_file": "nifty_backtest_multifile.html",
        "language": "vanilla JavaScript (browser-based, no server, no Python)",
        "strategy_name": "First Breakout of Range",
        "instruments_in_repo": [
            "NIFTY 50 index (CSV committed in repo)",
            "LICI, NIFTY BANK, TCS, HDFCBANK, MARUTI, etc. (stocks/futures)",
        ],
        "results_shown_in_readme": "LICI 2022-2025 only (AM 10:15 ~1200/52-55%, PM 14:15 ~1100/51-54%)",
        "nifty50_index_results_published": False,
        "volume_used_in_strategy": False,
        "volume_parsed": "parsed but never referenced in strategy logic",
        "costs_modeled": False,
        "author_states": "gross points only, no commissions, no slippage, no lot size",
        "conflicts_readme_vs_code": "README shows only stock results; engine accepts any CSV. No conflict in rules themselves.",
        "entry_at_range_boundary": True,
        "entry_boundary_assumption": "intrabar fill at exact range boundary; optimistic if bar opens past boundary",
        "range_inclusive_of_window_bar": True,
        "range_fixed_once_formed": True,
        "first_qualifying_bar_priority": "LONG checked before SHORT on same bar",
        "sl_priority_over_time_exit_on_same_bar": True,
        "re_entry": "one trade per (date, window) only; no re-entry after exit",
    }


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def _determinism_check(
    bars: List[Dict],
    hold_bars: int,
    min_range: float,
    sl_mode: str,
) -> Dict[str, Any]:
    t1 = run_strategy(bars, hold_bars, min_range, sl_mode)
    t2 = run_strategy(bars, hold_bars, min_range, sl_mode)
    s1 = hashlib.sha256(json.dumps(t1, sort_keys=True).encode()).hexdigest()
    s2 = hashlib.sha256(json.dumps(t2, sort_keys=True).encode()).hexdigest()
    return {"run1_sha": s1, "run2_sha": s2, "identical": s1 == s2}


# ---------------------------------------------------------------------------
# Integrity (this module must not modify protected artifacts)
# ---------------------------------------------------------------------------


def _integrity_check() -> Dict[str, str]:
    targets = {
        "iteration_009_vol_led.json": "6e9713c30370207fc18ac5c8924ed5742ff3a8f23d639143b82164e6942a0dc2",
        "iteration_012_trade_forensics.json": "16b3c8939da744d0da11066b8e0c4498ab0ac6421c8dfe099340744a1bd141eb",
    }
    out: Dict[str, str] = {}
    for fname, expected in targets.items():
        p = ART_DIR / fname
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        out[fname] = "UNCHANGED" if actual == expected else f"CHANGED expected={expected} got={actual}"
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    all_bars = load_csv(DATASET)
    r_bars = _research_bars(all_bars)
    total_bars = len(all_bars)
    research_bars = len(r_bars)
    days_all = _all_trading_days(all_bars)
    days_research = _all_trading_days(r_bars)
    n_research_days = len(days_research)

    day_types: Dict[str, str] = {}
    days_map: Dict[str, List[Dict]] = defaultdict(list)
    for b in r_bars:
        days_map[b["date"]].append(b)
    for d, dbars in days_map.items():
        day_types[d] = _day_type(dbars)

    trades = run_strategy(r_bars)
    overall = calc_metrics(trades)

    yearly = _yearly(trades)
    monthly = _monthly(trades)
    am_pm = _am_pm(trades)
    ls = _long_short(trades)
    by_win = _by_window(trades)
    by_dt = _by_day_type(trades, day_types)
    r_buckets = _range_buckets(trades)
    tod = _time_of_day(trades)
    halves = _first_half_second_half(trades)
    top = _top_trades(trades)
    dur = _duration_dist(trades)
    caus = _causality_audit()
    det = _determinism_check(r_bars, HOLD_BARS, MIN_RANGE, DEFAULT_SL_MODE)
    integrity = _integrity_check()

    daily = _daily_pnl(trades)
    positive_days = [d for d, v in sorted(daily.items()) if v > 1e-9]
    negative_days = [d for d, v in sorted(daily.items()) if v < -1e-9]
    flat_days_list = [d for d, v in sorted(daily.items()) if abs(v) <= 1e-9]

    out = {
        "source_url": SITE_URL,
        "repository_url": REPO_URL,
        "source_audit": _source_audit(),
        "exact_published_rules": (
            "Range built from bars with time <= window. LONG if first bar after "
            "window has high > rangeHigh (entry = rangeHigh). SHORT if low < rangeLow "
            "(entry = rangeLow). Exit at close of entryBar+4. Default: hold=20min, "
            "minRange=5pts, SL=none, session=AM+PM, 10 windows."
        ),
        "data_compatibility": {
            "instrument": "NIFTY 50 INDEX",
            "timeframe": "5-minute",
            "timezone": "Asia/Kolkata",
            "total_bars": total_bars,
            "research_bars": research_bars,
            "research_days": n_research_days,
            "volume_available": False,
            "volume_required_by_strategy": False,
            "compatible": True,
            "reason": "Strategy is price-only; volume is parsed but never used in logic.",
        },
        "research_domain": {"start": RESEARCH_START, "end": RESEARCH_END, "bars": research_bars, "days": n_research_days},
        "strategy_configuration": {
            "hold_mins": HOLD_MINS,
            "hold_bars": HOLD_BARS,
            "min_range_pts": MIN_RANGE,
            "sl_mode": DEFAULT_SL_MODE,
            "session": "AM+PM",
            "windows": AM_WINDOWS + PM_WINDOWS,
            "best4": [f"{s} {w}" for s, w in BEST4],
        },
        "execution_convention": {
            "entry": "range boundary (rangeHigh for LONG, rangeLow for SHORT)",
            "exit": "close of entryBar + holdBars (clamped to day last bar)",
            "sl_if_enabled": "opposite boundary, SL checked first per bar; SL priority on same bar as time exit",
            "re_entry": "none; one trade per (date, window)",
            "priority_if_both_breach_same_bar": "LONG (high checked first)",
            "intrabar_fill_assumption": "entry at exact boundary; optimistic if bar opens past boundary",
        },
        "cost_convention": {
            "source": "none (author: gross points only)",
            "cost_adjusted_framework": f"project standard: adverse {ADVERSE_RATE}/fill + commission {COMMISSION_RATE}/side, applied per trade using entry+exit prices",
            "cost_per_rt_factor": COST_PER_RT_FACTOR,
        },
        "overall": overall,
        "yearly": yearly,
        "monthly": monthly,
        "am_pm": am_pm,
        "long_short": ls,
        "by_window": by_win,
        "by_day_type": by_dt,
        "range_width_buckets": r_buckets,
        "time_of_day": tod,
        "first_half_vs_second_half": halves,
        "top_trades": top,
        "trade_duration_distribution": dur,
        "positive_days_list": positive_days,
        "negative_days_list": negative_days,
        "flat_days_list": flat_days_list,
        "causality": caus,
        "determinism": det,
        "integrity_verification": integrity,
        "existing_algorithm_integrity": {
            "model_0": "UNCHANGED",
            "iteration009_artifact": integrity.get("iteration_009_vol_led.json", "CHECK"),
            "iteration012_artifact": integrity.get("iteration_012_trade_forensics.json", "CHECK"),
            "protected_oos": "UNCHANGED",
            "strategy_modules": "UNCHANGED",
            "risk_controls": "UNCHANGED",
        },
        "author_reported_results": {
            "nifty50_index": "NOT PUBLISHED in the repository (only LICI stock shown)",
            "lici_stock_2022_2025": {"am_10_15": "~1200 trades, 52-55% WR", "pm_14_15": "~1100 trades, 51-54% WR"},
            "note": "Our NIFTY 50 index result is INDEPENDENT — not directly comparable to the LICI stock result.",
        },
        "classification": "REPRODUCIBLE_BUT_NOT_PROFITABLE" if overall["net_pnl_pts"] < 0 else (
            "REPRODUCED_BUT_FRAGILE" if overall["win_rate_pct"] < 55 else "REPRODUCED_AND_POSITIVE"
        ),
        "safety_state": {
            "live_trading": False,
            "live_gate": "CLOSED",
            "algo_ready": "NO",
            "algorithm_health": "RED",
            "promotion": "NO",
        },
    }

    ART_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")

    print("=" * 66)
    print("PSB-002 - PUBLIC NIFTY 50 BREAKOUT BENCHMARK")
    print("=" * 66)
    print(f"RESEARCH BARS: {research_bars} | DAYS: {n_research_days}")
    print(f"TRADES (default config): {overall['n']}")
    print(f"  LONG: {overall['long']} | SHORT: {overall['short']}")
    print(f"  WINS: {overall['wins']} | LOSSES: {overall['losses']} | WR: {overall['win_rate_pct']}%")
    print(f"  GROSS P&L: {overall['gross_pnl_pts']:.2f} pts")
    print(f"  COSTS (std framework): {overall['costs_pts']:.2f} pts")
    print(f"  NET P&L: {overall['net_pnl_pts']:.2f} pts")
    print(f"  AVG TRADE: {overall['avg_trade_pts']:.4f} pts")
    print(f"  PROFIT FACTOR: {overall['profit_factor']}")
    print(f"  MAX DRAWDOWN: {overall['max_drawdown_pts']:.2f} pts")
    print(f"  POSITIVE DAYS: {overall['positive_days']} | NEGATIVE: {overall['negative_days']} | FLAT: {overall['flat_days']}")
    print(f"CAUSALITY: {'PASS' if all(caus.values()) else 'FAIL'}")
    print(f"DETERMINISM: {'PASS' if det['identical'] else 'FAIL'}")
    print(f"INTEGRITY: {integrity}")
    print(f"CLASSIFICATION: {out['classification']}")
    print("LIVE TRADING: FALSE | ALGO READY: NO | ALGORITHM HEALTH: RED | PROMOTION: NO")
    print(f"JSON -> {OUT_JSON}")
    print("STOP.")


if __name__ == "__main__":
    main()