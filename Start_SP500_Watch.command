#!/bin/bash
# Double-click on a Mac. Python installs packages by itself the first time.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Install Python 3 from https://www.python.org/downloads/"
  echo "Then double-click this file again."
  read -r -p "Press Return to close..."
  exit 1
fi
echo "Starting... keep this window open."
python3 sp500_sma_watch.py
echo
read -r -p "Press Return to close..."
