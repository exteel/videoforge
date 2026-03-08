"""
VideoForge — Module 01: Script Generator.

Reads Transcriber output → builds LLM prompt → calls VoidAI → parses into script.json.

Features:
- Content templates: documentary, listicle, tutorial, comparison
- Hook system: curiosity, negative, storytelling, challenge, comparison
- Hook validation: 4 criteria, auto-regenerate intro on fail
- Script compare: --compare N generates N variants
- Dry run mode: estimate cost without API calls
- Prompt versioning: master_script_v1.txt, v2.txt (from channel config)

CLI:
    python modules/01_script_generator.py \\
        --source "D:/transscript batch/output/output/Video Title" \\
        --channel config/channels/history.json \\
        --template documentary \\
        --preset max
"""

import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import (
    get_llm_preset,
    load_channel_config,
    load_env,
    load_transcriber_output,
    setup_logging,
)

log = setup_logging("script_gen")

# ─── Constants ────────────────────────────────────────────────────────────────

PROMPTS_DIR = ROOT / "prompts"
VALIDATOR_MODEL = "gpt-4.1"  # gpt-4.1-nano consistently misclassifies good v3 hooks
MAX_HOOK_ATTEMPTS = 10  # max total validation+regen cycles (5+5 two-round strategy)
HOOK_ROUND_SIZE   = 5   # after round 1, pick best ≥3/4 before continuing round 2
MAX_TRANSCRIPT_CHARS = 14_000   # ~10K tokens — keeps total prompt manageable for Opus
MAX_HOOKS_GUIDE_CHARS = 8_000   # hooks_guide.md is now compact (~4KB), allow full pass-through

# Chunked generation constants
MAX_FIRST_CHUNK_TOKENS = 8_000  # first chunk cap — large enough for 37-min scripts in one shot
MAX_TOKENS_PER_CHUNK   = 4_000  # continuation chunks cap (remaining budget, cost-controlled)
MAX_SCRIPT_CHUNKS = 5           # max continuation attempts (raised for 20-30+ min scripts)

BlockType = Literal["intro", "section", "cta", "outro"]

# ─── V3 Block Architecture ────────────────────────────────────────────────────

BLOCK_STRUCTURE_V3: list[dict] = [
    {"name": "HOOK",           "pct": 0.04},
    {"name": "TENSION SETUP",  "pct": 0.10},
    {"name": "ROOT CAUSE",     "pct": 0.12},
    {"name": "RECOGNITION",    "pct": 0.16},
    {"name": "CORE FRAMEWORK", "pct": 0.20},
    {"name": "THE TURN",       "pct": 0.14},
    {"name": "THE PRACTICE",   "pct": 0.14},
    {"name": "CLOSING + CTA",  "pct": 0.10},
]


def _calc_images_for_block(start_word: int, block_words: int) -> int:
    """
    Calculate expected image count for a block using the 4-tier density model.

    Tier 1 (0–450 w):   1 image per  25 words  (~min 0–3)
    Tier 2 (450–900 w): 1 image per  50 words  (~min 3–6)
    Tier 3 (900–2250 w):1 image per 150 words  (~min 6–15)
    Tier 4 (2250+ w):   1 image per 280 words  (~min 15+)
    """
    TIERS: list[tuple[int, int]] = [(450, 25), (900, 50), (2250, 150), (10**9, 280)]
    count = 0.0
    pos = start_word
    remaining = block_words

    for boundary, interval in TIERS:
        if remaining <= 0:
            break
        if pos >= boundary:
            continue  # haven't entered this tier yet
        available_in_tier = boundary - pos
        used = min(remaining, available_in_tier)
        count += used / interval
        pos += used
        remaining -= used

    return max(1, round(count))


def _calc_block_targets(duration_min: int, duration_max: int) -> list[dict]:
    """
    Compute per-block word counts and image counts for BLOCK_STRUCTURE_V3.

    Uses midpoint of the duration range for image-tier calculation.
    Returns list of dicts: [{name, words_min, words_max, images}, ...].
    """
    words_min = duration_min * 140
    words_max = duration_max * 150
    total_mid = (words_min + words_max) // 2

    results: list[dict] = []
    cumulative_words = 0

    for block in BLOCK_STRUCTURE_V3:
        bmin = max(1, int(words_min * block["pct"]))
        bmax = max(1, int(words_max * block["pct"]))
        bmid = max(1, int(total_mid * block["pct"]))
        images = _calc_images_for_block(cumulative_words, bmid)
        results.append({"name": block["name"], "words_min": bmin, "words_max": bmax, "images": images})
        cumulative_words += bmid

    return results


# ─── Template / Hook tables ───────────────────────────────────────────────────

# Default hook type per content template
HOOK_PER_TEMPLATE: dict[str, str] = {
    "documentary": "curiosity",
    "listicle": "negative",
    "tutorial": "challenge",
    "comparison": "comparison",
}

# Template-specific instructions appended to __SPECIAL REQUESTS__
TEMPLATE_INSTRUCTIONS: dict[str, str] = {
    "documentary": (
        "Content format: DOCUMENTARY. Use a cinematic narrative arc — historical sweep, "
        "expert framing, emotional beats tied to real events or figures. "
        "Build from context to insight to transformation."
    ),
    "listicle": (
        "Content format: LISTICLE. Structure as a numbered list video "
        "(e.g. '5 signs you are X', '7 things Y people do differently'). "
        "Each [SECTION] = one list item. Order from least to most impactful."
    ),
    "tutorial": (
        "Content format: TUTORIAL. Structure as a step-by-step guide. "
        "Each [SECTION] = one clear, actionable step. Viewer should be able to follow along."
    ),
    "comparison": (
        "Content format: COMPARISON. Set up two or more contrasting concepts, approaches, "
        "or time periods. Systematically contrast them. Reveal a synthesis or winner at the end."
    ),
}

# Hook type instruction injected into user prompt
HOOK_INSTRUCTIONS: dict[str, str] = {
    "curiosity": (
        "Hook type: CURIOSITY. Use the 3-step formula: "
        "1) Context Lean-In — set familiar scene viewer recognizes, "
        "2) Scroll-Stop — insert 'BUT...' / 'HOWEVER...' subversion of expectation, "
        "3) Contrarian Snapback — reveal unexpected insight. "
        "Open a curiosity gap the viewer must resolve by watching."
    ),
    "negative": (
        "Hook type: NEGATIVE/FEAR. Lead with what the viewer LOSES, avoids, or fears. "
        "Pain framing beats benefit framing. 'If you continue doing X, you will lose Y forever.'"
    ),
    "storytelling": (
        "Hook type: STORYTELLING. Drop viewer in medias res — mid-action, mid-scene. "
        "Establish character, moment of tension, immediate stakes. "
        "No setup, straight to the moment that changes everything."
    ),
    "challenge": (
        "Hook type: CHALLENGE. Directly challenge the viewer's existing belief. "
        "'Everything you know about X is wrong.' Dare them to disprove you."
    ),
    "comparison": (
        "Hook type: COMPARISON. Open with a direct 'X vs Y' or 'Then vs Now' contrast. "
        "Make the comparison feel immediately relevant and high-stakes."
    ),
}


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class HookInfo(BaseModel):
    type: str = "curiosity"
    formula: str = "context_lean + scroll_stop + snapback"
    validation_score: int | None = None


