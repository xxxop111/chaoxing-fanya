@echo off
title chaoxing-fanya (venv)
setlocal

REM ============================================================
REM  Differences from the bundled start.bat:
REM    1. always uses the project-local .venv Python;
REM    2. never auto-installs paddlepaddle / paddlex;
REM    3. serves the prebuilt frontend from web\dist, so one
REM       single process is enough (no npm run dev needed).
REM    4. refuses to start a second instance (see port guard).
REM  Frontend hot-reload instead:
REM       cd web  ^&^&  npm run dev      (port 3000)
REM
REM  This file is intentionally ASCII-only.  cmd.exe parses batch
REM  files with the OEM codepage (cp936 on zh-CN Windows), so a
REM  UTF-8 file containing Chinese text gets split mid-command.
REM ============================================================

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set CHAOXING_ENABLE_OCR=1

if not exist "%PY%" (
    echo [ERROR] virtualenv not found:
    echo         %PY%
    echo.
    echo Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Windows lets a second process bind port 5000 anyway (SO_REUSEADDR),
REM which would silently split requests between two instances. Refuse it.
netstat -ano | findstr /c:"LISTENING" | findstr /c:":5000 " >nul 2>&1
if not errorlevel 1 (
    echo [WARN] port 5000 is already in use - an instance looks active.
    echo.
    echo        Just open: http://localhost:5000
    echo        To restart, stop the other console window first.
    echo.
    pause
    exit /b 0
)

if not exist "%ROOT%web\dist\index.html" (
    echo [WARN] web\dist not found - API service only.
    echo        For the UI: cd web ^&^& npm install ^&^& npm run build
    echo.
)

echo ========================================
echo   chaoxing-fanya - starting
echo   URL  : http://localhost:5000
echo   Stop : Ctrl+C in this window
echo ========================================
echo.

REM open the browser a few seconds later, without blocking the server
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:5000'"

cd /d "%ROOT%"
"%PY%" app.py

echo.
echo Server stopped.
pause
