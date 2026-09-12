@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m agy_qq_bridge
pause
