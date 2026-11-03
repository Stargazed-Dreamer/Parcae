@echo off
chcp 65001 >nul 2>&1
title Parcae Server

REM --- Parcae backend launcher --------------------------------------------
REM Keep this file ASCII-only with CRLF line endings. cmd.exe on Chinese Windows
REM is extremely sensitive to encoding and line endings, and a broken .bat breaks
REM the whole startup. Before editing, read .agents/skills/bat_writing/SKILL.md.
REM The "chcp 65001" above is required and must not be removed: server/main.py
REM reconfigures its stdio to UTF-8, so the console code page has to match or
REM Chinese log output turns into garbage.
REM See docs/deployment.md section "start.bat" for the do-not-edit note.
REM ---------------------------------------------------------------------------

net session >nul 2>&1
if %errorLevel% == 0 (
    echo [OK] Admin privileges confirmed
    goto :run
)

echo [INFO] Requesting admin privileges...
powershell -Command "Start-Process '%~f0' -Verb RunAs"
exit /b

:run
cd /d "%~dp0"

REM Existing-instance handling is delegated to the supervisor when it exists:
REM it asks politely first (POST /shutdown -> full cleanup chain) and only
REM escalates to taskkill if that fails. The hardcoded kill below stays as the
REM fallback for machines without the supervisor file.
if not exist "tools\runtime\run_backend.py" goto :legacy_kill
echo [INFO] Existing instance (if any) will be shut down gracefully by the supervisor
goto :skip_kill

:legacy_kill
echo [INFO] Supervisor missing - using legacy hard kill
REM Kill any existing backend. The port may have shifted (Windows reserved
REM ranges can block the preferred port), so trust data\runtime\server.pid
REM first and only then fall back to whoever listens on the preferred port.
if exist "data\runtime\server.pid" (
    for /f "usebackq delims=" %%a in ("data\runtime\server.pid") do (
        tasklist /FI "PID eq %%a" 2>nul | find /I "python.exe" >nul
        if not errorlevel 1 (
            echo [INFO] Killing previous backend PID %%a ...
            taskkill /PID %%a /F >nul 2>&1
            timeout /t 2 /nobreak >nul
        )
    )
    del "data\runtime\server.pid" >nul 2>&1
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8766.*LISTENING"') do (
    echo [INFO] Killing old backend PID %%a ...
    taskkill /PID %%a /F >nul 2>&1
    timeout /t 2 /nobreak >nul
)

:skip_kill

REM UTF-8 stdio for Python logs. main.py reconfigures as well; setting it here
REM makes sure the Python process starts with UTF-8 stderr already in place.
set PYTHONIOENCODING=utf-8

echo [INFO] Starting Parcae...
REM The preferred port comes from config.toml [server] port. If Windows has
REM reserved it (WinError 10013), the server shifts to port+1, +2 ... and
REM prints the actual URL below. See data\runtime\server_port.json .
echo [INFO] API / MCP: actual URL is printed by the server below
echo [INFO] Exit monitoring ON (exit code / stderr / memory samples)
echo [INFO]   data\logs\backend_console_YYYYMMDD.log  full console output
echo [INFO]   data\logs\backend_mem_YYYYMMDD.jsonl     memory samples
echo [INFO]   data\logs\backend_exit.jsonl            exit records
echo.

REM Launch through the supervisor so a silent death still leaves evidence:
REM exit code + hex, uptime, peak commit, graceful-vs-killed verdict, and the
REM stderr traceback that was previously lost with the console window.
REM If the supervisor file is missing, fall back to the plain command - a broken
REM helper must never block startup.
if not exist "tools\runtime\run_backend.py" goto :run_plain
.venv\Scripts\python.exe tools\runtime\run_backend.py
goto :after_run

:run_plain
echo [WARN] tools\runtime\run_backend.py not found - running WITHOUT exit monitoring
.venv\Scripts\python.exe -m server.main

:after_run
echo.
echo [INFO] Backend process has exited. See data\logs\backend_exit.jsonl
echo [INFO] Postmortem: python tools\runtime\backend_postmortem.py
pause
