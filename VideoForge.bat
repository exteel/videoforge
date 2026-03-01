@echo off
title VideoForge
cd /d "%~dp0"

echo ============================================================
echo   VideoForge — AI Video Generator
echo ============================================================
echo.

:: Перевірити чи Python доступний
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не знайдено. Встановіть Python 3.11+ та додайте до PATH.
    pause
    exit /b 1
)

:: Перевірити чи є frontend/dist (production build)
if exist "frontend\dist\index.html" (
    echo [MODE] Production ^(frontend/dist^)
    echo [INFO] Запускаємо backend + відкриваємо браузер...
    python launch.pyw
) else (
    echo [MODE] Development ^(Vite dev server^)
    echo [INFO] Запускаємо backend + Vite + відкриваємо браузер...
    python launch.pyw
)

echo.
echo VideoForge зупинено.
pause
