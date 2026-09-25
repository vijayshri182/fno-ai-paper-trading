# READ-ONLY monitor wrapper: TODAY Upstox 5m NIFTY 50 candle availability.
# Changes to the repo root, runs the monitor with the project .venv Python and
# preserves its exit code (0 = READY, 1 = NOT_READY, 2 = ERROR).
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath "C:\Vijay_GitHub\fno-ai-paper-trading"

& ".\.venv\Scripts\python.exe" "scripts\check_today_upstox_5m.py"
exit $LASTEXITCODE