@echo off
title VideoForge — Production
cd /d "%~dp0"

echo [1/2] Building frontend...
cd frontend
call npm run build
if errorlevel 1 (
    echo ERROR: Frontend build failed.
    pause
    exit /b 1
)
cd ..

echo [2/3] Starting Cloudflare Tunnel...
where cloudflared >nul 2>&1
if not errorlevel 1 (
    start "VideoForge Tunnel" cmd /k "cloudflared tunnel --url http://localhost:8000"
    echo  Tunnel started in separate window.
) else (
    echo  WARNING: cloudflared not found — skipping tunnel.
    echo  Install: winget install cloudflare.cloudflared
)

echo [3/3] Starting VideoForge backend (http://localhost:8000)...
echo.
echo  Access: http://localhost:8000
echo  Stop:   Ctrl+C
echo.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info
pause
