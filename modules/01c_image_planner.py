"""
VideoForge — Module 01c: Image Planner (Art Director pass).

Reads script.json after script generation (step 1) and:
1. Calculates image positions algorithmically using the 2-tier density model
   (no reliance on LLM-placed markers — LLM only writes narration)
2. Sends the full annotated narration to a dedicated Art Director LLM
3. Art Director writes one structured image prompt per position
4. Injects image_prompts + image_word_offsets back into script.json

Density model:
  Tier 1 — words 0–1,400 (~first 10 min): 1 image every TIER1_INTERVAL words
  Tier 2 — words 1,400+  (~after 10 min):  1 image every TIER2_INTERVAL words

This completely decouples "when images appear" (algorithm, exact)
from "what images show" (Art Director LLM, high quality).

CLI:
    python modules/01c_image_planner.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        [--preset high]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import get_llm_preset, load_channel_config, load_env, setup_logging

log = setup_logging("image_planner")

# ─── Density constants ────────────────────────────────────────────────────────

TIER1_BOUNDARY = 1400   # cumulative words where tier-1 ends
TIER1_INTERVAL = 25     # words between images in tier-1 (~10s at 2.5 wps)
TIER2_INTERVAL = 70     # words between images in tier-2 (~28s at 2.5 wps)
CONTEXT_WORDS  = 45     # words of narration before/after each position (for LLM context)

MARKER_SENTINEL    = "__MARKER__"   # legacy support
DEFAULT_PRESET     = "high"
DEFAULT_PLANNER_PROMPT = ROOT / "prompts" / "image_planner_v1.txt"
VOIDAI_BASE        = "https://api.voidai.app/v1"


# ─── Position calculator ──────────────────────────────────────────────────────

def _calculate_positions(blocks: list[dict]) -> list[dict]:
    """
    Calculate where images should appear using the 2-tier density model.

    Returns list of position dicts:
        {block_idx, word_offset, cumulative_word, context_before, context_after}
    """
    # Flatten all words with (block_idx, word_idx_in_block, cumulative) metadata
    flat: list[tuple[int, int, int, str]] = []   # (block_idx, wi, cumulative, word)
    cumulative = 0
    for bi, block in enumerate(blocks):
        narr  = block.get("narration") or ""
        words = narr.split()
        for wi, word in enumerate(words):
            flat.append((bi, wi, cumulative, word))
            cumulative += 1

    if not flat:
        return []

    total_words = cumulative
    positions: list[dict] = []
    next_at = 0     # cumulative word index for next image

    while next_at < total_words:
        # Find the flat entry at or just after next_at
        entry = None
        for item in flat:
            if item[2] >= next_at:
                entry = item
                break
        if entry is None:
            break

        bi, wi, cum_w, _ = entry

        # Build context: words before and after this position
        idx = flat.index(entry)
        before_words = [f[3] for f in flat[max(0, idx - CONTEXT_WORDS): idx]]
        after_words  = [f[3] for f in flat[idx: min(len(flat), idx + CONTEXT_WORDS)]]

        positions.append({
            "block_idx":      bi,
            "word_offset":    wi,
            "cumulative_word": cum_w,
            "context_before": " ".join(before_words),
            "context_after":  " ".join(after_words),
        })

        # Advance by tier interval
        interval = TIER1_INTERVAL if cum_w < TIER1_BOUNDARY else TIER2_INTERVAL
        next_at  = cum_w + interval

    log.info(
        "Density calc: %d total words → %d positions "
        "(tier-1 ~%d, tier-2 ~%d)",
        total_words,
        len(positions),
        min(len(positions), TIER1_BOUNDARY // TIER1_INTERVAL),
        max(0, len(positions) - TIER1_BOUNDARY // TIER1_INTERVAL),
    )
    return positions


# ─── Prompt builder ───────────────────────────────────────────────────────────

def _build_context_prompt(
    script: dict[str, Any],
    positions: list[dict],
    image_style: str,
    planner_system: str,
) -> tuple[str, str]:
    """Build (system_prompt, user_message) for the Art Director LLM."""

    title  = script.get("title", "Untitled")
    niche  = script.get("niche", "psychology/history")
    blocks = script.get("blocks", [])
    n      = len(positions)

    # Build annotated narration: insert [IMAGE N] labels at calculated positions
    # Map positions by block_idx → list of (word_offset, image_idx)
    pos_by_block: dict[int, list[tuple[int, int]]] = {}
    for img_i, pos in enumerate(positions):
        pos_by_block.setdefault(pos["block_idx"], []).append(
            (pos["word_offset"], img_i)
        )

    lines: list[str] = []
    for bi, block in enumerate(blocks):
        btype  = block.get("type", "section")
        btitle = block.get("title", "")
        narr   = block.get("narration") or ""
        words  = narr.split()

        lines.append(f"\n--- [{btype.upper()}] {btitle} ---")

        markers_in_block = sorted(pos_by_block.get(bi, []), key=lambda x: x[0])
        output: list[str] = []
        mi = 0   # index into markers_in_block

        for wi, word in enumerate(words):
            while mi < len(markers_in_block) and markers_in_block[mi][0] <= wi:
                _, img_i = markers_in_block[mi]
                cum = positions[img_i]["cumulative_word"]
                ts_sec = int(cum / 2.5)
                output.append(f"\n[IMAGE {img_i + 1}] (~{ts_sec // 60}m{ts_sec % 60:02d}s)\n")
                mi += 1
            output.append(word)

        # Flush any remaining markers after last word
        while mi < len(markers_in_block):
            _, img_i = markers_in_block[mi]
            output.append(f"\n[IMAGE {img_i + 1}]\n")
            mi += 1

        lines.append(" ".join(output))

    annotated = "\n".join(lines)

    system_prompt = planner_system.strip()
    user_message  = (
        f"VIDEO TITLE: {title}\n"
        f"NICHE/TOPIC: {niche}\n"
        f"TOTAL IMAGES: {n}\n"
        f"IMAGE STYLE (append to EVERY prompt): {image_style}\n\n"
        f"FULL SCRIPT WITH IMAGE POSITIONS:\n{annotated}\n\n"
        f"---\n"
        f"Write exactly {n} image prompts — one per [IMAGE N] label, in order.\n"
        f"Output ONLY a valid JSON array of {n} strings. No other text.\n"
    )
    return system_prompt, user_message


# ─── LLM call ─────────────────────────────────────────────────────────────────

def _parse_response(text: str, expected: int) -> list[str]:
    """Extract JSON array of strings from LLM response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$",          "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [str(p).strip() for p in result]
    except json.JSONDecodeError:
        pass

    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return [str(p).strip() for p in result]
        except json.JSONDecodeError:
            pass

    # Last resort: non-empty lines longer than 20 chars
    lines = [l.strip().strip('"').strip(",") for l in text.splitlines() if l.strip()]
    prompts = [l for l in lines if len(l) > 20]
    if prompts:
        log.warning("JSON parse failed — using %d lines as fallback", len(prompts))
        return prompts

    log.error("Could not parse LLM response")
    return []


