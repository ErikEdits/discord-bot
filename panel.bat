@echo off
REM Opens an SSH tunnel to the bot's web panel and launches the browser.
REM The panel port (8080) is closed to the internet; this tunnel is the only way in.
REM Close this window (or press Ctrl+C) to disconnect the tunnel.

echo Connecting to the bot panel via SSH tunnel...
echo The browser will open at http://localhost:8080 in a moment.
echo Keep this window open while using the panel. Close it to disconnect.
echo.

REM Open the browser a few seconds after the tunnel comes up.
start "" /b cmd /c "timeout /t 3 >nul & start """" http://localhost:8080"

ssh -i "%USERPROFILE%\.ssh\id_ed25519" -N -L 8080:localhost:8080 root@185.233.107.234
