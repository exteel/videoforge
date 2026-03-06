"""
VideoForge — WaveSpeed API client.

Primary image generation provider ($0.005/image).
Implements async polling pattern (POST → task_id → poll until completed).

Endpoints:
  text2img  — /wavespeed-ai/z-image/turbo
  img2img   — /wavespeed-ai/z-image-turbo/image-to-image
  upload    — /media/upload/binary
  poll      — /predictions/{task_id}/result

Fallback: if WaveSpeed fails → caller uses VoidAIClient.generate_image()
"""

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

log = setup_logging("wavespeed")

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://api.wavespeed.ai/api/v3"
T2I_ENDPOINT = "/wavespeed-ai/flux-dev-ultra-fast"          # z-image/turbo deprecated (requires image+audio now)
I2I_ENDPOINT = "/wavespeed-ai/z-image-turbo/image-to-image"
UPLOAD_ENDPOINT = "/media/upload/binary"
POLL_ENDPOINT = "/predictions/{task_id}/result"

COST_PER_IMAGE = 0.005   # USD
MAX_CONCURRENT = 5        # Semaphore — CLAUDE.md: max 5 WaveSpeed concurrent
POLL_INTERVAL = 2.0       # Seconds between status polls
MAX_POLLS = 90            # 90 × 2s = 3 minutes max wait
REQUEST_TIMEOUT = 60.0    # Seconds for initial POST
POLL_TIMEOUT = 30.0       # Seconds for each poll GET
MAX_RETRIES = 3
RETRY_BASE_DELAY = 3.0

# WaveSpeed supported sizes (W*H format)
VALID_SIZES = {
    "1024*1024", "1280*720", "1920*1080",
    "720*1280", "1080*1920", "1024*576",
    "576*1024", "1216*832", "832*1216",
}
DEFAULT_SIZE = "1280*720"
DEFAULT_SEED = 42


# ─── WaveSpeed Client ─────────────────────────────────────────────────────────

