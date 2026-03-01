"""
VideoForge — Module 02: Image Generator.

Reads script.json → generates images for each block via WaveSpeed (parallel).
Optional image quality validation via VoidAI vision.

Features:
- Parallel generation with asyncio.gather (WaveSpeed semaphore handles rate limiting)
- Image validation: VoidAI vision checks quality → auto-regenerate on fail (max 2 retries)
- Fallback: VoidAI image gen (gpt-image-1.5) if WaveSpeed fails completely
- tqdm progress bar
- Skip existing: if image already exists → skip (step caching)
- Image style injection from channel config (appended to each prompt)

CLI:
    python modules/02_image_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json

    # Dry run (no API calls):
    python modules/02_image_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json --dry-run
"""

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_channel_config, load_env, setup_logging

log = setup_logging("image_gen")

# ─── Constants ────────────────────────────────────────────────────────────────

VISION_VALIDATOR_MODEL = "gpt-4.1-mini"   # Cheap vision model for quality check
FALLBACK_IMAGE_MODEL   = "gpt-image-1.5"  # VoidAI fallback if WaveSpeed fails
DEFAULT_SIZE           = "1280*720"        # WaveSpeed 16:9
FALLBACK_SIZE          = "1792x1024"       # VoidAI 16:9 (x not *)
MAX_VALIDATION_RETRIES = 2                 # Extra attempts if validation fails
MIN_FILE_SIZE_BYTES    = 5_000            # Files smaller than this are considered corrupt

_VALIDATION_PROMPT = """\
You are a quality validator for AI-generated images in a YouTube video pipeline.

Target prompt: "{prompt}"

Evaluate this image on 3 criteria:
1. MATCH   — Does the image visually represent the described content?
2. CLEAN   — Is the image free of visible text, watermarks, logos, or UI overlays?
3. QUALITY — Is the image technically acceptable (no severe artifacts, heavy blur, or corruption)?

Respond ONLY with valid JSON on one line:
{{"ok": true, "issues": [], "score": 3}}

Rules:
- "ok": true if ALL 3 criteria pass, false otherwise
- "issues": list of failed criterion names (empty if ok=true)
- "score": integer 0-3 (count of passing criteria)
- No markdown, no explanation — pure JSON only\
"""


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ImageResult:
    block_id: str
    order: int
    path: str | None          # Local file path, or None if failed
    prompt: str
    attempts: int = 1
    validation_score: int | None = None
    fallback_used: bool = False
    skipped: bool = False
    error: str | None = None


@dataclass
class GenerationSummary:
    total: int           # Blocks with image_prompt
    generated: int       # Successfully generated (new)
    skipped: int         # Already existed (cache hit)
    failed: int          # Failed all attempts
    fallback_count: int  # Used VoidAI instead of WaveSpeed
    wavespeed_cost: float
    voidai_cost: float
    elapsed: float
    results: list[ImageResult] = field(default_factory=list)


# ─── Image validation ─────────────────────────────────────────────────────────

async def _validate_image(
    image_path: Path,
    prompt: str,
    voidai: Any,
) -> tuple[bool, list[str], int]:
    """
    Validate a generated image via VoidAI vision (gpt-4.1-mini).

    Returns:
        (ok: bool, issues: list[str], score: int 0-3)

    On any error returns (True, [], 3) — treat as OK to avoid blocking the pipeline.
    """
    from clients.voidai_client import VoidAIClient  # noqa: PLC0415

    val_prompt = _VALIDATION_PROMPT.format(prompt=prompt[:300])

    try:
        msg = VoidAIClient.image_message(image_path, val_prompt)
        raw = await voidai.vision_completion(
            [msg],
            model=VISION_VALIDATOR_MODEL,
            max_tokens=120,
            temperature=0.1,
        )

        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean.strip())

        data = json.loads(clean)
        ok: bool = bool(data.get("ok", True))
        issues: list[str] = data.get("issues", [])
        score: int = int(data.get("score", 3))
        return ok, issues, score

    except Exception as exc:
        log.warning("Image validation error (%s: %s) — treating as OK", type(exc).__name__, exc)
        return True, [], 3


# ─── Per-block generation ─────────────────────────────────────────────────────

