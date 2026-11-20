#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
echo "Checking dependencies..."
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

if [ ! -f ".env" ] && [ -z "${STEDI_API_KEY:-}" ]; then
  echo ""
  echo "Optional: Paste your Stedi API key (saved locally to .env), or press Enter to skip:"
  read -r -s STEDI_INPUT
  echo ""
  if [ -n "$STEDI_INPUT" ]; then
    printf 'STEDI_API_KEY=%s
' "$STEDI_INPUT" > .env
    chmod 600 .env
    echo "Saved STEDI_API_KEY to .env"
  fi
fi

echo ""
echo "Starting Provider Status App at http://127.0.0.1:8080 ..."
echo "Keep this window open while using the app. Press Ctrl+C to stop."
(sleep 2 && open "http://127.0.0.1:8080") >/dev/null 2>&1 &
exec python -m uvicorn app:app --host 127.0.0.1 --port 8080
