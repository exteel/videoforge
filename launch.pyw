"""
VideoForge Launcher — подвійний клік для запуску (без консолі).

Запускає backend (uvicorn) і відкриває браузер автоматично.
- Production mode: frontend/dist вже зібраний → один процес (uvicorn)
- Dev mode: запускає Vite dev server + backend

Для зупинки: знайти процес у Task Manager або натиснути Ctrl+C в консолі.
"""

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent

# Приховати консоль підпроцесів на Windows
_NO_WIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_BACKEND_PORT  = 8000
_FRONTEND_PORT = 5173


def _health_url() -> str:
    return f"http://localhost:{_BACKEND_PORT}/api/health"


def _wait_ready(url: str, timeout: int = 40) -> bool:
    """Чекаємо поки сервер не відповість (або timeout)."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _build_frontend() -> bool:
    """Запустити npm run build якщо dist ще не існує."""
    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        return True
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    node_modules = ROOT / "frontend" / "node_modules"
    if not node_modules.exists():
        subprocess.run([npm, "install"], cwd=str(ROOT / "frontend"), check=False)
    result = subprocess.run(
        [npm, "run", "build"],
        cwd=str(ROOT / "frontend"),
        capture_output=True,
    )
    return result.returncode == 0


def main() -> None:
    python   = sys.executable
    is_prod  = (ROOT / "frontend" / "dist").exists()
    env      = {**os.environ, "PYTHONUNBUFFERED": "1"}

    # ── 1. Start Backend ──────────────────────────────────────────────────────
    backend = subprocess.Popen(
        [
            python, "-m", "uvicorn",
            "backend.main:app",
            "--host", "0.0.0.0",
            "--port", str(_BACKEND_PORT),
            "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        creationflags=_NO_WIN,
    )

    frontend_proc = None

    if is_prod:
        # ── Production: uvicorn serves frontend/dist ──────────────────────────
        if _wait_ready(_health_url()):
            webbrowser.open(f"http://localhost:{_BACKEND_PORT}")
        else:
            # Якщо не запустився — показати помилку через dialog
            _show_error("Backend не відповів за 40 секунд.\nПеревірте консоль VideoForge.bat")
    else:
        # ── Dev: also start Vite ─────────────────────────────────────────────
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        frontend_proc = subprocess.Popen(
            [npm, "run", "dev"],
            cwd=str(ROOT / "frontend"),
            env=env,
            creationflags=_NO_WIN,
        )
        # Чекаємо обидва сервери
        _wait_ready(_health_url(), timeout=30)
        time.sleep(2)  # Vite стартує трохи повільніше
        webbrowser.open(f"http://localhost:{_FRONTEND_PORT}")

    # ── 2. Тримаємо процеси живими ────────────────────────────────────────────
    try:
        backend.wait()
    except KeyboardInterrupt:
        pass
    finally:
        backend.terminate()
        if frontend_proc:
            frontend_proc.terminate()


def _show_error(message: str) -> None:
    """Показати повідомлення про помилку без tkinter (мінімально)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "VideoForge — Помилка", 0x10)
    except Exception:
        pass  # На не-Windows просто ігноруємо


if __name__ == "__main__":
    main()
