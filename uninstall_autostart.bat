@echo off
chcp 936 >nul
title PC Monitor - 取消自启
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 需要管理员权限。
    echo        请右键本脚本，选择"以管理员身份运行"。
    echo.
    pause
    exit /b 1
)

echo 正在取消开机自启任务...
schtasks /Delete /TN "PCMonitor" /F

if errorlevel 1 (
    echo [提示] 未找到自启任务，可能尚未设置。
) else (
    echo 完成！已取消开机自启。
)
echo.
pause
