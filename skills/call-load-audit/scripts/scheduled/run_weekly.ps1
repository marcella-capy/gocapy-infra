# Scheduled launcher — WEEKLY call->HotHawk load audit (Friday morning).
# Runs audit_call_loads.py --mode week --discord: sums Monday..previous-business-day of this week.
$ErrorActionPreference = "Continue"
$scripts = Split-Path $PSScriptRoot -Parent
$logDir  = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\audit_week_$(Get-Date -Format yyyyMMdd).log"
$py  = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }
"=== $(Get-Date -Format o) call-load-audit WEEK START ===" | Out-File -Append -Encoding utf8 $log
& $py "$scripts\audit_call_loads.py" --mode week --discord 2>&1 | Out-File -Append -Encoding utf8 $log
$exitCode = $LASTEXITCODE
"=== $(Get-Date -Format o) call-load-audit WEEK END (exit $exitCode) ===" | Out-File -Append -Encoding utf8 $log

# exit 2 = pipeline failure (zero loaded), exit 1 = the audit itself died. Both must ping.
if ($exitCode -ne 0) {
    $notify = "c:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin\go-capy-outreach\skills\ai-sdr-manager\scripts\notify_run_failure.py"
    if (Test-Path $notify) {
        $tail = if (Test-Path $log) { (Get-Content $log -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { "" }
        $tail | & $py $notify --source CallLoadAudit_Weekly --exit "$exitCode" 2>&1 |
            Out-File -Append -Encoding utf8 $log
    }
}
