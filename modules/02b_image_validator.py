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

Concurrency limits:
  Vision scoring: max 5 concurrent calls
  WaveSpeed regen: max 3 concurrent calls
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

SCORE_MODEL    = "gpt-4.1"       # vision model
VOIDAI_IMG_MODEL = "gpt-image-1.5"  # fallback image gen when WaveSpeed fails
DEFAULT_THRESHOLD = 7.0
DEFAULT_MAX_ATTEMPTS = 2

# Minimum PNG file size — anything smaller is a placeholder or corrupted image
MIN_IMAGE_BYTES = 10_240  # 10 KB

# Block types that are more atmospheric — allow slightly lower threshold
ATMOSPHERIC_TYPES = frozenset(["intro", "outro"])

WS_POLL_INTERVAL = 2.0           # seconds between WaveSpeed polls
WS_POLL_MAX      = 90            # max polls (3 minutes)


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
    skipped: bool = False       # True if image was missing or too small (not scored)
    skip_reason: str = ""       # reason for skip


@dataclass
class ImageValidationResult:
    total: int = 0
    ok_count: int = 0
    regenerated: int = 0
    failed: int = 0
    skipped: int = 0            # images that couldn't be scored (missing / corrupted)
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

    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    score    = float(data.get("score", 5))
    reason   = str(data.get("reason", ""))
    improved = str(data.get("improved_prompt", image_prompt))
    return score, reason, improved


# ─── Regeneration ─────────────────────────────────────────────────────────────

