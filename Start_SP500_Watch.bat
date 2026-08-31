@echo off
cd /d "%~dp0"
echo Starting S&P 500 50/200 watch...
python "%~dp0sp500_sma_watch.py" --autoscan
if errorlevel 1 pause
