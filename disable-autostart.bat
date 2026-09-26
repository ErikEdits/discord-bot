@echo off
REM Turns OFF autostart. The bot will no longer launch on its own -
REM you start it manually with start-bot.bat.
title Disable Bot Autostart
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1" -Action disable
echo.
pause