async def _generate_one(
    block: dict[str, Any],
    images_dir: Path,
    image_style: str,
    wavespeed: Any,
    voidai: Any,
    *,
    size: str,
    validate: bool,
    skip_existing: bool,
    max_retries: int,
) -> ImageResult:
    """
    Generate image for a single script block.

    Retry logic:
    - Attempt 1:   WaveSpeed, seed=42  → validate
    - Attempt 2+:  WaveSpeed, seed=None (random) → validate
    - Any WaveSpeed exception → switch to VoidAI fallback for all remaining attempts
    - After max_retries+1 attempts: accept image regardless of validation score
    """
    block_id   = block["id"]
    order      = block["order"]
    raw_prompt = block.get("image_prompt", "").strip()

    # Blocks with no image_prompt (CTA etc.) — skip silently
    if not raw_prompt:
        log.debug("Block %s: no image_prompt — skip", block_id)
        return ImageResult(block_id=block_id, order=order, path=None, prompt="", skipped=True)

    # Full prompt = block prompt + channel style suffix
    full_prompt = f"{raw_prompt}, {image_style}".strip(", ") if image_style else raw_prompt
    out_path = images_dir / f"{block_id}.png"

    # Cache hit
    if skip_existing and out_path.exists() and out_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
        log.info("Block %s: cached → %s", block_id, out_path.name)
        return ImageResult(block_id=block_id, order=order, path=str(out_path), prompt=raw_prompt, skipped=True)

    result = ImageResult(block_id=block_id, order=order, path=None, prompt=raw_prompt)
    wavespeed_failed = False

    max_attempts = max_retries + 1  # Initial + retries

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt

        # ── Generate ──
        gen_ok = False
        if not wavespeed_failed:
            try:
                seed = 42 if attempt == 1 else None  # Fixed seed first, random on retries
                await wavespeed.generate_text2img(
                    full_prompt,
                    size=size,
                    seed=seed,
                    output_path=out_path,
                )
                gen_ok = True
            except Exception as exc:
                log.warning(
                    "Block %s: WaveSpeed error (attempt %d): %s — switching to VoidAI fallback",
                    block_id, attempt, exc,
                )
                wavespeed_failed = True
                result.fallback_used = True

        if wavespeed_failed:
            try:
                await voidai.generate_image(
                    full_prompt,
                    model=FALLBACK_IMAGE_MODEL,
                    size=FALLBACK_SIZE,
                    output_path=out_path,
                )
                gen_ok = True
            except Exception as exc:
                log.error("Block %s: VoidAI fallback error (attempt %d): %s", block_id, attempt, exc)
                if attempt >= max_attempts:
                    result.error = str(exc)
                    return result
                continue

        if not gen_ok:
            continue

        # ── Validate ──
        if validate and out_path.exists() and out_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
            ok, issues, score = await _validate_image(out_path, raw_prompt, voidai)
            result.validation_score = score

            if ok:
                log.info("Block %s: OK (score=%d/3, attempt=%d)", block_id, score, attempt)
                result.path = str(out_path)
                return result

            if attempt < max_attempts:
                log.warning(
                    "Block %s: validation FAIL (score=%d/3, issues=%s) — retry %d/%d",
                    block_id, score, issues, attempt, max_attempts,
                )
                continue
            else:
                # Last attempt — accept despite failed validation
                log.warning(
                    "Block %s: accepting image despite validation fail (score=%d/3)",
                    block_id, score,
                )
                result.path = str(out_path)
                return result
        else:
            # No validation
            if out_path.exists() and out_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
                log.info("Block %s: generated (attempt=%d)", block_id, attempt)
                result.path = str(out_path)
            else:
                log.error("Block %s: output file missing or too small after generation", block_id)
                result.error = "output file missing"
            return result

    # Exhausted all attempts
    if out_path.exists() and out_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
        result.path = str(out_path)
    else:
        result.error = "all attempts exhausted"
    return result


# ─── Main API ─────────────────────────────────────────────────────────────────

