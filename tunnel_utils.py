"""Cloudflare Tunnel (cloudflared) utilities for VideoForge.

Replaces ngrok — no bandwidth limits, no registration required.
Stores tunnel URLs in .tunnel_url files so all components can read them.

Quick Tunnel (default, no account):
    start_tunnel(port=8000, name="videoforge")
    → random URL like https://xxx-yyy.trycloudflare.com (changes each restart)

Named Tunnel (persistent URL, requires Cloudflare account):
    Set CLOUDFLARED_TUNNEL_ID and CLOUDFLARED_HOSTNAME in .env
    → fixed URL like https://videoforge.example.com

Setup for Named Tunnel:
    1. cloudflared login
    2. cloudflared tunnel create videoforge
    3. cloudflared tunnel route dns videoforge videoforge.yourdomain.com
    4. Add to .env:
       CLOUDFLARED_TUNNEL_ID=<uuid from step 2>
       CLOUDFLARED_HOSTNAME=videoforge.yourdomain.com
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("tunnel")

ROOT = Path(__file__).parent

# Active tunnel processes: {name: Popen}
_procs: dict[str, subprocess.Popen] = {}  # type: ignore[type-arg]
_lock = threading.Lock()


# ── Startup cleanup ──────────────────────────────────────────────────────────
# Remove stale .tunnel_url_* files left from a crashed/killed process.

def cleanup_stale_urls() -> None:
    """Remove .tunnel_url_* files that have no matching running process."""
    for f in ROOT.glob(".tunnel_url_*"):
        name = f.name.replace(".tunnel_url_", "")
        with _lock:
            proc = _procs.get(name)
        if proc is None or proc.poll() is not None:
            f.unlink(missing_ok=True)
            log.debug("Cleaned stale tunnel URL file: %s", f.name)


# Run cleanup on import — catches files from previous crashed sessions
cleanup_stale_urls()


# ── Shutdown cleanup ─────────────────────────────────────────────────────────

def _atexit_cleanup() -> None:
    """Stop all tunnels and remove URL files on interpreter exit."""
    with _lock:
        names = list(_procs.keys())
    for name in names:
        try:
            stop_tunnel(name)
        except Exception:
            pass


atexit.register(_atexit_cleanup)


# ── Core functions ───────────────────────────────────────────────────────────

def _url_file(name: str) -> Path:
    """Path to the file storing a tunnel's public URL."""
    return ROOT / f".tunnel_url_{name}"


def _find_cloudflared() -> str | None:
    """Locate cloudflared executable."""
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    candidates = [
        ROOT / "cloudflared.exe",
        Path("C:/cloudflared/cloudflared.exe"),
        Path(os.environ.get("USERPROFILE", "")) / "cloudflared.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "cloudflared" / "cloudflared.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _is_named_tunnel() -> bool:
    """Check if Named Tunnel is configured via env vars."""
    return bool(os.getenv("CLOUDFLARED_TUNNEL_ID")) and bool(os.getenv("CLOUDFLARED_HOSTNAME"))


def get_tunnel_url(name: str = "videoforge") -> str | None:
    """Read stored tunnel URL, or None if tunnel is not running."""
    f = _url_file(name)
    if f.exists():
        url = f.read_text(encoding="utf-8").strip()
        if url.startswith("https://"):
            return url
    return None


def tunnel_check(name: str = "videoforge") -> dict:
    """Check tunnel status — analogous to the old _ngrok_check."""
    url = get_tunnel_url(name)
    with _lock:
        proc = _procs.get(name)
    alive = proc is not None and proc.poll() is None
    if url and alive:
        return {"ok": True, "url": url, "name": name, "provider": "cloudflared"}
    if url and not alive:
        _url_file(name).unlink(missing_ok=True)
        return {"ok": False, "error": "Тунель зупинено (процес не працює)"}
    if alive and not url:
        return {"ok": False, "error": "Тунель запускається, URL ще не отримано"}
    return {"ok": False, "error": "Тунель не запущено"}


def stop_tunnel(name: str = "videoforge") -> None:
    """Stop a running tunnel."""
    with _lock:
        proc = _procs.pop(name, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _url_file(name).unlink(missing_ok=True)
    log.info("Tunnel '%s' stopped", name)


def start_tunnel(port: int = 8000, name: str = "videoforge") -> str:
    """Start a cloudflared tunnel and return status message.

    If CLOUDFLARED_TUNNEL_ID and CLOUDFLARED_HOSTNAME are set in .env,
    uses a Named Tunnel with a persistent URL. Otherwise uses a Quick
    Tunnel with a random trycloudflare.com URL.
    """
    exe = _find_cloudflared()
    if not exe:
        raise FileNotFoundError(
            "cloudflared не знайдено. Встанови: winget install cloudflare.cloudflared\n"
            "Або скачай з https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        )

    # Stop existing tunnel with same name
    stop_tunnel(name)

    tunnel_id = os.getenv("CLOUDFLARED_TUNNEL_ID", "").strip()
    hostname = os.getenv("CLOUDFLARED_HOSTNAME", "").strip()

    if tunnel_id and hostname:
        # Named Tunnel — persistent URL
        cmd = [
            exe, "tunnel", "run",
            "--url", f"http://localhost:{port}",
            tunnel_id,
        ]
        # Write URL immediately — it's known and fixed
        fixed_url = f"https://{hostname}"
        _url_file(name).write_text(fixed_url, encoding="utf-8")
        log.info("Named tunnel '%s' → %s (port %d)", name, fixed_url, port)
    else:
        # Quick Tunnel — random URL, parsed from stderr
        cmd = [exe, "tunnel", "--url", f"http://localhost:{port}"]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    with _lock:
        _procs[name] = proc

    # Background thread to read stderr (parse URL for Quick Tunnel, log for Named)
    def _reader() -> None:
        assert proc.stderr is not None
        url_found = bool(tunnel_id and hostname)
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            log.debug("cloudflared [%s]: %s", name, line)
            if not url_found:
                m = re.search(r"(https://[a-zA-Z0-9_-]+\.trycloudflare\.com)", line)
                if m:
                    url = m.group(1)
                    _url_file(name).write_text(url, encoding="utf-8")
                    url_found = True
                    log.info("Tunnel '%s' URL: %s (port %d)", name, url, port)

    t = threading.Thread(target=_reader, daemon=True, name=f"tunnel-{name}")
    t.start()

    mode = "named" if (tunnel_id and hostname) else "quick"
    log.info("cloudflared started [%s] for port %d (PID %s)", mode, port, proc.pid)
    return f"запущено (PID {proc.pid})"


def wait_tunnel_url(name: str = "videoforge", attempts: int = 20) -> str | None:
    """Wait up to `attempts` seconds for the tunnel URL to appear."""
    for _ in range(attempts):
        time.sleep(1)
        url = get_tunnel_url(name)
        if url:
            return url
    return None
