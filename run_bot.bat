@echo off
color 0A
title Funding Bot Auto-Restarter

:start
echo.
echo ╔═══════════════════════════════════════════════════════╗
echo ║               Funding-Bot wird gestartet...           ║
echo ║          %date% %time%                      ║
echo ╚═══════════════════════════════════════════════════════╝
echo.

python scripts\monitor_funding.py

echo.
echo ╔═══════════════════════════════════════════════════════╗
echo ║   Bot abgebrochen oder beendet – starte in 10 Sekunden neu...   ║
echo ║          %date% %time%                      ║
echo ╚═══════════════════════════════════════════════════════╝
echo.

timeout /t 10 >nul
goto start