class ScriptBlock(BaseModel):
    id: str
    order: int
    type: BlockType
    narration: str
    image_prompt: str = ""                          # primary image prompt (image_prompts[0])
    image_prompts: list[str] = Field(default_factory=list)  # all image prompts for this block
    image_word_offsets: list[int] = Field(default_factory=list)
    # Word offset (0-based) in the narration at which each image_prompt was placed by LLM.
    # image_word_offsets[i] corresponds to image_prompts[i].
    # Used by the video compiler to sync each image to the correct moment in audio:
    #   audio_time = (word_offset / total_words) * audio_duration
    animation: str = "zoom_in"
    timestamp_label: str = ""
    audio_duration: float | None = None
    hook: HookInfo | None = None


class SourceInfo(BaseModel):
    video_id: str = ""
    original_title: str = ""
    transcription_path: str = ""
    thumbnail_prompt: str = ""


class ChannelConfigSummary(BaseModel):
    name: str
    voice_id: str = ""
    image_style: str = ""
    subtitle_style: dict[str, Any] = Field(default_factory=dict)


class Script(BaseModel):
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    language: str = "en"
    niche: str = ""
    source: SourceInfo = Field(default_factory=SourceInfo)
    blocks: list[ScriptBlock]
    thumbnail_prompt: str = ""
    channel_config: ChannelConfigSummary
    duration_min: int = 8   # target minimum duration in minutes
    duration_max: int = 12  # target maximum duration in minutes

    @field_validator("blocks")
    @classmethod
    def blocks_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("Script must have at least one block")
        return v


class HookValidationResult(BaseModel):
    criteria: dict[str, Any] = Field(default_factory=dict)
    pass_count: int = 0
    passed: bool = False
    failed_criteria: list[str] = Field(default_factory=list)
    suggested_rewrite: str | None = None


# ─── Parser ───────────────────────────────────────────────────────────────────

