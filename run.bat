@echo off
title Music Agent v3
echo.
echo   Music Agent v3
echo.
py --version >nul 2>&1
if errorlevel 1 (
    echo   Python not found. Install from python.org
    pause
    exit
)
py -m pip show flask >nul 2>&1
if errorlevel 1 (
    echo   Installing dependencies...
    py -m pip install -r requirements.txt
    echo.
)
py src/server.py
pause
