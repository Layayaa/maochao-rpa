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

$StartupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$CmdPath = Join-Path $StartupDir "maochao_rpa_start.cmd"
$StartScript = Join-Path $PSScriptRoot "start_maochao.ps1"
$StartupLog = Join-Path $ProjectRoot "logs\startup.out.log"

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null

$Content = @"
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$StartScript" >> "$StartupLog" 2>&1
"@

Set-Content -Path $CmdPath -Value $Content -Encoding ASCII
Write-Host "[ok] startup command installed: $CmdPath"
Write-Host "[ok] it will run when the current Windows user logs in."
