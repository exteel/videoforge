"""
VideoForge Backend — Channels & Prompts CRUD routes.

GET    /api/channels              → list channel configs
GET    /api/channels/{name}       → get one channel config
PUT    /api/channels/{name}       → save channel config (create or update)
DELETE /api/channels/{name}       → delete channel config

GET    /api/prompts               → list prompt files (.txt, .md)
GET    /api/prompts/{name}        → get prompt text
PUT    /api/prompts/{name}        → save prompt text
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["channels"])

ROOT         = Path(__file__).parent.parent.parent
CHANNELS_DIR = ROOT / "config" / "channels"
PROMPTS_DIR  = ROOT / "prompts"


# ─── Models ───────────────────────────────────────────────────────────────────

class ChannelMeta(BaseModel):
    name: str           # filename stem, e.g. "history"
    channel_name: str   # display name from JSON
    niche: str
    language: str


class PromptMeta(BaseModel):
    name: str           # filename stem, e.g. "master_script_v1"
    filename: str       # full filename with ext
    size_bytes: int


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    """Strip path traversal attempts, keep only safe chars."""
    name = Path(name).stem   # drop any extension the caller sent
    name = re.sub(r"[^\w\-]", "", name)
    if not name:
        raise HTTPException(400, "Invalid name")
    return name


def _channel_path(name: str) -> Path:
    return CHANNELS_DIR / f"{name}.json"


def _prompt_path(name: str) -> Path:
    """Resolve prompt by stem — accepts .txt or .md."""
    for ext in (".txt", ".md"):
        p = PROMPTS_DIR / f"{name}{ext}"
        if p.exists():
            return p
    # default to .txt for new files
    return PROMPTS_DIR / f"{name}.txt"


# ─── Channels ─────────────────────────────────────────────────────────────────

@router.get("/channels", response_model=list[ChannelMeta])
async def list_channels() -> list[dict]:
    """List all channel configs in config/channels/."""
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for p in sorted(CHANNELS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            result.append({
                "name":         p.stem,
                "channel_name": data.get("channel_name", p.stem),
                "niche":        data.get("niche", ""),
                "language":     data.get("language", "en"),
            })
        except Exception:
            continue
    return result


@router.get("/channels/{name}")
async def get_channel(name: str) -> dict[str, Any]:
    """Return full channel config JSON."""
    name = _safe_name(name)
    p = _channel_path(name)
    if not p.exists():
        raise HTTPException(404, f"Channel not found: {name}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, f"Failed to read channel: {exc}") from exc


@router.put("/channels/{name}")
async def save_channel(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create or update a channel config."""
    name = _safe_name(name)
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    p = _channel_path(name)
    try:
        p.write_text(
            json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"saved": True, "name": name, "path": str(p)}
    except Exception as exc:
        raise HTTPException(500, f"Failed to save channel: {exc}") from exc


@router.delete("/channels/{name}")
async def delete_channel(name: str) -> dict[str, Any]:
    """Delete a channel config file."""
    name = _safe_name(name)
    p = _channel_path(name)
    if not p.exists():
        raise HTTPException(404, f"Channel not found: {name}")
    p.unlink()
    return {"deleted": True, "name": name}


# ─── Prompts ──────────────────────────────────────────────────────────────────

@router.get("/prompts", response_model=list[PromptMeta])
async def list_prompts() -> list[dict]:
    """List all prompt files (.txt and .md) in prompts/."""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for p in sorted(PROMPTS_DIR.glob("*")):
        if p.suffix in (".txt", ".md") and p.is_file():
            result.append({
                "name":       p.stem,
                "filename":   p.name,
                "size_bytes": p.stat().st_size,
            })
    return result


@router.get("/prompts/{name}")
async def get_prompt(name: str) -> dict[str, Any]:
    """Return prompt text content."""
    name = _safe_name(name)
    p = _prompt_path(name)
    if not p.exists():
        raise HTTPException(404, f"Prompt not found: {name}")
    try:
        return {
            "name":     p.stem,
            "filename": p.name,
            "content":  p.read_text(encoding="utf-8"),
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to read prompt: {exc}") from exc


@router.put("/prompts/{name}")
async def save_prompt(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Save prompt text. Body: {"content": "...", "filename": "optional.txt"}"""
    name = _safe_name(name)
    content = body.get("content", "")
    filename = body.get("filename", "")

    # Determine target path
    if filename:
        ext = Path(filename).suffix or ".txt"
    else:
        # keep existing extension if file exists
        existing = _prompt_path(name)
        ext = existing.suffix if existing.exists() else ".txt"

    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    p = PROMPTS_DIR / f"{name}{ext}"
    try:
        p.write_text(content, encoding="utf-8")
        return {"saved": True, "name": name, "path": str(p), "size_bytes": len(content.encode())}
    except Exception as exc:
        raise HTTPException(500, f"Failed to save prompt: {exc}") from exc
