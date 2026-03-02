"""
VideoForge — Component Test Script.

Tests individual compilation components without running the full pipeline.
Run from project root:

    python tests/test_components.py --all
    python tests/test_components.py --ken-burns
    python tests/test_components.py --music
    python tests/test_components.py --freq-tiers
    python tests/test_components.py --video-info <path_to_final.mp4>
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import setup_logging
from utils.ffmpeg_utils import (
    add_audio,
    get_duration,
    ken_burns,
    mix_audio,
)

log = setup_logging("test_components")


# ── Test 1: Ken Burns smoothness ──────────────────────────────────────────────

def test_ken_burns(image_path: Path | None = None, duration: float = 8.0) -> None:
    """
    Generate a short Ken Burns test clip and report its actual duration.
    Opens the clip if a viewer is available.

    Checks:
    - Clip generates without error
    - Output duration matches requested duration (±0.1s)
    - File size > 50KB (non-empty)
    """
    print("\n=== TEST: Ken Burns Animation ===")

    # Find a test image
    if image_path is None:
        # Look for any image in projects/
        candidates = list(ROOT.glob("projects/**/images/*.png"))
        if not candidates:
            candidates = list(ROOT.glob("projects/**/*.jpg"))
        if not candidates:
            print("  SKIP: No test image found. Pass --image <path>")
            return
        image_path = candidates[0]

    print(f"  Image: {image_path}")
    print(f"  Duration: {duration}s")

    out_dir = ROOT / "projects" / "_test_pipeline"
    out_dir.mkdir(parents=True, exist_ok=True)

    animations = ["zoom_in", "zoom_out", "pan_left", "pan_right"]
    results = []

    for anim in animations:
        out_path = out_dir / f"test_kb_{anim}.mp4"
        try:
            ken_burns(image_path, out_path, duration=duration, animation=anim)
            actual_dur = get_duration(out_path)
            size_kb = out_path.stat().st_size / 1024
            ok = abs(actual_dur - duration) < 0.2 and size_kb > 50
            status = "OK" if ok else "FAIL"
            print(f"  [{status}] {anim:12s} → {actual_dur:.2f}s  {size_kb:.0f}KB  {out_path.name}")
            results.append(ok)
        except Exception as exc:
            print(f"  [FAIL] {anim:12s} → ERROR: {exc}")
            results.append(False)

    if all(results):
        print(f"\n  All animations OK. Open files to verify smoothness:")
        print(f"  {out_dir}")
    else:
        print(f"\n  Some animations FAILED — check logs above.")


# ── Test 2: Music mixing ───────────────────────────────────────────────────────

def test_music(
    voice_path: Path | None = None,
    music_path: Path | None = None,
) -> None:
    """
    Test background music mixing.

    Checks:
    - Music file is found
    - mix_audio() succeeds
    - Output has correct duration (matches voice)
    """
    print("\n=== TEST: Music Mixing ===")

    # Default music path from channel config
    if music_path is None:
        music_path = ROOT / "assets" / "music" / "background_music.mp3"

    if not music_path.exists():
        print(f"  FAIL: Music file not found: {music_path}")
        print("  Put an .mp3 file in assets/music/")
        return

    size_kb = music_path.stat().st_size / 1024
    dur = get_duration(music_path)
    print(f"  Music: {music_path.name}  ({size_kb:.0f}KB, {dur:.1f}s)")

    # Find voice narration
    if voice_path is None:
        candidates = list(ROOT.glob("projects/**/audio/full_narration*.mp3"))
        if not candidates:
            print("  SKIP: No narration audio found. Run module 03 first.")
            return
        voice_path = candidates[0]

    voice_dur = get_duration(voice_path)
    print(f"  Voice: {voice_path.parent.parent.name}/{voice_path.name}  ({voice_dur:.1f}s)")

    out_dir = ROOT / "projects" / "_test_pipeline"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "test_mixed_audio.mp3"

    try:
        mix_audio(voice_path, music_path, out_path, music_volume=-20.0)
        mixed_dur = get_duration(out_path)
        size_kb = out_path.stat().st_size / 1024
        ok = abs(mixed_dur - voice_dur) < 1.0
        status = "OK" if ok else "WARN"
        print(f"  [{status}] Mixed: {mixed_dur:.1f}s  {size_kb:.0f}KB  → {out_path.name}")
        if not ok:
            print(f"  Expected {voice_dur:.1f}s, got {mixed_dur:.1f}s")
        else:
            print("  Music mixing OK. Open test_mixed_audio.mp3 to verify volume balance.")
    except Exception as exc:
        print(f"  [FAIL] mix_audio ERROR: {exc}")


# ── Test 3: Image frequency tiers ─────────────────────────────────────────────

def test_freq_tiers(script_path: Path | None = None) -> None:
    """
    Show how a script's blocks would be split by the current frequency tiers.

    No FFmpeg calls — pure logic test.
    """
    print("\n=== TEST: Image Frequency Tier Splitting ===")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vc", ROOT / "modules" / "05_video_compiler.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    tiers = m._DEFAULT_FREQ_TIERS
    print("  Current tiers:")
    for t in tiers:
        until = t.get("until_seconds")
        until_str = f"{until/60:.0f} min" if until else "end"
        print(f"    until={until_str:<8}  interval={t['interval']}s")

    if script_path is None:
        candidates = list(ROOT.glob("projects/**/script.json"))
        if not candidates:
            print("  SKIP: No script.json found.")
            return
        # Pick first with audio_duration
        for c in candidates:
            data = json.loads(c.read_text(encoding="utf-8"))
            if any(b.get("audio_duration") for b in data.get("blocks", [])):
                script_path = c
                break

    if script_path is None:
        print("  SKIP: No script with audio_duration found.")
        return

    script = json.loads(script_path.read_text(encoding="utf-8"))
    blocks = [b for b in script["blocks"] if b.get("audio_duration")]
    project_name = script_path.parent.name

    print(f"\n  Script: {project_name}")
    print(f"  Voiced blocks: {len(blocks)}\n")

    total_segs = 0
    elapsed = 0.0
    print(f"  {'Block':<18} {'Start':>7}  {'Dur':>7}  {'Segs':>4}  Breakdown")
    print("  " + "-" * 72)

    for b in blocks:
        dur = float(b["audio_duration"])
        segs = m._split_duration_to_segments(elapsed, dur, tiers)
        total_segs += len(segs)
        breakdown = " + ".join(f"{s:.0f}s" for s in segs[:6])
        if len(segs) > 6:
            breakdown += f" + ... ({len(segs)} total)"
        print(f"  {b['id']:<18} {elapsed:>7.0f}s  {dur:>6.1f}s  {len(segs):>4}  [{breakdown}]")
        elapsed += dur

    total_dur = sum(float(b["audio_duration"]) for b in blocks)
    print(f"\n  Total: {len(blocks)} blocks → {total_segs} segments  ({total_dur:.0f}s = {total_dur/60:.1f} min)")
    crossfade_loss = (len(blocks) - 1) * 0.5
    print(f"  Expected audio trim from crossfade: {crossfade_loss:.1f}s ({len(blocks)-1} block transitions × 0.5s)")


# ── Test 4: Video info ─────────────────────────────────────────────────────────

def test_video_info(video_path: Path) -> None:
    """
    Probe a video file and report streams, duration, and whether it has audio.
    """
    print(f"\n=== TEST: Video Info — {video_path.name} ===")

    if not video_path.exists():
        print(f"  FAIL: File not found: {video_path}")
        return

    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(video_path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        print(f"  FAIL: ffprobe error: {r.stderr[:200]}")
        return

    data = json.loads(r.stdout)
    dur = float(data["format"]["duration"])
    size_mb = int(data["format"]["size"]) / 1e6
    print(f"  Duration: {dur:.1f}s = {dur/60:.1f} min")
    print(f"  Size:     {size_mb:.1f} MB")

    for s in data.get("streams", []):
        codec = s.get("codec_name", "?")
        stype = s.get("codec_type", "?")
        if stype == "video":
            w, h = s.get("width", "?"), s.get("height", "?")
            fps_r = s.get("r_frame_rate", "?")
            print(f"  Video:  {codec}  {w}x{h}  fps={fps_r}")
        elif stype == "audio":
            rate = s.get("sample_rate", "?")
            ch = s.get("channels", "?")
            print(f"  Audio:  {codec}  {rate}Hz  {ch}ch  ← music should be mixed in here")

    # Check for audio track presence
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    if not has_audio:
        print("  [WARN] No audio stream found! Music mixing may have failed.")
    else:
        print("  [OK] Audio stream present. Open video and listen for background music.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="VideoForge — Component Tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tests/test_components.py --all
  python tests/test_components.py --ken-burns --image projects/test/images/block_001.png
  python tests/test_components.py --music
  python tests/test_components.py --freq-tiers --script projects/my_video/script.json
  python tests/test_components.py --video-info projects/my_video/output/final.mp4
        """,
    )
    parser.add_argument("--all", action="store_true", help="Run all tests")
    parser.add_argument("--ken-burns", action="store_true", help="Test Ken Burns animation")
    parser.add_argument("--music", action="store_true", help="Test background music mixing")
    parser.add_argument("--freq-tiers", action="store_true", help="Test image frequency tier splitting")
    parser.add_argument("--video-info", metavar="PATH", help="Probe video file info")
    parser.add_argument("--image", help="Image path for ken-burns test")
    parser.add_argument("--script", help="Script path for freq-tiers test")
    parser.add_argument("--duration", type=float, default=8.0, help="Clip duration for ken-burns test")

    args = parser.parse_args()

    if not any([args.all, args.ken_burns, args.music, args.freq_tiers, args.video_info]):
        parser.print_help()
        return

    img = Path(args.image) if args.image else None
    scr = Path(args.script) if args.script else None

    if args.all or args.ken_burns:
        test_ken_burns(img, args.duration)

    if args.all or args.music:
        test_music()

    if args.all or args.freq_tiers:
        test_freq_tiers(scr)

    if args.video_info:
        test_video_info(Path(args.video_info))

    print("\nDone.")


if __name__ == "__main__":
    main()
