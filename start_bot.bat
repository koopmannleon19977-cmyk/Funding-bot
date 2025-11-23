@echo off
title Funding Bot V4
chcp 65001 > NUL
cd /d "%~dp0"

echo ========================================
echo   FUNDING BOT V4 - AUTO START
echo ========================================
echo.

REM Generate a RUN_ID with COMPUTERNAME + timestamp + random suffix
for /f "usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"`) do set TS=%%t
set RUN_ID=%COMPUTERNAME%_%TS%_%RANDOM%
set RUN_ID=%RUN_ID:"=%
echo RUN_ID=%RUN_ID%

call .venv\Scripts\activate.bat
python scripts\monitor_funding_final.py

if errorlevel 1 (
    echo.
    echo [ERROR] Bot crashed!
    pause
)