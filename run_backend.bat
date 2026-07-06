@echo off
setlocal
chcp 65001 >nul

REM Force UTF-8 for Python file I/O on Windows.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
REM Keep the existing one-command local workflow. Production should leave this
REM false and run run_worker.bat as an independent process.
set "EMBEDDED_TASK_WORKER=true"

REM Disable LangSmith tracing for local development.
set "LANGCHAIN_TRACING_V2=false"
set "LANGSMITH_TRACING=false"
set "LANGCHAIN_API_KEY="
set "LANGSMITH_API_KEY="

cd /d "%~dp0backend"
if errorlevel 1 exit /b 1

python -X utf8 -m pip install -e .
if errorlevel 1 exit /b 1

python -X utf8 -m agent.db.init_db
if errorlevel 1 exit /b 1

python -X utf8 -c "from langgraph_cli.cli import cli; cli()" dev --no-browser --no-reload --allow-blocking

endlocal
