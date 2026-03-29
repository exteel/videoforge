"""
VideoForge Backend — FastAPI application entry point.

Start with:
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

Or via the project launcher:
    python backend/main.py

API docs at: http://localhost:8000/docs
"""

from __future__ import annotations

import sys
import time as _time
from contextlib import asynccontextmanager
from pathlib import Path

_startup_time = _time.monotonic()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 stdout on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from modules.common import load_env, setup_logging

log = setup_logging("backend")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Load environment on startup."""
    import json
    load_env()

    # Resume jobs that were running when backend stopped — must happen BEFORE
    # cancel_orphaned_jobs() so these rows are not marked cancelled.
    try:
        from utils.db import VideoTracker
        from backend.job_manager import manager as _mgr
        _db = VideoTracker()
        resumable = _db.get_resumable_jobs()
        for rj in resumable:
            _kwargs: dict = {}
            if rj["pipeline_kwargs"]:
                try:
                    _kwargs = json.loads(rj["pipeline_kwargs"])
                except Exception:
                    pass
            _source = Path(rj["source_dir"]) if rj["source_dir"] else None
            _channel = Path(f"config/channels/{rj['channel']}.json")
            if _channel.exists():
                _kwargs["from_step"] = (rj["current_step"] or 0) + 1
                if rj["project_dir"]:
                    _kwargs["project_dir"] = Path(rj["project_dir"])
                await _mgr.start_pipeline(
                    source_dir=_source,
                    channel_config_path=_channel,
                    **_kwargs,
                )
                log.info(
                    "Startup: resumed job from DB: video_id=%d, from_step=%d",
                    rj["id"],
                    _kwargs["from_step"],
                )
            else:
                log.warning(
                    "Startup: could not resume video_id=%d — channel config missing: %s",
                    rj["id"],
                    _channel,
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("Startup: job resume failed (non-fatal): %s", exc)

    # Cancel any remaining jobs left in running/waiting_review/queued that were
    # not resumed (e.g. channel config missing, resume failed).
    try:
        from utils.db import VideoTracker
        n = VideoTracker().cancel_orphaned_jobs()
        if n:
            log.warning("Startup: cancelled %d orphaned job(s) from previous session", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("Startup: could not cancel orphaned jobs: %s", exc)

    try:
        from backend.job_manager import manager
        n = manager.restore_from_db(limit=50)
        if n:
            log.info("Startup: restored %d completed job(s) from DB", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("Startup: could not restore jobs from DB: %s", exc)
    log.info("VideoForge API starting  (http://localhost:8000/docs)")
    yield
    log.info("VideoForge API shutting down")


app = FastAPI(
    title="VideoForge API",
    version="1.0.0",
    description=(
        "REST + WebSocket API for the VideoForge pipeline.\n\n"
        "Start jobs via POST, poll status via GET /api/jobs/{id}, "
        "or stream real-time progress via WebSocket /ws/{job_id}."
    ),
    lifespan=lifespan,
)

# CORS — whitelist localhost + dynamic tunnel URL only
_allowed_origins: list[str] = [
    "http://localhost:5173",    # Vite dev server
    "http://localhost:8000",    # Backend serves frontend
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]

# Read tunnel URL from file if available
_tunnel_file = ROOT / ".tunnel_url_videoforge"
if _tunnel_file.exists():
    try:
        _tunnel_url = _tunnel_file.read_text(encoding="utf-8").strip()
        if _tunnel_url:
            _allowed_origins.append(_tunnel_url)
    except Exception:
        pass

# Also try runtime tunnel utility (cloudflared)
try:
    from tunnel_utils import get_tunnel_url as _get_tunnel_url
    _live_tunnel = _get_tunnel_url("videoforge")
    if _live_tunnel and _live_tunnel not in _allowed_origins:
        _allowed_origins.append(_live_tunnel)
except Exception:
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

from fastapi import Depends

from backend.auth import verify_api_key
from backend.routes import auth as auth_router
from backend.routes import channels as channels_router
from backend.routes import pipeline as pipeline_router
from backend.routes import script as script_router
from backend.routes import transcriber as transcriber_router
from backend.routes import videos as videos_router
from backend.routes import ws as ws_router
from backend.routes import style as style_router
from backend.routes import youtube as youtube_router
from backend.routes import fs as fs_router
from backend.routes import music as music_router
from backend.routes import presets as presets_router
from backend.routes import drive as drive_router
from backend.routes import status as status_router

# Public routes (no auth required)
app.include_router(auth_router.router, prefix="/api")

# Protected routes — require X-API-Key if ACCESS_CODE is set
_auth = [Depends(verify_api_key)]
app.include_router(pipeline_router.router,     prefix="/api", dependencies=_auth)
app.include_router(script_router.router,       prefix="/api", dependencies=_auth)
app.include_router(videos_router.router,       prefix="/api", dependencies=_auth)
app.include_router(channels_router.router,     prefix="/api", dependencies=_auth)
app.include_router(youtube_router.router,      prefix="/api", dependencies=_auth)
app.include_router(transcriber_router.router,  prefix="/api", dependencies=_auth)
app.include_router(style_router.router,        prefix="/api", dependencies=_auth)
app.include_router(fs_router.router,           prefix="/api", dependencies=_auth)
app.include_router(music_router.router,        prefix="/api", dependencies=_auth)
app.include_router(presets_router.router,      prefix="/api", dependencies=_auth)
app.include_router(drive_router.router,        prefix="/api", dependencies=_auth)
app.include_router(status_router.router,       prefix="/api", dependencies=_auth)
app.include_router(ws_router.router)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["health"])
async def health() -> dict:
    """Liveness probe — returns 200 if the server is running."""
    try:
        from backend.job_manager import manager
        active = sum(
            1 for j in manager._jobs.values()
            if j.status in ("running", "queued", "waiting_review")
        )
    except Exception:
        active = 0
    return {
        "status": "ok",
        "service": "VideoForge API",
        "version": "1.0.0",
        "uptime_seconds": round(_time.monotonic() - _startup_time, 1),
        "active_jobs": active,
    }


def _read_tunnel_url() -> dict:
    from tunnel_utils import get_tunnel_url
    return {"url": get_tunnel_url("videoforge")}


@app.get("/api/tunnel", tags=["health"])
async def tunnel_url() -> dict:
    """Return the current public tunnel URL (cloudflared)."""
    return _read_tunnel_url()


@app.get("/api/ngrok", tags=["health"])
async def ngrok_url_compat() -> dict:
    """Backward-compatible alias for /api/tunnel."""
    return _read_tunnel_url()


# ── Static frontend (production / Docker) ─────────────────────────────────────
# Serve React build from frontend/dist when it exists.
# In local dev, Vite dev server (port 5173) handles the frontend.

_DIST     = ROOT / "frontend" / "dist"
_PROJECTS = ROOT / "projects"
_PROJECTS.mkdir(exist_ok=True)

# Serve project files (thumbnails, final.mp4, etc.) at /projects/<name>/...
app.mount("/projects", StaticFiles(directory=str(_PROJECTS)), name="projects")

if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        """Serve static files from dist/ or fall back to index.html for SPA routing."""
        fp = _DIST / full_path
        if fp.is_file():
            return FileResponse(str(fp))
        return FileResponse(str(_DIST / "index.html"))


# ── Dev launcher ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn  # type: ignore[import]
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
