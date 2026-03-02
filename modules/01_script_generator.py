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
VALIDATOR_MODEL = "gpt-4.1-nano"
MAX_INTRO_REGEN = 2  # Default max intro regeneration attempts
MAX_TRANSCRIPT_CHARS = 14_000   # ~10K tokens — keeps total prompt manageable for Opus
MAX_HOOKS_GUIDE_CHARS = 8_000   # hooks_guide.md is now compact (~4KB), allow full pass-through

# Chunked generation — each Opus call produces ~10K chars to avoid timeout
MAX_TOKENS_PER_CHUNK = 2_500    # ~10K chars output per API call
MAX_SCRIPT_CHUNKS = 4           # max continuation attempts before giving up

BlockType = Literal["intro", "section", "cta", "outro"]

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
_CTA_MID_RE = re.compile(r"^\[CTA_SUBSCRIBE_MID\]\s*$", re.IGNORECASE)
_CTA_FINAL_RE = re.compile(r"^\[CTA_SUBSCRIBE_FINAL\]\s*$", re.IGNORECASE)


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
    all_image_prompts: list[str] = []   # accumulates ALL [IMAGE_PROMPT:] in current section
    narration_lines: list[str] = []

    def flush() -> None:
        nonlocal order, all_image_prompts, narration_lines, section_title, is_cta_block

        # Strip inline IMAGE_PROMPTs from narration (closed: [IMAGE_PROMPT: ...])
        raw_narration = "\n".join(narration_lines)
        narration = _IMAGE_INLINE_RE.sub("", raw_narration).strip()
        # Strip unclosed [IMAGE_PROMPT: tags (no closing ]) — stops at \n\n to preserve text
        # after a paragraph break that follows a malformed/truncated tag
        narration = re.sub(r"\[IMAGE_PROMPT:.*?(?=\n\n|\Z)", "", narration, flags=re.IGNORECASE | re.DOTALL).strip()
        narration = re.sub(r"\n{3,}", "\n\n", narration)

        if not narration and not all_image_prompts:
            narration_lines = []
            return

        is_intro = order == 0
        hook_info = (
            HookInfo(type=hook_type, formula="context_lean + scroll_stop + snapback")
            if is_intro
            else None
        )

        actual_type: BlockType = "cta" if is_cta_block else section_type
        primary_prompt = all_image_prompts[0] if all_image_prompts else ""

        blocks.append(
            ScriptBlock(
                id=f"block_{order + 1:03d}",
                order=order + 1,
                type=actual_type,
                narration=narration,
                image_prompt=primary_prompt,
                image_prompts=list(all_image_prompts),  # copy so reset below doesn't mutate
                animation=default_animation,
                timestamp_label=section_title,
                hook=hook_info,
            )
        )
        order += 1
        all_image_prompts = []
        narration_lines = []

    lines = raw.splitlines()

    # Check if raw output has any section markers
    has_sections = bool(_SECTION_RE.search(raw))

    if not has_sections:
        # Fallback: treat entire output as one block, extract all image prompts
        found_images = [img.strip() for img in _IMAGE_INLINE_RE.findall(raw) if img.strip()]
        narration = _IMAGE_INLINE_RE.sub("", raw).strip()
        narration = re.sub(r"\n{3,}", "\n\n", narration)
        blocks.append(
            ScriptBlock(
                id="block_001",
                order=1,
                type="intro",
                narration=narration,
                image_prompt=found_images[0] if found_images else "",
                image_prompts=found_images,
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

            # [IMAGE_PROMPT: ...] standalone line (closed — has ] on same line)
            # Collect ALL prompts per section (not just the first — v2 prompt has multiple per section)
            img_m = _IMAGE_LINE_RE.match(stripped)
            if img_m:
                all_image_prompts.append(img_m.group(1).strip())
                # Skip adding to narration — it's a visual directive
                continue

            # Unclosed [IMAGE_PROMPT: tag (line starts with tag but has no closing ])
            # LLM sometimes omits ] or the response is truncated mid-tag
            if re.match(r"^\[IMAGE_PROMPT:", stripped, re.IGNORECASE) and "]" not in stripped:
                salvaged = re.sub(r"^\[IMAGE_PROMPT:\s*", "", stripped, flags=re.IGNORECASE).strip(" ,")
                if salvaged:
                    all_image_prompts.append(salvaged)
                # Never add raw tag text to narration
                continue

            narration_lines.append(stripped)

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
    video_title = source_data.get("title") or meta.get("title") or "Untitled"
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
            image_style=channel_config.get("image_style", ""),
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
) -> str:
    """Build user message (transcript + topic + special requests)."""
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

    # Build new topic string
    new_topic = title
    if description:
        desc_short = description[:300].replace("\n", " ").strip()
        new_topic += f"\n   Context: {desc_short}"

    # Compute target word count range (140-150 wpm speaking pace)
    target_words_min = duration_min * 140
    target_words_max = duration_max * 150

    return (
        f"[TRANSCRIPTION]\n{transcript}\n\n"
        f"__NEW TOPIC__: {new_topic}\n"
        f"[DURATION]: {duration_min}-{duration_max} minutes\n"
        f"[TARGET WORDS]: {target_words_min}–{target_words_max} words\n"
        f"__SPECIAL REQUESTS__:\n{special}"
    )


# ─── Hook Validation ──────────────────────────────────────────────────────────

async def _validate_intro_hook(
    intro_narration: str,
    niche: str,
    audience: str,
    voidai_client: Any,
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

_FINAL_CTA_RE = re.compile(r"\[CTA_SUBSCRIBE_FINAL\]|Thank you for being here", re.IGNORECASE)
_LAST_SECTION_NUM_RE = re.compile(r"\[SECTION\s+(\d+)\s*:", re.IGNORECASE)


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str,
    voidai_client: Any,
    temperature: float = 0.7,
) -> str:
    """
    Generate script in chunks of ~10K chars to avoid Opus timeout on long scripts.

    Strategy:
    - Call 1: normal prompt → max MAX_TOKENS_PER_CHUNK tokens
    - If no final CTA found → continuation call with tail context (up to MAX_SCRIPT_CHUNKS)
    - Continuation provides last 2K chars + "continue from Section N" instruction
    """
    full_output = ""

    for chunk_num in range(1, MAX_SCRIPT_CHUNKS + 1):
        if chunk_num == 1:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        else:
            # Detect last section number so continuation knows where to resume
            section_nums = _LAST_SECTION_NUM_RE.findall(full_output)
            last_section = int(section_nums[-1]) if section_nums else 0
            tail = full_output[-2_000:]  # last 2K chars as context anchor

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"{user_prompt}\n\n"
                    f"CONTINUATION — you already wrote sections 1-{last_section}. "
                    f"Here is the end of what you wrote so far:\n```\n{tail}\n```\n\n"
                    f"Continue the script starting with [SECTION {last_section + 1}: ...]. "
                    f"Do NOT repeat any content from sections 1-{last_section}. "
                    f"Write from Section {last_section + 1} through the final CTA."
                )},
            ]
            log.info(
                "Script continuation request: chunk %d/%d, resuming after Section %d",
                chunk_num, MAX_SCRIPT_CHUNKS, last_section,
            )

        chunk = await voidai_client.chat_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=MAX_TOKENS_PER_CHUNK,
        )

        full_output += ("\n" if full_output else "") + chunk
        log.info(
            "Script chunk %d/%d: %d chars → total %d chars",
            chunk_num, MAX_SCRIPT_CHUNKS, len(chunk), len(full_output),
        )

        if _FINAL_CTA_RE.search(full_output):
            log.info("Script complete (final CTA found) after %d chunk(s)", chunk_num)
            break

        if chunk_num == MAX_SCRIPT_CHUNKS:
            log.warning(
                "Reached max chunks (%d) without finding final CTA — using partial output (%d chars)",
                MAX_SCRIPT_CHUNKS, len(full_output),
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
) -> Script:
    """Generate and optionally validate a single script variant."""
    system_prompt = _build_system_prompt(channel_config)
    user_prompt = _build_user_prompt(
        source_data, channel_config, template, hook_type, duration_min, duration_max
    )

    log.info(
        "Calling LLM: model=%s template=%s hook=%s temperature=%.2f",
        model, template, hook_type, temperature,
    )

    raw = await _call_llm(system_prompt, user_prompt, model, voidai_client, temperature)
    log.info("LLM response: %d chars", len(raw))

    # Debug: save raw LLM output for inspection
    _debug_path = ROOT / "projects" / Path(source_data.get("source_dir", "debug")).name / "llm_raw_output.txt"
    try:
        _debug_path.parent.mkdir(parents=True, exist_ok=True)
        _debug_path.write_text(raw, encoding="utf-8")
        log.debug("Raw LLM output saved: %s", _debug_path)
    except Exception:
        pass

    script = _parse_llm_output(raw, channel_config, source_data, hook_type)
    # Store target duration range in script metadata
    script = script.model_copy(update={"duration_min": duration_min, "duration_max": duration_max})
    log.info("Parsed %d blocks from LLM output", len(script.blocks))

    if not do_validate or not script.blocks:
        return script

    # ── Hook validation loop ──
    hooks_cfg = channel_config.get("hooks", {})
    should_validate = hooks_cfg.get("auto_validate", True)
    max_regen = hooks_cfg.get("max_regenerate_intro", MAX_INTRO_REGEN)

    if not should_validate:
        return script

    niche = channel_config.get("niche", "")
    audience = channel_config.get("target_audience", "")

    intro_block = script.blocks[0]

    for attempt in range(1, max_regen + 1):
        log.info("Hook validation (attempt %d/%d)...", attempt, max_regen)

        result = await _validate_intro_hook(
            intro_block.narration, niche, audience, voidai_client
        )

        # Store validation score in hook metadata
        updated_hook = HookInfo(
            type=hook_type,
            formula="context_lean + scroll_stop + snapback",
            validation_score=result.pass_count,
        )
        intro_block = intro_block.model_copy(update={"hook": updated_hook})

        if result.passed:
            log.info(
                "Hook validation PASSED (%d/4 criteria: %s)",
                result.pass_count,
                [k for k, v in result.criteria.items() if v.get("pass", False)],
            )
            break

        log.warning(
            "Hook validation FAILED (%d/4). Failed: %s",
            result.pass_count,
            result.failed_criteria,
        )

        if result.suggested_rewrite:
            intro_block = intro_block.model_copy(
                update={"narration": result.suggested_rewrite, "hook": updated_hook}
            )
            log.info(
                "Intro replaced with validator suggested_rewrite (%d chars)",
                len(result.suggested_rewrite),
            )
        else:
            log.warning("No suggested_rewrite from validator — keeping original intro")
            break

    script.blocks[0] = intro_block
    return script


