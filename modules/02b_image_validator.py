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
WS_T2I_PATH    = "/wavespeed-ai/flux-dev-ultra-fast"  # z-image/turbo deprecated

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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _img_path_for(images_dir: Path, bid: str, idx: int) -> Path:
    """Return the file path for image index `idx` of block `bid`.
    idx=0 → bid.png  (primary)
    idx>0 → bid_N.png (secondary, matching 02_image_generator.py naming)
    """
    return images_dir / (f"{bid}.png" if idx == 0 else f"{bid}_{idx}.png")


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ImageScore:
    block_id: str
    score: float
    ok: bool
    image_index: int = 0        # 0 = primary (bid.png), N = secondary (bid_N.png)
    reason: str = ""
    improved_prompt: str = ""
    regenerated: bool = False
    attempts: int = 0
    skipped: bool = False       # True if image was missing/corrupted or scoring API failed
    skip_reason: str = ""       # human-readable reason for skip

    @property
    def image_label(self) -> str:
        """Human-readable label: 'block_001' or 'block_001[2]'."""
        return self.block_id if self.image_index == 0 else f"{self.block_id}[{self.image_index}]"


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
                    "image_index": s.image_index,
                    "label": s.image_label,
                    "score": s.score,
                    "ok": s.ok,
                    "reason": s.reason,
                    "improved_prompt": s.improved_prompt,
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
    vision_model: str | None = None,
) -> tuple[float, str, str]:
    """
    Score an image against its narration using a vision model (default: gpt-4.1).
    Returns (score 0-10, reason, improved_prompt).
    """
    model = vision_model or SCORE_MODEL
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
                    "model": model,
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
    img_url: str | None = None
    task_id: str | None = None

    # ── POST to flux-dev-ultra-fast (with sync mode — result may arrive immediately) ──
    try:
        async with sem:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{WAVESPEED_BASE}{WS_T2I_PATH}",
                    headers={
                        "Authorization": f"Bearer {ws_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "prompt": prompt,
                        "size": "1024x576",
                        "num_inference_steps": 28,
                        "guidance_scale": 3.5,
                        "output_format": "png",
                        "enable_sync_mode": True,
                    },
                )
                resp.raise_for_status()
                rdata = resp.json()
                # Sync mode: outputs may be in direct response
                data    = rdata.get("data", {})
                outputs = data.get("outputs") or rdata.get("outputs", [])
                if outputs:
                    img_url = outputs[0]
                    log.debug("WaveSpeed regen: sync response received")
                else:
                    task_id = data.get("id") or data.get("task_id") or rdata.get("id")
    except Exception as exc:
        log.warning("WaveSpeed regen POST failed: %s", exc)
        return False

    # ── Poll if task is async (fallback) ──
    if img_url is None and task_id:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for _ in range(WS_POLL_MAX):
                    await asyncio.sleep(WS_POLL_INTERVAL)
                    poll = await client.get(
                        f"{WAVESPEED_BASE}/predictions/{task_id}/result",
                        headers={"Authorization": f"Bearer {ws_key}"},
                    )
                    poll.raise_for_status()
                    pdata  = poll.json().get("data", {})
                    status = pdata.get("status", "")
                    if status == "completed":
                        outs = pdata.get("outputs", [])
                        if outs:
                            img_url = outs[0]
                        break
                    if status in ("failed", "error"):
                        log.warning("WaveSpeed regen task %s %s: %s", task_id, status, pdata)
                        return False
        except Exception as exc:
            log.warning("WaveSpeed regen poll failed (task=%s): %s", task_id, exc)
            return False

    if not img_url:
        log.warning("WaveSpeed regen: no image URL obtained (task=%s)", task_id)
        return False

    # ── Download and verify ──
    try:
        async with httpx.AsyncClient(timeout=60) as dl:
            dresp = await dl.get(img_url)
            dresp.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(dresp.content)
        saved_size = output_path.stat().st_size
        if saved_size < MIN_IMAGE_BYTES:
            log.warning("WaveSpeed regen download too small (%d bytes) — discarding", saved_size)
            output_path.unlink(missing_ok=True)
            return False
        log.debug("WaveSpeed regen saved: %s (%d bytes)", output_path.name, saved_size)
        return True
    except Exception as exc:
        log.warning("WaveSpeed regen download failed: %s", exc)
        return False


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
    vision_model: str | None = None,  # Override scoring model: "gpt-4.1" (default) | "gpt-4.1-mini"
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
        vision_model:      Vision model override (e.g. "gpt-4.1-mini"). Default: gpt-4.1.

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

    # ── Pre-flight: detect missing / corrupted PRIMARY images ────────────────
    # Checks only bid.png (index 0). Secondary images (bid_1.png, bid_2.png…)
    # are checked per-image inside _score_one and handled as skip if missing.
    # Three buckets:
    #   blocks_to_score    — primary image exists and is valid → score all images
    #   blocks_to_preregen — primary image missing/corrupted BUT has a prompt → regen first
    #   pre_skipped        — primary image missing AND no prompt → skip block
    pre_skipped: list[ImageScore]    = []
    blocks_to_score: list[dict]      = []
    blocks_to_preregen: list[dict]   = []

    for b in all_blocks:
        bid        = b.get("id", "")
        btype      = b.get("type", "section")
        has_prompt = bool((b.get("image_prompt") or "").strip()) or bool(b.get("image_prompts"))
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
            """Generate image for a block using its original image_prompt (or first of image_prompts)."""
            bid_        = block.get("id", "")
            # Use image_prompt (singular) first; fall back to first entry of image_prompts (plural)
            prompt_     = (block.get("image_prompt") or "").strip()
            if not prompt_:
                _prompts_list = block.get("image_prompts") or []
                prompt_ = (_prompts_list[0] or "").strip() if _prompts_list else ""
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

    # Count total images across all blocks_to_score
    _total_images = sum(
        max(len(b.get("image_prompts") or []), 1 if (b.get("image_prompt") or "").strip() else 0)
        for b in blocks_to_score
    )
    _eff_model = vision_model or SCORE_MODEL
    log.info(
        "Image validation: scoring %d images across %d blocks (threshold=%.0f/10, model=%s, skipped=%d)",
        _total_images, len(blocks_to_score), threshold, _eff_model, len(pre_skipped),
    )
    _emit(f"Scoring {_total_images} images across {len(blocks_to_score)} blocks…", 15.0)

    # ── Score all valid images concurrently (ALL image_prompts per block) ────
    async def _score_one(block: dict) -> list[ImageScore]:
        """Score every image in a block (primary + all secondaries).

        Returns a list — one ImageScore per image_prompt entry.
        """
        bid_      = block["id"]
        btype_    = block.get("type", "section")
        narration = (block.get("narration") or "")[:400]
        eff_threshold = threshold - 0.5 if btype_ in ATMOSPHERIC_TYPES else threshold

        # Collect all (index, prompt) pairs for this block.
        # image_prompts[0] == image_prompt (primary); image_prompts[N] → bid_N.png
        raw_prompts: list[str] = block.get("image_prompts") or []
        if not raw_prompts:
            primary = (block.get("image_prompt") or "").strip()
            raw_prompts = [primary] if primary else []
        if not raw_prompts:
            return []  # CTA/outro block with no images — nothing to score

        block_scores: list[ImageScore] = []
        for idx, prompt_ in enumerate(raw_prompts):
            img_path_ = _img_path_for(images_dir, bid_, idx)

            if not img_path_.exists() or img_path_.stat().st_size < MIN_IMAGE_BYTES:
                label = f"{bid_}[{idx}]" if idx > 0 else bid_
                log.warning("Missing/corrupted image: %s — marked as skipped", img_path_.name)
                block_scores.append(ImageScore(
                    block_id=bid_, image_index=idx, score=0.0, ok=False,
                    skipped=True, skip_reason=f"Image file missing or corrupted",
                ))
                continue

            try:
                score, reason, improved = await _score_image(
                    bid_, img_path_, narration, prompt_, voidai_key, score_sem,
                    vision_model=vision_model,
                )
            except Exception as exc:
                log.warning("Failed to score %s: %s — marking as skipped", img_path_.name, exc)
                block_scores.append(ImageScore(
                    block_id=bid_, image_index=idx, score=0.0, ok=False,
                    skipped=True, skip_reason=f"Vision API error: {exc}",
                ))
                continue

            ok    = score >= eff_threshold
            label = f"{bid_}[{idx}]" if idx > 0 else bid_
            log.info(
                "  %s [%s]: score=%.0f (thresh=%.1f) %s",
                label, btype_, score, eff_threshold, "✓" if ok else "✗ regen",
            )
            block_scores.append(ImageScore(
                block_id=bid_, image_index=idx, score=score, ok=ok,
                reason=reason, improved_prompt=improved,
            ))

        return block_scores

    scored_nested_raw = await asyncio.gather(
        *[_score_one(b) for b in blocks_to_score],
        return_exceptions=True,
    )
    # Handle per-block exceptions gracefully — mark whole block as skipped
    scored_nested: list[list[ImageScore]] = []
    for _b, _res in zip(blocks_to_score, scored_nested_raw):
        if isinstance(_res, BaseException):
            _bid = _b.get("id", "?")
            log.warning("_score_one(%s) raised unexpectedly: %s — block skipped", _bid, _res)
            scored_nested.append([ImageScore(
                block_id=_bid, score=0.0, ok=False,
                skipped=True, skip_reason=f"Unexpected scoring error: {_res}",
            )])
        else:
            scored_nested.append(_res)
    # Flatten list[list[ImageScore]] → list[ImageScore]
    scored_list: list[ImageScore] = [s for sublist in scored_nested for s in sublist]
    result.scores.extend(scored_list)
    # Update total to reflect actual image count (not block count)
    result.total = len(result.scores)

    # [FIXED] Exclude skipped from regen queue (scoring API errors ≠ bad images)
    bad_scores = [s for s in scored_list if not s.ok and not s.skipped]

    # Update skipped count (includes newly skipped from scoring failures)
    newly_skipped = [s for s in scored_list if s.skipped]
    result.skipped += len(newly_skipped)
    # NOTE: result.total was already set to len(result.scores) which includes skipped — do NOT add again

    _pre_ok_count = len([s for s in scored_list if s.ok and not s.skipped])
    log.info(
        "Scores done: %d OK, %d need regeneration, %d skipped (scoring error)",
        _pre_ok_count, len(bad_scores), len(newly_skipped),
    )
    _emit(f"{_pre_ok_count}/{len(blocks_to_score)} OK, regenerating {len(bad_scores)}…", 40.0)

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
    # Use all_blocks (not just blocks_to_score) so secondary image regen has full block data
    blocks_by_id = {b["id"]: b for b in all_blocks}

    async def _regen_one(image_score: ImageScore) -> None:
        """
        Regenerate a single image (identified by block_id + image_index) up to max_attempts.
        [FIXED] Uses loop instead of recursion — clearer and stack-safe.
        Handles both primary (index=0 → bid.png) and secondary (index=N → bid_N.png) images.
        """
        bid_   = image_score.block_id
        idx_   = image_score.image_index
        block_ = blocks_by_id.get(bid_, {})
        btype_ = block_.get("type", "section")

        # Resolve the specific prompt for this image index
        prompts_list  = block_.get("image_prompts") or [block_.get("image_prompt", "")]
        base_prompt   = prompts_list[idx_] if idx_ < len(prompts_list) else block_.get("image_prompt", "")

        img_path_     = _img_path_for(images_dir, bid_, idx_)
        eff_threshold = threshold - 0.5 if btype_ in ATMOSPHERIC_TYPES else threshold

        for attempt in range(1, max_attempts + 1):
            prompt_     = image_score.improved_prompt or base_prompt
            full_prompt = f"{prompt_}, {image_style}"

            log.info(
                "  Regenerating %s (score=%.0f, attempt=%d/%d): %s…",
                image_score.image_label, image_score.score, attempt, max_attempts, prompt_[:50],
            )

            # Try WaveSpeed first; fall back to VoidAI if WaveSpeed fails or key missing
            success = False
            if ws_key:
                success = await _wavespeed_generate(full_prompt, img_path_, ws_key, regen_sem)
                if not success:
                    log.warning(
                        "  WaveSpeed failed for %s (attempt %d) — trying VoidAI fallback",
                        image_score.image_label, attempt,
                    )
            if not success and voidai_key:
                success = await _voidai_generate(full_prompt, img_path_, voidai_key)
                if success:
                    log.info("  VoidAI fallback succeeded for %s", image_score.image_label)

            image_score.attempts = attempt

            if not success:
                log.warning(
                    "  All generation methods exhausted for %s (attempt %d) — keeping old image",
                    image_score.image_label, attempt,
                )
                break

            # Re-score the new image
            narration = (block_.get("narration") or "")[:400]
            try:
                new_score, new_reason, new_improved = await _score_image(
                    bid_, img_path_, narration, prompt_, voidai_key, score_sem,
                    vision_model=vision_model,
                )
                log.info(
                    "  %s: rescore=%.0f (was %.0f)",
                    image_score.image_label, new_score, image_score.score,
                )
                image_score.score           = new_score
                image_score.reason          = new_reason
                image_score.improved_prompt = new_improved
                image_score.ok              = new_score >= eff_threshold
                image_score.regenerated     = True

                if image_score.ok:
                    log.info("  %s: score acceptable after attempt %d ✓", image_score.image_label, attempt)
                    break
                elif attempt < max_attempts:
                    log.info(
                        "  %s: score still below threshold (%.0f < %.1f) — attempt %d next",
                        image_score.image_label, new_score, eff_threshold, attempt + 1,
                    )

            except Exception as exc:
                log.warning(
                    "  Re-score failed for %s: %s — keeping regenerated image",
                    image_score.image_label, exc,
                )
                image_score.regenerated = True
                break

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

    # Recalculate ok_count AFTER regen (loop mutates .ok in-place on ImageScore objects)
    result.ok_count = sum(1 for s in result.scores if s.ok and not s.skipped)

    # ── Save improved_prompt back to script.json (helps future regenerations) ──
    # Updates both image_prompts[idx] (for the specific image) and image_prompt (primary only)
    prompt_updates = 0
    blocks_in_script = {b["id"]: b for b in script.get("blocks", [])}
    for s in result.scores:
        if s.skipped or not s.improved_prompt:
            continue
        block = blocks_in_script.get(s.block_id)
        if not block:
            continue
        prompts_list = block.get("image_prompts") or []
        if s.image_index < len(prompts_list):
            if s.improved_prompt != prompts_list[s.image_index]:
                prompts_list[s.image_index] = s.improved_prompt
                block["image_prompts"] = prompts_list
                if s.image_index == 0:
                    block["image_prompt"] = s.improved_prompt
                prompt_updates += 1
        elif s.image_index == 0 and s.improved_prompt != block.get("image_prompt", ""):
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
