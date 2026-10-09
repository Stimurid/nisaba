param(
    [string]$ArchiveRoot
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ArchiveRoot) {
    $repoRoot    = (Resolve-Path (Join-Path $scriptDir '..')).Path
    $ArchiveRoot = Join-Path $repoRoot 'archive'
}

$reports = Join-Path $ArchiveRoot 'reports'
$latest  = Join-Path $reports 'latest_progress.json'
$log     = Join-Path $reports 'archive.log'
$errLog  = Join-Path $reports 'worker.err.log'

Write-Host "== worker processes =="
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match '-m nisaba|nisaba[\\/]archiver\.py' } |
    Select-Object ProcessId, CreationDate, CommandLine | Format-List

if (Test-Path $latest) {
    Write-Host "`n== latest_progress.json =="
    Get-Content -Raw $latest | ConvertFrom-Json | Format-List status, pass, phase, conversations_discovered, conversations_complete, conversations_pending, conversations_failed, files_discovered, files_complete, files_failed, bytes_downloaded, heartbeat_at, last_error, current_item, pagination_complete, attachment_discovery_status
} else {
    Write-Host "`n(latest_progress.json not yet written)"
}

if (Test-Path $log) {
    Write-Host "`n== archive.log (tail) =="
    Get-Content -Path $log -Tail 20
}

if (Test-Path $errLog) {
    $size = (Get-Item $errLog).Length
    if ($size -gt 0) {
        Write-Host "`n== worker.err.log (tail) =="
        Get-Content -Path $errLog -Tail 20
    }
}
