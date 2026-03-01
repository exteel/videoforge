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
DEFAULT_THRESHOLD = 7.0
DEFAULT_MAX_ATTEMPTS = 2

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


@dataclass
class ImageValidationResult:
    total: int = 0
    ok_count: int = 0
    regenerated: int = 0
    failed: int = 0
    scores: list[ImageScore] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok_count,
            "regenerated": self.regenerated,
            "failed": self.failed,
            "elapsed": round(self.elapsed, 2),
            "scores": [
                {
                    "block_id": s.block_id,
                    "score": s.score,
                    "ok": s.ok,
                    "reason": s.reason,
                    "regenerated": s.regenerated,
                    "attempts": s.attempts,
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

    # ── Collect blocks that have generated images ──
    script = json.loads(script_path.read_text(encoding="utf-8"))
    blocks = [
        b for b in script.get("blocks", [])
        if (images_dir / f"{b['id']}.png").exists()
    ]

    if not blocks:
        log.warning("No images found in %s — skipping image validation", images_dir)
        return ImageValidationResult(elapsed=time.monotonic() - t0)

    result = ImageValidationResult(total=len(blocks))
    log.info(
        "Image validation: scoring %d images (threshold=%.0f/10, model=%s)",
        len(blocks), threshold, SCORE_MODEL,
    )
    _emit(f"Scoring {len(blocks)} images…", 5.0)

    # ── Semaphores (created here, bound to the running event loop) ──
    score_sem = asyncio.Semaphore(5)
    regen_sem = asyncio.Semaphore(3)

    # ── Score all images concurrently ──
    async def _score_one(block: dict) -> ImageScore:
        bid       = block["id"]
        img_path  = images_dir / f"{bid}.png"
        narration = (block.get("narration") or "")[:400]
        prompt    = (block.get("image_prompt") or "")
        try:
            score, reason, improved = await _score_image(
                bid, img_path, narration, prompt, voidai_key, score_sem,
            )
        except Exception as exc:
            log.warning("Failed to score %s: %s", bid, exc)
            # On scoring failure, keep the image
            return ImageScore(block_id=bid, score=10.0, ok=True, reason=f"Scoring failed: {exc}")

        log.info("  %s: score=%.0f %s", bid, score, "✓" if score >= threshold else "✗ regen")
        return ImageScore(
            block_id=bid, score=score, ok=score >= threshold,
            reason=reason, improved_prompt=improved,
        )

    scored_list = await asyncio.gather(*[_score_one(b) for b in blocks])
    result.scores = list(scored_list)

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

    if not ws_key:
        log.warning(
            "WAVESPEED_API_KEY not set — cannot regenerate %d bad images", len(bad_scores)
        )
        result.failed = len(bad_scores)
        result.elapsed = time.monotonic() - t0
        return result

    # ── Regenerate bad images ──
    blocks_by_id = {b["id"]: b for b in blocks}

    async def _regen_one(image_score: ImageScore, attempt: int = 1) -> None:
        bid      = image_score.block_id
        block    = blocks_by_id.get(bid, {})
        img_path = images_dir / f"{bid}.png"
        prompt   = image_score.improved_prompt or block.get("image_prompt", "")
        full_prompt = f"{prompt}, {image_style}"

        log.info(
            "  Regenerating %s (score=%.0f, attempt=%d): %s…",
            bid, image_score.score, attempt, prompt[:50],
        )
        success = await _wavespeed_generate(full_prompt, img_path, ws_key, regen_sem)

        if not success:
            log.warning("  WaveSpeed failed for %s (attempt %d)", bid, attempt)
            image_score.attempts = attempt
            return

        image_score.attempts = attempt

        # Re-score the new image
        narration = (block.get("narration") or "")[:400]
        try:
            new_score, new_reason, new_improved = await _score_image(
                bid, img_path, narration, prompt, voidai_key, score_sem,
            )
            log.info("  %s: rescore=%.0f (was %.0f)", bid, new_score, image_score.score)
            image_score.score          = new_score
            image_score.reason         = new_reason
            image_score.improved_prompt = new_improved
            image_score.ok             = new_score >= threshold
            image_score.regenerated    = True

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

    result.elapsed = time.monotonic() - t0
    _emit("Image validation complete", 100.0)
    log.info(
        "Image validation done (%.1fs): %d OK, %d regenerated, %d failed",
        result.elapsed, result.ok_count, result.regenerated, result.failed,
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
