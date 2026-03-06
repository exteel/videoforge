"""
Generate ALL images for a project using BetaImage (csv666.ru new AI).

Reads script.json, generates every image_prompt in every block,
saves to <project_dir>/images_newai/ with same naming as images/:
    block_001.png, block_001_1.png, block_001_2.png ...

Skips files that already exist (resumable).

Usage:
    python tools/gen_all_newai.py --project "6 RARE Things..."
    python tools/gen_all_newai.py  (uses most recent project)
    python tools/gen_all_newai.py --out images  (write directly to images/)
    python tools/gen_all_newai.py --mode quality
    python tools/gen_all_newai.py --style "Naruto anime style, cel-shaded" --out images_naruto
"""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_env, setup_logging
from clients.betaimage_client import BetaImageClient

load_env()
log = setup_logging("gen_all_newai")


# Style keywords where the "scene" ends and "style suffix" begins.
# When --style is provided these are stripped and replaced.
_STYLE_BOUNDARY_KEYWORDS = [
    "dramatic cinematic lighting",
    "digital concept art",
    "epic sci-fi concept art",
    "cinematic environment",
]


def _image_path(block_id: str, img_idx: int) -> str:
    """block_001 idx=0 → 'block_001.png'; idx=1 → 'block_001_1.png'."""
    if img_idx == 0:
        return f"{block_id}.png"
    return f"{block_id}_{img_idx}.png"


def _apply_style(prompt: str, style: str) -> str:
    """
    Strip the existing style suffix from a prompt and replace with new style.

    Finds the first occurrence of any _STYLE_BOUNDARY_KEYWORDS and cuts there,
    then appends the new style. If no boundary is found, appends style directly.
    """
    prompt = prompt.strip().rstrip(",").strip()
    cut_pos = len(prompt)
    for kw in _STYLE_BOUNDARY_KEYWORDS:
        idx = prompt.lower().find(kw.lower())
        if idx != -1 and idx < cut_pos:
            cut_pos = idx

    scene_part = prompt[:cut_pos].strip().rstrip(",").strip()
    return f"{scene_part}, {style}"


async def generate_image(
    client: BetaImageClient,
    prompt: str,
    out_path: Path,
    mode: str,
    label: str,
) -> bool:
    """Generate one image. Returns True on success, False on failure."""
    if out_path.exists() and out_path.stat().st_size > 5_000:
        log.info("  SKIP (exists): %s", out_path.name)
        return True

    t0 = time.monotonic()
    try:
        await client.generate_text2img(prompt, mode=mode, output_path=out_path)
        elapsed = time.monotonic() - t0
        size_kb = out_path.stat().st_size // 1024
        log.info("  ✓ %s → %s (%.1fs, %dKB)", label, out_path.name, elapsed, size_kb)
        return True
    except Exception as exc:
        log.error("  ✗ %s FAILED: %s", label, exc)
        return False


async def main(project_name: str | None, out_subdir: str, mode: str, style: str | None) -> None:
    projects_dir = ROOT / "projects"

    # Find project
    if project_name:
        project_dir = projects_dir / project_name
        if not project_dir.exists():
            # Try partial match
            matches = [d for d in projects_dir.iterdir() if d.is_dir() and project_name.lower() in d.name.lower()]
            if not matches:
                log.error("Project not found: %s", project_name)
                sys.exit(1)
            project_dir = matches[0]
    else:
        # Use most recently modified project
        dirs = sorted(projects_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
        dirs = [d for d in dirs if d.is_dir() and (d / "script.json").exists()]
        if not dirs:
            log.error("No projects with script.json found")
            sys.exit(1)
        project_dir = dirs[0]

    script_path = project_dir / "script.json"
    if not script_path.exists():
        log.error("script.json not found in %s", project_dir)
        sys.exit(1)

    log.info("Project: %s", project_dir.name)

    # Load script
    script = json.loads(script_path.read_text(encoding="utf-8"))
    blocks = script.get("blocks", [])

    # Collect all jobs: (block_id, img_idx, prompt, out_path)
    out_dir = project_dir / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, int, str, Path]] = []
    for block in blocks:
        block_id = block["id"]
        prompts = block.get("image_prompts", [])
        if not prompts:
            # Fallback to single image_prompt
            single = block.get("image_prompt", "").strip()
            if single:
                prompts = [single]
        for idx, prompt in enumerate(prompts):
            if not prompt.strip():
                continue
            final_prompt = _apply_style(prompt.strip(), style) if style else prompt.strip()
            fname = _image_path(block_id, idx)
            out_path = out_dir / fname
            jobs.append((block_id, idx, final_prompt, out_path))

    total = len(jobs)
    log.info("=" * 60)
    log.info("BetaImage — %d images to generate, mode=%s", total, mode)
    if style:
        log.info("Style override: %s", style[:80])
    log.info("Output dir: %s", out_dir)
    log.info("=" * 60)

    if total == 0:
        log.warning("No image prompts found in script.json")
        return

    # Count already done
    already_done = sum(1 for _, _, _, p in jobs if p.exists() and p.stat().st_size > 5_000)
    to_generate = total - already_done
    log.info("Already done: %d / %d  →  generating %d", already_done, total, to_generate)

    if to_generate == 0:
        log.info("All images already exist. Done!")
        return

    t_start = time.monotonic()
    ok_count = 0
    fail_count = 0

    async with BetaImageClient(mode=mode) as client:
        # Run all in parallel (semaphore MAX_CONCURRENT=3 inside client)
        tasks = [
            generate_image(
                client,
                prompt,
                out_path,
                mode,
                f"{block_id}[{idx}]",
            )
            for block_id, idx, prompt, out_path in jobs
        ]
        results = await asyncio.gather(*tasks)

    for success in results:
        if success:
            ok_count += 1
        else:
            fail_count += 1

    elapsed_total = time.monotonic() - t_start

    log.info("")
    log.info("=" * 60)
    log.info("Done in %.1fs  ✓ %d generated  ✗ %d failed", elapsed_total, ok_count, fail_count)
    log.info("Output: %s", out_dir)

    # Summary by block
    generated = sorted(out_dir.glob("block_*.png"))
    total_kb = sum(f.stat().st_size for f in generated) // 1024
    log.info("Files: %d  Total size: %d KB (~%d MB)", len(generated), total_kb, total_kb // 1024)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate all images via BetaImage")
    parser.add_argument("--project", default=None, help="Project name (partial match ok)")
    parser.add_argument(
        "--out", default="images_newai",
        help="Output subdirectory inside project (default: images_newai; use 'images' to overwrite)"
    )
    parser.add_argument("--mode", default="fast", choices=["fast", "quality"], help="Generation mode")
    parser.add_argument("--style", default=None, help="Style suffix to replace existing style tags (e.g. 'Naruto anime style, cel-shaded')")
    args = parser.parse_args()

    asyncio.run(main(args.project, args.out, args.mode, args.style))
