#!/usr/bin/env python3
"""Validate the generated 5M Donchian 20/10 forensic package end-to-end.

Reads only the generated artifacts under <repo>/reports/forensics:
  * donchian_5m_20_10_trade_ledger.json
  * donchian_5m_20_10_trade_ledger.csv
  * donchian_5m_20_10_trade_forensics.md
  * donchian_5m_20_10_trade_forensics.html

and checks, strictly (exit code != 0 on any failure):

1. provenance identity   - meta claims the expected 5M experiment/fingerprint
2. structural            - 1 CSV row per ledger trade; header/row column count
3. reconciliation        - every known aggregate equals its recorded value,
                            per-trade P&L identity holds exactly (Decimals)
4. forward-horizon       - +5/..+30 moves consistent with closes and direction
5. MFE/MAE               - bounds are order-consistent (no look-ahead, held bars)
6. exit classification   - exit-reason totals equal the recorded distribution
7. aggregation           - section counters sum to the ledger totals
8. determinism           - canonical hash over the ledger reproduces output_hash
                           (generated_at excluded)

Nothing is modified. Pure read-only verification.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "reports" / "forensics"
LEDGER_JSON = OUT / "donchian_5m_20_10_trade_ledger.json"
LEDGER_CSV = OUT / "donchian_5m_20_10_trade_ledger.csv"
REPORT_MD = OUT / "donchian_5m_20_10_trade_forensics.md"
REPORT_HTML = OUT / "donchian_5m_20_10_trade_forensics.html"

EXPECTED = {
    "experiment_id": "5M_DIRECTIONAL_OPTIONS_EXPERIMENT",
    "fingerprint_prefix": "b1be2188",
    "trades": 229,
    "gross_close_total": Decimal("-28.80"),
    "slippage_total": Decimal("11667.50900"),
    "commission_total": Decimal("3500.252708640"),
    "realized_total": Decimal("-11696.30900"),
    "net_total": Decimal("-15196.561708640"),
    "fresh_breakout": 105,
    "reversion_state": 124,
    "neutral_exits": 225,
    "eod_exits": 3,
    "reversal_exits": 1,
    "stop_exits": 0,
    "winners_gross": 2,
    "holders_5m": 210,
    "holders_10m": 19,
    "max_consecutive_losses": 111,
}

D = Decimal


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def check(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def dec(v) -> Decimal | None:
    if v is None or (isinstance(v, str) and v == ""):
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def main() -> int:
    for p in (LEDGER_JSON, LEDGER_CSV, REPORT_MD, REPORT_HTML):
        if not p.exists():
            fail(f"missing artifact: {p}")

    d = json.loads(LEDGER_JSON.read_text(encoding="utf-8"))
    meta, summary, sections, trades = d["_meta"], d["summary"], d["sections"], d["trades"]

    # ---- 1. provenance identity ----
    check(meta["experiment_id"] == EXPECTED["experiment_id"], "meta experiment_id")
    check(meta["fingerprint"].startswith(EXPECTED["fingerprint_prefix"]),
          f"fingerprint prefix {meta['fingerprint']}")
    check(meta["runA_equal_runB"] is True, "runA fingerprint != runB")
    check(meta.get("runA_sha_matches_stored") is True, "runA sha != stored sha256")
    check(meta.get("runB_sha_matches_stored") is True, "runB sha != stored sha256")
    check(meta["window"]["bars"] == 4575, f"bars {meta['window']['bars']}")
    check(meta["window"]["decision_points"] == 4392, "decision points")
    check(meta["trades"] == EXPECTED["trades"], f"meta trades {meta['trades']}")

    # ---- 2. reconciliation of known aggregates ----
    pairs = [
        ("gross_close_total", summary["gross_close_total"]),
        ("slippage_total", summary["slippage_total"]),
        ("commission_total", summary["commission_total"]),
        ("realized_total", summary["realized_total"]),
        ("net_total", summary["net_total"]),
    ]
    for name, val in pairs:
        check(dec(val) == EXPECTED[name], f"summary {name}: {val} != {EXPECTED[name]}")

    net = dec(summary["net_total"])
    realized = dec(summary["realized_total"])
    comm = dec(summary["commission_total"])
    slip = dec(summary["slippage_total"])
    gross = dec(summary["gross_close_total"])
    check(net == realized - comm, "net != realized - commission (summary)")
    check(net == gross - slip - comm, "net != gross - slip - comm (summary)")
    check(int(summary["trades"]) == EXPECTED["trades"], "summary trades")
    check(int(summary["fresh_breakout_count"]) == EXPECTED["fresh_breakout"], "fresh count")
    check(int(summary["reversion_state_count"]) == EXPECTED["reversion_state"], "reversion count")
    check(int(summary["neutral_exit_count"]) == EXPECTED["neutral_exits"], "NEUTRAL exits")
    check(int(summary["eod_exit_count"]) == EXPECTED["eod_exits"], "EOD exits")
    check(int(summary["reversal_exit_count"]) == EXPECTED["reversal_exits"], "REVERSAL exits")
    check(int(summary["stop_exit_count"]) == EXPECTED["stop_exits"], "STOP exits")
    check(int(summary["winners_gross_fill"]) == EXPECTED["winners_gross"], "gross winners")
    check(int(summary["holding_5m_count"]) == EXPECTED["holders_5m"], "5m holds")
    check(int(summary["holding_10m_count"]) == EXPECTED["holders_10m"], "10m holds")
    check(int(summary["max_consecutive_losses"]) == EXPECTED["max_consecutive_losses"], "111-run")

    # ---- 3. per-trade structural invariants + P&L identity ----
    check(len(trades) == EXPECTED["trades"], f"trades rows {len(trades)}")
    ids = [t["entry_fill_order_id"] for t in trades]
    check(len(set(ids)) == len(ids), "duplicate entry order ids")
    num_to_id = {}
    tot_net = tot_gross = tot_slip = tot_comm = tot_real = D(0)
    for t in trades:
        num = t["_trade_number"]
        check(num_to_id.setdefault(num, t["entry_fill_order_id"]) == t["entry_fill_order_id"],
              f"trade_number collision at {num}")
        check(t["entered_at"] < t["exited_at"], f"exit before entry {t['entered_at']}")
        check(bool(t["entered_at"]) and bool(t["exited_at"]), f"missing timestamps {t['entered_at']}")
        check(t["leg"] in ("CALL", "PUT"), f"bad leg {t['leg']} @ {t['entered_at']}")
        check(1 <= t["quantity"] <= 2, f"bad qty {t['quantity']} @ {t['entered_at']}")
        g, s, sp, c, r, n = (dec(t[k]) for k in
                             ("gross_pnl_close", "slippage_cost", "spread_cost",
                              "commission_cost", "realized_pnl", "net_pnl"))
        check(g is not None and s is not None and c is not None and r is not None and n is not None,
              f"missing P&L fields {t['entered_at']}")
        check(sp == D(0), f"spread_cost nonzero @ {t['entered_at']}")
        check(n == r - c, f"per-trade realized-commission {t['entered_at']}")
        check(n == g - s - sp - c, f"per-trade full identity {t['entered_at']}")
        check(dec(t["recorded_realized"]) == r, f"recorded realized @ {t['entered_at']}")
        tot_net += n
        tot_gross += g
        tot_slip += s
        tot_comm += c
        tot_real += r
    check(tot_net == EXPECTED["net_total"], f"Σ net {tot_net}")
    check(tot_gross == EXPECTED["gross_close_total"], f"Σ gross-close {tot_gross}")
    check(tot_slip == EXPECTED["slippage_total"], f"Σ slippage {tot_slip}")
    check(tot_comm == EXPECTED["commission_total"], f"Σ commission {tot_comm}")
    check(tot_real == EXPECTED["realized_total"], f"Σ realized {tot_real}")

    # ---- 4. forward-horizon consistency (no look-ahead within a session) ----
    for t in trades:
        ec = dec(t["entry_close"])
        for h in ("5", "10", "15", "20", "25", "30"):
            f = t["forward"].get(h) or {}
            if f.get("close") is None:
                continue
            fc = dec(f["close"])
            sign = D(1) if t["leg"] == "CALL" else D(-1)
            rec_move_close = dec(f.get("move_close"))
            check(rec_move_close == (fc - ec) * sign,
                  f"{h}m move_close mismatch {t['entered_at']}")
            rec_correct = bool(f.get("correct_close"))
            check(rec_correct == (rec_move_close is not None and rec_move_close > 0),
                  f"correct_close mismatch {t['entered_at']}")
            if f.get("realized_if_exit_here") is not None:
                check(dec(f["realized_if_exit_here"]) == (fc - dec(t["entry_price"])) * sign * t["quantity"],
                      f"realized_if_exit_here @ {t['entered_at']}")

    # ---- 5. MFE/MAE window sanity (signed excursions; negative = never
    # favorable/adverse beyond the entry price, per the module contract) ----
    for t in trades:
        check(dec(t["mfe"]) is not None and dec(t["mae"]) is not None,
              f"missing mfe/mae {t['entered_at']}")
        check(dec(t["mfe_close"]) is not None and dec(t["mae_close"]) is not None,
              f"missing mfe/mae close {t['entered_at']}")
        check(t["n_held_bars"] == t["holding_minutes"] // 5,
              f"held bars vs minutes @ {t['entered_at']}")

    # ---- 6. exit classification totals (from the trades, independent path) ----
    reasons = Counter(t["exit_reason"] for t in trades)
    kinds = Counter(t["exit_kind"] for t in trades)
    check(reasons["NEUTRAL"] == EXPECTED["neutral_exits"], f"NEUTRAL recount {reasons}")
    check(reasons["EOD"] == EXPECTED["eod_exits"], f"EOD recount {reasons}")
    check(reasons["REVERSAL"] == EXPECTED["reversal_exits"], f"REVERSAL recount {reasons}")
    check(kinds["signal"] == 226 and kinds["EOD_FLATTEN"] == 3, f"closure kinds {kinds}")

    # ---- 7. aggregation sections match ledger totals ----
    eo = sections["entry_origin"]
    check(eo["fresh_breakout_count"] == EXPECTED["fresh_breakout"], "section fresh")
    check(eo["reversion_state_count"] == EXPECTED["reversion_state"], "section reversion")
    check(eo["fresh"]["count"] + eo["reversion"]["count"] == len(trades),
          "section fresh+reversion != trades")
    for rule in ("long_breakout", "short_breakout", "long_exit", "short_exit"):
        check(rule in eo["by_rule"], f"missing rule {rule}")
    holders = dict(Counter(t["holding_minutes"] for t in trades))
    ht = sections["holding_time"]
    check(sum(v["count"] for v in ht.values()) == len(trades), "holding buckets != trades")
    check(holders.get(5, 0) == ht.get("5", {}).get("count", 0), "bucket 5 mismatch")

    # ---- 8. determinism: canonical hash must reproduce output_hash ----
    from fno_ai_paper_trading.research.donchian_5m_forensics import canonical_hash
    canon = canonical_hash({
        "meta": {k: v for k, v in meta.items() if k not in ("generated_at", "output_hash")},
        "summary": summary,
        "trades": trades,
    })
    check(canon == meta["output_hash"], f"determinism: recomputed {canon} != {meta['output_hash']}")

    # ---- 9. CSV binding ----
    with open(LEDGER_CSV, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    check(len(rows) == EXPECTED["trades"] + 1, f"csv rows {len(rows)}")
    header = rows[0]
    check(len(header) == len(set(header)), "csv duplicate columns")
    csv_by_id = {}
    for r in rows[1:]:
        csv_by_id[r[1]] = r
    check(len(csv_by_id) == len(trades), "csv trade count mismatch")
    for t in trades:
        row = csv_by_id[t["entry_fill_order_id"]]
        check(row[4] == t["entered_at"], f"csv entry ts {t['entered_at']}")
        check(row[5] == t["exited_at"], f"csv exit ts {t['entered_at']}")

    # ---- 10. MD / HTML ping ----
    md = REPORT_MD.read_text(encoding="utf-8")
    html = REPORT_HTML.read_text(encoding="utf-8")
    for tag in ("## 1. Executive summary", "## 13. Research conclusion",
                f"Individual trade analysis ({len(trades)}/{len(trades)})"):
        check(tag in md, f"md missing: {tag}")
    check("<table>" in html and "</html>" in html, "html structure")
    check(html.count("<details>") == len(trades), "html details count")

    print("[OK] 5M Donchian 20/10 forensic package validated")
    print(f"   trades={len(trades)} net={summary['net_total']} "
          f"realized={summary['realized_total']} slip={summary['slippage_total']} "
          f"comm={summary['commission_total']} gross_close={summary['gross_close_total']}")
    print(f"   fresh={summary['fresh_breakout_count']} reversion={summary['reversion_state_count']} "
          f"neutral={summary['neutral_exit_count']} eod={summary['eod_exit_count']} "
          f"reversal={summary['reversal_exit_count']} 111run={summary['max_consecutive_losses']}")
    print(f"   output_hash={meta['output_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())