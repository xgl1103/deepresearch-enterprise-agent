@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
cd /d "%~dp0backend"
if errorlevel 1 exit /b 1
python -X utf8 -m agent.worker
endlocal
