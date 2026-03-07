"""
VideoForge — Module 06: Thumbnail Generator.

Generates a YouTube thumbnail (1280×720) from script.json thumbnail prompt.

Prompt sources (priority order):
  1. --transcriber-dir → thumbnail_prompt.txt from Transcriber output
  2. script.json → thumbnail_prompt field
  3. --prompt CLI override

Generation: WaveSpeed z-image/turbo (size 1280*720).
Validation: VoidAI vision (gpt-4.1) — 6 quality criteria, pass ≥ 5/6.
Iterative: up to --max-attempts (default 5), seed=42 on attempt 1, random after.

--draft: skip validation, single attempt, fixed seed.

CLI:
    python modules/06_thumbnail_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json

    python modules/06_thumbnail_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        --transcriber-dir "D:/transscript batch/output/output/My Video Title"
"""

import asyncio
import json
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from clients.voidai_client import VoidAIClient
from clients.voiceimage_client import VoiceImageClient
from clients.wavespeed_client import WaveSpeedClient
from modules.common import (
    get_llm_preset,
    load_channel_config,
    load_env,
    load_transcriber_output,
    setup_logging,
)

log = setup_logging("thumbnail_gen")

# ─── Constants ────────────────────────────────────────────────────────────────

THUMBNAIL_SIZE_WS  = "1280*720"    # WaveSpeed format
THUMBNAIL_SIZE_VAI = "1280x720"    # VoidAI format
THUMBNAIL_FILENAME = "thumbnail.png"
FIXED_SEED         = 42
MAX_ATTEMPTS       = 5
PASS_THRESHOLD     = 5             # out of 6 criteria
MIN_FILE_BYTES     = 10_000
VISION_MODEL       = "gpt-4.1"    # Default vision model (overridden by channel preset)
FALLBACK_IMG_MODEL = "gpt-image-1.5"

# Validation criteria names (used in JSON response parsing)
CRITERIA = (
    "composition",   # Good framing, balanced layout
    "focal_point",   # Clear subject, not cluttered
    "colors",        # High contrast, vibrant, eye-catching
    "quality",       # Sharp, professional-looking
    "topic_match",   # Represents the video topic
    "professional",  # Looks like a real YouTube thumbnail
)

_VISION_SYSTEM = (
    "You are a YouTube thumbnail quality evaluator. "
    "Analyze thumbnails objectively. "
    "Respond ONLY with a valid JSON object — no markdown, no explanation."
)

_VISION_TEMPLATE = """\
Evaluate this YouTube thumbnail for a {niche} channel.
Video topic prompt: "{prompt}"

Rate each criterion YES or NO:
1. composition — good framing, balanced layout, rule of thirds
2. focal_point — clear subject/focal point, not cluttered
3. colors — high contrast, vibrant, eye-catching colors
4. quality — sharp image, not blurry, looks professional
5. topic_match — visually represents the video topic
6. professional — looks like a real YouTube thumbnail

Reply with JSON only:
{{"results": {{"composition": "YES", "focal_point": "YES", "colors": "YES", "quality": "YES", "topic_match": "YES", "professional": "YES"}}, "score": 6, "issues": []}}"""


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    passed:   bool
    score:    int
    criteria: dict[str, bool]
    issues:   list[str]
    raw:      str = ""


@dataclass
class ThumbnailResult:
    output_path:        Path
    prompt_used:        str
    attempts:           int
    passed_validation:  bool
    score:              int          # -1 if validation was skipped
    issues:             list[str] = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_prompt(
    script:            dict[str, Any],
    channel_config:    dict[str, Any],
    transcriber_data:  dict[str, Any] | None,
    prompt_override:   str | None,
    text_overlay:      str | None,
) -> str:
    """Build the final image generation prompt for the thumbnail."""
    # Priority: CLI override > transcriber prompt > script.json field
    if prompt_override:
        base = prompt_override.strip()
    elif transcriber_data and transcriber_data.get("thumbnail_prompt"):
        base = transcriber_data["thumbnail_prompt"].strip()
        log.info("Using Transcriber thumbnail prompt (%d chars)", len(base))
    else:
        base = script.get("thumbnail_prompt", "").strip()

    if not base:
        title = script.get("title", "Untitled")
        niche = channel_config.get("niche", "educational")
        base = f"YouTube thumbnail for {niche} channel video: {title}"
        log.warning("No thumbnail_prompt found — using title fallback: %s", base[:80])

    # Append channel thumbnail style
    style = channel_config.get("thumbnail_style", "").strip()
    if style:
        base = f"{base}, {style}"

    # Append text overlay hint
    if text_overlay:
        base = f"{base}, with bold text overlay: '{text_overlay.strip()}'"

    # Ensure "YouTube thumbnail" appears in prompt for style guidance
    if "youtube thumbnail" not in base.lower():
        base = f"YouTube thumbnail, {base}"

    return base


