@echo off
REM Turns ON autostart: the bot will launch automatically when Windows starts.
REM Run this once when you're done testing and want it always-on.
title Enable Bot Autostart
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1" -Action enable
echo.
pause
