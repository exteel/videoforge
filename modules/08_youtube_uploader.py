"""
VideoForge — Module 08: YouTube Uploader.

output/final.mp4 + output/thumbnail.png + output/metadata.json → YouTube.

OAuth2 flow:
  - Per-channel tokens stored at config/oauth2/{channel_name}_token.pickle
  - Shared credentials: config/client_secrets.json (Desktop app OAuth2 client)
  - First run: browser consent → token saved; subsequent runs: auto-refresh
  - Scopes: youtube.upload + youtube (for thumbnail)

Upload:
  - Resumable upload (handles large video files safely)
  - Thumbnail set after video upload
  - Default: privacyStatus = "private" (set publish date manually in Studio)
  - Scheduled: privacyStatus = "private" + publishAt (ISO 8601 UTC)

Scheduling:
  --schedule "2026-03-05 18:00"   — specific datetime (local time, UTC stored)
  --auto-schedule                  — compute next slot from channel schedule config

Channel schedule config (in channel JSON):
  "schedule": {"interval_days": 7, "time": "18:00", "timezone": "UTC"}

Auto-schedule state stored at: config/oauth2/{channel_name}_schedule.json

CLI:
    python modules/08_youtube_uploader.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        --channel-name main

    python modules/08_youtube_uploader.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        --channel-name main \\
        --schedule "2026-03-05 18:00"

    python modules/08_youtube_uploader.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        --channel-name main \\
        --auto-schedule --dry-run
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from clients.youtube_auth import get_youtube_service_from_config
from modules.common import load_channel_config, load_env, setup_logging

log = setup_logging("yt_uploader")

# ─── Constants ────────────────────────────────────────────────────────────────

OAUTH2_DIR       = ROOT / "config" / "oauth2"
CHUNK_SIZE       = 8 * 1024 * 1024   # 8 MB resumable upload chunk
MAX_RETRIES      = 5
UPLOAD_RESULT    = "upload_result.json"

# YouTube video resource defaults
DEFAULT_CATEGORY = "27"              # Education
DEFAULT_PRIVACY  = "private"         # Always upload private; set publish date in Studio


# ─── Data classes ─────────────────────────────────────────────────────────────

class UploadResult:
    """Result of a successful YouTube upload."""

    def __init__(
        self,
        video_id:     str,
        title:        str,
        privacy:      str,
        publish_at:   str | None = None,
        thumbnail_ok: bool = False,
    ) -> None:
        self.video_id     = video_id
        self.title        = title
        self.privacy      = privacy
        self.publish_at   = publish_at
        self.thumbnail_ok = thumbnail_ok

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id":     self.video_id,
            "url":          self.url,
            "title":        self.title,
            "privacy":      self.privacy,
            "publish_at":   self.publish_at,
            "thumbnail_ok": self.thumbnail_ok,
            "uploaded_at":  datetime.now(timezone.utc).isoformat(),
        }


# ─── Auth helper ──────────────────────────────────────────────────────────────

def _build_youtube_service(channel_name: str, channel_config: dict):
    """
    Return an authenticated YouTube Data API v3 service for the given channel.

    Reads proxy from channel_config["proxy"] (optional).
    Delegates token storage and OAuth flow to clients.youtube_auth.

    Args:
        channel_name:   Logical channel name (e.g. "main", "philosophy").
        channel_config: Loaded channel config dict (may contain "proxy" key).
    """
    proxy = channel_config.get("proxy")
    if proxy:
        log.info("Channel '%s' — proxy: ...@%s",
                 channel_name, proxy.split("@")[-1] if "@" in proxy else proxy)
    else:
        log.info("Channel '%s' — no proxy (direct connection)", channel_name)
    service = get_youtube_service_from_config(channel_name, channel_config)
    log.info("YouTube service ready for channel '%s'", channel_name)
    return service


# ─── Schedule helpers ─────────────────────────────────────────────────────────

def _parse_schedule(schedule_str: str) -> str:
    """
    Parse a schedule string to UTC ISO 8601 string for YouTube publishAt.

    Args:
        schedule_str: "2026-03-05 18:00" (interpreted as local time)

    Returns:
        ISO 8601 UTC string, e.g. "2026-03-05T18:00:00Z"

    Raises:
        ValueError: If the format is unrecognized or time is in the past.
    """
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt_local = datetime.strptime(schedule_str.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(
            f"Cannot parse schedule '{schedule_str}'. "
            "Use format: 'YYYY-MM-DD HH:MM'"
        )

    # Treat as local time → add local UTC offset → express in UTC
    dt_utc = dt_local.astimezone(timezone.utc)

    if dt_utc <= datetime.now(timezone.utc):
        raise ValueError(f"Scheduled time is in the past: {dt_utc.isoformat()}")

    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_auto_schedule(channel_config: dict[str, Any], channel_name: str) -> str:
    """
    Compute next upload slot from channel schedule configuration.

    Channel config fields used:
        schedule.interval_days  — upload every N days (default 7)
        schedule.time           — "HH:MM" in UTC (default "18:00")

    State persisted at: config/oauth2/{channel_name}_schedule.json

    Returns:
        ISO 8601 UTC string for publishAt.
    """
    schedule_cfg   = channel_config.get("schedule", {})
    interval_days  = int(schedule_cfg.get("interval_days", 7))
    upload_time    = str(schedule_cfg.get("time", "18:00"))

    # Parse HH:MM
    try:
        h, m = map(int, upload_time.split(":"))
    except ValueError:
        h, m = 18, 0

    # Load last upload date from state file
    state_file = OAUTH2_DIR / f"{channel_name}_schedule.json"
    last_dt: datetime | None = None
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            last_str = state.get("last_upload_at", "")
            if last_str:
                last_dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc)

    if last_dt:
        next_dt = (last_dt + timedelta(days=interval_days)).replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        # No previous upload — schedule for next interval from now
        next_dt = (now_utc + timedelta(days=interval_days)).replace(hour=h, minute=m, second=0, microsecond=0)

    # If computed time is already past, push to next interval
    while next_dt <= now_utc:
        next_dt += timedelta(days=interval_days)

    iso = next_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(
        "Auto-schedule: interval=%dd at %s UTC → next=%s",
        interval_days, upload_time, iso,
    )
    return iso


def _save_schedule_state(channel_name: str, video_id: str, publish_at: str) -> None:
    """Persist last upload time for auto-schedule continuity."""
    OAUTH2_DIR.mkdir(parents=True, exist_ok=True)
    state_file = OAUTH2_DIR / f"{channel_name}_schedule.json"
    state = {"last_upload_at": publish_at, "last_video_id": video_id}
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    log.debug("Schedule state saved: %s", state_file)


# ─── Upload helpers ───────────────────────────────────────────────────────────

def _upload_video(
    service:    Any,
    video_path: Path,
    metadata:   dict[str, Any],
    privacy:    str,
    publish_at: str | None,
) -> str:
    """
    Upload video file to YouTube using resumable upload.

    Returns:
        video_id string.
    """
    try:
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise ImportError("google-api-python-client not installed") from exc

    body: dict[str, Any] = {
        "snippet": {
            "title":        metadata.get("title", "Untitled"),
            "description":  metadata.get("description", ""),
            "tags":         metadata.get("tags", []),
            "categoryId":   metadata.get("category_id", DEFAULT_CATEGORY),
            "defaultLanguage": metadata.get("language", "en"),
        },
        "status": {
            "privacyStatus":        privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    log.info("Starting resumable upload: %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6)
    t0 = time.monotonic()

    response = None
    retries  = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct % 10 == 0:
                    log.info("Upload progress: %d%%", pct)
        except Exception as exc:   # noqa: BLE001
            retries += 1
            if retries > MAX_RETRIES:
                raise RuntimeError(f"Upload failed after {MAX_RETRIES} retries: {exc}") from exc
            wait = 2 ** retries
            log.warning("Upload chunk error: %s — retry %d/%d in %ds", exc, retries, MAX_RETRIES, wait)
            time.sleep(wait)

    elapsed  = time.monotonic() - t0
    video_id = response["id"]
    log.info("Video uploaded in %.1fs: id=%s", elapsed, video_id)
    return video_id


def _set_thumbnail(service: Any, video_id: str, thumb_path: Path) -> bool:
    """
    Set thumbnail for an uploaded video.

    Returns True on success, False on failure (non-fatal).
    """
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        return False

    mime = "image/png" if thumb_path.suffix.lower() == ".png" else "image/jpeg"
    media = MediaFileUpload(str(thumb_path), mimetype=mime)

    try:
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
        log.info("Thumbnail set for video %s", video_id)
        return True
    except Exception as exc:
        log.warning("Thumbnail upload failed (non-fatal): %s", exc)
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def upload_video(
    script_path:         str | Path,
    channel_config_path: str | Path,
    *,
    channel_name:        str | None = None,
    schedule:            str | None = None,
    auto_schedule:       bool = False,
    privacy:             str = DEFAULT_PRIVACY,
    dry_run:             bool = False,
) -> UploadResult:
    """
    Upload video, thumbnail, and metadata to YouTube.

    Args:
        script_path: Path to script.json (used to locate output/ directory).
        channel_config_path: Path to channel config JSON.
        channel_name: Logical channel name for OAuth2 token lookup (e.g. "main").
                      If None, derived from channel_config["channel_name"].
        schedule: Specific publish datetime string "YYYY-MM-DD HH:MM".
        auto_schedule: Compute next upload slot from channel schedule config.
        privacy: Privacy status if not scheduling (default: "private").
        dry_run: Show plan without uploading.

    Returns:
        UploadResult with video_id, url, title, privacy.

    Raises:
        FileNotFoundError: If video, thumbnail, or metadata files are missing.
        RuntimeError: If upload fails.
    """
    load_env()

    script_path = Path(script_path)
    base_dir    = script_path.parent
    out_dir     = base_dir / "output"

    # Locate required files
    video_path = out_dir / "final.mp4"
    thumb_path = out_dir / "thumbnail.png"
    meta_path  = out_dir / "metadata.json"

    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata.json not found: {meta_path}")

    metadata       = json.loads(meta_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)
    # channel_name: explicit arg takes priority; fallback to config; then "channel"
    channel_name = (
        channel_name
        or channel_config.get("channel_name", "")
        or "channel"
    ).replace(" ", "_").lower()

    # Resolve publish time
    publish_at: str | None = None
    effective_privacy = privacy

    if schedule:
        publish_at        = _parse_schedule(schedule)
        effective_privacy = "private"   # YouTube requires private for scheduled
        log.info("Scheduled publish at: %s", publish_at)
    elif auto_schedule:
        publish_at        = _compute_auto_schedule(channel_config, channel_name)
        effective_privacy = "private"
        log.info("Auto-scheduled publish at: %s", publish_at)

    if dry_run:
        log.info("[DRY RUN] Would upload:")
        log.info("[DRY RUN]   Video:     %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6)
        log.info("[DRY RUN]   Thumbnail: %s", thumb_path.name if thumb_path.exists() else "NOT FOUND")
        log.info("[DRY RUN]   Title:     %s", metadata.get("title", "?"))
        log.info("[DRY RUN]   Tags:      %d tags", len(metadata.get("tags", [])))
        log.info("[DRY RUN]   Privacy:   %s", effective_privacy)
        if publish_at:
            log.info("[DRY RUN]   Publish at: %s", publish_at)
        return UploadResult(
            video_id="dry_run_id",
            title=metadata.get("title", "?"),
            privacy=effective_privacy,
            publish_at=publish_at,
        )

    # Build authenticated YouTube service (per-channel token + optional proxy)
    service = _build_youtube_service(channel_name, channel_config)

    # Upload video
    video_id = _upload_video(service, video_path, metadata, effective_privacy, publish_at)

    # Set thumbnail (optional, non-fatal)
    thumbnail_ok = False
    if thumb_path.exists():
        thumbnail_ok = _set_thumbnail(service, video_id, thumb_path)
    else:
        log.warning("No thumbnail.png found — skipping thumbnail upload")

    result = UploadResult(
        video_id=video_id,
        title=metadata.get("title", "?"),
        privacy=effective_privacy,
        publish_at=publish_at,
        thumbnail_ok=thumbnail_ok,
    )

    # Save upload result
    result_path = out_dir / UPLOAD_RESULT
    result_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Upload result saved: %s", result_path)

    # Update auto-schedule state
    if auto_schedule and publish_at:
        _save_schedule_state(channel_name, video_id, publish_at)

    log.info("Done: %s | %s", result.url, effective_privacy)
    return result


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — YouTube Uploader (Module 08)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Immediate public upload:
  python modules/08_youtube_uploader.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json

  # Schedule for specific date/time:
  python modules/08_youtube_uploader.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json \\
      --schedule "2026-03-05 18:00"

  # Auto-schedule from channel config (interval_days, time):
  python modules/08_youtube_uploader.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json \\
      --auto-schedule

  # Dry run (no upload):
  python modules/08_youtube_uploader.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json \\
      --auto-schedule --dry-run
        """,
    )

    parser.add_argument("--script",  required=True, help="Path to script.json")
    parser.add_argument("--channel", required=True, help="Channel config JSON path")
    parser.add_argument(
        "--channel-name", default=None,
        help="Logical channel name for OAuth2 token lookup (e.g. 'main', 'philosophy'). "
             "Defaults to channel_name from channel config JSON.",
    )
    parser.add_argument(
        "--schedule", default=None,
        help="Publish datetime (local time): 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "--auto-schedule", action="store_true",
        help="Auto-compute next publish slot from channel schedule config",
    )
    parser.add_argument(
        "--privacy", default=DEFAULT_PRIVACY,
        choices=["public", "private", "unlisted"],
        help=f"Privacy status for immediate upload (default: {DEFAULT_PRIVACY})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show upload plan without uploading",
    )

    args = parser.parse_args()

    if args.schedule and args.auto_schedule:
        parser.error("--schedule and --auto-schedule are mutually exclusive")

    result = upload_video(
        script_path=args.script,
        channel_config_path=args.channel,
        channel_name=args.channel_name,
        schedule=args.schedule,
        auto_schedule=args.auto_schedule,
        privacy=args.privacy,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        log.info("Uploaded: %s", result.url)
        if result.publish_at:
            log.info("Scheduled: %s", result.publish_at)


if __name__ == "__main__":
    _main()
