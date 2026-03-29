"""
VideoForge — VoiceAPI client (primary TTS provider).

Task-based async TTS via voiceapi.csv666.ru (ElevenLabs proxy).
Fallback TTS: VoidAI TTS (tts-1-hd) — automatic on failure.

API Flow:
    1. POST /tasks  {"text": "...", "template_uuid": "..."}  → {"id": task_id}
    2. GET  /tasks/{task_id}/status                          → {"status": "processing"|"ending"|"done"}
    3. GET  /tasks/{task_id}/result                          → audio/mpeg bytes

Auth: X-API-Key header

Usage:
    async with VoiceAPIClient() as client:
        path = await client.generate(
            "Hello world",
            voice_id="a4CnuaYbALRvW39mDitg",  # informational, template_uuid is used
            output_path="audio/block_001.mp3",
        )
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

log = setup_logging("voiceapi")

# ─── Constants ────────────────────────────────────────────────────────────────

VOICEAPI_BASE_URL = "https://voiceapi.csv666.ru"
DEFAULT_TEMPLATE_UUID = "a0c972ab-7c50-41c6-b59c-a73b1fe088e6"

MAX_CONCURRENT = 3        # Semaphore — CLAUDE.md: max 3 VoiceAPI concurrent
REQUEST_TIMEOUT = 120.0   # TTS can take a while for long texts
MAX_RETRIES = 3
RETRY_BASE_DELAY = 3.0

# Polling for task status
POLL_INTERVAL = 2.0       # seconds between status checks
POLL_TIMEOUT = 180.0      # max seconds to wait for a task

# Task terminal states
_DONE_STATUSES = {"done", "completed", "finished", "success"}
_ERROR_STATUSES = {"error", "failed", "cancelled"}

# Text chunk size — split long texts to avoid API limits
MAX_CHUNK_CHARS = 2000


# ─── VoiceAPI Client ──────────────────────────────────────────────────────────

class VoiceAPIClient:
    """
    Async client for VoiceAPI (voiceapi.csv666.ru — task-based ElevenLabs proxy).

    Flow: POST /tasks → poll GET /tasks/{id}/status → GET /tasks/{id}/result
    Automatically falls back to VoidAI TTS if VoiceAPI fails.

    Usage:
        async with VoiceAPIClient() as client:
            mp3_bytes = await client.generate("Text to speak", voice_id="abc123")
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        template_uuid: str | None = None,
        voidai_fallback: bool = True,
    ) -> None:
        load_env()
        self.api_key = api_key or require_env("VOICEAPI_KEY")
        self.base_url = (base_url or os.getenv("VOICEAPI_BASE_URL", VOICEAPI_BASE_URL)).rstrip("/")
        self.template_uuid = template_uuid or os.getenv("VOICEAPI_TEMPLATE_UUID", DEFAULT_TEMPLATE_UUID)
        self.default_voice_id = os.getenv("DEFAULT_VOICE_ID", "")
        self.voidai_fallback = voidai_fallback
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._session_chars: int = 0
        self._fallback_used: int = 0
        self._http: httpx.AsyncClient | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT,
        )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "VoiceAPIClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "VoiceAPIClient not opened. "
                "Use 'async with VoiceAPIClient()' or call await client.open() first."
            )
        return self._http

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    # ─── Task lifecycle ───────────────────────────────────────────────────────

    async def _create_task(self, text: str) -> str:
        """POST /tasks → task_id string."""
        payload = {
            "text": text,
            "template_uuid": self.template_uuid,
        }
        resp = await self._client().post("/tasks", json=payload, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            raise RuntimeError(f"VoiceAPI: no task_id in /tasks response: {data}")
        return str(task_id)

    async def _poll_status(self, task_id: str) -> None:
        """
        Poll GET /tasks/{task_id}/status until terminal state or timeout.
        States observed: processing → ending → (result available)
        """
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            resp = await self._client().get(
                f"/tasks/{task_id}/status", headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()
            status = (data.get("status") or "").lower()

            if status in _DONE_STATUSES:
                log.debug("Task %s: status=%s — done", task_id, status)
                return

            if status in _ERROR_STATUSES:
                raise RuntimeError(
                    f"VoiceAPI task {task_id} failed with status: {status}"
                )

            log.debug("Task %s: status=%s — polling in %.1fs", task_id, status, POLL_INTERVAL)
            await asyncio.sleep(POLL_INTERVAL)

        raise RuntimeError(
            f"VoiceAPI task {task_id} timed out after {POLL_TIMEOUT:.0f}s"
        )

    async def _fetch_result(self, task_id: str) -> bytes:
        """GET /tasks/{task_id}/result → MP3 bytes."""
        resp = await self._client().get(
            f"/tasks/{task_id}/result", headers=self._headers()
        )
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "audio" not in content_type or len(resp.content) < 100:
            raise RuntimeError(
                f"VoiceAPI result: unexpected content-type={content_type!r}, "
                f"size={len(resp.content)} bytes"
            )

        return resp.content

    # ─── Core TTS ─────────────────────────────────────────────────────────────

    async def _post_tts(
        self,
        text: str,
        voice_id: str,    # kept for interface compat; template_uuid is the actual selector
        language: str = "en",
    ) -> bytes:
        """
        Full TTS flow with retry: create task → poll status → fetch result.
        Returns raw MP3 bytes.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                task_id = await self._create_task(text)
                log.debug("Task created: id=%s (attempt %d/%d)", task_id, attempt, MAX_RETRIES)
                await self._poll_status(task_id)
                return await self._fetch_result(task_id)

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    log.warning(
                        "Rate limit (429). Wait %.1fs (attempt %d/%d)", wait, attempt, MAX_RETRIES
                    )
                    await asyncio.sleep(wait)
                    if attempt == MAX_RETRIES:
                        raise
                elif 400 <= status < 500:
                    body = exc.response.text[:300]
                    log.error("VoiceAPI client error %d: %s", status, body)
                    raise
                else:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    else:
                        raise
            except (httpx.ConnectError, httpx.TimeoutException, RuntimeError) as exc:
                if attempt < MAX_RETRIES:
                    log.warning(
                        "VoiceAPI error: %s. Retry %d/%d", exc, attempt, MAX_RETRIES
                    )
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                else:
                    raise

        raise RuntimeError(f"VoiceAPI TTS failed after {MAX_RETRIES} retries")

    # ─── Fallback via VoidAI ──────────────────────────────────────────────────

    async def _voidai_tts_fallback(
        self,
        text: str,
        model: str = "tts-1-hd",
        voice: str = "onyx",
    ) -> bytes:
        """Call VoidAI TTS as fallback when VoiceAPI is unavailable."""
        from clients.voidai_client import VoidAIClient

        log.warning("Using VoidAI TTS fallback (model=%s, voice=%s)", model, voice)
        async with VoidAIClient() as voidai:
            return await voidai.generate_tts(text, model=model, voice=voice)

    # ─── Text splitting ───────────────────────────────────────────────────────

    @staticmethod
    def _split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
        """
        Split long text into chunks at sentence boundaries.
        Keeps chunks under max_chars while preserving sentence integrity.
        """
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        current = ""

        import re
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())

        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= max_chars:
                current = f"{current} {sentence}".strip()
            else:
                if current:
                    chunks.append(current)
                if len(sentence) > max_chars:
                    parts = sentence.split(", ")
                    part_buf = ""
                    for part in parts:
                        if len(part_buf) + len(part) + 2 <= max_chars:
                            part_buf = f"{part_buf}, {part}".strip(", ")
                        else:
                            if part_buf:
                                chunks.append(part_buf)
                            part_buf = part
                    if part_buf:
                        current = part_buf
                else:
                    current = sentence

        if current:
            chunks.append(current)

        return chunks

    # ─── MP3 concatenation ────────────────────────────────────────────────────

    @staticmethod
    def _concat_mp3_bytes(parts: list[bytes]) -> bytes:
        """Concatenate multiple MP3 byte chunks."""
        return b"".join(parts)

    # ─── Public API ───────────────────────────────────────────────────────────

    async def generate(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        language: str = "en",
        output_path: str | Path | None = None,
        fallback_model: str = "tts-1-hd",
        fallback_voice: str = "onyx",
    ) -> bytes:
        """
        Generate speech from text using VoiceAPI (with automatic VoidAI fallback).

        Long texts are automatically split into chunks and concatenated.

        Args:
            text: Text to synthesize (any length).
            voice_id: Informational only (template_uuid controls the actual voice).
                      Falls back to DEFAULT_VOICE_ID env var for logging.
            language: Language code — for logging only.
            output_path: If provided, saves MP3 to this path.
            fallback_model: VoidAI TTS model if VoiceAPI fails.
            fallback_voice: VoidAI voice name if VoiceAPI fails.

        Returns:
            Raw MP3 audio bytes.
        """
        vid = voice_id or self.default_voice_id or "default"

        text = text.strip()
        if not text:
            raise ValueError("Text cannot be empty")

        t0 = time.monotonic()
        audio_parts: list[bytes] = []
        use_fallback = False

        async with self._semaphore:
            chunks = self._split_text(text)
            if len(chunks) > 1:
                log.info(
                    "Text split into %d chunks (total %d chars) template=%s",
                    len(chunks), len(text), self.template_uuid[:8],
                )

            for i, chunk in enumerate(chunks):
                try:
                    part = await self._post_tts(chunk, vid, language)
                    audio_parts.append(part)
                    self._session_chars += len(chunk)
                except Exception as exc:
                    if self.voidai_fallback:
                        log.warning(
                            "VoiceAPI failed on chunk %d/%d (%s). Switching to VoidAI fallback.",
                            i + 1, len(chunks), type(exc).__name__,
                        )
                        use_fallback = True
                        # Process each remaining chunk individually via VoidAI
                        for remaining_chunk in chunks[i:]:
                            part = await self._voidai_tts_fallback(
                                remaining_chunk, model=fallback_model, voice=fallback_voice
                            )
                            audio_parts.append(part)
                        self._fallback_used += 1
                        break
                    else:
                        raise

        audio = self._concat_mp3_bytes(audio_parts)
        elapsed = time.monotonic() - t0

        log.info(
            "TTS done: voice=%s lang=%s chars=%d chunks=%d size=%dKB elapsed=%.1fs%s",
            vid[:8], language, len(text), len(chunks),
            len(audio) // 1024, elapsed,
            " [FALLBACK]" if use_fallback else "",
        )

        if output_path:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(audio)
            log.info("Audio saved: %s", p)

        return audio

    # ─── Voice / template listing ──────────────────────────────────────────────

    async def list_voices(self) -> list[dict[str, Any]]:
        """
        List available TTS templates from VoiceAPI.
        Returns list of dicts with at least {"template_uuid": str, "name": str}.
        """
        try:
            resp = await self._client().get("/templates", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("templates", data.get("voices", []))
        except Exception as exc:
            log.warning("Could not list templates: %s", exc)
            return []

    # ─── Balance ──────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """GET /balance → {"balance": int, "unit": "chars"} or similar."""
        try:
            resp = await self._client().get("/balance", headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("Could not fetch balance: %s", exc)
            return {}

    # ─── Stats ────────────────────────────────────────────────────────────────

    @property
    def session_chars(self) -> int:
        """Total characters synthesized via VoiceAPI in this session."""
        return self._session_chars

    @property
    def fallback_count(self) -> int:
        """Number of times VoidAI fallback was triggered."""
        return self._fallback_used

    # ─── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        Connectivity test for `python dev.py check-apis`.
        Tests balance endpoint (no audio generated).
        """
        balance_data = await self.get_balance()
        if balance_data:
            balance = balance_data.get("balance", balance_data.get("characters", "?"))
            return {
                "ok": True,
                "balance_chars": balance,
                "template_uuid": self.template_uuid,
                "provider": "voiceapi",
            }

        if not self.api_key or self.api_key.startswith("your_"):
            return {"ok": False, "error": "VOICEAPI_KEY not configured"}

        return {"ok": True, "balance_chars": "unknown", "provider": "voiceapi"}


# ─── CLI self-test ────────────────────────────────────────────────────────────

async def _self_test() -> None:
    """Run when executed directly: python clients/voiceapi_client.py"""
    import argparse

    parser = argparse.ArgumentParser(description="VoiceAPI client — self-test")
    parser.add_argument(
        "--text",
        default="This is a test of the VideoForge voice synthesis system. The audio quality should be clear and natural.",
        help="Text to synthesize",
    )
    parser.add_argument("--output", default="projects/test_voiceapi.mp3", help="Output MP3 path")
    parser.add_argument("--list-voices", action="store_true", help="List available templates and exit")
    parser.add_argument("--balance", action="store_true", help="Show balance and exit")
    parser.add_argument("--no-fallback", action="store_true", help="Disable VoidAI fallback")
    args = parser.parse_args()

    log.info("VoiceAPI client self-test starting...")
    log.info("Base URL: %s", VOICEAPI_BASE_URL)
    log.info("API key: %s...", (os.getenv("VOICEAPI_KEY") or "NOT SET")[:12])

    async with VoiceAPIClient(voidai_fallback=not args.no_fallback) as client:
        log.info("Template UUID: %s", client.template_uuid)

        if args.balance:
            data = await client.get_balance()
            log.info("Balance: %s", data)
            return

        if args.list_voices:
            log.info("--- Listing templates ---")
            templates = await client.list_voices()
            log.info("Found %d templates:", len(templates))
            for t in templates[:10]:
                log.info(
                    "  uuid=%s name=%s voice_id=%s",
                    t.get("uuid", t.get("id", "?")),
                    t.get("name", "?"),
                    t.get("voice_id", "?"),
                )
            return

        log.info("--- Test: generate TTS ---")
        log.info("Text: %s", args.text[:80])

        audio = await client.generate(
            args.text,
            output_path=args.output,
        )

        log.info("Audio: %d bytes → %s", len(audio), args.output)
        log.info("Session chars: %d", client.session_chars)
        if client.fallback_count:
            log.warning("Fallback used %d time(s)", client.fallback_count)
        log.info("voiceapi_client.py self-test OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
