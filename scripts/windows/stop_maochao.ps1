function Get-ProjectRoot {
    $candidates = @()
    try { $candidates += (Resolve-Path (Join-Path $PSScriptRoot "..")).Path } catch {}
    try { $candidates += (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path } catch {}
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path (Join-Path $candidate "api_server.py")) {
            return $candidate
        }
    }
    throw "Could not locate project root from $PSScriptRoot"
}

$ErrorActionPreference = "Stop"
$ProjectRoot = Get-ProjectRoot

$Pids = @()

try {
    $health = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 2
    if ($health.pid) { $Pids += [int]$health.pid }
} catch {}

try {
    $worker = Invoke-RestMethod "http://127.0.0.1:8000/api/worker" -TimeoutSec 2
    if ($worker.worker_pid) { $Pids += [int]$worker.worker_pid }
} catch {}

try {
    $Processes = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object {
            $_.CommandLine -and
            ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
            ($_.CommandLine -like "*api_server.py*" -or $_.CommandLine -like "*rpa_worker.py*")
        }
    foreach ($Process in $Processes) {
        if ($Process.ProcessId) { $Pids += [int]$Process.ProcessId }
    }
} catch {
    Write-Host "[warn] process list unavailable, using API pids only: $($_.Exception.Message)"
}

$Pids = $Pids | Where-Object { $_ -gt 0 } | Select-Object -Unique

if (!$Pids) {
    Write-Host "[ok] no maochao process found"
    exit 0
}

foreach ($TargetPid in $Pids) {
    Write-Host "[ok] stopping pid=$TargetPid"
    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
}
