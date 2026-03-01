@echo off
chcp 65001 >nul 2>&1
title VideoForge
cd /d "%~dp0"

echo ============================================================
echo   VideoForge - AI Video Generator
echo ============================================================
echo.

:: Check Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ and add to PATH.
    pause
    exit /b 1
)

:: Launch
if exist "frontend\dist\index.html" (
    echo [MODE] Production (frontend/dist)
    echo [INFO] Starting backend + opening browser...
) else (
    echo [MODE] Development (Vite dev server)
    echo [INFO] Starting backend + Vite + opening browser...
)

python launch.pyw

echo.
echo VideoForge stopped.
pause
