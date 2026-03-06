"""
Quality test: generate images via BetaImage (voiceapi.csv666.ru/api/v2/image/).

Takes 5 representative prompts from the last project's script.json,
adapts them to the new AI master prompt rules (explicit camera, mechanical physics,
structured scene + style), generates in parallel, saves to:
    projects/<project_name>/test_newai/

Usage:
    python tools/test_newai_quality.py
    python tools/test_newai_quality.py --mode quality
    python tools/test_newai_quality.py --prompt "custom prompt here"
"""

from __future__ import annotations

import asyncio
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_env, setup_logging
from clients.betaimage_client import BetaImageClient

load_env()
log = setup_logging("test_newai")

# ─── Channel image style (from script.json channel_config.image_style) ────────

IMAGE_STYLE = (
    "epic sci-fi concept art, cinematic environment design, "
    "volumetric lighting, ultra detailed, high contrast, photorealistic rendering, "
    "vivid crimson and gold palette, wide symmetrical composition, "
    "ancient monolithic ruins with sci-fi elements, atmospheric mist"
)

# ─── Adapted prompts (scene block + camera + style) ───────────────────────────
#
# Adaptation rules from master_script rules:
#  1. Replace abstract ideas with visible mechanical/physical effects
#  2. Add explicit camera specification (low angle / wide / 35mm lens)
#  3. Add atmospheric details (mist, dust particles, volumetric light)
#  4. Keep scene vivid and physical; style appended at end
#
# ──────────────────────────────────────────────────────────────────────────────

ADAPTED_PROMPTS: list[dict] = [

    # ── BLOCK_001 — The Hook (lone figure on staircase) ───────────────────────
    {
        "id": "block_001",
        "label": "Lone Figure — Staircase to Crimson Sky",
        "prompt": (
            "A lone human figure standing at the summit of a colossal crumbling monolithic staircase "
            "that stretches upward into a burning crimson sky, each step worn and fractured with golden cracks running through the stone, "
            "posture of quiet stillness against the overwhelming scale, "
            "massive towering ancient stone pillars flanking the staircase on both sides, "
            "thin atmospheric mist drifting across the lower steps with dust particles catching golden backlight, "
            "dramatic cinematic lighting with intense crimson backlight from the sky illuminating the figure as a silhouette, "
            "volumetric golden light rays cutting through the mist from above, "
            "crimson and gold color palette, "
            "extreme low angle cinematic shot near the base of the staircase, wide perspective 35mm lens, "
        ) + IMAGE_STYLE,
    },

    # ── BLOCK_003 — The Clock (time frozen, mechanical obsession) ─────────────
    {
        "id": "block_003",
        "label": "Ancient Clock — Gears Frozen Mid-Turn",
        "prompt": (
            "An enormous ancient clockwork mechanism embedded into a towering cliff face of dark stone, "
            "colossal interlocking gears made of crimson crystal and layered gold metal, "
            "each gear frozen mid-turn with mechanical teeth locked against each other, "
            "fractured gear shards and stone debris resting at the base, "
            "one large central gear cracked through the middle, gold dust suspended in the air around it, "
            "inside a vast canyon of ancient stone ruins with monumental archways carved into the cliff walls, "
            "thin atmospheric mist pooling at the base with dust particles floating upward catching the light, "
            "powerful crimson backlight radiating from within the frozen mechanism, "
            "volumetric light rays escaping through the gaps between jammed gears, "
            "crimson and gold color palette, "
            "extreme low angle cinematic shot near the ground, wide perspective 35mm lens, "
        ) + IMAGE_STYLE,
    },

    # ── BLOCK_006 — Clockwork Interrupted (unpredictability) ──────────────────
    {
        "id": "block_006",
        "label": "Clockwork — One Gear Rotating Opposite Direction",
        "prompt": (
            "An intricate massive golden clockwork mechanism dominating the frame, "
            "layered brass gears of different sizes interlocking in complex rotating rings, "
            "one glowing crimson gear at the center rotating in the opposite direction to all surrounding gears, "
            "the opposing rotation forcing surrounding golden gears out of alignment, "
            "several gears jammed and frozen mid-motion with mechanical debris suspended around the collision points, "
            "inside monumental ancient stone ruins with towering pillars and massive archways, "
            "thin atmospheric mist drifting across the stone floor with dust particles catching the light, "
            "dramatic cinematic lighting with intense crimson backlight from behind the mechanism, "
            "glow reflecting on metal surfaces and carved stone walls, "
            "crimson and gold color palette, "
            "extreme low angle cinematic shot near the ground, wide perspective 35mm lens, "
        ) + IMAGE_STYLE,
    },

    # ── BLOCK_008 — Gravitational Center (psychological sovereignty) ───────────
    {
        "id": "block_008",
        "label": "Figure at Gravitational Center — Circular Platform",
        "prompt": (
            "A single human figure standing at the exact center of a massive circular stone platform "
            "floating above a vast crimson void, "
            "concentric rings of ancient golden symbols carved into the platform radiating outward from the figure like sound waves, "
            "the outer rings of the platform crumbling and breaking away into the void below, "
            "mechanical golden armatures rising from the platform edges and bending inward toward the central figure, "
            "ancient stone ruins visible below through the void, distant monumental pillars in atmospheric mist, "
            "thin atmospheric mist rising from the void below with dust particles suspended around the platform, "
            "dramatic cinematic lighting from directly above casting a single shaft of golden light onto the figure, "
            "crimson void glowing faintly below, "
            "crimson and gold color palette, "
            "low angle cinematic shot looking upward, wide perspective 35mm lens, "
        ) + IMAGE_STYLE,
    },

    # ── BLOCK_012 — Shadow Retrieval (withdrawing projected power) ─────────────
    {
        "id": "block_012",
        "label": "Shadow Retrieval — Golden Threads Reclaimed",
        "prompt": (
            "A human figure at the center of an ancient stone chamber, "
            "luminous golden threads extending outward from the figure's chest to three shadowy humanoid forms surrounding them, "
            "the figure pulling the threads inward as the shadowy forms dissolve into particles of crimson light, "
            "each thread visibly tightening as the golden light strengthens at the figure's core, "
            "the chamber walls carved with ancient symbols that glow where the threads pass, "
            "massive crumbling stone columns in the background, a fractured stone ceiling letting in crimson light from above, "
            "thin atmospheric mist along the stone floor with dust particles suspended in the golden light, "
            "dramatic cinematic lighting from the glowing threads and the figure's chest illuminating the chamber, "
            "crimson and gold color palette, "
            "medium cinematic shot centered on the figure, wide perspective 35mm lens, slight low angle, "
        ) + IMAGE_STYLE,
    },

]


