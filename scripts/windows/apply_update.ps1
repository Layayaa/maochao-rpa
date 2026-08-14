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
Set-Location $ProjectRoot

$ZipCandidates = @(
    (Join-Path $env:USERPROFILE "Desktop\maochao_rpa_code_update.zip"),
    (Join-Path $env:USERPROFILE "Downloads\maochao_rpa_code_update.zip"),
    (Join-Path $ProjectRoot "maochao_rpa_code_update.zip"),
    (Join-Path $PSScriptRoot "maochao_rpa_code_update.zip")
)
$ZipPath = $ZipCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $ZipPath) {
    throw "找不到 maochao_rpa_code_update.zip。请先把它放到桌面、下载目录或 $ProjectRoot"
}

Write-Host "ProjectRoot: $ProjectRoot"
Write-Host "Zip: $ZipPath"

$Staging = Join-Path $env:TEMP ("maochao_rpa_update_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Staging | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $Staging -Force

$Allowed = @(
    "api_server.py",
    "backend_core.py",
    "backend_selftest.py",
    "maochao_rpa.py",
    "rpa_worker.py",
    "config.example.json",
    "web\app.js",
    "web\index.html",
    "web\styles.css",
    "scripts\windows\start_maochao.ps1",
    "scripts\windows\start_maochao.cmd",
    "scripts\windows\stop_maochao.ps1",
    "scripts\windows\stop_maochao.cmd",
    "scripts\windows\status_maochao.ps1",
    "scripts\windows\status_maochao.cmd",
    "scripts\windows\install_startup.ps1",
    "scripts\windows\apply_update.ps1"
)

foreach ($Relative in $Allowed) {
    $Source = Join-Path $Staging $Relative
    if (!(Test-Path $Source)) {
        Write-Host "[skip] missing in zip: $Relative"
        continue
    }
    $Target = Join-Path $ProjectRoot $Relative
    New-Item -ItemType Directory -Force -Path (Split-Path $Target) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Target -Force
    Write-Host "[ok] updated $Relative"
}

Remove-Item -LiteralPath $Staging -Recurse -Force

Write-Host ""
Write-Host "Stopping old API/Worker..."
& (Join-Path $PSScriptRoot "stop_maochao.ps1")
Start-Sleep -Seconds 2
Write-Host "Starting API/Worker..."
& (Join-Path $PSScriptRoot "start_maochao.ps1")
Start-Sleep -Seconds 3
Write-Host ""
Write-Host "Health:"
& (Join-Path $PSScriptRoot "status_maochao.ps1")
