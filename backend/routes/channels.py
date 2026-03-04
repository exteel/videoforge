"""
VideoForge Backend — Channels & Prompts CRUD routes.

GET    /api/channels                    → list channel configs (with auth status)
GET    /api/channels/{name}             → get one channel config
PUT    /api/channels/{name}             → save channel config (create or update)
DELETE /api/channels/{name}             → delete channel config

GET    /api/channels/{name}/auth        → check OAuth2 token status for channel
POST   /api/channels/{name}/auth        → start OAuth2 browser flow for channel
DELETE /api/channels/{name}/auth        → revoke (delete) channel token
POST   /api/channels/{name}/branding    → apply channel branding via YouTube API

GET    /api/prompts               → list prompt files (.txt, .md)
GET    /api/prompts/{name}        → get prompt text
PUT    /api/prompts/{name}        → save prompt text
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["channels"])

ROOT         = Path(__file__).parent.parent.parent
CHANNELS_DIR = ROOT / "config" / "channels"
PROMPTS_DIR  = ROOT / "prompts"
OAUTH2_DIR   = ROOT / "config" / "oauth2"

sys.path.insert(0, str(ROOT))


# ─── Models ───────────────────────────────────────────────────────────────────

class ChannelMeta(BaseModel):
    name: str           # filename stem, e.g. "history"
    channel_name: str   # display name from JSON
    niche: str
    language: str
    auth_connected: bool = False
    proxy: str = ""


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

def _token_path(channel_slug: str) -> Path:
    """Path to the OAuth2 pickle token for a channel."""
    safe = channel_slug.replace(" ", "_").replace("/", "_")
    return OAUTH2_DIR / f"{safe}_token.pickle"


def _auth_status(channel_slug: str) -> bool:
    """Return True if a non-empty token pickle exists for this channel."""
    p = _token_path(channel_slug)
    return p.exists() and p.stat().st_size > 0


@router.get("/channels", response_model=list[ChannelMeta])
async def list_channels() -> list[dict]:
    """List all channel configs in config/channels/ with auth status."""
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for p in sorted(CHANNELS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            slug = p.stem
            result.append({
                "name":           slug,
                "channel_name":   data.get("channel_name", slug),
                "niche":          data.get("niche", ""),
                "language":       data.get("language", "en"),
                "auth_connected": _auth_status(slug),
                "proxy":          data.get("proxy", ""),
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


# ─── Client secrets (shared across all channels) ─────────────────────────────

SECRETS_PATH = ROOT / "config" / "client_secrets.json"


@router.get("/channels/secrets-status")
async def secrets_status() -> dict[str, Any]:
    """Check if config/client_secrets.json exists."""
    exists = SECRETS_PATH.exists()
    client_id = ""
    if exists:
        try:
            data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
            installed = data.get("installed") or data.get("web") or {}
            client_id = installed.get("client_id", "")[:30] + "…" if installed.get("client_id") else ""
        except Exception:
            pass
    return {"exists": exists, "path": str(SECRETS_PATH), "client_id_preview": client_id}


class SecretsBody(BaseModel):
    content: str   # raw JSON string of client_secrets.json


@router.post("/channels/secrets")
async def save_secrets(body: SecretsBody) -> dict[str, Any]:
    """Save client_secrets.json content to config/client_secrets.json."""
    try:
        data = json.loads(body.content)
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON: {exc}") from exc
    if "installed" not in data and "web" not in data:
        raise HTTPException(400, "Invalid client_secrets.json: must have 'installed' or 'web' key")
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"saved": True, "path": str(SECRETS_PATH)}


# ─── Channel OAuth auth ───────────────────────────────────────────────────────

@router.get("/channels/{name}/auth")
async def channel_auth_status(name: str) -> dict[str, Any]:
    """Check OAuth2 token status for a channel."""
    name = _safe_name(name)
    token_path = _token_path(name)
    connected = token_path.exists() and token_path.stat().st_size > 0
    return {
        "channel": name,
        "connected": connected,
        "token_file": str(token_path) if connected else None,
    }


@router.post("/channels/{name}/auth")
async def channel_auth_connect(name: str) -> dict[str, Any]:
    """
    Start OAuth2 browser flow for a channel.
    Opens system browser once — token saved to config/oauth2/{name}_token.pickle.
    """
    name = _safe_name(name)
    ch_path = _channel_path(name)
    if not ch_path.exists():
        raise HTTPException(404, f"Channel not found: {name}")

    from modules.common import load_env
    load_env()

    def _do_auth() -> None:
        try:
            from clients.youtube_auth import get_youtube_service
            get_youtube_service(name)  # triggers browser flow if no token
        except Exception as exc:
            print(f"[channel auth] {name}: {exc}", flush=True)

    thread = threading.Thread(target=_do_auth, daemon=True)
    thread.start()

    return {
        "status": "auth_started",
        "channel": name,
        "message": "Browser opened for YouTube OAuth2. Complete consent, then refresh status.",
    }


@router.delete("/channels/{name}/auth")
async def channel_auth_revoke(name: str) -> dict[str, Any]:
    """Delete OAuth2 token for a channel (forces re-auth next time)."""
    name = _safe_name(name)
    token_path = _token_path(name)
    if token_path.exists():
        token_path.unlink()
        return {"status": "revoked", "channel": name}
    return {"status": "not_connected", "channel": name}


# ─── Channel branding ─────────────────────────────────────────────────────────

class BrandingRequest(BaseModel):
    description:      str | None = None
    keywords:         list[str] | None = None
    country:          str | None = None
    banner_path:      str | None = None
    trailer_video_id: str | None = None


_branding_jobs: dict[str, dict[str, Any]] = {}


@router.post("/channels/{name}/branding")
async def channel_apply_branding(name: str, req: BrandingRequest) -> dict[str, Any]:
    """
    Apply channel branding via YouTube API (async background task).
    Reads base config from channel JSON; req fields override config values.
    Returns job_id for polling.
    """
    import uuid
    name = _safe_name(name)
    ch_path = _channel_path(name)
    if not ch_path.exists():
        raise HTTPException(404, f"Channel not found: {name}")

    job_id = uuid.uuid4().hex[:8]
    _branding_jobs[job_id] = {"job_id": job_id, "status": "running", "channel": name, "error": ""}

    async def _run() -> None:
        try:
            import importlib.util
            from modules.common import load_env
            load_env()
            spec = importlib.util.spec_from_file_location(
                "channel_setup",
                ROOT / "modules" / "08c_channel_setup.py",
            )
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)                  # type: ignore[union-attr]
            setup_channel = mod.setup_channel
            await setup_channel(
                channel_config_path=ch_path,
                channel_name=name,
                description=req.description or None,
                keywords=req.keywords or None,
                country=req.country or None,
                banner_path=req.banner_path or None,
                trailer_video_id=req.trailer_video_id or None,
                generate=False,
                no_upload=False,
                dry_run=False,
            )
            _branding_jobs[job_id]["status"] = "done"
        except Exception as exc:
            _branding_jobs[job_id]["status"] = "failed"
            _branding_jobs[job_id]["error"] = str(exc)[:500]

    asyncio.create_task(_run(), name=f"branding-{job_id}")
    return _branding_jobs[job_id]


@router.get("/channels/{name}/branding/{job_id}")
async def channel_branding_status(name: str, job_id: str) -> dict[str, Any]:
    """Poll branding job status."""
    job = _branding_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Branding job not found: {job_id}")
    return job


# ─── Competitor analysis ───────────────────────────────────────────────────────

class CompetitorRequest(BaseModel):
    urls: list[str]   # YouTube channel URLs (any format)


def _extract_channel_identifier(url: str) -> tuple[str, str]:
    """
    Parse a YouTube channel URL and return (type, value) where type is:
      "id"      → UCxxxxxx (channel ID, use channels.list id=...)
      "handle"  → @handle  (use channels.list forHandle=...)
      "user"    → username (use channels.list forUsername=...)
      "search"  → name     (use search.list)
    """
    url = url.strip().rstrip("/")
    # Direct channel ID in URL
    for prefix in ("/channel/", "channel/"):
        idx = url.find(prefix)
        if idx != -1:
            cid = url[idx + len(prefix):].split("/")[0].split("?")[0]
            if cid.startswith("UC"):
                return ("id", cid)
    # @handle
    for prefix in ("/@", "@"):
        idx = url.find(prefix)
        if idx != -1:
            handle = url[idx + len(prefix):].split("/")[0].split("?")[0]
            return ("handle", handle)
    # /user/
    idx = url.find("/user/")
    if idx != -1:
        uname = url[idx + 6:].split("/")[0].split("?")[0]
        return ("user", uname)
    # /c/
    idx = url.find("/c/")
    if idx != -1:
        cname = url[idx + 3:].split("/")[0].split("?")[0]
        return ("search", cname)
    # Bare handle without URL
    if url.startswith("@"):
        return ("handle", url[1:])
    # Just a channel ID
    if url.startswith("UC") and len(url) > 10:
        return ("id", url)
    return ("search", url)


def _fetch_channel_data(service, ident_type: str, ident_val: str) -> dict | None:
    """Fetch channel snippet + brandingSettings via YouTube API."""
    try:
        if ident_type == "id":
            resp = service.channels().list(
                part="snippet,brandingSettings,statistics",
                id=ident_val,
            ).execute()
        elif ident_type == "handle":
            resp = service.channels().list(
                part="snippet,brandingSettings,statistics",
                forHandle=ident_val,
            ).execute()
        elif ident_type == "user":
            resp = service.channels().list(
                part="snippet,brandingSettings,statistics",
                forUsername=ident_val,
            ).execute()
        else:
            # search fallback
            sr = service.search().list(
                part="snippet", q=ident_val, type="channel", maxResults=1
            ).execute()
            items = sr.get("items", [])
            if not items:
                return None
            cid = items[0]["snippet"]["channelId"]
            resp = service.channels().list(
                part="snippet,brandingSettings,statistics",
                id=cid,
            ).execute()

        items = resp.get("items", [])
        if not items:
            return None
        item = items[0]
        snippet  = item.get("snippet", {})
        branding = item.get("brandingSettings", {}).get("channel", {})
        stats    = item.get("statistics", {})
        return {
            "title":        snippet.get("title", ""),
            "description":  snippet.get("description", ""),
            "country":      snippet.get("country", branding.get("country", "")),
            "keywords":     branding.get("keywords", ""),
            "subscribers":  int(stats.get("subscriberCount", 0)),
            "videos":       int(stats.get("videoCount", 0)),
        }
    except Exception as exc:
        return {"error": str(exc)}


_COMPETITOR_SYSTEM_PROMPT = """\
You are a YouTube channel branding expert.
You will receive data from several competitor channels in the same niche.
Your task: generate branding for a NEW channel that will compete in the same niche.

