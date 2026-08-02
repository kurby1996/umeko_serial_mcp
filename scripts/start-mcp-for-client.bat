@echo off
setlocal
cd /d "%~dp0.."

REM MCP stdio launcher for AI clients (Cursor / Codex / Claude).
REM Prefer pointing the client directly to start-serial-mcp.exe if possible.

set "PY=%CD%\.venv\Scripts\python.exe"
set "EXE=%CD%\.venv\Scripts\start-serial-mcp.exe"

if exist "%EXE%" goto run_exe
if exist "%PY%" goto run_py

echo [ERROR] venv not found. Run: uv sync 1>&2
exit /b 1

:run_exe
"%EXE%" %*
exit /b %ERRORLEVEL%

:run_py
"%PY%" -m umeko_serial_mcp %*
exit /b %ERRORLEVEL%
