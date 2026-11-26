@echo off
chcp 65001 >nul
REM ============================================================
REM  电脑监测代理 —— 一键打包成 Windows 单文件 exe
REM  用法：双击本脚本，产物在 dist\PCMonitor.exe
REM ============================================================
echo [1/3] 安装打包工具 PyInstaller...
pip install pyinstaller -q

echo [2/3] 打包中（单文件模式）...
cd /d "%~dp0.."
pyinstaller --onefile --noconsole --name PCMonitor agent.py

echo [3/3] 完成！
echo 产物位置：%~dp0..\dist\PCMonitor.exe
pause
