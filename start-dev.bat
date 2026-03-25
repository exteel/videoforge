@echo off
title VideoForge — Dev
cd /d "%~dp0"

echo Starting VideoForge in development mode...
echo  Backend:  http://localhost:8000
echo  Frontend: http://localhost:5173
echo  API docs: http://localhost:8000/docs
echo.

:: Start backend in a new window
start "VideoForge Backend" cmd /k "python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload --log-level info"

:: Give backend a moment to start
timeout /t 2 /nobreak > nul

:: Start Cloudflare Tunnel in a new window
where cloudflared >nul 2>&1
if not errorlevel 1 (
    start "VideoForge Tunnel" cmd /k "cloudflared tunnel --url http://localhost:8000"
    echo  Tunnel started.
) else (
    echo  WARNING: cloudflared not found — skipping tunnel.
)

:: Start Vite dev server in a new window
start "VideoForge Frontend" cmd /k "cd /d "%~dp0frontend" && npm run dev"

echo All servers started in separate windows.
echo Close those windows to stop them.
pause