async def _validate_thumbnail(
    image_path:   Path,
    prompt:       str,
    niche:        str,
    voidai:       VoidAIClient,
    vision_model: str,
) -> ValidationResult:
    """
    Validate thumbnail quality using VoidAI vision.

    Returns ValidationResult with passed=True if score >= PASS_THRESHOLD.
    On parse failure, returns a passing result to avoid blocking the pipeline.
    """
    user_text = _VISION_TEMPLATE.format(niche=niche, prompt=prompt[:300])
    msg = VoidAIClient.image_message(image_path, user_text)

    try:
        raw = await voidai.vision_completion(
            [{"role": "system", "content": _VISION_SYSTEM}, msg],
            model=vision_model,
            max_tokens=300,
            temperature=0.1,
        )
    except Exception as exc:
        log.warning("Vision validation API error: %s — treating as passed", exc)
        return ValidationResult(passed=True, score=6, criteria={}, issues=[], raw=str(exc))

    # Strip markdown fences if model wraps JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned

    try:
        data = json.loads(cleaned)
        results_raw  = data.get("results", {})
        criteria_map = {k: str(v).upper() == "YES" for k, v in results_raw.items()}
        score        = int(data.get("score", sum(criteria_map.values())))
        issues       = data.get("issues", [])
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("Could not parse vision JSON: %s | raw=%s", exc, raw[:200])
        return ValidationResult(passed=True, score=6, criteria={}, issues=["parse_error"], raw=raw)

    passed = score >= PASS_THRESHOLD
    log.info(
        "Validation: score=%d/6 passed=%s | issues: %s",
        score, passed, issues or "none",
    )
    return ValidationResult(
        passed=passed, score=score, criteria=criteria_map, issues=issues, raw=raw,
    )


# ─── Main function ────────────────────────────────────────────────────────────

