"""
VideoForge — VoiceAPI client (primary TTS provider).

Primary TTS: VoiceAPI (voiceapi.csv666.ru → ElevenLabs cloned voices).
Fallback TTS: VoidAI TTS (tts-1-hd / gpt-4o-mini-tts) — automatic on failure.

Usage:
    async with VoiceAPIClient() as client:
        path = await client.generate(
            "Hello world",
            voice_id="your_voice_id",
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
MAX_CONCURRENT = 3        # Semaphore — CLAUDE.md: max 3 VoiceAPI concurrent
REQUEST_TIMEOUT = 120.0   # TTS can take a while for long texts
MAX_RETRIES = 3
RETRY_BASE_DELAY = 3.0

# Text chunk size — split long texts to avoid API limits (~2500 chars safe)
MAX_CHUNK_CHARS = 2000


# ─── VoiceAPI Client ──────────────────────────────────────────────────────────

class VoiceAPIClient:
    """
    Async client for VoiceAPI (ElevenLabs cloned voices).

    Automatically falls back to VoidAI TTS if VoiceAPI is unavailable.

    Usage:
        async with VoiceAPIClient() as client:
            mp3_bytes = await client.generate("Text to speak", voice_id="abc123")
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        voidai_fallback: bool = True,
    ) -> None:
        load_env()
        self.api_key = api_key or require_env("VOICEAPI_KEY")
        self.base_url = (base_url or os.getenv("VOICEAPI_BASE_URL", VOICEAPI_BASE_URL)).rstrip("/")
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

    # ─── Core request ─────────────────────────────────────────────────────────

    async def _post_tts(
        self,
        text: str,
        voice_id: str,
        language: str = "en",
    ) -> bytes:
        """
        POST TTS request to VoiceAPI. Returns raw MP3 bytes.

        VoiceAPI endpoint pattern (ElevenLabs-compatible):
          POST /v1/text-to-speech/{voice_id}
          Body: {"text": "...", "model_id": "eleven_multilingual_v2"}
          Auth: xi-api-key header
        """
        endpoint = f"/v1/text-to-speech/{voice_id}"
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.85,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
                "speed": 1.1,
            },
        }
        headers = {
            "xi-api-key": self.api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client().post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()

                # Verify we got audio content
                content_type = resp.headers.get("content-type", "")
                if "audio" not in content_type and len(resp.content) < 100:
                    raise RuntimeError(f"VoiceAPI returned non-audio response: {content_type}")

                return resp.content

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    log.warning("Rate limit (429). Wait %.1fs (attempt %d/%d)", wait, attempt, MAX_RETRIES)
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
                    log.warning("VoiceAPI error: %s. Retry %d/%d", exc, attempt, MAX_RETRIES)
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

        # Split by sentences (period/exclamation/question + space)
        import re
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())

        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= max_chars:
                current = f"{current} {sentence}".strip()
            else:
                if current:
                    chunks.append(current)
                # Handle very long single sentences
                if len(sentence) > max_chars:
                    # Split by comma as fallback
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
        """Concatenate multiple MP3 byte chunks (raw byte join — works for most players)."""
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
            voice_id: ElevenLabs voice ID. Falls back to DEFAULT_VOICE_ID env var.
            language: Language code ("en", "de", "es", etc.) — for logging.
            output_path: If provided, saves MP3 to this path.
            fallback_model: VoidAI TTS model if VoiceAPI fails.
            fallback_voice: VoidAI voice name if VoiceAPI fails.

        Returns:
            Raw MP3 audio bytes.
        """
        vid = voice_id or self.default_voice_id
        if not vid:
            raise ValueError(
                "No voice_id provided and DEFAULT_VOICE_ID env var not set. "
                "Pass voice_id= or set DEFAULT_VOICE_ID in .env"
            )

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
                    "Text split into %d chunks (total %d chars) for voice_id=%s",
                    len(chunks), len(text), vid[:8],
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
                        # Fallback for remaining chunks (including current)
                        remaining_text = " ".join(chunks[i:])
                        part = await self._voidai_tts_fallback(
                            remaining_text, model=fallback_model, voice=fallback_voice
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

    # ─── Voice listing ────────────────────────────────────────────────────────

    async def list_voices(self) -> list[dict[str, Any]]:
        """
        List available voices from VoiceAPI.

        Returns:
            List of voice dicts with at least {"voice_id": str, "name": str}.
        """
        headers = {"xi-api-key": self.api_key}
        try:
            resp = await self._client().get("/v1/voices", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("voices", data if isinstance(data, list) else [])
        except Exception as exc:
            log.warning("Could not list voices: %s", exc)
            return []

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

        Tests voice listing (doesn't generate audio to save quota).
        """
        voices = await self.list_voices()
        if voices:
            return {"ok": True, "voices_available": len(voices), "provider": "voiceapi"}

        # If listing fails, check if API key is set
        if not self.api_key or self.api_key.startswith("your_"):
            return {"ok": False, "error": "VOICEAPI_KEY not configured"}

        # API key set but couldn't list — may still work for generation
        return {"ok": True, "voices_available": "unknown", "provider": "voiceapi"}


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
    parser.add_argument("--voice-id", help="ElevenLabs voice ID (overrides DEFAULT_VOICE_ID)")
    parser.add_argument("--output", default="projects/test_voiceapi.mp3", help="Output MP3 path")
    parser.add_argument("--list-voices", action="store_true", help="List available voices and exit")
    parser.add_argument("--no-fallback", action="store_true", help="Disable VoidAI fallback")
    args = parser.parse_args()

    log.info("VoiceAPI client self-test starting...")
    log.info("Base URL: %s", VOICEAPI_BASE_URL)
    log.info("API key: %s...", (os.getenv("VOICEAPI_KEY") or "NOT SET")[:8])

    async with VoiceAPIClient(voidai_fallback=not args.no_fallback) as client:
        if args.list_voices:
            log.info("--- Listing voices ---")
            voices = await client.list_voices()
            log.info("Found %d voices:", len(voices))
            for v in voices[:10]:
                log.info("  voice_id=%s name=%s", v.get("voice_id", "?"), v.get("name", "?"))
            return

        log.info("--- Test: generate TTS ---")
        log.info("Text: %s", args.text[:80])

        audio = await client.generate(
            args.text,
            voice_id=args.voice_id,
            output_path=args.output,
        )

        log.info("Audio: %d bytes", len(audio))
        log.info("Session chars: %d", client.session_chars)
        if client.fallback_count:
            log.warning("Fallback used %d time(s)", client.fallback_count)
        log.info("voiceapi_client.py self-test OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
