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
"=== $(Get-Date -Format o) call-load-audit WEEK END (exit $LASTEXITCODE) ===" | Out-File -Append -Encoding utf8 $log
