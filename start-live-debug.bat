@echo off
setlocal EnableDelayedExpansion

title Funding Arbitrage Bot (LIVE DEBUG)
color 0E

echo ========================================================
echo   FUNDING ARBITRAGE BOT - DEBUG MODE
echo ========================================================
echo.

:: Change to project root
cd /d "%~dp0"
echo [DEBUG] Working directory: %CD%
echo.

:: ========================================================
:: STEP 1: CHECK PYTHON
:: ========================================================

echo [1/7] Pruefe Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python ist nicht installiert oder nicht im PATH!
    echo [INFO] Installiere Python von https://python.org
    pause
    exit /b 1
)
python --version
echo [OK] Python gefunden
echo.

:: ========================================================
:: STEP 2: CHECK VENV
:: ========================================================

echo [2/7] Pruefe Virtual Environment...
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Erstelle Virtual Environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Konnte venv nicht erstellen!
        pause
        exit /b 1
    )
)

echo [OK] Venv existiert
call venv\Scripts\activate.bat
echo [OK] Venv aktiviert
echo.

:: ========================================================
:: STEP 3: CHECK PYTHON IN VENV
:: ========================================================

echo [3/7] Pruefe Python im venv...
venv\Scripts\python.exe --version
if errorlevel 1 (
    echo [ERROR] Python im venv funktioniert nicht!
    pause
    exit /b 1
)
echo [OK] Python im venv funktioniert
echo.

:: ========================================================
:: STEP 4: CHECK INSTALLATION
:: ========================================================

echo [4/7] Pruefe funding_bot Installation...
venv\Scripts\python.exe -c "import funding_bot; print('funding_bot version:', funding_bot.__version__)" 2>&1
if errorlevel 1 (
    echo [WARN] funding_bot nicht installiert oder Fehler beim Import
    echo [INFO] Installiere funding_bot...
    venv\Scripts\pip.exe install -e ".[dev]"
    if errorlevel 1 (
        echo [ERROR] Installation fehlgeschlagen!
        pause
        exit /b 1
    )
)
echo [OK] funding_bot installiert
echo.

:: ========================================================
:: STEP 5: CHECK SDKs
:: ========================================================

echo [5/7] Pruefe Exchange SDKs...
venv\Scripts\python.exe -c "import lighter" 2>&1
if errorlevel 1 (
    echo [INFO] Lighter SDK nicht gefunden, installiere...
    venv\Scripts\pip.exe install git+https://github.com/elliottech/lighter-python.git
)

venv\Scripts\python.exe -c "import x10_python_trading" 2>&1
if errorlevel 1 (
    echo [INFO] X10 SDK nicht gefunden, installiere...
    venv\Scripts\pip.exe install x10-python-trading-starknet
)
echo [OK] SDKs installiert
echo.

:: ========================================================
:: STEP 6: CHECK .env
:: ========================================================

echo [6/7] Pruefe .env Datei...
if not exist ".env" (
    echo [ERROR] .env Datei nicht gefunden!
    if exist ".env.example" (
        echo [INFO] Kopiere .env.example nach .env
        copy .env.example .env
        echo [WARN] Bitte .env Datei editieren und API Keys eintragen!
    )
    pause
    exit /b 1
)
echo [OK] .env Datei gefunden
echo.

:: ========================================================
:: STEP 7: TEST RUN (DRY-RUN)
:: ========================================================

echo [7/7] Teste Bot Start (ohne Live-Trading)...
echo [INFO] Dies ist ein TEST um zu sehen ob der Bot ueberhaupt startet
echo.

venv\Scripts\python.exe -m funding_bot run --help
if errorlevel 1 (
    echo.
    echo [ERROR] Bot help command fehlgeschlagen!
    echo [INFO] Das deutet auf ein Import-Problem hin
    echo.
    echo [DEBUG] Versuche Import-Test...
    venv\Scripts\python.exe -c "from funding_bot.app.supervisor.lifecycle import main; print('Import OK')"
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   [OK] BOT KANN STARTEN
echo ========================================================
echo.

:: ========================================================
:: CONFIRMATION
:: ========================================================

echo Du bist dabei den Bot im LIVE MODUS zu starten.
echo.
echo Der Bot wird jetzt gestartet mit vollem Debug-Output.
echo ALLE Ausgaben werden auf der Konsole angezeigt.
echo.
set /p confirm="Moechtest du wirklich fortfahren? (JA um fortzufahren): "

if /i not "%confirm%"=="JA" (
    echo.
    echo [ABGEBROCHEN]
    pause
    exit /b 0
)

:: ========================================================
:: START BOT WITH FULL DEBUG
:: ========================================================

echo.
echo ========================================================
echo   STARTE BOT MIT VOLLEM DEBUG
echo ========================================================
echo.

venv\Scripts\python.exe -m funding_bot run --env prod --live --confirm-live

:: ========================================================
:: POST-RUN
:: ========================================================

set EXIT_CODE=%errorlevel%

echo.
echo ========================================================
echo   BOT BEENDET - EXIT CODE: %EXIT_CODE%
echo ========================================================
echo.

if %EXIT_CODE% NEQ 0 (
    echo [ERROR] Bot ist abgestuerzt!
    echo.
    echo [DEBUG] Haeufige Ursachen:
    echo   1. Fehlende oder falsche API Keys in .env
    echo   2. Netzwerk-Probleme
    echo   3. Falsche Konfiguration
    echo.
    echo [DEBUG] Pruefe .env:
    findstr /B "LIGHTER_API_KEY X10_" .env
    echo.
) else (
    echo [OK] Bot wurde normal beendet
)

echo.
pause
