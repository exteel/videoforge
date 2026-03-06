"""
VideoForge — BetaImage client (betatestru.csv666.ru image generation).

Task-based async image generation via betatestru.csv666.ru
(same Telegram-based developer as VoiceAPI; different API structure).

API Flow:
    1. POST /api/generate  multipart/form-data: prompt, aspect_ratio, ...
                           → {"task_id": int, "status": "queued", ...}
    2. GET  /api/tasks/{task_id}/status
                           → {"status": "queued"|"in_progress"|"completed"|"failed"|"cancelled", ...}
    3. GET  /api/tasks/{task_id}/result
                           → {"image_url": "https://s3.twcstorage.ru/...", ...}
    4. GET  <image_url>    → PNG bytes (S3 pre-signed URL, expires in ~1h)

Key differences vs voiceapi.csv666.ru image v2:
    - Endpoint: /api/generate (not /api/v2/image/generate)
    - Request: multipart/form-data (not JSON)
    - Result: image_url is a real S3 URL (not null — no /download endpoint needed)
    - Auth key: BETATEST_KEY env var (different from VOICEAPI_KEY)
    - Generation time: ~3-5s per image

Resolutions (aspect_ratio → actual):
    16:9  → 1360×768   (default, YouTube landscape)
    9:16  → 768×1360   (Shorts)
    1:1   → 1024×1024  (square)

Generation modes:
    fast    → ~3-5s/image  (default)
    quality → slower, higher detail

Auth: X-API-Key header with BETATEST_KEY env var.

Usage:
    async with BetaImageClient() as client:
        path = await client.generate_text2img(
            "Cinematic golden ruins under crimson sky",
            output_path="images/block_001.png",
        )
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_env, require_env, setup_logging

log = setup_logging("betaimage")

# ─── Constants ────────────────────────────────────────────────────────────────

BETAIMAGE_BASE_URL = "https://betatestru.csv666.ru"

# API endpoints
GENERATE_ENDPOINT = "/api/generate"
STATUS_ENDPOINT   = "/api/tasks/{task_id}/status"
RESULT_ENDPOINT   = "/api/tasks/{task_id}/result"
ME_ENDPOINT       = "/api/me"

# Default generation params
DEFAULT_ASPECT_RATIO = "16:9"      # → 1360×768 (YouTube landscape)
DEFAULT_MODE         = "quality"   # "fast" ~3-5s; "quality" = better results

MAX_CONCURRENT   = 3              # Semaphore — keep low to avoid rate limits
REQUEST_TIMEOUT  = 60.0           # seconds for initial POST (quality mode is slow to enqueue)
DOWNLOAD_TIMEOUT = 120.0          # seconds for S3 download
POLL_INTERVAL    = 3.0            # seconds between status checks
POLL_TIMEOUT     = 300.0          # max seconds to wait per task (quality mode can take 60-90s)
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 5.0            # slightly longer backoff for rate-limit scenarios

# Terminal states
_DONE_STATUSES  = {"completed"}
_ERROR_STATUSES = {"failed", "cancelled"}


# ─── BetaImage Client ─────────────────────────────────────────────────────────

class BetaImageClient:
    """
    Async client for BetaImage API (betatestru.csv666.ru).

    Telegram-based auth, multipart/form-data request, S3 image URLs.
    ~3-5s per image in fast mode, 1360×768 for 16:9 aspect ratio.

    Interface matches WaveSpeedClient.generate_text2img() for drop-in usage.

    Env var: BETATEST_KEY  (format: <telegram_id>:<hex_token>)

    Usage:
        async with BetaImageClient() as client:
            path = await client.generate_text2img(prompt, output_path="out.png")
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        mode: str = DEFAULT_MODE,
    ) -> None:
        load_env()
        self.api_key = api_key or require_env("BETATEST_KEY")
        self.base_url = (base_url or os.getenv("BETAIMAGE_BASE_URL", BETAIMAGE_BASE_URL)).rstrip("/")
        self.aspect_ratio = aspect_ratio
        self.mode = mode
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._session_images: int = 0
        self._http: httpx.AsyncClient | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        # Note: NOT using base_url — pass full URLs in every request.
        # base_url combined with the API key format (id:hex) caused httpx to
        # mangle URLs in some configurations. Full URLs are always safe.
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "BetaImageClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "BetaImageClient not opened. "
                "Use 'async with BetaImageClient()' or call await client.open() first."
            )
        return self._http

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    # ─── Task lifecycle ───────────────────────────────────────────────────────

    async def _create_task(
        self,
        prompt: str,
        aspect_ratio: str,
        mode: str,
        prompt_upsampling: bool,
    ) -> int:
        """POST /api/generate multipart/form-data → task_id (int)."""
        url = self.base_url + GENERATE_ENDPOINT
        form_data = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "generation_mode": mode,
            "prompt_upsampling": str(prompt_upsampling),  # must be 'True'/'False' (capital)
            "num_images": "1",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client().post(
                    url,
                    data=form_data,        # multipart/form-data
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("task_id") or data.get("id")
                if not task_id:
                    raise RuntimeError(f"BetaImage: no task_id in response: {data}")
                log.debug("Task created: id=%s (attempt %d/%d)", task_id, attempt, MAX_RETRIES)
                return int(task_id)

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                body = exc.response.text[:300]
                if status == 429:
                    wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    log.warning("Rate limit (429). Wait %.1fs (attempt %d/%d)", wait, attempt, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    if attempt == MAX_RETRIES:
                        raise
                elif 400 <= status < 500:
                    log.error("BetaImage client error %d: %s", status, body)
                    raise
                else:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    else:
                        raise

            except (httpx.ConnectError, httpx.TimeoutException, RuntimeError) as exc:
                if attempt < MAX_RETRIES:
                    log.warning("BetaImage error on create: %s. Retry %d/%d", exc, attempt, MAX_RETRIES)
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                else:
                    raise

        raise RuntimeError(f"BetaImage: failed to create task after {MAX_RETRIES} retries")

    async def _poll_status(self, task_id: int) -> None:
        """
        Poll GET /api/tasks/{task_id}/status until terminal state or timeout.
        States: queued → in_progress → completed | failed | cancelled
        """
        url = self.base_url + STATUS_ENDPOINT.format(task_id=task_id)
        deadline = time.monotonic() + POLL_TIMEOUT

        while time.monotonic() < deadline:
            try:
                resp = await self._client().get(url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                status = (data.get("status") or "").lower()
                progress = data.get("progress")

                if status in _DONE_STATUSES:
                    exec_ms = data.get("execution_time_ms") or data.get("generation_total_seconds")
                    log.debug("Task %s: completed exec=%s progress=%s", task_id, exec_ms, progress)
                    return

                if status in _ERROR_STATUSES:
                    error = data.get("error_message") or data.get("error") or status
                    raise RuntimeError(f"BetaImage task {task_id} {status}: {error}")

                log.debug(
                    "Task %s: status=%s progress=%s — polling in %.1fs",
                    task_id, status, progress, POLL_INTERVAL,
                )
            except RuntimeError:
                raise
            except Exception as exc:
                log.warning("Poll error for task %s: %s", task_id, exc)

            await asyncio.sleep(POLL_INTERVAL)

        raise RuntimeError(f"BetaImage task {task_id} timed out after {POLL_TIMEOUT:.0f}s")

    async def _fetch_image(self, task_id: int) -> bytes:
        """
        GET /api/tasks/{task_id}/result → image_url (S3 pre-signed)
        Then download PNG bytes from S3.
        """
        url = self.base_url + RESULT_ENDPOINT.format(task_id=task_id)
        resp = await self._client().get(url, headers=self._headers())
        resp.raise_for_status()

        data = resp.json()
        image_url = data.get("image_url") or (data.get("image_urls") or [None])[0]
        if not image_url:
            raise RuntimeError(f"BetaImage task {task_id}: no image_url in result: {data}")

        log.debug("Task %s: downloading from S3 (%s...)", task_id, image_url[:60])

        # Download from S3 (no auth needed — pre-signed URL)
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as dl:
            img_resp = await dl.get(image_url)
            img_resp.raise_for_status()
            content = img_resp.content

        if len(content) < 5_000:
            raise RuntimeError(
                f"BetaImage task {task_id}: download too small ({len(content)} bytes)"
            )

        log.debug("Task %s: downloaded %d bytes", task_id, len(content))
        return content

    # ─── Core generation ──────────────────────────────────────────────────────

    async def generate_text2img(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        mode: str | None = None,
        prompt_upsampling: bool = True,
        output_path: str | Path | None = None,
        # Kept for interface compat with WaveSpeedClient (ignored):
        size: str | None = None,
        seed: int | None = None,
        num_inference_steps: int | None = None,
        guidance_scale: float | None = None,
    ) -> str:
        """
        Generate an image from a text prompt.

        Args:
            prompt:            Text description of the desired image.
            aspect_ratio:      "16:9" (default, 1360×768), "9:16", "1:1", etc.
            mode:              "fast" (~3-5s, default) or "quality" (slower/better).
            prompt_upsampling: Let the API expand/improve the prompt (default False).
            output_path:       If provided, saves PNG to this path.
            size, seed, num_inference_steps, guidance_scale:
                               Ignored — accepted for WaveSpeedClient interface compat.

        Returns:
            Local file path string if output_path given, else empty string.
        """
        ar = aspect_ratio or self.aspect_ratio
        md = mode or self.mode

        t0 = time.monotonic()
        async with self._semaphore:
            task_id = await self._create_task(prompt, ar, md, prompt_upsampling)
            await self._poll_status(task_id)
            png_bytes = await self._fetch_image(task_id)

        elapsed = time.monotonic() - t0
        self._session_images += 1
        log.info(
            "betaimage done: task=%s elapsed=%.1fs size=%dKB session=%d",
            task_id, elapsed, len(png_bytes) // 1024, self._session_images,
        )

        if output_path:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(png_bytes)
            log.info("Image saved: %s (%d KB)", p, len(png_bytes) // 1024)
            return str(p)

        return ""

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def session_images(self) -> int:
        """Total images generated in this session."""
        return self._session_images

    # ─── Health / auth check ──────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        Connectivity test for `python dev.py check-apis`.
        Calls /api/me to verify auth without generating images.
        Then generates a minimal test image.
        """
        try:
            resp = await self._client().get(self.base_url + ME_ENDPOINT, headers=self._headers())
            resp.raise_for_status()
            me = resp.json()
            username = me.get("username", "?")
            telegram_id = me.get("telegram_id", "?")
            log.info("BetaImage auth OK: @%s (id=%s)", username, telegram_id)
            return {
                "ok": True,
                "username": username,
                "telegram_id": telegram_id,
                "provider": "betatestru",
                "base_url": self.base_url,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": "betatestru"}


# ─── CLI self-test ────────────────────────────────────────────────────────────

async def _self_test() -> None:
    """Run when executed directly: python clients/betaimage_client.py"""
    import argparse

    parser = argparse.ArgumentParser(description="BetaImage client — self-test")
    parser.add_argument(
        "--prompt",
        default=(
            "A massive ancient golden clockwork wheel tilted at an angle resting on the stone floor, "
            "dominating the frame, intricate brass gears and rotating rings, "
            "inside monumental ancient stone ruins with towering pillars, "
            "thin atmospheric mist, dramatic crimson backlight, crimson and gold color palette, "
            "extreme low angle cinematic shot, 35mm lens, "
            "epic sci-fi concept art, volumetric lighting, ultra detailed, photorealistic rendering"
        ),
        help="Image prompt to test",
    )
    parser.add_argument("--output", default="projects/test_betaimage.png", help="Output PNG path")
    parser.add_argument("--aspect", default=DEFAULT_ASPECT_RATIO, help="Aspect ratio (16:9, 9:16, 1:1)")
    parser.add_argument("--mode", default=DEFAULT_MODE, choices=["fast", "quality"], help="Generation mode")
    parser.add_argument("--auth-only", action="store_true", help="Only check auth, skip generation")
    args = parser.parse_args()

    log.info("BetaImage client self-test starting...")
    log.info("Base URL: %s", BETAIMAGE_BASE_URL)
    log.info("API key: %s...", (os.getenv("BETATEST_KEY") or "NOT SET")[:20])

    async with BetaImageClient(aspect_ratio=args.aspect, mode=args.mode) as client:
        health = await client.health_check()
        log.info("Auth: %s", health)

        if args.auth_only:
            return

        log.info("--- Test: generate_text2img ---")
        log.info("Prompt: %s", args.prompt[:100])
        log.info("Aspect: %s | Mode: %s", args.aspect, args.mode)

        result = await client.generate_text2img(
            args.prompt,
            output_path=args.output,
        )

        log.info("Result: %s", result)
        log.info("Session: %d image(s)", client.session_images)
        log.info("betaimage_client.py self-test OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
