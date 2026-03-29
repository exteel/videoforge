"""
VideoForge — shared auth dependency.

If ACCESS_CODE is set in .env, all /api/* routes require
the header `X-API-Key: <code>`.  If it is NOT set the
server runs in open mode (local dev with no protection).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: str | None = Security(_scheme)) -> None:
    """FastAPI dependency — raises 401 if ACCESS_CODE is set and key doesn't match."""
    code = os.getenv("ACCESS_CODE", "").strip()
    if not code:
        return  # protection disabled — local / dev mode
    if api_key != code:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access code",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ── In-memory rate limiter ────────────────────────────────────────────────────

_request_times: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 10   # max requests per minute per IP
_WINDOW = 60.0     # seconds


def check_rate_limit(client_ip: str) -> None:
    """Raise 429 if the client IP exceeds the rate limit.

    Limits pipeline-creation endpoints to _RATE_LIMIT requests per _WINDOW
    seconds. Uses a sliding window backed by a simple in-memory list — safe
    for a single-process server (uvicorn/gunicorn single worker).
    """
    now = time.monotonic()
    # Evict timestamps outside the current window
    _request_times[client_ip] = [
        t for t in _request_times[client_ip] if now - t < _WINDOW
    ]
    if len(_request_times[client_ip]) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: max {_RATE_LIMIT} requests per {int(_WINDOW)}s",
        )
    _request_times[client_ip].append(now)
