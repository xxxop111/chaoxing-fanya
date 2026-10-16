@echo off
rem ==========================================================================
rem  超星学习通助手 —— 一键安装并配置
rem
rem  做三件事：
rem    1) 静默安装安装包（显示进度条，不弹向导）
rem    2) 把你已经配好的 web_config.json 复制进安装目录（免重新登录、免重填密钥）
rem    3) 启动程序
rem
rem  为什么必须你自己双击：从 DSH 界面/文件卡片里启动的进程是「受限令牌」，
rem  安装器连自己的临时目录都建不了（Error 5 拒绝访问）。你自己的会话才是正常令牌。
rem
rem  调试钩子（一般用不到）：
rem    set CX_DEST=...        改安装/配置的目标目录
rem    set CX_SKIP_INSTALL=1  只写配置，不安装
rem    set CX_NO_LAUNCH=1     装完不启动
rem ==========================================================================
setlocal
set "ROOT=%~dp0"
set "SETUP=%ROOT%dist\installer\chaoxing-fanya-setup-1.0.0.exe"
set "DEST=%LOCALAPPDATA%\Programs\ChaoxingFanya"
set "EXE=%DEST%\chaoxing-fanya.exe"
set "CFG=%ROOT%installer\web_config.json"
if not exist "%CFG%" set "CFG=%ROOT%web_config.json"
if defined CX_DEST set "DEST=%CX_DEST%"
if defined CX_DEST set "EXE=%CX_DEST%\chaoxing-fanya.exe"

echo.
echo [1/4] 检查文件
if not exist "%SETUP%" goto :no_setup
if not exist "%CFG%" goto :no_cfg
echo      安装包：%SETUP%
echo      配置：  %CFG%

if not defined CX_SKIP_INSTALL goto :maybe_install
echo [2/4] CX_SKIP_INSTALL 已设置，跳过安装
goto :configure

:maybe_install
if not exist "%EXE%" goto :install
echo [2/4] 已检测到安装：%EXE%
tasklist /fi "imagename eq chaoxing-fanya.exe" 2>nul | find /i "chaoxing-fanya.exe" >nul
if not errorlevel 1 (
    echo      程序正在运行，先让它退出（等价于托盘菜单的「退出」）...
    "%EXE%" --stop
    timeout /t 3 /nobreak >nul
)
echo      跳过安装，只更新配置
goto :configure

:install
set "LOG=%TEMP%\chaoxing-fanya-install.log"
echo [2/4] 正在安装（进度条走完前请不要关窗口）...
echo      日志：%LOG%
"%SETUP%" /SILENT /SUPPRESSMSGBOXES /NORESTART /SP- /LOG="%LOG%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :install_failed
echo      安装器返回 0，等待文件落盘 ...
set /a WAIT=0
:wait_exe
if exist "%EXE%" goto :configure
set /a WAIT+=1
if %WAIT% GEQ 60 goto :wait_failed
timeout /t 2 /nobreak >nul
goto :wait_exe

:configure
echo [3/4] 写入配置
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%CFG%" "%DEST%\web_config.json" >nul
if errorlevel 1 goto :copy_failed
echo      已写入：%DEST%\web_config.json

if defined CX_NO_LAUNCH goto :done
echo [4/4] 启动程序
start "" "%EXE%"
echo.
echo 完成：程序已启动，登录信息与 AI 密钥都已就位。
echo 提示：关窗只是缩到托盘（后台继续跑）；要退出用托盘右键菜单，
echo       或在命令行里跑 "%EXE%" --stop
echo.
pause
exit /b 0

:done
echo.
echo 完成（CX_NO_LAUNCH 已设置，未启动程序）。
exit /b 0

:no_setup
echo      找不到安装包：%SETUP%
goto :fail

:no_cfg
echo      找不到配置文件（installer\web_config.json 或根目录 web_config.json）
goto :fail

:install_failed
echo      安装器返回 %RC%，日志：%LOG%
echo      如果是 Error 5 / 拒绝访问：见 docs\DESKTOP_BUILD.md 第 5.1 节
goto :fail

:wait_failed
echo      安装器返回 0，但一直等不到 %EXE%
goto :fail

:copy_failed
echo      配置复制失败：%DEST%\web_config.json
goto :fail

:fail
echo.
echo 出错了：把上面的信息（以及日志）发给助手。
echo.
pause
exit /b 1
