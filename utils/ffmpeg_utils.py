"""
VideoForge — FFmpeg subprocess utilities.

All video/audio operations go through FFmpeg (no moviepy).
Functions cover the full pipeline:
  get_duration()     — probe audio/video duration
  resize()           — scale to target resolution
  ken_burns()        — animated zoom/pan effect on still image
  concat_videos()    — concatenate video segments with optional crossfade
  add_audio()        — mux audio track onto video
  add_subtitles()    — burn ASS/SRT subtitles into video
  normalize_audio()  — EBU R128 loudnorm
  mix_audio()        — mix voice + background music
  extract_audio()    — rip audio from video
  image_to_video()   — still image → short video clip (for Ken Burns)
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import setup_logging

log = setup_logging("ffmpeg")

# ─── Constants ────────────────────────────────────────────────────────────────

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Default video encoding settings
DEFAULT_VIDEO_CODEC = "libx264"
DEFAULT_CRF = 18          # High quality (lower = better, 0 = lossless)
DEFAULT_PRESET = "slow"   # Encoding speed/quality tradeoff
DEFAULT_AUDIO_CODEC = "aac"
DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_FPS = 30
DEFAULT_RESOLUTION = "1920x1080"

# Draft mode — fast/small encoding
DRAFT_RESOLUTION = "854x480"
DRAFT_CRF = 28
DRAFT_PRESET = "ultrafast"

# Ken Burns animation types
AnimationType = Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "static"]

# Crossfade duration in seconds
CROSSFADE_DURATION = 0.5


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run an FFmpeg/FFprobe command synchronously."""
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "")[:500]
        raise RuntimeError(
            f"FFmpeg command failed (exit {result.returncode}):\n"
            f"  CMD: {' '.join(cmd[:6])}...\n"
            f"  ERR: {stderr}"
        )
    return result


async def _run_async(cmd: list[str]) -> None:
    """Run an FFmpeg command asynchronously (non-blocking)."""
    log.debug("Async run: %s", " ".join(cmd[:8]))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"FFmpeg async command failed (exit {proc.returncode}):\n"
            f"  CMD: {' '.join(cmd[:6])}...\n"
            f"  ERR: {err}"
        )


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# ─── Probe ────────────────────────────────────────────────────────────────────

