#!/usr/bin/env bash
# Mac / Linux — same job as Start_SP500_Watch.bat
set -e
cd "$(dirname "$0")"
echo "FinSense watcher — keep this window open."
echo "Page: http://127.0.0.1:8765/"
echo
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 is not installed."
  echo "Install from https://www.python.org/downloads/ or: brew install python"
  exit 1
fi
"$PY" -u sp500_sma_watch.py --autoscan
echo
echo "Watcher stopped."
read -r -p "Press Enter to close."
