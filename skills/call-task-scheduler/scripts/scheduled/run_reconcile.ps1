# Scheduled launcher — DAILY call-task reconcile -> HotHawk voicemail load (weekday mornings).
#
# Runs reconcile_and_load.py: finds call tasks marked DONE in the last 3 business days and pushes
# those people into their principal's HotHawk voicemail campaign. Sibling call tasks stay OPEN.
#
# Replaces the claude.ai "Call Task Reconcile" cloud routine, which failed silently three times
# because nothing local ever saw its exit code. Runs at 08:20, 30 min before CallLoadAudit_Daily,
# so the audit independently verifies that morning's work.
$ErrorActionPreference = "Continue"
$scripts = Split-Path $PSScriptRoot -Parent          # ...\call-task-scheduler\scripts
$logDir  = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\reconcile_$(Get-Date -Format yyyyMMdd).log"
$py  = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }

"=== $(Get-Date -Format o) call-task-reconcile START ===" | Out-File -Append -Encoding utf8 $log
& $py "$scripts\reconcile_and_load.py" 2>&1 | Out-File -Append -Encoding utf8 $log
$exitCode = $LASTEXITCODE
"=== $(Get-Date -Format o) call-task-reconcile END (exit $exitCode) ===" | Out-File -Append -Encoding utf8 $log

# The whole point of moving this off the cloud routine: a failure has to reach a human.
if ($exitCode -ne 0) {
    $notify = "c:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin\go-capy-outreach\skills\ai-sdr-manager\scripts\notify_run_failure.py"
    if (Test-Path $notify) {
        $tail = if (Test-Path $log) { (Get-Content $log -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { "" }
        $tail | & $py $notify --source CallTaskReconcile --exit "$exitCode" 2>&1 |
            Out-File -Append -Encoding utf8 $log
    }
}
