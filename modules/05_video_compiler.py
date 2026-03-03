"""
VideoForge — Module 05: Video Compiler.

images/ + audio/ + subtitles.ass → final.mp4 (1920x1080 H.264).

Pipeline per block:
  image → ken_burns() → block_NNN.mp4

Then:
  concat_videos() [with crossfade]
  → add_audio() [full_narration_normalized.mp3]
  → (optional) mix_audio() [background music at -20dB]
  → (optional) add_subtitles() [burn-in ASS]
  → (optional) prepend intro / append outro
  → final.mp4

--draft mode: 854x480, no Ken Burns, no crossfade, ultrafast encoding.

CLI:
    python modules/05_video_compiler.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json

    python modules/05_video_compiler.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json --draft
"""

import json
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_channel_config, load_env, setup_logging
from utils.ffmpeg_utils import (
    CROSSFADE_DURATION,
    DEFAULT_RESOLUTION,
    DRAFT_RESOLUTION,
    add_audio,
    add_subtitles,
    check_ffmpeg,
    concat_videos,
    ken_burns,
    mix_audio,
    prepend_outro_video,
    resize,
    static_slideshow,
)

log = setup_logging("video_compiler")

# ─── Constants ────────────────────────────────────────────────────────────────

MIN_IMAGE_BYTES = 5_000
MIN_AUDIO_BYTES = 1_000
BLOCK_VIDEO_EXT = ".mp4"

# Inter-block animation cycle (applied at block boundaries with crossfade).
# Only zoom_in/zoom_out — pan_left/pan_right cause visible jerks at transitions.
_KB_CYCLE = ["zoom_in", "zoom_out"]

# Within-block animation cycle for multi-segment blocks.
# ONLY zoom_in/zoom_out — they chain SEAMLESSLY at hard-cut boundaries (zoompan):
#   zoom_in  last frame:  z≈1.15  →  x = iw/2 - iw/1.15/2  (centered, zoomed)
#   zoom_out first frame: z=1.15  →  x = iw/2 - iw/1.15/2  ← identical ✓
#   zoom_out last frame:  z≈1.0   →  x = 0                  (full frame)
#   zoom_in  first frame: z=1.0   →  x = 0                  ← identical ✓
# No crossfade needed → block duration preserved exactly → no audio sync loss.
_WITHIN_BLOCK_KB_CYCLE = ["zoom_in", "zoom_out"]

# Fixed duration of a single animation segment (zoom_in or zoom_out).
# Two animations form one visual cycle: zoom_in(10s) + zoom_out(10s) = 20s loop.
# This repeats to fill the full block duration regardless of video position or tier.
ANIM_SEGMENT_DURATION = 10.0

# Default image frequency tiers — used by master_script_v2.txt and test_components.py
# to describe IMAGE_PROMPT density in the LLM prompt. NOT used for segment splitting.
#   Tier 1 (0–3 min):  every 10s  → ~25 words → one IMAGE_PROMPT per ~10s of speech
#   Tier 2 (3–6 min):  every 20s  → ~50 words
#   Tier 3 (6–15 min): every 60s  → ~150 words
#   Tier 4 (15+ min):  every 120s → ~280 words
_DEFAULT_FREQ_TIERS: list[dict] = [
    {"until_seconds": 180,  "interval": 10},   # 0–3 min:  every 10s
    {"until_seconds": 360,  "interval": 20},   # 3–6 min:  every 20s
    {"until_seconds": 900,  "interval": 60},   # 6–15 min: every 60s
    {"until_seconds": None, "interval": 120},  # 15+ min:  every 2 min
]


def _get_interval_for_time(t: float, tiers: list[dict]) -> float:
    """Return the image-change interval (seconds) for a given video timestamp t."""
    for tier in tiers:
        until = tier.get("until_seconds")
        if until is None or t < until:
            return float(tier.get("interval", 20))
    return 20.0


