@echo off
set SCRIPT_DIR=%~dp0
for %%I in ("%SCRIPT_DIR%..\..") do set PROJECT_ROOT=%%~fI
cd /d "%PROJECT_ROOT%"
if not exist logs mkdir logs
".venv\Scripts\python.exe" -u rpa_worker.py >> "logs\rpa_worker.out.log" 2>> "logs\rpa_worker.err.log"
