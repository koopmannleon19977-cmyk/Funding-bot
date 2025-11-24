@echo off
title Funding Bot V4 - WebSocket Test
chcp 65001 > NUL
cd /d "%~dp0"

echo ========================================
echo   FUNDING BOT V4 - WEBSOCKET TEST
echo ========================================
echo.

REM Generate a RUN_ID with COMPUTERNAME + timestamp + random suffix
for /f "usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"`) do set TS=%%t
set RUN_ID=%COMPUTERNAME%_%TS%_%RANDOM%
set RUN_ID=%RUN_ID:"=%
echo RUN_ID=%RUN_ID%
echo.

call .venv\Scripts\activate.bat

echo Testing Python path...
python --version
echo.

echo Testing script existence...
if exist scripts\monitor_funding_final.py (
    echo ✓ Script found
) else (
    echo ✗ Script NOT found!
    pause
    exit /b 1
)
echo.

echo Starting bot - output will appear in console...
echo Press Ctrl+C to stop
echo.

REM Run WITHOUT redirection to see what's happening
python scripts\monitor_funding_final.py

echo.
echo Bot stopped - Exit code: %ERRORLEVEL%
pause