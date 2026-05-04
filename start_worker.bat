@echo off
chcp 65001 >nul
title GeminiGen Worker

cd /d "%~dp0"

echo ============================================================
echo  GeminiGen Worker
echo ============================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

:loop
python start.py
echo.
echo [INFO] Worker stopped. Restarting in 5 seconds...
echo [INFO] Press Ctrl+C to exit.
timeout /t 5 /nobreak >nul
goto loop
