"""
VideoForge — Health watchdog.

Checks /api/health every 30s. After 3 consecutive failures, restarts uvicorn.
Run as a separate process: python tools/watchdog.py

Usage:
    python tools/watchdog.py              # check localhost:8000
    python tools/watchdog.py --port 9000  # custom port
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] watchdog — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "watchdog.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")

CHECK_INTERVAL = 30  # seconds
MAX_FAILURES = 3


def _find_uvicorn_pid(port: int) -> int | None:
    """Find PID of uvicorn process listening on given port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                return int(parts[-1])
    except Exception as exc:
        log.warning("Could not find uvicorn PID: %s", exc)
    return None


def _kill_pid(pid: int) -> None:
    """Kill a process by PID."""
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _start_uvicorn(port: int) -> None:
    """Start uvicorn in background."""
    cmd = [
        sys.executable, "-m", "uvicorn",
        "backend.main:app",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--reload",
    ]
    log.info("Starting uvicorn: %s", " ".join(cmd))
    subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="VideoForge health watchdog")
    parser.add_argument("--port", type=int, default=8000, help="Backend port (default: 8000)")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}/api/health"
    failures = 0

    log.info("Watchdog started — monitoring %s every %ds", url, CHECK_INTERVAL)

    while True:
        try:
            resp = httpx.get(url, timeout=10)
            data = resp.json()
            if data.get("status") == "ok":
                if failures > 0:
                    log.info("Backend recovered after %d failure(s)", failures)
                failures = 0
            else:
                failures += 1
                log.warning("Health check returned non-ok: %s (failure %d/%d)", data, failures, MAX_FAILURES)
        except Exception as exc:
            failures += 1
            log.warning("Health check failed: %s (failure %d/%d)", exc, failures, MAX_FAILURES)

        if failures >= MAX_FAILURES:
            log.error("Backend unresponsive after %d checks — restarting", MAX_FAILURES)
            pid = _find_uvicorn_pid(args.port)
            if pid:
                log.info("Killing uvicorn PID %d", pid)
                _kill_pid(pid)
                time.sleep(3)
            _start_uvicorn(args.port)
            failures = 0
            time.sleep(15)  # give uvicorn time to start

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
