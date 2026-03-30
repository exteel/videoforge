"""
VideoForge Module 10 — Grok Loop Export

Converts grok_scenes.json into a format compatible with the
grok-video-loop Chrome extension's preset import system.

Output: grok_loop_import.json that can be imported via
the extension's "Import Config" button.

Usage:
    python modules/10_grok_export.py \\
        --scenes projects/channel/video/grok_scenes.json \\
        --channel config/channels/PsychologySimplified.json \\
        [--output grok_loop_import.json]
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent))

from modules.common import setup_logging, load_channel_config

log = setup_logging("grok_export")


@dataclass
class GrokExportSummary:
    total_scenes: int
    total_duration: float
    output_path: str
    elapsed: float


def export_for_grok_loop(
    scenes_path: str | Path,
    channel_config_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> GrokExportSummary:
    """
    Convert grok_scenes.json to grok-video-loop import format.

    The grok-video-loop extension uses this format for presets:
    {
        "scenes": [{"prompt": "...", "image": null}],
        "settings": {"timeout": 120000, "globalPrompt": "...", ...}
    }
    """
    t0 = time.time()

    scenes_path = Path(scenes_path)
    channel_config = load_channel_config(str(channel_config_path))

    with open(scenes_path, encoding="utf-8") as f:
        plan = json.load(f)

    if output_path is None:
        output_path = scenes_path.parent / "grok_loop_import.json"
    output_path = Path(output_path)

    grok_cfg = channel_config.get("grok", {})
    global_suffix = plan.get("global_suffix", grok_cfg.get("global_suffix", ""))
    resolution = grok_cfg.get("resolution", "720p")
    extend_duration = grok_cfg.get("extend_duration", "6s")

    # Build scenes array for extension
    ext_scenes = []
    for scene in plan.get("scenes", []):
        prompt = scene.get("grok_prompt", "")
        duration = scene.get("estimated_duration_sec", 6)
        scene_type = scene.get("scene_type", "literal")

        # Add duration hint to prompt if scene is long
        if duration > 10:
            prompt += ", slow smooth camera movement"

        ext_scenes.append({
            "prompt": prompt,
            "image": None,  # No pre-set image; extension chains frames
        })

    # Build settings for extension
    ext_settings = {
        "timeout": 120000,
        "maxDelay": 5,
        "retryLimit": 3,
        "moderationRetryLimit": 2,
        "upscale": resolution == "720p",
        "autoDownload": True,
        "autoSkip": False,
        "reuseInitialImage": False,
        "continueOnFailure": True,
        "pauseOnModeration": True,
        "pauseAfterScene": False,
        "showDashboard": True,
        "showDebugLogs": True,
        "extendDuration": extend_duration,
        "globalPrompt": global_suffix,
        "filenamePrefix": plan.get("character_description", "scene")[:20].replace(" ", "_"),
    }

    # Build export object
    export = {
        "scenes": ext_scenes,
        "settings": ext_settings,
        "timestamp": int(time.time() * 1000),
        "_metadata": {
            "source": "VideoForge Module 10",
            "total_scenes": len(ext_scenes),
            "total_duration_sec": plan.get("total_duration_sec", 0),
            "global_suffix": global_suffix,
        },
    }

    # Also create a human-readable prompt list
    prompt_list_path = output_path.parent / "grok_prompts.txt"
    with open(prompt_list_path, "w", encoding="utf-8") as f:
        f.write(f"# Grok Video Prompts ({len(ext_scenes)} scenes)\n")
        f.write(f"# Global Suffix: {global_suffix}\n")
        f.write(f"# Total Duration: {plan.get('total_duration_sec', 0):.0f}s\n\n")
        for i, scene in enumerate(plan.get("scenes", []), 1):
            dur = scene.get("estimated_duration_sec", 0)
            stype = scene.get("scene_type", "literal")
            narr = scene.get("narration_excerpt", "")[:80]
            prompt = scene.get("grok_prompt", "")
            f.write(f"--- Scene {i:02d} ({dur:.1f}s) [{stype}] ---\n")
            f.write(f"Narration: {narr}...\n")
            f.write(f"Prompt: {prompt}\n\n")

    # Write export JSON
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    log.info(
        "Grok export complete: %d scenes -> %s + %s (%.1fs)",
        len(ext_scenes), output_path, prompt_list_path, elapsed,
    )

    return GrokExportSummary(
        total_scenes=len(ext_scenes),
        total_duration=plan.get("total_duration_sec", 0),
        output_path=str(output_path),
        elapsed=elapsed,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Export grok_scenes.json to grok-video-loop format",
    )
    parser.add_argument("--scenes", required=True, help="Path to grok_scenes.json")
    parser.add_argument("--channel", required=True, help="Path to channel config JSON")
    parser.add_argument("--output", help="Output path for grok_loop_import.json")
    args = parser.parse_args()

    summary = export_for_grok_loop(
        args.scenes,
        args.channel,
        output_path=args.output,
    )
    log.info("Summary: %s", summary)


if __name__ == "__main__":
    _main()
