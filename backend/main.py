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
from contextlib import asynccontextmanager
from pathlib import Path

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
    load_env()
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

# Allow all origins for local dev — restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

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

app.include_router(pipeline_router.router, prefix="/api")
app.include_router(script_router.router, prefix="/api")
app.include_router(videos_router.router, prefix="/api")
app.include_router(channels_router.router, prefix="/api")
app.include_router(youtube_router.router, prefix="/api")
app.include_router(transcriber_router.router, prefix="/api")
app.include_router(style_router.router, prefix="/api")
app.include_router(fs_router.router, prefix="/api")
app.include_router(music_router.router, prefix="/api")
app.include_router(ws_router.router)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["health"])
async def health() -> dict:
    """Liveness probe — returns 200 if the server is running."""
    return {"status": "ok", "service": "VideoForge API", "version": "1.0.0"}


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
        """Serve index.html for all non-API routes (SPA fallback)."""
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
