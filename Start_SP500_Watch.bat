@echo off
title S&P 500 SMA Watch
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
  echo Installing packages if needed...
  py -3 -m pip install -q -r requirements.txt
  echo Starting...
  py -3 sp500_sma_watch.py
  goto done
)

where python >nul 2>&1
if %errorlevel%==0 (
  echo Installing packages if needed...
  python -m pip install -q -r requirements.txt
  echo Starting...
  python sp500_sma_watch.py
  goto done
)

echo Install Python 3 from https://www.python.org/downloads/
echo During setup, check "Add python.exe to PATH".
pause
exit /b 1

:done
pause
