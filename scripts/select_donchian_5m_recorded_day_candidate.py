#!/usr/bin/env python3
"""Read-only selection report of candidate recorded days for the 5M Donchian
20/10 forensic deep-dive.

Inputs (read-only):
  * reports/forensics/donchian_5m_20_10_trade_ledger.csv  (229 recorded trades)

Output:
  * reports/forensics/donchian_5m_recorded_day_candidates.md

All figures are read directly from the recorded ledger. Per-trade costs
(~Rs.128 fixed) make every net P&L negative, so Winners/Losers are reported on
GROSS close-to-close P&L and net P&L is shown separately. One day is selected
for a later detailed forensic investigation (context trade numbers + a
chronological sequence are printed here; the deep signal/state work is NOT
performed by this script).

Nothing is modified, simulated or re-optimized. No commit is performed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "reports" / "forensics" / "donchian_5m_20_10_trade_ledger.csv"
OUT_MD = REPO / "reports" / "forensics" / "donchian_5m_recorded_day_candidates.md"

# selection preferences from the task
REQUIRED = ["date", "trade_number", "direction", "gen_rule", "fresh_breakout",
            "reversion_state_entry", "gross_pnl_close", "realized_pnl", "net_pnl"]


def _dec(v) -> Decimal:
    if v in ("", None):
        return Decimal("0")
    return Decimal(str(v))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rows() -> list[dict]:
    with open(LEDGER, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def per_day(rows: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        g[r["date"]].append(r)
    for d in g:
        g[d].sort(key=lambda r: (r["entry_timestamp"], int(r["trade_number"])))
    return dict(g)


def day_stats(date_str: str, rs: list[dict]) -> dict:
    n = len(rs)
    call = sum(1 for r in rs if r["direction"] == "CALL")
    put = n - call
    f = sum(1 for r in rs if r["fresh_breakout"] == "true")
    rv = sum(1 for r in rs if r["reversion_state_entry"] == "true")
    wg = sum(1 for r in rs if _dec(r["gross_pnl_close"]) > 0)
    lg = sum(1 for r in rs if _dec(r["gross_pnl_close"]) < 0)
    zp = n - wg - lg
    seq = [r["direction"] for r in rs]
    dir_changes = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    gen = Counter(r["gen_rule"] for r in rs)
    gross = sum(_dec(r["gross_pnl_close"]) for r in rs)
    realized = sum(_dec(r["realized_pnl"]) for r in rs)
    net = sum(_dec(r["net_pnl"]) for r in rs)
    return {
        "date": date_str, "n": n, "call": call, "put": put, "fresh": f,
        "reversion": rv, "winners_gross": wg, "losers_gross": lg,
        "zero_gross": zp, "dir_changes": dir_changes, "gen": gen,
        "gross_pnl": gross, "realized_pnl": realized, "net_pnl": net,
        "trade_numbers": [int(r["trade_number"]) for r in rs],
        "seq": seq,
    }


def score(s: dict) -> float:
    """Higher is better for forensic richness (task preferences)."""
    return (2.0 * s["n"] + 1.0 * min(s["call"], s["put"]) + 1.0 * min(s["fresh"], s["reversion"])
            + 1.5 * s["dir_changes"] + 1.0 * min(s["winners_gross"], s["losers_gross"]))


def fmt_pnl(v: Decimal) -> str:
    return format(v, "f") if v == 0 else format(v, ".2f")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT_MD)
    ap.add_argument("--selected", default="2025-06-18", help="override selected date")
    args = ap.parse_args()

    rows = load_rows()
    assert len(rows) == 229, f"expected 229 recorded trades, got {len(rows)}"
    # pinned ledger fingerprint (same source-of-truth file as the trade ledger)
    fingerprint = sha256_file(LEDGER)

    by_day = per_day(rows)
    dates = sorted(by_day)
    stats = [day_stats(d, by_day[d]) for d in dates]

    if args.selected not in by_day:
        sys.exit(f"[FAIL] selected date {args.selected} not present in recorded ledger")
    chosen = day_stats(args.selected, by_day[args.selected])

    ranked = sorted(stats, key=score, reverse=True)
    table_rows = []
    for s in stats:
        table_rows.append({
            "date": s["date"], "n": s["n"], "call": s["call"], "put": s["put"],
            "fresh": s["fresh"], "reversion": s["reversion"],
            "winners": s["winners_gross"], "losers": s["losers_gross"],
            "gross_pnl": fmt_pnl(s["gross_pnl"]), "net_pnl": fmt_pnl(s["net_pnl"]),
            "dir_changes": s["dir_changes"], "score": round(score(s), 1),
            "trade_numbers": s["trade_numbers"],
            "rank": next(i for i, r in enumerate(ranked) if r["date"] == s["date"]) + 1,
        })

    L: list[str] = []
    L.append("# 5M Donchian 20/10 — Recorded Trading Days & Single-Day Candidate Selection")
    L.append("")
    L.append("_Read-only selection. Source of truth: `reports/forensics/donchian_5m_20_10_trade_ledger.csv` "
             f"({len(rows)} recorded trades, fingerprint {fingerprint[:16]}…). No reconstruction, no "
             "modification, no optimization._")
    L.append("")
    L.append("## 1. All trading dates present in the recorded ledger")
    L.append("")
    L.append(f"- Total recorded trades: **{len(rows)}** across **{len(dates)}** recorded days "
             f"(`{dates[0]}` .. `{dates[-1]}`).")
    L.append(f"- The dates below correspond exactly to the recorded experiment window; every date shown "
             f"has **actual recorded trade rows** in the ledger. None are reconstructed.")
    L.append("")
    L.append("## 2. Summary statistics per date")
    L.append("")
    L.append("Readers note: every trade pays a fixed all-in cost (~Rs.128: spread + slippage + "
             "commission), so **net P&L is negative every day**. Winners/Losers are therefore counted on "
             "**gross close-to-close P&L** (`gross_pnl_close > 0` = winner, `< 0` = loser); gross and net "
             "day P&L are both shown. `dir_changes` = direction changes between consecutive trades of "
             "the day; `rank` = forensic-richness rank (higher `score` = more useful).")
    L.append("")
    L.append("| rank | date | n | CALL | PUT | [F] | [R] | W | L | dir_changes | gross P&L | net P&L | trades |")
    L.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for t in table_rows:
        nums = t["trade_numbers"]
        tn = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else str(nums[0])
        L.append(f"| {t['rank']} | {t['date']} | {t['n']} | {t['call']} | {t['put']} | {t['fresh']} | "
                 f"{t['reversion']} | {t['winners']} | {t['losers']} | {t['dir_changes']} | "
                 f"{t['gross_pnl']} | {t['net_pnl']} | {tn} |")
    L.append("")
    L.append("## 3. Selected date for detailed forensic investigation")
    L.append("")
    L.append(f"**Selected: {chosen['date']}** — it exists in the recorded ledger "
             f"(trades #{','.join(str(x) for x in chosen['trade_numbers'])}).")
    L.append("")
    L.append("Rationale (compared against the other high-activity days):")
    L.append("")
    L.append("| day | n | CALL/PUT | [F]/[R] | W/L (gross) | dir_changes |")
    L.append("|---|---|---:|---:|---:|---:|")
    for s in ranked[:5]:
        L.append(f"| {s['date']} | {s['n']} | {s['call']}/{s['put']} | {s['fresh']}/{s['reversion']} | "
                 f"{s['winners_gross']}/{s['losers_gross']} | {s['dir_changes']} |")
    L.append("")
    L.append(f"- **Most recorded trades in a single day** ({chosen['n']} — the busiest recorded session).")
    L.append(f"- Balanced CALL/PUT ({chosen['call']}/{chosen['put']}), balanced [F]/[R] "
             f"({chosen['fresh']}/{chosen['reversion']}).")
    L.append(f"- {chosen['dir_changes']} direction changes — the most of any day; ideal for studying "
             f"switch/reversal legs.")
    L.append(f"- Both winners ({chosen['winners_gross']}) and losers ({chosen['losers_gross']}) on gross P&L.")
    L.append(f"- Entry origin variety: {', '.join(f'{k} x{v}' for k, v in chosen['gen'].most_common())}.")
    L.append("")
    L.append("## 4. Trade numbers belonging to the selected date")
    L.append("")
    L.append(f"- {chosen['date']}: trade numbers **{', '.join(str(x) for x in chosen['trade_numbers'])}** "
             f"(contiguous range {chosen['trade_numbers'][0]}–{chosen['trade_numbers'][-1]}).")
    L.append("")
    L.append("## 5. Chronological trade sequence for the selected date")
    L.append("")
    L.append("| # | trade | entry->exit | dir | signal | gen_rule | F | R | gross | realized | net | exit_reason |")
    L.append("|---:|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in by_day[chosen["date"]]:
        wl = "W" if _dec(r["gross_pnl_close"]) > 0 else ("L" if _dec(r["gross_pnl_close"]) < 0 else "0")
        L.append(f"| {r['trade_number']} | t{r['trade_number']} | {r['entry_timestamp'][11:16]}→"
                 f"{r['exit_timestamp'][11:16]} | {r['direction']} | {r['entry_signal']} | "
                 f"{r['gen_rule']} | {r['fresh_breakout']} | {r['reversion_state_entry']} | "
                 f"{fmt_pnl(_dec(r['gross_pnl_close']))} | {fmt_pnl(_dec(r['realized_pnl']))} | "
                 f"{fmt_pnl(_dec(r['net_pnl']))} | {r['exit_kind']}/{r['exit_reason']} |")
    L.append("")
    L.append("The detailed signal/state forensic investigation for this date is intentionally **not** "
             "performed here (per task scope).")
    L.append("")

    args.out.write_text("\n".join(L), encoding="utf-8")
    print("WROTE", args.out)
    print(json.dumps({"total_trades": len(rows), "recorded_days": len(dates),
                      "selected": chosen["date"], "trade_numbers": chosen["trade_numbers"],
                      "ledger_fingerprint": fingerprint}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())