"""
VideoForge — Transcription Worker.

YouTube URL → yt-dlp download → faster-whisper (local GPU/CPU) → Transcriber-compatible output files.

Output structure (matches Transcriber exactly):
    {output_base}/{sanitized_title}/
        transcript.txt       — plain text
        transcript.srt       — SRT with timestamps
        metadata.json        — {title, description, url, video_id, duration_seconds, ...}
        title.txt            — video title
        description.txt      — video description
        thumbnail.jpg        — downloaded thumbnail
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parent.parent

WHISPER_MODEL_SIZE = "turbo"   # faster-whisper-large-v3-turbo-ct2 — fast + accurate

# Only 1 GPU transcription at a time — prevents CUDA out-of-memory with parallel workers
_GPU_TRANSCRIBE_LOCK = threading.Semaphore(1)

log = logging.getLogger("transcribe")


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


def _segments_to_srt(segments: list) -> str:
    """Convert faster-whisper segment objects (or dicts) to SRT string."""
    parts = []
    for i, seg in enumerate(segments, 1):
        if hasattr(seg, "start"):
            start, end, text = seg.start, seg.end, seg.text
        else:
            start, end, text = seg.get("start", 0), seg.get("end", 0), seg.get("text", "")
        parts.append(f"{i}\n{_fmt_srt(start)} --> {_fmt_srt(end)}\n{text.strip()}\n")
    return "\n".join(parts)


def _segments_to_text(segments: list) -> str:
    """Join segment text into plain transcript."""
    parts = []
    for seg in segments:
        text = seg.text if hasattr(seg, "text") else seg.get("text", "")
        parts.append(text.strip())
    return " ".join(parts)


_whisper_model: Any = None  # lazy singleton — loaded once, reused across requests

def _load_whisper_model() -> Any:
    """Load faster-whisper model (CUDA → CPU fallback). Cached as singleton."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    from faster_whisper import WhisperModel, BatchedInferencePipeline

    try:
        base = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="int8_float16")
        _whisper_model = BatchedInferencePipeline(model=base)
    except Exception:
        base = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        _whisper_model = BatchedInferencePipeline(model=base)
    log.info("Whisper model loaded: %s (cached for reuse)", WHISPER_MODEL_SIZE)
    return _whisper_model


def _run_transcription(audio_path: Path, language: str | None) -> tuple[list, Any]:
    """Run faster-whisper transcription synchronously (GPU-locked). Returns (segments, info)."""
    with _GPU_TRANSCRIBE_LOCK:
        log.info("GPU lock acquired — starting transcription")
        model = _load_whisper_model()
        opts: dict[str, Any] = {
            "beam_size": 5,
            "batch_size": 16,
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            "word_timestamps": False,
            "condition_on_previous_text": False,
            "hallucination_silence_threshold": 2.0,
        }
        if language:
            opts["language"] = language

        segments_gen, info = model.transcribe(str(audio_path), **opts)
        return list(segments_gen), info


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
    Download YouTube video and transcribe it via local faster-whisper.

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

    _log = logging.getLogger("transcribe")

    def log(msg: str) -> None:
        _log.info(msg)
        if on_progress:
            on_progress(msg)

    # ── 1. Get video info ─────────────────────────────────────────────────────
    log("Fetching video info…")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not installed. Run: pip install yt-dlp")

    loop = asyncio.get_running_loop()

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

    # ── Cache check: skip if already transcribed ───────────────────────────────
    from utils.db import VideoTracker
    _db = VideoTracker()
    _cached = _db.get_cached_transcription(video_id)
    if _cached:
        log("Transcript cache hit — skipping download + transcription")
        return Path(_cached)

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

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path  = Path(tmp)
        audio_out = tmp_path / "audio.%(ext)s"

        def _download() -> Path:
            import shutil as _shutil
            _ffmpeg_dir = _shutil.which("ffmpeg")
            _ffmpeg_location = str(Path(_ffmpeg_dir).parent) if _ffmpeg_dir else None

            opts = {
                "format":       "bestaudio/best",
                "outtmpl":      str(audio_out),
                "postprocessors": [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "128",
                }],
                "quiet":       True,
                "no_warnings": True,
            }
            if _ffmpeg_location:
                opts["ffmpeg_location"] = _ffmpeg_location
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            files = list(tmp_path.glob("audio.*"))
            if not files:
                raise RuntimeError("Audio download failed")
            return files[0]

        audio_path = await loop.run_in_executor(None, _download)

        # ── 4. Transcribe ─────────────────────────────────────────────────────
        log(f"Transcribing with faster-whisper ({WHISPER_MODEL_SIZE})…")
        segments, fw_info = await loop.run_in_executor(
            None, _run_transcription, audio_path, language
        )
        detected_lang = fw_info.language if hasattr(fw_info, "language") else (language or "")
        log(f"Transcription done — {len(segments)} segments, lang={detected_lang}")

    # ── 5. Write output files ─────────────────────────────────────────────────
    log("Saving output files…")

    full_text = _segments_to_text(segments)
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

    log(f"Done → {out_dir}")

    # ── Cache result ───────────────────────────────────────────────────────────
    try:
        from utils.db import VideoTracker
        VideoTracker().cache_transcription(video_id, url, safe_name, str(out_dir))
    except Exception as _cache_err:
        log(f"Cache write failed (non-fatal): {_cache_err}")

    # ── 6. (Optional) Auto-start pipeline ────────────────────────────────────
    if start_pipeline:
        log("Starting pipeline…")
        from backend.job_manager import manager
        from pathlib import Path as _P
        channel_path = _P(pipeline_kwargs.get("channel", "config/channels/history.json"))
        if not channel_path.is_absolute():
            channel_path = ROOT / channel_path
        extra = {k: v for k, v in (pipeline_kwargs or {}).items() if k != "channel"}
        await manager.start_pipeline(
            source_dir=out_dir,
            channel_config_path=channel_path,
            **extra,
        )

    return out_dir