async def generate_thumbnail(
    script_path:       str | Path,
    channel_config_path: str | Path,
    *,
    transcriber_dir:   str | Path | None = None,
    prompt_override:   str | None = None,
    text_overlay:      str | None = None,
    output_path:       str | Path | None = None,
    max_attempts:      int = MAX_ATTEMPTS,
    iterate:           bool = True,
    preset:            str | None = None,
    dry_run:           bool = False,
) -> ThumbnailResult:
    """
    Generate a YouTube thumbnail (1280×720) from script.json.

    Args:
        script_path: Path to script.json.
        channel_config_path: Path to channel config JSON.
        transcriber_dir: Transcriber output dir (for thumbnail_prompt.txt).
        prompt_override: Override the thumbnail prompt entirely.
        text_overlay: Text overlay hint to append to the prompt.
        output_path: Where to save thumbnail.png. Default: script.parent/output/thumbnail.png.
        max_attempts: Max generation + validation attempts (default 5).
        iterate: Enable VoidAI vision quality validation with retry.
        preset: LLM preset name (max/high/balanced/bulk/test).
        dry_run: Show plan without generating.

    Returns:
        ThumbnailResult with output_path, score, etc.

    Raises:
        FileNotFoundError: If script.json not found.
        RuntimeError: If all generation attempts fail.
    """
    load_env()

    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found: {script_path}")

    script         = json.loads(script_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)
    base_dir       = script_path.parent
    out_dir        = base_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(output_path) if output_path else out_dir / THUMBNAIL_FILENAME

    # Load Transcriber output if provided
    transcriber_data: dict[str, Any] | None = None
    if transcriber_dir:
        try:
            transcriber_data = load_transcriber_output(transcriber_dir)
        except (FileNotFoundError, NotADirectoryError) as exc:
            log.warning("Transcriber dir not usable: %s", exc)

    # Build generation prompt
    prompt = _build_prompt(script, channel_config, transcriber_data, prompt_override, text_overlay)
    log.info("Prompt: %s", prompt[:140])

    # Resolve vision model from channel LLM preset
    llm_preset   = get_llm_preset(channel_config, preset)
    vision_model = llm_preset.get("thumbnail", VISION_MODEL)
    niche        = channel_config.get("niche", "educational")

    if dry_run:
        log.info("[DRY RUN] Output: %s", out_path)
        log.info("[DRY RUN] Prompt: %s", prompt[:140])
        log.info(
            "[DRY RUN] Vision model: %s | Iterate: %s | Max attempts: %d",
            vision_model, iterate, max_attempts,
        )
        return ThumbnailResult(
            output_path=out_path,
            prompt_used=prompt,
            attempts=0,
            passed_validation=True,
            score=-1,
        )

    t0 = time.monotonic()

    # Temp dir for per-attempt files
    tmp_dir = out_dir / ".thumb_tmp"
    tmp_dir.mkdir(exist_ok=True)

    best_path:   Path | None = None
    best_score:  int         = -1
    best_result: ValidationResult | None = None
    attempts_made = 0

    try:
        async with WaveSpeedClient() as wave, VoidAIClient() as void:
            for attempt in range(1, max_attempts + 1):
                attempts_made = attempt
                seed = FIXED_SEED if attempt == 1 else random.randint(1, 999_999)
                log.info("Attempt %d/%d (seed=%s)", attempt, max_attempts, seed)

                attempt_path = tmp_dir / f"attempt_{attempt:02d}.png"

                # Generate image — WaveSpeed primary, VoidAI fallback
                generated = False
                try:
                    await wave.generate_text2img(
                        prompt,
                        size=THUMBNAIL_SIZE_WS,
                        seed=seed,
                        num_inference_steps=4,
                        output_path=attempt_path,
                    )
                    generated = True
                except Exception as exc:
                    log.warning("WaveSpeed attempt %d failed: %s", attempt, exc)

                if not generated:
                    log.info("Trying VoidAI fallback (attempt %d)...", attempt)
                    try:
                        await void.generate_image(
                            prompt,
                            model=FALLBACK_IMG_MODEL,
                            size=THUMBNAIL_SIZE_VAI,
                            output_path=attempt_path,
                        )
                        generated = True
                    except Exception as exc2:
                        log.error("VoidAI fallback also failed: %s", exc2)
                        continue

                if not attempt_path.exists() or attempt_path.stat().st_size < MIN_FILE_BYTES:
                    log.warning("Attempt %d: file too small or missing", attempt)
                    continue

                # Validate quality
                if iterate:
                    val = await _validate_thumbnail(
                        attempt_path, prompt, niche, void, vision_model,
                    )
                else:
                    # No validation — treat as perfect
                    val = ValidationResult(passed=True, score=6, criteria={}, issues=[])

                # Track best result
                if val.score > best_score:
                    best_score  = val.score
                    best_path   = attempt_path
                    best_result = val

                if val.passed:
                    log.info("Attempt %d passed (score=%d/6) ✓", attempt, val.score)
                    break

                log.info(
                    "Attempt %d: score=%d/6 < %d — will retry",
                    attempt, val.score, PASS_THRESHOLD,
                )

    finally:
        # Copy best result to final output; clean up temp files
        if best_path and best_path.exists():
            shutil.copy2(best_path, out_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not out_path.exists():
        raise RuntimeError(
            f"Thumbnail generation failed after {attempts_made} attempt(s). "
            "Check logs for details."
        )

    elapsed = time.monotonic() - t0
    size_kb = out_path.stat().st_size // 1024
    passed  = best_result.passed if best_result else True

    log.info(
        "Done: %s (%d KB) | attempts=%d | score=%d/6 | passed=%s | %.1fs",
        out_path.name, size_kb, attempts_made, best_score, passed, elapsed,
    )
    return ThumbnailResult(
        output_path=out_path,
        prompt_used=prompt,
        attempts=attempts_made,
        passed_validation=passed,
        score=best_score,
        issues=best_result.issues if best_result else [],
    )


# ─── Multi-variant generation ─────────────────────────────────────────────────

# Fixed seeds for 3 variants — different enough to produce diverse compositions
VARIANT_SEEDS = [42, 1337, 777777]


async def generate_thumbnail_variants(
    script_path:         str | Path,
    channel_config_path: str | Path,
    *,
    count:               int = 3,
    transcriber_dir:     str | Path | None = None,
    prompt_override:     str | None = None,
    text_overlay:        str | None = None,
    iterate:             bool = True,
    preset:              str | None = None,
    dry_run:             bool = False,
) -> list[ThumbnailResult]:
    """
    Generate N thumbnail variants (for A/B testing) from script.json.

    Saves thumbnail_1.png … thumbnail_N.png in output/.
    Also copies the best-scored one to thumbnail.png (for backward compat).

    Args:
        count: Number of variants to generate (default 3).
        Other args: same as generate_thumbnail().

    Returns:
        List of ThumbnailResult, one per variant, sorted best-first.
    """
    load_env()

    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found: {script_path}")

    script         = json.loads(script_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)
    base_dir       = script_path.parent
    out_dir        = base_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    transcriber_data: dict[str, Any] | None = None
    if transcriber_dir:
        try:
            transcriber_data = load_transcriber_output(transcriber_dir)
        except (FileNotFoundError, NotADirectoryError) as exc:
            log.warning("Transcriber dir not usable: %s", exc)

    prompt       = _build_prompt(script, channel_config, transcriber_data, prompt_override, text_overlay)
    llm_preset   = get_llm_preset(channel_config, preset)
    vision_model = llm_preset.get("thumbnail", VISION_MODEL)
    niche        = channel_config.get("niche", "educational")

    log.info("Generating %d thumbnail variants | prompt: %s", count, prompt[:100])

    if dry_run:
        results = []
        for i in range(1, count + 1):
            results.append(ThumbnailResult(
                output_path=out_dir / f"thumbnail_{i}.png",
                prompt_used=prompt,
                attempts=0,
                passed_validation=True,
                score=-1,
            ))
        log.info("[DRY RUN] Would generate %d variants in %s", count, out_dir)
        return results

    tmp_dir = out_dir / ".thumb_variants_tmp"
    tmp_dir.mkdir(exist_ok=True)

    results: list[ThumbnailResult] = []

    # Determine image backend from channel config (same logic as 02_image_generator)
    _img_provider = (channel_config.get("images") or {}).get("provider", "wavespeed")

    try:
        async with VoiceImageClient(aspect_ratio="16:9") as voiceimg, \
                   WaveSpeedClient() as wave, VoidAIClient() as void:
            seeds = VARIANT_SEEDS[:count]
            # Pad with random seeds if count > len(VARIANT_SEEDS)
            while len(seeds) < count:
                seeds.append(random.randint(100_000, 999_999))

            for i, seed in enumerate(seeds, 1):
                log.info("Variant %d/%d (seed=%d)…", i, count, seed)
                tmp_path = tmp_dir / f"variant_{i}.png"
                out_path = out_dir / f"thumbnail_{i}.png"

                # Generate — VoiceImage primary, WaveSpeed 2nd, VoidAI 3rd
                generated = False

                # Try VoiceImage first (if configured or always as cheapest option)
                if not generated and _img_provider in ("voiceimage", "betatest"):
                    try:
                        await voiceimg.generate_text2img(
                            prompt,
                            output_path=tmp_path,
                        )
                        generated = True
                    except Exception as exc:
                        log.warning("VoiceImage variant %d failed: %s", i, exc)

                # Try WaveSpeed
                if not generated:
                    try:
                        await wave.generate_text2img(
                            prompt,
                            size=THUMBNAIL_SIZE_WS,
                            seed=seed,
                            num_inference_steps=4,
                            output_path=tmp_path,
                        )
                        generated = True
                    except Exception as exc:
                        log.warning("WaveSpeed variant %d failed: %s", i, exc)

                # Try VoidAI last
                if not generated:
                    try:
                        await void.generate_image(
                            prompt,
                            model=FALLBACK_IMG_MODEL,
                            size=THUMBNAIL_SIZE_VAI,
                            output_path=tmp_path,
                        )
                        generated = True
                    except Exception as exc2:
                        log.error("VoidAI fallback variant %d failed: %s", i, exc2)
                        continue

                if not tmp_path.exists() or tmp_path.stat().st_size < MIN_FILE_BYTES:
                    log.warning("Variant %d: file too small or missing", i)
                    continue

                # Validate quality
                if iterate:
                    val = await _validate_thumbnail(tmp_path, prompt, niche, void, vision_model)
                else:
                    val = ValidationResult(passed=True, score=6, criteria={}, issues=[])

                shutil.copy2(tmp_path, out_path)
                log.info(
                    "Variant %d saved: %s (score=%d/6, passed=%s)",
                    i, out_path.name, val.score, val.passed,
                )
                results.append(ThumbnailResult(
                    output_path=out_path,
                    prompt_used=prompt,
                    attempts=1,
                    passed_validation=val.passed,
                    score=val.score,
                    issues=val.issues,
                ))

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not results:
        raise RuntimeError("All thumbnail variant generations failed. Check logs.")

    # Copy best-scored variant to thumbnail.png (backward compat + pipeline default)
    best = max(results, key=lambda r: r.score)
    best_idx = results.index(best) + 1
    shutil.copy2(best.output_path, out_dir / THUMBNAIL_FILENAME)
    log.info(
        "Best variant: thumbnail_%d.png (score=%d/6) → copied to thumbnail.png",
        best_idx, best.score,
    )

    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — Thumbnail Generator (Module 06)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python modules/06_thumbnail_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json

  # With Transcriber reference prompt:
  python modules/06_thumbnail_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json \\
      --transcriber-dir "D:/transscript batch/output/output/My Video Title"

  # Custom text overlay, skip validation:
  python modules/06_thumbnail_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json \\
      --text "The REAL Story" --no-iterate

  # Dry run:
  python modules/06_thumbnail_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/history.json --dry-run
        """,
    )

    parser.add_argument("--script",  required=True, help="Path to script.json")
    parser.add_argument("--channel", required=True, help="Channel config JSON path")
    parser.add_argument(
        "--transcriber-dir", default=None,
        help="Transcriber output directory (provides thumbnail_prompt.txt)",
    )
    parser.add_argument("--prompt",  default=None, help="Override thumbnail prompt")
    parser.add_argument(
        "--text", default=None,
        help="Text overlay hint (e.g. 'The REAL Story') added to generation prompt",
    )
    parser.add_argument("--output", default=None, help="Output path (default: output/thumbnail.png)")
    parser.add_argument(
        "--max-attempts", type=int, default=MAX_ATTEMPTS,
        help=f"Max generation attempts (default: {MAX_ATTEMPTS})",
    )
    parser.add_argument(
        "--no-iterate", action="store_true",
        help="Skip vision quality validation (single-pass, faster)",
    )
    parser.add_argument(
        "--preset", default=None,
        help="LLM preset for vision model (max/high/balanced/bulk/test)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate inputs and show plan without generating",
    )

    args = parser.parse_args()

    result = asyncio.run(generate_thumbnail(
        script_path=args.script,
        channel_config_path=args.channel,
        transcriber_dir=args.transcriber_dir,
        prompt_override=args.prompt,
        text_overlay=args.text,
        output_path=args.output,
        max_attempts=args.max_attempts,
        iterate=not args.no_iterate,
        preset=args.preset,
        dry_run=args.dry_run,
    ))

    if not args.dry_run:
        log.info(
            "Thumbnail saved: %s | score=%d/6 | attempts=%d | passed=%s",
            result.output_path, result.score, result.attempts, result.passed_validation,
        )


if __name__ == "__main__":
    _main()
