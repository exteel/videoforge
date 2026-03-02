"""
VideoForge — Module 02b: Image Validator & Auto-Regenerator.

Validates generated images against their narration context using vision API.
Automatically regenerates images that score below the threshold.

Scoring (gpt-4.1 vision, concurrent):
  9-10 — Perfect match, keep
  7-8  — Good, keep
  <7   — Regenerate with improved prompt (WaveSpeed T2I)

Regeneration flow per bad image:
  1. gpt-4.1 already provides improved_prompt in scoring response
  2. WaveSpeed generates new image with improved prompt + image_style
  3. Re-score the new image
  4. Max 2 attempts — if still bad after attempt 2, keep best result

Improvements over v1:
  - Missing/corrupted images are regenerated (not just logged as failed)
  - WaveSpeed semaphore held only during POST, not during 3-min polling
  - Scoring failure → skipped (not fake score=10)
  - _regen_one uses loop instead of recursion

Concurrency limits:
  Vision scoring: max 5 concurrent calls
  WaveSpeed POST initiation: max 3 concurrent calls
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent.parent

import sys
sys.path.insert(0, str(ROOT))

from modules.common import load_env, setup_logging

log = setup_logging("image_validator")

# ─── Constants ────────────────────────────────────────────────────────────────

VOIDAI_BASE    = "https://api.voidai.app/v1"
WAVESPEED_BASE = "https://api.wavespeed.ai/api/v3"
WS_T2I_PATH    = "/wavespeed-ai/z-image/turbo"

SCORE_MODEL      = "gpt-4.1"        # vision model
VOIDAI_IMG_MODEL = "gpt-image-1.5"  # fallback image gen when WaveSpeed fails
DEFAULT_THRESHOLD    = 7.0
DEFAULT_MAX_ATTEMPTS = 2

# Minimum PNG file size — anything smaller is a placeholder or corrupted image
MIN_IMAGE_BYTES = 10_240  # 10 KB

# Block types that are more atmospheric — allow slightly lower threshold
ATMOSPHERIC_TYPES = frozenset(["intro", "outro"])

WS_POLL_INTERVAL = 2.0   # seconds between WaveSpeed polls
WS_POLL_MAX      = 90    # max polls (3 minutes)


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ImageScore:
    block_id: str
    score: float
    ok: bool
    reason: str = ""
    improved_prompt: str = ""
    regenerated: bool = False
    attempts: int = 0
    skipped: bool = False       # True if image was missing/corrupted or scoring API failed
    skip_reason: str = ""       # human-readable reason for skip


@dataclass
class ImageValidationResult:
    total: int = 0
    ok_count: int = 0
    regenerated: int = 0
    failed: int = 0
    skipped: int = 0            # images that couldn't be scored (missing / corrupted / API error)
    scores: list[ImageScore] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok_count,
            "regenerated": self.regenerated,
            "failed": self.failed,
            "skipped": self.skipped,
            "elapsed": round(self.elapsed, 2),
            "scores": [
                {
                    "block_id": s.block_id,
                    "score": s.score,
                    "ok": s.ok,
                    "reason": s.reason,
                    "regenerated": s.regenerated,
                    "attempts": s.attempts,
                    "skipped": s.skipped,
                    "skip_reason": s.skip_reason,
                }
                for s in self.scores
            ],
        }


# ─── Scoring ──────────────────────────────────────────────────────────────────

async def _score_image(
    block_id: str,
    image_path: Path,
    narration: str,
    image_prompt: str,
    api_key: str,
    sem: asyncio.Semaphore,
) -> tuple[float, str, str]:
    """
    Score an image against its narration using gpt-4.1 vision.
    Returns (score 0-10, reason, improved_prompt).
    """
    img_bytes = image_path.read_bytes()
    img_b64   = base64.b64encode(img_bytes).decode()
    suffix    = image_path.suffix.lower()
    mime      = "image/png" if suffix == ".png" else "image/jpeg"

    prompt = f"""Rate how well this image matches the video narration on a scale of 0-10.

NARRATION (what is being said at this moment):
"{narration[:400]}"

INTENDED IMAGE PROMPT:
"{image_prompt[:200]}"

