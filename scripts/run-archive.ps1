# Nisaba ? ChatGPT archive launcher (Windows / PowerShell).
# One worker. Detached scheduled task. Never prints or writes the token anywhere.
#
# Prerequisites:
#   - python 3.11+ on PATH
#   - pip install --user curl_cffi
#   - token.txt next to this script OR -TokenFile <path>
#
# Usage:
#   .\run-archive.ps1
#     Uses defaults: output = .\archive\, repo = .., token = .\token.txt
#   .\run-archive.ps1 -ArchiveRoot D:\archives\chatgpt\primary -TokenFile C:\secrets\gpt1.txt

param(
    [string]$ArchiveRoot,
    [string]$RepoRoot,
    [string]$TokenFile,
    [double]$ThrottleSeconds = 1.0,
    [string]$TaskName = 'ChatGPTFullArchive'
)

$ErrorActionPreference = 'Stop'

$launcherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RepoRoot)    { $RepoRoot    = (Resolve-Path (Join-Path $launcherDir '..')).Path }
if (-not $TokenFile)   { $TokenFile   = Join-Path $launcherDir 'token.txt' }
if (-not $ArchiveRoot) { $ArchiveRoot = Join-Path $RepoRoot 'archive' }

# -------- token --------
if (-not (Test-Path $TokenFile)) {
    Write-Error "token.txt not found at $TokenFile. See docs/BRAVE_COOKIE.md for how to extract __Secure-next-auth.session-token."
    exit 2
}
$token = (Get-Content -Raw -Path $TokenFile).Trim()
if (-not $token) {
    Write-Error "$TokenFile is empty. Paste the session cookie value (or value.0|||value.1 if two parts)."
    exit 2
}
if ($token -match '^ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$') {
    Write-Warning "Token looks like a raw Bearer JWT (short-lived, ~1h). Prefer __Secure-next-auth.session-token - it lasts weeks."
}
Write-Host ("[OK] token loaded ({0} chars)" -f $token.Length)

# -------- repo layout --------
$srcDir = Join-Path $RepoRoot 'src'
$pkgDir = Join-Path $srcDir   'nisaba'
foreach ($p in @($pkgDir, (Join-Path $pkgDir 'archiver.py'), (Join-Path $pkgDir 'client.py'))) {
    if (-not (Test-Path $p)) { Write-Error "Missing prerequisite: $p"; exit 3 }
}
$reports = Join-Path $ArchiveRoot 'reports'
New-Item -ItemType Directory -Force -Path $reports | Out-Null

# -------- python --------
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { Write-Error "python not on PATH."; exit 4 }
$pyExe = $py.Source
Write-Host ("[OK] python: {0}" -f $pyExe)

# curl_cffi check (fatal if missing ? session exchange needs it)
$cffi = & $pyExe -c "import curl_cffi; print(curl_cffi.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "curl_cffi not installed. Run: python -m pip install --user curl_cffi"
    exit 5
}
Write-Host ("[OK] curl_cffi {0}" -f $cffi)

# -------- if already running, don't spawn a second worker --------
$existing = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'nisaba\.?\\?archiver\.py|-m nisaba' })
if ($existing.Count -gt 0) {
    $keep = $existing[0].ProcessId
    Write-Host ("ALREADY_RUNNING pid={0} count={1}" -f $keep, $existing.Count)
    Write-Host "Use status.ps1 to watch. stop.ps1 to cancel."
    exit 0
}

# -------- write the launcher .cmd (used by scheduled task) --------
# Reasoning: a scheduled task starts a NEW cmd.exe process. It cannot inherit
# our current PowerShell env, so the token has to be in the .cmd. The .cmd is
# .gitignored by default. Protect $reports/ if you share it.
$cmdPath = Join-Path $reports 'worker_launch.cmd'
$outLog  = Join-Path $reports 'worker.out.log'
$errLog  = Join-Path $reports 'worker.err.log'

$cmdBody = @"
@echo off
setlocal
cd /d "$RepoRoot"
set "PYTHONPATH=$srcDir"
set "PYTHONUNBUFFERED=1"
set "LITOPS_SKIP_DOTENV=1"
set "LITOPS_CHATGPT_THROTTLE_SECONDS=$ThrottleSeconds"
set "LITOPS_CHATGPT_SESSION_TOKEN=$token"
"$pyExe" -u -m nisaba --output "$ArchiveRoot" --resume --include-archived --include-projects --include-files --json-status --throttle $ThrottleSeconds >> "$outLog" 2>> "$errLog"
endlocal
"@
Set-Content -Path $cmdPath -Value $cmdBody -Encoding ASCII -NoNewline

# -------- register + start scheduled task --------
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    $action   = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c call `"$cmdPath`"" -WorkingDirectory $RepoRoot
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 14)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $pids = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '-m nisaba' })
    if ($pids.Count -gt 0) {
        Write-Host ("LAUNCHED pid={0} via=scheduled_task archive={1}" -f $pids[0].ProcessId, $ArchiveRoot)
        Write-Host "Watch: .\status.ps1     Stop: .\stop.ps1"
        exit 0
    }
    Write-Warning "Scheduled task started but no worker pid observed yet. Falling back to detached process."
} catch {
    Write-Warning "scheduled task path failed: $($_.Exception.Message). Falling back to detached process."
}

# -------- fallback: detached breakaway process --------
$env:PYTHONPATH = $srcDir
$env:PYTHONUNBUFFERED = '1'
$env:LITOPS_SKIP_DOTENV = '1'
$env:LITOPS_CHATGPT_THROTTLE_SECONDS = $ThrottleSeconds
$env:LITOPS_CHATGPT_SESSION_TOKEN = $token
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $pyExe
$startInfo.Arguments = "-u -m nisaba --output `"$ArchiveRoot`" --resume --include-archived --include-projects --include-files --json-status --throttle $ThrottleSeconds"
$startInfo.WorkingDirectory = $RepoRoot
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.CreateNoWindow = $true
$startInfo.EnvironmentVariables['PYTHONPATH'] = $srcDir
$startInfo.EnvironmentVariables['LITOPS_SKIP_DOTENV'] = '1'
$startInfo.EnvironmentVariables['LITOPS_CHATGPT_SESSION_TOKEN'] = $token
$startInfo.EnvironmentVariables['LITOPS_CHATGPT_THROTTLE_SECONDS'] = "$ThrottleSeconds"
$proc = [System.Diagnostics.Process]::Start($startInfo)
Write-Host ("LAUNCHED pid={0} via=fallback (dies when this shell exits - prefer scheduled task path)" -f $proc.Id)
