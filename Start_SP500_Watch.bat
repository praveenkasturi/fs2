@echo off
title FinSense Watcher
cd /d "%~dp0"
set "PY=python"
where python >nul 2>&1 || set "PY=C:\Python313\python.exe"
echo Starting FinSense — keep this window open.
echo Page will open at http://127.0.0.1:8765/
echo.
"%PY%" -u "%~dp0sp500_sma_watch.py" --autoscan
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo Failed to start. Check that Python is installed.
) else (
  echo Watcher stopped.
)
pause
exit /b %ERR%
