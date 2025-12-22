@echo off
title Funding Bot V5 - Architected Clean Architecture
chcp 65001 > NUL
cd /d "%~dp0"

echo ========================================
echo   FUNDING BOT V5 - CLEAN ARCHITECTURE
echo ========================================
echo. 

REM --- LOGGING KONFIGURATION ---
for /f "usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"`) do set TS=%%t
set "RUN_ID=%TS%"

mkdir logs 2>nul

set "FULL_LOG=logs\funding_bot_%RUN_ID%_FULL.log"
set "ERROR_LOG=logs\%RUN_ID%_ERRORS.log"

REM === Feste Dateien (immer gleiche Namen, für schnellen Zugriff) ===
set "LATEST_LOG=logs\latest_run.log"
set "LATEST_ERROR=logs\latest_errors.log"

echo RUN_ID:    %RUN_ID%
echo Full Log:  %FULL_LOG%
echo Error Log: %ERROR_LOG%
echo Latest:    %LATEST_LOG%
echo. 

call .venv\Scripts\activate.bat

echo Testing Python path... & python --version
echo. 
if not exist main.py (
    echo ✗ Script NOT found! 
    pause
    exit /b 1
) else (
    echo ✓ Script found
)
echo. 

REM --- CACHE BEREINIGUNG ---
echo Cleaning Python cache (__pycache__ and .pyc files)...
powershell -NoProfile -Command "Get-ChildItem -Path . -Include __pycache__,*.pyc -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
echo ✓ Cache cleared
echo. 

echo Starting bot... Der Bot schreibt den vollständigen Log direkt in die Datei.
echo Press Ctrl+C to stop
echo. 

set BOT_LOG_FILE=%FULL_LOG%

python main.py

REM --- FEHLER-LOG ERSTELLUNG ---
echo. 
echo ========================================
echo   POST-RUN VERARBEITUNG
echo ========================================
echo. 

echo Filtering full log for errors and warnings... 
findstr /c:"[ERROR]" /c:"[CRITICAL]" /c:"[WARNING]" "%FULL_LOG%" > "%ERROR_LOG%" 2>NUL

REM === Kopiere in feste Dateien ===
echo Copying to latest logs...
copy /Y "%FULL_LOG%" "%LATEST_LOG%" >NUL
if exist "%ERROR_LOG%" (
    copy /Y "%ERROR_LOG%" "%LATEST_ERROR%" >NUL 2>NUL
)

echo.
echo Bot gestoppt. Exit code: %ERRORLEVEL%
echo. 
echo Logs erstellt:
echo   - Full Log:    %FULL_LOG%
echo   - Error Log:   %ERROR_LOG%
echo   - Latest:      %LATEST_LOG%
echo. 

REM === AUTOMATISCHER GIT UPLOAD (DEAKTIVIERT) ===
echo ========================================
echo   GIT AUTO-UPLOAD (DEAKTIVIERT)
echo ========================================
echo.
echo Hinweis: Logs/Exports/DBs bleiben lokal und werden nicht mehr automatisch nach Git gepusht.
echo Wenn du wirklich committen willst, mach das manuell (gezielt) via git add/commit.
echo.

:skip_git

echo.
pause