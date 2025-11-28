@echo off
title Funding Bot V4 - WebSocket Test
chcp 65001 > NUL
cd /d "%~dp0"

echo ========================================
echo   FUNDING BOT V4 - WEBSOCKET TEST
echo ========================================
echo.

REM --- LOGGING KONFIGURATION ---
REM Generiert eine saubere RUN_ID (ohne Zufallszahl)
for /f "usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"` ) do set TS=%%t
set "RUN_ID=%COMPUTERNAME%_%TS%"
set RUN_ID=%RUN_ID:\"=%

REM Definiere Log-Pfade und erstelle den Ordner
mkdir logs 2>nul

REM WICHTIG: Python MUSS diesen Pfad in config.py (via os.environ['BOT_LOG_FILE']) als LOG_FILE verwenden.
set "FULL_LOG=logs\funding_bot_%RUN_ID%_FULL.log"
set "ERROR_LOG=logs\%RUN_ID%_ERRORS.log"

echo RUN_ID:    %RUN_ID%
echo Full Log:  %FULL_LOG%
echo Error Log: %ERROR_LOG%
echo.

call .venv\Scripts\activate.bat

echo Testing Python path... & python --version
echo.
if not exist scripts\monitor_funding_final.py (echo ✗ Script NOT found! & pause & exit /b 1) else echo ✓ Script found
echo.

echo Starting bot... Der Bot schreibt den vollständigen Log direkt in die Datei.
echo Press Ctrl+C to stop
echo.

REM Setze Environment Variable, das Python in config.py für den Log-Pfad nutzen MUSS.
set BOT_LOG_FILE=%FULL_LOG%

REM Führe den Bot aus. Der Bot muss den Log in die durch BOT_LOG_FILE definierte Datei schreiben.
python scripts\monitor_funding_final.py

REM --- FEHLER-LOG ERSTELLUNG (nachdem der Bot beendet wurde) ---
echo Filtering full log for errors and warnings...
REM Filtert alle Zeilen, die [ERROR], [CRITICAL] oder [WARNING] enthalten, und speichert sie im separaten Fehler-Log.
REM 2>NUL unterdrückt Fehlermeldungen, falls die Datei nicht existiert oder leer ist.
findstr /c:"[ERROR]" /c:"[CRITICAL]" /c:"[WARNING]" "%FULL_LOG%" > "%ERROR_LOG%" 2>NUL

echo.
echo Bot gestoppt. Exit code: %ERRORLEVEL%
echo Fehler-Log erstellt: %ERROR_LOG%
pause