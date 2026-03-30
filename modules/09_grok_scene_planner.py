"""
VideoForge Module 09 — Grok Scene Planner

Converts script.json blocks (with audio_duration from voice generation)
into Grok video scene prompts with precise timing boundaries.

Each scene = one visual idea synced to narration timing.
Output: grok_scenes.json compatible with grok-video-loop Chrome extension.

Features:
    - LLM-driven scene splitting based on narration content
    - Word-boundary timing synced to audio duration
    - Grok prompt format (Subject + Action + Camera + Mood)
    - Visual-to-narration correlation (literal/metaphor/text_card)
    - Global suffix for style consistency
    - Export format compatible with grok-video-loop extension

Usage:
    python modules/09_grok_scene_planner.py \\
        --script projects/channel/video/script.json \\
        --channel config/channels/PsychologySimplified.json \\
        [--output grok_scenes.json] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modules.common import (
    setup_logging,
    load_env,
    load_channel_config,
    require_env,
    PROMPTS_DIR,
)

log = setup_logging("grok_scene_planner")


# ── Data models ──────────────────────────────────────────────────────────────


@dataclass
class GrokScene:
    id: str
    block_id: str
    scene_type: str  # literal | metaphor | text_card | transition
    narration_excerpt: str
    grok_prompt: str
    start_word: int
    end_word: int
    word_count: int
    estimated_duration_sec: float


@dataclass
class GrokScenePlan:
    global_suffix: str
    character_description: str
    scenes: list[GrokScene]
    total_scenes: int
    total_duration_sec: float
    blocks_processed: int


@dataclass
class GrokSceneSummary:
    total_scenes: int
    total_duration: float
    blocks_processed: int
    failed_blocks: int
    output_path: str
    elapsed: float


# ── Prompt loading ───────────────────────────────────────────────────────────


def _load_system_prompt() -> str:
    """Load the grok_scene_planner system prompt."""
    prompt_path = PROMPTS_DIR / "grok_scene_planner_v1.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Grok scene planner prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


# ── Block preparation ────────────────────────────────────────────────────────


def _prepare_blocks_for_llm(script: dict) -> list[dict]:
    """Extract narration blocks with timing info for LLM input."""
    blocks = []
    for block in script.get("blocks", []):
        narration = block.get("narration", "").strip()
        if not narration:
            continue
        audio_dur = block.get("audio_duration")
        if not audio_dur or audio_dur <= 0:
            # Estimate from word count at 150 wpm
            wc = len(narration.split())
            audio_dur = (wc / 150) * 60

        blocks.append({
            "block_id": block["id"],
            "order": block["order"],
            "narration": narration,
            "audio_duration": round(audio_dur, 1),
            "word_count": len(narration.split()),
        })
    return blocks


# ── LLM call ─────────────────────────────────────────────────────────────────


async def _call_llm_for_scenes(
    blocks: list[dict],
    character_desc: str,
    visual_style: str,
    llm_model: str,
) -> list[dict]:
    """Call LLM to generate scene splits for all blocks."""
    from clients.voidai_client import VoidAIClient

    api_key = require_env("VOIDAI_API_KEY")
    base_url = require_env("VOIDAI_BASE_URL")
    client = VoidAIClient(api_key=api_key, base_url=base_url)

    system_prompt = _load_system_prompt()

    user_content = json.dumps({
        "CHARACTER_DESCRIPTION": character_desc,
        "VISUAL_STYLE": visual_style,
        "blocks": blocks,
    }, ensure_ascii=False, indent=2)

    log.info(
        "Calling LLM for scene planning: %d blocks, %d total words, model=%s",
        len(blocks),
        sum(b["word_count"] for b in blocks),
        llm_model,
    )

    response = await client.chat_completion(
        model=llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.4,
        max_tokens=16000,
    )

    raw_text = response.get("choices", [{}])[0].get("message", {}).get("content", "")

    # Extract JSON from response (may be wrapped in ```json blocks)
    json_text = raw_text.strip()
    if json_text.startswith("```"):
        # Remove markdown code fences
        lines = json_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        json_text = "\n".join(lines)

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        log.error("Failed to parse LLM response as JSON: %s", e)
        log.error("Raw response (first 500 chars): %s", raw_text[:500])
        raise ValueError(f"LLM returned invalid JSON: {e}") from e

    scenes = result.get("scenes", [])
    log.info("LLM returned %d scenes", len(scenes))
    return scenes


# ── Post-processing & validation ─────────────────────────────────────────────


def _validate_and_fix_scenes(
    scenes: list[dict],
    blocks: list[dict],
    max_scene_duration: float = 15.0,
    min_scene_duration: float = 3.0,
) -> list[GrokScene]:
    """Validate scenes and fix common issues."""
    block_map = {b["block_id"]: b for b in blocks}
    validated = []
    scene_counter = 0

    for scene in scenes:
        block_id = scene.get("block_id", "")
        if block_id not in block_map:
            log.warning("Scene references unknown block_id: %s, skipping", block_id)
            continue

        block = block_map[block_id]
        word_count = scene.get("word_count", 0)
        if word_count <= 0:
            end_w = scene.get("end_word", 0)
            start_w = scene.get("start_word", 0)
            word_count = end_w - start_w + 1

        # Recalculate duration based on word proportion
        proportion = word_count / max(block["word_count"], 1)
        duration = round(proportion * block["audio_duration"], 1)

        # Clamp duration
        if duration > max_scene_duration:
            duration = max_scene_duration
        if duration < min_scene_duration and scene.get("scene_type") != "transition":
            duration = min_scene_duration

        scene_counter += 1
        validated.append(GrokScene(
            id=f"scene_{scene_counter:03d}",
            block_id=block_id,
            scene_type=scene.get("scene_type", "literal"),
            narration_excerpt=scene.get("narration_excerpt", "")[:200],
            grok_prompt=scene.get("grok_prompt", "")[:200],
            start_word=scene.get("start_word", 0),
            end_word=scene.get("end_word", 0),
            word_count=word_count,
            estimated_duration_sec=duration,
        ))

    # Validate prompt length
    for s in validated:
        prompt_words = len(s.grok_prompt.split())
        if prompt_words > 50:
            log.warning(
                "Scene %s prompt too long (%d words), truncating to 50",
                s.id, prompt_words,
            )
            s.grok_prompt = " ".join(s.grok_prompt.split()[:50])

    log.info(
        "Validated %d scenes (%.1f sec total)",
        len(validated),
        sum(s.estimated_duration_sec for s in validated),
    )
    return validated


# ── Main entry point ─────────────────────────────────────────────────────────


async def plan_grok_scenes(
    script_path: str | Path,
    channel_config_path: str | Path,
    *,
    output_path: str | Path | None = None,
    dry_run: bool = False,
    llm_preset: str = "high",
    progress_callback: Any | None = None,
) -> GrokSceneSummary:
    """
    Generate Grok video scene prompts from script.json.

    Args:
        script_path: Path to script.json (must have audio_duration per block)
        channel_config_path: Path to channel config JSON
        output_path: Where to write grok_scenes.json (default: same dir as script)
        dry_run: If True, skip LLM call and return empty summary
        llm_preset: LLM quality preset (max/high/balanced/bulk)
        progress_callback: Optional callback for progress updates

    Returns:
        GrokSceneSummary with results
    """
    load_env()
    t0 = time.time()

    script_path = Path(script_path)
    channel_config = load_channel_config(str(channel_config_path))

    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)

    # Output path default: alongside script.json
    if output_path is None:
        output_path = script_path.parent / "grok_scenes.json"
    output_path = Path(output_path)

    # Extract Grok config from channel
    grok_cfg = channel_config.get("grok", {})
    global_suffix = grok_cfg.get("global_suffix", "")
    character_desc = grok_cfg.get("character_description", "A white round-headed cartoon character with simple features")
    max_scene_dur = grok_cfg.get("max_scene_duration", 12.0)
    min_scene_dur = grok_cfg.get("min_scene_duration", 3.0)

    # Resolve LLM model
    from modules.common import get_llm_preset
    llm_models = get_llm_preset(channel_config, llm_preset)
    llm_model = llm_models.get("script", "claude-sonnet-4-5-20250929")

    # Prepare blocks
    blocks = _prepare_blocks_for_llm(script)
    total_words = sum(b["word_count"] for b in blocks)
    total_duration = sum(b["audio_duration"] for b in blocks)

    log.info(
        "Planning Grok scenes: %d blocks, %d words, %.0f sec audio",
        len(blocks), total_words, total_duration,
    )

    if dry_run:
        log.info("Dry run — skipping LLM call")
        return GrokSceneSummary(
            total_scenes=0,
            total_duration=total_duration,
            blocks_processed=len(blocks),
            failed_blocks=0,
            output_path=str(output_path),
            elapsed=time.time() - t0,
        )

    # Call LLM
    raw_scenes = await _call_llm_for_scenes(
        blocks, character_desc, global_suffix, llm_model,
    )

    # Validate and fix
    scenes = _validate_and_fix_scenes(
        raw_scenes, blocks, max_scene_dur, min_scene_dur,
    )

    # Build output
    plan = {
        "global_suffix": global_suffix,
        "character_description": character_desc,
        "total_scenes": len(scenes),
        "total_duration_sec": round(sum(s.estimated_duration_sec for s in scenes), 1),
        "blocks_processed": len(blocks),
        "scenes": [
            {
                "id": s.id,
                "block_id": s.block_id,
                "scene_type": s.scene_type,
                "narration_excerpt": s.narration_excerpt,
                "grok_prompt": s.grok_prompt,
                "start_word": s.start_word,
                "end_word": s.end_word,
                "word_count": s.word_count,
                "estimated_duration_sec": s.estimated_duration_sec,
            }
            for s in scenes
        ],
    }

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    log.info(
        "Grok scene plan complete: %d scenes, %.0f sec, written to %s (%.1fs)",
        len(scenes), plan["total_duration_sec"], output_path, elapsed,
    )

    return GrokSceneSummary(
        total_scenes=len(scenes),
        total_duration=plan["total_duration_sec"],
        blocks_processed=len(blocks),
        failed_blocks=0,
        output_path=str(output_path),
        elapsed=elapsed,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Grok video scene prompts from script.json",
    )
    parser.add_argument("--script", required=True, help="Path to script.json")
    parser.add_argument("--channel", required=True, help="Path to channel config JSON")
    parser.add_argument("--output", help="Output path for grok_scenes.json")
    parser.add_argument("--preset", default="high", help="LLM preset (max/high/balanced)")
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM call")
    args = parser.parse_args()

    summary = await plan_grok_scenes(
        args.script,
        args.channel,
        output_path=args.output,
        dry_run=args.dry_run,
        llm_preset=args.preset,
    )
    log.info("Summary: %s", summary)


if __name__ == "__main__":
    asyncio.run(_main())