def get_duration(file_path: str | Path) -> float:
    """
    Get the duration of an audio or video file in seconds.

    Uses ffprobe JSON output for reliability.

    Args:
        file_path: Path to audio or video file.

    Returns:
        Duration in seconds (float).

    Raises:
        RuntimeError: If ffprobe fails or duration cannot be determined.
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")

    cmd = [
        FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(p),
    ]
    result = _run(cmd, capture=True)
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        log.debug("Duration of %s: %.3fs", p.name, duration)
        return duration
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not parse duration from ffprobe output: {exc}") from exc


def get_video_info(file_path: str | Path) -> dict:
    """Return full ffprobe JSON info for a file (format + streams)."""
    p = Path(file_path)
    cmd = [
        FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(p),
    ]
    result = _run(cmd, capture=True)
    return json.loads(result.stdout)


# ─── Resize ───────────────────────────────────────────────────────────────────

def resize(
    input_path: str | Path,
    output_path: str | Path,
    resolution: str = DEFAULT_RESOLUTION,
    *,
    draft: bool = False,
) -> Path:
    """
    Resize an image or video to target resolution.

    Uses scale filter with pad to maintain aspect ratio (letterbox/pillarbox).

    Args:
        input_path: Source file.
        output_path: Destination file.
        resolution: Target "WxH" string (e.g. "1920x1080").
        draft: Use faster/smaller draft settings.

    Returns:
        Path to output file.
    """
    inp, out = Path(input_path), Path(output_path)
    _ensure_parent(out)

    w, h = resolution.split("x")
    # Scale to fit within WxH, pad to exact WxH
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"

    cmd = [
        FFMPEG, "-y", "-i", str(inp),
        "-vf", vf,
        str(out),
    ]
    _run(cmd)
    log.info("Resized %s → %s (%s)", inp.name, out.name, resolution)
    return out


# ─── Ken Burns ────────────────────────────────────────────────────────────────

def ken_burns(
    image_path: str | Path,
    output_path: str | Path,
    duration: float,
    animation: AnimationType = "zoom_in",
    resolution: str = DEFAULT_RESOLUTION,
    *,
    fps: int = DEFAULT_FPS,
    draft: bool = False,
) -> Path:
    """
    Apply Ken Burns effect to a still image to create a video clip.

    Animations:
      zoom_in    — slow zoom from 100% to 115% (dramatic reveal)
      zoom_out   — slow zoom from 115% to 100% (establishing shot)
      pan_left   — pan from right to left
      pan_right  — pan from left to right
      static     — no motion (simple image hold)

    Args:
        image_path: Source image (JPG, PNG).
        output_path: Output video file (.mp4).
        duration: Clip duration in seconds (matches audio block).
        animation: Type of Ken Burns motion.
        resolution: Output video resolution "WxH".
        fps: Output frames per second.
        draft: Skip Ken Burns for faster draft rendering.

    Returns:
        Path to output video file.
    """
    inp, out = Path(image_path), Path(output_path)
    _ensure_parent(out)

    w, h = (int(x) for x in resolution.split("x"))
    total_frames = int(duration * fps)
    crf = DRAFT_CRF if draft else DEFAULT_CRF
    preset = DRAFT_PRESET if draft else DEFAULT_PRESET
    res = DRAFT_RESOLUTION if draft else resolution

    if draft or animation == "static":
        # Simple static: image → video at target resolution
        dw, dh = (int(x) for x in res.split("x"))
        vf = (
            f"scale={dw}:{dh}:force_original_aspect_ratio=decrease,"
            f"pad={dw}:{dh}:(ow-iw)/2:(oh-ih)/2"
        )
        cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-i", str(inp),
            "-vf", vf,
            "-c:v", DEFAULT_VIDEO_CODEC, "-crf", str(crf),
            "-preset", preset,
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            str(out),
        ]
    else:
        # Overscan factor — work with slightly larger canvas for motion room
        overscan = 1.15
        ow, oh = int(w * overscan), int(h * overscan)

        if animation == "zoom_in":
            # Zoom from 1.0× to 1.15× (scale from w→ow over duration)
            vf = (
                f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},"
                f"zoompan=z='1+{(overscan-1)/total_frames:.6f}*on':"
                f"x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={w}x{h}:fps={fps},"
                f"scale={w}:{h}"
            )
        elif animation == "zoom_out":
            # Zoom from 1.15× to 1.0×
            vf = (
                f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},"
                f"zoompan=z='{overscan:.4f}-{(overscan-1)/total_frames:.6f}*on':"
                f"x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={w}x{h}:fps={fps},"
                f"scale={w}:{h}"
            )
        elif animation == "pan_left":
            # Pan from right to left
            vf = (
                f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},"
                f"zoompan=z='{overscan:.4f}':"
                f"x='iw/zoom*(on/{total_frames})':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={w}x{h}:fps={fps},"
                f"scale={w}:{h}"
            )
        elif animation == "pan_right":
            # Pan from left to right
            vf = (
                f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},"
                f"zoompan=z='{overscan:.4f}':"
                f"x='iw/zoom*(1-on/{total_frames})':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={w}x{h}:fps={fps},"
                f"scale={w}:{h}"
            )
        else:
            raise ValueError(f"Unknown animation type: {animation!r}")

        cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-i", str(inp),
            "-vf", vf,
            "-c:v", DEFAULT_VIDEO_CODEC, "-crf", str(crf),
            "-preset", preset,
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            str(out),
        ]

    _run(cmd)
    log.info("Ken Burns [%s] %s → %s (%.1fs)", animation, inp.name, out.name, duration)
    return out


# ─── Concat ───────────────────────────────────────────────────────────────────

def concat_videos(
    video_paths: list[str | Path],
    output_path: str | Path,
    *,
    crossfade: bool = True,
    crossfade_duration: float = CROSSFADE_DURATION,
) -> Path:
    """
    Concatenate multiple video files into one.

    With crossfade=True, applies 0.5s xfade transition between clips.
    Without crossfade, uses FFmpeg concat demuxer (fast, no re-encode).

    Args:
        video_paths: Ordered list of video files to concatenate.
        output_path: Destination file.
        crossfade: Apply smooth xfade transition between clips.
        crossfade_duration: Crossfade length in seconds.

    Returns:
        Path to concatenated output file.
    """
    if not video_paths:
        raise ValueError("video_paths cannot be empty")

    out = Path(output_path)
    _ensure_parent(out)

    if len(video_paths) == 1:
        import shutil
        shutil.copy2(video_paths[0], out)
        return out

    if not crossfade:
        # Fast path: concat demuxer (no re-encode)
        list_file = out.parent / "_concat_list.txt"
        list_file.write_text(
            "\n".join(f"file '{Path(p).resolve()}'" for p in video_paths),
            encoding="utf-8",
        )
        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(out),
        ]
        _run(cmd)
        list_file.unlink(missing_ok=True)
    else:
        # Crossfade path: chain xfade filters
        # Build filter_complex for N inputs with N-1 xfade transitions
        paths = [Path(p) for p in video_paths]
        durations = [get_duration(p) for p in paths]

        inputs = []
        for p in paths:
            inputs += ["-i", str(p)]

        # Build xfade filter chain
        # [0][1]xfade=...[v01]; [v01][2]xfade=...[v012]; ...
        filters = []
        offset = durations[0] - crossfade_duration

        prev_label = "[0:v]"
        for i in range(1, len(paths)):
            out_label = f"[v{i}]" if i < len(paths) - 1 else "[vout]"
            filters.append(
                f"{prev_label}[{i}:v]xfade=transition=fade:"
                f"duration={crossfade_duration}:offset={offset:.3f}{out_label}"
            )
            prev_label = out_label
            if i < len(paths) - 1:
                offset += durations[i] - crossfade_duration

        filter_complex = "; ".join(filters)

        cmd = [
            FFMPEG, "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", DEFAULT_VIDEO_CODEC,
            "-crf", str(DEFAULT_CRF),
            "-preset", DEFAULT_PRESET,
            "-pix_fmt", "yuv420p",
            str(out),
        ]
        _run(cmd)

    log.info("Concat %d clips → %s", len(video_paths), out.name)
    return out


# ─── Audio ────────────────────────────────────────────────────────────────────

def add_audio(
    video_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    shortest: bool = True,
) -> Path:
    """
    Mux an audio track onto a video file.

    Args:
        video_path: Source video (no audio or audio to replace).
        audio_path: Audio file (MP3, AAC, WAV).
        output_path: Output video with audio.
        shortest: Trim to shortest stream (audio or video).

    Returns:
        Path to output file.
    """
    inp, aud, out = Path(video_path), Path(audio_path), Path(output_path)
    _ensure_parent(out)

    cmd = [
        FFMPEG, "-y",
        "-i", str(inp),
        "-i", str(aud),
        "-c:v", "copy",
        "-c:a", DEFAULT_AUDIO_CODEC,
        "-b:a", DEFAULT_AUDIO_BITRATE,
        "-map", "0:v:0",
        "-map", "1:a:0",
    ]
    if shortest:
        cmd += ["-shortest"]
    cmd.append(str(out))

    _run(cmd)
    log.info("add_audio %s + %s → %s", inp.name, aud.name, out.name)
    return out


def normalize_audio(
    input_path: str | Path,
    output_path: str | Path,
) -> Path:
    """
    Normalize audio to EBU R128 loudness standard (-23 LUFS, LRA 7, TP -2).

    Two-pass loudnorm filter for precise normalization.

    Args:
        input_path: Source audio or video file.
        output_path: Normalized output file.

    Returns:
        Path to normalized file.
    """
    inp, out = Path(input_path), Path(output_path)
    _ensure_parent(out)

    # Pass 1: measure
    cmd_measure = [
        FFMPEG, "-y", "-i", str(inp),
        "-af", "loudnorm=I=-23:LRA=7:TP=-2:print_format=json",
        "-f", "null", "-",
    ]
    result = _run(cmd_measure, capture=True)

    # Parse measured values from stderr (ffmpeg outputs to stderr)
    import re
    json_match = re.search(r'\{[^}]+\}', result.stderr, re.DOTALL)
    if json_match:
        measured = json.loads(json_match.group())
        il = measured.get("input_i", "-23")
        lra = measured.get("input_lra", "7")
        tp = measured.get("input_tp", "-2")
        thresh = measured.get("input_thresh", "-33")
        offset = measured.get("target_offset", "0")
        loudnorm_filter = (
            f"loudnorm=I=-23:LRA=7:TP=-2:"
            f"measured_I={il}:measured_LRA={lra}:measured_TP={tp}:"
            f"measured_thresh={thresh}:offset={offset}:linear=true:print_format=summary"
        )
    else:
        # Fallback: single-pass
        loudnorm_filter = "loudnorm=I=-23:LRA=7:TP=-2"

    # Pass 2: apply
    cmd_apply = [
        FFMPEG, "-y", "-i", str(inp),
        "-af", loudnorm_filter,
        "-c:v", "copy",  # Pass through video stream if present
        str(out),
    ]
    _run(cmd_apply)
    log.info("Normalized audio: %s → %s", inp.name, out.name)
    return out


def mix_audio(
    voice_path: str | Path,
    music_path: str | Path,
    output_path: str | Path,
    *,
    music_volume: float = -20.0,  # dB relative (negative = quieter)
) -> Path:
    """
    Mix voice narration with background music track.

    Music is volume-adjusted and mixed under the voice.
    Music loops if shorter than voice; trimmed if longer.

    Args:
        voice_path: Primary voice narration audio.
        music_path: Background music file.
        output_path: Mixed output audio.
        music_volume: Music level in dB relative to voice (default -20dB).

    Returns:
        Path to mixed audio file.
    """
    vp, mp, out = Path(voice_path), Path(music_path), Path(output_path)
    _ensure_parent(out)

    voice_dur = get_duration(vp)

    # Loop music if needed, adjust volume, mix
    filter_complex = (
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:{voice_dur:.3f},"
        f"volume={music_volume:.1f}dB[music];"
        f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[aout]"
    )

    cmd = [
        FFMPEG, "-y",
        "-i", str(vp),
        "-i", str(mp),
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", DEFAULT_AUDIO_CODEC,
        "-b:a", DEFAULT_AUDIO_BITRATE,
        str(out),
    ]
    _run(cmd)
    log.info("mix_audio: voice=%s music=%s (%.0fdB) → %s", vp.name, mp.name, music_volume, out.name)
    return out


def concat_audio(
    audio_paths: list[str | Path],
    output_path: str | Path,
) -> Path:
    """
    Concatenate multiple audio files into one using concat demuxer.

    Args:
        audio_paths: Ordered list of audio files.
        output_path: Destination audio file.

    Returns:
        Path to concatenated audio.
    """
    if not audio_paths:
        raise ValueError("audio_paths cannot be empty")
    if len(audio_paths) == 1:
        import shutil
        out = Path(output_path)
        _ensure_parent(out)
        shutil.copy2(audio_paths[0], out)
        return out

    out = Path(output_path)
    _ensure_parent(out)

    list_file = out.parent / "_audio_concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(p).resolve()}'" for p in audio_paths),
        encoding="utf-8",
    )

    # Always re-encode to libmp3lame for .mp3 output.
    # Normalize sample rate/channels to handle mixed sources
    # (VoiceAPI 24kHz mono + VoidAI TTS 24kHz mono → uniform 44100 Hz stereo).
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-ar", "44100",
        "-ac", "1",
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        str(out),
    ]
    _run(cmd)
    list_file.unlink(missing_ok=True)
    log.info("concat_audio: %d files → %s", len(audio_paths), out.name)
    return out


# ─── Subtitles ────────────────────────────────────────────────────────────────

def add_subtitles(
    video_path: str | Path,
    subtitle_path: str | Path,
    output_path: str | Path,
) -> Path:
    """
    Burn subtitles (ASS or SRT) into video.

    Uses FFmpeg subtitles/ass filter (hardcoded subtitles).
    ASS format is preferred for custom styling.

    Args:
        video_path: Source video file.
        subtitle_path: ASS or SRT subtitle file.
        output_path: Output video with burned-in subtitles.

    Returns:
        Path to output file.
    """
    vid, subs, out = Path(video_path), Path(subtitle_path), Path(output_path)
    _ensure_parent(out)

    # Escape path for FFmpeg filter string: \ → /, then : → \: (colon is option separator)
    subs_escaped = str(subs).replace("\\", "/").replace(":", "\\:")

    ext = subs.suffix.lower()
    if ext == ".ass":
        vf = f"ass='{subs_escaped}'"
    else:
        # SRT — use subtitles filter (converts to ASS internally)
        vf = f"subtitles='{subs_escaped}'"

    cmd = [
        FFMPEG, "-y",
        "-i", str(vid),
        "-vf", vf,
        "-c:v", DEFAULT_VIDEO_CODEC,
        "-crf", str(DEFAULT_CRF),
        "-preset", DEFAULT_PRESET,
        "-c:a", "copy",
        str(out),
    ]
    _run(cmd)
    log.info("add_subtitles %s + %s → %s", vid.name, subs.name, out.name)
    return out


# ─── Intro / Outro ────────────────────────────────────────────────────────────

def prepend_outro_video(
    main_video: str | Path,
    template_video: str | Path,
    output_path: str | Path,
    *,
    mode: Literal["prepend", "append"] = "prepend",
) -> Path:
    """
    Prepend (intro) or append (outro) a channel template video to the main video.

    Template is re-encoded to match main video specs if needed.

    Args:
        main_video: The main content video.
        template_video: Intro or outro template file.
        output_path: Combined output file.
        mode: "prepend" = template first, "append" = template last.

    Returns:
        Path to combined video.
    """
    main, tmpl, out = Path(main_video), Path(template_video), Path(output_path)
    _ensure_parent(out)

    if mode == "prepend":
        parts = [tmpl, main]
    else:
        parts = [main, tmpl]

    return concat_videos(parts, out, crossfade=False)


# ─── Extract audio ────────────────────────────────────────────────────────────

def extract_audio(
    video_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Extract audio track from a video file."""
    vid, out = Path(video_path), Path(output_path)
    _ensure_parent(out)

    cmd = [
        FFMPEG, "-y",
        "-i", str(vid),
        "-vn",
        "-c:a", DEFAULT_AUDIO_CODEC,
        "-b:a", DEFAULT_AUDIO_BITRATE,
        str(out),
    ]
    _run(cmd)
    log.info("extract_audio %s → %s", vid.name, out.name)
    return out