_SECTION_RE = re.compile(r"^\[SECTION\s+\d+\s*:\s*(.+?)\]\s*$", re.IGNORECASE | re.MULTILINE)
_IMAGE_LINE_RE = re.compile(r"^\[IMAGE_PROMPT:\s*(.+?)\]\s*$", re.IGNORECASE | re.DOTALL)
_IMAGE_INLINE_RE = re.compile(r"\[IMAGE_PROMPT:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_IMAGE_MARKER_RE = re.compile(r"^\[IMAGE_MARKER\]\s*$", re.IGNORECASE)   # two-pass: marker only
_CTA_MID_RE = re.compile(r"^\[CTA_SUBSCRIBE_MID\]\s*$", re.IGNORECASE)
_CTA_FINAL_RE = re.compile(r"^\[CTA_SUBSCRIBE_FINAL\]\s*$", re.IGNORECASE)

# Sentinel stored in image_prompts for IMAGE_MARKER positions (replaced by 01c_image_planner)
_MARKER_SENTINEL = "__MARKER__"


def _block_type_from_title(title: str, order: int) -> BlockType:
    """Infer block type from section title and position."""
    if order == 0:
        return "intro"
    t = title.lower()
    if any(w in t for w in ("hook", "intro", "opening")):
        return "intro"
    if any(w in t for w in ("closing", "outro", "final cta", "end")):
        return "outro"
    if any(w in t for w in ("subscribe", "cta")):
        return "cta"
    return "section"


def _parse_llm_output(
    raw: str,
    channel_config: dict[str, Any],
    source_data: dict[str, Any],
    hook_type: str,
    image_style: str = "",
    custom_topic: str = "",
) -> Script:
    """
    Parse LLM narrative output into a Script pydantic model.

    Handles:
    - [SECTION X: Title] markers → blocks
    - [IMAGE_PROMPT: ...] → image_prompt field (first per section)
    - [CTA_SUBSCRIBE_MID] / [CTA_SUBSCRIBE_FINAL] → cta/outro blocks
    - Inline [IMAGE_PROMPT:...] stripped from narration
    - Fallback: if no [SECTION] markers, create single block
    """
    default_animation = channel_config.get("default_animation", "zoom_in")
    channel_name = channel_config.get("channel_name", "")
    niche = channel_config.get("niche", "")
    language = channel_config.get("language", "en")

    blocks: list[ScriptBlock] = []
    order = 0

    # State per section
    section_title: str = "Hook"
    section_type: BlockType = "intro"
    is_cta_block = False
    all_image_prompts: list[str] = []       # accumulates ALL [IMAGE_PROMPT:] in current section
    all_word_offsets: list[int] = []        # word count in narration at time of each IMAGE_PROMPT
    narration_lines: list[str] = []
    _narration_word_count: int = 0          # running word count within current section

    def flush() -> None:
        nonlocal order, all_image_prompts, all_word_offsets, narration_lines, section_title
        nonlocal is_cta_block, _narration_word_count

        # Strip inline IMAGE_PROMPTs from narration (closed: [IMAGE_PROMPT: ...])
        raw_narration = "\n".join(narration_lines)
        narration = _IMAGE_INLINE_RE.sub("", raw_narration).strip()
        # Strip unclosed [IMAGE_PROMPT: tags (no closing ]) — stops at \n\n to preserve text
        # after a paragraph break that follows a malformed/truncated tag
        narration = re.sub(r"\[IMAGE_PROMPT:.*?(?=\n\n|\Z)", "", narration, flags=re.IGNORECASE | re.DOTALL).strip()
        # Strip any stray [IMAGE_MARKER] tags from narration (two-pass mode safety net)
        narration = re.sub(r"\[IMAGE_MARKER\]", "", narration, flags=re.IGNORECASE).strip()
        narration = re.sub(r"\n{3,}", "\n\n", narration)

        if not narration and not all_image_prompts:
            narration_lines = []
            _narration_word_count = 0
            return

        is_intro = order == 0
        hook_info = (
            HookInfo(type=hook_type, formula="context_lean + scroll_stop + snapback")
            if is_intro
            else None
        )

        actual_type: BlockType = "cta" if is_cta_block else section_type
        primary_prompt = all_image_prompts[0] if all_image_prompts else ""

        # All blocks use zoom_in — only supported animation type
        _animation = "zoom_in"

        blocks.append(
            ScriptBlock(
                id=f"block_{order + 1:03d}",
                order=order + 1,
                type=actual_type,
                narration=narration,
                image_prompt=primary_prompt,
                image_prompts=list(all_image_prompts),        # copy so reset below doesn't mutate
                image_word_offsets=list(all_word_offsets),   # parallel list: word pos per image
                animation=_animation,
                timestamp_label=section_title,
                hook=hook_info,
            )
        )
        order += 1
        all_image_prompts = []
        all_word_offsets = []
        narration_lines = []
        _narration_word_count = 0

    lines = raw.splitlines()

    # Check if raw output has any section markers
    has_sections = bool(_SECTION_RE.search(raw))

    if not has_sections:
        # Fallback: treat entire output as one block, extract all image prompts.
        # Compute word offsets by scanning the raw text line by line.
        found_images: list[str] = []
        fallback_offsets: list[int] = []
        _fb_words = 0
        for _fb_line in raw.splitlines():
            _fb_stripped = _fb_line.rstrip()
            # Two-pass mode: [IMAGE_MARKER] lines
            if _IMAGE_MARKER_RE.match(_fb_stripped):
                found_images.append(_MARKER_SENTINEL)
                fallback_offsets.append(_fb_words)
                continue
            _img_fb = _IMAGE_INLINE_RE.search(_fb_stripped)
            if _img_fb:
                found_images.append(_img_fb.group(1).strip())
                fallback_offsets.append(_fb_words)
            else:
                _fb_words += len(_fb_stripped.split()) if _fb_stripped.strip() else 0
        narration = _IMAGE_INLINE_RE.sub("", raw).strip()
        narration = re.sub(r"\[IMAGE_MARKER\]", "", narration, flags=re.IGNORECASE).strip()
        narration = re.sub(r"\n{3,}", "\n\n", narration)
        blocks.append(
            ScriptBlock(
                id="block_001",
                order=1,
                type="intro",
                narration=narration,
                image_prompt=found_images[0] if found_images else "",
                image_prompts=found_images,
                image_word_offsets=fallback_offsets,
                animation=default_animation,
                timestamp_label="Hook",
                hook=HookInfo(type=hook_type),
            )
        )
        log.warning("No [SECTION] markers found in LLM output — created single block")
    else:
        for line in lines:
            stripped = line.rstrip()

            # [SECTION X: Title]
            m = _SECTION_RE.match(stripped)
            if m:
                flush()
                section_title = m.group(1).strip()
                section_type = _block_type_from_title(section_title, order)
                is_cta_block = False
                continue

            # [CTA_SUBSCRIBE_MID]
            if _CTA_MID_RE.match(stripped):
                flush()
                section_title = "Subscribe"
                section_type = "cta"
                is_cta_block = True
                continue

            # [CTA_SUBSCRIBE_FINAL]
            if _CTA_FINAL_RE.match(stripped):
                flush()
                section_title = "Subscribe Final"
                section_type = "outro"
                is_cta_block = True
                continue

            # [IMAGE_MARKER] — two-pass mode: position-only marker, prompt filled later by 01c
            if _IMAGE_MARKER_RE.match(stripped):
                all_image_prompts.append(_MARKER_SENTINEL)
                all_word_offsets.append(_narration_word_count)
                continue  # never add marker tag to narration text

            # [IMAGE_PROMPT: ...] standalone line (closed — has ] on same line)
            # Collect ALL prompts per section (not just the first — v2 prompt has multiple per section)
            # Record the current narration word count as the word offset for this image.
            img_m = _IMAGE_LINE_RE.match(stripped)
            if img_m:
                all_image_prompts.append(img_m.group(1).strip())
                all_word_offsets.append(_narration_word_count)  # snapshot at time of image tag
                # Skip adding to narration — it's a visual directive
                continue

            # Unclosed [IMAGE_PROMPT: tag (line starts with tag but has no closing ])
            # LLM sometimes omits ] or the response is truncated mid-tag
            if re.match(r"^\[IMAGE_PROMPT:", stripped, re.IGNORECASE) and "]" not in stripped:
                salvaged = re.sub(r"^\[IMAGE_PROMPT:\s*", "", stripped, flags=re.IGNORECASE).strip(" ,")
                if salvaged:
                    all_image_prompts.append(salvaged)
                    all_word_offsets.append(_narration_word_count)
                # Never add raw tag text to narration
                continue

            # Malformed [SECTION marker (no number+colon+title — continuation artifact).
            # e.g. "[SECTION" alone on a line when LLM was cut off mid-marker.
            # The proper regex _SECTION_RE already handled the well-formed case above;
            # this guard prevents the bare tag from leaking into narration text.
            if re.match(r"^\[SECTION\b", stripped, re.IGNORECASE) and not _SECTION_RE.match(stripped):
                log.debug("Skipping malformed [SECTION marker line: %r", stripped)
                continue

            narration_lines.append(stripped)
            # Count words accumulated in narration so far (for image offset tracking)
            _narration_word_count += len(stripped.split()) if stripped.strip() else 0

        flush()  # Flush the last section

    # Ensure first block is always "intro" type
    if blocks:
        if blocks[0].type != "intro":
            blocks[0] = blocks[0].model_copy(update={"type": "intro"})
        if blocks[0].hook is None:
            blocks[0] = blocks[0].model_copy(
                update={"hook": HookInfo(type=hook_type)}
            )

    # Collect metadata
    meta = source_data.get("metadata") or {}
    thumbnail_prompt_src = source_data.get("thumbnail_prompt") or ""
    ref_title = source_data.get("title") or meta.get("title") or "Untitled"
    # Use custom_topic as the video title when provided — reference video title only used as fallback
    video_title = custom_topic.strip() if custom_topic.strip() else ref_title
    video_desc = source_data.get("description") or meta.get("description") or ""

    return Script(
        title=video_title,
        description=video_desc,
        tags=[niche] if niche else [],
        language=language,
        niche=niche,
        source=SourceInfo(
            video_id=meta.get("video_id") or "",
            original_title=video_title,
            transcription_path=source_data.get("source_dir") or "",
            thumbnail_prompt=thumbnail_prompt_src,
        ),
        blocks=blocks,
        thumbnail_prompt=thumbnail_prompt_src,
        channel_config=ChannelConfigSummary(
            name=channel_name,
            voice_id=channel_config.get("voice_id", ""),
            image_style=image_style,  # from UI only — not channel_config
            subtitle_style=channel_config.get("subtitle_style", {}),
        ),
    )


# ─── Prompt Building ──────────────────────────────────────────────────────────

def _load_master_prompt(channel_config: dict[str, Any]) -> str:
    """Load master prompt from channel config's master_prompt_path."""
    prompt_path = channel_config.get("master_prompt_path", "prompts/master_script_v1.txt")
    p = Path(prompt_path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise FileNotFoundError(
            f"Master prompt not found: {p}. "
            f"Check 'master_prompt_path' in channel config."
        )
    return p.read_text(encoding="utf-8").strip()


def _load_hooks_guide() -> str:
    """Load hooks guide for injection into system prompt (truncated to avoid context overflow)."""
    p = PROMPTS_DIR / "hooks_guide.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    if len(text) > MAX_HOOKS_GUIDE_CHARS:
        log.debug(
            "hooks_guide.md truncated from %d to %d chars to keep system prompt manageable",
            len(text), MAX_HOOKS_GUIDE_CHARS,
        )
        text = text[:MAX_HOOKS_GUIDE_CHARS]
    return text


def _build_system_prompt(channel_config: dict[str, Any]) -> str:
    """Build system prompt: master prompt + hooks guide."""
    master = _load_master_prompt(channel_config)
    hooks = _load_hooks_guide()
    if hooks:
        return f"{master}\n\n---\n\n## HOOKS REFERENCE GUIDE\n\n{hooks}"
    return master


def _build_user_prompt(
    source_data: dict[str, Any],
    channel_config: dict[str, Any],
    template: str,
    hook_type: str,
    duration_min: int,
    duration_max: int,
    image_style: str = "",
    custom_topic: str = "",
) -> str:
    """Build user message (transcript + topic + special requests)."""
    language = channel_config.get("language", "en")
    transcript = source_data.get("transcript") or ""
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        log.warning(
            "Transcript too large (%d chars) — truncating to %d chars to keep Opus prompt manageable",
            len(transcript), MAX_TRANSCRIPT_CHARS,
        )
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]
    title = source_data.get("title") or ""
    description = source_data.get("description") or ""
    thumbnail_prompt = source_data.get("thumbnail_prompt") or ""

    # Assemble special requests
    requests: list[str] = []

    if template != "auto" and template in TEMPLATE_INSTRUCTIONS:
        requests.append(TEMPLATE_INSTRUCTIONS[template])

    hook_instr = HOOK_INSTRUCTIONS.get(hook_type, "")
    if hook_instr:
        requests.append(hook_instr)

    if thumbnail_prompt:
        snippet = thumbnail_prompt[:200].replace("\n", " ").strip()
        requests.append(
            f"Source thumbnail context (use for visual reference only): {snippet}"
        )

    special = "\n".join(f"- {r}" for r in requests) if requests else "None"

    # Build new topic string:
    # custom_topic (from UI) always wins; fall back to reference video title + description.
    if custom_topic.strip():
        new_topic = custom_topic.strip()
        log.info("Custom topic override: %s", new_topic[:120])
    else:
        new_topic = title
        if description:
            desc_short = description[:300].replace("\n", " ").strip()
            new_topic += f"\n   Context: {desc_short}"

    # Compute target word count range (140-150 wpm speaking pace)
    target_words_min = duration_min * 140
    target_words_max = duration_max * 150

    # Image style — injected so the LLM applies it to every [IMAGE_PROMPT:] tag.
    # The 5-element formula in the master prompt has a "Style" slot; this replaces the
    # generic examples (e.g. "cinematic photorealism") with the channel's actual style.
    # NOTE: image_style comes exclusively from the UI parameter — channel_config is NOT used.
    image_style = image_style.strip()
    image_style_line = (
        f"\n[IMAGE STYLE] — Apply to EVERY [IMAGE_PROMPT:] tag (replace the 'Style' element):\n"
        f"{image_style}\n"
    ) if image_style else ""

    # v3 block targets — injected when channel config uses master_script_v3
    master_prompt_path = channel_config.get("master_prompt_path", "")
    is_v3 = "v3" in Path(master_prompt_path).name
    block_section = ""
    if is_v3:
        targets = _calc_block_targets(duration_min, duration_max)
        word_lines = "\n".join(
            f"Block {i + 1} {t['name']}: {t['words_min']}–{t['words_max']} words "
            f"({int(BLOCK_STRUCTURE_V3[i]['pct'] * 100)}%)"
            for i, t in enumerate(targets)
        )

        # Global image minimum — derived from the 4-tier density model applied to the
        # ENTIRE script word count (midpoint). This is the canonical calculation:
        #   Tier 1 (0–450 w):   1 image / 25 w  → ~18 images for first 3 min
        #   Tier 2 (450–900 w): 1 image / 50 w  → ~9 images for min 3–6
        #   Tier 3 (900–2250 w):1 image / 150 w → ~9 images for min 6–15
        #   Tier 4 (2250+ w):   1 image / 280 w → ~N images for min 15+
        total_mid_words = (target_words_min + target_words_max) // 2
        total_images_min = _calc_images_for_block(0, total_mid_words)

        block_section = (
            f"\n[BLOCK WORD TARGETS] — NARRATION WORDS ONLY (do NOT count [IMAGE_PROMPT:] tags):\n"
            f"{word_lines}\n"
            f"\n⚠️ IMAGE COUNT — MANDATORY:\n"
            f"You MUST place at least {total_images_min} [IMAGE_PROMPT:] tags across the ENTIRE script.\n"
            f"This is the minimum calculated from the 4-tier density model for a "
            f"{duration_min}–{duration_max} min ({target_words_min}–{target_words_max} word) video.\n"
            f"Follow the tier intervals from the system prompt (every ~25w / ~50w / ~150w / ~280w).\n"
            f"Do NOT reduce density — if you feel the scene needs fewer images, add more, not fewer.\n"
        )
        log.info(
            "v3 targets injected: %d blocks, words %d–%d, images MINIMUM %d (4-tier for %d mid-words)",
            len(targets), target_words_min, target_words_max,
            total_images_min, total_mid_words,
        )

    return (
        f"[TRANSCRIPTION]\n{transcript}\n\n"
        f"__NEW TOPIC__: {new_topic}\n"
        f"[DURATION]: {duration_min}-{duration_max} minutes\n"
        f"[TARGET WORDS]: {target_words_min}–{target_words_max} words\n"
        f"[LANGUAGE]: {language} — Write the ENTIRE script in this language, "
        f"including all CTAs, image prompts, and section titles.\n"
        f"{image_style_line}"
        f"{block_section}"
        f"⚠️ WORD COUNT REQUIREMENTS (BOTH apply — NARRATION WORDS ONLY, [IMAGE_PROMPT:] tags do NOT count):\n"
        f"  MINIMUM — Do NOT write [CTA_SUBSCRIBE_FINAL] until you have written "
        f"at least {target_words_min} NARRATION words (spoken text only, excluding [IMAGE_PROMPT:] tags). "
        f"A {duration_min}-min video requires this depth — do not rush to close. "
        f"If you feel done before {target_words_min} narration words, add more depth, examples, or analysis.\n"
        f"  MAXIMUM — Hard limit: {target_words_max} NARRATION words. "
        f"Write [CTA_SUBSCRIBE_FINAL] as soon as you approach this ceiling.\n"
        f"__SPECIAL REQUESTS__:\n{special}"
    )


# ─── Hook Validation ──────────────────────────────────────────────────────────

async def _validate_intro_hook(
    intro_narration: str,
    niche: str,
    audience: str,
    voidai_client: Any,
    topic: str = "",    # actual video topic/title — for CLARITY criterion
) -> HookValidationResult:
    """
    Validate intro block with cheap model (gpt-4.1-nano) + hook_validator.txt.

    Returns HookValidationResult — passed=True if >= 3/4 criteria pass.
    On any error, returns a "pass" result to avoid blocking generation.
    """
    validator_path = PROMPTS_DIR / "hook_validator.txt"
    if not validator_path.exists():
        log.warning("hook_validator.txt not found — skipping validation")
        return HookValidationResult(pass_count=4, passed=True)

    validator_template = validator_path.read_text(encoding="utf-8").strip()
    user_content = (
        validator_template
        .replace("{intro_narration}", intro_narration)
        .replace("{niche}", niche or "general")
        .replace("{audience}", audience or (niche + " enthusiasts" if niche else "general viewers"))
        .replace("{topic}", topic or "not specified")
    )

    try:
        raw = await voidai_client.chat_completion(
            model=VALIDATOR_MODEL,
            messages=[{"role": "user", "content": user_content}],
            temperature=0.1,
            max_tokens=800,
            use_fallback=False,
        )

        # Strip markdown code fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean.strip())

        data = json.loads(clean)
        return HookValidationResult(**data)

    except json.JSONDecodeError as exc:
        log.warning("Hook validator returned non-JSON (%s) — treating as passed", exc)
        return HookValidationResult(pass_count=3, passed=True)
    except Exception as exc:
        log.warning("Hook validation error (%s: %s) — treating as passed", type(exc).__name__, exc)
        return HookValidationResult(pass_count=3, passed=True)


# ─── Core Generation ──────────────────────────────────────────────────────────

# Strict marker — used ONLY in the chunking loop to detect script completion.
# We deliberately do NOT match "Thank you for being here" here because that phrase
# can appear in regular outro narration (not only in the CTA section), which
# previously caused premature CTA detection → strip → double-ending artefacts.
_FINAL_CTA_MARKER_RE = re.compile(r"\[CTA_SUBSCRIBE_FINAL\]", re.IGNORECASE)

# Broad pattern — used ONLY in the post-loop CTA repair / dedup section where we
# need to find the CTA even when the LLM omitted the bracket marker.
_FINAL_CTA_RE = re.compile(r"\[CTA_SUBSCRIBE_FINAL\]|Thank you for being here", re.IGNORECASE)
_LAST_SECTION_NUM_RE = re.compile(r"\[SECTION\s+(\d+)\s*:", re.IGNORECASE)

# Strips all non-narration markup before word counting:
#   [IMAGE_PROMPT: ...]  — inline image directives (can span multiple lines)
#   [SECTION N: ...]     — section headers
#   [CTA_SUBSCRIBE...]   — CTA marker lines
_NARRATION_STRIP_RE = re.compile(
    r"\[IMAGE_PROMPT:.*?\]"         # image prompt tags (DOTALL applied below)
    r"|\[SECTION\s+\d+[^\]]*\]"    # section headers
    r"|\[CTA_SUBSCRIBE[^\]]*\]",   # CTA markers
    re.IGNORECASE | re.DOTALL,
)


def _count_narration_words(text: str) -> int:
    """Return word count of *narration only*, stripping IMAGE_PROMPT tags and structural markers.

    IMAGE_PROMPT tags can add 30-40% to total LLM output words for v3 scripts (~40 images
    × ~35 words/prompt = ~1 400 extra words). Counting only narration words gives accurate
    duration estimates for all target lengths (25 min, 40 min, etc.).
    """
    clean = _NARRATION_STRIP_RE.sub("", text)
    return len(clean.split())


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str,
    voidai_client: Any,
    temperature: float = 0.7,
    duration_min: int = 8,
    duration_max: int = 12,
) -> str:
    """
    Generate script in chunks of ~10K chars to avoid Opus timeout on long scripts.

    Strategy:
    - Call 1: normal prompt → max tokens calibrated to duration_max
    - If no final CTA found AND word count < budget → continuation call (up to MAX_SCRIPT_CHUNKS)
    - Continuation provides last 2K chars + "continue from Section N" instruction
    - Hard stop: if accumulated word count exceeds duration_max * 150 * 1.15, no more chunks.
    """
    full_output = ""

    # All word budgets are in NARRATION words (IMAGE_PROMPT tags stripped by _count_narration_words).
    # This gives accurate duration control for all target lengths (25 min, 40 min, etc.).
    #
    # word_budget: ceiling — stop requesting more chunks once narration reaches this.
    #   +15% headroom so the LLM can round off the last block cleanly.
    word_budget = int(duration_max * 150 * 1.15)
    # min_words_for_cta: floor — LLM must NOT write [CTA_SUBSCRIBE_FINAL] before this many
    #   narration words. Set to 100% of min-duration target (no haircut).
    min_words_for_cta = int(duration_min * 140)

    # First chunk: multiplier 2.0 accounts for inline IMAGE_PROMPT overhead.
    # v3 scripts with ~40 images × ~35 words/prompt ≈ 1 400 extra words beyond narration.
    # For duration_max=25: min(8000, 25*150*2.0=7500) = 7500 tokens → fits full script in one shot.
    # For duration_max=12: min(8000, 12*150*2.0=3600) = 3600 — same as before for short scripts.
    tokens_first_chunk = min(MAX_FIRST_CHUNK_TOKENS, int(duration_max * 150 * 2.0))

    for chunk_num in range(1, MAX_SCRIPT_CHUNKS + 1):
        # Hard word-count guard: narration words only (IMAGE_PROMPT tags stripped).
        current_words = _count_narration_words(full_output)
        if chunk_num > 1 and current_words >= word_budget:
            log.warning(
                "Word budget reached (%d words ≥ %d limit for %d-min target) — "
                "stopping continuation to avoid over-length script.",
                current_words, word_budget, duration_max,
            )
            break

        if chunk_num == 1:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            chunk_tokens = tokens_first_chunk
        else:
            # Detect last section number so continuation knows where to resume
            section_nums = _LAST_SECTION_NUM_RE.findall(full_output)
            last_section = int(section_nums[-1]) if section_nums else 0
            tail = full_output[-2_000:]  # last 2K chars as context anchor
            remaining_words = max(0, word_budget - current_words)
            # remaining_words is narration-only; total output (narration + image prompts) is
            # ~1.5× narration, at ~1.4 tokens/word → effective multiplier ≈ 2.1
            remaining_tokens = min(MAX_TOKENS_PER_CHUNK, int(remaining_words * 2.1))

            # Detect cut-off: if full_output ends without sentence-closing punctuation,
            # the previous chunk was interrupted mid-sentence (hit max_tokens).
            # In that case, tell the LLM to complete the interrupted section before
            # moving on — otherwise it starts a fresh section, leaving a broken block.
            last_chars = full_output.rstrip()[-8:]
            is_cut_off = bool(last_chars) and not any(c in last_chars for c in ".!?\"'")

            if is_cut_off:
                continuation_instruction = (
                    f"CONTINUATION — the previous response was cut off mid-sentence (hit token limit). "
                    f"Here is exactly where it ended:\n```\n{tail}\n```\n\n"
                    f"IMPORTANT: Do NOT start a new [SECTION]. "
                    f"First, complete the interrupted sentence/paragraph from exactly where it was cut. "
                    f"Then continue naturally with the remaining sections through [CTA_SUBSCRIBE_FINAL]. "
                    f"You have approximately {remaining_words} words remaining."
                )
                log.warning(
                    "Cut-off detected — chunk %d/%d instructed to complete Section %d before continuing",
                    chunk_num, MAX_SCRIPT_CHUNKS, last_section,
                )
            else:
                continuation_instruction = (
                    f"CONTINUATION — you already wrote sections 1-{last_section}. "
                    f"Here is the end of what you wrote so far:\n```\n{tail}\n```\n\n"
                    f"CRITICAL: Your response MUST start IMMEDIATELY with "
                    f"[SECTION {last_section + 1}: <Title>]. "
                    f"Do NOT output any reasoning, meta-commentary, or statements about what you "
                    f"are doing. Do NOT write phrases like 'I need to reassess', 'Looking at what "
                    f"you've written', 'Continuing from', 'I'll continue', or anything similar. "
                    f"Start directly with [SECTION {last_section + 1}: ...] — nothing before it.\n\n"
                    f"Do NOT repeat any content from sections 1-{last_section}. "
                    f"Write from Section {last_section + 1} through the final [CTA_SUBSCRIBE_FINAL]. "
                    f"You have approximately {remaining_words} words remaining before the hard limit."
                )
                log.info(
                    "Script continuation request: chunk %d/%d, resuming after Section %d "
                    "(%d words used, %d remaining)",
                    chunk_num, MAX_SCRIPT_CHUNKS, last_section, current_words, remaining_words,
                )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{user_prompt}\n\n{continuation_instruction}"},
            ]
            chunk_tokens = remaining_tokens

        if chunk_tokens < 200:
            log.warning("Remaining token budget too small (%d) — stopping.", chunk_tokens)
            break

        chunk = await voidai_client.chat_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=chunk_tokens,
        )

        full_output += ("\n" if full_output else "") + chunk
        chunk_words = len(chunk.split())
        narration_words = _count_narration_words(full_output)
        log.info(
            "Script chunk %d/%d: %d chars / %d raw words → "
            "total %d chars / %d narration words (budget: %d narration words)",
            chunk_num, MAX_SCRIPT_CHUNKS,
            len(chunk), chunk_words,
            len(full_output), narration_words,
            word_budget,
        )

        # Use the strict marker-only regex here so that "Thank you for being here"
        # in a regular outro section does not falsely trigger early-stop or CTA-strip.
        if _FINAL_CTA_MARKER_RE.search(full_output):
            if narration_words < min_words_for_cta:
                # LLM closed script too early — strip premature CTA and request more.
                cta_match = _FINAL_CTA_MARKER_RE.search(full_output)
                full_output = full_output[: cta_match.start()].rstrip()
                log.warning(
                    "Premature CTA stripped: %d narration words written but minimum is %d "
                    "(duration_min=%d min) — requesting more content (chunk %d/%d)",
                    narration_words, min_words_for_cta, duration_min, chunk_num, MAX_SCRIPT_CHUNKS,
                )
                # Don't break — fall through to next chunk
            else:
                log.info(
                    "Script complete (%d narration words, min=%d) after %d chunk(s)",
                    narration_words, min_words_for_cta, chunk_num,
                )
                break

        if chunk_num == MAX_SCRIPT_CHUNKS:
            log.warning(
                "Reached max chunks (%d) without finding final CTA — using partial output "
                "(%d narration words / %d chars)",
                MAX_SCRIPT_CHUNKS, narration_words, len(full_output),
            )

    # ── Post-loop expansion guard ──────────────────────────────────────────────
    # Catches the case where the LLM ended script too early (narration below minimum).
    # Key fix: check word count FIRST; if short AND CTA marker is already present,
    # strip it before expansion — otherwise the guard was silently skipped when LLM
    # added [CTA_SUBSCRIBE_FINAL] prematurely (causing 7-min scripts instead of 25+).
    _final_narration = _count_narration_words(full_output)
    if _final_narration < min_words_for_cta:
        if _FINAL_CTA_MARKER_RE.search(full_output):
            full_output = _FINAL_CTA_MARKER_RE.sub("", full_output).rstrip()
            log.warning(
                "Post-loop: stripped premature CTA — script has only %d narration words "
                "(min %d); will expand to hit target",
                _final_narration, min_words_for_cta,
            )
        _shortage = min_words_for_cta - _final_narration
        log.warning(
            "Script below minimum after all chunks: %d narration words (need %d, short %d) "
            "— requesting targeted expansion",
            _final_narration, min_words_for_cta, _shortage,
        )
        _tail = full_output[-2_000:]
        _expansion_msg = (
            f"The script is {_shortage} narration words too short "
            f"(written: {_final_narration}, minimum: {min_words_for_cta} for a "
            f"{duration_min}-min video). Here is where it ended:\n```\n{_tail}\n```\n\n"
            f"Continue expanding existing sections with deeper analysis, examples, and "
            f"psychological insights. Add at least {_shortage + 100} more narration words, "
            f"then conclude with [CTA_SUBSCRIBE_FINAL] and the full sign-off."
        )
        try:
            _expansion_chunk = await voidai_client.chat_completion(
                model=model,
                messages=[
                    {"role": "system",    "content": system_prompt},
                    {"role": "user",      "content": user_prompt},
                    {"role": "assistant", "content": full_output},
                    {"role": "user",      "content": _expansion_msg},
                ],
                temperature=temperature,
                max_tokens=min(6_000, int(_shortage * 2.5)),
            )
            full_output += "\n" + _expansion_chunk
            log.info(
                "Expansion added %d narration words (total now: %d / min: %d)",
                _count_narration_words(_expansion_chunk),
                _count_narration_words(full_output),
                min_words_for_cta,
            )
        except Exception as _exp_err:
            log.warning("Expansion call failed (non-fatal): %s", _exp_err)

    # ── CTA repair / dedup ────────────────────────────────────────────────────
    # [CTA_SUBSCRIBE_FINAL] can appear multiple times when:
    #   • LLM echoes the marker mid-output then again at the end
    #   • Chunk boundary splits mid-marker and model restarts it
    # Strategy:
    #   1. Collect ALL explicit marker positions (or fallback to regex match).
    #   2. Check the LAST occurrence — if truncated (< 80 words, no terminal punct):
    #      a. Complete copy exists earlier → strip truncated tail. No API call.
    #      b. No complete copy → repair via targeted API call (original behaviour).
    cta_search = _FINAL_CTA_RE.search(full_output)
    if cta_search:
        _marker = "[CTA_SUBSCRIBE_FINAL]"
        all_positions = [m.start() for m in re.finditer(re.escape(_marker), full_output)]

        if not all_positions:
            # "Thank you for being here" matched but no explicit marker
            all_positions = [cta_search.start()]

        last_pos  = all_positions[-1]
        last_tail = full_output[last_pos:].rstrip()
        last_words    = len(last_tail.split())
        last_ends_ok  = bool(last_tail) and last_tail[-1] in ".!?\""

        if last_words < 80 and not last_ends_ok:
            # Last CTA section looks truncated.
            # Check whether a complete CTA section exists at an earlier position.
            earlier_complete: int | None = None
            for pos in reversed(all_positions[:-1]):   # all positions except the last
                tail = full_output[pos:].rstrip()
                if len(tail.split()) >= 80 or (tail and tail[-1] in ".!?\""):
                    earlier_complete = pos
                    break

            if earlier_complete is not None:
                # A complete CTA already exists — just strip the truncated duplicate tail.
                log.info(
                    "CTA last occurrence truncated (%d words, last=%r) — "
                    "complete copy found at earlier position; stripping duplicate tail",
                    last_words, last_tail[-1] if last_tail else "",
                )
                full_output = full_output[:last_pos].rstrip()
            else:
                # Only one (truncated) CTA — do targeted repair via API.
                log.warning(
                    "CTA appears truncated (%d words, last char=%r) — requesting repair",
                    last_words, last_tail[-1] if last_tail else "",
                )
                repair_tail = full_output[-1_500:]
                repair_instruction = (
                    f"The previous response was cut off during the final CTA (hit token limit). "
                    f"Here is exactly where it ended:\n```\n{repair_tail}\n```\n\n"
                    f"Complete ONLY the [CTA_SUBSCRIBE_FINAL] section — pick up exactly where it was "
                    f"cut and finish it naturally. "
                    f"End the CTA with: 'Thank you for being here. I will see you in the next one.'\n"
                    f"Do NOT repeat any narration that came before the CTA."
                )
                try:
                    repair_chunk = await voidai_client.chat_completion(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"{user_prompt}\n\n{repair_instruction}"},
                        ],
                        temperature=temperature,
                        max_tokens=500,
                    )
                    # Replace the truncated CTA section with the repaired version
                    full_output = full_output[:last_pos].rstrip() + "\n\n" + repair_chunk.strip()
                    log.info(
                        "CTA repaired: %d chars added (total output now %d words)",
                        len(repair_chunk), len(full_output.split()),
                    )
                except Exception as exc:
                    log.warning("CTA repair call failed (%s: %s) — keeping truncated CTA", type(exc).__name__, exc)

    final_words = len(full_output.split())
    log.info(
        "LLM output: %d words (target: ≤%d, budget: %d)",
        final_words, duration_max * 150, word_budget,
    )
    return full_output


