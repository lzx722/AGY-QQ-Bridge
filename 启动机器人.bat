@echo off
chcp 65001 >nul
cd /d "%~dp0windows"
python agy_qq_bridge_win.py
pause
