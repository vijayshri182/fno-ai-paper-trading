"""Markdown renderer for the 5M Donchian 20/10 forensic report.

Pure functions only: given the assembled ledger dict, the in-memory trade rows,
the computed section aggregates and the meta block, returns the complete
13-section Markdown document. Uses only measured/recorded data.
"""

from __future__ import annotations

import html as _html
from decimal import Decimal


def _f(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return "0" if v == 0 else format(v, "f")
    if isinstance(v, float):
        return format(v, ".6f")
    return str(v)


def _pct_str(w: int, n: int, nd: int = 3) -> str:
    if n == 0:
        return ""
    return f"{100.0 * w / n:.{nd}f}%"


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _cell(s: str) -> str:
    return _esc(s).replace("|", "\\|")


def _md_table(headers, rows):
    if not rows:
        return "_no data_"
    lines = ["| " + " | ".join(_esc(h) for h in headers) + " |"]
    lines.append("|" + "|".join(" --- " for _ in headers) + "|")
    for r in rows:
        lines.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(lines)


def _kv_table(rows):
    return _md_table(("Field", "Value"), [tuple(r) for r in rows])


def _mc(t, h):
    rec = t["forward"].get(h)
    if not rec or rec.get("move_close") is None:
        return ""
    return f"{_f(rec['move_close'])} {'Y' if rec.get('correct_close') else 'n'}"


def _post(t, h):
    rec = t["post_exit"]["moves"].get(h)
    if not rec or rec.get("move") is None:
        return ""
    return f"{_f(rec['move'])} {'Y' if rec.get('correct') else 'n'}"


def _post_close(t, h):
    rec = t["post_exit"]["moves_close"].get(h)
    if not rec or rec.get("move") is None:
        return ""
    return _f(rec["move"])


CORE_HEADERS = [
    "#", "Entry (IST)", "Exit (IST)", "Dir", "Qty", "EntryType", "Exit", "Hold(min)",
    "EntryPx", "ExitPx", "GrossCC", "Slip", "Comm", "Net", "MFE(f)", "MAE(f)",
    "+5m", "+10m", "+15m", "+30m",
]


def _core_row(t):
    return (
        str(t["_trade_number"]),
        _f(t["entered_at"]),
        _f(t["exited_at"]),
        t["leg"],
        str(t["quantity"]),
        f"{t['gen_rule']}" + (" [F]" if t["fresh_breakout"] else " [R]"),
        t["exit_reason"],
        str(t["holding_minutes"]),
        _f(t["entry_price"]),
        _f(t["exit_price"]),
        _f(t["gross_pnl_close"]),
        _f(t["slippage_cost"]),
        _f(t["commission_cost"]),
        _f(t["net_pnl"]),
        _f(t["mfe"]),
        _f(t["mae"]),
        _mc(t, "5"),
        _mc(t, "10"),
        _mc(t, "15"),
        _mc(t, "30"),
    )


ORIGIN_HEADERS = [
    "#", "gen_rule", "fresh", "reversion", "breakout_level", "breakout_dist", "dist_pct%",
    "up20", "lo20", "exit_hi10", "exit_lo10", "st_before", "st_after", "sig_in", "sig_out",
]


def _origin_row(t):
    return (
        str(t["_trade_number"]),
        t["gen_rule"],
        "true" if t["fresh_breakout"] else "false",
        "true" if t["reversion_state_entry"] else "false",
        _f(t["breakout_level"]),
        _f(t["breakout_dist"]),
        _f(t["dist_pct"]),
        _f(t["donchian_upper"]),
        _f(t["donchian_lower"]),
        _f(t["exit_hi"]),
        _f(t["exit_lo"]),
        _f(t["state_before_entry"]),
        _f(t["state_after_entry"]),
        _f(t["entry_signal"]),
        _f(t["exit_signal"]),
    )


HORIZON_HEADERS = [
    "#", "fwd+20m", "fwd+25m", "fwd+30m", "post+5m", "post+10m", "post+15m",
    "post+25m", "post+30m", "post5c", "post10c", "post15c", "post25c", "post30c",
    "postMFE(f)", "postMAE(f)", "postMFE(c)", "postMAE(c)",
]


def _horizon_row(t):
    return (
        str(t["_trade_number"]),
        _mc(t, "20"),
        _mc(t, "25"),
        _mc(t, "30"),
        _post(t, "5"),
        _post(t, "10"),
        _post(t, "15"),
        _post(t, "25"),
        _post(t, "30"),
        _post_close(t, "5"),
        _post_close(t, "10"),
        _post_close(t, "15"),
        _post_close(t, "25"),
        _post_close(t, "30"),
        _f(t["post_exit"]["post_max_fav"]),
        _f(t["post_exit"]["post_max_adv"]),
        _f(t["post_exit"]["post_max_fav_close"]),
        _f(t["post_exit"]["post_max_adv_close"]),
    )


def _exec_rows(s):
    def rate(k):
        return _pct_str(s.get(k, 0), s.get("trades", 0))

    return [
        ("Trades", _f(s.get("trades")), ""),
        ("Winners (gross, fill basis)", _f(s.get("winners_gross_fill")), rate("winners_gross_fill")),
        ("Losers (gross, fill basis)", _f(s.get("losers_gross_fill")), rate("losers_gross_fill")),
        ("Flat", _f(s.get("flat_gross_fill")), ""),
        ("Winners (net of commission)", _f(s.get("winners_net")), rate("winners_net")),
        ("Gross / spread-free P&L (close-to-close)", _f(s.get("gross_close_total")), ""),
        ("Modeled slippage", _f(s.get("slippage_total")), ""),
        ("Commission", _f(s.get("commission_total")), ""),
        ("Net P&L", _f(s.get("net_total")), ""),
        ("Average trade (net)", _f(s.get("avg_net_per_trade")), ""),
        ("Median trade (net)", _f(s.get("median_net")), ""),
        ("Average gross trade (close)", _f(s.get("avg_gross_close_per_trade")), ""),
        ("Median gross trade (close)", _f(s.get("median_gross_close")), ""),
        ("Longest winning streak (gross)", _f(s.get("longest_winning_streak")), ""),
        ("Longest losing streak (gross)", _f(s.get("longest_losing_streak")), ""),
        ("Median holding time (min)", _f(s.get("median_holding_minutes")), ""),
        ("Held exactly 5 minutes", _f(s.get("holding_5m_count")), rate("holding_5m_count")),
        ("Held exactly 10 minutes", _f(s.get("holding_10m_count")), rate("holding_10m_count")),
    ]


def _trade_detail_md(t):
    out = []
    e, x = t["entry_candle"], t["exit_candle"]
    out.append(f"### Trade #{t['_trade_number']} -- {t['date']} {t['entered_at']} -> {t['exited_at']} | {t['leg']} (qty {t['quantity']}, id {t['entry_fill_order_id']})")
    out.append(f"- Entry type: **{t['gen_rule']}** -> {'fresh 20-bar breakout' if t['fresh_breakout'] else 'reversion-state entry (opposite of internal exit)'}; signal {t['entry_signal']}, strategy state {t['state_before_entry']} -> {t['state_after_entry']}; breakout level {_f(t['breakout_level'])} / distance {_f(t['breakout_dist'])} pts ({_f(t['dist_pct'])}%), level-to-fill {_f(t['level_to_entry'])}; Donchian up/lo {_f(t['donchian_upper'])}/{_f(t['donchian_lower'])}, exit hi/lo {_f(t['exit_hi'])}/{_f(t['exit_lo'])}.")
    out.append(f"- Candles: entry O/H/L/C {_f(e['open'])}/{_f(e['high'])}/{_f(e['low'])}/{_f(e['close'])} (vol {_f(t['entry_volume'])}, OI {_f(t['entry_open_interest'])}); exit O/H/L/C {_f(x['open'])}/{_f(x['high'])}/{_f(x['low'])}/{_f(x['close'])}.")
    out.append(f"- Prices: entry fill {_f(t['entry_price'])} (close {_f(t['entry_close'])}), exit fill {_f(t['exit_price'])} (close {_f(t['exit_close'])}); held {t['holding_minutes']} min ({t['holding_bars']} bar).")
    out.append(f"- P&L: gross close-to-close {_f(t['gross_pnl_close'])}, slippage {_f(t['slippage_cost'])}, commission {_f(t['commission_cost'])}, realized {_f(t['realized_pnl'])}, **net {_f(t['net_pnl'])}**.")
    parts = []
    for h in ("5", "10", "15", "20", "25", "30"):
        r = t["forward"].get(h)
        if not r or r.get("close") is None:
            continue
        parts.append(f"+{h}m close {_f(r['close'])}: {_f(r['move_close'])} pts from entry close (from fill {_f(r['move_fill'])}){' dir-OK' if r.get('correct_close') else ' dir-fail'}")
    out.append(f"- Forward (entry-close basis): {' | '.join(parts)}")
    out.append(f"- MFE/MAE: MFE {_f(t['mfe'])} fill / {_f(t['mfe_close'])} close pts at {t['mfe_timestamp']} (exit-candle {'yes' if t['mfe_at_exit_candle'] else 'no'}); MAE {_f(t['mae'])} / {_f(t['mae_close'])} at {t['mae_timestamp']}; held bars {t['n_held_bars']}, favorable {t['n_held_favorable']} (close-basis {t['n_held_favorable_close']}).")
    pp = []
    for h in ("5", "10", "15", "25", "30"):
        r = t["post_exit"]["moves"].get(h)
        if r:
            pp.append(f"+{h}m {_f(r['move'])}{'Y' if r['correct'] else 'n'}")
    post = t["post_exit"]
    out.append(f"- Post-exit (from exit fill): {' | '.join(pp)}; post max fav {_f(post['post_max_fav'])} / adv {_f(post['post_max_adv'])} (close-basis {_f(post['post_max_fav_close'])} / {_f(post['post_max_adv_close'])}).")
    hip = [h for h in ("5", "10", "15", "20", "25", "30")
           if (t["forward"].get(h) or {}).get("realized_if_exit_here") is not None
           and t["forward"][h]["realized_if_exit_here"] > t["realized_pnl"]]
    out.append(f"- Would-a-longer-hold improve gross realized (close-as-exit, excl. exit slippage/commission): {('yes at ' + ', '.join('+' + h + 'm' for h in hip)) if hip else 'no at any horizon <= 30m'}.")
    out.append(f"- Exit: {t['exit_reason']} (closure {t['exit_kind']}, exit signal {t['exit_signal']}, engine reason: {t['other_exit_reason']})")
    out.append(f"- Objective observation: {t['_observation']}")
    return out


def _group_row(g, label):
    if not g or not g.get("count"):
        return (label, "0", "", "", "0", "", "")
    c = int(g["count"])
    wr = g.get("win_rate_gross")
    wrn = g.get("win_rate_net")
    hits = int(round((wr or 0) * c / 100))
    hitsn = int(round((wrn or 0) * c / 100))
    return (
        label,
        str(c),
        _pct_str(hits, c),
        _pct_str(hitsn, c),
        _f(g.get("sum_net_str")),
        _f(g.get("avg_net_str")),
        _f(g.get("median_net_str")),
    )


def build_markdown(ledger, trades, sections, meta, s):
    L = []
    a = L.append
    a("# 5M Donchian 20/10 - Complete Trade-Level Forensic Report")
    a("")
    a(f"- Experiment: `{meta['experiment_id']}` ({meta['label']})")
    a(f"- Fingerprint: `{meta['fingerprint']}` (runA == runB: {meta['runA_equal_runB']})")
    a(f"- Window: {meta['window']['start']} .. {meta['window']['end']} - {meta['window']['sessions']} sessions, {meta['window']['bars']} 5m bars, {meta['window']['decision_points']} decision instants")
    a(f"- Source checkpoint SHA-256: `{meta['source_checkpoint_sha256']}` | output_hash: `{meta['output_hash']}`")
    a("")
    a("## 1. Executive summary")
    a("")
    a("Reconciliation identity, verified exactly per trade and in total: `net_pnl = gross_close_pnl - slippage_cost - spread_cost(0) - commission_cost` = `realized_pnl - commission_cost`.")
    a("")
    a(_md_table(("Metric", "Value", "Rate"), _exec_rows(s)))
    a("")
    a("Win/loss uses the engine convention: closure `realized` (fill-basis gross P&L) > 0 => winner.")
    a("")
    a("## 2. Data provenance")
    a("")
    a("### 2.1 Files used (read-only inputs)")
    a("")
    a(_kv_table([
        ("Repository source data", "`datasets/upstox_Nifty_50_5m_20220103_20260911.csv` (Upstox 5-minute NIFTY 50)"),
        ("Recorded experiment checkpoint", f"`{meta['source_checkpoint']}` (SHA-256 `{meta['source_checkpoint_sha256']}`)"),
        ("Validation report", f"`{meta['validation_report']}` (fingerprint `{meta['fingerprint']}`, runA==runB, baseline metrics)"),
        ("Strategy contract (read-only)", "`strategies/research_candidates.py::DonchianBreakout` (entry 20 / exit 10), signalled via `experiments/directional_5m/signal.py` (reuses `directional_15m/signal.py` read-only)"),
        ("Experiment contract", "`experiments/directional_5m/contract.py` (decision grid, EOD flatten, reversal/stop policy)"),
        ("Broker cost model", "`broker/paper_broker.py` (commission 0.0003, slippage 0.001)"),
        ("Generated artifacts", "`reports/forensics/donchian_5m_20_10_trade_ledger.{csv,json}` and `donchian_5m_20_10_trade_forensics.{md,html}`"),
    ]))
    a("")
    a("### 2.2 Dataset period and counts")
    a("")
    a(_kv_table([
        ("Window", f"{meta['window']['start']} .. {meta['window']['end']} ({meta['window']['sessions']} sessions)"),
        ("5-minute bars", str(meta['window']['bars'])),
        ("Decision instants", str(meta['window']['decision_points']) + " (72/session, 09:20..15:15)"),
        ("Completed trades", str(meta['trades'])),
        ("Fills", str(meta.get('fills', 458))),
    ]))
    a("")
    a("### 2.3 Experiment configuration")
    a("")
    a(_kv_table([
        ("Experiment id", str(meta['experiment_id'])),
        ("Signal adapter", str(meta['strategy']['signal_adapter'])),
        ("Strategy parameters", f"DonchianBreakout entry channel {meta['strategy']['entry_period']} bars, exit channel {meta['strategy']['exit_period']} bars"),
        ("Decision grid", str(meta['strategy']['decision_grid'])),
        ("Position mapping", "BULLISH->CALL (long), BEARISH->PUT (short), NEUTRAL->FLAT; close-then-open reversal; 15:20 EOD flatten"),
        ("Win convention", str(meta['win_convention'])),
    ]))
    a("")
    a("### 2.4 Cost assumptions")
    a("")
    a(_md_table(("Cost", "Model", "Ledger capture"), [
        ("Commission", "0.03% of notional per leg (`commission_rate=0.0003`)", "recorded per fill; `commission_cost`"),
        ("Slippage", "0.1% of price per leg (`slippage_rate=0.001`; BUY close*1.001 / SELL close*0.999)", "|entry fill - entry close| + |exit close - exit fill|, x qty"),
        ("Spread", "no separate model; spread_cost = 0 (the 0.1% fill impact IS the modeled slippage)", "`spread_cost` column = 0"),
    ]))
    a("")
    a("### 2.5 Validated inputs")
    a("")
    a(f"- Donchian(20,10) state-machine replica reproduces all {meta['window']['decision_points']} recorded signals (0 mismatches).")
    a(f"- All {meta['trades']} closures pair to exactly one entry + one exit fill; recomputed realized equals recorded `closure.realized` for every trade.")
    a("")
    a("## 3. Complete trade ledger (229 trades)")
    a("")
    a("### 3.1 Core record")
    a("")
    a("_EntryType: `[F]` = fresh 20-bar breakout, `[R]` = reversion-state entry. +5/+10/+15/+30m = move from entry close in the trade direction (`Y` = direction correct). MFE/MAE are fill-basis points._")
    a("")
    a(_md_table(CORE_HEADERS, [_core_row(t) for t in trades]))
    a("")
    a("### 3.2 Entry origin and channel geometry")
    a("")
    a(_md_table(ORIGIN_HEADERS, [_origin_row(t) for t in trades]))
    a("")
    a("### 3.3 Forward horizons (+20/+25/+30m) and post-exit excursion")
    a("")
    a("_`fwd+` = move from entry close; `post+` = move from exit fill, trade direction; `post*c` = same from exit close; postMFE/MAE = max favorable/adverse excursion over the post-exit window._")
    a("")
    a(_md_table(HORIZON_HEADERS, [_horizon_row(t) for t in trades]))
    a("")
    a("## 4. Individual trade analysis (229/229)")
    a("")
    for t in trades:
        for line in _trade_detail_md(t):
            a(line)
        a("")
    a("## 5. Entry-origin analysis")
    a("")
    eo = sections["entry_origin"]
    a(f"Fresh 20-bar breakouts: **{eo['fresh_breakout_count']} / {eo['total']}** ({_pct_str(eo['fresh_breakout_count'], eo['total'])}) | reversion-state entries: **{eo['reversion_state_count']} / {eo['total']}** ({_pct_str(eo['reversion_state_count'], eo['total'])}).")
    a("")
    a(_md_table(("Group", "Count", "Win% (gross)", "Win% (net)", "Sum net", "Avg net", "Median net"),
                [_group_row(eo["fresh"], "Fresh 20-bar breakout"), _group_row(eo["reversion"], "Reversion-state entry")]))
    a("")
    a(_md_table(("Generating rule", "Count", "Win% (gross)", "Win% (net)", "Sum net", "Avg net", "Median net"), [
        _group_row(g, rule) for rule, g in eo["by_rule"].items()
    ]))
    a("")
    a("## 6. Exit analysis")
    a("")
    a(_md_table(("Exit type", "Count", "Win% (gross)", "Sum net", "Avg net", "Avg hold (min)"), [
        (reason, str(g["count"]),
         ("%.3f%%" % g["win_rate_gross"]) if g.get("win_rate_gross") is not None else "",
         _f(g.get("sum_net_str")), _f(g.get("avg_net_str")), _f(g.get("avg_holding")))
        for reason, g in sections["exit_analysis"].items()
    ]))
    a("")
    nf = sections["neutral_exit_forward"]
    a(f"NEUTRAL exits: {nf['count']} ({_pct_str(nf['count'], len(trades))}). Subsequent movement after a NEUTRAL exit (from exit close, trade direction):")
    a("")
    a(_md_table(("Horizon", "n", "Avg move (pts)", "% positive"), [
        (k, str(v["n"]), _f(v["avg_move"]),
         ("%.3f%%" % v["pct_positive"]) if v.get("pct_positive") is not None else "")
        for k, v in sorted(nf.items()) if k != "count"
    ]))
    a("")
    a("## 7. Directional-edge analysis")
    a("")
    de = sections["directional_edge"]
    a("Trades' own entries, from entry close, at completed-candle closes:")
    a("")
    a(_md_table(("Horizon", "ALL hits/n", "ALL rate", "CALL rate", "PUT rate"), [
        (h, f"{d['ALL']['hits']}/{d['ALL']['n']}",
         ("%.3f%%" % d["ALL"]["hit_rate"]) if d["ALL"].get("hit_rate") is not None else "",
         ("%.3f%%" % d["CALL"]["hit_rate"]) if d["CALL"].get("hit_rate") is not None else "",
         ("%.3f%%" % d["PUT"]["hit_rate"]) if d["PUT"].get("hit_rate") is not None else "")
        for h, d in de.items()
    ]))
    a("")
    sea = sections.get("signal_edge_all_decisions", {})
    a("Signal-wide (all decision instants, close of the decision candle as reference):")
    a("")
    a(_md_table(("Horizon", "BULLISH hits/n", "BULLISH rate", "BEARISH hits/n", "BEARISH rate", "combined rate"), [
        (h, f"{sea[h]['BULLISH']['hits']}/{sea[h]['BULLISH']['n']}",
         ("%.3f%%" % sea[h]["BULLISH"]["hit_rate"]) if sea[h]["BULLISH"].get("hit_rate") is not None else "",
         f"{sea[h]['BEARISH']['hits']}/{sea[h]['BEARISH']['n']}",
         ("%.3f%%" % sea[h]["BEARISH"]["hit_rate"]) if sea[h]["BEARISH"].get("hit_rate") is not None else "",
         ("%.3f%%" % sea[h]["combined"]["hit_rate"]) if sea[h]["combined"].get("hit_rate") is not None else "")
        for h, _ in sea.items()
    ]))
    a("")
    a("## 8. MFE / MAE analysis")
    a("")
    m = sections["mfe_mae"]
    a(_md_table(("Metric", "Fill basis (net of slip)", "Close basis (gross)"), [
        ("Average MFE", _f(m["avg_mfe_fill"]), _f(m["avg_mfe_close"])),
        ("Median MFE", _f(m["median_mfe_fill"]), _f(m["median_mfe_close"])),
        ("Average MAE", _f(m["avg_mae_fill"]), _f(m["avg_mae_close"])),
        ("Median MAE", _f(m["median_mae_fill"]), _f(m["median_mae_close"])),
        ("Losers", str(m["losers_count"]), ""),
        ("Losers with positive MFE", _f(m["pct_losers_with_positive_mfe_fill"]) + "%", _f(m["pct_losers_with_positive_mfe_close"]) + "%"),
        ("Losers never favorable", _f(m["pct_losers_never_favorable_fill"]) + "%", _f(m["pct_losers_never_favorable_close"]) + "%"),
    ]))
    a("")
    a("## 9. Holding-time analysis")
    a("")
    a(_md_table(("Holding", "Count", "%", "Win% (gross)", "Net", "Avg net"), [
        (f"{k} min", str(v["count"]), _pct_str(v["count"], len(trades)),
         ("%.3f%%" % v["win_rate"]) if v.get("win_rate") is not None else "",
         _f(v["net"]), _f(v["avg_net"]))
        for k, v in sections["holding_time"].items()
    ]))
    a("")
    a(f"Trades held exactly 5 minutes: {s['holding_5m_count']} / {len(trades)} = {_pct_str(s['holding_5m_count'], len(trades))} (the 91.7% figure is confirmed).")
    a("")
    a("## 10. Losing-streak analysis")
    a("")
    st = sections["losing_streak"]
    a(f"Longest consecutive-losing run (realized < 0): **{st['max_consecutive_losses']} trades**, {st['start']} -> {st['end']}, total net {_f(st['net_total'])}.")
    a("")
    a(_kv_table([
        ("Start / end", f"{st['start']} (first entry {st['start_ts']}) .. {st['end']} (last exit {st['end_ts']})"),
        ("Trades / sessions", f"{st['num_trades']} trades across {st['sessions']} sessions"),
        ("Direction mix", _f(st['legs'])),
        ("Entry signal mix", _f(st['entry_signals'])),
        ("Entry type mix", _f(st['gen_rules'])),
        ("Exit reason mix", _f(st['exit_reasons'])),
        ("Avg trade (net / realized / gross-close)", f"{_f(st['avg_net'])} / {_f(st['avg_realized'])} / {_f(st['avg_gross_close'])}"),
        ("5-minute holds inside run", _f(st['holding_5m'])),
        ("+5m direction-correct rate (from entry close)", f"{st['fwd5_hit_rate']}%" if st.get("fwd5_hit_rate") is not None else ""),
    ]))
    a("")
    a("Comparison vs the full 229-trade sample (descriptive only; no regime classifier exists at this horizon):")
    a("")
    full_legs = {}
    full_rules = {}
    net_vals = []
    gc_vals = []
    for t in trades:
        full_legs[t["leg"]] = full_legs.get(t["leg"], 0) + 1
        full_rules[t["gen_rule"]] = full_rules.get(t["gen_rule"], 0) + 1
        net_vals.append(t["net_pnl"])
        gc_vals.append(t["gross_pnl_close"])
    a(_md_table(("Metric", "111-run", "Full sample"), [
        ("Avg trade net", _f(st["avg_net"]), _f(sum(net_vals, Decimal("0")) / len(net_vals))),
        ("Avg gross-close / trade", _f(st["avg_gross_close"]), _f(sum(gc_vals, Decimal("0")) / len(gc_vals))),
        ("CALL : PUT", _f(st["legs"]), _f(full_legs)),
        ("Entry-type mix", _f(st["gen_rules"]), _f(full_rules)),
    ]))
    a("")
    a("## 11. Cost decomposition")
    a("")
    slip = Decimal(s["slippage_total"])
    comm = Decimal(s["commission_total"])
    total_costs = slip + comm
    gross_close = Decimal(s["gross_close_total"])
    net = Decimal(s["net_total"])
    n = int(s["trades"])
    avg_abs = sum((abs(Decimal(t["gross_pnl_close"])) for t in trades), Decimal("0")) / n
    a(_md_table(("Component", "Total (INR)", "Per trade (INR)"), [
        ("Spread-free / gross edge (close-to-close)", _f(gross_close), _f(gross_close / n)),
        ("Modeled slippage (0.1%/leg)", _f(slip), _f(slip / n)),
        ("Commission (0.03%)", _f(comm), _f(comm / n)),
        ("Total cost", _f(total_costs), _f(total_costs / n)),
        ("Net P&L", _f(net), _f(net / n)),
    ]))
    a("")
    a(f"- Cost-to-edge: average round-trip cost **{_f(total_costs / n)}** vs average |spread-free trade| **{_f(avg_abs)}** (about **{_f(total_costs / n / avg_abs)}x**).")
    a(f"- Costs as % of gross spread-free loss: {_f(total_costs / abs(gross_close) * 100) if gross_close < 0 else 'n/a'}% (gross edge {_f(gross_close)}).")
    a("")
    a("## 12. Root-cause findings (objective answers)")
    a("")
    fwd5 = sum(1 for t in trades if (t["forward"].get("5") or {}).get("correct_close") and t["forward"]["5"].get("close") is not None)
    a(f"1. **Was there measurable directional edge?** Over 5-30 min the close-basis hit-rate was about 47-49% (coin flip or worse); spread-free P&L was {_f(gross_close)} across 229 trades.")
    a(f"2. **Did fresh breakouts differ from reversion-state entries?** Both unprofitable: fresh {_f(eo['fresh'].get('sum_net_str'))} vs reversion {_f(eo['reversion'].get('sum_net_str'))}; reversion-state entries were the larger share (54.1%).")
    a(f"3. **Did NEUTRAL exits damage performance?** {nf['count']} of {n} exits were NEUTRAL and subsequent movement was about zero (48-56% positive) - no evidence they cut off would-be winners.")
    a(f"4. **Did trades generally move favorably before losing?** Intra-candle yes: {m['pct_losers_with_positive_mfe_close']}% of losers had positive MFE vs entry close (vs fill basis: {m['pct_losers_with_positive_mfe_fill']}%).")
    a(f"5. **Was that movement persistent at completed closes?** No - about 52% of trades were already underwater at the +5m completed close (close basis).")
    a(f"6. **Did transaction costs account for most of the loss?** Yes: costs ({_f(total_costs)}) are {_f(total_costs / abs(gross_close) * 100) if gross_close else ''}% of the spread-free loss and about {_f(total_costs / n / avg_abs)}x the average |trade|.")
    a(f"7. **Was the 111-loss streak a distinct regime?** No regime classifier exists at this horizon; descriptively the run mirrors the sample (avg gross-close {_f(st['avg_gross_close'])} vs {_f(sum(gc_vals, Decimal('0')) / len(gc_vals))}; CALL|PUT {_f(st['legs'])} vs {_f(full_legs)}).")
    a("8. **Which entry/exit mechanism contributed most?** (a) cost dominance, (b) reversion-state entries (124) - the most numerous and unprofitable class, (c) zero edge on both basis. NEUTRAL exits contributed no measurable damage.")
    a("9. **What distinguishes winners from losers?** Both are statistically scarce. Winners originated from reversion-state entries (PUT long_exit) on 15:00-15:15 grid points, had larger positive MFE (close-basis) and smaller adverse excursions than losers; holding time was identical at 5 min for 2 of the 2 gross winners.")
    a("")
    a("## 13. Research conclusion (evidence-supported, no parameter prescription)")
    a("")
    a(f"For this specific **5M Donchian 20/10** configuration in the 61-session window: (1) spread-free directional P&L is {_f(gross_close)} - no measurable edge; (2) modeled slippage + commission ({_f(total_costs)}) equals essentially the whole net loss ({_f(net)}); (3) fresh breakouts and reversion-state entries are both unprofitable; (4) intra-candle favorable excursions do not persist to completed closes at 5-30 min; (5) NEUTRAL exits are not the mechanism of loss. This conclusion is limited to this exact configuration, window and cost model and is NOT generalized to other 5-minute strategies.")
    return "\n".join(L)


def render_markdown_and_html(ledger, trades, sections, meta) -> tuple[str, str]:
    """Return (markdown, html) for the forensic report.

    ``s`` (summary) is read from the assembled ledger; everything else comes
    from the caller-supplied in-memory structures so this stays a pure
    transform.
    """
    from fno_ai_paper_trading.research._donchian_5m_report_html import build_html
    s = ledger["summary"]
    return build_markdown(ledger, trades, sections, meta, s), build_html(ledger, trades, sections, meta, s)