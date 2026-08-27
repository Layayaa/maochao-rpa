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

function Test-ApiOnline {
    try {
        $response = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 3
        return $response.status -eq "ok"
    } catch {
        return $false
    }
}

function Test-WorkerOnline {
    try {
        $response = Invoke-RestMethod "http://127.0.0.1:8000/api/worker" -TimeoutSec 3
        return $response.worker_online -eq $true
    } catch {
        return $false
    }
}

function Test-InteractiveSession {
    try {
        $sessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
        return $sessionId -gt 0
    } catch {
        return $false
    }
}

function Start-MaochaoProcess {
    param(
        [string]$Name,
        [string]$ScriptName,
        [string]$OutLog,
        [string]$ErrLog
    )

    if ($Name -eq "api" -and (Test-ApiOnline)) {
        Write-Host "[ok] api already online"
        return
    }
    if ($Name -eq "worker" -and (Test-WorkerOnline)) {
        Write-Host "[ok] worker already online"
        return
    }
    if ($Name -eq "worker" -and !(Test-InteractiveSession)) {
        Write-Host "[skip] worker was not started because this shell is not an interactive desktop session."
        Write-Host "[hint] Log in to the Windows desktop and double-click Desktop\Start_Maochao_RPA_Visible_Worker.cmd."
        return
    }

    $ScriptPath = Join-Path $ProjectRoot $ScriptName
    Start-Process `
        -FilePath $Python `
        -ArgumentList @("-u", $ScriptPath) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Minimized `
        -RedirectStandardOutput (Join-Path $LogDir $OutLog) `
        -RedirectStandardError (Join-Path $LogDir $ErrLog)
    Write-Host "[ok] started $Name"
}

$ErrorActionPreference = "Stop"
$ProjectRoot = Get-ProjectRoot

Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"

if (!(Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

foreach ($Dir in @("logs", "data", "downloads", "browser_profiles", "backend")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $Dir) | Out-Null
}

Start-MaochaoProcess "api" "api_server.py" "api_server.out.log" "api_server.err.log"
Start-MaochaoProcess "worker" "rpa_worker.py" "rpa_worker.out.log" "rpa_worker.err.log"
