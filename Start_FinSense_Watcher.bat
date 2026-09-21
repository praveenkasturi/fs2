@echo off
title FinSense Watcher
cd /d "%~dp0"
set "PY=python"
where python >nul 2>&1 || set "PY=C:\Python313\python.exe"
echo.
echo  FinSense watcher
echo  Keep this window open while you use the screen.
echo  Page: http://127.0.0.1:8765/
echo.
"%PY%" -u "%~dp0sp500_sma_watch.py" --no-open %*
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo  Watcher stopped with an error.
) else (
  echo  Watcher stopped.
)
pause
exit /b %ERR%