# ─── FFmpeg version check ─────────────────────────────────────────────────────

def check_ffmpeg() -> tuple[str, str]:
    """
    Verify FFmpeg and FFprobe are installed and return their versions.

    Returns:
        (ffmpeg_version, ffprobe_version) strings.

    Raises:
        RuntimeError: If FFmpeg or FFprobe is not found.
    """
    def _version(binary: str) -> str:
        try:
            r = subprocess.run(
                [binary, "-version"],
                capture_output=True, text=True, encoding="utf-8",
            )
            return r.stdout.split("\n")[0]
        except FileNotFoundError:
            raise RuntimeError(
                f"'{binary}' not found. Install FFmpeg: https://ffmpeg.org/download.html"
            )

    ffmpeg_v = _version(FFMPEG)
    ffprobe_v = _version(FFPROBE)
    log.info("FFmpeg: %s", ffmpeg_v)
    log.info("FFprobe: %s", ffprobe_v)
    return ffmpeg_v, ffprobe_v


# ─── CLI self-test ────────────────────────────────────────────────────────────

def _self_test() -> None:
    """Run when executed directly: python utils/ffmpeg_utils.py"""
    import argparse

    parser = argparse.ArgumentParser(description="FFmpeg utils — self-test")
    parser.add_argument("--image", help="Test image path for Ken Burns effect")
    parser.add_argument("--audio", help="Test audio path for duration/normalize")
    parser.add_argument("--animation", default="zoom_in",
                        choices=["zoom_in", "zoom_out", "pan_left", "pan_right", "static"],
                        help="Ken Burns animation type")
    parser.add_argument("--duration", type=float, default=5.0, help="Test clip duration")
    parser.add_argument("--output-dir", default="projects/test_ffmpeg", help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Test 1: FFmpeg version check
    log.info("--- Test 1: FFmpeg version ---")
    try:
        ffv, fpv = check_ffmpeg()
        log.info("FFmpeg OK: %s", ffv[:60])
        log.info("FFprobe OK: %s", fpv[:60])
    except RuntimeError as exc:
        log.error("FFmpeg not found: %s", exc)
        return

    # Test 2: Ken Burns (if image provided)
    if args.image:
        log.info("--- Test 2: Ken Burns [%s] ---", args.animation)
        img = Path(args.image)
        if not img.exists():
            log.error("Image not found: %s", img)
        else:
            out_clip = out_dir / f"ken_burns_{args.animation}.mp4"
            ken_burns(img, out_clip, args.duration, args.animation)
            dur = get_duration(out_clip)
            log.info("Ken Burns output: %s (%.2fs)", out_clip, dur)

    # Test 3: Audio duration (if audio provided)
    if args.audio:
        log.info("--- Test 3: Audio duration + normalize ---")
        aud = Path(args.audio)
        if not aud.exists():
            log.error("Audio not found: %s", aud)
        else:
            dur = get_duration(aud)
            log.info("Duration: %.3fs", dur)
            out_norm = out_dir / f"normalized_{aud.name}"
            normalize_audio(aud, out_norm)
            log.info("Normalized: %s", out_norm)

    log.info("ffmpeg_utils.py self-test OK")


if __name__ == "__main__":
    _self_test()
