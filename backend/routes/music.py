"""
VideoForge Backend — Music routes.

GET /api/music  → list available background music tracks from assets/music/
"""

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["music"])

ROOT      = Path(__file__).parent.parent.parent
MUSIC_DIR = ROOT / "assets" / "music"
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}


class MusicTrack(BaseModel):
    name: str       # display name (stem without extension)
    filename: str   # filename with extension
    rel_path: str   # path relative to assets/music/ — shown in UI
    path: str       # absolute path — sent to backend as music_track
    size_mb: float


@router.get("/music", response_model=list[MusicTrack])
async def list_music() -> list[dict]:
    """List all available background music tracks from assets/music/ (recursive)."""
    if not MUSIC_DIR.exists():
        return []

    tracks: list[dict] = []
    for f in sorted(MUSIC_DIR.rglob("*")):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS and not f.name.startswith("."):
            rel = f.relative_to(MUSIC_DIR)
            tracks.append({
                "name":     f.stem,
                "filename": f.name,
                "rel_path": str(rel),
                "path":     str(f),
                "size_mb":  round(f.stat().st_size / 1_048_576, 2),
            })
    return tracks
