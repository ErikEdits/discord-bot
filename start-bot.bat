@echo off
REM ===========================================================
REM  ErikEdits Discord Bot - Windows start script
REM  Double-click this file to run the bot on this PC.
REM  No Docker needed - just Python.
REM ===========================================================
title ErikEdits Discord Bot
cd /d "%~dp0"

REM --- 1) Find Python (py launcher preferred, then python) ---
set "PY="
where py >nul 2>nul
if %errorlevel% equ 0 set "PY=py"
if defined PY goto have_python
where python >nul 2>nul
if %errorlevel% equ 0 set "PY=python"
if defined PY goto have_python
goto no_python

:have_python
REM --- 2) Install dependencies only if something is missing ---
%PY% -c "import discord, fastapi, uvicorn, jinja2, psutil, aiohttp, dotenv" >nul 2>nul
if %errorlevel% equ 0 goto run
echo.
echo ============================================================
echo   First run: installing dependencies (needs internet)...
echo ============================================================
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if %errorlevel% neq 0 goto pip_failed

:run
cls
echo ============================================================
echo   ErikEdits Discord Bot is starting...
echo.
echo   Web panel:  http://localhost:8080   (password in .env)
echo   To STOP the bot: just close this window.
echo ============================================================
echo.
%PY% bot.py
echo.
echo ------------------------------------------------------------
echo  Bot stopped. Restarting in 5 seconds.
echo  Close this window (or press Ctrl+C) to quit for good.
echo ------------------------------------------------------------
timeout /t 5 >nul
goto run

:no_python
echo.
echo  Python is NOT installed on this PC.
echo.
echo   1) Download it from:  https://www.python.org/downloads/
echo   2) IMPORTANT: during setup, tick "Add python.exe to PATH"
echo   3) Then double-click this file again.
echo.
pause
exit /b 1

:pip_failed
echo.
echo  Installing dependencies failed.
echo  Check your internet connection and the error above,
echo  then run this file again.
echo.
pause
exit /b 1
