@echo off
REM Windows quick start
cd /d "%~dp0"

echo === Goal Chat Agent ===
echo.

REM Check .env
if not exist .env (
    echo [!] No .env file found. Copy .env.example to .env and edit it.
    copy .env.example .env
    echo [^>] Created .env from example. Edit it with your API key!
    pause
    exit /b 1
)

REM Check and create venv
if not exist .venv (
    echo [^>] Creating virtual environment...
    python -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\pip install -r requirements.txt
)

echo [^>] Starting server...
.venv\Scripts\python main.py
pause
