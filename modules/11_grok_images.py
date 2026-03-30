"""
VideoForge Module 11 — Grok Scene Images

Generates static images for each Grok scene from grok_scenes.json.
These images serve as input frames for Grok image-to-video generation.

Usage:
    python modules/11_grok_images.py \
        --scenes projects/channel/video/grok_scenes.json \
        --channel config/channels/PsychologySimplified.json \
        [--output-dir grok_images/] \
        [--skip-existing] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.common import setup_logging, load_env, load_channel_config, require_env

log = setup_logging("grok_images")


@dataclass
class ImageResult:
    scene_id: str
    prompt: str
    path: str | None = None
    error: str | None = None


@dataclass
class GrokImagesSummary:
    total: int
    generated: int
    skipped: int
    failed: int
    output_dir: str
    elapsed: float
    results: list[ImageResult] = field(default_factory=list)


async def generate_grok_images(
    scenes_path: str | Path,
    channel_config_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    skip_existing: bool = True,
    dry_run: bool = False,
    image_backend: str = "voiceimage",
    voidai_model: str = "gemini-2.5-flash",
    progress_callback=None,
) -> GrokImagesSummary:
    """Generate one image per Grok scene."""
    load_env()
    t0 = time.time()

    scenes_path = Path(scenes_path)
    channel_config = load_channel_config(str(channel_config_path))

    with open(scenes_path, encoding="utf-8") as f:
        plan = json.load(f)

    if output_dir is None:
        output_dir = scenes_path.parent / "grok_images"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    global_suffix = plan.get("global_suffix", "")
    image_style = channel_config.get("image_style", "")
    scenes = plan.get("scenes", [])

    log.info("Generating %d images for Grok scenes", len(scenes))

    if dry_run:
        log.info("Dry run — skipping image generation")
        return GrokImagesSummary(
            total=len(scenes), generated=0, skipped=0, failed=0,
            output_dir=str(output_dir), elapsed=time.time() - t0,
        )

    # Select image backend
    _backend = image_backend.lower().strip()
    log.info("Image backend: %s", _backend)

    client = None
    client_ctx = None  # for async with

    if _backend == "voiceimage":
        from clients.voiceimage_client import VoiceImageClient
        client = VoiceImageClient()
        client_ctx = client
    elif _backend == "voidai":
        from clients.voidai_client import VoidAIClient
        api_key = require_env("VOIDAI_API_KEY")
        base_url = require_env("VOIDAI_BASE_URL")
        client = VoidAIClient(api_key=api_key, base_url=base_url)
        client_ctx = client
    else:  # wavespeed
        from clients.wavespeed_client import WaveSpeedClient
        api_key = require_env("WAVESPEED_API_KEY")
        client = WaveSpeedClient(api_key=api_key)
        client_ctx = client

    results = []
    generated = 0
    skipped = 0
    failed = 0
    semaphore = asyncio.Semaphore(3)

    async def _gen_one(scene: dict, active_client) -> ImageResult:
        nonlocal generated, skipped, failed

        scene_id = scene["id"]
        ext = "png" if _backend == "voiceimage" else "jpg"
        img_path = output_dir / f"{scene_id}.{ext}"

        if skip_existing and img_path.exists() and img_path.stat().st_size > 1000:
            skipped += 1
            log.debug("Skip existing: %s", img_path)
            return ImageResult(scene_id=scene_id, prompt="", path=str(img_path))

        prompt = scene.get("grok_prompt", "")
        full_prompt = f"{prompt}, {image_style}" if image_style else prompt
        if len(full_prompt) > 500:
            full_prompt = full_prompt[:500]

        async with semaphore:
            try:
                log.info("[%d/%d] Generating: %s (%s)", generated + skipped + failed + 1, len(scenes), scene_id, _backend)

                if _backend == "voiceimage":
                    await active_client.generate_text2img(
                        full_prompt,
                        aspect_ratio="16:9",
                        output_path=str(img_path),
                    )
                elif _backend == "voidai":
                    if "flash-image" in voidai_model or "gemini" in voidai_model:
                        # Gemini image models use chat completions with images in response
                        import base64 as _b64
                        import httpx as _httpx
                        _api_key = require_env("VOIDAI_API_KEY")
                        _base_url = require_env("VOIDAI_BASE_URL")
                        async with _httpx.AsyncClient(timeout=60) as _http:
                            _resp = await _http.post(
                                f"{_base_url}/chat/completions",
                                headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
                                json={
                                    "model": voidai_model,
                                    "messages": [{"role": "user", "content": f"Generate image: {full_prompt}"}],
                                    "max_tokens": 4096,
                                },
                            )
                            _data = _resp.json()
                            _msg = _data["choices"][0]["message"]
                            _images = _msg.get("images", [])
                            if not _images:
                                raise ValueError(f"No images in response for {scene_id}")
                            _img_info = _images[0]
                            _url = _img_info.get("image_url", {}).get("url", "") if isinstance(_img_info.get("image_url"), dict) else _img_info.get("image_url", "")
                            if _url.startswith("data:image"):
                                _b64_start = _url.index("base64,") + 7
                                img_path.write_bytes(_b64.b64decode(_url[_b64_start:]))
                            elif _url.startswith("http"):
                                _dl = await _http.get(_url)
                                img_path.write_bytes(_dl.content)
                            else:
                                raise ValueError(f"Unknown image format for {scene_id}")
                    else:
                        await active_client.generate_image(
                            full_prompt,
                            model=voidai_model,
                            size="1536x1024",
                            output_path=str(img_path),
                        )
                else:  # wavespeed
                    await active_client.generate_text2img(
                        full_prompt,
                        width=1280,
                        height=720,
                        output_path=str(img_path),
                    )

                generated += 1
                size = img_path.stat().st_size if img_path.exists() else 0
                log.info("[%s] Saved: %s (%d bytes)", scene_id, img_path, size)
                return ImageResult(scene_id=scene_id, prompt=full_prompt, path=str(img_path))
            except Exception as e:
                failed += 1
                log.error("[%s] Failed: %s", scene_id, e)
                return ImageResult(scene_id=scene_id, prompt=full_prompt, error=str(e))

    # Run all in parallel with semaphore, using async context manager for client
    async def _run_all():
        if client_ctx and hasattr(client_ctx, '__aenter__'):
            async with client_ctx as active:
                tasks = [_gen_one(scene, active) for scene in scenes]
                return await asyncio.gather(*tasks)
        else:
            tasks = [_gen_one(scene, client) for scene in scenes]
            return await asyncio.gather(*tasks)

    results = await _run_all()

    # Update grok_scenes.json with image paths
    for scene in scenes:
        matching = [r for r in results if r.scene_id == scene["id"] and r.path]
        if matching:
            scene["image_path"] = matching[0].path

    with open(scenes_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    # Also update grok_loop_import.json if it exists
    import_path = scenes_path.parent / "grok_loop_import.json"
    if import_path.exists():
        with open(import_path, encoding="utf-8") as f:
            export = json.load(f)
        for i, scene in enumerate(scenes):
            if i < len(export.get("scenes", [])):
                img_result = [r for r in results if r.scene_id == scene["id"] and r.path]
                if img_result:
                    # Read image as base64 for extension import
                    import base64
                    img_p = Path(img_result[0].path)
                    if img_p.exists():
                        b64 = base64.b64encode(img_p.read_bytes()).decode()
                        export["scenes"][i]["image"] = {
                            "dataUrl": f"data:image/jpeg;base64,{b64}",
                            "fileName": img_p.name,
                        }
        with open(import_path, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        log.info("Updated grok_loop_import.json with %d image references", generated)

    elapsed = time.time() - t0
    log.info(
        "Done: %d generated, %d skipped, %d failed (%.1fs)",
        generated, skipped, failed, elapsed,
    )

    return GrokImagesSummary(
        total=len(scenes),
        generated=generated,
        skipped=skipped,
        failed=failed,
        output_dir=str(output_dir),
        elapsed=elapsed,
        results=list(results),
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate images for Grok scenes")
    parser.add_argument("--scenes", required=True, help="Path to grok_scenes.json")
    parser.add_argument("--channel", required=True, help="Path to channel config")
    parser.add_argument("--output-dir", help="Output directory for images")
    parser.add_argument("--backend", default="voiceimage", choices=["voiceimage", "voidai", "wavespeed"], help="Image backend")
    parser.add_argument("--voidai-model", default="gemini-2.5-flash", help="VoidAI image model")
    parser.add_argument("--skip-existing", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    summary = await generate_grok_images(
        args.scenes, args.channel,
        output_dir=args.output_dir,
        skip_existing=args.skip_existing,
        dry_run=args.dry_run,
        image_backend=args.backend,
        voidai_model=args.voidai_model,
    )
    log.info("Summary: %s", summary)


if __name__ == "__main__":
    asyncio.run(_main())
