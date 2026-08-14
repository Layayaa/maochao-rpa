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

$Processes = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -and
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
        ($_.CommandLine -like "*api_server.py*" -or $_.CommandLine -like "*rpa_worker.py*")
    }

if (!$Processes) {
    Write-Host "[ok] no maochao process found"
    exit 0
}

foreach ($Process in $Processes) {
    Write-Host "[ok] stopping pid=$($Process.ProcessId) command=$($Process.CommandLine)"
    Stop-Process -Id $Process.ProcessId -Force
}
