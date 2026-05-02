@echo off
chcp 65001 >nul
title GeminiGen Worker

echo ============================================================
echo  GeminiGen Platform Worker
echo ============================================================
echo.

REM 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 检查依赖
python -c "import gemini_gen" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 正在安装依赖...
    pip install -r requirements_worker.txt
    if %errorlevel% neq 0 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
    echo [提示] 安装 Playwright Chromium...
    playwright install chromium
)

echo [提示] 启动 Worker...
echo.

:loop
python worker_standalone.py
echo.
echo [提示] Worker 已停止，5 秒后自动重启...
echo （按 Ctrl+C 可终止重启）
timeout /t 5 /nobreak >nul
goto loop