async def generate_images(
    script_path: str | Path,
    channel_config_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    validate: bool = True,
    skip_existing: bool = True,
    dry_run: bool = False,
    max_retries: int = MAX_VALIDATION_RETRIES,
    size: str | None = None,
    progress_callback: Any | None = None,
) -> GenerationSummary:
    """
    Generate images for all blocks in a script.json.

    Args:
        script_path: Path to script.json.
        channel_config_path: Path to channel config JSON.
        output_dir: Output directory for images. Default: script_path.parent/images/.
        validate: Run VoidAI vision quality check after each generation.
        skip_existing: Skip blocks where image already exists (step caching).
        dry_run: Log plan without making API calls.
        max_retries: Max regeneration attempts per image if validation fails.
        size: WaveSpeed image size string (e.g. "1280*720"). Default from constants.

    Returns:
        GenerationSummary with per-block results and cost totals.
    """
    load_env()

    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found: {script_path}")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)

    blocks: list[dict[str, Any]] = script.get("blocks", [])
    image_style = channel_config.get("image_style", "")
    image_size = size or DEFAULT_SIZE

    # Output directory
    images_dir = Path(output_dir) if output_dir else script_path.parent / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Only blocks that have an image_prompt
    blocks_with_prompts = [b for b in blocks if b.get("image_prompt", "").strip()]
    n_skippable = len(blocks) - len(blocks_with_prompts)

    log.info(
        "Script: %d blocks total | %d with image_prompt | %d skipped (no prompt)",
        len(blocks), len(blocks_with_prompts), n_skippable,
    )
    log.info("Style: %s", (image_style[:80] + "...") if len(image_style) > 80 else image_style or "(none)")
    log.info("Size: %s | Validate: %s | Skip existing: %s", image_size, validate, skip_existing)

    if dry_run:
        n_existing = sum(
            1 for b in blocks_with_prompts
            if (images_dir / f"{b['id']}.png").exists()
            and (images_dir / f"{b['id']}.png").stat().st_size >= MIN_FILE_SIZE_BYTES
        )
        n_to_gen = len(blocks_with_prompts) - n_existing
        log.info(
            "[DRY RUN] Would generate %d images, skip %d existing | Est. cost: $%.3f",
            n_to_gen, n_existing, n_to_gen * 0.005,
        )
        return GenerationSummary(
            total=len(blocks_with_prompts),
            generated=0, skipped=0, failed=0, fallback_count=0,
            wavespeed_cost=0.0, voidai_cost=0.0, elapsed=0.0,
        )

    t0 = time.monotonic()

    from clients.voidai_client import VoidAIClient       # noqa: PLC0415
    from clients.wavespeed_client import WaveSpeedClient  # noqa: PLC0415

    # ── Progress helper (reports 0-100% of image generation) ──
    n_img_total = len(blocks_with_prompts)
    _img_done = [0]

    def _emit_img_progress(done: int) -> None:
        if progress_callback and n_img_total > 0:
            try:
                pct = round(done / n_img_total * 100.0, 1)
                progress_callback({
                    "type": "sub_progress",
                    "pct": pct,
                    "message": f"Image {done}/{n_img_total}",
                })
            except Exception:
                pass

    results: list[ImageResult] = []

    async with WaveSpeedClient() as wavespeed, VoidAIClient() as voidai:
        coros = [
            _generate_one(
                block=b,
                images_dir=images_dir,
                image_style=image_style,
                wavespeed=wavespeed,
                voidai=voidai,
                size=image_size,
                validate=validate,
                skip_existing=skip_existing,
                max_retries=max_retries,
            )
            for b in blocks_with_prompts
        ]

        # Progress bar via tqdm if available
        try:
            from tqdm.asyncio import tqdm as atqdm  # noqa: PLC0415
            tasks = [asyncio.ensure_future(c) for c in coros]
            for fut in atqdm(
                asyncio.as_completed(tasks),
                total=len(tasks),
                desc="Generating images",
                unit="img",
            ):
                results.append(await fut)
                _img_done[0] += 1
                _emit_img_progress(_img_done[0])

        except ImportError:
            log.info("tqdm not available — running without progress bar")
            raw = await asyncio.gather(*coros, return_exceptions=True)
            for i, item in enumerate(raw):
                if isinstance(item, Exception):
                    log.error("Unexpected gather exception: %s", item)
                else:
                    results.append(item)
                _emit_img_progress(i + 1)

        wavespeed_cost = wavespeed.session_cost
        voidai_cost    = voidai.session_cost

    elapsed = time.monotonic() - t0

    # Sort by block order for consistent output
    results.sort(key=lambda r: r.order)

    # Tally
    generated     = sum(1 for r in results if r.path and not r.skipped)
    skipped       = sum(1 for r in results if r.skipped)
    failed        = sum(1 for r in results if not r.path and not r.skipped)
    fallback_count = sum(1 for r in results if r.fallback_used)

    log.info(
        "Done: %d generated | %d skipped | %d failed | %d fallback | "
        "WaveSpeed=$%.3f VoidAI=$%.3f | %.1fs",
        generated, skipped, failed, fallback_count,
        wavespeed_cost, voidai_cost, elapsed,
    )

    if failed:
        failed_ids = [r.block_id for r in results if not r.path and not r.skipped]
        log.warning("Failed blocks: %s", failed_ids)

    return GenerationSummary(
        total=len(blocks_with_prompts),
        generated=generated,
        skipped=skipped,
        failed=failed,
        fallback_count=fallback_count,
        wavespeed_cost=wavespeed_cost,
        voidai_cost=voidai_cost,
        elapsed=elapsed,
        results=results,
    )


# ─── CLI ──────────────────────────────────────────────────────────────────────

async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — Image Generator (Module 02)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python modules/02_image_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json

  # Custom output dir + no validation (faster):
  python modules/02_image_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json \\
      --output projects/my_video/images --no-validate

  # Dry run:
  python modules/02_image_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json \\
      --dry-run
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
        "--output",
        help="Output directory for images (default: script.json dir / images/)",
    )
    parser.add_argument(
        "--size",
        default=None,
        help=f'WaveSpeed image size e.g. "1280*720", "1920*1080" (default: {DEFAULT_SIZE})',
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip VoidAI vision quality validation (faster, cheaper)",
    )
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Regenerate even if image already exists (disables caching)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_VALIDATION_RETRIES,
        help=f"Max regeneration attempts per image if validation fails (default: {MAX_VALIDATION_RETRIES})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show plan and cost estimate without making API calls",
    )

    args = parser.parse_args()

    summary = await generate_images(
        script_path=args.script,
        channel_config_path=args.channel,
        output_dir=args.output,
        validate=not args.no_validate,
        skip_existing=not args.no_skip,
        dry_run=args.dry_run,
        max_retries=args.max_retries,
        size=args.size,
    )

    if args.dry_run:
        return

    if summary.failed > 0:
        log.warning("%d image(s) failed — pipeline can continue with missing images", summary.failed)
        sys.exit(2)  # Exit code 2 = partial failure (not fatal)

    log.info("Image generation complete: %d/%d images ready", summary.generated + summary.skipped, summary.total)


if __name__ == "__main__":
    asyncio.run(_main())
