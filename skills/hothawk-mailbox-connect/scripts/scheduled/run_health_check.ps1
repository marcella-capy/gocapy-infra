# Scheduled launcher: daily HotHawk mailbox health scan across every workspace.
# Plain Python (no agent) — runs check_mailbox_health.py, which buckets every mailbox by
# connection status, flags disconnected / stuck-gathering accounts, optionally reconnects or
# removes dead ones, and posts a Discord digest. Idempotent; safe to re-run.
#
# Starts in REPORT-ONLY mode (no --remediate) so you can watch a few digests before letting it
# reconnect/delete. To enable auto-remediation, add " --remediate" to the $argline below and
# re-run register_scheduler.ps1 (or just edit this file — it's read fresh each run).
#
# Paths are derived from $PSScriptRoot, NOT hardcoded. This skill used to live under
# gocapy-claude-plugin\go-capy-outreach\skills\; when it moved to gocapy-infra the hardcoded
# $root left this pointing at a directory that no longer exists, and the scan died silently on
# 2026-06-03 — nobody noticed for two months because a launcher that can't find its script
# writes an empty log and exits 0. Keep these relative.
$ErrorActionPreference = "Continue"
$script  = Join-Path $PSScriptRoot "..\check_mailbox_health.py"
$argline = ""   # e.g. "--remediate" to enable reconnect/delete once you trust the digests
$logDir  = "$PSScriptRoot\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = "$logDir\health_$(Get-Date -Format yyyyMMdd).log"

# Fail LOUDLY on a missing script. The old version just ran `py <nonexistent path>`, which wrote
# nothing useful to the log and still exited 0 — that is how this went unnoticed for two months.
if (-not (Test-Path $script)) {
    "=== $(Get-Date -Format o) mailbox-health ABORT: script not found at $script ===" |
        Out-File -Append -Encoding utf8 $log
    exit 1
}
Set-Location $PSScriptRoot
"=== $(Get-Date -Format o) mailbox-health START (args: '$argline') ===" | Out-File -Append -Encoding utf8 $log
& py $script $argline.Split(" ", [StringSplitOptions]::RemoveEmptyEntries) 2>&1 | Out-File -Append -Encoding utf8 $log
"=== $(Get-Date -Format o) mailbox-health END (exit $LASTEXITCODE) ===" | Out-File -Append -Encoding utf8 $log
