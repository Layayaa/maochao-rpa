function Get-ProjectRoot {
    $candidates = @()
    try { $candidates += (Resolve-Path (Join-Path $PSScriptRoot "..")).Path } catch {}
    try { $candidates += (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path } catch {}
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path (Join-Path $candidate "api_server.py")) {
            return $candidate
        }
    }
    return (Resolve-Path $PSScriptRoot).Path
}

$ErrorActionPreference = "Continue"
$ProjectRoot = Get-ProjectRoot

Write-Host "ProjectRoot: $ProjectRoot"

Write-Host ""
Write-Host "API:"
try {
    Invoke-RestMethod "http://127.0.0.1:8000/api/health" | ConvertTo-Json -Compress
} catch {
    Write-Host $_.Exception.Message
}

Write-Host ""
Write-Host "Worker:"
try {
    Invoke-RestMethod "http://127.0.0.1:8000/api/worker" | ConvertTo-Json -Compress
} catch {
    Write-Host $_.Exception.Message
}
