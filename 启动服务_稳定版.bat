@echo off
chcp 437 >nul
setlocal EnableDelayedExpansion
title Start PCMonitor Service (Stable)
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Please click YES on UAC prompt. Only once.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
echo [OK] Administrator
echo.

echo [1/3] Cleaning old agent process only (no FPS/PresentMon/ETW - removed in v3.1.4)...
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| find ":8888" ^| find "LISTENING"') do taskkill /F /PID %%P >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/3] Starting agent in a VISIBLE window (you can close it anytime)...
wscript "%~dp0start_visible.vbs"

echo [3/3] Checking service (retry up to 40s, agent may need time to load)...
set "OK="
for /L %%i in (1,1,20) do (
    if not defined OK (
        curl -s --max-time 3 "http://127.0.0.1:8888/api/status" > "%TEMP%\pc_status.json" 2>nul
        for %%F in ("%TEMP%\pc_status.json") do if %%~zF GTR 100 set "OK=1"
        if not defined OK timeout /t 2 /nobreak >nul
    )
)

if defined OK (
    echo   [OK] Service running. Details:
    findstr /i /c:"\"version\"" /c:"\"reason\"" "%TEMP%\pc_status.json" 2>nul
) else (
    echo   [!] Service did NOT respond within 40s. Real problem - check:
    echo       1) tasklist ^| find /i "python.exe"
    echo       2) python agent.py  (see the real error)
    tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /i "python.exe"
)
echo.
echo ============================================================
echo  A window titled "PCMonitor-Agent-RUNNING" is now open on
echo  the taskbar. That IS the service - keep it open while using
echo  the APP. To STOP the service, just CLOSE that window (or
echo  press Ctrl+C in it). No hidden background process anymore.
echo ============================================================
pause
