# Enables or disables Windows autostart for the bot by placing (or removing)
# a shortcut to start-bot.bat in the current user's Startup folder.
# Called by enable-autostart.bat / disable-autostart.bat.

param(
    [ValidateSet('enable', 'disable')]
    [string]$Action = 'enable'
)

$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup 'ErikEdits Bot.lnk'
$botBat  = Join-Path $PSScriptRoot 'start-bot.bat'

if ($Action -eq 'enable') {
    if (-not (Test-Path $botBat)) {
        Write-Host "ERROR: start-bot.bat was not found next to this script." -ForegroundColor Red
        exit 1
    }
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath        = $botBat
    $sc.WorkingDirectory  = $PSScriptRoot
    $sc.WindowStyle       = 7            # start minimized
    $sc.Description       = 'ErikEdits Discord Bot autostart'
    $sc.Save()
    Write-Host ""
    Write-Host "  Autostart ENABLED." -ForegroundColor Green
    Write-Host "  The bot will launch automatically when you log in to Windows."
    Write-Host "  Shortcut: $lnk"
    Write-Host ""
    Write-Host "  Note: this starts on user LOGIN. If nobody is logged in after a"
    Write-Host "  reboot, set this PC to auto-login for a truly unattended bot."
} else {
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Write-Host ""
        Write-Host "  Autostart DISABLED (startup shortcut removed)." -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "  Autostart was not enabled - nothing to remove."
    }
}
