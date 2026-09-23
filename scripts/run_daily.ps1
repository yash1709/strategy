# Runs the daily job. Invoked by Windows Task Scheduler (see register_task.ps1).
# Output is redirected by cmd.exe, not PowerShell: Windows PowerShell 5.1 turns a native
# program's stderr lines (our normal log output) into errors, which aborts the run.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$python = "$root\.venv\Scripts\python.exe"
$log = "$root\logs\scheduler.log"
Add-Content -Path $log -Value "`r`n===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run =====" -Encoding utf8
& cmd.exe /d /c "`"$python`" -m nse_monitor run >> `"$log`" 2>&1"
$code = $LASTEXITCODE
Add-Content -Path $log -Value "===== exit code $code =====" -Encoding utf8
exit $code
