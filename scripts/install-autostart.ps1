# 将串口 Web 监控面板加入当前用户「开机启动」
# 用法（PowerShell）:
#   cd "F:\AI\串口MCP\umeko_serial_mcp"
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
# 取消:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1 -Remove

param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BatPath = Join-Path $PSScriptRoot "start-dashboard.bat"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "Umeko Serial MCP Dashboard.lnk"

if (-not (Test-Path $BatPath)) {
    Write-Error "找不到启动脚本: $BatPath"
}

if ($Remove) {
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
        Write-Host "已取消开机自启: $ShortcutPath"
    } else {
        Write-Host "未找到开机快捷方式，无需取消。"
    }
    exit 0
}

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $BatPath
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.WindowStyle = 7  # 最小化
$Shortcut.Description = "Umeko Serial MCP Web 监控面板"
$Shortcut.Save()

Write-Host "已设置开机自启:"
Write-Host "  快捷方式: $ShortcutPath"
Write-Host "  目标脚本: $BatPath"
Write-Host ""
Write-Host "登录后将自动启动监控面板: http://127.0.0.1:8080"
Write-Host "取消自启请执行: .\scripts\install-autostart.ps1 -Remove"
