# start_bot.ps1
Set-Location $PSScriptRoot
& .\.venv\Scripts\Activate.ps1
python scripts\monitor_funding.py
pause