"""
VideoForge — Transcription Worker.

YouTube URL → yt-dlp download → VoidAI Whisper → Transcriber-compatible output files.

Output structure (matches Transcriber exactly):
    {output_base}/{sanitized_title}/
        transcript.txt       — plain text
        transcript.srt       — SRT with timestamps
        metadata.json        — {title, description, url, video_id, duration_seconds, ...}
        title.txt            — video title
        description.txt      — video description
        thumbnail.jpg        — downloaded thumbnail

VoidAI Whisper has 25 MB file limit — long audio is split into chunks via FFmpeg.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parent.parent

WHISPER_MODEL    = "whisper-1"
MAX_BYTES        = 24 * 1024 * 1024   # 24 MB — stay under 25 MB API limit
AUDIO_BITRATE    = "64k"              # ~480 KB/min → ~28 MB/hr; split if larger
CHUNK_MINUTES    = 25                 # Split into 25-min chunks if needed


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sanitize(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", title)
    name = name.strip(". ")[:200]
    return name or "untitled"


def _output_base() -> Path:
    env = os.environ.get("TRANSCRIBER_OUTPUT", "")
    if env:
        return Path(env)
    return Path(r"D:\transscript batch\output\output")


def _fmt_srt(seconds: float) -> str:
    t = int(seconds)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _segments_to_srt(segments: list[dict]) -> str:
    parts = []
    for i, seg in enumerate(segments, 1):
        start = _fmt_srt(seg.get("start", 0))
        end   = _fmt_srt(seg.get("end", 0))
        text  = seg.get("text", "").strip()
        parts.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(parts)


def _ffmpeg_split(audio_path: Path, chunk_dir: Path, chunk_min: int) -> list[Path]:
    """Split audio into N-minute chunks with FFmpeg. Returns list of chunk paths."""
    chunk_dir.mkdir(exist_ok=True)
    pattern = str(chunk_dir / "chunk_%03d.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", str(audio_path),
        "-f", "segment",
        "-segment_time", str(chunk_min * 60),
        "-c", "copy",
        pattern,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return sorted(chunk_dir.glob("chunk_*.mp3"))


async def _whisper_transcribe(
    audio_path: Path,
    api_key: str,
    base_url: str,
    language: str | None,
) -> dict[str, Any]:
    """Transcribe a single audio file via VoidAI Whisper API."""
    import httpx

    with open(audio_path, "rb") as f:
        content = f.read()

    files  = {"file": (audio_path.name, content, "audio/mpeg")}
    data: dict[str, str] = {"model": WHISPER_MODEL, "response_format": "verbose_json"}
    if language:
        data["language"] = language

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Whisper API error {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


async def _transcribe_file(
    audio_path: Path,
    api_key: str,
    base_url: str,
    language: str | None,
    log: Callable[[str], None],
) -> tuple[str, list[dict], str]:
    """
    Transcribe audio file, splitting into chunks if too large.
    Returns (full_text, all_segments, detected_language).
    """
    size = audio_path.stat().st_size
    log(f"Audio size: {size / 1e6:.1f} MB")

    if size <= MAX_BYTES:
        log("Transcribing (single pass)…")
        result = await _whisper_transcribe(audio_path, api_key, base_url, language)
        return (
            result.get("text", ""),
            result.get("segments", []),
            result.get("language", language or ""),
        )

    # ── Split into chunks ─────────────────────────────────────────────────────
    log(f"Audio > 24 MB — splitting into {CHUNK_MINUTES}-min chunks…")
    with tempfile.TemporaryDirectory() as tmp:
        chunk_dir  = Path(tmp) / "chunks"
        chunks     = _ffmpeg_split(audio_path, chunk_dir, CHUNK_MINUTES)
        log(f"Split into {len(chunks)} chunks.")

        all_text: list[str] = []
        all_segs: list[dict] = []
        detected_lang = language or ""
        time_offset   = 0.0

        for i, chunk in enumerate(chunks, 1):
            log(f"Transcribing chunk {i}/{len(chunks)}…")
            result = await _whisper_transcribe(chunk, api_key, base_url, language)
            text   = result.get("text", "")
            segs   = result.get("segments", [])
            lang   = result.get("language", "")

            all_text.append(text)
            if not detected_lang and lang:
                detected_lang = lang

            # Offset segment timestamps
            for seg in segs:
                seg = dict(seg)
                seg["start"] = seg.get("start", 0) + time_offset
                seg["end"]   = seg.get("end",   0) + time_offset
                all_segs.append(seg)

            # Advance offset by chunk duration
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", str(chunk),
            ]
            try:
                dur_str = subprocess.check_output(probe_cmd, text=True).strip()
                time_offset += float(dur_str)
            except Exception:
                time_offset += CHUNK_MINUTES * 60

    return " ".join(all_text), all_segs, detected_lang


# ─── Main entry point ─────────────────────────────────────────────────────────

async def transcribe_url(
    url: str,
    *,
    language: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    start_pipeline: bool = False,
    pipeline_kwargs: dict[str, Any] | None = None,
) -> Path:
    """
    Download YouTube video and transcribe it via VoidAI Whisper.

    Args:
        url:            YouTube (or other yt-dlp-supported) URL.
        language:       ISO 639-1 language code hint (None = auto-detect).
        on_progress:    Callback for progress messages.
        start_pipeline: Whether to auto-start VideoForge pipeline after.
        pipeline_kwargs: Extra kwargs for pipeline (channel, quality, etc.).

    Returns:
        Path to the output directory (Transcriber-compatible).
    """
    sys.path.insert(0, str(ROOT))
    from modules.common import load_env
    load_env()

    api_key  = os.environ.get("VOIDAI_API_KEY", "")
    base_url = os.environ.get("VOIDAI_BASE_URL", "https://api.voidai.app/v1")

    if not api_key:
        raise RuntimeError("VOIDAI_API_KEY not set in .env")

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # ── 1. Get video info ─────────────────────────────────────────────────────
    log("Fetching video info…")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not installed. Run: pip install yt-dlp")

    loop = asyncio.get_event_loop()

    def _get_info() -> dict:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)  # type: ignore[return-value]

    info        = await loop.run_in_executor(None, _get_info)
    title       = info.get("title", "untitled")
    video_id    = info.get("id", "")
    description = info.get("description", "") or ""
    duration    = info.get("duration", 0)

    log(f"Video: {title} ({duration}s)")

    safe_name = _sanitize(title)
    out_dir   = _output_base() / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 2. Download thumbnail ─────────────────────────────────────────────────
    log("Downloading thumbnail…")
    try:
        import httpx
        thumb_url = info.get("thumbnail", "")
        if thumb_url:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(thumb_url)
                if r.status_code == 200:
                    (out_dir / "thumbnail.jpg").write_bytes(r.content)
    except Exception:
        pass

    # ── 3. Download audio ─────────────────────────────────────────────────────
    log("Downloading audio…")
    audio_path = out_dir / "audio.mp3"

    def _download() -> None:
        opts = {
            "format":       "bestaudio/best",
            "outtmpl":      str(out_dir / "audio.%(ext)s"),
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "64",   # 64 kbps — minimise file size
            }],
            "quiet":       True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _download)

    # Find downloaded file (extension may vary before postprocessing)
    mp3_files = list(out_dir.glob("audio.*"))
    if not mp3_files:
        raise RuntimeError("Audio download failed")
    audio_path = mp3_files[0]

    # ── 4. Transcribe ─────────────────────────────────────────────────────────
    log("Transcribing…")
    full_text, segments, detected_lang = await _transcribe_file(
        audio_path, api_key, base_url, language, log
    )

    # ── 5. Write output files ─────────────────────────────────────────────────
    log("Saving output files…")

    (out_dir / "transcript.txt").write_text(full_text, encoding="utf-8")

    if segments:
        (out_dir / "transcript.srt").write_text(
            _segments_to_srt(segments), encoding="utf-8"
        )

    metadata = {
        "title":                title,
        "description":          description,
        "url":                  url,
        "video_id":             video_id,
        "duration_seconds":     duration,
        "detected_language":    detected_lang,
        "language_probability": 1.0,
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "title.txt").write_text(title, encoding="utf-8")
    (out_dir / "description.txt").write_text(description, encoding="utf-8")

    # Clean up audio
    try:
        audio_path.unlink()
    except Exception:
        pass

    log(f"Done → {out_dir}")

    # ── 6. (Optional) Auto-start pipeline ────────────────────────────────────
    if start_pipeline:
        log("Starting pipeline…")
        from backend.job_manager import manager
        from pathlib import Path as _P
        channel_path = _P(pipeline_kwargs.get("channel", "config/channels/history.json"))
        if not channel_path.is_absolute():
            channel_path = ROOT / channel_path
        # 'channel' is already resolved to channel_config_path — exclude it from kwargs
        extra = {k: v for k, v in (pipeline_kwargs or {}).items() if k != "channel"}
        await manager.start_pipeline(
            source_dir=out_dir,
            channel_config_path=channel_path,
            **extra,
        )

    return out_dir
