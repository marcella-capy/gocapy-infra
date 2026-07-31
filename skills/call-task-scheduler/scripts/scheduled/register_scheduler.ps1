# Registers the daily call-task reconcile (removes any prior copy first). Idempotent.
# Run from PowerShell:  .\register_scheduler.ps1
#
#   CallTaskReconcile_Daily : Mon-Fri @08:20 -> run_reconcile.ps1
#
# Finds call tasks marked DONE in the last 3 business days and loads those people into their
# principal's HotHawk voicemail campaign. 08:20 is deliberate: it is a free minute (nearest
# neighbours 08:10 KillStaleClaudeSessions / 08:50 CallLoadAudit_Daily) and it lands 30 minutes
# before the daily audit, so the audit independently re-checks that morning's reconcile.
#
# Interactive principal so capy_env + Kodie's webhook resolve. WakeToRun wakes the laptop;
# StartWhenAvailable catches up if the machine was off at trigger time.
# NOTE: WakeToRun only fires if the active power plan allows wake timers (powercfg /waketimers).

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName "CallTaskReconcile_Daily" -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task 'CallTaskReconcile_Daily'..."
    Unregister-ScheduledTask -TaskName "CallTaskReconcile_Daily" -Confirm:$false
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$hidden = Join-Path $PSScriptRoot "run_hidden.vbs"
if (-not (Test-Path $hidden)) { throw "hidden launcher not found: $hidden" }
$runner = Join-Path $PSScriptRoot "run_reconcile.ps1"
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$hidden`" `"$runner`""
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 8:20AM

Register-ScheduledTask -TaskName "CallTaskReconcile_Daily" -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Daily (Mon-Fri 08:20) call-task reconcile: finds call tasks marked DONE in the last 3 business days and loads those people into their principal's HotHawk voicemail campaign (sibling call tasks stay OPEN). Alerts Discord on failure. Replaces the claude.ai cloud routine, which failed silently." | Out-Null

Write-Host "Registered. Current state:"
Get-ScheduledTask -TaskName "CallTaskReconcile_Daily" |
    ForEach-Object { $i = $_ | Get-ScheduledTaskInfo
        "{0,-26} State={1}  Next={2}" -f $_.TaskName, $_.State, $i.NextRunTime }
