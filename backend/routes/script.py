"""
VideoForge Backend — Script viewer / editor routes.

GET  /api/script?source_dir=...   → read script.json for a project
PUT  /api/script?source_dir=...   → save an edited script.json
GET  /api/script/exists?source_dir=...  → check if script.json exists
"""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

router = APIRouter(tags=["script"])

ROOT = Path(__file__).parent.parent.parent


def _resolve_script_path(source_dir: str) -> Path:
    """
    Return the path to script.json for a given source_dir.

    Maps source_dir → projects/{source_dir.name}/script.json
    (same logic as pipeline.py: proj = ROOT / "projects" / source_dir.name).
    """
    src = Path(source_dir)
    proj = ROOT / "projects" / src.name
    return proj / "script.json"


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get("/script")
async def get_script(source_dir: str) -> dict:
    """Return parsed script.json for the given source_dir."""
    sp = _resolve_script_path(source_dir)
    if not sp.exists():
        raise HTTPException(
            404,
            f"script.json not found. "
            f"Run pipeline step 1 first. (looked at: {sp})",
        )
    try:
        data: dict = json.loads(sp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Malformed script.json: {exc}") from exc
    return data


# ── Exists ────────────────────────────────────────────────────────────────────

@router.get("/script/exists")
async def script_exists(source_dir: str) -> dict:
    """Quick check whether script.json exists for a given source_dir."""
    sp = _resolve_script_path(source_dir)
    return {"exists": sp.exists(), "path": str(sp)}


# ── Save ──────────────────────────────────────────────────────────────────────

@router.put("/script")
async def save_script(
    source_dir: str,
    script: Any = Body(..., media_type="application/json"),
) -> dict:
    """
    Overwrite script.json with the provided body.

    The project directory must already exist (i.e. pipeline step 1 was run).
    Returns {"saved": true, "path": "..."}.
    """
    if not isinstance(script, dict):
        raise HTTPException(400, "Request body must be a JSON object (script dict)")
    if not script.get("blocks"):
        raise HTTPException(400, "script.blocks is required and must not be empty")

    sp = _resolve_script_path(source_dir)
    if not sp.parent.exists():
        raise HTTPException(
            404,
            f"Project directory not found: {sp.parent}. "
            f"Run pipeline step 1 first.",
        )

    sp.write_text(
        json.dumps(script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"saved": True, "path": str(sp)}