async def generate_one(
    client: BetaImageClient,
    item: dict,
    out_dir: Path,
    mode: str,
    idx: int,
    total: int,
) -> None:
    """Generate a single image and save it."""
    out_path = out_dir / f"{item['id']}_{idx+1:02d}.png"
    label = item["label"]
    prompt = item["prompt"]

    log.info("[%d/%d] Generating: %s", idx + 1, total, label)
    log.info("  Prompt (%d chars): %s...", len(prompt), prompt[:120])

    t0 = time.monotonic()
    try:
        await client.generate_text2img(
            prompt,
            mode=mode,
            output_path=out_path,
        )
        elapsed = time.monotonic() - t0
        size_kb = out_path.stat().st_size // 1024
        log.info("  ✓ Done in %.1fs → %s (%d KB)", elapsed, out_path.name, size_kb)
    except Exception as exc:
        log.error("  ✗ Failed: %s", exc)


async def main(mode: str, custom_prompt: str | None) -> None:
    # Locate output directory
    project_dir = (
        ROOT
        / "projects"
        / "6 RARE Things That Make People Mentally Obsessed With You _ Carl Jung"
    )
    out_dir = project_dir / "test_newai"
    out_dir.mkdir(parents=True, exist_ok=True)

    items = ADAPTED_PROMPTS.copy()

    if custom_prompt:
        items = [{
            "id": "custom",
            "label": "Custom Prompt",
            "prompt": custom_prompt,
        }]

    log.info("=" * 60)
    log.info("BetaImage Quality Test — %d prompts, mode=%s", len(items), mode)
    log.info("Output: %s", out_dir)
    log.info("=" * 60)

    total = len(items)
    async with BetaImageClient(mode=mode) as client:
        tasks = [
            generate_one(client, item, out_dir, mode, idx, total)
            for idx, item in enumerate(items)
        ]
        await asyncio.gather(*tasks)

    log.info("")
    log.info("=" * 60)
    log.info("Done. Images saved to: %s", out_dir)
    results = list(out_dir.glob("*.png"))
    log.info("Generated: %d files", len(results))
    for r in sorted(results):
        log.info("  %s  (%d KB)", r.name, r.stat().st_size // 1024)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BetaImage quality test")
    parser.add_argument(
        "--mode", default="fast", choices=["fast", "quality"],
        help="Generation mode (fast ~6s, quality = slower/better)"
    )
    parser.add_argument("--prompt", default=None, help="Custom single prompt to test instead of all 5")
    args = parser.parse_args()

    asyncio.run(main(args.mode, args.prompt))
