"""
VideoForge — VoidAI API client.

Single async client for all VoidAI (OpenAI-compatible) services:
  - chat_completion()    — LLM text generation with smart fallback chain
  - vision_completion()  — image analysis (gpt-4.1)
  - generate_tts()       — text-to-speech backup for VoiceAPI
  - generate_image()     — image generation backup for WaveSpeed

Smart fallback chain: Opus → Sonnet → GPT-4.1 on model failure.
Rate-limited via asyncio.Semaphore (max 10 concurrent requests).
"""

import asyncio
import base64
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_env, require_env, setup_logging

log = setup_logging("voidai")

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_CONCURRENT = 10      # Semaphore: max parallel VoidAI requests
DEFAULT_TIMEOUT = 300.0  # Seconds per request (5 min — Opus needs time for long transcripts)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0   # Doubles each attempt (2s, 4s, 8s)

# Smart fallback chain — on model failure, try next model (NOT the same model)
FALLBACK_CHAIN: dict[str, str | None] = {
    "claude-opus-4-6":            "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5-20250929": "gpt-4.1",
    "claude-sonnet-4-5":          "gpt-4.1",
    "gpt-5.2":                    "gpt-4.1",
    "deepseek-v3.1":              "gpt-4.1",
    "mistral-small-latest":       "gpt-4.1",
    "gpt-4.1":                    None,   # Last resort — no further fallback
    "gpt-4.1-mini":               None,
    "gpt-4.1-nano":               None,
    "gemini-2.5-flash":           None,
    "gemma-3n-e4b-it":            None,
}

# Cost per 1K tokens in USD — (input, output)
# Approximate values relative to VoidAI pricing
MODEL_COSTS: dict[str, tuple[float, float]] = {
    "claude-opus-4-6":            (0.015, 0.075),
    "claude-sonnet-4-5-20250929": (0.003, 0.015),
    "claude-sonnet-4-5":          (0.003, 0.015),
    "gpt-5.2":                    (0.002, 0.008),
    "gpt-4.1":                    (0.002, 0.008),
    "gpt-4.1-mini":               (0.00015, 0.0006),
    "gpt-4.1-nano":               (0.0001,  0.0004),
    "deepseek-v3.1":              (0.00015, 0.0006),
    "mistral-small-latest":       (0.00003, 0.00009),
    "gemma-3n-e4b-it":            (0.00005, 0.0001),
    "gemini-2.5-flash":           (0.000075, 0.0003),
    # TTS — cost per 1K characters (output only)
    "tts-1-hd":                   (0.030, 0.0),
    "gpt-4o-mini-tts":            (0.006, 0.0),
    # Image — flat cost per image
    "gpt-image-1.5":              (0.04, 0.0),
    "imagen-4.0":                 (0.04, 0.0),
}


def _estimate_cost(model: str, input_units: int, output_tokens: int = 0) -> float:
    """Estimate cost in USD. input_units = tokens for LLM, chars for TTS, 1 for images."""
    costs = MODEL_COSTS.get(model, (0.002, 0.008))
    return (input_units / 1000) * costs[0] + (output_tokens / 1000) * costs[1]


# ─── VoidAI Client ────────────────────────────────────────────────────────────