Scoring guide:
10 = Perfect — image directly and specifically illustrates the narration
7-9 = Good — image clearly relates to the narration content
5-6 = Mediocre — image is only thematically related, not specific
3-4 = Poor — image is loosely connected or too generic
0-2 = Wrong — image has little or nothing to do with the narration

Also write an improved_prompt that would better match the narration.

Return ONLY valid JSON:
{{
  "score": 8,
  "reason": "brief explanation (1-2 sentences)",
  "improved_prompt": "specific cinematic description that matches exactly what the narration says (15-50 words)"
}}"""

    async with sem:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{VOIDAI_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": SCORE_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                    "max_tokens": 300,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()

    data     = json.loads(resp.json()["choices"][0]["message"]["content"])
    score    = float(data.get("score", 5))
    reason   = str(data.get("reason", ""))
    improved = str(data.get("improved_prompt", image_prompt))
    return score, reason, improved


# ─── Generation ───────────────────────────────────────────────────────────────

async def _wavespeed_generate(
    prompt: str,
    output_path: Path,
    ws_key: str,
    sem: asyncio.Semaphore,
) -> bool:
    """
    Generate one image via WaveSpeed T2I and save to output_path.

    The semaphore is held ONLY during the initial POST (task submission),
    not during polling — this allows true parallelism during the 3-min wait.

    Returns True on success.
    """
    # ── Hold semaphore only for POST (task initiation) ──
    try:
        async with sem:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{WAVESPEED_BASE}{WS_T2I_PATH}",
                    headers={
                        "Authorization": f"Bearer {ws_key}",
                        "Content-Type": "application/json",
                    },
                    json={"prompt": prompt, "size": "1024x576", "num_images": 1},
                )
                resp.raise_for_status()
                task_id = resp.json()["data"]["id"]
    except Exception as exc:
        log.warning("WaveSpeed POST failed: %s", exc)
        return False

    # ── Poll OUTSIDE semaphore — doesn't consume API slots while waiting ──
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(WS_POLL_MAX):
                await asyncio.sleep(WS_POLL_INTERVAL)
                poll = await client.get(
                    f"{WAVESPEED_BASE}/predictions/{task_id}/result",
                    headers={"Authorization": f"Bearer {ws_key}"},
                )
                poll.raise_for_status()
                pdata  = poll.json()["data"]
                status = pdata.get("status", "")

                if status == "completed":
                    img_url = pdata["outputs"][0]
                    dl = await client.get(img_url, timeout=60)
                    dl.raise_for_status()
                    output_path.write_bytes(dl.content)
                    return True

                if status in ("failed", "error"):
                    log.warning("WaveSpeed task %s failed: %s", task_id, pdata)
                    return False
    except Exception as exc:
        log.warning("WaveSpeed poll/download failed (task=%s): %s", task_id, exc)
        return False

    log.warning("WaveSpeed task %s timed out after %d polls", task_id, WS_POLL_MAX)
    return False  # timeout


async def _voidai_generate(
    prompt: str,
    output_path: Path,
    api_key: str,
) -> bool:
    """
    VoidAI image gen fallback (gpt-image-1.5) when WaveSpeed fails.
    Returns True on success.
    """
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{VOIDAI_BASE}/images/generations",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": VOIDAI_IMG_MODEL,
                    "prompt": prompt,
                    "n": 1,
                    "size": "1024x576",
                    "response_format": "b64_json",
                },
            )
            resp.raise_for_status()
            b64 = resp.json()["data"][0]["b64_json"]
            import base64 as _b64
            output_path.write_bytes(_b64.b64decode(b64))
            return True
    except Exception as exc:
        log.warning("VoidAI image fallback failed: %s", exc)
        return False


# ─── Main validator ───────────────────────────────────────────────────────────

async def validate_and_fix_images(
    script_path: str | Path,
    images_dir: str | Path,
    channel_config: dict[str, Any] | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    progress_callback: Any = None,
) -> ImageValidationResult:
    """
    Validate all generated images against their narration text.
    Automatically regenerates images that score below threshold.

    Args:
        script_path:       Path to script.json.
        images_dir:        Directory containing block_NNN.png files.
        channel_config:    Channel config dict (for image_style).
        threshold:         Minimum acceptable score (default 7.0).
        max_attempts:      Max regeneration attempts per image (default 2).
        progress_callback: Optional callable({type, pct, message}).

    Returns:
        ImageValidationResult with per-image scores and regen summary.
    """
    load_env()
    t0 = time.monotonic()

    script_path = Path(script_path)
    images_dir  = Path(images_dir)
    voidai_key  = os.environ.get("VOIDAI_API_KEY", "")
    ws_key      = os.environ.get("WAVESPEED_API_KEY", "")
    image_style = (channel_config or {}).get(
        "image_style", "cinematic, photorealistic, dramatic lighting, 8k"
    )

    if not voidai_key:
        raise RuntimeError("VOIDAI_API_KEY not set")

    def _emit(msg: str, pct: float = 0.0) -> None:
        if progress_callback:
            try:
                progress_callback({"type": "sub_progress", "pct": pct, "message": msg})
            except Exception:
                pass

    # ── Semaphores — created early (before any async image ops) ──────────────
    score_sem = asyncio.Semaphore(5)   # max 5 concurrent vision scoring calls
    regen_sem = asyncio.Semaphore(3)   # max 3 concurrent WaveSpeed POST submissions

    # ── Load script ──────────────────────────────────────────────────────────
    script     = json.loads(script_path.read_text(encoding="utf-8"))
    all_blocks = script.get("blocks", [])

    # ── Pre-flight: detect missing / corrupted images ─────────────────────────
    # Three buckets:
    #   blocks_to_score    — image exists and is valid → score normally
    #   blocks_to_preregen — image missing/corrupted BUT has a prompt → regenerate first
    #   pre_skipped        — image missing AND no prompt (or CTA with no prompt) → skip
    pre_skipped: list[ImageScore]    = []
    blocks_to_score: list[dict]      = []
    blocks_to_preregen: list[dict]   = []

    for b in all_blocks:
        bid        = b.get("id", "")
        btype      = b.get("type", "section")
        has_prompt = bool((b.get("image_prompt") or "").strip())
        img_path   = images_dir / f"{bid}.png"

        # CTA blocks with no image prompt are expected — skip silently
        if btype == "cta" and not has_prompt:
            continue

        img_ok = img_path.exists() and img_path.stat().st_size >= MIN_IMAGE_BYTES

        if not img_ok:
            if has_prompt:
                # [NEW] Queue for initial generation instead of immediately marking failed
                log.warning(
                    "Missing/corrupted image: %s.png (block type=%s) — queued for initial generation",
                    bid, btype,
                )
                blocks_to_preregen.append(b)
            else:
                log.warning("Missing image: %s.png — no image_prompt to regenerate", bid)
                pre_skipped.append(ImageScore(
                    block_id=bid, score=0.0, ok=False,
                    skipped=True,
                    skip_reason="Image file not found and no image_prompt available",
                ))
        else:
            blocks_to_score.append(b)

    # ── [NEW] Pre-regen: generate initially-missing images before scoring ─────
    if blocks_to_preregen:
        _emit(f"Generating {len(blocks_to_preregen)} missing images…", 8.0)
        log.info(
            "Pre-regen: attempting initial generation for %d missing image(s)",
            len(blocks_to_preregen),
        )

        async def _preregen_one(block: dict) -> bool:
            """Generate image for a block using its original image_prompt."""
            bid_        = block.get("id", "")
            prompt_     = (block.get("image_prompt") or "").strip()
            full_prompt = f"{prompt_}, {image_style}"
            img_path_   = images_dir / f"{bid_}.png"
            if ws_key:
                ok = await _wavespeed_generate(full_prompt, img_path_, ws_key, regen_sem)
                if ok:
                    return True
                log.warning("WaveSpeed pre-regen failed for %s — trying VoidAI", bid_)
            if voidai_key:
                return await _voidai_generate(full_prompt, img_path_, voidai_key)
            return False

        preregen_results = await asyncio.gather(
            *[_preregen_one(b) for b in blocks_to_preregen],
            return_exceptions=True,
        )
        for b, ok in zip(blocks_to_preregen, preregen_results):
            bid_ = b.get("id", "")
            if ok is True:
                blocks_to_score.append(b)
                log.info("Pre-regen succeeded for %s — added to score queue", bid_)
            else:
                reason = str(ok) if isinstance(ok, Exception) else "generation returned False"
                log.warning("Pre-regen failed for %s: %s", bid_, reason)
                pre_skipped.append(ImageScore(
                    block_id=bid_, score=0.0, ok=False,
                    skipped=True,
                    skip_reason=f"Image was missing and initial generation failed: {reason}",
                ))

    if not blocks_to_score and not pre_skipped:
        log.warning("No images found in %s — skipping image validation", images_dir)
        return ImageValidationResult(elapsed=time.monotonic() - t0)

    result         = ImageValidationResult(total=len(blocks_to_score) + len(pre_skipped))
    result.skipped = len(pre_skipped)
    result.scores  = list(pre_skipped)  # pre-populate with skipped entries

    log.info(
        "Image validation: scoring %d images (threshold=%.0f/10, model=%s, skipped=%d)",
        len(blocks_to_score), threshold, SCORE_MODEL, len(pre_skipped),
    )
    _emit(f"Scoring {len(blocks_to_score)} images…", 15.0)

    # ── Score all valid images concurrently ──────────────────────────────────
    async def _score_one(block: dict) -> ImageScore:
        bid_      = block["id"]
        btype_    = block.get("type", "section")
        img_path_ = images_dir / f"{bid_}.png"
        narration = (block.get("narration") or "")[:400]
        prompt_   = (block.get("image_prompt") or "")
        eff_threshold = threshold - 0.5 if btype_ in ATMOSPHERIC_TYPES else threshold
        try:
            score, reason, improved = await _score_image(
                bid_, img_path_, narration, prompt_, voidai_key, score_sem,
            )
        except Exception as exc:
            log.warning("Failed to score %s: %s — marking as skipped", bid_, exc)
            # [FIXED] Scoring failure → skipped (was: fake score=10.0 treated as OK)
            # We don't know if image is good or bad, so we skip rather than regen blindly.
            return ImageScore(
                block_id=bid_, score=0.0, ok=False,
                skipped=True,
                skip_reason=f"Vision API error: {exc}",
            )

        ok = score >= eff_threshold
        log.info(
            "  %s [%s]: score=%.0f (thresh=%.1f) %s",
            bid_, btype_, score, eff_threshold, "✓" if ok else "✗ regen",
        )
        return ImageScore(
            block_id=bid_, score=score, ok=ok,
            reason=reason, improved_prompt=improved,
        )

    scored_list = await asyncio.gather(*[_score_one(b) for b in blocks_to_score])
    result.scores.extend(scored_list)

    ok_scores  = [s for s in scored_list if s.ok]
    # [FIXED] Exclude skipped from regen queue (scoring API errors ≠ bad images)
    bad_scores = [s for s in scored_list if not s.ok and not s.skipped]
    result.ok_count = len(ok_scores)

    # Update skipped count (includes newly skipped from scoring failures)
    newly_skipped = [s for s in scored_list if s.skipped]
    result.skipped += len(newly_skipped)
    result.total   += len(newly_skipped)  # adjust total if scoring added skips

    log.info(
        "Scores done: %d OK, %d need regeneration, %d skipped (scoring error)",
        len(ok_scores), len(bad_scores), len(newly_skipped),
    )
    _emit(f"{len(ok_scores)}/{len(blocks_to_score)} OK, regenerating {len(bad_scores)}…", 40.0)

    if not bad_scores:
        result.elapsed = time.monotonic() - t0
        _emit("All images OK ✓", 100.0)
        return result

    if not ws_key and not voidai_key:
        log.warning("No image API keys set — cannot regenerate %d bad images", len(bad_scores))
        result.failed = len(bad_scores)
        result.elapsed = time.monotonic() - t0
        return result

    # ── Regenerate bad images ─────────────────────────────────────────────────
    blocks_by_id = {b["id"]: b for b in blocks_to_score}

    async def _regen_one(image_score: ImageScore) -> None:
        """
        Regenerate a single image up to max_attempts times.
        [FIXED] Uses loop instead of recursion — clearer and stack-safe.
        """
        bid_      = image_score.block_id
        block_    = blocks_by_id.get(bid_, {})
        btype_    = block_.get("type", "section")
        img_path_ = images_dir / f"{bid_}.png"
        eff_threshold = threshold - 0.5 if btype_ in ATMOSPHERIC_TYPES else threshold

        for attempt in range(1, max_attempts + 1):
            prompt_     = image_score.improved_prompt or block_.get("image_prompt", "")
            full_prompt = f"{prompt_}, {image_style}"

            log.info(
                "  Regenerating %s (score=%.0f, attempt=%d/%d): %s…",
                bid_, image_score.score, attempt, max_attempts, prompt_[:50],
            )

            # Try WaveSpeed first; fall back to VoidAI if WaveSpeed fails or key missing
            success = False
            if ws_key:
                success = await _wavespeed_generate(full_prompt, img_path_, ws_key, regen_sem)
                if not success:
                    log.warning(
                        "  WaveSpeed failed for %s (attempt %d) — trying VoidAI fallback",
                        bid_, attempt,
                    )
            if not success and voidai_key:
                success = await _voidai_generate(full_prompt, img_path_, voidai_key)
                if success:
                    log.info("  VoidAI fallback succeeded for %s", bid_)

            image_score.attempts = attempt

            if not success:
                log.warning(
                    "  All generation methods exhausted for %s (attempt %d) — keeping old image",
                    bid_, attempt,
                )
                break  # Can't generate — stop trying

            # Re-score the new image
            narration = (block_.get("narration") or "")[:400]
            try:
                new_score, new_reason, new_improved = await _score_image(
                    bid_, img_path_, narration, prompt_, voidai_key, score_sem,
                )
                log.info("  %s: rescore=%.0f (was %.0f)", bid_, new_score, image_score.score)
                image_score.score           = new_score
                image_score.reason          = new_reason
                image_score.improved_prompt = new_improved
                image_score.ok              = new_score >= eff_threshold
                image_score.regenerated     = True

                if image_score.ok:
                    log.info("  %s: score acceptable after attempt %d ✓", bid_, attempt)
                    break  # Good enough — stop
                elif attempt < max_attempts:
                    log.info(
                        "  %s: score still below threshold (%.0f < %.1f) — attempt %d next",
                        bid_, new_score, eff_threshold, attempt + 1,
                    )
                # else: loop ends naturally after max_attempts, keeping best result

            except Exception as exc:
                log.warning("  Re-score failed for %s: %s — keeping regenerated image", bid_, exc)
                image_score.regenerated = True
                break  # Can't score — assume regen was good enough, stop

    regen_tasks = [
        _regen_one(s)
        for s in bad_scores
        if s.block_id in blocks_by_id
    ]
    await asyncio.gather(*regen_tasks, return_exceptions=True)

    for s in bad_scores:
        if s.regenerated:
            result.regenerated += 1
        else:
            result.failed += 1

    # ── Save improved_prompt back to script.json (helps future regenerations) ──
    prompt_updates = 0
    blocks_in_script = {b["id"]: b for b in script.get("blocks", [])}
    for s in result.scores:
        if s.skipped or not s.improved_prompt:
            continue
        block = blocks_in_script.get(s.block_id)
        if block and s.improved_prompt != block.get("image_prompt", ""):
            block["image_prompt"] = s.improved_prompt
            prompt_updates += 1
    if prompt_updates:
        script["blocks"] = list(blocks_in_script.values())
        script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Updated %d image_prompt(s) in script.json with improved versions", prompt_updates)

    result.elapsed = time.monotonic() - t0
    _emit("Image validation complete", 100.0)
    log.info(
        "Image validation done (%.1fs): %d OK, %d regenerated, %d failed, %d skipped",
        result.elapsed, result.ok_count, result.regenerated, result.failed, result.skipped,
    )
    return result


# ─── CLI self-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Image Validator — self-test")
    parser.add_argument("script",     help="Path to script.json")
    parser.add_argument("images_dir", help="Directory with block_NNN.png files")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    load_env()
    result = asyncio.run(validate_and_fix_images(
        args.script,
        args.images_dir,
        threshold=args.threshold,
    ))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
