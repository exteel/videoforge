"""
VideoForge — Pipeline Integration Test.

Runs all modules in sequence with fixture data.
- Modules without API calls (subtitle_generator, ffmpeg check): run for real.
- Modules with API calls: run in --dry-run mode.

Usage:
    python tests/test_pipeline.py
    python tests/test_pipeline.py --keep           # don't delete project dir
    python tests/test_pipeline.py --real-subs-only # skip dry-run steps

Exit code: 0 on all pass, 1 on any failure.
"""

import argparse
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES_DIR    = ROOT / "tests" / "test_data"
SCRIPT_FIXTURE  = FIXTURES_DIR / "script_full.json"
CONFIG_FIXTURE  = FIXTURES_DIR / "sample_config.json"
PROJECT_DIR     = ROOT / "projects" / "_test_pipeline"
CHANNEL_CFG     = ROOT / "config" / "channels" / "example_history.json"

# ─── Minimal valid files ───────────────────────────────────────────────────────

def _make_png(width: int = 16, height: int = 16) -> bytes:
    """Generate a minimal valid PNG (solid gray)."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + ctype + data
        return c + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    sig  = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))

    # Raw image data: width * height gray pixels
    raw  = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _make_mp3(duration_hint_bytes: int = 8_000) -> bytes:
    """Return bytes that look like an MP3 (ID3 header + silence filler)."""
    id3 = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    # MP3 frame header: MPEG1, Layer3, 128kbps, 44100Hz, stereo
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    return id3 + frame * max(1, duration_hint_bytes // 417)


# ─── Test infrastructure ───────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []


def _run_test(name: str, fn, *args, **kwargs) -> bool:
    """Execute a test function and record result."""
    t0 = time.monotonic()
    try:
        fn(*args, **kwargs)
        elapsed = time.monotonic() - t0
        _results.append((name, True, f"{elapsed:.1f}s"))
        print(f"  \033[92m✓\033[0m  {name}  ({elapsed:.1f}s)")
        return True
    except Exception as exc:
        elapsed = time.monotonic() - t0
        _results.append((name, False, str(exc)))
        print(f"  \033[91m✗\033[0m  {name}: {exc}")
        return False


def _cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command, capturing combined output."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}):\n"
            f"  {' '.join(args)}\n"
            f"  stdout: {result.stdout[-500:]}\n"
            f"  stderr: {result.stderr[-500:]}"
        )
    return result


def _import_module(rel_path: str) -> object:
    """Import a module by relative path for symbol verification."""
    full = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(full.stem, full)
    mod  = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─── Setup ────────────────────────────────────────────────────────────────────

def setup_project() -> None:
    """Create test project directory with fixture data and fake intermediate files."""
    if PROJECT_DIR.exists():
        shutil.rmtree(PROJECT_DIR)

    for sub in ("images", "audio", "subtitles", "output"):
        (PROJECT_DIR / sub).mkdir(parents=True)

    # Copy script fixture
    script = json.loads(SCRIPT_FIXTURE.read_text(encoding="utf-8"))
    (PROJECT_DIR / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # Create fake block images (valid PNG, >5KB each)
    png_data = _make_png(64, 64)
    # Pad to 10KB to pass MIN_FILE_BYTES checks
    png_padded = png_data + b"\x00" * max(0, 10_000 - len(png_data))
    for block in script["blocks"]:
        if block.get("image_prompt", "").strip():
            (PROJECT_DIR / "images" / f"{block['id']}.png").write_bytes(png_padded)

    # Create fake per-block audio files (>1KB each)
    mp3_block = _make_mp3(5_000)
    for block in script["blocks"]:
        if block.get("narration", "").strip():
            (PROJECT_DIR / "audio" / f"{block['id']}.mp3").write_bytes(mp3_block)

    # Create fake full narration (>5MB — simulates real audio)
    mp3_full = _make_mp3(6_000_000)
    (PROJECT_DIR / "audio" / "full_narration_normalized.mp3").write_bytes(mp3_full)

    # Create fake thumbnail (>10KB, valid PNG)
    thumb_png = _make_png(128, 72) + b"\x00" * 15_000
    (PROJECT_DIR / "output" / "thumbnail.png").write_bytes(thumb_png)

    # Create fake final video (>1MB, mp4 header)
    mp4_header = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41"
    (PROJECT_DIR / "output" / "final.mp4").write_bytes(mp4_header + b"\x00" * 2_000_000)

    # Write metadata.json (as would be output by metadata_generator)
    metadata = {
        "title": script["title"],
        "description": "Test description for pipeline validation.",
        "tags": script.get("tags", []),
        "category_id": "27",
        "language": "en",
        "timestamps": [
            {"time": "0:00", "label": "Introduction"},
            {"time": "0:09", "label": "Three Centuries of Decline"},
        ],
        "total_duration_seconds": 196.0,
        "script_title": script["title"],
        "generated_by": "VideoForge",
    }
    (PROJECT_DIR / "output" / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    print(f"  Project: {PROJECT_DIR}")
    print(f"  Images:  {len(list((PROJECT_DIR/'images').glob('*.png')))} PNG files")
    print(f"  Audio:   {len(list((PROJECT_DIR/'audio').glob('*.mp3')))} MP3 files")


# ─── Module import checks ─────────────────────────────────────────────────────

def test_imports() -> None:
    """Verify all 8 modules import correctly and expose their main function."""
    expected = {
        "modules/01_script_generator.py":    "generate_scripts",
        "modules/02_image_generator.py":     "generate_images",
        "modules/03_voice_generator.py":     "generate_voices",
        "modules/04_subtitle_generator.py":  "generate_subtitles",
        "modules/05_video_compiler.py":      "compile_video",
        "modules/06_thumbnail_generator.py": "generate_thumbnail",
        "modules/07_metadata_generator.py":  "generate_metadata",
        "modules/08_youtube_uploader.py":    "upload_video",
    }
    for rel_path, symbol in expected.items():
        full = ROOT / rel_path
        if not full.exists():
            raise FileNotFoundError(f"Module not found: {rel_path}")
        mod = _import_module(rel_path)
        if not callable(getattr(mod, symbol, None)):
            raise AttributeError(f"{rel_path}: '{symbol}' not found or not callable")


def test_help_output() -> None:
    """Verify --help works for all CLI modules."""
    modules = [
        "modules/01_script_generator.py",
        "modules/02_image_generator.py",
        "modules/03_voice_generator.py",
        "modules/04_subtitle_generator.py",
        "modules/05_video_compiler.py",
        "modules/06_thumbnail_generator.py",
        "modules/07_metadata_generator.py",
        "modules/08_youtube_uploader.py",
    ]
    for mod in modules:
        r = _cmd([sys.executable, str(ROOT / mod), "--help"], check=False)
        if r.returncode not in (0, 1):   # argparse exits with 0 on --help
            raise RuntimeError(f"{mod} --help returned exit {r.returncode}")
        if "usage:" not in r.stdout.lower() and "usage:" not in r.stderr.lower():
            raise RuntimeError(f"{mod} --help output lacks 'usage:' keyword")


# ─── Subtitle generator (real run, no API) ────────────────────────────────────

def test_subtitle_generator_real() -> None:
    """Run subtitle_generator for real — pure Python, no API needed."""
    r = _cmd([
        sys.executable, str(ROOT / "modules" / "04_subtitle_generator.py"),
        "--script",  str(PROJECT_DIR / "script.json"),
        "--channel", str(CHANNEL_CFG),
        "--output",  str(PROJECT_DIR / "subtitles"),
    ])

    srt = PROJECT_DIR / "subtitles" / "subtitles.srt"
    ass = PROJECT_DIR / "subtitles" / "subtitles.ass"
    if not srt.exists():
        raise FileNotFoundError(f"subtitles.srt not created: {r.stdout}")
    if not ass.exists():
        raise FileNotFoundError(f"subtitles.ass not created: {r.stdout}")

    # Verify SRT structure
    content = srt.read_text(encoding="utf-8")
    if "-->" not in content:
        raise ValueError("SRT file missing timestamps")

    # Count subtitle entries
    entries = [l for l in content.splitlines() if l.strip().isdigit()]
    if len(entries) < 3:
        raise ValueError(f"SRT has too few entries: {len(entries)}")


def test_subtitle_content() -> None:
    """Verify subtitle timestamps are monotonically increasing."""
    srt = PROJECT_DIR / "subtitles" / "subtitles.srt"
    if not srt.exists():
        raise FileNotFoundError("subtitles.srt not found — run test_subtitle_generator_real first")

    content = srt.read_text(encoding="utf-8")
    times: list[float] = []
    for line in content.splitlines():
        if "-->" in line:
            start_str = line.split("-->")[0].strip()
            # Parse HH:MM:SS,mmm
            parts = start_str.replace(",", ".").split(":")
            if len(parts) == 3:
                h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
                times.append(h * 3600 + m * 60 + s)

    if not times:
        raise ValueError("No timestamps found in SRT")
    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            raise ValueError(
                f"Timestamps not monotonic at entry {i+1}: "
                f"{times[i-1]:.3f} → {times[i]:.3f}"
            )


# ─── Dry-run tests ────────────────────────────────────────────────────────────

def _dry_run(module: str, extra_args: list[str] | None = None) -> None:
    """Run a module in --dry-run mode and verify zero exit code."""
    args = [
        sys.executable, str(ROOT / "modules" / module),
        "--script",  str(PROJECT_DIR / "script.json"),
        "--channel", str(CHANNEL_CFG),
        "--dry-run",
    ]
    if extra_args:
        args += extra_args
    _cmd(args)


def test_script_gen_dryrun() -> None:
    """Script Generator dry-run."""
    r = _cmd([
        sys.executable, str(ROOT / "modules" / "01_script_generator.py"),
        "--source",  str(ROOT / "tests" / "test_data" / "sample_transcriber_output"),
        "--channel", str(CHANNEL_CFG),
        "--output",  str(PROJECT_DIR / "script_gen_test.json"),
        "--dry-run",
    ])


def test_image_gen_dryrun() -> None:
    _dry_run("02_image_generator.py", ["--output", str(PROJECT_DIR / "images")])


def test_voice_gen_dryrun() -> None:
    _dry_run("03_voice_generator.py", ["--output", str(PROJECT_DIR / "audio")])


def test_video_compiler_dryrun() -> None:
    _dry_run("05_video_compiler.py",
             ["--no-music", "--no-intro-outro"])


def test_thumbnail_gen_dryrun() -> None:
    _dry_run("06_thumbnail_generator.py")


def test_metadata_gen_dryrun() -> None:
    _dry_run("07_metadata_generator.py")


def test_uploader_dryrun() -> None:
    _dry_run("08_youtube_uploader.py")


def test_uploader_schedule_dryrun() -> None:
    """Test uploader --schedule and --auto-schedule flags."""
    _dry_run("08_youtube_uploader.py", ["--schedule", "2026-12-31 18:00"])
    _dry_run("08_youtube_uploader.py", ["--auto-schedule"])


# ─── FFmpeg check ─────────────────────────────────────────────────────────────

def test_ffmpeg_available() -> None:
    """Verify FFmpeg and FFprobe are available on PATH."""
    from utils.ffmpeg_utils import check_ffmpeg
    ffmpeg_ver, ffprobe_ver = check_ffmpeg()
    if not ffmpeg_ver:
        raise RuntimeError("FFmpeg not found")
    if not ffprobe_ver:
        raise RuntimeError("FFprobe not found")


# ─── Timestamp logic unit test ────────────────────────────────────────────────

def test_timestamp_computation() -> None:
    """Unit test for metadata_generator timestamp logic."""
    mod = _import_module("modules/07_metadata_generator.py")

    blocks = [
        {"narration": "Intro", "timestamp_label": "Introduction", "audio_duration": 9.5},
        {"narration": "Content", "timestamp_label": "The Decline", "audio_duration": 28.5},
        {"narration": "",  "timestamp_label": "CTA", "audio_duration": 10.0},
        {"narration": "More", "timestamp_label": "Conclusion", "audio_duration": 18.0},
    ]
    ts = mod._build_timestamps(blocks)

    # Should have 3 entries (CTA with empty narration is skipped)
    assert len(ts) == 3, f"Expected 3 timestamps, got {len(ts)}: {ts}"
    assert ts[0] == {"time": "0:00", "label": "Introduction"}, ts[0]
    assert ts[1] == {"time": "0:09", "label": "The Decline"}, ts[1]
    assert ts[2] == {"time": "0:48", "label": "Conclusion"}, ts[2]


# ─── Script fixture integrity ─────────────────────────────────────────────────

def test_fixture_integrity() -> None:
    """Verify script_full.json has all required fields."""
    script = json.loads(SCRIPT_FIXTURE.read_text(encoding="utf-8"))

    required_top = ("title", "language", "niche", "blocks")
    for key in required_top:
        if key not in script:
            raise KeyError(f"script_full.json missing top-level key: '{key}'")

    voiced = [b for b in script["blocks"] if b.get("narration", "").strip()]
    if len(voiced) < 3:
        raise ValueError(f"Too few voiced blocks: {len(voiced)}")

    missing_duration = [b["id"] for b in voiced if not b.get("audio_duration")]
    if missing_duration:
        raise ValueError(f"Blocks missing audio_duration: {missing_duration}")

    total = sum(b.get("audio_duration", 0) for b in script["blocks"])
    if total < 60:
        raise ValueError(f"Total duration too short: {total:.1f}s (expected >60s)")

    print(f"    Blocks: {len(script['blocks'])} | Voiced: {len(voiced)} | Total: {total:.1f}s")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(keep: bool = False) -> int:
    print("\n\033[1m\033[96m" + "-" * 56 + "\033[0m")
    print("\033[1m  VideoForge — Pipeline Integration Test\033[0m")
    print("\033[1m\033[96m" + "-" * 56 + "\033[0m\n")

    # ── Setup ──
    print("\033[1m[Setup]\033[0m Creating test project...")
    try:
        setup_project()
    except Exception as exc:
        print(f"  \033[91m✗  Setup failed: {exc}\033[0m")
        return 1

    print()

    # ── Tests ──
    groups: list[tuple[str, list[tuple[str, object]]]] = [
        ("Fixtures", [
            ("fixture integrity", test_fixture_integrity),
        ]),
        ("Module Imports", [
            ("all 8 modules import OK", test_imports),
            ("all --help outputs OK",   test_help_output),
        ]),
        ("System", [
            ("FFmpeg available",         test_ffmpeg_available),
        ]),
        ("Unit Tests", [
            ("timestamp computation",    test_timestamp_computation),
        ]),
        ("Subtitle Generator (real run)", [
            ("generate SRT + ASS",       test_subtitle_generator_real),
            ("timestamps monotonic",     test_subtitle_content),
        ]),
        ("Dry-run pipeline", [
            ("01 Script Generator",      test_script_gen_dryrun),
            ("02 Image Generator",       test_image_gen_dryrun),
            ("03 Voice Generator",       test_voice_gen_dryrun),
            ("05 Video Compiler",        test_video_compiler_dryrun),
            ("06 Thumbnail Generator",   test_thumbnail_gen_dryrun),
            ("07 Metadata Generator",    test_metadata_gen_dryrun),
            ("08 Uploader (public)",     test_uploader_dryrun),
            ("08 Uploader (scheduled)",  test_uploader_schedule_dryrun),
        ]),
    ]

    for group_name, tests in groups:
        print(f"\033[1m[{group_name}]\033[0m")
        for test_name, fn in tests:
            _run_test(test_name, fn)
        print()

    # ── Summary ──
    total   = len(_results)
    passed  = sum(1 for _, ok, _ in _results if ok)
    failed  = total - passed

    print("\033[1m\033[96m" + "-" * 56 + "\033[0m")
    print(f"\033[1m  Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  \033[91m({failed} FAILED)\033[0m")
    else:
        print(f"  \033[92m✓ ALL PASSED\033[0m")
    print("\033[1m\033[96m" + "-" * 56 + "\033[0m")

    if failed:
        print("\nFailed tests:")
        for name, ok, msg in _results:
            if not ok:
                print(f"  ✗ {name}: {msg}")

    # ── Cleanup ──
    if not keep and PROJECT_DIR.exists():
        shutil.rmtree(PROJECT_DIR)
        print(f"\nCleaned up: {PROJECT_DIR}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VideoForge pipeline integration test")
    parser.add_argument("--keep", action="store_true", help="Keep test project directory after run")
    args = parser.parse_args()
    sys.exit(main(keep=args.keep))