async def _call_llm(
    system_prompt: str,
    user_message: str,
    preset: dict[str, Any],
    expected: int,
) -> list[str]:
    import httpx
    load_env()
    import os
    api_key = os.environ.get("VOIDAI_API_KEY", "")
    model   = preset.get("model", "claude-sonnet-4-5-20250929")

    # For very large scripts (100+ images), split into chunks to stay within output limits
    MAX_PER_CALL = 80
    if expected <= MAX_PER_CALL:
        return await _call_llm_single(system_prompt, user_message, model, api_key, expected)

    # Chunked mode: call LLM in batches — not needed for typical 25-33 min videos
    log.warning("Large script (%d images) — using single call anyway", expected)
    return await _call_llm_single(system_prompt, user_message, model, api_key, expected)


async def _call_llm_single(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    expected: int,
) -> list[str]:
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":      model,
        "max_tokens": max(8192, expected * 150),
        "system":     system_prompt,
        "messages":   [{"role": "user", "content": user_message}],
    }

    log.info("Art Director LLM: model=%s  images=%d", model, expected)
    t0 = time.monotonic()

    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(f"{VOIDAI_BASE}/chat/completions",
                                 headers=headers, json=payload)
        resp.raise_for_status()

    elapsed = time.monotonic() - t0
    data    = resp.json()
    content = data["choices"][0]["message"]["content"]
    usage   = data.get("usage", {})
    log.info("Art Director done in %.1fs — in=%d out=%d tokens",
             elapsed,
             usage.get("prompt_tokens", 0),
             usage.get("completion_tokens", 0))

    return _parse_response(content, expected)


# ─── Injector ─────────────────────────────────────────────────────────────────

