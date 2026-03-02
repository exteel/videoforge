"""
VideoForge Backend — Transcriber integration routes.

POST /api/transcribe              → start transcription job (URL → download → Whisper → files)
GET  /api/transcribe/{job_id}     → poll job status + logs
GET  /api/transcriber/status      → check if Transcriber GUI is available
POST /api/transcriber/launch      → open Transcriber GUI in separate window
GET  /api/transcriber/outputs     → list completed output dirs (scan output base)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["transcriber"])

ROOT = Path(__file__).parent.parent.parent

# ─── Transcribe job store ──────────────────────────────────────────────────────

@dataclass
class TranscribeJob:
    job_id:    str
    url:       str
    status:    str = "queued"   # queued | running | done | failed
    logs:      list[str] = field(default_factory=list)
    error:     str = ""
    out_dir:   str = ""
    created_at: float = field(default_factory=time.time)


_jobs: dict[str, TranscribeJob] = {}


# ─── Request model ────────────────────────────────────────────────────────────

class TranscribeRequest(BaseModel):
    url:            str
    language:       str | None = None      # ISO 639-1, None = auto-detect
    # Pipeline options (run pipeline automatically after transcription)
    auto_pipeline:  bool = False
    channel:        str = "config/channels/history.json"
    quality:        str = "max"
    template:       str = "auto"
    draft:          bool = False
    dry_run:        bool = False
    background_music: bool = True
    image_style:    str | None = None
    voice_id:       str | None = None
    master_prompt:  str | None = None
    duration_min:   int | None = None      # min video duration in minutes
    duration_max:   int | None = None      # max video duration in minutes


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _transcriber_py() -> Path:
    env = os.environ.get("TRANSCRIBER_PY", "")
    return Path(env) if env else Path(r"D:\transscript batch\Transcriber\transcriber.py")


def _output_base() -> Path:
    env = os.environ.get("TRANSCRIBER_OUTPUT", "")
    return Path(env) if env else Path(r"D:\transscript batch\output\output")


REQUIRED_FILES = {"transcript.txt", "metadata.json", "title.txt"}


def _is_ready(d: Path) -> bool:
    if not d.is_dir():
        return False
    files = {f.name for f in d.iterdir() if f.is_file()}
    return REQUIRED_FILES.issubset(files)


def _scan_outputs(since_ts: float = 0.0) -> list[dict[str, Any]]:
    out = _output_base()
    if not out.exists():
        return []
    results = []
    for d in sorted(out.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir() or not _is_ready(d):
            continue
        mtime = d.stat().st_mtime
        if mtime < since_ts:
            continue
        title = d.name
        language = ""
        try:
            t = (d / "title.txt").read_text(encoding="utf-8").strip()
            if t:
                title = t
        except Exception:
            pass
        try:
            import json
            meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
            language = meta.get("detected_language", meta.get("language", ""))
        except Exception:
            pass
        results.append({
            "dir":             str(d),
            "name":            d.name,
            "title":           title,
            "language":        language,
            "modified_at":     mtime,
            "has_srt":         (d / "transcript.srt").exists(),
            "has_description": (d / "description.txt").exists(),
            "has_thumbnail":   (d / "thumbnail.jpg").exists(),
        })
    return results[:50]


# ─── Routes: transcription jobs ───────────────────────────────────────────────

@router.post("/transcribe", status_code=202)
async def start_transcribe(req: TranscribeRequest) -> dict[str, Any]:
    """
    Start a transcription job.
    Downloads YouTube video, transcribes via VoidAI Whisper, saves output files.
    Optionally runs the VideoForge pipeline after.
    """
    job_id = uuid.uuid4().hex[:8]
    job = TranscribeJob(job_id=job_id, url=req.url)
    _jobs[job_id] = job

    async def _run() -> None:
        sys.path.insert(0, str(ROOT))
        from backend.transcribe_worker import transcribe_url

        job.status = "running"

        def on_progress(msg: str) -> None:
            job.logs.append(msg)

        try:
            pipeline_kwargs: dict[str, Any] = {
                "channel":          req.channel,
                "quality":          req.quality,
                "template":         req.template,
                "draft":            req.draft,
                "dry_run":          req.dry_run,
                "background_music": req.background_music,
                "duration_min":     req.duration_min if req.duration_min is not None else 8,
                "duration_max":     req.duration_max if req.duration_max is not None else 12,
            }
            if req.image_style:
                pipeline_kwargs["image_style"] = req.image_style
            if req.voice_id:
                pipeline_kwargs["voice_id"] = req.voice_id
            if req.master_prompt:
                pipeline_kwargs["master_prompt"] = req.master_prompt

            out_dir = await transcribe_url(
                req.url,
                language=req.language,
                on_progress=on_progress,
                start_pipeline=req.auto_pipeline,
                pipeline_kwargs=pipeline_kwargs,
            )
            job.status  = "done"
            job.out_dir = str(out_dir)
        except Exception as exc:
            job.status = "failed"
            job.error  = str(exc)[:500]
            job.logs.append(f"Error: {exc}")

    asyncio.create_task(_run(), name=f"transcribe-{job_id}")
    return {"job_id": job_id, "status": "queued", "url": req.url}


@router.get("/transcribe/{job_id}")
async def get_transcribe_job(job_id: str) -> dict[str, Any]:
    """Poll transcription job status."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    return {
        "job_id":  job.job_id,
        "url":     job.url,
        "status":  job.status,
        "logs":    job.logs,
        "error":   job.error,
        "out_dir": job.out_dir,
    }


@router.get("/transcribe")
async def list_transcribe_jobs() -> list[dict[str, Any]]:
    """List recent transcription jobs."""
    return [
        {
            "job_id":  j.job_id,
            "url":     j.url,
            "status":  j.status,
            "logs":    j.logs[-5:],   # last 5 log lines
            "error":   j.error,
            "out_dir": j.out_dir,
        }
        for j in sorted(_jobs.values(), key=lambda x: x.created_at, reverse=True)
    ]


# ─── Routes: Transcriber GUI ──────────────────────────────────────────────────

@router.get("/transcriber/status")
async def transcriber_status() -> dict[str, Any]:
    py_path = _transcriber_py()
    out_dir = _output_base()
    return {
        "transcriber_found": py_path.exists(),
        "transcriber_path":  str(py_path),
        "output_dir":        str(out_dir),
        "output_dir_exists": out_dir.exists(),
        "outputs_count":     len(_scan_outputs()),
    }


@router.post("/transcriber/launch")
async def transcriber_launch() -> dict[str, Any]:
    """Open the external Transcriber GUI app in a separate window."""
    py_path = _transcriber_py()
    if not py_path.exists():
        raise HTTPException(
            404,
            f"Transcriber not found at {py_path}. "
            "Set TRANSCRIBER_PY in .env to override."
        )
    try:
        kwargs: dict[str, Any] = {"cwd": str(py_path.parent)}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen([sys.executable, str(py_path)], **kwargs)
        return {"status": "launched", "path": str(py_path)}
    except Exception as exc:
        raise HTTPException(500, f"Failed to launch Transcriber: {exc}") from exc


@router.get("/transcriber/outputs")
async def transcriber_outputs(since: float = 0.0) -> list[dict[str, Any]]:
    """List completed Transcriber output directories."""
    return _scan_outputs(since_ts=since)
