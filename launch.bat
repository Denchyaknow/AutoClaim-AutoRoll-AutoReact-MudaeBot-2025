@echo off
title Mudae Bot 2025
echo ========================================
echo   Mudae Bot 2025 - Launching
echo ========================================
echo.
echo Checking Python installation...
python --version
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    pause
    exit /b 1
)
echo.
echo Starting bot...
echo.
python Bot.py
pause
