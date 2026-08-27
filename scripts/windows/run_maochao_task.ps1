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

$ErrorActionPreference = "Continue"
$ProjectRoot = Get-ProjectRoot
$StartScript = Join-Path $ProjectRoot "scripts\windows\start_maochao.ps1"
$LogPath = Join-Path $ProjectRoot "logs\task_guardian.out.log"

while ($true) {
    try {
        & $StartScript *>> $LogPath
    } catch {
        $_ | Out-String | Add-Content -Path $LogPath
    }
    Start-Sleep -Seconds 10
}
