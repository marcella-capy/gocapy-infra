# Scheduled launcher - MONTHLY Pipedrive dedup REPORT (1st of the month, 06:10).
# Runs the org and person dedup DRY RUNS against today's snapshot and posts each report + review
# CSV to the dedup task (86bc8kj30) as Kodie. It never merges: merging waits for Marcella's reply.
# ASCII-only on purpose (a non-ASCII char once broke scheduler registration halfway through).
$ErrorActionPreference = "Continue"
$orgScripts    = Split-Path $PSScriptRoot -Parent
$personScripts = Join-Path (Split-Path (Split-Path $orgScripts -Parent) -Parent) "pipedrive-person-dedup\scripts"
$task   = "86bc8kj30"
$logDir = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\dedup_month_$(Get-Date -Format yyyyMMdd).log"
$py = "C:\Users\marce\AppData\Local\Python\bin\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { $py = "python" }

"=== $(Get-Date -Format o) pipedrive-dedup MONTH START ===" | Out-File -Append -Encoding utf8 $log
& $py "$orgScripts\dedup_orgs.py" --post-task $task 2>&1 | Out-File -Append -Encoding utf8 $log
$orgExit = $LASTEXITCODE
& $py "$personScripts\dedup_persons.py" --post-task $task 2>&1 | Out-File -Append -Encoding utf8 $log
$personExit = $LASTEXITCODE
$exitCode = [Math]::Max($orgExit, $personExit)
"RESULT: pipedrive-dedup monthly report org_exit=$orgExit person_exit=$personExit" | Out-File -Append -Encoding utf8 $log
"=== $(Get-Date -Format o) pipedrive-dedup MONTH END (exit $exitCode) ===" | Out-File -Append -Encoding utf8 $log

if ($exitCode -ne 0) {
    $notify = "c:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin\go-capy-outreach\skills\ai-sdr-manager\scripts\notify_run_failure.py"
    if (Test-Path $notify) {
        $tail = if (Test-Path $log) { (Get-Content $log -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { "" }
        $tail | & $py $notify --source PipedriveDedup_Monthly --exit "$exitCode" 2>&1 |
            Out-File -Append -Encoding utf8 $log
    }
}
exit $exitCode
