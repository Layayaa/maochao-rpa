@echo off
set SCRIPT_DIR=%~dp0
for %%I in ("%SCRIPT_DIR%..\..") do set PROJECT_ROOT=%%~fI
cd /d "%PROJECT_ROOT%"
if not exist logs mkdir logs
".venv\Scripts\python.exe" -u api_server.py >> "logs\api_server.out.log" 2>> "logs\api_server.err.log"
