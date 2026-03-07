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
import hashlib
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
FALLBACK_SIZE          = "1536x1024"       # VoidAI 16:9 — valid size (1792x1024 not supported)
MAX_VALIDATION_RETRIES = 2                 # Extra attempts if validation fails
MIN_FILE_SIZE_BYTES    = 5_000            # Files smaller than this are considered corrupt

def _derive_video_seed(title: str) -> int:
    """Derive a stable per-video seed from the video title.

    Using MD5 (not Python's hash(), which is process-randomized by PYTHONHASHSEED)
    so the same title always produces the same seed across runs — enabling
    reproducible image generation for the same video.

    All images in a video share a seed family (video_seed + block_order),
    which nudges the model toward a consistent visual style within a video
    while different videos get distinct visual DNA.

    Returns:
        Integer seed in range [0, 2**30) — safe for all image gen APIs.
    """
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (2 ** 30)


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

def _image_output_name(block_id: str, idx: int) -> str:
    """Return output filename for a block image at given index.

    idx=0 → {block_id}.png   (primary — backward compatible)
    idx>0 → {block_id}_{idx}.png  (additional images from image_prompts list)
    """
    return f"{block_id}.png" if idx == 0 else f"{block_id}_{idx}.png"


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
    idx: int = 0,  # image index: 0=primary ({block_id}.png), 1+= additional ({block_id}_N.png)
    wavespeed_globally_failed: list[bool] | None = None,  # shared mutable flag across coroutines
    block_seed: int | None = None,  # per-video seed for style consistency (video_seed + block_order)
) -> ImageResult:
    """
    Generate one image for a script block.

    idx=0 uses block["image_prompt"] (or image_prompts[0]).
    idx>0 uses block["image_prompts"][idx] — additional images for v2 multi-prompt sections.

    Retry logic:
    - Attempt 1:   WaveSpeed, seed=42  → validate
    - Attempt 2+:  WaveSpeed, seed=None (random) → validate
    - Any WaveSpeed exception → switch to VoidAI fallback for all remaining attempts
    - wavespeed_globally_failed: once ANY block hits a WaveSpeed API error, all subsequent
      blocks skip WaveSpeed entirely and go straight to VoidAI — prevents mass failed charges.
    - After max_retries+1 attempts: accept image regardless of validation score
    """
    block_id = block["id"]
    order    = block["order"]

    # Get the right prompt for this index
    image_prompts_list = block.get("image_prompts", [])
    if idx > 0 and idx < len(image_prompts_list):
        raw_prompt = image_prompts_list[idx].strip()
    else:
        raw_prompt = block.get("image_prompt", "").strip()

    # Blocks with no prompt (CTA etc.) — skip silently
    if not raw_prompt:
        log.debug("Block %s[%d]: no image_prompt — skip", block_id, idx)
        return ImageResult(block_id=block_id, order=order, path=None, prompt="", skipped=True)

    # image_style is already embedded in raw_prompt by the script generator (Step 1 LLM).
    # Do NOT append it again — single source, UI-provided only.
    full_prompt = raw_prompt
    out_path = images_dir / _image_output_name(block_id, idx)

    # Cache hit
    if skip_existing and out_path.exists() and out_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
        log.info("Block %s: cached → %s", block_id, out_path.name)
        return ImageResult(block_id=block_id, order=order, path=str(out_path), prompt=raw_prompt, skipped=True)

    result = ImageResult(block_id=block_id, order=order, path=None, prompt=raw_prompt)
    # Inherit global failure state — if WaveSpeed is broken for ANY block, skip it for ALL
    wavespeed_failed = bool(wavespeed_globally_failed and wavespeed_globally_failed[0])
    if wavespeed_failed:
        result.fallback_used = True
        log.debug("Block %s: WaveSpeed globally failed — using VoidAI directly", block_id)

    max_attempts = max_retries + 1  # Initial + retries

    # Track the best image across attempts (score + path) so we never discard a
    # better image when a retry produces something worse — WaveSpeed already
    # charged for each generation, so we want to keep the best result.
    best_score: int = -1
    best_path:  Path | None = None

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt

        # On retries, generate to a temp path so we can compare before overwriting
        attempt_path = out_path if attempt == 1 else out_path.with_suffix(f".attempt{attempt}.png")

        # ── Generate ──
        gen_ok = False
        if not wavespeed_failed:
            try:
                seed = block_seed if attempt == 1 else None  # Per-video seed first, random on retries
                await wavespeed.generate_text2img(
                    full_prompt,
                    size=size,
                    seed=seed,
                    output_path=attempt_path,
                )
                gen_ok = True
            except Exception as exc:
                log.warning(
                    "Block %s: WaveSpeed error (attempt %d): %s — switching to VoidAI fallback",
                    block_id, attempt, exc,
                )
                wavespeed_failed = True
                result.fallback_used = True
                # Mark globally so other concurrent blocks skip WaveSpeed immediately
                if wavespeed_globally_failed is not None:
                    wavespeed_globally_failed[0] = True
                    log.warning("WaveSpeed marked as globally failed — remaining blocks → VoidAI")

        if wavespeed_failed:
            try:
                await voidai.generate_image(
                    full_prompt,
                    model=FALLBACK_IMAGE_MODEL,
                    size=FALLBACK_SIZE,
                    output_path=attempt_path,
                )
                gen_ok = True
            except Exception as exc:
                log.error("Block %s: VoidAI fallback error (attempt %d): %s", block_id, attempt, exc)
                if attempt >= max_attempts:
                    result.error = str(exc)
                    # Keep the best image we have, even if generation failed this attempt
                    if best_path and best_path.exists():
                        if best_path != out_path:
                            import shutil
                            shutil.move(str(best_path), str(out_path))
                        result.path = str(out_path)
                    return result
                continue

        if not gen_ok:
            continue

        # ── Validate ──
        if validate and attempt_path.exists() and attempt_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
            ok, issues, score = await _validate_image(attempt_path, raw_prompt, voidai)
            result.validation_score = score

            # Keep track of the best image seen so far
            if score > best_score:
                best_score = score
                # If this is a retry path, we need to copy/move it as the best candidate
                if best_path and best_path.exists() and best_path != attempt_path:
                    best_path.unlink(missing_ok=True)
                best_path = attempt_path

            if ok:
                log.info("Block %s: OK (score=%d/3, attempt=%d)", block_id, score, attempt)
                # Move best image to final path if needed
                if best_path != out_path:
                    import shutil
                    shutil.move(str(best_path), str(out_path))
                result.path = str(out_path)
                return result

            if attempt < max_attempts:
                log.warning(
                    "Block %s: validation FAIL (score=%d/3, issues=%s) — retry %d/%d",
                    block_id, score, issues, attempt, max_attempts,
                )
                continue
            else:
                # Last attempt — use the best image we have
                log.warning(
                    "Block %s: accepting best image (score=%d/3) after %d attempts",
                    block_id, best_score, attempt,
                )
                if best_path and best_path.exists():
                    if best_path != out_path:
                        import shutil
                        shutil.move(str(best_path), str(out_path))
                    result.path = str(out_path)
                else:
                    result.error = "all attempts failed validation"
                return result
        else:
            # No validation — accept the generated image as-is
            if attempt_path.exists() and attempt_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
                log.info("Block %s: generated (attempt=%d)", block_id, attempt)
                if attempt_path != out_path:
                    import shutil
                    shutil.move(str(attempt_path), str(out_path))
                result.path = str(out_path)
            else:
                log.error("Block %s: output file missing or too small after generation", block_id)
                result.error = "output file missing"
            return result

    # Exhausted all attempts — use the best image we have
    if best_path and best_path.exists():
        if best_path != out_path:
            import shutil
            shutil.move(str(best_path), str(out_path))
        result.path = str(out_path)
    elif out_path.exists() and out_path.stat().st_size >= MIN_FILE_SIZE_BYTES:
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
    image_style: str | None = None,
    video_seed: int | None = None,  # Per-video style seed; None = auto-derive from title
    image_backend: str | None = None,  # "wavespeed" (default) | "betatest" | "voidai"
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
        image_style: Override image style from channel config (e.g. "cinematic, 8k").
            If None, falls back to channel_config["image_style"].

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
    # image_style param overrides channel_config value when provided
    image_style = image_style or ""  # UI-provided only; no channel_config fallback
    image_size = size or DEFAULT_SIZE

    # ── Per-video style seed ──────────────────────────────────────────────────
    # All images in a video use seeds from the same "family" (video_seed + block_order)
    # so the model tends toward a consistent visual aesthetic within a video.
    # Different videos get different seeds → different visual identity.
    if video_seed is None:
        title = script.get("video_title") or script.get("title") or ""
        video_seed = _derive_video_seed(title) if title else 42
    log.info("Image style seed: %d (video: %s)", video_seed, (script.get("video_title") or "")[:50])

    # Output directory
    images_dir = Path(output_dir) if output_dir else script_path.parent / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Build generation jobs: (block, idx) pairs.
    # For v2 scripts with image_prompts list → one job per entry.
    # For v1 scripts with only image_prompt → one job (idx=0).
    all_jobs: list[tuple[dict, int]] = []
    for b in blocks:
        prompts_list = b.get("image_prompts", [])
        if prompts_list:
            for i in range(len(prompts_list)):
                all_jobs.append((b, i))
        elif b.get("image_prompt", "").strip():
            all_jobs.append((b, 0))

    n_blocks_with_images = len({b["id"] for b, _ in all_jobs})
    n_total_images = len(all_jobs)

    log.info(
        "Script: %d blocks | %d unique blocks with images | %d total images to generate",
        len(blocks), n_blocks_with_images, n_total_images,
    )
    log.info("Style: %s", (image_style[:80] + "...") if len(image_style) > 80 else image_style or "(none)")
    log.info("Size: %s | Validate: %s | Skip existing: %s", image_size, validate, skip_existing)

    if dry_run:
        n_existing = sum(
            1 for b, idx in all_jobs
            if (images_dir / _image_output_name(b["id"], idx)).exists()
            and (images_dir / _image_output_name(b["id"], idx)).stat().st_size >= MIN_FILE_SIZE_BYTES
        )
        n_to_gen = n_total_images - n_existing
        log.info(
            "[DRY RUN] Would generate %d images, skip %d existing | Est. cost: $%.3f",
            n_to_gen, n_existing, n_to_gen * 0.005,
        )
        return GenerationSummary(
            total=n_total_images,
            generated=0, skipped=0, failed=0, fallback_count=0,
            wavespeed_cost=0.0, voidai_cost=0.0, elapsed=0.0,
        )

    t0 = time.monotonic()

    from clients.voidai_client import VoidAIClient  # noqa: PLC0415

    # ── Select primary image generation backend ───────────────────────────────
    _backend = (image_backend or "wavespeed").lower().strip()
    log.info("Image backend: %s", _backend)

    if _backend in ("betatest", "voiceimage"):
        # "betatest" kept as alias for backward compat — betatestru.csv666.ru is shut down,
        # voiceimage (voiceapi.csv666.ru) is its replacement with the same VOICEAPI_KEY.
        from clients.voiceimage_client import VoiceImageClient  # noqa: PLC0415
        PrimaryClient = VoiceImageClient
        image_size = "16:9"
    elif _backend == "voidai":
        PrimaryClient = None   # VoidAI only — primary client skipped entirely
        image_size = FALLBACK_SIZE
    else:  # "wavespeed" (default)
        from clients.wavespeed_client import WaveSpeedClient  # noqa: PLC0415
        PrimaryClient = WaveSpeedClient

    # ── Progress helper (reports 0-100% of image generation) ──
    n_img_total = n_total_images
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

    # Shared mutable flag: set to True the moment any primary client call fails.
    # Remaining blocks skip the primary client and use VoidAI directly.
    _ws_global_failed: list[bool] = [_backend == "voidai"]  # True = skip primary from start

    async def _run_coros(primary_client: Any) -> list[ImageResult]:
        # WaveSpeed: share global-failure flag so one API error stops all WaveSpeed calls
        # (prevents wasted charges on a broken session).
        # BetaImage / others: pass None so each block retries independently — a single
        # timeout does NOT force the entire batch to fall back to VoidAI.
        _global_fail_flag = _ws_global_failed if _backend == "wavespeed" else None

        coros = [
            _generate_one(
                block=b,
                images_dir=images_dir,
                image_style=image_style,
                wavespeed=primary_client,
                voidai=voidai,
                size=image_size,
                validate=validate,
                skip_existing=skip_existing,
                max_retries=max_retries,
                idx=idx,
                wavespeed_globally_failed=_global_fail_flag,
                # Per-video seed family: each block gets video_seed + block_order,
                # so images within one video share the same "random DNA" for style consistency
                # while different blocks vary in seed (preserving prompt-driven content differences).
                block_seed=(video_seed + b.get("order", 0)) % (2 ** 30) if video_seed is not None else None,
            )
            for b, idx in all_jobs
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
                try:
                    results.append(await fut)
                except Exception as _task_exc:
                    # Individual task failure must NOT abort the entire loop.
                    # Log and continue — 02b_image_validator will catch missing files.
                    log.error("Image generation task raised unexpectedly: %s", _task_exc)
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

    # ── Open clients and run ──────────────────────────────────────────────────
    primary_cost = 0.0
    voidai_cost  = 0.0

    # Initial generation pass
    if PrimaryClient is not None:
        async with PrimaryClient() as primary_client, VoidAIClient() as voidai:
            await _run_coros(primary_client)
            primary_cost += getattr(primary_client, "session_cost", 0.0)
            voidai_cost  += voidai.session_cost
    else:
        async with VoidAIClient() as voidai:
            await _run_coros(None)
            voidai_cost += voidai.session_cost

    # ── Retry passes: re-generate any images still missing after initial run ──
    _MAX_RETRY_PASSES = 5
    for _retry_pass in range(1, _MAX_RETRY_PASSES + 1):
        _retry_jobs = [
            (b, idx) for b, idx in all_jobs
            if not (images_dir / _image_output_name(b["id"], idx)).exists()
            or (images_dir / _image_output_name(b["id"], idx)).stat().st_size < MIN_FILE_SIZE_BYTES
        ]
        if not _retry_jobs:
            if _retry_pass > 1:
                log.info("All images generated — took %d extra pass(es)", _retry_pass - 1)
            break
        log.warning(
            "Retry pass %d/%d: %d image(s) still missing — retrying",
            _retry_pass, _MAX_RETRY_PASSES, len(_retry_jobs),
        )
        # Remove stale failed entries from results so _run_coros can re-add fresh ones.
        # Keep skipped (CTA / cache-hit) and already-successful results.
        _retry_names = {_image_output_name(b["id"], idx) for b, idx in _retry_jobs}
        results[:] = [
            r for r in results
            if r.skipped or (r.path is not None and Path(r.path).name not in _retry_names)
        ]
        # Narrow all_jobs to only the retry subset (closure reads at call time).
        all_jobs[:] = _retry_jobs
        # Force skip_existing=True so already-generated images are not re-generated.
        skip_existing = True  # noqa: PLW2901  (closure reads this variable dynamically)
        # Reset primary-client failure flag for WaveSpeed so it gets another chance on retry.
        # (For betatest, _global_fail_flag=None so this has no effect on generation behaviour.)
        _ws_global_failed[0] = (_backend == "voidai")
        _img_done[0] = 0
        if PrimaryClient is not None:
            async with PrimaryClient() as primary_client, VoidAIClient() as voidai:
                await _run_coros(primary_client)
                primary_cost += getattr(primary_client, "session_cost", 0.0)
                voidai_cost  += voidai.session_cost
        else:
            async with VoidAIClient() as voidai:
                await _run_coros(None)
                voidai_cost += voidai.session_cost
    else:
        # for-else: loop completed without break → some images still missing after all passes
        _still_missing = sum(
            1 for b, idx in all_jobs
            if not (images_dir / _image_output_name(b["id"], idx)).exists()
            or (images_dir / _image_output_name(b["id"], idx)).stat().st_size < MIN_FILE_SIZE_BYTES
        )
        if _still_missing:
            log.error(
                "Could not generate %d image(s) after %d retry passes",
                _still_missing, _MAX_RETRY_PASSES,
            )

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
        "primary=$%.3f VoidAI=$%.3f | %.1fs",
        generated, skipped, failed, fallback_count,
        primary_cost, voidai_cost, elapsed,
    )

    if failed:
        failed_ids = [r.block_id for r in results if not r.path and not r.skipped]
        log.warning("Failed blocks: %s", failed_ids)

    return GenerationSummary(
        total=n_total_images,
        generated=generated,
        skipped=skipped,
        failed=failed,
        fallback_count=fallback_count,
        wavespeed_cost=primary_cost,
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