class WaveSpeedClient:
    """
    Async client for WaveSpeed image generation API.

    Usage:
        async with WaveSpeedClient() as client:
            path = await client.generate_text2img(
                "Cinematic sunset over mountains",
                output_path="images/block_001.png",
            )

    Cost: $0.005 per generated image (tracked in session_cost).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        load_env()
        self.api_key = api_key or require_env("WAVESPEED_API_KEY")
        self.base_url = (base_url or os.getenv("WAVESPEED_BASE_URL", BASE_URL)).rstrip("/")
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._session_cost: float = 0.0
        self._session_images: int = 0
        self._http: httpx.AsyncClient | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "WaveSpeedClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def session_cost(self) -> float:
        """Total estimated USD spent in this session."""
        return self._session_cost

    @property
    def session_images(self) -> int:
        """Total images generated in this session."""
        return self._session_images

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "WaveSpeedClient not opened. "
                "Use 'async with WaveSpeedClient()' or call await client.open() first."
            )
        return self._http

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ─── Core polling ─────────────────────────────────────────────────────────

    async def _post_and_poll(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> str:
        """
        POST a generation request, then poll until completion.

        Returns the first output URL from the completed result.

        Raises:
            RuntimeError: If polling times out or generation fails.
            httpx.HTTPStatusError: On unrecoverable HTTP errors.
        """
        url = f"{self.base_url}{endpoint}"

        # POST initial request
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client().post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    log.warning("Rate limit (429). Wait %.1fs (attempt %d/%d)", wait, attempt, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    if attempt == MAX_RETRIES:
                        raise
                elif 400 <= status < 500:
                    log.error("WaveSpeed client error %d: %s", status, exc.response.text[:200])
                    raise
                else:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    else:
                        raise
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt < MAX_RETRIES:
                    log.warning("Network error: %s. Retry %d/%d", exc, attempt, MAX_RETRIES)
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                else:
                    raise
        else:
            raise RuntimeError(f"POST to {endpoint} failed after {MAX_RETRIES} retries")

        # Extract task_id
        task_id: str | None = None
        if isinstance(data, dict):
            task_id = (
                data.get("data", {}).get("id")
                or data.get("id")
                or data.get("task_id")
            )
            # Check if result is already available (synchronous response)
            outputs = (
                data.get("data", {}).get("outputs")
                or data.get("outputs")
            )
            if outputs:
                log.debug("WaveSpeed sync response, no polling needed")
                return outputs[0]

        if not task_id:
            log.error("No task_id in WaveSpeed response: %s", str(data)[:300])
            raise RuntimeError(f"WaveSpeed response has no task_id: {data}")

        log.debug("WaveSpeed task_id=%s, polling...", task_id)

        # Poll for completion
        poll_url = f"{self.base_url}{POLL_ENDPOINT.format(task_id=task_id)}"
        poll_client = httpx.AsyncClient(
            headers=self._auth_headers(),
            timeout=POLL_TIMEOUT,
        )

        async with poll_client as pc:
            for poll_num in range(1, MAX_POLLS + 1):
                await asyncio.sleep(POLL_INTERVAL)
                try:
                    poll_resp = await pc.get(poll_url)
                    poll_resp.raise_for_status()
                    poll_data = poll_resp.json()
                except Exception as exc:
                    log.warning("Poll %d error: %s", poll_num, exc)
                    continue

                status = (
                    poll_data.get("data", {}).get("status")
                    or poll_data.get("status", "")
                )

                if status == "completed":
                    outputs = (
                        poll_data.get("data", {}).get("outputs")
                        or poll_data.get("outputs", [])
                    )
                    if outputs:
                        log.debug("WaveSpeed task=%s completed after %d polls", task_id, poll_num)
                        return outputs[0]
                    raise RuntimeError(f"WaveSpeed task {task_id} completed but no outputs")

                if status in ("failed", "error", "cancelled"):
                    error = poll_data.get("data", {}).get("error") or poll_data.get("error", "unknown")
                    raise RuntimeError(f"WaveSpeed task {task_id} {status}: {error}")

                if poll_num % 10 == 0:
                    log.debug("WaveSpeed task=%s status=%s (poll %d/%d)", task_id, status, poll_num, MAX_POLLS)

        raise RuntimeError(
            f"WaveSpeed task {task_id} timed out after {MAX_POLLS * POLL_INTERVAL:.0f}s"
        )

    # ─── Image download ───────────────────────────────────────────────────────

    async def _download(self, image_url: str, output_path: Path) -> Path:
        """Download an image URL to a local file and verify it is non-empty.

        Automatically converts WebP→PNG: flux-dev-ultra-fast may return WebP despite
        ``output_format='png'``.  FFmpeg concat demuxer cannot loop WebP images for their
        full duration — it decodes only 1 frame and stops — so PNG is required.
        Pillow is used for the conversion (available in project dependencies).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=60.0) as dl:
            resp = await dl.get(image_url)
            resp.raise_for_status()
            data = resp.content

        # Detect WebP by magic bytes: RIFF????WEBP (bytes 0-3 and 8-11)
        # WaveSpeed flux-dev-ultra-fast ignores output_format='png' and may return WebP.
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            try:
                import io
                from PIL import Image  # type: ignore[import]
                img = Image.open(io.BytesIO(data))
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                converted = buf.getvalue()
                log.info(
                    "WebP→PNG converted: %s (%d KB → %d KB)",
                    output_path.name, len(data) // 1024, len(converted) // 1024,
                )
                data = converted
            except Exception as exc:
                log.warning("WebP→PNG conversion failed for %s: %s — saving WebP as-is", output_path.name, exc)

        output_path.write_bytes(data)
        saved_size = output_path.stat().st_size
        if saved_size < 5_000:  # 5 KB minimum — image gen should always produce >50 KB
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded image too small ({saved_size} bytes) — likely an error response, not an image"
            )
        log.debug("Image saved: %s (%d bytes)", output_path, saved_size)
        return output_path

    def _track(self, model: str = "flux-dev-ultra-fast") -> None:
        self._session_cost += COST_PER_IMAGE
        self._session_images += 1
        log.info(
            "WaveSpeed %s — cost=$%.3f session_total=%d images / $%.3f",
            model, COST_PER_IMAGE, self._session_images, self._session_cost,
        )

    # ─── Text-to-image ────────────────────────────────────────────────────────

    async def generate_text2img(
        self,
        prompt: str,
        *,
        size: str = DEFAULT_SIZE,
        seed: int | None = None,
        num_inference_steps: int = 28,   # flux-dev-ultra-fast optimal (was 4 for z-image/turbo)
        guidance_scale: float = 3.5,     # flux-dev-ultra-fast optimal (was 1.0 for z-image/turbo)
        output_path: str | Path | None = None,
    ) -> str:
        """
        Generate an image from a text prompt (primary method).

        Args:
            prompt: Text description of the desired image.
            size: Image dimensions in "W*H" format (default "1280*720").
            seed: Random seed for reproducibility. None = random.
            num_inference_steps: Inference steps (4 is optimal for turbo model).
            guidance_scale: Prompt adherence strength.
            output_path: If provided, downloads image to this path.

        Returns:
            Image URL (remote) or local file path string if output_path given.
        """
        if size not in VALID_SIZES:
            log.warning("Size '%s' may not be supported. Supported: %s", size, VALID_SIZES)

        # flux-dev-ultra-fast payload (confirmed working from Thumbnail Analyzer)
        payload: dict[str, Any] = {
            "prompt": prompt,
            "size": size,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "output_format": "png",
            "enable_sync_mode": True,
        }
        if seed is not None:
            payload["seed"] = seed

        t0 = time.monotonic()
        async with self._semaphore:
            image_url = await self._post_and_poll(T2I_ENDPOINT, payload)

        elapsed = time.monotonic() - t0
        self._track()
        log.info("text2img done in %.1fs: %s", elapsed, image_url[:80])

        if output_path:
            p = Path(output_path)
            await self._download(image_url, p)
            return str(p)

        return image_url

    # ─── Image-to-image ───────────────────────────────────────────────────────

    async def generate_img2img(
        self,
        prompt: str,
        ref_url: str,
        *,
        strength: float = 0.7,
        size: str = DEFAULT_SIZE,
        seed: int | None = None,
        num_inference_steps: int = 4,
        output_path: str | Path | None = None,
    ) -> str:
        """
        Refine an existing image using a reference (image-to-image).

        Args:
            prompt: Text description for the refined image.
            ref_url: URL of the reference image (use upload_image() for local files).
            strength: How much to change the image (0.0 = no change, 1.0 = full regen).
            size: Output dimensions in "W*H" format.
            seed: Random seed. None = random.
            output_path: If provided, downloads image to this path.

        Returns:
            Image URL or local file path string if output_path given.
        """
        payload: dict[str, Any] = {
            "prompt": prompt,
            "image": ref_url,
            "strength": strength,
            "size": size,
            "num_inference_steps": num_inference_steps,
            "enable_safety_checker": False,
        }
        if seed is not None:
            payload["seed"] = seed

        t0 = time.monotonic()
        async with self._semaphore:
            image_url = await self._post_and_poll(I2I_ENDPOINT, payload)

        elapsed = time.monotonic() - t0
        self._track("z-image-turbo/i2i")
        log.info("img2img done in %.1fs: %s", elapsed, image_url[:80])

        if output_path:
            p = Path(output_path)
            await self._download(image_url, p)
            return str(p)

        return image_url

    # ─── Upload ───────────────────────────────────────────────────────────────

    async def upload_image(self, file_path: str | Path) -> str:
        """
        Upload a local image to WaveSpeed for use as img2img reference.

        Args:
            file_path: Path to local image file (JPEG, PNG).

        Returns:
            Public URL of the uploaded image.
        """
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"Image not found: {p}")

        upload_url = f"{self.base_url}{UPLOAD_ENDPOINT}"
        content_type = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60.0,
        ) as client:
            resp = await client.post(
                upload_url,
                content=p.read_bytes(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": content_type,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        url = (
            data.get("data", {}).get("url")
            or data.get("url")
            or data.get("file_url")
        )
        if not url:
            raise RuntimeError(f"WaveSpeed upload returned no URL: {data}")

        log.info("Uploaded %s → %s", p.name, url[:80])
        return url

    # ─── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        Connectivity test for `python dev.py check-apis`.
        Generates a minimal 1-step image (cheapest possible call).
        """
        url = await self.generate_text2img(
            "solid blue gradient, abstract, minimal",
            size="1024*1024",
            seed=1,
            num_inference_steps=1,
        )
        return {"ok": True, "url": url[:80], "cost": COST_PER_IMAGE}


# ─── CLI self-test ────────────────────────────────────────────────────────────

async def _self_test() -> None:
    """Run when executed directly: python clients/wavespeed_client.py"""
    import argparse

    parser = argparse.ArgumentParser(description="WaveSpeed client — self-test")
    parser.add_argument(
        "--prompt",
        default="Cinematic landscape at golden hour, dramatic clouds, photorealistic, 8k",
        help="Image prompt to test",
    )
    parser.add_argument("--output", default="projects/test_wavespeed.png", help="Output file path")
    parser.add_argument("--size", default=DEFAULT_SIZE, help="Image size (W*H)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Random seed")
    parser.add_argument("--upload", help="Test upload_image() with this local file path")
    args = parser.parse_args()

    log.info("WaveSpeed client self-test starting...")
    log.info("Base URL: %s", BASE_URL)
    log.info("API key: %s...", (os.getenv("WAVESPEED_API_KEY") or "NOT SET")[:8])

    async with WaveSpeedClient() as client:
        if args.upload:
            log.info("--- Test: upload_image(%s) ---", args.upload)
            ref_url = await client.upload_image(args.upload)
            log.info("Uploaded URL: %s", ref_url)

        log.info("--- Test: generate_text2img ---")
        log.info("Prompt: %s", args.prompt[:80])
        log.info("Size: %s | Seed: %s", args.size, args.seed)

        result = await client.generate_text2img(
            args.prompt,
            size=args.size,
            seed=args.seed,
            output_path=args.output,
        )

        log.info("Result: %s", result)
        log.info("Session: %d image(s) / $%.3f", client.session_images, client.session_cost)
        log.info("wavespeed_client.py self-test OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
