"""
VideoForge — YouTube Data API v3 upload client.

Handles resumable video upload with exponential backoff retry.
All uploads are private (privacyStatus="private") — publish date is set
manually in YouTube Studio after upload.

Usage (import):
    from clients.youtube_client import upload_video, UploadResult

    result = upload_video(
        channel_name="main",
        video_path=Path("projects/.../final.mp4"),
        title="My Video Title",
        description="Full description text...",
        tags=["philosophy", "stoicism"],
        category_id="27",          # 27 = Education
        thumbnail_path=Path("projects/.../thumbnail.jpg"),  # optional
    )
    print(result.video_id, result.url)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from clients.youtube_auth import get_youtube_service

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# YouTube category IDs (most commonly used)
CATEGORY_EDUCATION = "27"
CATEGORY_ENTERTAINMENT = "24"
CATEGORY_SCIENCE = "28"
CATEGORY_PEOPLE = "22"

# Resumable upload chunk size (-1 = single PUT, recommended for files < 5 GB)
_CHUNK_SIZE = -1

# Retry settings
_MAX_RETRIES = 5
_RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
_RETRIABLE_EXCEPTIONS = (Exception,)  # broad — network errors etc.


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass
class UploadResult:
    video_id: str
    url: str
    title: str
    channel_name: str
    status: str = "private"
    thumbnail_uploaded: bool = False
    extra: dict = field(default_factory=dict)


# ─── Upload logic ─────────────────────────────────────────────────────────────

def upload_video(
    channel_name: str,
    video_path: Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = CATEGORY_EDUCATION,
    thumbnail_path: Path | None = None,
    secrets_path: Path | None = None,
) -> UploadResult:
    """
    Upload a video to YouTube as private and optionally set a custom thumbnail.

    Args:
        channel_name:   Logical channel name (must have a valid token in config/oauth2/).
        video_path:     Path to the .mp4 file to upload.
        title:          Video title (max 100 chars; truncated if longer).
        description:    Video description (max 5000 chars; truncated if longer).
        tags:           List of tags (each max 500 chars, total max 500 chars).
        category_id:    YouTube category ID (default: 27 = Education).
        thumbnail_path: Optional path to thumbnail .jpg/.png (max 2 MB).
        secrets_path:   Override path to client_secrets.json.

    Returns:
        UploadResult with video_id and YouTube URL.

    Raises:
        FileNotFoundError: If video_path does not exist.
        HttpError: On non-retriable YouTube API errors.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    service = get_youtube_service(channel_name, secrets_path)

    # Truncate to YouTube limits
    safe_title = title[:100]
    safe_description = description[:5000]
    safe_tags = _sanitize_tags(tags or [])

    body = {
        "snippet": {
            "title": safe_title,
            "description": safe_description,
            "tags": safe_tags,
            "categoryId": category_id,
            "defaultLanguage": "uk",   # Ukrainian (channel language)
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=_CHUNK_SIZE,
        resumable=True,
    )

    log.info("Uploading '%s' (%s MB) → YouTube [channel=%s]...",
             safe_title, _mb(video_path), channel_name)

    request = service.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    video_id = _execute_with_retry(request)
    url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("✅ Upload complete: %s  (%s)", url, video_id)

    # Optional thumbnail upload
    thumb_ok = False
    if thumbnail_path and thumbnail_path.exists():
        thumb_ok = _upload_thumbnail(service, video_id, thumbnail_path)

    return UploadResult(
        video_id=video_id,
        url=url,
        title=safe_title,
        channel_name=channel_name,
        status="private",
        thumbnail_uploaded=thumb_ok,
    )


def _execute_with_retry(request) -> str:
    """
    Execute a resumable upload request with exponential backoff on transient errors.
    Returns the video_id on success.
    """
    retry = 0
    while True:
        try:
            status, response = request.next_chunk()
            if response is not None:
                video_id: str = response["id"]
                return video_id
            # status is not None → upload in progress (chunked mode)
            if status:
                pct = int(status.progress() * 100)
                log.debug("Upload progress: %d%%", pct)
        except HttpError as e:
            if e.resp.status in _RETRIABLE_STATUS_CODES:
                log.warning("HTTP %s — will retry (%d/%d)...", e.resp.status, retry + 1, _MAX_RETRIES)
            else:
                raise
        except Exception as e:
            log.warning("Network error: %s — will retry (%d/%d)...", e, retry + 1, _MAX_RETRIES)

        retry += 1
        if retry > _MAX_RETRIES:
            raise RuntimeError(f"Upload failed after {_MAX_RETRIES} retries")

        sleep_sec = min(2 ** retry, 64)
        log.info("Sleeping %ds before retry...", sleep_sec)
        time.sleep(sleep_sec)


def _upload_thumbnail(service, video_id: str, thumbnail_path: Path) -> bool:
    """Upload a custom thumbnail. Returns True on success."""
    try:
        mime = "image/jpeg" if thumbnail_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        media = MediaFileUpload(str(thumbnail_path), mimetype=mime)
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
        log.info("Thumbnail uploaded for %s", video_id)
        return True
    except HttpError as e:
        log.warning("Thumbnail upload failed (non-fatal): %s", e)
        return False


def get_video_info(channel_name: str, video_id: str) -> dict:
    """
    Fetch basic info about a video. Useful to verify upload completed.

    Returns dict with: id, title, status, publishedAt
    """
    service = get_youtube_service(channel_name)
    response = (
        service.videos()
        .list(part="snippet,status", id=video_id)
        .execute()
    )
    items = response.get("items", [])
    if not items:
        return {}
    item = items[0]
    return {
        "id": item["id"],
        "title": item["snippet"]["title"],
        "status": item["status"]["privacyStatus"],
        "publishedAt": item["snippet"].get("publishedAt"),
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mb(path: Path) -> str:
    size = path.stat().st_size
    return f"{size / 1_048_576:.1f}"


def _sanitize_tags(tags: list[str]) -> list[str]:
    """Truncate tags to YouTube limits: each tag ≤ 500 chars, total ≤ 500 chars."""
    result: list[str] = []
    total = 0
    for tag in tags:
        t = tag[:500]
        if total + len(t) > 500:
            break
        result.append(t)
        total += len(t)
    return result
