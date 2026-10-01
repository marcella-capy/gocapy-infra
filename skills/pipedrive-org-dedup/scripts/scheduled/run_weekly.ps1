# Scheduled launcher - PipedriveDedup_Weekly (DAILY 06:10, after the nightly Pipedrive snapshot).
# Every day : dedup_approval.py - if Marcella replied "merge them" on the dedup task (86bc8kj30)
#             after a weekly report, merge that report's SURE list (a budget-capped batch a night).
# Mondays   : org + person dedup reports, each with a label import file for the sure duplicates,
#             posted to 86bc8kj30 as Kodie. The reports never merge anything.
# Replaced PipedriveDedup_Monthly on 2026-09-30 (Marcella: weekly, label via CSV, merge on approval).
# ASCII-only on purpose (a non-ASCII char once broke scheduler registration halfway through).
$ErrorActionPreference = "Continue"
$orgScripts    = Split-Path $PSScriptRoot -Parent
$personScripts = Join-Path (Split-Path (Split-Path $orgScripts -Parent) -Parent) "pipedrive-person-dedup\scripts"
$task   = "86bc8kj30"
$logDir = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\dedup_week_$(Get-Date -Format yyyyMMdd).log"
$py = "C:\Users\marce\AppData\Local\Python\bin\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { $py = "python" }

"=== $(Get-Date -Format o) pipedrive-dedup START ===" | Out-File -Append -Encoding utf8 $log
& $py "$orgScripts\dedup_approval.py" 2>&1 | Out-File -Append -Encoding utf8 $log
$approvalExit = $LASTEXITCODE
$orgExit = 0
$personExit = 0
if ((Get-Date).DayOfWeek -eq "Monday") {
    & $py "$orgScripts\dedup_orgs.py" --post-task $task 2>&1 | Out-File -Append -Encoding utf8 $log
    $orgExit = $LASTEXITCODE
    & $py "$personScripts\dedup_persons.py" --post-task $task 2>&1 | Out-File -Append -Encoding utf8 $log
    $personExit = $LASTEXITCODE
}
$exitCode = [Math]::Max($approvalExit, [Math]::Max($orgExit, $personExit))
"RESULT: pipedrive-dedup approval_exit=$approvalExit org_exit=$orgExit person_exit=$personExit" | Out-File -Append -Encoding utf8 $log
"=== $(Get-Date -Format o) pipedrive-dedup END (exit $exitCode) ===" | Out-File -Append -Encoding utf8 $log

if ($exitCode -ne 0) {
    $notify = "c:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin\go-capy-outreach\skills\ai-sdr-manager\scripts\notify_run_failure.py"
    if (Test-Path $notify) {
        $tail = if (Test-Path $log) { (Get-Content $log -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { "" }
        $tail | & $py $notify --source PipedriveDedup_Weekly --exit "$exitCode" 2>&1 |
            Out-File -Append -Encoding utf8 $log
    }
}
exit $exitCode
