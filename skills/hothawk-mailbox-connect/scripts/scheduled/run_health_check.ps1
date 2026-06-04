# Scheduled launcher: daily HotHawk mailbox health scan across every workspace.
# Plain Python (no agent) — runs check_mailbox_health.py, which buckets every mailbox by
# connection status, flags disconnected / stuck-gathering accounts, optionally reconnects or
# removes dead ones, and posts a Discord digest. Idempotent; safe to re-run.
#
# Starts in REPORT-ONLY mode (no --remediate) so you can watch a few digests before letting it
# reconnect/delete. To enable auto-remediation, add " --remediate" to the $argline below and
# re-run register_scheduler.ps1 (or just edit this file — it's read fresh each run).
$ErrorActionPreference = "Continue"
$root    = "c:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin"
$script  = "$root\go-capy-outreach\skills\hothawk-mailbox-connect\scripts\check_mailbox_health.py"
$argline = ""   # e.g. "--remediate" to enable reconnect/delete once you trust the digests
$logDir  = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\health_$(Get-Date -Format yyyyMMdd).log"

Set-Location $root
"=== $(Get-Date -Format o) mailbox-health START (args: '$argline') ===" | Out-File -Append -Encoding utf8 $log
& py $script $argline.Split(" ", [StringSplitOptions]::RemoveEmptyEntries) 2>&1 | Out-File -Append -Encoding utf8 $log
"=== $(Get-Date -Format o) mailbox-health END (exit $LASTEXITCODE) ===" | Out-File -Append -Encoding utf8 $log