def _split_duration_to_segments(
    start_time: float,
    duration: float,
    tiers: list[dict],
) -> list[float]:
    """
    Split a block's audio duration into fixed ANIM_SEGMENT_DURATION animation segments.

    Each segment gets its own ken_burns() clip (zoom_in / zoom_out alternating).
    Result: zoom_in(10s) + zoom_out(10s) loop repeating for the full block duration.

    Example — 90s block:
      [10, 10, 10, 10, 10, 10, 10, 10, 10]  (9 × 10s)
      animations: zoom_in, zoom_out, zoom_in, zoom_out, ... (seamless hard cuts)

    Args:
        start_time: Kept for API compatibility (no longer used for tier selection).
        duration:   Block audio duration in seconds.
        tiers:      Kept for API compatibility (no longer used for segment sizing).

    Returns:
        List of segment durations summing to `duration` (always at least one element).
    """
    segments: list[float] = []
    remaining = duration
    while remaining > 0.01:   # 10ms floor to avoid float-noise micro-segments
        seg_dur = min(ANIM_SEGMENT_DURATION, remaining)
        segments.append(seg_dur)
        remaining -= seg_dur
    return segments if segments else [duration]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _find_narration_audio(audio_dir: Path) -> Path | None:
    """Find best available narration audio file."""
    for name in ("full_narration_normalized.mp3", "full_narration.mp3"):
        p = audio_dir / name
        if p.exists() and p.stat().st_size >= MIN_AUDIO_BYTES:
            return p
    return None


def _find_music_track(music_dir: str | Path, random_pick: bool = True) -> Path | None:
    """Pick a background music track from directory."""
    d = Path(music_dir)
    if not d.exists():
        return None
    tracks = sorted(d.glob("*.mp3")) + sorted(d.glob("*.m4a")) + sorted(d.glob("*.wav"))
    if not tracks:
        return None
    return random.choice(tracks) if random_pick else tracks[0]


def _get_block_images(
    block: dict[str, Any],
    images_dir: Path,
    prev_image: Path | None,
) -> list[Path]:
    """
    Return list of image paths for a block.

    Checks primary {block_id}.png, then additional {block_id}_1.png, {block_id}_2.png, etc.
    (additional images are generated from image_prompts[1:] by module 02).

    Falls back to [prev_image] if no images found (e.g. CTA blocks without image_prompt).
    """
    block_id = block["id"]
    images: list[Path] = []

    # Primary image (always {block_id}.png — backward compatible)
    primary = images_dir / f"{block_id}.png"
    if primary.exists() and primary.stat().st_size >= MIN_IMAGE_BYTES:
        images.append(primary)

    # Additional images from image_prompts[1:] ({block_id}_1.png, {block_id}_2.png, …)
    idx = 1
    while True:
        extra = images_dir / f"{block_id}_{idx}.png"
        if extra.exists() and extra.stat().st_size >= MIN_IMAGE_BYTES:
            images.append(extra)
            idx += 1
        else:
            break

    if images:
        return images

    # Fallback: hold previous block's image
    if prev_image and prev_image.exists():
        log.debug("Block %s: no image — holding previous: %s", block_id, prev_image.name)
        return [prev_image]

    return []


def _image_for_segment(
    image_list: list[Path],
    word_offsets: list[int],
    total_words: int,
    seg_idx: int,
    n_segments: int,
) -> Path:
    """
    Select the appropriate image for a given animation segment.

    Uses LLM-placed word offsets to sync images to narration timing instead of
    uniform cycling.  Each image is shown from its word-offset position until the
    next image's word-offset position (or end of block).

    Args:
        image_list:    Ordered list of available image paths for this block.
        word_offsets:  Word position in the narration where each image was placed by the LLM.
                       Parallel to image_list (len must match or be ≤ len(image_list)).
        total_words:   Total words in the block's narration.
        seg_idx:       Current 10-second segment index (0-based).
        n_segments:    Total number of segments in this block.

    Returns:
        Path to the image that should play during this segment.
    """
    # If no offset data (v1 script / fallback) — fall back to simple cycling
    if not word_offsets or len(word_offsets) != len(image_list):
        return image_list[seg_idx % len(image_list)]

    if total_words <= 0:
        return image_list[0]

    # Convert segment index to approximate word position in narration
    seg_word_pos = int((seg_idx / n_segments) * total_words)

    # Find the last image whose word_offset ≤ seg_word_pos
    chosen_idx = 0
    for i, offset in enumerate(word_offsets):
        if offset <= seg_word_pos:
            chosen_idx = i
        else:
            break

    return image_list[chosen_idx]


