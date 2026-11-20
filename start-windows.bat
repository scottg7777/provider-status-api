@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (
  echo Python 3 is required. Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv
)

call .venv\Scripts\activate.bat
echo Checking dependencies...
python -m pip install -q -r requirements.txt

if not exist .env if "%STEDI_API_KEY%"=="" (
  echo.
  set /p STEDI_INPUT=Optional: Paste your Stedi API key (saved locally to .env), or press Enter to skip: 
  if not "%STEDI_INPUT%"=="" (
    echo STEDI_API_KEY=%STEDI_INPUT%> .env
    echo Saved STEDI_API_KEY to .env
  )
)

echo.
echo Starting Provider Status App at http://127.0.0.1:8080 ...
echo Keep this window open while using the app. Press Ctrl+C to stop.
start "" http://127.0.0.1:8080
python -m uvicorn app:app --host 127.0.0.1 --port 8080
