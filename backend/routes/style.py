"""
VideoForge — Backend Route: Style Analyzer.

POST /api/style/analyze
  Accepts a reference image (multipart/form-data).
  Calls VoidAI Vision (gpt-4.1) to extract a compact image_style descriptor.
  Returns {"style": "cinematic photorealism, dramatic side lighting, ..."}.

Usage in frontend:
  User pastes/drops a reference image → clicks "Аналізувати стиль"
  → response fills the Image Style field for that pipeline run.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_env, setup_logging

log = setup_logging("style_route")

router = APIRouter()

# ─── Vision prompt ────────────────────────────────────────────────────────────

STYLE_EXTRACT_PROMPT = """Analyze this reference image and extract its visual style as a compact generation tag.

Return ONLY a single line of comma-separated style descriptors (15-50 words). No explanation, no preamble, no quotes.

Cover these dimensions in order of visual prominence:
1. Artistic medium / rendering style (e.g. "cinematic photorealism", "oil painting", "documentary photography")
2. Lighting quality (e.g. "dramatic chiaroscuro", "soft diffused light", "golden hour backlight")
3. Color palette mood (e.g. "warm amber tones", "desaturated blue-grey", "rich jewel tones")
4. Compositional approach (e.g. "wide establishing shot", "tight close-up", "symmetrical framing")
5. Atmosphere / setting (e.g. "baroque stone architecture", "industrial brutalism", "misty forests")
6. Technical quality (e.g. "4K ultra-detailed", "shallow depth of field", "film grain")

Example output:
cinematic photorealism, dramatic side lighting, warm amber tones, wide establishing shot, baroque stone architecture, 4K ultra-detailed, shallow depth of field

Now extract the style from the provided image:"""


# ─── Models ───────────────────────────────────────────────────────────────────

class StyleResponse(BaseModel):
    style: str


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/style/analyze", response_model=StyleResponse)
async def analyze_style(image: UploadFile = File(...)) -> dict:
    """
    Analyze a reference image and return a compact image_style descriptor.

    The returned string is ready to paste into the 'Image Style' field of the
    pipeline run form — it will be appended to every [IMAGE_PROMPT:] tag.

    Args:
        image: Multipart image file (jpg/png/webp/gif, max 20 MB).

    Returns:
        {"style": "cinematic photorealism, dramatic lighting, ..."}
    """
    load_env()

    # ── MIME type resolution ──────────────────────────────────────────────────
    content_type = image.content_type or ""
    if content_type.startswith("image/"):
        mime = content_type.split("/")[1]
        if mime == "jpg":
            mime = "jpeg"
    else:
        # Fall back to extension detection (some browsers send application/octet-stream)
        suffix = Path(image.filename or "").suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            raise HTTPException(
                status_code=400,
                detail="File must be an image (jpg/png/webp/gif)",
            )
        mime = "jpeg" if suffix in (".jpg", ".jpeg") else suffix.lstrip(".")

    # ── Read + validate ───────────────────────────────────────────────────────
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(image_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 20 MB)")

    # ── Build vision message (inline base64) ──────────────────────────────────
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": STYLE_EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
        ],
    }

    # ── Call VoidAI Vision ────────────────────────────────────────────────────
    from clients.voidai_client import VoidAIClient  # noqa: PLC0415

    try:
        async with VoidAIClient() as client:
            style = await client.vision_completion(
                messages=[message],
                model="gpt-4.1",
                max_tokens=200,
                temperature=0.3,
            )
        # Strip any surrounding quotes the model may add
        style = style.strip().strip('"').strip("'").strip()
        log.info(
            "Style extracted (%d chars): %s",
            len(style),
            style[:100],
        )
        return {"style": style}

    except Exception as exc:
        log.error("Style analysis failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Style analysis failed: {exc}",
        ) from exc
