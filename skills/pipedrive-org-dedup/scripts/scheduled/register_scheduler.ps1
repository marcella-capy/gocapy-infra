# Registers PipedriveDedup_Monthly (removes any prior copy first). Idempotent.
# Run from PowerShell:  .\register_scheduler.ps1            -> registered DISABLED (default)
#                       .\register_scheduler.ps1 -Enable    -> registered ENABLED (only when Marcella says so)
#
#   PipedriveDedup_Monthly : 1st of the month @06:10 -> run_monthly.ps1
#   Org + person dedup DRY RUNS, report + review CSVs posted to the dedup task (86bc8kj30). Never merges.
#
# 06:10 is after the nightly Pipedrive snapshot (AISDR_PipedriveCache). ASCII-only file.
param([switch]$Enable)
$ErrorActionPreference = "Stop"
$name = "PipedriveDedup_Monthly"

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$name'..."
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}

$hidden = "$PSScriptRoot\..\..\..\call-load-audit\scripts\scheduled\run_hidden.vbs"
$hidden = (Resolve-Path $hidden).Path
$runner = Join-Path $PSScriptRoot "run_monthly.ps1"
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }
$enabled = if ($Enable) { "true" } else { "false" }

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monthly (1st 06:10) Pipedrive org + person duplicate REPORT: dry runs only, posts the review sheets to the dedup ClickUp task. Merging waits for Marcella's approval.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T06:10:00</StartBoundary>
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
    <Enabled>$enabled</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"$hidden" "$runner"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
Register-ScheduledTask -TaskName $name -Xml $xml | Out-Null
$t = Get-ScheduledTask -TaskName $name
Write-Host ("{0}  State={1}" -f $t.TaskName, $t.State)
