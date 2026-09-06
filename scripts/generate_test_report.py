"""Generate a detailed HTML test report by running pytest and parsing its JUnit XML.

Usage:
    python scripts/generate_test_report.py

Output:
    reports/test_report.html
"""
from __future__ import annotations

import html
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "reports" / "test_report.html"

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #f6f8fa; color: #1f2328; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
header { padding: 20px 24px; border-radius: 10px; background: linear-gradient(135deg, #0d1117, #1f2937); color: #fff; margin-bottom: 20px; }
header h1 { margin: 0; font-size: 22px; }
header p { margin: 6px 0 0; opacity: .85; font-size: 13px; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 24px; }
.card { flex: 1 1 140px; background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: 14px 18px; }
.card .num { font-size: 26px; font-weight: 700; }
.card .lbl { font-size: 12px; color: #57606a; text-transform: uppercase; letter-spacing: .04em; }
.card.ok .num { color: #1a7f37; }
.card.bad .num { color: #cf222e; }
.card.warn .num { color: #9a6700; }
.card.neutral .num { color: #0d1117; }
section { background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: 18px 20px; margin-bottom: 20px; }
section h2 { margin: 0 0 12px; font-size: 16px; border-bottom: 1px solid #eaeef2; padding-bottom: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #eaeef2; }
th { background: #f6f8fa; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: #57606a; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; color: #fff; }
.badge.passed { background: #1a7f37; }
.badge.failed, .badge.error { background: #cf222e; }
.badge.skipped { background: #9a6700; }
.filter { margin-bottom: 12px; font-size: 13px; }
.filter label { margin-right: 14px; cursor: pointer; }
details { margin-top: 8px; padding: 8px 12px; background: #fcfcfd; border: 1px solid #d0d7de; border-radius: 6px; }
details summary { cursor: pointer; font-weight: 600; color: #cf222e; }
details pre { white-space: pre-wrap; background: #0d1117; color: #e6edf3; padding: 12px; border-radius: 6px; font-size: 12px; }
.footer { font-size: 12px; color: #57606a; text-align: center; margin-top: 8px; }
.hidden { display: none; }
"""


def run_pytest() -> ET.Element:
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "junit.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--junitxml", str(xml_path), "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if not xml_path.exists():
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise RuntimeError("pytest did not produce a JUnit XML report")
        return ET.parse(xml_path).getroot()


def testsuite_time(root: ET.Element) -> float:
    for suite in root.iter("testsuite"):
        return float(suite.get("time", "0"))
    return 0.0


def build_html(root: ET.Element) -> str:
    suites = list(root.iter("testsuite"))
    total_tests = sum(int(s.get("tests", "0")) for s in suites)
    total_failures = sum(int(s.get("failures", "0")) for s in suites)
    total_errors = sum(int(s.get("errors", "0")) for s in suites)
    total_skipped = sum(int(s.get("skipped", "0")) for s in suites)
    total_passed = total_tests - total_failures - total_errors - total_skipped
    total_time = testsuite_time(root)
    timestamp = suites[0].get("timestamp") if suites else ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    py = platform.python_version()
    plat = platform.platform()

    rows: list[dict[str, str]] = []
    for suite in suites:
        module = suite.get("name", "").replace(".py", "")
        for case in suite.iter("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            time_ms = float(case.get("time", "0")) * 1000
            status = "passed"
            detail = ""
            for fail in case.iter("failure"):
                status = "error" if fail.get("type") == "Exception" else "failed"
                detail = fail.text or ""
            if case.find("error") is not None:
                status = "error"
                detail = (case.findtext("error") or "")
            if case.find("skipped") is not None:
                status = "skipped"
                detail = (case.findtext("skipped") or "")
            rows.append({
                "module": module,
                "classname": classname,
                "name": name,
                "status": status,
                "time": f"{time_ms:.0f}",
                "detail": detail.strip(),
                "display": f"{classname}::{name}" if classname else name,
            })

    module_rows = ""
    by_module: dict[str, dict[str, int]] = {}
    for r in rows:
        m = by_module.setdefault(r["module"], {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0})
        m["tests"] += 1
        m["time"] += float(r["time"])
        if r["status"] == "passed":
            m["passed"] += 1
        elif r["status"] == "failed" or r["status"] == "error":
            m["failed"] += 1
        else:
            m["skipped"] += 1
    for name, m in sorted(by_module.items()):
        badge = "passed" if m["failed"] == 0 else "failed"
        module_rows += (
            f"<tr><td><code>{html.escape(name)}</code></td>"
            f"<td class='num'>{m['tests']}</td>"
            f"<td class='num'>{m['passed']}</td>"
            f"<td class='num'>{m['failed']}</td>"
            f"<td class='num'>{m['skipped']}</td>"
            f"<td class='num'>{m['time']:.0f} ms</td>"
            f"<td><span class='badge {badge}'>{badge.upper()}</span></td></tr>"
        )

    detail_rows = ""
    for r in sorted(rows, key=lambda x: (x["module"], x["display"])):
        detail_block = ""
        if r["detail"]:
            detail_block = (
                f"<details><summary>Failure details</summary><pre>{html.escape(r['detail'])}</pre></details>"
            )
        test_cell = f"<code>{html.escape(r['display'])}</code>{detail_block}"
        detail_rows += (
            f"<tr class='row {r['status']}'>"
            f"<td><span class='badge {r['status']}'>{r['status'].upper()}</span></td>"
            f"<td>{test_cell}</td>"
            f"<td><code>{html.escape(r['module'])}</code></td>"
            f"<td class='num'>{r['time']} ms</td>"
            f"</tr>"
        )

    verdict = "PASS" if total_failures == 0 and total_errors == 0 else "FAIL"
    verdict_class = "ok" if verdict == "PASS" else "bad"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F&O AI Paper Trading - Test Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>F&O AI Paper Trading - Test Report</h1>
    <p>Phase 1 test suite &middot; generated {now} &middot; Python {py} ({plat})</p>
    <p>Timestamp: {html.escape(timestamp)} &middot; Total duration: {total_time:.2f}s</p>
  </header>

  <div class="cards">
    <div class="card ok"><div class="num">{total_tests}</div><div class="lbl">Total Tests</div></div>
    <div class="card ok"><div class="num">{total_passed}</div><div class="lbl">Passed</div></div>
    <div class="card bad"><div class="num">{total_failures + total_errors}</div><div class="lbl">Failed</div></div>
    <div class="card warn"><div class="num">{total_skipped}</div><div class="lbl">Skipped</div></div>
    <div class="card {verdict_class}"><div class="num">{verdict}</div><div class="lbl">Verdict</div></div>
  </div>

  <section>
    <h2>Summary by test module</h2>
    <table>
      <thead><tr><th>Module</th><th class='num'>Tests</th><th class='num'>Passed</th><th class='num'>Failed</th><th class='num'>Skipped</th><th class='num'>Time</th><th>Result</th></tr></thead>
      <tbody>{module_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>Detailed test results</h2>
    <div class="filter">
      <label><input type="radio" name="f" value="all" checked onchange="applyFilter()"> All</label>
      <label><input type="radio" name="f" value="passed" onchange="applyFilter()"> Passed</label>
      <label><input type="radio" name="f" value="failed" onchange="applyFilter()"> Failed</label>
      <label><input type="radio" name="f" value="skipped" onchange="applyFilter()"> Skipped</label>
    </div>
    <table>
      <thead><tr><th>Status</th><th>Test</th><th>Module</th><th class='num'>Time</th></tr></thead>
      <tbody>{detail_rows}</tbody>
    </table>
  </section>

  <div class="footer">Generated by scripts/generate_test_report.py &middot; paper-trading only &middot; no secret data included</div>
</div>
<script>
function applyFilter() {{
  const v = document.querySelector('input[name="f"]:checked').value;
  document.querySelectorAll('tr.row').forEach(tr => {{
    tr.classList.toggle('hidden', v !== 'all' && !tr.classList.contains(v));
  }});
}}
</script>
</body>
</html>"""


def main() -> None:
    root = run_pytest()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(root), encoding="utf-8")
    suites = list(root.iter("testsuite"))
    tests = sum(int(s.get("tests", "0")) for s in suites)
    failures = sum(int(s.get("failures", "0")) + int(s.get("errors", "0")) for s in suites)
    print(f"Report written: {OUTPUT}")
    print(f"Tests: {tests} | Failures: {failures} | Duration: {testsuite_time(root):.2f}s")


if __name__ == "__main__":
    main()