def _animation_for_block(
    block: dict[str, Any],
    channel_config: dict[str, Any],
    block_index: int = 0,
) -> str:
    """Return animation type for a block.

    If the block has an explicit non-default animation set in script.json, use that.
    Otherwise rotate through _KB_CYCLE: zoom_in → pan_left → zoom_in → pan_right → …
    """
    anim = block.get("animation", "")
    if anim and anim not in ("zoom_in", ""):
        return anim
    return _KB_CYCLE[block_index % len(_KB_CYCLE)]


# ─── Main compiler ────────────────────────────────────────────────────────────

def compile_video(
    script_path: str | Path,
    channel_config_path: str | Path,
    *,
    output_path: str | Path | None = None,
    draft: bool = False,
    no_subs: bool = False,
    no_music: bool = False,
    no_intro_outro: bool = False,
    crossfade: bool = True,
    no_ken_burns: bool = False,
    dry_run: bool = False,
    progress_callback: Any | None = None,
) -> Path:
    """
    Compile final video from script.json resources.

    Args:
        script_path: Path to script.json (with audio_duration per block).
        channel_config_path: Path to channel config JSON.
        output_path: Where to save final.mp4. Default: script.parent/output/final.mp4.
        draft: 854x480, no Ken Burns, no crossfade — fast preview.
        no_subs: Skip subtitle burn-in.
        no_music: Skip background music mixing.
        no_intro_outro: Skip intro/outro templates.
        crossfade: Enable 0.5s crossfade between blocks (ignored in draft mode).
        dry_run: Validate inputs and log plan without running FFmpeg.
        progress_callback: Optional callable({type, pct, message}) for real-time
            sub-step progress (pct in 0–100 within this step).

    Returns:
        Path to final.mp4.

    Raises:
        FileNotFoundError: If required inputs are missing.
        RuntimeError: If FFmpeg fails.
    """
    load_env()

    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found: {script_path}")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)

    base_dir    = script_path.parent
    images_dir  = base_dir / "images"
    audio_dir   = base_dir / "audio"
    subs_dir    = base_dir / "subtitles"
    out_dir     = base_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = Path(output_path) if output_path else out_dir / "final.mp4"

    # ── Validate FFmpeg ──
    try:
        ffmpeg_ver, ffprobe_ver = check_ffmpeg()
        log.info("FFmpeg: %s | FFprobe: %s", ffmpeg_ver, ffprobe_ver)
    except FileNotFoundError as exc:
        raise RuntimeError(f"FFmpeg not found: {exc}") from exc

    # ── Resolve blocks ──
    blocks = script.get("blocks", [])
    voiced_blocks = [b for b in blocks if b.get("audio_duration") and b.get("narration", "").strip()]

    if not voiced_blocks:
        raise ValueError(
            "No blocks with audio_duration found. Run Voice Generator (module 03) first."
        )

    resolution = DRAFT_RESOLUTION if draft else DEFAULT_RESOLUTION
    use_crossfade = crossfade and not draft
    crossfade_dur = float(channel_config.get("crossfade_duration", CROSSFADE_DURATION))

    log.info(
        "Compiling %d blocks | %s | crossfade=%s | draft=%s",
        len(voiced_blocks), resolution, use_crossfade, draft,
    )

    # ── Find narration audio ──
    narration_audio = _find_narration_audio(audio_dir)
    if not narration_audio:
        raise FileNotFoundError(
            f"full_narration.mp3 not found in {audio_dir}. "
            "Run Voice Generator (module 03) first."
        )
    log.info("Narration audio: %s", narration_audio.name)

    # ── Find subtitles ──
    subs_file: Path | None = None
    if not no_subs:
        for name in ("subtitles.ass", "subtitles.srt"):
            p = subs_dir / name
            if p.exists():
                subs_file = p
                log.info("Subtitles: %s", p.name)
                break
        if not subs_file:
            log.info("No subtitle file found — skipping subtitle burn-in")

    # ── Find background music ──
    music_track: Path | None = None
    if not no_music:
        music_cfg = channel_config.get("background_music", {})
        if music_cfg:
            tracks_dir = music_cfg.get("tracks_dir", "")
            random_pick = music_cfg.get("random", True)
            if tracks_dir:
                p = Path(tracks_dir)
                if not p.is_absolute():
                    p = ROOT / p
                music_track = _find_music_track(p, random_pick)
                if music_track:
                    log.info("Background music: %s", music_track.name)
                else:
                    log.info("No music tracks found in %s", tracks_dir)

    # ── Intro / Outro ──
    intro_video: Path | None = None
    outro_video: Path | None = None
    if not no_intro_outro:
        for key, var_name in [("intro_video", "intro_video"), ("outro_video", "outro_video")]:
            cfg_path = channel_config.get(key, "")
            if cfg_path:
                p = Path(cfg_path)
                if not p.is_absolute():
                    p = ROOT / p
                if p.exists():
                    if key == "intro_video":
                        intro_video = p
                        log.info("Intro: %s", p.name)
                    else:
                        outro_video = p
                        log.info("Outro: %s", p.name)
                else:
                    log.debug("%s not found: %s", key, p)

    if dry_run:
        n_images = sum(
            1 for b in voiced_blocks
            if (images_dir / f"{b['id']}.png").exists()
        )
        total_dur = sum(b["audio_duration"] for b in voiced_blocks)
        log.info(
            "[DRY RUN] Would compile: %d blocks | %d images found | %.1fs total audio",
            len(voiced_blocks), n_images, total_dur,
        )
        log.info("[DRY RUN] Output: %s | Resolution: %s", out_path, resolution)
        return out_path

    t0 = time.monotonic()

    def _emit_progress(pct: float, message: str = "") -> None:
        """Emit a sub_progress event via progress_callback (if set)."""
        if progress_callback:
            try:
                progress_callback({"type": "sub_progress", "pct": round(pct, 1), "message": message})
            except Exception:
                pass

    with tempfile.TemporaryDirectory(prefix="vf_compile_") as tmp_str:
        tmp = Path(tmp_str)

        # ── Step 1: Video from images ──
        n_blocks = len(voiced_blocks)
        use_static = no_ken_burns or draft
        log.info(
            "Step 1: Building video from %d blocks (mode=%s)...",
            n_blocks, "static" if use_static else "ken_burns",
        )

        video_raw = tmp / "video_raw.mp4"

        if use_static:
            # ── Fast path: ONE FFmpeg call via concat demuxer ──
            # No per-block processes, no temp .mp4 files — images go directly to video.
            _emit_progress(5.0, "Building slideshow…")
            prev_image: Path | None = None
            frames: list[tuple[Path, float]] = []
            for block in voiced_blocks:
                duration = float(block["audio_duration"])
                image_path = _get_block_image(block, images_dir, prev_image)
                if image_path is None:
                    log.warning("Block %s: no image — using black", block["id"])
                    # Reuse previous or skip; ffmpeg concat requires a file
                    if prev_image:
                        image_path = prev_image
                    else:
                        continue
                frames.append((image_path, duration))
                prev_image = image_path

            static_res = DRAFT_RESOLUTION if draft else resolution
            static_slideshow(frames, video_raw, resolution=static_res)
            _emit_progress(75.0, "Slideshow done")

        else:
            # ── Normal path: Ken Burns per block with image-frequency splitting ──
            #
            # Architecture (critical for audio sync):
            #   WITHIN block: fixed 10s segments concat WITHOUT crossfade (hard cuts —
            #     invisible because zoom_in/zoom_out share identical zoompan z/x/y at boundary).
            #     zoom_in(10s) + zoom_out(10s) = 20s loop, repeating for the full block.
            #     → block duration preserved exactly, no audio trim.
            #   BETWEEN blocks: crossfade 0.5s as before.
            #     → only N_blocks-1 crossfades (acceptable trim).
            #
            # If all 10s segments used crossfade, 80+ segments × 0.5s ≈ 40s audio would be cut!
            freq_cfg   = channel_config.get("image_frequency", {})
            freq_on    = freq_cfg.get("enabled", True)
            freq_tiers = freq_cfg.get("tiers", _DEFAULT_FREQ_TIERS) if freq_on else None

            block_videos: list[Path] = []   # one video per block (segments pre-merged)
            prev_image: Path | None = None
            elapsed_video_time = 0.0
            _kb_idx = 0                     # inter-block animation cycle counter

            for i, block in enumerate(voiced_blocks, 1):
                duration    = float(block["audio_duration"])
                image_list  = _get_block_images(block, images_dir, prev_image)

                if not image_list:
                    log.warning("Block %s: no image — creating black frame", block["id"])
                    clip_path = tmp / f"clip_{block['id']}_black{BLOCK_VIDEO_EXT}"
                    from utils.ffmpeg_utils import _run  # noqa: PLC0415
                    _run([
                        "ffmpeg", "-y",
                        "-f", "lavfi", "-i", f"color=c=black:size={resolution}:rate=30",
                        "-t", str(duration),
                        "-c:v", "libx264", "-crf", "22", str(clip_path),
                    ])
                    block_videos.append(clip_path)
                    _kb_idx += 1

                else:
                    segments = (
                        _split_duration_to_segments(elapsed_video_time, duration, freq_tiers)
                        if freq_tiers else [duration]
                    )

                    if len(segments) == 1:
                        # Short block — single clip, use inter-block cycle for variety
                        animation = _KB_CYCLE[_kb_idx % len(_KB_CYCLE)]
                        _kb_idx += 1
                        clip_path = tmp / f"clip_{block['id']}_00{BLOCK_VIDEO_EXT}"
                        ken_burns(
                            image_list[0], clip_path,
                            duration=segments[0],
                            animation=animation,
                            resolution=resolution,
                        )
                        block_videos.append(clip_path)
                        log.info(
                            "  [%d/%d] %s (%.1fs, %s, %d img)",
                            i, n_blocks, block["id"], duration, animation, len(image_list),
                        )

                    else:
                        # Long block — 10s zoom_in/zoom_out loop, seamlessly hard-cut.
                        # Image assignment is word-offset-aware: each image shows when the
                        # narration reaches the word position where the LLM placed it.
                        # Falls back to simple cycling for v1 scripts without offset data.
                        #
                        # Seamless zoom chain (identical zoompan z/x/y at hard-cut boundary):
                        #   zoom_in  last:  z≈1.15, x=iw/2-iw/1.15/2  (centered)
                        #   zoom_out first: z=1.15, x=iw/2-iw/1.15/2  ← identical ✓
                        word_offsets: list[int] = block.get("image_word_offsets", [])
                        total_words: int = len(block.get("narration", "").split())
                        n_segments = len(segments)
                        seg_clips: list[Path] = []
                        for seg_idx, seg_dur in enumerate(segments):
                            within_anim = _WITHIN_BLOCK_KB_CYCLE[seg_idx % len(_WITHIN_BLOCK_KB_CYCLE)]
                            # Timing-aware image selection: show image aligned to narration position
                            seg_image = _image_for_segment(
                                image_list, word_offsets, total_words, seg_idx, n_segments,
                            )
                            seg_path = tmp / f"clip_{block['id']}_{seg_idx:02d}{BLOCK_VIDEO_EXT}"
                            ken_burns(
                                seg_image, seg_path,
                                duration=seg_dur,
                                animation=within_anim,
                                resolution=resolution,
                            )
                            seg_clips.append(seg_path)

                        # Merge segments WITHOUT crossfade — hard cuts, duration preserved
                        block_clip = tmp / f"block_{block['id']}{BLOCK_VIDEO_EXT}"
                        concat_videos(seg_clips, block_clip, crossfade=False)
                        block_videos.append(block_clip)
                        _kb_idx += 1
                        log.info(
                            "  [%d/%d] %s (%.1fs → %d segs, %d imgs, zoom_in↔zoom_out)",
                            i, n_blocks, block["id"], duration, len(segments), len(image_list),
                        )

                if image_list:
                    prev_image = image_list[0]   # use primary for continuity fallback
                elapsed_video_time += duration
                _emit_progress(i / n_blocks * 75.0, f"Block {i}/{n_blocks}")

            if not block_videos:
                raise RuntimeError("No video clips generated")

            # Crossfade only between BLOCKS (N_blocks-1 transitions, not N_segments-1)
            _emit_progress(76.0, "Concatenating blocks…")
            concat_videos(
                block_videos, video_raw,
                crossfade=use_crossfade,
                crossfade_duration=crossfade_dur,
            )
            _emit_progress(82.0, "Concat done")

        # ── Step 2: Mix audio (narration + background music) ──
        step = 2
        final_audio = narration_audio
        if music_track:
            log.info("Step %d: Mixing background music at -20dB...", step)
            music_volume = float(
                channel_config.get("background_music", {}).get("volume_db", -20)
            )
            mixed_audio = tmp / "audio_mixed.mp3"
            _emit_progress(84.0, "Mixing music…")
            mix_audio(narration_audio, music_track, mixed_audio, music_volume=music_volume)
            final_audio = mixed_audio
            step += 1

        # ── Step 4: Add audio track ──
        log.info("Step %d: Adding audio track (%s)...", step, final_audio.name)
        video_with_audio = tmp / "video_with_audio.mp4"
        _emit_progress(88.0, "Adding audio…")
        add_audio(video_raw, final_audio, video_with_audio, shortest=True)
        current_video = video_with_audio
        _emit_progress(94.0, "Audio done")

        # ── Step 5: Burn subtitles ──
        step += 1
        if subs_file:
            log.info("Step %d: Burning subtitles (%s)...", step, subs_file.name)
            video_with_subs = tmp / "video_with_subs.mp4"
            add_subtitles(current_video, subs_file, video_with_subs)
            current_video = video_with_subs
            step += 1

        # ── Step 6: Intro ──
        if intro_video:
            log.info("Step %d: Prepending intro...", step)
            video_with_intro = tmp / "video_with_intro.mp4"
            prepend_outro_video(current_video, intro_video, video_with_intro, mode="prepend")
            current_video = video_with_intro
            step += 1

        # ── Step 7: Outro ──
        if outro_video:
            log.info("Step %d: Appending outro...", step)
            video_with_outro = tmp / "video_with_outro.mp4"
            prepend_outro_video(current_video, outro_video, video_with_outro, mode="append")
            current_video = video_with_outro

        # ── Final: Move to output ──
        import shutil
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current_video, out_path)

    elapsed = time.monotonic() - t0
    size_mb  = out_path.stat().st_size / 1e6
    log.info(
        "Done: %s (%.1f MB) in %.1fs",
        out_path.name, size_mb, elapsed,
    )
    return out_path


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — Video Compiler (Module 05)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python modules/05_video_compiler.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json

  # Draft mode (fast 480p preview):
  python modules/05_video_compiler.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json --draft

  # No subtitles, no music:
  python modules/05_video_compiler.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json \\
      --no-subs --no-music

  # Dry run:
  python modules/05_video_compiler.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json --dry-run
        """,
    )

    parser.add_argument(
        "--script",
        required=True,
        help="Path to script.json (with audio_duration per block)",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel config JSON path",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for final.mp4 (default: script.json dir / output/final.mp4)",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Fast draft mode: 854x480, no Ken Burns, no crossfade, ultrafast encode",
    )
    parser.add_argument(
        "--no-subs",
        action="store_true",
        help="Skip subtitle burn-in",
    )
    parser.add_argument(
        "--no-music",
        action="store_true",
        help="Skip background music mixing",
    )
    parser.add_argument(
        "--no-intro-outro",
        action="store_true",
        help="Skip intro/outro video templates",
    )
    parser.add_argument(
        "--no-crossfade",
        action="store_true",
        help="Disable crossfade between clips",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and show plan without running FFmpeg",
    )

    args = parser.parse_args()

    out = compile_video(
        script_path=args.script,
        channel_config_path=args.channel,
        output_path=args.output,
        draft=args.draft,
        no_subs=args.no_subs,
        no_music=args.no_music,
        no_intro_outro=args.no_intro_outro,
        crossfade=not args.no_crossfade,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        log.info("Final video: %s", out)


if __name__ == "__main__":
    _main()