async def _generate_one_variant(
    source_data: dict[str, Any],
    channel_config: dict[str, Any],
    model: str,
    template: str,
    hook_type: str,
    duration_min: int,
    duration_max: int,
    voidai_client: Any,
    do_validate: bool = True,
    temperature: float = 0.7,
    image_style: str = "",
    custom_topic: str = "",
) -> Script:
    """Generate and optionally validate a single script variant."""
    system_prompt = _build_system_prompt(channel_config)
    user_prompt = _build_user_prompt(
        source_data, channel_config, template, hook_type, duration_min, duration_max,
        image_style=image_style,
        custom_topic=custom_topic,
    )

    log.info(
        "Calling LLM: model=%s template=%s hook=%s temperature=%.2f",
        model, template, hook_type, temperature,
    )

    raw = await _call_llm(
        system_prompt, user_prompt, model, voidai_client, temperature,
        duration_min=duration_min,
        duration_max=duration_max,
    )
    log.info("LLM response: %d chars", len(raw))

    # Debug: save raw LLM output for inspection
    _debug_path = ROOT / "projects" / Path(source_data.get("source_dir", "debug")).name / "llm_raw_output.txt"
    try:
        _debug_path.parent.mkdir(parents=True, exist_ok=True)
        _debug_path.write_text(raw, encoding="utf-8")
        log.debug("Raw LLM output saved: %s", _debug_path)
    except Exception:
        pass

    script = _parse_llm_output(raw, channel_config, source_data, hook_type, image_style=image_style, custom_topic=custom_topic)
    # Store target duration range in script metadata
    script = script.model_copy(update={"duration_min": duration_min, "duration_max": duration_max})
    log.info("Parsed %d blocks from LLM output", len(script.blocks))

    if not do_validate or not script.blocks:
        return script

    # ── Hook validation loop (5+5 two-round strategy) ──
    hooks_cfg = channel_config.get("hooks", {})
    should_validate = hooks_cfg.get("auto_validate", True)
    max_attempts = hooks_cfg.get("max_hook_attempts", MAX_HOOK_ATTEMPTS)

    if not should_validate:
        return script

    niche = channel_config.get("niche", "")
    audience = channel_config.get("target_audience", "")
    # Pass the actual video topic so validator evaluates CLARITY against the specific
    # content, not just the channel niche (e.g. niche="history" but topic="Carl Jung shadow work").
    # custom_topic wins over reference title when provided.
    topic = custom_topic.strip() if custom_topic.strip() else source_data.get("title", "")

    # Hook pass threshold: 4/4 criteria required.
    # CLARITY allows Context Lean-In (topic clear by sentence 4-5, not sentence 1-2).
    # Strategy:
    #   Round 1 (attempts 1–5): collect candidates, exit early if ≥4/4.
    #   After attempt 5: if best candidate ≥3/4, use it; else start round 2.
    #   Round 2 (attempts 6–10): continue regenerating, exit if ≥4/4.
    #   After attempt 10: use best available regardless of score.
    hook_pass_threshold = 4
    round1_threshold    = 3   # minimum acceptable score after round 1

    intro_block = script.blocks[0]
    candidates: list[tuple[str, int, HookInfo]] = []  # (narration, score, hook_meta)

    for attempt in range(1, max_attempts + 1):
        log.info("Hook validation (attempt %d/%d)...", attempt, max_attempts)

        result = await _validate_intro_hook(
            intro_block.narration, niche, audience, voidai_client, topic=topic
        )

        updated_hook = HookInfo(
            type=hook_type,
            formula="context_lean + scroll_stop + snapback",
            validation_score=result.pass_count,
        )
        intro_block = intro_block.model_copy(update={"hook": updated_hook})
        candidates.append((intro_block.narration, result.pass_count, updated_hook))

        is_passed = result.passed or (result.pass_count >= hook_pass_threshold)

        if is_passed:
            log.info(
                "Hook validation PASSED (%d/4 criteria, threshold=%d): %s",
                result.pass_count, hook_pass_threshold,
                [k for k, v in result.criteria.items() if v.get("pass", False)],
            )
            break

        log.warning(
            "Hook validation FAILED (%d/4, threshold=%d). Failed: %s",
            result.pass_count, hook_pass_threshold,
            result.failed_criteria,
        )

        # After round 1 (attempt 5): check if any candidate meets ≥3/4 threshold
        if attempt == HOOK_ROUND_SIZE:
            best_narr, best_score, best_hook = max(candidates, key=lambda x: x[1])
            if best_score >= round1_threshold:
                log.info(
                    "Round 1 complete — best candidate %d/4 meets ≥%d threshold. Using it.",
                    best_score, round1_threshold,
                )
                intro_block = intro_block.model_copy(
                    update={"narration": best_narr, "hook": best_hook}
                )
                break
            log.info(
                "Round 1 complete — best is only %d/4 (below %d). Starting round 2.",
                best_score, round1_threshold,
            )

        # After max attempts: use best available
        if attempt == max_attempts:
            best_narr, best_score, best_hook = max(candidates, key=lambda x: x[1])
            log.warning(
                "Max attempts (%d) reached — using best available (%d/4).",
                max_attempts, best_score,
            )
            intro_block = intro_block.model_copy(
                update={"narration": best_narr, "hook": best_hook}
            )
            break

        # Regenerate the hook using the same production model with targeted feedback.
        # Using production model (e.g. Opus) produces dramatically better results than
        # nano's generic suggested_rewrite.
        failed_feedback = "\n".join(
            f"  - {k.upper()}: {v.get('feedback', '')}"
            for k, v in result.criteria.items()
            if not v.get("pass", False)
        )
        regen_prompt = (
            f"Rewrite ONLY the HOOK (opening narration block) for this video.\n\n"
            f"Video topic (MUST be the subject of the hook): \"{topic}\"\n\n"
            f"The previous hook FAILED on these criteria:\n{failed_feedback}\n\n"
            f"Previous hook:\n\"\"\"\n{intro_block.narration}\n\"\"\"\n\n"
            f"Write a new 80–120 word hook STRICTLY about the topic above. "
            f"Do NOT drift to unrelated subjects even if the channel niche suggests it.\n"
            f"Rules:\n"
            f"- Follow the v3 HOOK formula: Context Lean-In (2-3 sentences) → contrast word "
            f"(But/However/Yet/And yet) → Contrarian Snapback (unexpected insight or reversal)\n"
            f"- Short staccato sentences in the first 3-4 sentences\n"
            f"- Use 'you/your' language — speak directly to the viewer's pain\n"
            f"- Create an open loop the viewer must keep watching to resolve\n"
            f"- Zero filler: no 'In this video', 'Today we', 'Welcome back'\n"
            f"- Output ONLY the narration text. No labels, no headers, no quotes."
        )
        try:
            new_hook_text = await voidai_client.chat_completion(
                model=model,
                messages=[{"role": "user", "content": regen_prompt}],
                temperature=0.8,
                max_tokens=300,
            )
            new_hook_text = new_hook_text.strip()
            intro_block = intro_block.model_copy(
                update={"narration": new_hook_text, "hook": updated_hook}
            )
            log.info(
                "Hook regenerated (attempt %d) by %s (%d chars): %.120s...",
                attempt, model, len(new_hook_text), new_hook_text.replace("\n", " "),
            )
        except Exception as exc:
            log.warning(
                "Hook regeneration failed (%s: %s) — using best candidate so far",
                type(exc).__name__, exc,
            )
            best_narr, best_score, best_hook = max(candidates, key=lambda x: x[1])
            intro_block = intro_block.model_copy(
                update={"narration": best_narr, "hook": best_hook}
            )
            break

    script.blocks[0] = intro_block
    return script


