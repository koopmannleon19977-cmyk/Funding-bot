@echo off
title Funding Bot V4
chcp 65001 > NUL
cd /d "%~dp0"

echo ========================================
echo   FUNDING BOT V4 - AUTO START
echo ========================================
echo.

call .venv\Scripts\activate.bat
python scripts\monitor_funding_final.py

if errorlevel 1 (
    echo.
    echo [ERROR] Bot crashed!
    pause
)