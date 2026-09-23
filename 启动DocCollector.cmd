@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "APP_PYTHON=.venv\Scripts\python.exe"
) else (
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo Cannot find Python. Install Python 3.10+ and run: pip install -r requirements.txt
        pause
        exit /b 1
    )
    set "APP_PYTHON=python.exe"
)

"%APP_PYTHON%" main.py
if errorlevel 1 (
    echo.
    echo Startup failed. Check Python and run: pip install -r requirements.txt
    pause
    exit /b 1
)
