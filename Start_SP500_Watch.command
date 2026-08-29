#!/bin/bash
# Double-click this file on a Mac to start the dashboard.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Install Python 3 from https://www.python.org/downloads/"
  echo "Then double-click this file again."
  read -r -p "Press Return to close..."
  exit 1
fi

echo "Setting up (first time can take a minute)..."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -r requirements.txt

echo "Starting..."
echo "Keep this window open. Close it when you are done."
python sp500_sma_watch.py
echo
read -r -p "Press Return to close..."