def _inject_prompts(
    script: dict[str, Any],
    positions: list[dict],
    prompts: list[str],
) -> dict[str, Any]:
    """
    Inject image_prompts and image_word_offsets into each block based on
    calculated positions and art director prompts.
    Completely replaces any existing image_prompts (including __MARKER__ sentinels).
    """
    fallback = "dramatic historical scene, cinematic lighting, oil painting baroque style"
    last_valid = fallback

    # Group by block
    from collections import defaultdict
    by_block: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for i, pos in enumerate(positions):
        prompt = prompts[i] if i < len(prompts) else last_valid
        if prompt:
            last_valid = prompt
        by_block[pos["block_idx"]].append((pos["word_offset"], prompt or last_valid))

    blocks = script.get("blocks", [])
    for bi, block in enumerate(blocks):
        entries = sorted(by_block.get(bi, []), key=lambda x: x[0])
        block["image_prompts"]      = [p for _, p in entries]
        block["image_word_offsets"] = [wo for wo, _ in entries]
        block["image_prompt"]       = block["image_prompts"][0] if block["image_prompts"] else ""

    log.info("Injected %d prompts across %d blocks", len(prompts), len(blocks))
    return script


# ─── Public API ───────────────────────────────────────────────────────────────

async def plan_images(
    script_path: Path,
    channel_config: dict[str, Any],
    *,
    preset_name: str = DEFAULT_PRESET,
    planner_prompt_path: Path | None = None,
    image_style: str = "",
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """
    Main entry point. Reads script.json, calculates image positions from
    density model, calls Art Director LLM, injects prompts, saves script.

    Always runs (does not check for __MARKER__ sentinels anymore — positions
    are always calculated algorithmically from narration word counts).
    """
    def _emit(msg: str, pct: float = 0.0) -> None:
        if progress_callback:
            try:
                progress_callback({"type": "sub_progress", "step": "1c",
                                   "pct": pct, "message": msg})
            except Exception:
                pass
        log.info(msg)

    with script_path.open(encoding="utf-8") as f:
        script = json.load(f)

    blocks = script.get("blocks", [])
    if not blocks:
        log.warning("Script has no blocks — skipping image planner")
        return script

    # Step 1: calculate positions
    positions = _calculate_positions(blocks)
    if not positions:
        log.warning("No positions calculated — skipping")
        return script

    _emit(f"Art Director: {len(positions)} image positions calculated", 5.0)

    # Step 2: load system prompt
    prompt_file = planner_prompt_path or DEFAULT_PLANNER_PROMPT
    if not prompt_file.exists():
        raise FileNotFoundError(f"Image planner prompt not found: {prompt_file}")
    planner_system = prompt_file.read_text(encoding="utf-8")

    # Step 3: determine style
    style = image_style or channel_config.get("image_style", "")

    # Step 4: get LLM preset
    preset = get_llm_preset(channel_config, preset_name)

    # Step 5: build and send prompt
    system_prompt, user_message = _build_context_prompt(
        script, positions, style, planner_system
    )
    _emit(f"Art Director: calling {preset.get('model','?')} …", 10.0)
    prompts = await _call_llm(system_prompt, user_message, preset, len(positions))

    if not prompts:
        raise RuntimeError("Art Director returned no prompts")

    if len(prompts) < len(positions):
        log.warning("Got %d prompts for %d positions — last prompt reused for remainder",
                    len(prompts), len(positions))

    # Step 6: inject and save
    _emit(f"Art Director: injecting {len(prompts)} prompts …", 90.0)
    script = _inject_prompts(script, positions, prompts)

    with script_path.open("w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    log.info("Saved: %s", script_path)

    _emit(f"Art Director: done — {len(positions)} positions filled.", 100.0)
    return script


# ─── CLI ──────────────────────────────────────────────────────────────────────

async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="VideoForge — Image Planner (Art Director pass)"
    )
    parser.add_argument("--script",  required=True)
    parser.add_argument("--channel", default="config/channels/history.json")
    parser.add_argument("--preset",  default=DEFAULT_PRESET)
    parser.add_argument("--planner-prompt", default=None)
    parser.add_argument("--image-style",    default="")
    args = parser.parse_args()

    load_env()
    channel_config = load_channel_config(args.channel)
    script_path    = Path(args.script)
    prompt_path    = Path(args.planner_prompt) if args.planner_prompt else None

    await plan_images(
        script_path, channel_config,
        preset_name=args.preset,
        planner_prompt_path=prompt_path,
        image_style=args.image_style or "",
    )


if __name__ == "__main__":
    asyncio.run(_main())
