# Bot Panel launcher.
# Opens an SSH tunnel to the bot's web panel (port closed to the internet) and
# launches the browser. Reuses an existing tunnel if one is already running.

$ErrorActionPreference = 'SilentlyContinue'
$PanelUrl = 'http://localhost:8080'
$Server   = 'root@185.233.107.234'
$Key      = "$env:USERPROFILE\.ssh\id_ed25519"

function Test-Panel {
    try {
        $r = Invoke-WebRequest -Uri "$PanelUrl/login" -UseBasicParsing -TimeoutSec 3
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# Already reachable? Just open the browser.
if (Test-Panel) {
    Start-Process $PanelUrl
    return
}

# Start the SSH tunnel hidden, in its own process so it survives this script exiting.
Start-Process ssh -WindowStyle Hidden -ArgumentList @(
    '-o','ExitOnForwardFailure=yes',
    '-o','StrictHostKeyChecking=no',
    '-o','ServerAliveInterval=30',
    '-i', $Key,
    '-N','-L','8080:localhost:8080',
    $Server
)

# Wait up to ~15s for the tunnel to come up, then open the browser.
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Panel) { break }
}

if (Test-Panel) {
    Start-Process $PanelUrl
} else {
    [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null
    [System.Windows.Forms.MessageBox]::Show(
        "Could not reach the bot panel. Is the server up? (185.233.107.234)",
        "Bot Panel", 0, 16) | Out-Null
}
