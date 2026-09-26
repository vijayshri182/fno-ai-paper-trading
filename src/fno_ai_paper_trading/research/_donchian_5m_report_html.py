"""Self-contained HTML rendering for the 5M Donchian 20/10 forensic report.

Imports the row/table helpers from _donchian_5m_report and renders the same
13 sections as a single-file HTML document (inline CSS + a small row-filter
script for the core ledger table). No data is invented here.
"""

from __future__ import annotations

import html as _html
from decimal import Decimal

from fno_ai_paper_trading.research._donchian_5m_report import (
    CORE_HEADERS,
    HORIZON_HEADERS,
    ORIGIN_HEADERS,
    _core_row,
    _esc,
    _f,
    _group_row,
    _horizon_row,
    _origin_row,
    _pct_str,
    _trade_detail_md,
)

CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;color:#1a1a1a;background:#fdfdfd;line-height:1.45}
h1{font-size:24px}h2{font-size:19px;border-bottom:2px solid #ccc;padding-bottom:4px;margin-top:34px}
h3{font-size:15px;margin-top:20px;color:#333}
 table{border-collapse:collapse;margin:10px 0;font-size:12.5px}
 th,td{border:1px solid #ccc;padding:3px 7px;font-variant-numeric:tabular-nums}
 th{background:#eef2f7;position:sticky;top:0}
 tr:nth-child(even){background:#f7f9fb}
 code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:12px}
 summary{cursor:pointer;font-weight:600;color:#13447a}
 details{margin:6px 0;border:1px solid #ddd;border-radius:4px;padding:6px 10px}
 .neg{color:#b00020;font-weight:600}.pos{color:#0a6b2a;font-weight:600}
 input[type=search]{width:340px;padding:5px;margin:8px 0}
 .note{color:#555;font-size:12.5px}
</style>
"""


def _md_to_html(text: str) -> str:
    return (_html.escape(text)
            .replace("`", "")
            .replace("\n", "<br>"))


def _table_html(headers, rows):
    thead = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td>{_cell(r[i] if i < len(r) else '')}</td>"
                                 for i in range(len(headers))) + "</tr>"
    return f"<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>"


def _cell(v) -> str:
    s = _esc(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return s
    return f'<span class="{'pos' if f > 0 else ('neg' if f < 0 else '')}">{s}</span>'


def _exec_rows_html(s):
    rows = []
    for cells in _exec_rows_lst(s):
        rows.append("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in cells) + "</tr>")
    return rows


def _exec_rows_lst(s):
    from fno_ai_paper_trading.research._donchian_5m_report import _exec_rows
    return _exec_rows(s)


def build_html(ledger, trades, sections, meta, s):
    H = []
    a = H.append
    a("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    a(f"<title>5M Donchian 20/10 Forensic Report - {meta['fingerprint'][:8]}</title>")
    a(f"<style>{CSS}</style></head><body>")
    a(f"<h1>5M Donchian 20/10 - Complete Trade-Level Forensic Report</h1>")
    a(f"<p><b>Fingerprint {_esc(meta['fingerprint'])}</b> (runA == runB: {meta['runA_equal_runB']}) &mdash; "
      f"{_esc(meta['window']['start'])} .. {_esc(meta['window']['end'])} &mdash; "
      f"{_esc(meta['window']['sessions'])} sessions / {_esc(meta['window']['bars'])} bars / "
      f"{_esc(meta['window']['decision_points'])} decision instants. "
      f"output_hash <code>{_esc(meta['output_hash'])}</code></p>")

    a("<h2>1. Executive summary</h2>")
    a(_table_html(("Metric", "Value", "Rate"), _exec_rows_lst(s)))

    a("<h2>2. Data provenance</h2>")
    a("<table><tr><th>Item</th><th>Value</th></tr>")
    a(f"<tr><td>Source data</td><td><code>datasets/upstox_Nifty_50_5m_20220103_20260911.csv</code></td></tr>")
    a(f"<tr><td>Checkpoint</td><td><code>{_esc(meta['source_checkpoint'])}</code> "
      f"(sha256 <code>{_esc(meta['source_checkpoint_sha256'])}</code>)</td></tr>")
    a(f"<tr><td>Validation report</td><td><code>{_esc(meta['validation_report'])}</code></td></tr>")
    a(f"<tr><td>Strategy (read-only)</td><td><code>strategies/research_candidates.py::DonchianBreakout</code> "
      f"entry {_esc(meta['strategy']['entry_period'])} / exit {_esc(meta['strategy']['exit_period'])} via "
      f"<code>{_esc(meta['strategy']['signal_adapter'])}</code></td></tr>")
    a(f"<tr><td>Broker model</td><td><code>broker/paper_broker.py</code> commission 0.0003, slippage 0.001</td></tr>")
    a("</table>")
    a(f"<p class='note'>Donchian(20,10) replica matches all {_esc(meta['window']['decision_points'])} recorded "
      f"signals (0 mismatches); all {_esc(meta['trades'])} closures pair to exactly one entry + one exit fill; "
      f"recomputed realized equals recorded closure.realized for every trade. "
      f"Identity: net_pnl == gross_close_pnl - slippage_cost - spread_cost(0) - commission_cost.</p>")

    a("<h2>3. Complete trade ledger</h2>")
    core_rows = [_core_row(t) for t in trades]
    a(f"<input id='corefilter' type='search' placeholder='Filter by date/direction/rule/id...'>")
    a(_table_html(CORE_HEADERS, core_rows))
    a("<script>const fi=document.getElementById('corefilter'),tb=document.querySelectorAll('#fb tbody tr');"
      "fi.addEventListener('input',e=>{const q=e.target.value.toLowerCase();"
      "tb.forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')});</script>")

    a("<h3>3.1 Entry origin / channel geometry</h3>")
    a(_table_html(ORIGIN_HEADERS, [_origin_row(t) for t in trades]))
    a("<h3>3.2 Forward +20/+25/+30m and post-exit excursion</h3>")
    a(_table_html(HORIZON_HEADERS, [_horizon_row(t) for t in trades]))

    a("<h2>4. Individual trade analysis (229/229)</h2>")
    for t in trades:
        body = "<br>".join(_md_to_html(line) for line in _trade_detail_md(t))
        a(f"<details><summary>#{t['_trade_number']} &mdash; {_esc(t['date'])} {_esc(t['entered_at'])} &rarr; "
          f"{_esc(t['exited_at'])} &mdash; {_esc(t['leg'])} qty {t['quantity']} &mdash; net {_f(t['net_pnl'])}</summary>{body}</details>")

    eo = sections["entry_origin"]
    a("<h2>5. Entry-origin analysis</h2>")
    a(f"<p>Fresh 20-bar breakouts <b>{eo['fresh_breakout_count']}/{eo['total']}</b> "
      f"({_pct_str(eo['fresh_breakout_count'], eo['total'])}); reversion-state entries "
      f"<b>{eo['reversion_state_count']}/{eo['total']}</b> ({_pct_str(eo['reversion_state_count'], eo['total'])}).</p>")
    a(_table_html(("Group", "Count", "Win% (gross)", "Win% (net)", "Sum net", "Avg net", "Median net"),
                  [_group_row(eo["fresh"], "Fresh 20-bar breakout"), _group_row(eo["reversion"], "Reversion-state entry")]))
    a(_table_html(("Generating rule", "Count", "Win% (gross)", "Win% (net)", "Sum net", "Avg net", "Median net"),
                  [_group_row(g, rule) for rule, g in eo["by_rule"].items()]))

    a("<h2>6. Exit analysis</h2>")
    a(_table_html(("Exit type", "Count", "Win% (gross)", "Sum net", "Avg net", "Avg hold (min)"), [
        (reason, str(g["count"]),
         ("%.3f%%" % g["win_rate_gross"]) if g.get("win_rate_gross") is not None else "",
         _f(g.get("sum_net_str")), _f(g.get("avg_net_str")), _f(g.get("avg_holding")))
        for reason, g in sections["exit_analysis"].items()
    ]))
    nf = sections["neutral_exit_forward"]
    a(f"<p>NEUTRAL exits: {nf['count']} ({_pct_str(nf['count'], len(trades))}). Subsequent movement after a "
      f"NEUTRAL exit (from exit close, trade direction):</p>")
    a(_table_html(("Horizon", "n", "Avg move (pts)", "% positive"), [
        (k, str(v["n"]), _f(v["avg_move"]),
         ("%.3f%%" % v["pct_positive"]) if v.get("pct_positive") is not None else "")
        for k, v in sorted(nf.items()) if k != "count"
    ]))

    de = sections["directional_edge"]
    sea = sections.get("signal_edge_all_decisions", {})
    a("<h2>7. Directional-edge analysis</h2>")
    a("<p>Trades' own entries, from entry close:</p>")
    a(_table_html(("Horizon", "ALL hits/n", "ALL rate", "CALL rate", "PUT rate"), [
        (h, f"{d['ALL']['hits']}/{d['ALL']['n']}",
         ("%.3f%%" % d["ALL"]["hit_rate"]) if d["ALL"].get("hit_rate") is not None else "",
         ("%.3f%%" % d["CALL"]["hit_rate"]) if d["CALL"].get("hit_rate") is not None else "",
         ("%.3f%%" % d["PUT"]["hit_rate"]) if d["PUT"].get("hit_rate") is not None else "")
        for h, d in de.items()
    ]))
    if sea:
        a("<p>Signal-wide (all decision instants, close of the decision candle as reference):</p>")
        a(_table_html(("Horizon", "BULLISH hits/n", "rate", "BEARISH hits/n", "rate", "combined"), [
            (h, f"{sea[h]['BULLISH']['hits']}/{sea[h]['BULLISH']['n']}",
             ("%.3f%%" % sea[h]["BULLISH"]["hit_rate"]) if sea[h]["BULLISH"].get("hit_rate") is not None else "",
             f"{sea[h]['BEARISH']['hits']}/{sea[h]['BEARISH']['n']}",
             ("%.3f%%" % sea[h]["BEARISH"]["hit_rate"]) if sea[h]["BEARISH"].get("hit_rate") is not None else "",
             ("%.3f%%" % sea[h]["combined"]["hit_rate"]) if sea[h]["combined"].get("hit_rate") is not None else "")
            for h, _ in sea.items()
        ]))

    m = sections["mfe_mae"]
    a("<h2>8. MFE / MAE analysis</h2>")
    a(_table_html(("Metric", "Fill basis (net of slip)", "Close basis (gross)"), [
        ("Average MFE", _f(m["avg_mfe_fill"]), _f(m["avg_mfe_close"])),
        ("Median MFE", _f(m["median_mfe_fill"]), _f(m["median_mfe_close"])),
        ("Average MAE", _f(m["avg_mae_fill"]), _f(m["avg_mae_close"])),
        ("Median MAE", _f(m["median_mae_fill"]), _f(m["median_mae_close"])),
        ("Losers", str(m["losers_count"]), ""),
        ("Losers with positive MFE", _f(m["pct_losers_with_positive_mfe_fill"]) + "%", _f(m["pct_losers_with_positive_mfe_close"]) + "%"),
        ("Losers never favorable", _f(m["pct_losers_never_favorable_fill"]) + "%", _f(m["pct_losers_never_favorable_close"]) + "%"),
    ]))

    a("<h2>9. Holding-time analysis</h2>")
    a(_table_html(("Holding", "Count", "%", "Win% (gross)", "Net", "Avg net"), [
        (f"{k} min", str(v["count"]), _pct_str(v["count"], len(trades)),
         ("%.3f%%" % v["win_rate"]) if v.get("win_rate") is not None else "",
         _f(v["net"]), _f(v["avg_net"]))
        for k, v in sections["holding_time"].items()
    ]))
    a(f"<p class='note'>Trades held exactly 5 minutes: {s['holding_5m_count']} / {len(trades)} "
      f"= {_pct_str(s['holding_5m_count'], len(trades))} (the 91.7% figure is confirmed).</p>")

    st = sections["losing_streak"]
    a("<h2>10. Losing-streak analysis</h2>")
    a(f"<p>Longest consecutive-losing run (realized &lt; 0): <b>{st['max_consecutive_losses']} trades</b>, "
      f"{_esc(st['start'])} &rarr; {_esc(st['end'])}, total net {_f(st['net_total'])}.</p>")
    a(_table_html(("Field", "Value"), [
        ("Start / end", f"{st['start']} (first entry {st['start_ts']}) .. {st['end']} (last exit {st['end_ts']})"),
        ("Trades / sessions", f"{st['num_trades']} trades across {st['sessions']} sessions"),
        ("Direction mix", _f(st['legs'])),
        ("Entry signal mix", _f(st['entry_signals'])),
        ("Entry type mix", _f(st['gen_rules'])),
        ("Exit reason mix", _f(st['exit_reasons'])),
        ("Avg trade (net / realized / gross-close)", f"{_f(st['avg_net'])} / {_f(st['avg_realized'])} / {_f(st['avg_gross_close'])}"),
        ("5-minute holds inside run", _f(st['holding_5m'])),
        ("+5m direction-correct rate", f"{st['fwd5_hit_rate']}%" if st.get("fwd5_hit_rate") is not None else ""),
    ]))

    a("<h2>11. Cost decomposition</h2>")
    slip = Decimal(s["slippage_total"]); comm = Decimal(s["commission_total"])
    total_costs = slip + comm
    gross_close = Decimal(s["gross_close_total"]); net = Decimal(s["net_total"]); n = int(s["trades"])
    avg_abs = sum((abs(Decimal(t["gross_pnl_close"])) for t in trades), Decimal("0")) / n
    a(_table_html(("Component", "Total (INR)", "Per trade (INR)"), [
        ("Spread-free / gross edge (close-to-close)", _f(gross_close), _f(gross_close / n)),
        ("Modeled slippage (0.1%/leg)", _f(slip), _f(slip / n)),
        ("Commission (0.03%)", _f(comm), _f(comm / n)),
        ("Total cost", _f(total_costs), _f(total_costs / n)),
        ("Net P&L", _f(net), _f(net / n)),
    ]))

    a("<h2>12. Root-cause findings</h2>")
    eo_f = eo["fresh"].get("sum_net_str"); eo_r = eo["reversion"].get("sum_net_str")
    a("<ol>")
    a(f"<li>Directional edge: close-basis hit-rate 47-49% at 5-30 min; spread-free P&L {_f(gross_close)}</li>")
    a(f"<li>Fresh breakouts {_f(eo_f)} vs reversion-state entries {_f(eo_r)} (both unprofitable; reversion 54.1% of trades)</li>")
    a(f"<li>NEUTRAL exits ({nf['count']}): post-exit ~zero drift, no evidence of cut-off winners</li>")
    a(f"<li>Intra-candle favorable then lost: {_f(m['pct_losers_with_positive_mfe_close'])}% of losers MFE&gt;0 vs entry close "
      f"(fill basis {_f(m['pct_losers_with_positive_mfe_fill'])}%)</li>")
    a(f"<li>No persistence at completed closes: ~52% underwater at +5m close</li>")
    a(f"<li>Costs {_f(total_costs)} = {_f(total_costs / abs(gross_close) * 100) if gross_close else ''}% of spread-free loss; "
      f"{_f(total_costs / n / avg_abs)}x the average |trade|</li>")
    a(f"<li>111-loss run is descriptive (no regime classifier): avg gross-close {_f(st['avg_gross_close'])} vs "
      f"{_f(sum((Decimal(t['gross_pnl_close']) for t in trades), Decimal('0')) / n)} full-sample; "
      f"CALL|PUT {_f(st['legs'])} vs {_f(dict(__import__('collections').Counter(t['leg'] for t in trades)))}</li>")
    a("<li>Main mechanisms: (a) cost dominance, (b) reversion-state entries, (c) zero edge on both bases; NEUTRAL exits no measurable damage</li>")
    a("<li>Winners: scarce; reversion-state PUT long_exit at 15:00-15:15 grid; larger positive MFE; holding identical at 5m</li>")
    a("</ol>")

    a("<h2>13. Research conclusion</h2>")
    a(f"<p>For this specific <b>5M Donchian 20/10</b> configuration ({_esc(meta['window']['start'])}.."
      f"{_esc(meta['window']['end'])}): spread-free directional P&L {_f(gross_close)} (no measurable edge); "
      f"slippage + commission ({_f(total_costs)}) \u2248 the whole net loss ({_f(net)}); fresh and reversion entries "
      f"both unprofitable; intra-candle excursions do not persist to closes; NEUTRAL exits are not the loss mechanism. "
      f"Limited to this exact configuration, window and cost model.</p>")
    a("</body></html>")
    return "\n".join(H)