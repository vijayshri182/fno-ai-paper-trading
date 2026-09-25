#!/usr/bin/env bash
set -euo pipefail

PROJECT="/c/Vijay_GitHub/fno-ai-paper-trading"
CONSENT_FILE="$PROJECT/reports/execution/operator_consent.json"

if [[ ! -f "$CONSENT_FILE" ]]; then
echo "ERROR: Consent file not found: $CONSENT_FILE"
exit 1
fi

if ! command -v python >/dev/null 2>&1; then
echo "ERROR: Python is not available in PATH."
exit 1
fi

read -r -s -p "Upstox access token: " UPSTOX_TOKEN
echo

if [[ -z "$UPSTOX_TOKEN" ]]; then
echo "ERROR: Token cannot be empty."
unset UPSTOX_TOKEN
exit 1
fi

export CONSENT_FILE
export UPSTOX_TOKEN

python -c 'import hashlib,json,os; from pathlib import Path; p=Path(os.environ["CONSENT_FILE"]); t=os.environ["UPSTOX_TOKEN"]; c=json.loads(p.read_text(encoding="utf-8")); required=["operator","purpose","created_at","expires_at","token_fingerprint_sha256"]; missing=[x for x in required if x not in c]; raise SystemExit("ERROR: Missing required consent field(s): "+", ".join(missing)) if missing else None; c["token_fingerprint_sha256"]=hashlib.sha256(t.encode("utf-8")).hexdigest(); p.write_text(json.dumps(c,indent=2)+"\n",encoding="utf-8"); print("Consent token fingerprint updated successfully."); print("File:",p); print("Updated field: token_fingerprint_sha256"); print("Token and fingerprint were not printed."); print("Live enablement was NOT changed."); print("No order was placed.")'

unset UPSTOX_TOKEN
unset CONSENT_FILE

