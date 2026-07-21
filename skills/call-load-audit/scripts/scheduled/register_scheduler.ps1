# Registers the three call-load-audit tasks (removes any prior copies first). Idempotent.
# Run from PowerShell:  .\register_scheduler.ps1
#
#   CallLoadAudit_Daily   : Mon-Fri @08:50 -> run_daily.ps1   (--mode day)
#   CallLoadAudit_Weekly  : Fri     @09:12 -> run_weekly.ps1  (--mode week, this week so far)
#   CallLoadAudit_Monthly : 1st     @07:42 -> run_monthly.ps1 (--mode month, previous month)
#
# Each audits the call-task-scheduler done calls in its window against LIVE HotHawk enrollment and
# posts the roster (principal / User / Company / # of Calls) plus any NOT-LOADED misses to Discord.
#
# Interactive principal so capy_env + Kodie's webhook resolve. Distinct start minutes (no collision
# with the other AISDR wake-to-run tasks). WakeToRun wakes the laptop for the run; StartWhenAvailable
# catches up if the machine was fully off at trigger time (relevant for the 1st-of-month run).
# NOTE: WakeToRun only fires if the active power plan allows wake timers (powercfg /waketimers).

$ErrorActionPreference = "Stop"

foreach ($old in @("CallLoadAudit_Daily", "CallLoadAudit_Weekly", "CallLoadAudit_Monthly")) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Write-Host "Removing existing task '$old'..."
        Unregister-ScheduledTask -TaskName $old -Confirm:$false
    }
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$hidden = Join-Path $PSScriptRoot "run_hidden.vbs"
if (-not (Test-Path $hidden)) { throw "hidden launcher not found: $hidden" }

function New-Action($runnerName) {
    $runner = Join-Path $PSScriptRoot $runnerName
    if (-not (Test-Path $runner)) { throw "runner not found: $runner" }
    # Launch PowerShell via the VBScript shim so no console window flashes; wscript waits for the
    # run to finish so MultipleInstances=IgnoreNew and ExecutionTimeLimit still apply.
    New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$hidden`" `"$runner`""
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# --- Daily (Mon-Fri 08:50) ---
$dailyTrig = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 8:50AM
Register-ScheduledTask -TaskName "CallLoadAudit_Daily" -Action (New-Action "run_daily.ps1") `
    -Trigger $dailyTrig -Settings $settings -Principal $principal `
    -Description "Daily (Mon-Fri 08:50) call->HotHawk load audit: checks the previous business day's DONE call-scheduler calls actually got enrolled (and sending) in each principal's HotHawk voicemail campaign, and posts the roster + any misses to the AISDR Discord." | Out-Null

# --- Weekly (Fri 09:12) ---
$weeklyTrig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 9:12AM
Register-ScheduledTask -TaskName "CallLoadAudit_Weekly" -Action (New-Action "run_weekly.ps1") `
    -Trigger $weeklyTrig -Settings $settings -Principal $principal `
    -Description "Weekly (Fri 09:12) call->HotHawk load audit: sums Monday..previous-business-day of the current week and posts the roster + any misses to the AISDR Discord." | Out-Null

# --- Monthly (1st @07:42) — CalendarTrigger via XML, since New-ScheduledTaskTrigger has no monthly ---
$monthlyRunner = Join-Path $PSScriptRoot "run_monthly.ps1"
if (-not (Test-Path $monthlyRunner)) { throw "runner not found: $monthlyRunner" }
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monthly (1st 07:42) call-&gt;HotHawk load audit: sums the whole previous calendar month and posts the roster + any misses to the AISDR Discord.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T07:42:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth><Day>1</Day></DaysOfMonth>
        <Months>
          <January/><February/><March/><April/><May/><June/>
          <July/><August/><September/><October/><November/><December/>
        </Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$env:USERDOMAIN\$env:USERNAME</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"$hidden" "$monthlyRunner"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
Register-ScheduledTask -TaskName "CallLoadAudit_Monthly" -Xml $xml | Out-Null

Write-Host "Registered call-load-audit tasks. Current state:"
Get-ScheduledTask -TaskName "CallLoadAudit_*" |
    ForEach-Object { $i = $_ | Get-ScheduledTaskInfo
        "{0,-24} State={1}  Next={2}" -f $_.TaskName, $_.State, $i.NextRunTime }
