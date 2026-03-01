@echo off
title VideoForge — First-time Setup
cd /d "%~dp0"

echo ============================================================
echo   VideoForge — First-time Setup
echo ============================================================
echo.

:: 1. Перевірити Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не знайдено. Встановіть Python 3.11+ та додайте до PATH.
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK] Python %PY_VER%

:: 2. Встановити Python залежності
echo.
echo [STEP 1/3] Встановлення Python залежностей...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install провалився.
    pause & exit /b 1
)
echo [OK] Python залежності встановлено.

:: 3. Встановити Node залежності
echo.
echo [STEP 2/3] Встановлення Node.js залежностей...
where npm >nul 2>&1
if errorlevel 1 (
    echo [WARN] npm не знайдено. Пропускаємо frontend build.
    echo        Для UI потрібен Node.js 18+: https://nodejs.org
    goto :skip_npm
)
cd frontend
npm install
if errorlevel 1 ( echo [WARN] npm install провалився & cd .. & goto :skip_npm )
cd ..
echo [OK] Node залежності встановлено.

:: 4. Зібрати frontend (production build)
echo.
echo [STEP 3/3] Збірка frontend (production)...
cd frontend
npm run build
if errorlevel 1 (
    echo [WARN] Build провалився. Буде використаний dev режим.
) else (
    echo [OK] Frontend зібрано у frontend/dist
)
cd ..

:skip_npm

:: 5. Перевірити .env
echo.
if not exist ".env" (
    echo [WARN] Файл .env не знайдено!
    echo        Створіть .env на основі .env.example:
    echo.
    echo        VOIDAI_API_KEY=your-key
    echo        WAVESPEED_API_KEY=your-key
    echo        VOICEAPI_API_KEY=your-key
    echo        YOUTUBE_CLIENT_ID=your-id        ^(optional^)
    echo        YOUTUBE_CLIENT_SECRET=your-secret ^(optional^)
    echo.
) else (
    echo [OK] .env знайдено.
)

echo.
echo ============================================================
echo   Setup завершено!
echo   Запустіть VideoForge.vbs ^(без консолі^) або VideoForge.bat
echo ============================================================
echo.
pause
