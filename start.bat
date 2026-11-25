@echo off
chcp 936 >nul
title PC Monitor
cd /d "%~dp0"

echo ============================================
echo   电脑监测代理 - 启动器
echo ============================================
echo.

rem ---- 1. 查找可用的 Python ----
set "PY="
if exist "%LocalAppData%\Python\bin\python.exe" set "PY=%LocalAppData%\Python\bin\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PY=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"

if not defined PY (
    echo [错误] 没有找到 Python。
    echo        请先到 https://www.python.org/downloads/ 下载安装，
    echo        安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

echo [1/3] 使用 Python: %PY%
echo [2/3] 检查依赖（首次会自动安装，约 10~60 秒）...
"%PY%" -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

echo [3/3] 启动监测服务...
echo.
echo   启动后，手机浏览器访问下方显示的  http://192.168.x.x:8888
echo   关闭本窗口即停止监测。按 Ctrl+C 也可停止。
echo.
"%PY%" agent.py %*

echo.
echo 监测已停止。
pause
