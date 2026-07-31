# Scheduled launcher — DAILY call->HotHawk load audit (weekday mornings).
# Runs audit_call_loads.py --mode day --discord: audits the previous business day's done calls
# against live HotHawk enrollment and posts the roster + any NOT-LOADED misses to the AISDR Discord.
$ErrorActionPreference = "Continue"
$scripts = Split-Path $PSScriptRoot -Parent          # ...\call-load-audit\scripts
$logDir  = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\audit_day_$(Get-Date -Format yyyyMMdd).log"
$py  = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }
"=== $(Get-Date -Format o) call-load-audit DAY START ===" | Out-File -Append -Encoding utf8 $log
& $py "$scripts\audit_call_loads.py" --mode day --discord 2>&1 | Out-File -Append -Encoding utf8 $log
$exitCode = $LASTEXITCODE
"=== $(Get-Date -Format o) call-load-audit DAY END (exit $exitCode) ===" | Out-File -Append -Encoding utf8 $log

# Alert on BOTH failure modes this audit has actually hit:
#   exit 2 = pipeline failure (done calls exist, zero reached HotHawk) — 2026-07-28..30
#   exit 1 = the audit itself died (2026-07-29 DNS blip: exit 1, nothing posted, silent day)
# Without this the only signal was a Discord report that never arrived.
if ($exitCode -ne 0) {
    $notify = "c:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin\go-capy-outreach\skills\ai-sdr-manager\scripts\notify_run_failure.py"
    if (Test-Path $notify) {
        $tail = if (Test-Path $log) { (Get-Content $log -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { "" }
        $tail | & $py $notify --source CallLoadAudit_Daily --exit "$exitCode" 2>&1 |
            Out-File -Append -Encoding utf8 $log
    }
}
