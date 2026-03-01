@echo off
chcp 65001 >nul 2>&1
title VideoForge - Setup
cd /d "%~dp0"

echo ============================================================
echo   VideoForge - First-time Setup
echo ============================================================
echo.

:: 1. Check Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ and add to PATH.
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK] Python %PY_VER%

:: 2. Install Python dependencies
echo.
echo [STEP 1/3] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause & exit /b 1
)
echo [OK] Python dependencies installed.

:: 3. Install Node dependencies
echo.
echo [STEP 2/3] Installing Node.js dependencies...
where npm >nul 2>&1
if errorlevel 1 (
    echo [WARN] npm not found. Skipping frontend build.
    echo        For UI you need Node.js 18+: https://nodejs.org
    goto :skip_npm
)
cd frontend
npm install
if errorlevel 1 ( echo [WARN] npm install failed & cd .. & goto :skip_npm )
cd ..
echo [OK] Node dependencies installed.

:: 4. Build frontend (production)
echo.
echo [STEP 3/3] Building frontend (production)...
cd frontend
npm run build
if errorlevel 1 (
    echo [WARN] Build failed. Dev mode will be used.
) else (
    echo [OK] Frontend built to frontend/dist
)
cd ..

:skip_npm

:: 5. Check .env
echo.
if not exist ".env" (
    echo [WARN] .env file not found!
    echo        Create .env based on .env.example:
    echo.
    echo        VOIDAI_API_KEY=your-key
    echo        WAVESPEED_API_KEY=your-key
    echo        VOICEAPI_KEY=your-key
    echo        YOUTUBE_CLIENT_ID=your-id        (optional)
    echo        YOUTUBE_CLIENT_SECRET=your-secret (optional)
    echo.
) else (
    echo [OK] .env found.
)

echo.
echo ============================================================
echo   Setup complete!
echo   Run VideoForge.vbs (no console) or VideoForge.bat
echo ============================================================
echo.
pause
