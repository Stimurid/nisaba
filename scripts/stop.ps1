param(
    [string]$TaskName = 'ChatGPTFullArchive'
)

Write-Host "== stopping scheduled task '$TaskName' if any =="
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop; Write-Host "task stopped" } catch { Write-Host "no active task ($($_.Exception.Message))" }
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop; Write-Host "task unregistered" } catch {}

Write-Host "`n== killing lingering workers =="
$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '-m nisaba|nisaba[\\/]archiver\.py' })
if ($procs.Count -eq 0) {
    Write-Host "no nisaba archiver processes"
} else {
    foreach ($p in $procs) {
        Write-Host ("killing pid={0}" -f $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Write-Host "`nDone. Next run-archive.ps1 will resume from checkpoint."
