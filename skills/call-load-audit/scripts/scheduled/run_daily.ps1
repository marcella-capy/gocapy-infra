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
"=== $(Get-Date -Format o) call-load-audit DAY END (exit $LASTEXITCODE) ===" | Out-File -Append -Encoding utf8 $log
