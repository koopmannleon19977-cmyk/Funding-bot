@echo off
setlocal EnableDelayedExpansion

title Funding Arbitrage Bot (PAPER)
color 0A

echo ========================================================
echo   FUNDING ARBITRAGE BOT - QUICK START
echo ========================================================
echo.
echo [INFO] Mode: PAPER TRADING (no real orders)
echo.

:: Change to project root
cd /d "%~dp0"

:: Check/Create venv
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv!
        pause
        exit /b 1
    )
)

:: Activate venv
call venv\Scripts\activate.bat

:: Install package if needed
python -c "import funding_bot" 2>nul
if errorlevel 1 (
    echo [INFO] Installing funding_bot...
    pip install -e ".[dev]"
)

:: Install SDKs if needed
python -c "import lighter" 2>nul
if errorlevel 1 (
    echo [INFO] Installing Lighter SDK...
    pip install git+https://github.com/elliottech/lighter-python.git
)

python -c "import x10_python_trading" 2>nul
if errorlevel 1 (
    echo [INFO] Installing X10 SDK...
    pip install x10-python-trading-starknet
)

echo.
echo [INFO] Starting bot in PAPER mode...
echo [INFO] Press Ctrl+C to stop
echo.

python -m funding_bot run --paper

if errorlevel 1 (
    echo.
    echo [ERROR] Bot crashed!
    pause
)
