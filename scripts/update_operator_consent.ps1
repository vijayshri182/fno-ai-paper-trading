# =====================================================================
# scripts\update_operator_consent.ps1
# Project  : C:\Vijay_GitHub\fno-ai-paper-trading
# Purpose   : OPERATOR-ONLY helper: refresh ONLY the sha256 fingerprint
#             field inside reports\execution\operator_consent.json.
#
# READ WHOLE FILE BEFORE RUNNING:
#   * MUST be run by a genuine human OPERATOR, interactively.
#   * Reads reports\execution\operator_consent.json and REQUIRES these
#     fields to already exist: operator, purpose, created_at,
#     expires_at, token_fingerprint_sha256.
#   * Prompts for the CURRENT Upstox access token using
#     Read-Host -AsSecureString. The token is decrypted ONLY inside
#     this process, hashed, then zeroed. It is NEVER:
#        - printed          - written to disk
#        - logged           - included in consent JSON
#        - included in git  - sent anywhere
#   * Writes back ONLY token_fingerprint_sha256; ALL other fields are
#     preserved byte-for-byte semantics (operator, purpose,
#     created_at, expires_at stay untouched, in that order).
#
# WHAT THIS SCRIPT DOES NOT DO (by design):
#   - Does NOT enable LIVE / Environment.LIVE.
#   - Does NOT touch the execution gate or its settings.
#   - Does NOT place, send, or simulate ANY order (paper or real).
#   - Does NOT create/modify operator consent itself (only the token
#     fingerprint inside an ALREADY-EXISTING consent record).
#   - Does NOT modify ANY file other than operator_consent.json.
#   - Does NOT git add / commit / push anything.
#   - Does NOT read any other secret, key, credential, or cookie.
#
# EXIT CODES:
#   0  fingerprint updated in operator_consent.json (nothing else changed)
#   1  refusal: consent JSON missing / field validation failed /
#      consent not in a valid editable state
#   2  token prompt cancelled / empty / hash or write failed
# =====================================================================

[CmdletBinding()]
param(
    [string]$ConsentJsonPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# $PSScriptRoot is NOT usable inside a param() default (it's still empty at
# binding time — that was the "empty string" crash). Resolve the default here
# in the body, where $PSScriptRoot is valid AND stays operator-path-locked.
if ([string]::IsNullOrWhiteSpace($ConsentJsonPath)) {
    $ConsentJsonPath = Join-Path $PSScriptRoot '..\reports\execution\operator_consent.json'
}

$requiredFields = @('operator', 'purpose', 'created_at', 'expires_at', 'token_fingerprint_sha256')

# ---------------------------------------------------------------- load
if (-not (Test-Path -LiteralPath $ConsentJsonPath)) {
    Write-Error "Consent JSON not found at: $ConsentJsonPath (refusing; nothing changed)."
    exit 1
}

$consent = $null
try {
    $consent = Get-Content -LiteralPath $ConsentJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    Write-Error "Consent JSON could not be parsed: $($_.Exception.Message) (refusing; nothing changed)."
    exit 1
}

# The file existed, but we still require a JSON object with the 5 fields.
$missing = @($requiredFields | Where-Object { -not ($consent.PSObject.Properties.Name -contains $_) })
if ($missing.Count -gt 0) {
    Write-Error "Consent JSON is missing required field(s): $($missing -join ', ') (refusing; nothing changed)."
    exit 1
}

# Never allow a fingerprint refresh against consent that has no valid expiry.
if (-not $consent.expires_at) {
    Write-Error "Consent JSON has no expires_at (refusing; nothing changed)."
    exit 1
}

# ----------------------------------------------------------- secure token
Write-Host ''
Write-Host 'Token fingerprint refresh for operator consent.' -ForegroundColor Cyan
Write-Host 'This prompts for the CURRENT Upstox access token. The token is' -ForegroundColor Cyan
Write-Host 'used only to compute a SHA-256 fingerprint in memory, then zeroed.' -ForegroundColor Cyan
Write-Host 'It is never printed, stored, logged, or sent anywhere.' -ForegroundColor Cyan
Write-Host ''

$secureToken = $null
try {
    $secureToken = Read-Host -Prompt 'Paste the CURRENT Upstox access token (press Enter only after pasting)' -AsSecureString
}
catch {
    Write-Error "Secure token prompt failed: $($_.Exception.Message) (refusing; nothing changed)."
    exit 2
}

if ($null -eq $secureToken -or $secureToken.Length -eq 0) {
    Write-Error 'No token was entered (refusing; nothing changed).'
    exit 2
}

# ------------------------------------------------------ hash (in memory)
$plainToken = $null
$bstr = [IntPtr]::Zero
$uid = $null
$sha = $null
$fingerprint = $null
try {
    # Decrypt SecureString to a BSTR on the native heap; we zero it after.
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    $plainToken = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    if ([string]::IsNullOrEmpty($plainToken)) {
        Write-Error 'Token could not be decrypted (refusing; nothing changed).'
        exit 2
    }

    $uid = [System.Text.Encoding]::UTF8.GetBytes($plainToken)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hashBytes = $sha.ComputeHash($uid)

    # Byte-to-hex WITHOUT [Convert]::ToHexString (unavailable on this host).
    $fingerprint = ([System.BitConverter]::ToString($hashBytes) -replace '-', '').ToLowerInvariant()
}
catch {
    Write-Error "Fingerprint computation failed: $($_.Exception.Message) (refusing; nothing changed)."
    exit 2
}
finally {
    if ($null -ne $sha)    { $sha.Dispose() }
    if ($null -ne $uid)    { [Array]::Clear($uid, 0, $uid.Length) }
    if ($null -ne $plainToken) { $plainToken = $null }
    if ($bstr -ne [IntPtr]::Zero) {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $bstr = [IntPtr]::Zero
    }
    if ($null -ne $secureToken) { $secureToken.Dispose() }
    if ($null -ne $hashBytes)   { [Array]::Clear($hashBytes, 0, $hashBytes.Length) }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

# ---------------------------------------------------------------- update
# Update ONLY token_fingerprint_sha256; preserve everything else and the
# original property order (operator, purpose, created_at, expires_at).
$consent.token_fingerprint_sha256 = $fingerprint

$outJson = $consent | ConvertTo-Json -Depth 10

# Write UTF-8 WITHOUT BOM so the file stays clean for git/CI.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Resolve-Path -LiteralPath $ConsentJsonPath).Path, $outJson, $utf8NoBom)

Write-Host ''
Write-Host ('Consent fingerprint updated. Path: {0}' -f (Resolve-Path -LiteralPath $ConsentJsonPath).Path)
Write-Host 'Fields preserved unchanged: operator, purpose, created_at, expires_at.'
Write-Host 'Nothing else was modified. No LIVE, no gate change, no order, no push.'
exit 0
