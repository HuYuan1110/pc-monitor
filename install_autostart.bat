@echo off
chcp 936 >nul
title PC Monitor - 开机自启
cd /d "%~dp0"

rem ---- 需要管理员权限 ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 需要管理员权限。
    echo        请右键本脚本，选择"以管理员身份运行"。
    echo.
    pause
    exit /b 1
)

rem ---- 查找 Python ----
set "PY="
if exist "%LocalAppData%\Python\bin\python.exe" set "PY=%LocalAppData%\Python\bin\python.exe"
if not defined PY set "PY=python"

set "AGENT=%~dp0agent.py"

echo 正在注册开机自启任务（任务名：PCMonitor）...
powershell -NoProfile -Command "$a=New-ScheduledTaskAction -Execute '%PY%' -Argument '\"%AGENT%\"'; $t=New-ScheduledTaskTrigger -AtStartup; Register-ScheduledTask -TaskName 'PCMonitor' -Action $a -Trigger $t -RunLevel Highest -Force"

if errorlevel 1 (
    echo [错误] 注册失败。
    pause
    exit /b 1
)

echo.
echo 完成！下次开机将自动启动监测服务。
echo 取消自启：双击 uninstall_autostart.bat
echo.
pause