class VoidAIClient:
    """
    Async client for VoidAI (OpenAI-compatible API).

    Usage:
        async with VoidAIClient() as client:
            text = await client.chat_completion("gpt-4.1-nano", messages)

    Or without context manager (manual lifecycle):
        client = VoidAIClient()
        await client.open()
        text = await client.chat_completion(...)
        await client.close()
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        load_env()
        self.api_key = api_key or require_env("VOIDAI_API_KEY")
        self.base_url = (
            base_url or os.getenv("VOIDAI_BASE_URL", "https://api.voidai.app/v1")
        ).rstrip("/")
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._session_cost: float = 0.0
        self._http: httpx.AsyncClient | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the underlying httpx session."""
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

    async def close(self) -> None:
        """Close the underlying httpx session."""
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "VoidAIClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def session_cost(self) -> float:
        """Estimated total USD spent via this client instance."""
        return self._session_cost

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "VoidAIClient not opened. Use 'async with VoidAIClient()' "
                "or call await client.open() first."
            )
        return self._http

    # ─── Low-level POST ───────────────────────────────────────────────────────

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        POST to API endpoint with semaphore, retry, and exponential backoff.

        - 429 Rate limit → always retry (up to MAX_RETRIES)
        - 5xx Server error → retry with backoff
        - 4xx Client error (not 429) → raise immediately (no retry)
        - Network/timeout → retry with backoff
        """
        async with self._semaphore:
            last_exc: Exception | None = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = await self._http_client().post(endpoint, json=payload)
                    resp.raise_for_status()
                    return resp.json()

                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    last_exc = exc

                    if status == 429:
                        wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        log.warning(
                            "Rate limit (429) on %s. Wait %.1fs (attempt %d/%d)",
                            endpoint, wait, attempt, MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue

                    if 400 <= status < 500:
                        # Client error — don't retry
                        body = exc.response.text[:300]
                        log.error("Client error %d on %s: %s", status, endpoint, body)
                        raise

                    # 5xx — retry
                    if attempt < MAX_RETRIES:
                        wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        log.warning(
                            "Server error %d on %s. Retry %d/%d in %.1fs",
                            status, endpoint, attempt, MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise

                except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
                    last_exc = exc
                    if attempt < MAX_RETRIES:
                        wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        log.warning(
                            "Network error on %s: %s. Retry %d/%d in %.1fs",
                            endpoint, exc, attempt, MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise

            raise RuntimeError(
                f"All {MAX_RETRIES} retries exhausted for {endpoint}"
            ) from last_exc

    # ─── Chat / LLM ───────────────────────────────────────────────────────────

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        use_fallback: bool = True,
        **kwargs: Any,
    ) -> str:
        """
        Generate text with LLM.

        On model failure, automatically falls back to the next model in
        FALLBACK_CHAIN (Opus → Sonnet → GPT-4.1) unless use_fallback=False.

        Args:
            model: VoidAI model ID (e.g. "claude-opus-4-6").
            messages: Chat messages [{"role": "user", "content": "..."}].
            temperature: Sampling temperature 0–2.
            max_tokens: Max output tokens.
            use_fallback: Enable smart fallback chain on failure.
            **kwargs: Extra params forwarded to the API (e.g. top_p, stop).

        Returns:
            Generated text string.
        """
        base_payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        current_model = model
        while True:
            payload = {**base_payload, "model": current_model}
            try:
                t0 = time.monotonic()
                data = await self._post("/chat/completions", payload)
                elapsed = time.monotonic() - t0

                content: str = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                in_tok = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                cost = _estimate_cost(current_model, in_tok, out_tok)
                self._session_cost += cost

                if current_model != model:
                    log.info(
                        "chat_completion used fallback model=%s (requested=%s) "
                        "tokens=%d+%d cost=$%.5f elapsed=%.1fs",
                        current_model, model, in_tok, out_tok, cost, elapsed,
                    )
                else:
                    log.info(
                        "chat_completion model=%s tokens=%d+%d cost=$%.5f elapsed=%.1fs",
                        current_model, in_tok, out_tok, cost, elapsed,
                    )

                return content

            except Exception as exc:
                next_model = FALLBACK_CHAIN.get(current_model) if use_fallback else None
                if next_model:
                    log.warning(
                        "Model %s failed (%s: %s). Falling back to %s.",
                        current_model, type(exc).__name__, str(exc)[:120], next_model,
                    )
                    current_model = next_model
                else:
                    log.error("Model %s failed with no fallback: %s", current_model, exc)
                    raise

    # ─── Vision ───────────────────────────────────────────────────────────────

    async def vision_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "gpt-4.1",
        max_tokens: int = 500,
        temperature: float = 0.4,
    ) -> str:
        """
        Analyze image(s) using a vision-capable model.

        Build messages with VoidAIClient.image_message() helper.

        Args:
            messages: List of message dicts (see image_message() helper).
            model: Vision model ID.
            max_tokens: Max response tokens.
            temperature: Sampling temperature.

        Returns:
            Model response text.
        """
        t0 = time.monotonic()
        data = await self._post("/chat/completions", {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        elapsed = time.monotonic() - t0

        content: str = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        cost = _estimate_cost(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        self._session_cost += cost

        log.info(
            "vision_completion model=%s cost=$%.5f elapsed=%.1fs",
            model, cost, elapsed,
        )
        return content

    @staticmethod
    def encode_image(image_path: str | Path) -> str:
        """Base64-encode an image file for vision messages."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    @staticmethod
    def image_message(
        image_path: str | Path,
        text: str = "Analyze this image.",
    ) -> dict[str, Any]:
        """
        Build a user message dict with embedded base64 image for vision_completion.

        Example:
            msg = VoidAIClient.image_message("thumb.jpg", "Describe this thumbnail.")
            result = await client.vision_completion([msg])
        """
        b64 = VoidAIClient.encode_image(image_path)
        ext = Path(image_path).suffix.lstrip(".").lower()
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
            ],
        }

    # ─── TTS ──────────────────────────────────────────────────────────────────

    async def generate_tts(
        self,
        text: str,
        *,
        model: str = "tts-1-hd",
        voice: str = "alloy",
        output_path: str | Path | None = None,
    ) -> bytes:
        """
        Convert text to speech (backup for VoiceAPI).

        Args:
            text: Text to synthesize.
            model: TTS model ("tts-1-hd" or "gpt-4o-mini-tts").
            voice: Voice name ("alloy", "echo", "fable", "onyx", "nova", "shimmer").
            output_path: If provided, saves MP3 to this path.

        Returns:
            Raw MP3 audio bytes.
        """
        async with self._semaphore:
            last_exc: Exception | None = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = await self._http_client().post(
                        "/audio/speech",
                        json={
                            "model": model,
                            "input": text,
                            "voice": voice,
                            "response_format": "mp3",
                        },
                    )
                    resp.raise_for_status()
                    audio: bytes = resp.content

                    cost = _estimate_cost(model, len(text))
                    self._session_cost += cost
                    log.info(
                        "generate_tts model=%s chars=%d cost=$%.5f",
                        model, len(text), cost,
                    )

                    if output_path:
                        p = Path(output_path)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(audio)
                        log.info("TTS audio saved: %s", p)

                    return audio

                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    if 400 <= exc.response.status_code < 500:
                        raise
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    else:
                        raise
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    last_exc = exc
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    else:
                        raise

        raise RuntimeError(f"TTS failed after {MAX_RETRIES} retries") from last_exc

    # ─── Image generation ─────────────────────────────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str = "gpt-image-1.5",
        size: str = "1792x1024",
        quality: str = "high",
        output_path: str | Path | None = None,
    ) -> str:
        """
        Generate an image (backup for WaveSpeed).

        Args:
            prompt: Image description.
            model: Image model ("gpt-image-1.5", "imagen-4.0").
            size: Dimensions ("1792x1024", "1024x1024", "1280x720", etc.).
            quality: "standard" or "high".
            output_path: If provided, downloads and saves the image.

        Returns:
            Image URL string (or base64 data URL if API returns b64_json).
        """
        data = await self._post("/images/generations", {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
        })

        image_data = data["data"][0]
        url: str = image_data.get("url") or image_data.get("b64_json", "")

        cost = _estimate_cost(model, 1)  # Flat per-image pricing
        self._session_cost += cost
        log.info("generate_image model=%s size=%s cost=$%.4f", model, size, cost)

        if output_path and url.startswith("http"):
            async with httpx.AsyncClient(timeout=60.0) as dl:
                img_resp = await dl.get(url)
                img_resp.raise_for_status()
                p = Path(output_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(img_resp.content)
                log.info("Image saved: %s", p)

        return url

    # ─── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        Quick connectivity test for `python dev.py check-apis`.

        Calls chat_completion with gpt-4.1-nano (cheapest model).

        Returns:
            {"ok": True, "model": "gpt-4.1-nano", "response": "OK"}
        """
        response = await self.chat_completion(
            "gpt-4.1-nano",
            [{"role": "user", "content": "Reply with exactly: OK"}],
            temperature=0,
            max_tokens=5,
            use_fallback=False,
        )
        return {"ok": True, "model": "gpt-4.1-nano", "response": response.strip()}


# ─── CLI self-test ────────────────────────────────────────────────────────────

async def _self_test() -> None:
    """Run when executed directly: python clients/voidai_client.py"""
    import argparse

    parser = argparse.ArgumentParser(description="VoidAI client — self-test")
    parser.add_argument("--model", default="gpt-4.1-nano", help="Model to test")
    parser.add_argument("--prompt", default="What is 2+2? Reply with just the number.", help="Test prompt")
    parser.add_argument("--vision", help="Path to image file to test vision_completion")
    parser.add_argument("--no-fallback", action="store_true", help="Disable fallback chain")
    args = parser.parse_args()

    log.info("VoidAI client self-test starting...")
    log.info("Base URL: %s", os.getenv("VOIDAI_BASE_URL", "https://api.voidai.app/v1"))
    log.info("API key: %s...", (os.getenv("VOIDAI_API_KEY") or "NOT SET")[:8])

    async with VoidAIClient() as client:
        # Test 1: chat_completion
        log.info("--- Test 1: chat_completion (model=%s) ---", args.model)
        messages = [{"role": "user", "content": args.prompt}]
        result = await client.chat_completion(
            args.model,
            messages,
            use_fallback=not args.no_fallback,
        )
        log.info("Response: %r", result[:200])

        # Test 2: vision (optional)
        if args.vision:
            log.info("--- Test 2: vision_completion ---")
            img_path = Path(args.vision)
            if not img_path.exists():
                log.error("Image not found: %s", img_path)
            else:
                msg = VoidAIClient.image_message(img_path, "Describe this image in one sentence.")
                vision_result = await client.vision_completion([msg])
                log.info("Vision response: %r", vision_result[:200])

        log.info("Session cost: $%.5f", client.session_cost)
        log.info("voidai_client.py self-test OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
