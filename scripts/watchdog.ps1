# Nisaba watchdog. Runs forever until the archive finishes.
#   - every CheckEveryS seconds:
#       * if reports\final_report.md exists -> toast with summary, exit 0
#       * elif no `-m nisaba` process -> restart via run-archive.ps1, toast "restarted"
#       * else -> continue
# Toast is a Windows 10/11 native notification (no third-party module).
# Also writes a human log to watchdog.log beside the archive.

param(
    [string]$ArchiveRoot,
    [string]$Launcher,
    [int]$CheckEveryS    = 120,
    [int]$MaxRestarts    = 50
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Launcher)    { $Launcher    = Join-Path $scriptDir 'run-archive.ps1' }
if (-not $ArchiveRoot) {
    $repoRoot    = (Resolve-Path (Join-Path $scriptDir '..')).Path
    $ArchiveRoot = Join-Path $repoRoot 'archive'
}

$ErrorActionPreference = 'Continue'
$reports = Join-Path $ArchiveRoot 'reports'
$final   = Join-Path $reports 'final_report.md'
$wdlog   = Join-Path $reports 'watchdog.log'
$progF   = Join-Path $reports 'latest_progress.json'

function Write-WDLog([string]$msg) {
    $line = ("{0}  {1}" -f (Get-Date -Format 's'), $msg)
    try { Add-Content -Path $wdlog -Value $line -Encoding utf8 } catch {}
    Write-Host $line
}

function Show-Toast([string]$title, [string]$body) {
    # NotifyIcon balloon: no WinRT, works everywhere, appears as a Windows 10/11 toast.
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        Add-Type -AssemblyName System.Drawing      -ErrorAction Stop
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Information
        $ni.Visible = $true
        # trim to the limits NotifyIcon tolerates
        $t = if ($title.Length -gt 60)  { $title.Substring(0,60) }  else { $title }
        $b = if ($body.Length  -gt 250) { $body.Substring(0,250) } else { $body }
        $ni.BalloonTipTitle = $t
        $ni.BalloonTipText  = $b
        $ni.BalloonTipIcon  = [System.Windows.Forms.ToolTipIcon]::Info
        $ni.ShowBalloonTip(15000)
        # Keep the icon alive briefly so the balloon has time to render
        Start-Sleep -Seconds 2
        $ni.Visible = $false
        $ni.Dispose()
        return $true
    } catch {
        Write-WDLog ("notifyicon failed: {0}" -f $_.Exception.Message)
        # Last-ditch fallback: a MessageBox. Blocking but at least impossible to miss.
        try {
            [System.Windows.Forms.MessageBox]::Show($body, $title) | Out-Null
            return $true
        } catch { return $false }
    }
}

function Worker-Alive {
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match '-m nisaba|nisaba[\\/]archiver\.py' })
    return $procs.Count -gt 0
}

Write-WDLog ("watchdog started, pid={0}, archive={1}" -f $PID, $ArchiveRoot)
Show-Toast 'ChatGPT archive watchdog' ('Started. Will notify on completion or restart.')

$restarts = 0
while ($true) {
    if (Test-Path $final) {
        $body = ''
        try {
            $raw = Get-Content -Raw $final
            # pull a few key lines for the toast
            $wanted = @('full_archive_complete','unique conversations discovered','conversation JSON saved','Markdown saved','files saved on disk','files failed','total bytes downloaded','pagination_complete')
            $picked = ($raw -split "`n") | Where-Object { foreach ($w in $wanted) { if ($_ -match [regex]::Escape($w)) { return $true } }; return $false } | Select-Object -First 6
            $body = ($picked -join ' | ')
            if ($body.Length -gt 400) { $body = $body.Substring(0,400) + '...' }
        } catch { $body = 'see reports\final_report.md' }
        Write-WDLog ("FINISHED. final_report.md present. body={0}" -f $body)
        Show-Toast 'ChatGPT archive FINISHED' ($body + "  --  reports\final_report.md")
        break
    }
    if (-not (Worker-Alive)) {
        if ($restarts -ge $MaxRestarts) {
            Write-WDLog ("worker dead and restart budget exhausted ({0}). Giving up." -f $MaxRestarts)
            Show-Toast 'ChatGPT archive STUCK' ("Worker dead, hit $MaxRestarts restart cap. Check reports\worker.err.log")
            break
        }
        # Pull a status fingerprint for the toast
        $hint = ''
        if (Test-Path $progF) {
            try {
                $p = Get-Content -Raw $progF | ConvertFrom-Json
                $hint = ("pass={0} complete={1}/{2} files={3}/{4}" -f $p.pass, $p.conversations_complete, $p.conversations_discovered, $p.files_complete, $p.files_discovered)
            } catch {}
        }
        $restarts++
        Write-WDLog ("worker dead; restart #{0}/{1}. hint={2}" -f $restarts, $MaxRestarts, $hint)
        Show-Toast ("ChatGPT archive restart #{0}" -f $restarts) ($hint + '  --  resuming from checkpoint')
        try {
            Start-Process -FilePath 'powershell' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Launcher) -WindowStyle Hidden -Wait
            Write-WDLog ("launcher ran, exitcode=$LASTEXITCODE")
        } catch {
            Write-WDLog ("launcher invocation failed: {0}" -f $_.Exception.Message)
        }
        # give the new worker a few seconds to appear in Win32_Process
        Start-Sleep -Seconds 10
    }
    Start-Sleep -Seconds $CheckEveryS
}
Write-WDLog 'watchdog exiting'
