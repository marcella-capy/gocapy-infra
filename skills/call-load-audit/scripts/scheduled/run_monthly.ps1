# Scheduled launcher — MONTHLY call->HotHawk load audit (1st of the month).
# Runs audit_call_loads.py --mode month --discord: sums the whole PREVIOUS calendar month.
$ErrorActionPreference = "Continue"
$scripts = Split-Path $PSScriptRoot -Parent
$logDir  = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\audit_month_$(Get-Date -Format yyyyMMdd).log"
$py  = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }
"=== $(Get-Date -Format o) call-load-audit MONTH START ===" | Out-File -Append -Encoding utf8 $log
& $py "$scripts\audit_call_loads.py" --mode month --discord 2>&1 | Out-File -Append -Encoding utf8 $log
"=== $(Get-Date -Format o) call-load-audit MONTH END (exit $LASTEXITCODE) ===" | Out-File -Append -Encoding utf8 $log