# ─── Main API ─────────────────────────────────────────────────────────────────

async def generate_scripts(
    source_dir: str | Path,
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
) -> list[Path]:
    """
    Generate script(s) from Transcriber output.

    Args:
        source_dir: Path to Transcriber output directory.
        channel_config_path: Path to channel config JSON.
        template: Content template (documentary/listicle/tutorial/comparison/auto).
        preset: LLM quality preset (max/high/balanced/bulk/test). Default from config.
        compare: Number of script variants to generate.
        dry_run: Estimate cost without API calls.
        output_dir: Directory to save script.json. Default: source_dir.
        hook_type: Hook type override. Default: auto (from template).
        duration_min: Minimum target video duration in minutes.
        duration_max: Maximum target video duration in minutes.
        no_validate: Skip hook validation step.

    Returns:
        List of saved script.json paths (empty for dry_run).
    """
    load_env()

    channel_config = load_channel_config(channel_config_path)
    source_data = load_transcriber_output(source_dir)

    # Resolve template
    if template == "auto":
        template = channel_config.get("default_template", "documentary")
    log.info("Template: %s", template)

    # Resolve hook type
    if hook_type == "auto":
        hooks_cfg = channel_config.get("hooks", {})
        per_template = hooks_cfg.get("per_template", HOOK_PER_TEMPLATE)
        hook_type = per_template.get(template, "curiosity")
    log.info("Hook type: %s", hook_type)

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

    out_dir = Path(output_dir) if output_dir else Path(source_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from clients.voidai_client import VoidAIClient  # noqa: PLC0415

    saved: list[Path] = []

    async with VoidAIClient() as voidai:
        for i in range(compare):
            # Slightly vary temperature for multiple variants
            temperature = 0.7 + (i * 0.05)

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
            )

            # Determine output filename
            if compare == 1:
                out_path = out_dir / "script.json"
            else:
                out_path = out_dir / f"script_v{i + 1}.json"

            script_dict = script.model_dump()
            out_path.write_text(
                json.dumps(script_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log.info(
                "Saved: %s (%d blocks, %d chars narration)",
                out_path,
                len(script.blocks),
                sum(len(b.narration) for b in script.blocks),
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
