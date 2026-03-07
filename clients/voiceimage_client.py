"""
VideoForge — VoiceImage client (voiceapi.csv666.ru image generation).

Replaces BetaImage (betatestru.csv666.ru — permanently shut down).
Same developer, same Telegram-based auth, same VOICEAPI_KEY.

API Flow (v2 async):
    1. POST /api/v2/image/generate  JSON body: prompt, aspect_ratio, ...
                           → {"task_id": int, "status": "queued", ...}
    2. GET  /api/v2/image/tasks/{task_id}/status
                           → {"status": "queued"|"in_progress"|"completed"|"failed"|"cancelled", ...}
    3. GET  /api/v2/image/tasks/{task_id}/download?format=png
                           → PNG bytes (direct binary download)

Resolutions (aspect_ratio → actual):
    16:9  → 1360×768   (default, YouTube landscape)
    9:16  → 768×1360   (Shorts)
    1:1   → 1024×1024  (square)

Generation modes:
    fast    → ~3-5s/image  (4 steps)
    quality → slower, higher detail (8 steps, default)

Auth: X-API-Key header with VOICEAPI_KEY env var.
Alternative domain: voiceapiru.csv666.ru (VOICEIMAGE_BASE_URL env var).

Usage:
    async with VoiceImageClient() as client:
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

log = setup_logging("voiceimage")

# ─── Constants ────────────────────────────────────────────────────────────────

VOICEIMAGE_BASE_URL = "https://voiceapi.csv666.ru"

# API endpoints
GENERATE_ENDPOINT = "/api/v2/image/generate"
STATUS_ENDPOINT   = "/api/v2/image/tasks/{task_id}/status"
DOWNLOAD_ENDPOINT = "/api/v2/image/tasks/{task_id}/download"
BALANCE_ENDPOINT  = "/balance"

# Default generation params
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_MODE         = "quality"

MAX_CONCURRENT   = 3
REQUEST_TIMEOUT  = 60.0
DOWNLOAD_TIMEOUT = 120.0
POLL_INTERVAL    = 3.0
POLL_TIMEOUT     = 300.0
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 5.0

_DONE_STATUSES  = {"completed"}
_ERROR_STATUSES = {"failed", "cancelled"}


# ─── VoiceImage Client ────────────────────────────────────────────────────────

class VoiceImageClient:
    """
    Async client for VoiceAPI image generation (voiceapi.csv666.ru).

    Drop-in replacement for BetaImageClient — same interface, new API endpoints.
    Uses VOICEAPI_KEY for auth (same key as TTS).

    Interface matches WaveSpeedClient.generate_text2img() for drop-in usage.

    Env vars:
        VOICEAPI_KEY         — required (format: <telegram_id>:<hex_token>)
        VOICEIMAGE_BASE_URL  — optional override (default: https://voiceapi.csv666.ru)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        mode: str = DEFAULT_MODE,
    ) -> None:
        load_env()
        self.api_key = api_key or require_env("VOICEAPI_KEY")
        self.base_url = (base_url or os.getenv("VOICEIMAGE_BASE_URL", VOICEIMAGE_BASE_URL)).rstrip("/")
        self.aspect_ratio = aspect_ratio
        self.mode = mode
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._session_images: int = 0
        self._http: httpx.AsyncClient | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "VoiceImageClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "VoiceImageClient not opened. "
                "Use 'async with VoiceImageClient()' or call await client.open() first."
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
        """POST /api/v2/image/generate JSON → task_id (int)."""
        url = self.base_url + GENERATE_ENDPOINT
        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "generation_mode": mode,
            "prompt_upsampling": prompt_upsampling,
            "num_images": 1,
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client().post(
                    url,
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("task_id") or data.get("id")
                if not task_id:
                    raise RuntimeError(f"VoiceImage: no task_id in response: {data}")
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
                    log.error("VoiceImage client error %d: %s", status, body)
                    raise
                else:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    else:
                        raise

            except (httpx.ConnectError, httpx.TimeoutException, RuntimeError) as exc:
                if attempt < MAX_RETRIES:
                    log.warning("VoiceImage error on create: %s. Retry %d/%d", exc, attempt, MAX_RETRIES)
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                else:
                    raise

        raise RuntimeError(f"VoiceImage: failed to create task after {MAX_RETRIES} retries")

    async def _poll_status(self, task_id: int) -> None:
        """Poll GET /api/v2/image/tasks/{task_id}/status until terminal state."""
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
                    exec_ms = data.get("execution_time_ms")
                    log.debug("Task %s: completed exec=%s progress=%s", task_id, exec_ms, progress)
                    return

                if status in _ERROR_STATUSES:
                    error = data.get("error_message") or data.get("error") or status
                    raise RuntimeError(f"VoiceImage task {task_id} {status}: {error}")

                log.debug(
                    "Task %s: status=%s progress=%s — polling in %.1fs",
                    task_id, status, progress, POLL_INTERVAL,
                )
            except RuntimeError:
                raise
            except Exception as exc:
                log.warning("Poll error for task %s: %s", task_id, exc)

            await asyncio.sleep(POLL_INTERVAL)

        raise RuntimeError(f"VoiceImage task {task_id} timed out after {POLL_TIMEOUT:.0f}s")

    async def _fetch_image(self, task_id: int) -> bytes:
        """GET /api/v2/image/tasks/{task_id}/download?format=png → PNG bytes."""
        url = self.base_url + DOWNLOAD_ENDPOINT.format(task_id=task_id)
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as dl:
            resp = await dl.get(url, params={"format": "png"}, headers=self._headers())
            resp.raise_for_status()
            content = resp.content

        if len(content) < 5_000:
            raise RuntimeError(
                f"VoiceImage task {task_id}: download too small ({len(content)} bytes)"
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
        # Interface compat with WaveSpeedClient (ignored):
        size: str | None = None,
        seed: int | None = None,
        num_inference_steps: int | None = None,
        guidance_scale: float | None = None,
    ) -> str:
        """
        Generate an image from a text prompt.

        Args:
            prompt:            Text description of the desired image.
            aspect_ratio:      "16:9" (default), "9:16", "1:1", etc.
            mode:              "fast" or "quality" (default).
            prompt_upsampling: Let the API improve the prompt (default True).
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
            "voiceimage done: task=%s elapsed=%.1fs size=%dKB session=%d",
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
        return self._session_images

    # ─── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Connectivity test — calls /balance to verify auth."""
        try:
            resp = await self._client().get(self.base_url + BALANCE_ENDPOINT, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            balance = data.get("balance", "?")
            log.info("VoiceImage auth OK: balance=%s", balance)
            return {
                "ok": True,
                "balance": balance,
                "provider": "voiceimage",
                "base_url": self.base_url,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": "voiceimage"}


# ─── CLI self-test ────────────────────────────────────────────────────────────

async def _self_test() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="VoiceImage client — self-test")
    parser.add_argument("--prompt", default="A golden ancient temple at sunset, cinematic", help="Image prompt")
    parser.add_argument("--output", default="projects/test_voiceimage.png", help="Output PNG path")
    parser.add_argument("--aspect", default=DEFAULT_ASPECT_RATIO)
    parser.add_argument("--mode", default=DEFAULT_MODE, choices=["fast", "quality"])
    parser.add_argument("--auth-only", action="store_true")
    args = parser.parse_args()

    log.info("VoiceImage client self-test starting...")
    log.info("Base URL: %s", VOICEIMAGE_BASE_URL)
    log.info("API key: %s...", (os.getenv("VOICEAPI_KEY") or "NOT SET")[:20])

    async with VoiceImageClient(aspect_ratio=args.aspect, mode=args.mode) as client:
        health = await client.health_check()
        log.info("Auth: %s", health)

        if args.auth_only:
            return

        log.info("--- Test: generate_text2img ---")
        result = await client.generate_text2img(args.prompt, output_path=args.output)
        log.info("Result: %s", result)
        log.info("Session: %d image(s)", client.session_images)
        log.info("voiceimage_client.py self-test OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
