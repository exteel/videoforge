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
_KB_CYCLE = ["zoom_in", "pan_left", "zoom_in", "pan_right"]

# Within-block animation cycle for multi-segment blocks.
# ONLY zoom_in/zoom_out — they chain SEAMLESSLY at hard-cut boundaries:
#   zoom_in  ends at: crop 1920×1080 center → zoom_out starts at: crop 1920×1080 center ✓
#   zoom_out ends at: crop 2208×1242 wide   → zoom_in  starts at: crop 2208×1242 wide   ✓
# No crossfade needed → block duration preserved exactly → no audio sync loss.
_WITHIN_BLOCK_KB_CYCLE = ["zoom_in", "zoom_out"]

# Default image frequency tiers when not configured in channel config.
# Tiers are matched against elapsed VIDEO TIME (not block time).
# Each tier fires until `until_seconds` is reached; last tier (until_seconds=None) is the catch-all.
#
# Mirrors the 4-tier density model in master_script_v2.txt:
#   Tier 1 (0–3 min)  — Hook zone:       short segments, max visual variety → every 10s
#   Tier 2 (3–6 min)  — Engagement zone: sustain attention, core content   → every 20s
#   Tier 3 (6–15 min) — Depth zone:      viewer invested, fewer cuts        → every 60s
#   Tier 4 (15+ min)  — Long-form zone:  image = chapter anchor, not pacing → every 120s
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
    Split a block's audio duration into image segments based on frequency tiers.

    Each segment will get its own ken_burns() clip using the same image
    but a different animation from _KB_CYCLE — giving the visual effect of
    the image "changing" every 10–20 seconds rather than holding for the full block.

    Args:
        start_time: Video timestamp (seconds) where this block begins.
        duration:   Block audio duration in seconds.
        tiers:      Image frequency tier config (list of dicts with 'until_seconds', 'interval').

    Returns:
        List of segment durations summing to `duration` (always at least one element).
    """
    segments: list[float] = []
    remaining = duration
    t = start_time
    while remaining > 0.01:   # 10ms floor to avoid float-noise micro-segments
        interval = _get_interval_for_time(t, tiers)
        seg_dur = min(interval, remaining)
        segments.append(seg_dur)
        t += seg_dur
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


def _get_block_image(
    block: dict[str, Any],
    images_dir: Path,
    prev_image: Path | None,
) -> Path | None:
    """
    Return image path for a block.

    Falls back to the previous block's image (e.g. for CTA blocks with no image_prompt).
    """
    img = images_dir / f"{block['id']}.png"
    if img.exists() and img.stat().st_size >= MIN_IMAGE_BYTES:
        return img
    # Hold previous image for blocks without generated images
    if prev_image and prev_image.exists():
        log.debug("Block %s: no image — holding previous: %s", block["id"], prev_image.name)
        return prev_image
    return None


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
            #   WITHIN block: segments concat WITHOUT crossfade (hard cuts — invisible
            #     because zoom_in/zoom_out start+end at identical crop positions).
            #     → block duration preserved exactly, no audio trim.
            #   BETWEEN blocks: crossfade 0.5s as before.
            #     → only N_blocks-1 crossfades (7 for 8 blocks = 3.5s trim, acceptable).
            #
            # If all segments used crossfade, 80+ segments × 0.5s ≈ 40s audio would be cut!
            freq_cfg   = channel_config.get("image_frequency", {})
            freq_on    = freq_cfg.get("enabled", True)
            freq_tiers = freq_cfg.get("tiers", _DEFAULT_FREQ_TIERS) if freq_on else None

            block_videos: list[Path] = []   # one video per block (segments pre-merged)
            prev_image: Path | None = None
            elapsed_video_time = 0.0
            _kb_idx = 0                     # inter-block animation cycle counter

            for i, block in enumerate(voiced_blocks, 1):
                duration   = float(block["audio_duration"])
                image_path = _get_block_image(block, images_dir, prev_image)

                if image_path is None:
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
                            image_path, clip_path,
                            duration=segments[0],
                            animation=animation,
                            resolution=resolution,
                        )
                        block_videos.append(clip_path)
                        log.info(
                            "  [%d/%d] %s (%.1fs, %s)",
                            i, n_blocks, block["id"], duration, animation,
                        )

                    else:
                        # Long block — split into seamlessly-chainable zoom_in/zoom_out segments.
                        # Hard cuts between them are mathematically invisible:
                        #   zoom_in  at t=T: crop 1920×1080 at x=144,y=81 (center)
                        #   zoom_out at t=0: crop 1920×1080 at x=144,y=81 (center) ← identical
                        #   zoom_out at t=T: crop 2208×1242 at x=0,y=0   (wide)
                        #   zoom_in  at t=0: crop 2208×1242 at x=0,y=0   (wide)   ← identical
                        seg_clips: list[Path] = []
                        for seg_idx, seg_dur in enumerate(segments):
                            within_anim = _WITHIN_BLOCK_KB_CYCLE[seg_idx % len(_WITHIN_BLOCK_KB_CYCLE)]
                            seg_path = tmp / f"clip_{block['id']}_{seg_idx:02d}{BLOCK_VIDEO_EXT}"
                            ken_burns(
                                image_path, seg_path,
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
                            "  [%d/%d] %s (%.1fs → %d segments, zoom_in↔zoom_out)",
                            i, n_blocks, block["id"], duration, len(segments),
                        )

                if image_path:
                    prev_image = image_path
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
