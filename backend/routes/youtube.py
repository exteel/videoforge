"""
VideoForge Backend — YouTube Upload routes.

GET  /api/youtube/status          → OAuth2 auth status + channel info
POST /api/youtube/auth            → trigger OAuth2 browser flow (runs locally)
GET  /api/youtube/ready           → list project dirs with final.mp4 ready for upload
POST /api/youtube/upload          → start upload job (async background task)
GET  /api/youtube/uploads         → list recent upload results
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["youtube"])

ROOT        = Path(__file__).parent.parent.parent
OAUTH2_DIR  = ROOT / "config" / "oauth2"
TOKEN_FILE  = OAUTH2_DIR / "token.json"
PROJECTS    = ROOT / "projects"


# ─── Models ───────────────────────────────────────────────────────────────────

class UploadRequest(BaseModel):
    project_dir:        str
    channel:            str = "config/channels/history.json"
    privacy:            str = "private"          # private | unlisted | public
    schedule:           str | None = None        # "YYYY-MM-DD HH:MM"
    auto_schedule:      bool = False
    dry_run:            bool = False
    selected_thumbnail: str | None = None        # "thumbnail_1.png" / "thumbnail_2.png" / ...
    selected_title:     str | None = None        # chosen title string


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _token_status() -> dict[str, Any]:
    """Return auth status without importing Google libs (fast check)."""
    if not TOKEN_FILE.exists():
        return {"connected": False, "reason": "no_token"}
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        expiry = data.get("expiry") or data.get("token_expiry", "")
        return {
            "connected": True,
            "token_file": str(TOKEN_FILE),
            "expiry": expiry,
        }
    except Exception as exc:
        return {"connected": False, "reason": str(exc)}


def _thumb_variants(out_dir: Path, project_name: str) -> list[dict[str, Any]]:
    """Return list of thumbnail variants with URL paths for frontend."""
    variants = []
    for i in range(1, 6):  # thumbnail_1.png ... thumbnail_5.png
        f = out_dir / f"thumbnail_{i}.png"
        if f.exists():
            variants.append({
                "index":    i,
                "filename": f.name,
                "url":      f"/projects/{project_name}/output/thumbnail_{i}.png",
                "size_kb":  round(f.stat().st_size / 1024),
            })
    # If no numbered variants, fall back to thumbnail.png
    if not variants:
        for name in ("thumbnail.png", "thumbnail.jpg"):
            f = out_dir / name
            if f.exists():
                variants.append({
                    "index":    1,
                    "filename": name,
                    "url":      f"/projects/{project_name}/output/{name}",
                    "size_kb":  round(f.stat().st_size / 1024),
                })
                break
    return variants


def _scan_ready_projects() -> list[dict[str, Any]]:
    """Scan projects/ for dirs with final.mp4 and metadata.json."""
    PROJECTS.mkdir(exist_ok=True)
    results = []
    for p in sorted(PROJECTS.iterdir()):
        if not p.is_dir():
            continue
        out = p / "output"
        video = out / "final.mp4"
        if not video.exists():
            continue

        meta_data: dict = {}
        meta = out / "metadata.json"
        if meta.exists():
            try:
                meta_data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                pass

        title = meta_data.get("title") or p.name
        title_variants: list[str] = meta_data.get("title_variants", [title])

        # Thumbnail variants
        thumb_variants = _thumb_variants(out, p.name)

        # Check if already uploaded
        uploaded: dict | None = None
        upload_result_file = out / "upload_result.json"
        if upload_result_file.exists():
            try:
                uploaded = json.loads(upload_result_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        results.append({
            "dir":              str(p),
            "name":             p.name,
            "title":            title,
            "title_variants":   title_variants,
            "description":      meta_data.get("description", ""),
            "tags":             meta_data.get("tags", []),
            "category_id":      meta_data.get("category_id", "27"),
            "language":         meta_data.get("language", ""),
            "video_size_mb":    round(video.stat().st_size / 1e6, 1),
            "thumbnail_variants": thumb_variants,
            "has_thumbnail":    len(thumb_variants) > 0,
            "tags_count":       len(meta_data.get("tags", [])),
            "uploaded":         uploaded,
        })
    return results


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/youtube/status")
async def youtube_status() -> dict[str, Any]:
    """Check OAuth2 auth status."""
    return _token_status()


@router.post("/youtube/auth")
async def youtube_auth() -> dict[str, Any]:
    """
    Trigger OAuth2 browser consent flow in a background thread.
    Opens the system browser for user to grant YouTube access.
    Returns immediately with status 'auth_started'.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from modules.common import load_env
    load_env()

    status = _token_status()
    if status.get("connected"):
        return {"status": "already_connected", **status}

    def _do_auth() -> None:
        try:
            import os
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
            client_id     = os.environ.get("YOUTUBE_CLIENT_ID", "")
            client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
            if not client_id or not client_secret:
                return

            from google.oauth2.credentials import Credentials  # noqa: F401
            from google_auth_oauthlib.flow import InstalledAppFlow

            scopes = [
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube",
            ]
            client_config = {
                "installed": {
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                    "token_uri":     "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow  = InstalledAppFlow.from_client_config(client_config, scopes)
            creds = flow.run_local_server(port=0, open_browser=True)
            OAUTH2_DIR.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            pass

    thread = threading.Thread(target=_do_auth, daemon=True)
    thread.start()

    return {
        "status":  "auth_started",
        "message": "Browser opened for YouTube OAuth2. Complete consent in the browser, then refresh status.",
    }


@router.post("/youtube/auth/revoke")
async def youtube_revoke() -> dict[str, Any]:
    """Delete cached OAuth2 token (disconnect)."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        return {"status": "revoked"}
    return {"status": "not_connected"}


@router.get("/youtube/ready")
async def youtube_ready_videos() -> list[dict[str, Any]]:
    """List project directories with final.mp4 ready for upload."""
    return _scan_ready_projects()


# ── Upload jobs ────────────────────────────────────────────────────────────────

_upload_jobs: dict[str, dict[str, Any]] = {}


@router.post("/youtube/upload", status_code=202)
async def youtube_upload(req: UploadRequest) -> dict[str, Any]:
    """
    Start YouTube upload in background. Returns job_id for polling.
    Supports selected_thumbnail (e.g. "thumbnail_2.png") and selected_title.
    """
    import uuid
    import sys
    import shutil
    sys.path.insert(0, str(ROOT))
    from modules.common import load_env
    load_env()

    project_dir = Path(req.project_dir)
    script_json = project_dir / "script.json"
    out_dir     = project_dir / "output"

    if not (out_dir / "final.mp4").exists():
        raise HTTPException(400, f"final.mp4 not found in {out_dir}")

    channel_path = Path(req.channel)
    if not channel_path.is_absolute():
        channel_path = ROOT / channel_path
    if not channel_path.exists():
        raise HTTPException(400, f"Channel config not found: {channel_path}")

    # If user selected a specific thumbnail variant — copy it to thumbnail.png
    if req.selected_thumbnail:
        src_thumb = out_dir / req.selected_thumbnail
        if src_thumb.exists():
            shutil.copy2(src_thumb, out_dir / "thumbnail.png")

    # If user selected a specific title — patch metadata.json temporarily
    if req.selected_title:
        meta_path = out_dir / "metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["title"] = req.selected_title
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    job_id = uuid.uuid4().hex[:8]
    _upload_jobs[job_id] = {
        "job_id":  job_id,
        "status":  "queued",
        "project": project_dir.name,
        "error":   "",
        "result":  None,
    }

    async def _run() -> None:
        _upload_jobs[job_id]["status"] = "running"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "youtube_uploader",
                ROOT / "modules" / "08_youtube_uploader.py",
            )
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)                  # type: ignore[union-attr]
            upload_video = mod.upload_video

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: upload_video(
                    script_path=script_json,
                    channel_config_path=channel_path,
                    schedule=req.schedule or None,
                    auto_schedule=req.auto_schedule,
                    privacy=req.privacy,
                    dry_run=req.dry_run,
                ),
            )
            _upload_jobs[job_id]["status"] = "done"
            _upload_jobs[job_id]["result"] = result.to_dict()
        except Exception as exc:
            _upload_jobs[job_id]["status"] = "failed"
            _upload_jobs[job_id]["error"]  = str(exc)[:500]

    asyncio.create_task(_run(), name=f"yt-upload-{job_id}")
    return _upload_jobs[job_id]


@router.get("/youtube/uploads")
async def youtube_upload_jobs() -> list[dict[str, Any]]:
    """List recent upload jobs."""
    return list(reversed(list(_upload_jobs.values())))


@router.get("/youtube/uploads/{job_id}")
async def youtube_upload_job(job_id: str) -> dict[str, Any]:
    """Get status of a specific upload job."""
    job = _upload_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Upload job not found: {job_id}")
    return job
