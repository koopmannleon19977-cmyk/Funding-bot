@echo off
title Funding Bot V4 - WebSocket Test
chcp 65001 > NUL
cd /d "%~dp0"

echo ========================================
echo   FUNDING BOT V4 - WEBSOCKET TEST
echo ========================================
echo. 

REM --- LOGGING KONFIGURATION ---
for /f "usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"`) do set TS=%%t
set "RUN_ID=%COMPUTERNAME%_%TS%"
set RUN_ID=%RUN_ID:\"=%

mkdir logs 2>nul

set "FULL_LOG=logs\funding_bot_%RUN_ID%_FULL.log"
set "ERROR_LOG=logs\%RUN_ID%_ERRORS.log"

REM === Feste Dateien für Git-Tracking ===
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
if not exist src\main.py (
    echo ✗ Script NOT found! 
    pause
    exit /b 1
) else (
    echo ✓ Script found
)
echo. 

echo Starting bot...  Der Bot schreibt den vollständigen Log direkt in die Datei.
echo Press Ctrl+C to stop
echo. 

set BOT_LOG_FILE=%FULL_LOG%

python src\main.py

REM --- FEHLER-LOG ERSTELLUNG ---
echo. 
echo ========================================
echo   POST-RUN VERARBEITUNG
echo ========================================
echo. 

echo Filtering full log for errors and warnings... 
findstr /c:"[ERROR]" /c:"[CRITICAL]" /c:"[WARNING]" "%FULL_LOG%" > "%ERROR_LOG%" 2>NUL

REM === Kopiere in feste Dateien für Git ===
echo Copying to latest logs for Git tracking...
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
echo   - Git Latest:  %LATEST_LOG%
echo. 

REM === AUTOMATISCHER GIT UPLOAD ===
echo ========================================
echo   GIT AUTO-UPLOAD
echo ========================================
echo. 

REM Prüfe ob Git verfügbar ist
git --version >NUL 2>&1
if errorlevel 1 (
    echo ✗ Git ist nicht installiert! 
    goto :skip_git
)

echo Adding logs to Git staging area... 

REM WICHTIG: Beide Pfade hinzufügen! 
REM Logs nur aus dem logs Ordner hinzufügen
git add -f logs 2>NUL

REM Zeige was staged wurde
echo.
echo Staged files:
git diff --cached --name-only

REM Nur committen wenn Aenderungen da sind
git diff --cached --quiet
if errorlevel 1 (
    echo. 
    echo Committing log changes...
    git commit -m "Auto-upload: Bot log vom %date% %time%"
    
    echo Pushing to origin main...
    git push origin main
    
    if errorlevel 1 (
        echo ✗ Push fehlgeschlagen! 
    ) else (
        echo. 
        echo ✓ Logs erfolgreich hochgeladen!
    )
) else (
    echo.
    echo Keine neuen Log-Aenderungen zum Committen. 
)

:skip_git

echo.
pause