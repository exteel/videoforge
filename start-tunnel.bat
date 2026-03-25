@echo off
title VideoForge — Cloudflare Tunnel
cd /d "%~dp0"

echo Starting Cloudflare Tunnel for VideoForge (port 8000)...
echo.
echo  Make sure VideoForge is running first (start-prod.bat or start-dev.bat).
echo  First-time setup:
echo    winget install cloudflare.cloudflared
echo    (or download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
echo.

:: Try to find cloudflared in PATH or common locations
where cloudflared >nul 2>&1
if not errorlevel 1 (
    cloudflared tunnel --url http://localhost:8000
    goto :done
)

if exist "%LOCALAPPDATA%\cloudflared\cloudflared.exe" (
    "%LOCALAPPDATA%\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
    goto :done
)

if exist "C:\cloudflared\cloudflared.exe" (
    "C:\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
    goto :done
)

if exist "%USERPROFILE%\cloudflared.exe" (
    "%USERPROFILE%\cloudflared.exe" tunnel --url http://localhost:8000
    goto :done
)

echo ERROR: cloudflared not found.
echo Install with: winget install cloudflare.cloudflared
echo Or download from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
echo.
echo Place cloudflared.exe in:
echo   - Anywhere in PATH, or
echo   - %LOCALAPPDATA%\cloudflared\cloudflared.exe, or
echo   - C:\cloudflared\cloudflared.exe

:done
pause
