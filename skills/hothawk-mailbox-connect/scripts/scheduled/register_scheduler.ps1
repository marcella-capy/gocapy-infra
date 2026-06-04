# Registers the daily HotHawk mailbox health-check task. Idempotent — safe to re-run.
#
#   HotHawk_MailboxHealth : once @08:00, weekdays -> run_health_check.ps1
#     Scans every HotHawk workspace, buckets mailboxes by connection status, flags
#     disconnected / stuck-gathering accounts (and, once --remediate is enabled in the
#     launcher, reconnects or removes dead ones), then posts a Discord digest.
#
# Interactive principal so the mapped G:\ drive (token + creds + Discord webhook) resolves.
# StartWhenAvailable catches up after the laptop wakes. MultipleInstances=IgnoreNew avoids
# pile-up. The scan is read-only by default; remediation is opt-in via run_health_check.ps1.
#
# Run from PowerShell:  .\register_scheduler.ps1

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName "HotHawk_MailboxHealth" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task 'HotHawk_MailboxHealth'..."
    Unregister-ScheduledTask -TaskName "HotHawk_MailboxHealth" -Confirm:$false
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$runner = Join-Path $PSScriptRoot "run_health_check.ps1"
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 8:00AM

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "HotHawk_MailboxHealth" -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Daily HotHawk mailbox health scan across all workspaces: buckets by connection status, flags disconnected / stuck-gathering accounts, optionally reconnects or removes dead ones, posts a Discord digest. Weekdays 08:00." | Out-Null

Write-Host "Registered HotHawk_MailboxHealth. Current state:"
$t = Get-ScheduledTask -TaskName "HotHawk_MailboxHealth"
$i = $t | Get-ScheduledTaskInfo
"{0,-22} State={1}  Next={2}" -f $t.TaskName, $t.State, $i.NextRunTime
