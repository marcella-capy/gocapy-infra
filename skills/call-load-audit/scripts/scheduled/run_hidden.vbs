' Launches a PowerShell script with NO visible console window.
'
' Task Scheduler launching powershell.exe directly creates the console window at
' process-creation time, so "-WindowStyle Hidden" only hides it AFTER PowerShell has
' started -> a real window flashes up and steals focus. Starting PowerShell via
' WshShell.Run with intWindowStyle = 0 hides it from creation, so nothing appears.
'
' bWaitOnReturn = True keeps this wrapper alive for the whole run, so the scheduled task
' stays Running for the duration -> MultipleInstances=IgnoreNew / ExecutionTimeLimit apply.
'
' Usage:  wscript.exe run_hidden.vbs "C:\path\to\runner.ps1"
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & WScript.Arguments(0) & """"
sh.Run cmd, 0, True