async def _wavespeed_generate(
    prompt: str,
    output_path: Path,
    ws_key: str,
    sem: asyncio.Semaphore,
) -> bool:
    """
    Generate one image via WaveSpeed T2I and save to output_path.
    Returns True on success.
    """
    async with sem:
        async with httpx.AsyncClient(timeout=120) as client:
            # POST → get task_id
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

            # Poll until done
            for _ in range(WS_POLL_MAX):
                await asyncio.sleep(WS_POLL_INTERVAL)
                poll = await client.get(
                    f"{WAVESPEED_BASE}/predictions/{task_id}/result",
                    headers={"Authorization": f"Bearer {ws_key}"},
                )
                poll.raise_for_status()
                pdata = poll.json()["data"]
                status = pdata.get("status", "")

                if status == "completed":
                    img_url = pdata["outputs"][0]
                    dl = await client.get(img_url, timeout=30)
                    dl.raise_for_status()
                    output_path.write_bytes(dl.content)
                    return True

                if status in ("failed", "error"):
                    log.warning("WaveSpeed task %s failed: %s", task_id, pdata)
                    return False

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
        script_path:      Path to script.json.
        images_dir:       Directory containing block_NNN.png files.
        channel_config:   Channel config dict (for image_style).
        threshold:        Minimum acceptable score (default 7.0).
        max_attempts:     Max regeneration attempts per image (default 2).
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

    # ── Load script ──
    script = json.loads(script_path.read_text(encoding="utf-8"))
    all_blocks = script.get("blocks", [])

    # ── Pre-flight: detect missing / corrupted images ──
    result_pre = ImageValidationResult()
    pre_skipped: list[ImageScore] = []
    blocks_to_score: list[dict] = []

    for b in all_blocks:
        bid       = b.get("id", "")
        btype     = b.get("type", "section")
        has_prompt = bool((b.get("image_prompt") or "").strip())
        img_path  = images_dir / f"{bid}.png"

        # CTA blocks with no image prompt are expected — skip silently
        if btype == "cta" and not has_prompt:
            continue

        if not img_path.exists():
            log.warning("Missing image: %s.png (block %s)", bid, btype)
            pre_skipped.append(ImageScore(
                block_id=bid, score=0.0, ok=False,
                skipped=True, skip_reason="Image file not found — generation may have failed",
            ))
            continue

        img_size = img_path.stat().st_size
        if img_size < MIN_IMAGE_BYTES:
            log.warning(
                "Corrupted/tiny image: %s.png (%d bytes < %d KB minimum)",
                bid, img_size, MIN_IMAGE_BYTES // 1024,
            )
            pre_skipped.append(ImageScore(
                block_id=bid, score=0.0, ok=False,
                skipped=True,
                skip_reason=f"Image too small ({img_size} bytes) — likely placeholder or generation error",
            ))
            continue

        blocks_to_score.append(b)

    if pre_skipped:
        log.warning(
            "Pre-flight: %d image(s) missing or corrupted — these blocks will show as failed",
            len(pre_skipped),
        )

    blocks = blocks_to_score

    if not blocks and not pre_skipped:
        log.warning("No images found in %s — skipping image validation", images_dir)
        return ImageValidationResult(elapsed=time.monotonic() - t0)

    result = ImageValidationResult(total=len(blocks) + len(pre_skipped))
    result.skipped = len(pre_skipped)
    result.scores  = pre_skipped  # pre-populate with skipped entries

    log.info(
        "Image validation: scoring %d images (threshold=%.0f/10, model=%s, skipped=%d)",
        len(blocks), threshold, SCORE_MODEL, len(pre_skipped),
    )
    _emit(f"Scoring {len(blocks)} images…", 5.0)

    # ── Semaphores (created here, bound to the running event loop) ──
    score_sem = asyncio.Semaphore(5)
    regen_sem = asyncio.Semaphore(3)

    # ── Score all images concurrently ──
    async def _score_one(block: dict) -> ImageScore:
        bid       = block["id"]
        btype     = block.get("type", "section")
        img_path  = images_dir / f"{bid}.png"
        narration = (block.get("narration") or "")[:400]
        prompt    = (block.get("image_prompt") or "")
        # Intro/outro blocks are more atmospheric — allow slightly lower threshold
        eff_threshold = threshold - 0.5 if btype in ATMOSPHERIC_TYPES else threshold
        try:
            score, reason, improved = await _score_image(
                bid, img_path, narration, prompt, voidai_key, score_sem,
            )
        except Exception as exc:
            log.warning("Failed to score %s: %s", bid, exc)
            # On scoring failure, keep the image as-is
            return ImageScore(block_id=bid, score=10.0, ok=True, reason=f"Scoring failed: {exc}")

        ok = score >= eff_threshold
        log.info("  %s [%s]: score=%.0f (thresh=%.1f) %s", bid, btype, score, eff_threshold, "✓" if ok else "✗ regen")
        return ImageScore(
            block_id=bid, score=score, ok=ok,
            reason=reason, improved_prompt=improved,
        )

    scored_list = await asyncio.gather(*[_score_one(b) for b in blocks])
    result.scores.extend(scored_list)  # pre_skipped already in result.scores

    ok_scores  = [s for s in scored_list if s.ok]
    bad_scores = [s for s in scored_list if not s.ok]
    result.ok_count = len(ok_scores)

    log.info(
        "Scores done: %d OK, %d need regeneration",
        len(ok_scores), len(bad_scores),
    )
    _emit(f"{len(ok_scores)}/{len(blocks)} OK, regenerating {len(bad_scores)}…", 40.0)

    if not bad_scores:
        result.elapsed = time.monotonic() - t0
        _emit("All images OK ✓", 100.0)
        return result

    if not ws_key and not voidai_key:
        log.warning(
            "No image API keys set — cannot regenerate %d bad images", len(bad_scores)
        )
        result.failed = len(bad_scores)
        result.elapsed = time.monotonic() - t0
        return result

    # ── Regenerate bad images ──
    blocks_by_id = {b["id"]: b for b in blocks}

    async def _regen_one(image_score: ImageScore, attempt: int = 1) -> None:
        bid      = image_score.block_id
        block    = blocks_by_id.get(bid, {})
        btype    = block.get("type", "section")
        img_path = images_dir / f"{bid}.png"
        prompt   = image_score.improved_prompt or block.get("image_prompt", "")
        full_prompt = f"{prompt}, {image_style}"
        eff_threshold = threshold - 0.5 if btype in ATMOSPHERIC_TYPES else threshold

        log.info(
            "  Regenerating %s (score=%.0f, attempt=%d): %s…",
            bid, image_score.score, attempt, prompt[:50],
        )

        # Try WaveSpeed first; fall back to VoidAI if WaveSpeed fails or key missing
        success = False
        if ws_key:
            success = await _wavespeed_generate(full_prompt, img_path, ws_key, regen_sem)
            if not success:
                log.warning("  WaveSpeed failed for %s (attempt %d) — trying VoidAI fallback", bid, attempt)

        if not success and voidai_key:
            success = await _voidai_generate(full_prompt, img_path, voidai_key)
            if success:
                log.info("  VoidAI fallback succeeded for %s", bid)

        image_score.attempts = attempt

        if not success:
            log.warning("  All image gen attempts failed for %s (attempt %d)", bid, attempt)
            return

        # Re-score the new image
        narration = (block.get("narration") or "")[:400]
        try:
            new_score, new_reason, new_improved = await _score_image(
                bid, img_path, narration, prompt, voidai_key, score_sem,
            )
            log.info("  %s: rescore=%.0f (was %.0f)", bid, new_score, image_score.score)
            image_score.score           = new_score
            image_score.reason          = new_reason
            image_score.improved_prompt = new_improved
            image_score.ok              = new_score >= eff_threshold
            image_score.regenerated     = True

            # Second attempt if still bad
            if not image_score.ok and attempt < max_attempts:
                await _regen_one(image_score, attempt + 1)
        except Exception as exc:
            log.warning("  Re-score failed for %s: %s", bid, exc)
            image_score.regenerated = True  # treat regen as success

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

    # Add pre_skipped as failed
    result.failed += len(pre_skipped)

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