Rules:
- Description: max 900 characters, compelling, clear value proposition, ends with a call to subscribe. Write in the SAME LANGUAGE as the competitor descriptions.
- Keywords: 12-16 highly relevant tags (mix of broad + niche-specific). Return as JSON array.
- Do NOT copy competitor descriptions — create unique, better copy.
- Focus on what makes the niche appealing to viewers.

Return ONLY valid JSON:
{
  "description": "...",
  "keywords": ["tag1", "tag2", ...],
  "analysis": "2-3 sentences: what you observed about this niche"
}
"""


@router.post("/channels/{name}/analyze-competitors")
async def analyze_competitors(name: str, req: CompetitorRequest) -> dict[str, Any]:
    """
    1. Fetch competitor channel data via YouTube API
    2. Generate description + keywords via VoidAI LLM
    Returns: { description, keywords, analysis, competitors_found }
    """
    name = _safe_name(name)
    ch_path = _channel_path(name)
    if not ch_path.exists():
        raise HTTPException(404, f"Channel not found: {name}")
    if not req.urls:
        raise HTTPException(400, "No URLs provided")

    from modules.common import load_env
    load_env()

    # ── Fetch competitor data via YouTube API ─────────────────────────────────
    try:
        channel_config = json.loads(ch_path.read_text(encoding="utf-8"))
        from clients.youtube_auth import get_youtube_service_from_config
        service = get_youtube_service_from_config(name, channel_config)
    except Exception as exc:
        raise HTTPException(400, f"YouTube auth failed: {exc}") from exc

    competitor_data: list[dict] = []
    for url in req.urls[:8]:   # max 8 competitors
        ident_type, ident_val = _extract_channel_identifier(url)
        data = _fetch_channel_data(service, ident_type, ident_val)
        if data:
            competitor_data.append({"url": url, **data})

    if not competitor_data:
        raise HTTPException(422, "Could not fetch any channel data. Check URLs and YouTube auth.")

    found_ok = [c for c in competitor_data if "error" not in c]
    if not found_ok:
        errors = [c.get("error", "unknown") for c in competitor_data]
        raise HTTPException(422, f"All channels failed: {errors}")

    # ── Build LLM user message ────────────────────────────────────────────────
    lines = ["COMPETITOR CHANNELS:\n"]
    for i, c in enumerate(found_ok, 1):
        lines.append(f"--- Channel {i}: {c['title']} ---")
        lines.append(f"Subscribers: {c['subscribers']:,} | Videos: {c['videos']}")
        if c.get("country"):
            lines.append(f"Country: {c['country']}")
        if c.get("description"):
            lines.append(f"Description:\n{c['description'][:600]}")
        if c.get("keywords"):
            lines.append(f"Keywords: {c['keywords'][:300]}")
        lines.append("")

    user_msg = "\n".join(lines)

    # ── Call VoidAI ───────────────────────────────────────────────────────────
    try:
        from clients.voidai_client import VoidAIClient
        async with VoidAIClient() as client:
            raw = await client.complete(
                messages=[
                    {"role": "system", "content": _COMPETITOR_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                model="gpt-4.1-mini",
                max_tokens=1200,
                temperature=0.7,
            )
    except Exception as exc:
        raise HTTPException(500, f"LLM call failed: {exc}") from exc

    # ── Parse JSON response ───────────────────────────────────────────────────
    try:
        import re as _re
        # Extract JSON block if wrapped in markdown
        m = _re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise ValueError("No JSON in response")
        result = json.loads(m.group())
    except Exception:
        # Fallback: return raw text as analysis only
        result = {"description": "", "keywords": [], "analysis": raw[:500]}

    return {
        "description":        result.get("description", ""),
        "keywords":           result.get("keywords", []),
        "analysis":           result.get("analysis", ""),
        "competitors_found":  len(found_ok),
        "competitors_failed": len(competitor_data) - len(found_ok),
    }


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


# ─── Voices ───────────────────────────────────────────────────────────────────

@router.get("/voices")
async def list_voices() -> list[dict[str, Any]]:
    """
    List available TTS voices/templates from VoiceAPI.
    Falls back to returning voices from all channel configs if API unavailable.
    """
    # Try fetching live templates from VoiceAPI
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from modules.common import load_env
        load_env()
        from clients.voiceapi_client import VoiceAPIClient
        async with VoiceAPIClient(voidai_fallback=False) as client:
            templates = await client.list_voices()
        if templates:
            return [
                {
                    "id":   t.get("uuid") or t.get("id") or t.get("template_uuid", ""),
                    "name": t.get("name") or t.get("title") or t.get("id", "Unknown"),
                    "voice_id": t.get("voice_id", ""),
                    "source": "voiceapi",
                }
                for t in templates
            ]
    except Exception:
        pass

    # Fallback: collect unique voice_ids from channel configs
    voices: dict[str, dict] = {}
    for p in CHANNELS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            vid = data.get("voice_id", "")
            if vid and vid not in voices:
                voices[vid] = {
                    "id":      vid,
                    "name":    f"{data.get('channel_name', p.stem)} voice",
                    "voice_id": vid,
                    "source":  "channel_config",
                }
            # Also check multilang dict
            for lang_vid in data.get("voice_ids_multilang", {}).values():
                if lang_vid and lang_vid not in voices:
                    voices[lang_vid] = {
                        "id":      lang_vid,
                        "name":    lang_vid[:20],
                        "voice_id": lang_vid,
                        "source":  "channel_config",
                    }
        except Exception:
            continue

    return list(voices.values())
