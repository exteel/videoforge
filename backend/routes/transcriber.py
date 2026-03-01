"""
VideoForge Backend — Transcriber integration routes.

GET  /api/transcriber/status   → перевірити чи Transcriber доступний
POST /api/transcriber/launch   → відкрити Transcriber GUI у окремому вікні
GET  /api/transcriber/outputs  → список готових директорій (мають всі файли)
GET  /api/transcriber/watch    → (long-poll) чекати нові виходи

Transcriber paths (override via .env):
    TRANSCRIBER_PY     = D:/transscript batch/Transcriber/transcriber.py
    TRANSCRIBER_OUTPUT = D:/transscript batch/output/output
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["transcriber"])

ROOT = Path(__file__).parent.parent.parent

# ─── Paths (override via .env) ─────────────────────────────────────────────────

def _transcriber_py() -> Path:
    env = os.environ.get("TRANSCRIBER_PY", "")
    if env:
        return Path(env)
    # Default known path
    return Path(r"D:\transscript batch\Transcriber\transcriber.py")


def _output_dir() -> Path:
    env = os.environ.get("TRANSCRIBER_OUTPUT", "")
    if env:
        return Path(env)
    return Path(r"D:\transscript batch\output\output")


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Required files that Transcriber produces when done
REQUIRED_FILES = {"transcript.txt", "metadata.json", "title.txt"}


def _is_ready(d: Path) -> bool:
    """True if directory contains all required Transcriber output files."""
    if not d.is_dir():
        return False
    files = {f.name for f in d.iterdir() if f.is_file()}
    return REQUIRED_FILES.issubset(files)


def _scan_outputs(since_ts: float = 0.0) -> list[dict[str, Any]]:
    """Scan Transcriber output dir for ready sub-directories."""
    out = _output_dir()
    if not out.exists():
        return []

    results = []
    for d in sorted(out.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir() or not _is_ready(d):
            continue
        mtime = d.stat().st_mtime
        if mtime < since_ts:
            continue
        # Read title
        title = d.name
        title_file = d / "title.txt"
        if title_file.exists():
            try:
                title = title_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        # Read language from metadata
        language = ""
        meta_file = d / "metadata.json"
        if meta_file.exists():
            try:
                import json
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                language = meta.get("detected_language", meta.get("language", ""))
            except Exception:
                pass

        results.append({
            "dir":         str(d),
            "name":        d.name,
            "title":       title,
            "language":    language,
            "modified_at": mtime,
            "has_srt":     (d / "transcript.srt").exists(),
            "has_description": (d / "description.txt").exists(),
            "has_thumbnail":   (d / "thumbnail.jpg").exists(),
        })

    return results[:50]  # Limit to latest 50


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/transcriber/status")
async def transcriber_status() -> dict[str, Any]:
    """Check if Transcriber tool is available and output dir exists."""
    py_path = _transcriber_py()
    out_dir = _output_dir()
    return {
        "transcriber_found": py_path.exists(),
        "transcriber_path":  str(py_path),
        "output_dir":        str(out_dir),
        "output_dir_exists": out_dir.exists(),
        "outputs_count":     len(_scan_outputs()),
    }


@router.post("/transcriber/launch")
async def transcriber_launch() -> dict[str, Any]:
    """Open Transcriber GUI in a separate window (non-blocking)."""
    py_path = _transcriber_py()
    if not py_path.exists():
        raise HTTPException(
            404,
            f"Transcriber not found at {py_path}. "
            "Set TRANSCRIBER_PY in .env to override path."
        )

    try:
        kwargs: dict[str, Any] = {
            "cwd": str(py_path.parent),
        }
        if sys.platform == "win32":
            # CREATE_NEW_CONSOLE: відкрити в окремому вікні (GUI app)
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen([sys.executable, str(py_path)], **kwargs)
        return {
            "status":  "launched",
            "message": "Transcriber відкрито у окремому вікні.",
            "path":    str(py_path),
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to launch Transcriber: {exc}") from exc


@router.get("/transcriber/outputs")
async def transcriber_outputs(
    since: float = 0.0,   # Unix timestamp — show only outputs newer than this
) -> list[dict[str, Any]]:
    """List completed Transcriber output directories."""
    return _scan_outputs(since_ts=since)


@router.get("/transcriber/watch")
async def transcriber_watch(
    timeout: int = 30,     # Max seconds to wait
    since:   float = 0.0,  # Show only outputs newer than this timestamp
) -> list[dict[str, Any]]:
    """
    Long-poll: wait up to `timeout` seconds for new Transcriber outputs.
    Returns immediately if new output already exists, otherwise polls every 2s.
    """
    deadline = time.monotonic() + min(timeout, 60)

    while time.monotonic() < deadline:
        results = _scan_outputs(since_ts=since)
        if results:
            return results
        await asyncio.sleep(2)

    return []
