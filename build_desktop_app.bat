@echo off
REM ============================================================
REM  Build the standalone desktop app (PyInstaller) into
REM  dist\chaoxing-fanya\ and (optionally) deploy it to the Desktop.
REM
REM  This file is intentionally ASCII-only: cmd.exe parses batch
REM  files with the OEM codepage (cp936 on zh-CN Windows), so a
REM  UTF-8 file containing Chinese gets split mid-command.
REM
REM  Usage:
REM      build_desktop_app.bat            :: build only
REM      build_desktop_app.bat deploy     :: build + copy to Desktop
REM ============================================================
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "DIST=%ROOT%dist\chaoxing-fanya"

if not exist "%PY%" (
    echo [ERROR] virtualenv python not found: %PY%
    echo         Run start.bat once, or create .venv and install requirements.txt
    pause
    exit /b 1
)

echo [1/4] Building frontend (web/dist) ...
pushd "%ROOT%web"
call npm run build
if errorlevel 1 (
    echo [ERROR] frontend build failed
    popd
    pause
    exit /b 1
)
popd

echo [2/4] Ensuring PyInstaller is installed ...
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo     installing pyinstaller ...
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] failed to install pyinstaller
        pause
        exit /b 1
    )
)

echo [3/4] Packaging desktop app ...
"%PY%" -m PyInstaller --noconfirm --clean --onedir --console ^
    --name chaoxing-fanya ^
    --add-data "web/dist;web/dist" ^
    --add-data "resource;resource" ^
    --hidden-import flask_cors --hidden-import loguru ^
    --hidden-import httpx --hidden-import pyaes ^
    --exclude-module paddleocr --exclude-module paddle ^
    --exclude-module ddddocr --exclude-module matplotlib ^
    --exclude-module PyQt5 ^
    "%ROOT%desktop_app.py"
if errorlevel 1 (
    echo [ERROR] packaging failed
    pause
    exit /b 1
)

echo [4/4] Build complete: %DIST%

if /i "%~1"=="deploy" (
    set "TARGET=%USERPROFILE%\Desktop\chaoxing-fanya-app"
    echo     deploying to: %TARGET%
    if exist "%TARGET%" rd /s /q "%TARGET%"
    mkdir "%TARGET%"
    xcopy /e /i /y /q "%DIST%\*" "%TARGET%\" >nul
    echo     deployed. Run %TARGET%\chaoxing-fanya.exe
)

echo.
echo Done. Start it with:  "%DIST%\chaoxing-fanya.exe"
echo Stop a running instance with:  "%DIST%\chaoxing-fanya.exe" --stop
pause
