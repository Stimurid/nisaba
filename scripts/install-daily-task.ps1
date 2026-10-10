# Install Nisaba as a daily Windows Scheduled Task.
# Default: run every day at 03:17 local time, incremental mode.
# Idempotent ? safe to re-run; it unregisters the prior version.

param(
    [string]$ArchiveRoot,
    [string]$RepoRoot,
    [string]$TokenFile,
    [double]$ThrottleSeconds = 1.0,
    [string]$DailyAt = '03:17',
    [string]$TaskName = 'NisabaDaily',
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RepoRoot)    { $RepoRoot    = (Resolve-Path (Join-Path $scriptDir '..')).Path }
if (-not $TokenFile)   { $TokenFile   = Join-Path $scriptDir 'token.txt' }
if (-not $ArchiveRoot) { $ArchiveRoot = Join-Path $RepoRoot 'archive' }

foreach ($p in @((Join-Path $RepoRoot 'src\nisaba\archiver.py'), $TokenFile)) {
    if (-not (Test-Path $p)) { Write-Error "Missing prerequisite: $p"; exit 2 }
}

$token = (Get-Content -Raw -Path $TokenFile).Trim()
if (-not $token) { Write-Error "token.txt is empty"; exit 2 }

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { Write-Error "python not on PATH."; exit 4 }
$pyExe = $py.Source

# curl_cffi fatal check ? fail-fast at install time, not at 03:17 silently
$null = & $pyExe -c "import curl_cffi" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "curl_cffi not installed. Run: python -m pip install --user curl_cffi"
    exit 5
}

$srcDir  = Join-Path $RepoRoot 'src'
$reports = Join-Path $ArchiveRoot 'reports'
New-Item -ItemType Directory -Force -Path $reports | Out-Null

# .cmd that the Scheduled Task actually invokes. Token embedded here ? the
# file sits beside the archive, protect it like a password.
$cmdPath = Join-Path $reports 'nisaba_daily.cmd'
$outLog  = Join-Path $reports 'nisaba_daily.out.log'
$errLog  = Join-Path $reports 'nisaba_daily.err.log'

$cmdBody = @"
@echo off
setlocal
cd /d "$RepoRoot"
set "PYTHONPATH=$srcDir"
set "PYTHONUNBUFFERED=1"
set "LITOPS_SKIP_DOTENV=1"
set "LITOPS_CHATGPT_THROTTLE_SECONDS=$ThrottleSeconds"
set "LITOPS_CHATGPT_SESSION_TOKEN=$token"
echo [%DATE% %TIME%] starting Nisaba daily incremental >> "$outLog"
"$pyExe" -u -m nisaba --output "$ArchiveRoot" --resume --include-archived --include-projects --include-files --json-status --throttle $ThrottleSeconds --incremental >> "$outLog" 2>> "$errLog"
set EC=%ERRORLEVEL%
echo [%DATE% %TIME%] Nisaba daily exit code: %EC% >> "$outLog"
endlocal
"@
Set-Content -Path $cmdPath -Value $cmdBody -Encoding ASCII -NoNewline

# Register the task
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null } catch {}

$action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c call `"$cmdPath`"" -WorkingDirectory $RepoRoot
$trigger   = New-ScheduledTaskTrigger -Daily -At $DailyAt
# Allow bootup catch-up: if the machine was off at 03:17, run when it comes online within 24h.
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "[OK] registered scheduled task '$TaskName' daily at $DailyAt"
Write-Host "     output archive : $ArchiveRoot"
Write-Host "     cmd            : $cmdPath"
Write-Host "     stdout log     : $outLog"
Write-Host "     stderr log     : $errLog"
Write-Host "     next run       : $((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"

if ($RunNow) {
    Write-Host "`n[RunNow] starting the task once now..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "     last run time   : $($info.LastRunTime)"
    Write-Host "     last task result: $($info.LastTaskResult)"
    Write-Host "     tail -f $outLog  (or scripts\status.ps1) to watch"
}