# ─── Main API ─────────────────────────────────────────────────────────────────

async def generate_scripts(
    source_dir: str | Path | None,
    channel_config_path: str | Path,
    *,
    template: str = "auto",
    preset: str | None = None,
    compare: int = 1,
    dry_run: bool = False,
    output_dir: str | Path | None = None,
    hook_type: str = "auto",
    duration_min: int = 8,
    duration_max: int = 12,
    no_validate: bool = False,
    master_prompt_path: str | None = None,
    image_style: str = "",
    custom_topic: str = "",
) -> list[Path]:
    """
    Generate script(s) from Transcriber output or a custom topic alone.

    Args:
        source_dir: Path to Transcriber output directory.
                    Pass ``None`` for topic-only mode (no reference video needed).
        template: Content template (documentary/listicle/tutorial/comparison/auto).
        preset: LLM quality preset (max/high/balanced/bulk/test). Default from config.
        compare: Number of script variants to generate.
        dry_run: Estimate cost without API calls.
        output_dir: Directory to save script.json. Default: source_dir.
        hook_type: Hook type override. Default: auto (from template).
        duration_min: Minimum target video duration in minutes.
        duration_max: Maximum target video duration in minutes.
        no_validate: Skip hook validation step.
        master_prompt_path: Override master prompt path (bypasses channel config).

    Returns:
        List of saved script.json paths (empty for dry_run).
    """
    load_env()

    channel_config = load_channel_config(channel_config_path)

    if source_dir is not None:
        source_data = load_transcriber_output(source_dir)
    else:
        # Topic-only mode: no reference video — generate purely from custom_topic
        if not custom_topic.strip():
            raise ValueError("custom_topic is required when source_dir is None (topic-only mode)")
        log.info("Topic-only mode: generating from topic: %s", custom_topic[:80])
        source_data = {
            "transcript": "",
            "transcript_srt": None,
            "metadata": {
                "language": channel_config.get("language", "en"),
                "duration_seconds": 0,
            },
            "title": custom_topic,
            "description": "",
            "thumbnail": None,
            "thumbnail_prompt": "",
            "source_dir": str(output_dir) if output_dir else "",
        }

    # Apply master_prompt_path override (bypasses channel config lookup)
    if master_prompt_path:
        channel_config = {**channel_config, "master_prompt_path": master_prompt_path}
        log.info("Master prompt overridden: %s", master_prompt_path)

    # Resolve template
    if template == "auto":
        template = channel_config.get("default_template", "documentary")
    log.info("Template: %s", template)

    # Resolve hook type
    if hook_type == "auto":
        hooks_cfg = channel_config.get("hooks", {})
        allowed = hooks_cfg.get("allowed_types", list(HOOK_INSTRUCTIONS.keys()))
        hook_type = random.choice(allowed)
        log.info("Hook type: %s (randomly selected from %s)", hook_type, allowed)
    else:
        log.info("Hook type: %s (explicit)", hook_type)

    # Resolve LLM model
    llm_preset = get_llm_preset(channel_config, preset)
    model = llm_preset["script"]
    preset_name = preset or channel_config.get("llm", {}).get("default_preset", "max")
    log.info("LLM model: %s (preset=%s)", model, preset_name)

    if dry_run:
        log.info(
            "[DRY RUN] Would generate %d variant(s): model=%s template=%s hook=%s duration=%d-%dmin",
            compare, model, template, hook_type, duration_min, duration_max,
        )
        est_words = (duration_min + duration_max) // 2 * 145
        est_tokens = int(est_words * 1.3)
        log.info("[DRY RUN] Estimated output tokens per script: ~%d–%d", est_tokens - 500, est_tokens + 500)
        log.info("[DRY RUN] No API calls made.")
        return []

    if output_dir:
        out_dir = Path(output_dir)
    elif source_dir is not None:
        out_dir = Path(source_dir)
    else:
        raise ValueError("output_dir is required in topic-only mode (source_dir=None)")
    out_dir.mkdir(parents=True, exist_ok=True)

    from clients.voidai_client import VoidAIClient       # noqa: PLC0415
    from modules.script_validator import validate_and_fix  # noqa: PLC0415

    saved: list[Path] = []

    async with VoidAIClient() as voidai:
        for i in range(compare):
            # Higher base temperature for hook creativity + slight variance per variant
            temperature = 0.9 + (i * 0.05)

            log.info(
                "Generating variant %d/%d (temperature=%.2f)...", i + 1, compare, temperature
            )

            script = await _generate_one_variant(
                source_data=source_data,
                channel_config=channel_config,
                model=model,
                template=template,
                hook_type=hook_type,
                duration_min=duration_min,
                duration_max=duration_max,
                voidai_client=voidai,
                do_validate=not no_validate,
                temperature=temperature,
                image_style=image_style,
                custom_topic=custom_topic,
            )

            # Determine output filename
            if compare == 1:
                out_path = out_dir / "script.json"
            else:
                out_path = out_dir / f"script_v{i + 1}.json"

            script_dict = script.model_dump()

            # ── Post-generation validation and auto-fix ──────────────────────
            script_dict, val_issues = validate_and_fix(script_dict)
            if val_issues:
                log.warning(
                    "Script validator: %d auto-fix(es) applied to variant %d/%d:",
                    len(val_issues), i + 1, compare,
                )
                for iss in val_issues:
                    log.warning("  %s", iss)

            out_path.write_text(
                json.dumps(script_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            fixed_blocks = script_dict.get("blocks", [])
            log.info(
                "Saved: %s (%d blocks, %d chars narration)",
                out_path,
                len(fixed_blocks),
                sum(len(b.get("narration", "")) for b in fixed_blocks),
            )
            saved.append(out_path)

    return saved


# ─── CLI ──────────────────────────────────────────────────────────────────────

async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — Script Generator (Module 01)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python modules/01_script_generator.py \\
      --source "D:/transscript batch/output/output/My Video" \\
      --channel config/channels/example_history.json

  python modules/01_script_generator.py \\
      --source "D:/transscript batch/output/output/My Video" \\
      --channel config/channels/example_history.json \\
      --template listicle --preset high --duration 10

  # Generate 3 variants for A/B comparison:
  python modules/01_script_generator.py \\
      --source "D:/transscript batch/output/output/My Video" \\
      --channel config/channels/example_history.json \\
      --compare 3

  # Dry run (no API calls, just estimate):
  python modules/01_script_generator.py \\
      --source "D:/transscript batch/output/output/My Video" \\
      --channel config/channels/example_history.json \\
      --dry-run
        """,
    )

    parser.add_argument(
        "--source",
        required=True,
        help="Transcriber output directory (contains transcript.txt, metadata.json, etc.)",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel config JSON path (e.g. config/channels/history.json)",
    )
    parser.add_argument(
        "--template",
        default="auto",
        choices=["documentary", "listicle", "tutorial", "comparison", "auto"],
        help="Content template. 'auto' reads from channel config (default: auto)",
    )
    parser.add_argument(
        "--preset",
        choices=["max", "high", "balanced", "bulk", "test"],
        help="LLM quality preset. Default: from channel config (usually 'max')",
    )
    parser.add_argument(
        "--compare",
        type=int,
        default=1,
        metavar="N",
        help="Generate N script variants (saved as script_v1.json, script_v2.json, ...)",
    )
    parser.add_argument(
        "--output",
        help="Output directory for script.json (default: same as --source)",
    )
    parser.add_argument(
        "--hook-type",
        default="auto",
        choices=["curiosity", "negative", "storytelling", "challenge", "comparison", "auto"],
        help="Hook type for intro block. 'auto' picks from template config (default: auto)",
    )
    parser.add_argument(
        "--duration-min",
        type=int,
        default=None,
        dest="duration_min",
        help="Minimum target duration in minutes (default: 8)",
    )
    parser.add_argument(
        "--duration-max",
        type=int,
        default=None,
        dest="duration_max",
        help="Maximum target duration in minutes (default: 12)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Legacy: set both min and max to the same value (e.g. --duration 12)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate cost and show plan without making API calls",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip hook validation (faster, but no intro quality check)",
    )
    parser.add_argument(
        "--custom-topic",
        default="",
        dest="custom_topic",
        help="Override video topic for the script (reference video used as structural template only)",
    )

    args = parser.parse_args()

    if args.compare < 1:
        parser.error("--compare must be >= 1")

    # Resolve duration: --duration is a legacy alias for setting both min and max
    if args.duration is not None:
        resolved_min = args.duration
        resolved_max = args.duration
    else:
        resolved_min = args.duration_min if args.duration_min is not None else 8
        resolved_max = args.duration_max if args.duration_max is not None else 12
    if resolved_min > resolved_max:
        parser.error(f"--duration-min ({resolved_min}) must be <= --duration-max ({resolved_max})")

    t0 = time.monotonic()

    paths = await generate_scripts(
        source_dir=args.source,
        channel_config_path=args.channel,
        template=args.template,
        preset=args.preset,
        compare=args.compare,
        dry_run=args.dry_run,
        output_dir=args.output,
        hook_type=args.hook_type,
        duration_min=resolved_min,
        duration_max=resolved_max,
        no_validate=args.no_validate,
        custom_topic=args.custom_topic,
    )

    elapsed = time.monotonic() - t0

    if args.dry_run:
        log.info("[DRY RUN] Completed in %.1fs", elapsed)
    elif paths:
        log.info("Done: %d script(s) in %.1fs", len(paths), elapsed)
        for p in paths:
            log.info("  -> %s", p)
    else:
        log.error("No scripts were generated!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
