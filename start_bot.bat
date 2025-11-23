@echo off
title Funding Arbitrage Bot V3

:: 1. Setze Windows-Konsole auf UTF-8 (verhindert Emoji-Crashes)
chcp 65001 > NUL

:: 2. Zwinge Python zu UTF-8 Output
set PYTHONIOENCODING=utf-8

:: 3. Gehe sicher in das Verzeichnis der .bat Datei
cd /d "%~dp0"

echo ==================================================
echo    FUNDING ARBITRAGE BOT - STARTUP
echo ==================================================

:: 4. Prüfe, ob venv existiert
if not exist ".venv\Scripts\activate.bat" (
    echo [FEHLER] .venv nicht gefunden! Bitte 'python -m venv .venv' ausfuehren.
    pause
    exit /b
)

:: 5. Umgebung aktivieren & Bot starten
echo Aktiviere Virtual Environment...
call .venv\Scripts\activate.bat

echo Starte Monitor...
python scripts\monitor_funding.py

:: 6. Fenster offen halten bei Crash
if errorlevel 1 (
    echo.
    echo [ACHTUNG] Der Bot wurde mit einem Fehler beendet.
)
pause