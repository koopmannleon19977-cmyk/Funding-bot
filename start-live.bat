@echo off
setlocal EnableDelayedExpansion

title Funding Arbitrage Bot (LIVE)
color 0E

:: ========================================================
::   FUNDING ARBITRAGE BOT - LIVE TRADING STARTUP
:: ========================================================
::   WARNUNG: Dieser Start-Script platziert ECHTE ORDERS!
::   Nur verwenden wenn:
::   - Config vollstaendig geprueft
::   - Risiko-Parameter verstanden
::   - .env Datei korrekt konfiguriert
:: ========================================================

echo.
echo ========================================================
echo   FUNDING ARBITRAGE BOT - LIVE TRADING
echo ========================================================
echo.
echo [!!!] WARNUNG: ECHTE GELD-TRANSKTIONEN MÖGLICH
echo [!!!] Druecke STRG+C zum Abbrechen
echo.
timeout /t 3 >nul

color 0C

:: Change to project root
cd /d "%~dp0"
echo [INFO] Working directory: %CD%
echo.

:: ========================================================
:: PRE-FLIGHT CHECKS
:: ========================================================

echo [1/7] Pruefe ob Bot bereits laeuft...
tasklist /FI "WINDOWTITLE eq Funding Arbitrage Bot*" 2>nul | find /I "python.exe" >nul
if not errorlevel 1 (
    echo [ERROR] Bot laeuft bereits! Bitte erst stoppen.
    pause
    exit /b 1
)
echo [OK] Kein laufender Bot gefunden.

echo.
echo [2/7] Pruefe .env Datei...
if not exist ".env" (
    echo [ERROR] .env Datei nicht gefunden!
    echo [INFO] Erstelle .env aus .env.example
    if exist ".env.example" (
        copy .env.example .env >nul
        echo [INFO] .env erstellt - bitte konfigurieren!
    )
    pause
    exit /b 1
)
echo [OK] .env Datei gefunden.

echo.
echo [3/7] Pruefe Virtual Environment...
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Erstelle Virtual Environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Konnte venv nicht erstellen!
        pause
        exit /b 1
    )
)
echo [OK] Virtual Environment bereit.

:: Activate venv
call venv\Scripts\activate.bat

echo.
echo [4/7] Pruefe Installation...
python -c "import funding_bot" 2>nul
if errorlevel 1 (
    echo [INFO] Installiere funding_bot...
    pip install -e ".[dev]" --quiet
    if errorlevel 1 (
        echo [ERROR] Installation fehlgeschlagen!
        pause
        exit /b 1
    )
)
echo [OK] Bot installiert.

echo.
echo [5/7] Pruefe Exchange SDKs...
python -c "import lighter" 2>nul
if errorlevel 1 (
    echo [INFO] Installiere Lighter SDK...
    pip install git+https://github.com/elliottech/lighter-python.git --quiet
)

python -c "import x10_python_trading" 2>nul
if errorlevel 1 (
    echo [INFO] Installiere X10 SDK...
    pip install x10-python-trading-starknet --quiet
)
echo [OK] SDKs installiert.

echo.
echo [6/7] Pruefe Logs Verzeichnis...
if not exist "logs" mkdir logs
echo [OK] Logs Verzeichnis bereit.

:: ========================================================
:: SAFETY CONFIRMATION
:: ========================================================

echo.
echo ========================================================
echo   SICHERHEITS-CHECK
echo ========================================================
echo.
echo Du bist dabei den Bot im LIVE MODUS zu starten.
echo.
echo Es werden ECHTE ORDERS platziert wenn:
echo   - live_trading=true in Config
echo   - API Keys korrekt konfiguriert
echo   - Guthaben vorhanden
echo.
echo Config pruefen: .env Datei editieren
echo.
set /p confirm="Moechtest du wirklich fortfahren? (JA um fortzufahren): "

if /i not "%confirm%"=="JA" (
    echo.
    echo [ABGEBROCHEN] Bot nicht gestartet.
    pause
    exit /b 0
)

:: ========================================================
:: START BOT
:: ========================================================

echo.
echo ========================================================
echo   STARTE LIVE BOT
echo ========================================================
echo.

:: Create timestamped log
set LOGFILE=logs\live_%date:~-4,4%%date:~-7,2%%date:~-10,2%_%time:~0,2%%time:~3,2%%time:~6,2%.log
set LOGFILE=%LOGFILE: =0%

echo [INFO] Logfile: %LOGFILE%
echo [INFO] Startzeit: %date% %time%
echo.

python -m funding_bot run --env prod --live --confirm-live 2>&1

:: ========================================================
:: POST-RUN
:: ========================================================

set BOT_EXIT_CODE=%errorlevel%

if %BOT_EXIT_CODE% NEQ 0 (
    color 4F
    echo.
    echo ========================================================
    echo   [ERROR] BOT CRASHED - EXIT CODE: %BOT_EXIT_CODE%
    echo ========================================================
    echo.
    echo Logfile: %LOGFILE%
    echo.
    echo [DEBUG] Letzte 20 Zeilen aus dem Log:
    echo --------------------------------------------------------
    powershell -Command "Get-Content %LOGFILE% -Tail 20"
    echo --------------------------------------------------------
    echo.
    echo [DEBUG] Haeufige Ursachen:
    echo   - Fehlende oder falsche API Keys in .env
    echo   - Netzwerk-Verbindungsprobleme
    echo   - Falsche Config-Parameter
    echo.
    echo [DEBUG] Pruefe .env Datei:
    if exist ".env" (
        echo   [OK] .env existiert
        echo   [INFO] Pruefe ob LIGHTER_API_KEY und X10_* gesetzt sind
        findstr /B "LIGHTER_API_KEY X10_" .env
    ) else (
        echo   [ERROR] .env Datei fehlt!
    )
    echo.
    pause
    exit /b %BOT_EXIT_CODE%
) else (
    color 0A
    echo.
    echo ========================================================
    echo   [OK] BOT GESTOPPT
    echo ========================================================
    echo.
    echo Logfile: %LOGFILE%
    echo Stoppzeit: %date% %time%
    echo.
    timeout /t 5
    exit /b 0
)
