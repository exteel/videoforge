"""
VideoForge — Module 03: Voice Generator.

script.json → VoiceAPI TTS → audio/block_NNN.mp3 → concat → EBU R128 normalize.

Features:
- Parallel generation (VoiceAPIClient internal semaphore = 3 concurrent)
- ffprobe duration measurement → writes audio_duration back to script.json
- Concat all blocks → full_narration.mp3
- EBU R128 loudnorm → full_narration_normalized.mp3
- Fallback: VoidAI TTS (tts-1-hd) — handled inside VoiceAPIClient automatically
- Multi-lang: --lang de → voice_ids_multilang, saves to audio/de/
- Step caching: skip if .mp3 exists and > 1 KB
- tqdm progress bar

CLI:
    python modules/03_voice_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json

    # German dub:
    python modules/03_voice_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json --lang de

    # Skip existing (step caching, default on):
    python modules/03_voice_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json --no-normalize
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_channel_config, load_env, setup_logging
from modules.script_validator import sanitize_narration_for_tts
from utils.ffmpeg_utils import concat_audio, get_duration, normalize_audio

log = setup_logging("voice_gen")

# ─── Constants ────────────────────────────────────────────────────────────────

MIN_FILE_BYTES = 1_000   # Anything smaller is considered corrupt/empty


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class AudioResult:
    block_id: str
    order: int
    path: str | None        # Local .mp3 path, None if failed
    duration: float | None  # Seconds from ffprobe, None if failed/skipped
    chars: int              # Narration character count
    skipped: bool = False
    error: str | None = None


@dataclass
class VoiceSummary:
    total: int              # Blocks with narration
    generated: int          # New files generated
    skipped: int            # Cache hits
    failed: int             # Failed all attempts
    fallback_count: int     # VoidAI TTS fallback uses (session total)
    total_chars: int        # Total characters voiced
    total_duration: float   # Total narration seconds
    concat_path: str | None
    normalized_path: str | None
    elapsed: float
    results: list[AudioResult] = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_voice_id(channel_config: dict[str, Any], lang: str) -> str:
    """Return voice_id for the given language from channel config."""
    primary = channel_config.get("language", "en")

    if lang == primary:
        vid = channel_config.get("voice_id", "")
        if not vid:
            raise ValueError(
                "No 'voice_id' in channel config. "
                "Set it to your ElevenLabs voice ID."
            )
        return vid

    multi = channel_config.get("voice_ids_multilang", {})
    vid = multi.get(lang, "")
    if not vid:
        raise ValueError(
            f"No voice_id for lang='{lang}' in 'voice_ids_multilang'. "
            f"Add it to your channel config."
        )
    return vid


def _audio_dir(script_dir: Path, lang: str, primary_lang: str) -> Path:
    """Return audio output directory for the given language."""
    base = script_dir / "audio"
    return base if lang == primary_lang else base / lang


# ─── Per-block generation ─────────────────────────────────────────────────────

async def _generate_block_audio(
    block: dict[str, Any],
    audio_dir: Path,
    voice_id: str,
    language: str,
    voiceapi: Any,
    *,
    skip_existing: bool,
) -> AudioResult:
    """
    Generate TTS audio for a single script block.

    Returns AudioResult with path and duration (from ffprobe), or error.
    """
    block_id  = block["id"]
    order     = block["order"]
    narration = sanitize_narration_for_tts(block.get("narration", ""))

    if not narration:
        log.debug("Block %s: empty narration — skip", block_id)
        return AudioResult(
            block_id=block_id, order=order,
            path=None, duration=None, chars=0, skipped=True,
        )

    out_path = audio_dir / f"{block_id}.mp3"

    # Cache hit
    if skip_existing and out_path.exists() and out_path.stat().st_size >= MIN_FILE_BYTES:
        try:
            dur = get_duration(out_path)
        except Exception:
            dur = None
        log.info("Block %s: cached (%.1fs)", block_id, dur or 0)
        return AudioResult(
            block_id=block_id, order=order,
            path=str(out_path), duration=dur,
            chars=len(narration), skipped=True,
        )

    result = AudioResult(
        block_id=block_id, order=order,
        path=None, duration=None, chars=len(narration),
    )

    try:
        audio_dir.mkdir(parents=True, exist_ok=True)
        await voiceapi.generate(
            narration,
            voice_id=voice_id,
            language=language,
            output_path=out_path,
        )

        if not out_path.exists() or out_path.stat().st_size < MIN_FILE_BYTES:
            raise RuntimeError("Output file missing or too small")

        try:
            dur = get_duration(out_path)
        except Exception as exc:
            log.warning("Block %s: ffprobe failed (%s) — duration unknown", block_id, exc)
            dur = None

        result.path = str(out_path)
        result.duration = dur
        log.info(
            "Block %s: %d chars → %.1fs audio",
            block_id, len(narration), dur or 0,
        )

    except Exception as exc:
        result.error = str(exc)
        log.error("Block %s: FAILED — %s", block_id, exc)

    return result


# ─── Script.json update ───────────────────────────────────────────────────────

def _update_script_durations(
    script: dict[str, Any],
    results: list[AudioResult],
    script_path: Path,
) -> None:
    """Write audio_duration back into script.json blocks in-place."""
    dur_map = {r.block_id: r.duration for r in results if r.duration is not None}
    if not dur_map:
        return

    updated = 0
    for block in script.get("blocks", []):
        bid = block.get("id", "")
        if bid in dur_map:
            block["audio_duration"] = round(dur_map[bid], 3)
            updated += 1

    script_path.write_text(
        json.dumps(script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("script.json updated: %d blocks got audio_duration", updated)


# ─── Main API ─────────────────────────────────────────────────────────────────

async def generate_voices(
    script_path: str | Path,
    channel_config_path: str | Path,
    *,
    lang: str | None = None,
    voice_id_override: str | None = None,
    output_dir: str | Path | None = None,
    skip_existing: bool = True,
    dry_run: bool = False,
    no_normalize: bool = False,
    progress_callback: Any | None = None,
) -> VoiceSummary:
    """
    Generate TTS audio for all narration blocks in script.json.

    Args:
        script_path: Path to script.json.
        channel_config_path: Path to channel config JSON.
        lang: Language code override (e.g. "de"). Default: channel config language.
        voice_id_override: Override voice ID (bypasses channel config lookup).
        output_dir: Base directory for audio output. Default: script_path.parent.
        skip_existing: Skip blocks where .mp3 already exists (step caching).
        dry_run: Log plan without making API calls.
        no_normalize: Skip EBU R128 normalization step.

    Returns:
        VoiceSummary with per-block results, total duration, and file paths.
    """
    load_env()

    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found: {script_path}")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)

    primary_lang = channel_config.get("language", "en")
    language     = lang or primary_lang
    voice_id     = voice_id_override or _get_voice_id(channel_config, language)

    base_dir  = Path(output_dir) if output_dir else script_path.parent
    audio_dir = _audio_dir(base_dir, language, primary_lang)

    blocks: list[dict[str, Any]] = script.get("blocks", [])
    voiced_blocks = [b for b in blocks if b.get("narration", "").strip()]
    n_empty = len(blocks) - len(voiced_blocks)

    log.info(
        "Script: %d blocks total | %d with narration | %d skipped (empty)",
        len(blocks), len(voiced_blocks), n_empty,
    )
    log.info(
        "Lang: %s | Voice ID: %s... | Output: %s",
        language, voice_id[:8], audio_dir,
    )

    if dry_run:
        n_existing = sum(
            1 for b in voiced_blocks
            if (audio_dir / f"{b['id']}.mp3").exists()
            and (audio_dir / f"{b['id']}.mp3").stat().st_size >= MIN_FILE_BYTES
        )
        total_chars = sum(len(b.get("narration", "")) for b in voiced_blocks)
        to_gen = len(voiced_blocks) - n_existing
        log.info(
            "[DRY RUN] Would voice %d blocks (%d chars), skip %d existing",
            to_gen, total_chars, n_existing,
        )
        return VoiceSummary(
            total=len(voiced_blocks),
            generated=0, skipped=0, failed=0, fallback_count=0,
            total_chars=total_chars, total_duration=0.0,
            concat_path=None, normalized_path=None, elapsed=0.0,
        )

    t0 = time.monotonic()

    from clients.voiceapi_client import VoiceAPIClient  # noqa: PLC0415

    # ── Progress helper (reports 0-100% of voice generation) ──
    n_voice_total = len(voiced_blocks)
    _voice_done = [0]

    def _emit_voice_progress(done: int) -> None:
        if progress_callback and n_voice_total > 0:
            try:
                pct = round(done / n_voice_total * 100.0, 1)
                progress_callback({
                    "type": "sub_progress",
                    "pct": pct,
                    "message": f"Voice {done}/{n_voice_total}",
                })
            except Exception:
                pass

    results: list[AudioResult] = []

    async with VoiceAPIClient() as voiceapi:
        coros = [
            _generate_block_audio(
                block=b,
                audio_dir=audio_dir,
                voice_id=voice_id,
                language=language,
                voiceapi=voiceapi,
                skip_existing=skip_existing,
            )
            for b in voiced_blocks
        ]

        # Progress bar via tqdm if available
        try:
            from tqdm.asyncio import tqdm as atqdm  # noqa: PLC0415
            tasks = [asyncio.ensure_future(c) for c in coros]
            for fut in atqdm(
                asyncio.as_completed(tasks),
                total=len(tasks),
                desc="Generating audio",
                unit="block",
            ):
                try:
                    results.append(await fut)
                except Exception as _task_exc:
                    log.error("Audio generation task raised unexpectedly: %s", _task_exc)
                _voice_done[0] += 1
                _emit_voice_progress(_voice_done[0])

        except ImportError:
            log.info("tqdm not available — running without progress bar")
            raw = await asyncio.gather(*coros, return_exceptions=True)
            for i, item in enumerate(raw):
                if isinstance(item, Exception):
                    log.error("Unexpected gather exception: %s", item)
                else:
                    results.append(item)
                _emit_voice_progress(i + 1)

        fallback_count = voiceapi.fallback_count

    # Sort by block order for concat
    results.sort(key=lambda r: r.order)

    # Tally
    generated = sum(1 for r in results if r.path and not r.skipped)
    skipped   = sum(1 for r in results if r.skipped and r.path)
    failed    = sum(1 for r in results if not r.path and not r.skipped)
    total_chars = sum(r.chars for r in results)
    total_duration = sum(r.duration for r in results if r.duration)

    log.info(
        "Audio done: %d generated | %d skipped | %d failed | %d fallback | "
        "total=%.1fs (%.1fmin)",
        generated, skipped, failed, fallback_count,
        total_duration, total_duration / 60,
    )

    if failed:
        log.warning("Failed blocks: %s", [r.block_id for r in results if not r.path and not r.skipped])

    # ── Write audio_duration back to script.json ──
    _update_script_durations(script, results, script_path)

    # ── Concat all audio files ──
    audio_paths = [Path(r.path) for r in results if r.path]
    concat_path: str | None = None
    normalized_path: str | None = None

    if not audio_paths:
        log.warning("No audio files to concat — skipping concat/normalize")
    else:
        concat_file = audio_dir / "full_narration.mp3"
        log.info("Concatenating %d audio files → %s", len(audio_paths), concat_file.name)
        try:
            concat_audio(audio_paths, concat_file)
            concat_path = str(concat_file)
            log.info("Concat done: %s (%.1f MB)", concat_file.name, concat_file.stat().st_size / 1e6)
        except Exception as exc:
            log.error("Concat failed: %s", exc)

        # ── EBU R128 normalization ──
        if concat_path and not no_normalize:
            norm_file = audio_dir / "full_narration_normalized.mp3"
            log.info("Normalizing audio (EBU R128 loudnorm)...")
            try:
                normalize_audio(concat_file, norm_file)
                normalized_path = str(norm_file)
                log.info("Normalized: %s", norm_file.name)
            except Exception as exc:
                log.error("Normalization failed: %s — using unnormalized audio", exc)

    elapsed = time.monotonic() - t0
    log.info("Voice generation complete in %.1fs", elapsed)

    return VoiceSummary(
        total=len(voiced_blocks),
        generated=generated,
        skipped=skipped,
        failed=failed,
        fallback_count=fallback_count,
        total_chars=total_chars,
        total_duration=total_duration,
        concat_path=concat_path,
        normalized_path=normalized_path,
        elapsed=elapsed,
        results=results,
    )


# ─── CLI ──────────────────────────────────────────────────────────────────────

async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — Voice Generator (Module 03)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python modules/03_voice_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json

  # German dub:
  python modules/03_voice_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json --lang de

  # Dry run:
  python modules/03_voice_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json --dry-run

  # Regenerate all (ignore cache):
  python modules/03_voice_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json --no-skip
        """,
    )

    parser.add_argument(
        "--script",
        required=True,
        help="Path to script.json (output of module 01)",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel config JSON path",
    )
    parser.add_argument(
        "--lang",
        default=None,
        help="Language code for multi-lang dub (e.g. 'de', 'es'). Default: channel primary language",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Base output directory (default: same as --script directory)",
    )
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Regenerate all audio even if files already exist",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="Skip EBU R128 loudnorm after concat",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show plan and estimate without making API calls",
    )

    args = parser.parse_args()

    summary = await generate_voices(
        script_path=args.script,
        channel_config_path=args.channel,
        lang=args.lang,
        output_dir=args.output,
        skip_existing=not args.no_skip,
        dry_run=args.dry_run,
        no_normalize=args.no_normalize,
    )

    if args.dry_run:
        return

    if summary.failed > 0:
        log.warning("%d block(s) failed TTS — continuing with partial audio", summary.failed)

    log.info(
        "Voice summary: %d/%d blocks voiced | %.1fmin narration | "
        "concat=%s | normalized=%s",
        summary.generated + summary.skipped,
        summary.total,
        summary.total_duration / 60,
        "OK" if summary.concat_path else "FAILED",
        "OK" if summary.normalized_path else ("SKIP" if args.no_normalize else "FAILED"),
    )

    if summary.failed == summary.total:
        log.error("All TTS failed!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
