"""
VideoForge Backend — Filesystem routes.

POST /api/fs/open  → open a folder in Windows Explorer
"""

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["filesystem"])


class OpenFolderRequest(BaseModel):
    path: str


@router.post("/fs/open")
async def open_folder(req: OpenFolderRequest) -> dict:
    """Open a folder in Windows Explorer. Windows-only."""
    if sys.platform != "win32":
        raise HTTPException(501, "Open folder is only supported on Windows")

    p = Path(req.path)
    if not p.exists():
        raise HTTPException(404, f"Path not found: {p}")

    # explorer.exe accepts both files and directories
    subprocess.Popen(["explorer", str(p)])
    return {"status": "opened", "path": str(p)}
