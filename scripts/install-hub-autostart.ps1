# Install Serial Hub to current user Startup folder (web stays available after login).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-hub-autostart.ps1
# Remove:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-hub-autostart.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BatPath = Join-Path $PSScriptRoot "start-hub.bat"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "Umeko Serial Hub.lnk"

if (-not (Test-Path $BatPath)) {
    Write-Error "Missing: $BatPath"
}

if ($Remove) {
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
        Write-Host "Removed autostart: $ShortcutPath"
    } else {
        Write-Host "No autostart shortcut found."
    }
    exit 0
}

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $BatPath
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Umeko Serial Hub (web + COM)"
$Shortcut.Save()

Write-Host "Autostart installed:"
Write-Host "  $ShortcutPath"
Write-Host "  -> $BatPath"
Write-Host "After login, open http://127.0.0.1:8080"
