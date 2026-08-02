# Web serial dashboard only (no MCP stdio)
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\start-dashboard.ps1
# Browser: http://127.0.0.1:8080

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $env:SERIAL_MCP_ENCODING) {
    $env:SERIAL_MCP_ENCODING = "gbk"
}
$HttpPort = 8080

$Py  = Join-Path $Root ".venv\Scripts\python.exe"
$Exe = Join-Path $Root ".venv\Scripts\start-serial-mcp.exe"

if (Test-Path $Py) {
    & $Py -m umeko_serial_mcp --dashboard --http-port $HttpPort
} elseif (Test-Path $Exe) {
    & $Exe --dashboard --http-port $HttpPort
} else {
    Write-Error "venv not found. Run in project root: uv sync"
}
