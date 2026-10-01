# Registers PipedriveDedup_Weekly (removes any prior copy AND the retired PipedriveDedup_Monthly). Idempotent.
# Run from PowerShell:  .\register_scheduler.ps1            -> registered DISABLED (default)
#                       .\register_scheduler.ps1 -Enable    -> registered ENABLED (only when Marcella says so)
#
#   PipedriveDedup_Weekly : DAILY @06:10 -> run_weekly.ps1
#   Daily: merge the sure list once Marcella replies "merge them" (dedup_approval.py).
#   Mondays: org + person reports with a label import file, posted to the dedup task (86bc8kj30).
#
# 06:10 is after the nightly Pipedrive snapshot (AISDR_PipedriveCache). ASCII-only file.
param([switch]$Enable)
$ErrorActionPreference = "Stop"
$name = "PipedriveDedup_Weekly"
if (Get-ScheduledTask -TaskName "PipedriveDedup_Monthly" -ErrorAction SilentlyContinue) {
    Write-Host "Removing retired task 'PipedriveDedup_Monthly'..."
    Unregister-ScheduledTask -TaskName "PipedriveDedup_Monthly" -Confirm:$false
}

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$name'..."
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}

$hidden = "$PSScriptRoot\..\..\..\call-load-audit\scripts\scheduled\run_hidden.vbs"
$hidden = (Resolve-Path $hidden).Path
$runner = Join-Path $PSScriptRoot "run_weekly.ps1"
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }
$enabled = if ($Enable) { "true" } else { "false" }

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Daily 06:10 Pipedrive dedup: Mondays post the org + person duplicate reports with a label import file for the sure duplicates; every day merges a sure list once Marcella has replied merge them on the dedup ClickUp task.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T06:10:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
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
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
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
