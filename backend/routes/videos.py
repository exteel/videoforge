"""
VideoForge Backend — Video history routes (reads from SQLite DB).

GET /api/videos            → list videos
GET /api/videos/{id}       → video detail + cost breakdown
GET /api/videos/{id}/costs → cost entries only
PUT /api/videos/{id}/youtube → set YouTube URL
GET /api/stats             → aggregate stats
"""

from fastapi import APIRouter, HTTPException, Query

from backend.models import StatsResponse, VideoDetail
from utils.db import VideoTracker

router = APIRouter(tags=["videos"])

# Lazy singleton — created on first request so DB path can be resolved after startup
_tracker: VideoTracker | None = None


def _get_tracker() -> VideoTracker:
    global _tracker
    if _tracker is None:
        _tracker = VideoTracker()
    return _tracker


# ── Videos ────────────────────────────────────────────────────────────────────

@router.get("/videos")
async def list_videos(
    channel: str | None = Query(None, description="Filter by channel name"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List pipeline run history, newest first."""
    tracker = _get_tracker()
    return tracker.list_videos(channel=channel, status=status, limit=limit, offset=offset)


@router.get("/videos/{video_id}", response_model=VideoDetail)
async def get_video(video_id: int) -> dict:
    """Get full detail for one video run including cost breakdown."""
    tracker = _get_tracker()
    video = tracker.get_video(video_id)
    if not video:
        raise HTTPException(404, f"Video {video_id} not found")
    costs = tracker.get_costs(video_id)
    total = tracker.video_total_cost(video_id)
    return {"video": video, "costs": costs, "total_cost_usd": total}


@router.get("/videos/{video_id}/costs")
async def get_video_costs(video_id: int) -> list[dict]:
    """Return cost entries for a video."""
    tracker = _get_tracker()
    if not tracker.get_video(video_id):
        raise HTTPException(404, f"Video {video_id} not found")
    return tracker.get_costs(video_id)


@router.put("/videos/{video_id}/youtube", status_code=200)
async def set_youtube_url(video_id: int, youtube_url: str, youtube_video_id: str = "") -> dict:
    """Record YouTube upload URL and video ID for a completed video."""
    tracker = _get_tracker()
    if not tracker.get_video(video_id):
        raise HTTPException(404, f"Video {video_id} not found")
    tracker.set_youtube_url(video_id, youtube_url, youtube_video_id)
    return {"video_id": video_id, "youtube_url": youtube_url}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse)
async def get_stats() -> dict:
    """Aggregate stats across all pipeline runs."""
    return _get_tracker().session_stats()
