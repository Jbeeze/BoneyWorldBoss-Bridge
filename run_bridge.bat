@echo off
REM Boney World Bosses - Bridge Launcher (Windows)
REM Double-click this file to start the bridge

cd /d "%~dp0"

echo ========================================
echo   Boney World Bosses - Bridge
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM Check if requests module is installed
python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo Installing required packages...
    pip install requests
    echo.
)

REM Run the bridge. Configuration is read from the addon's SavedVariables -
REM run /bwb setup in WoW if the bridge reports that config is missing.
python bridge.py

REM Keep window open if bridge exits
echo.
pause
