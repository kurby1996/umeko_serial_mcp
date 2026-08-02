@echo off
setlocal
cd /d "%~dp0.."

REM Always-on Serial Hub (web + API + COM). Keep this window open.
REM Browser: http://127.0.0.1:8080
REM Codex MCP connects to this Hub; do NOT open COM in a second process.

if not defined SERIAL_MCP_ENCODING set SERIAL_MCP_ENCODING=gbk
if not defined SERIAL_MCP_HTTP_PORT set SERIAL_MCP_HTTP_PORT=8080
if not defined SERIAL_MCP_HOST set SERIAL_MCP_HOST=127.0.0.1

set "PY=%CD%\.venv\Scripts\python.exe"
set "HUB=%CD%\.venv\Scripts\start-serial-hub.exe"

if exist "%PY%" goto run_py
if exist "%HUB%" goto run_hub

echo [ERROR] venv not found. Run: python -m pip install -e .
echo Expected: %PY%
pause
exit /b 1

:run_py
"%PY%" -m umeko_serial_mcp --hub --host %SERIAL_MCP_HOST% --http-port %SERIAL_MCP_HTTP_PORT%
goto end

:run_hub
"%HUB%"
goto end

:end
if errorlevel 1 pause
endlocal
