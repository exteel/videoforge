"""
VideoForge — Module 01b: Script Validator & Auto-Fixer.

Validates script.json after LLM generation and auto-fixes common issues.

Detection (structural, no API cost):
  cut_off        — script ends mid-sentence / without CTA
  missing_cta    — no outro/CTA block at end
  bad_prompt     — image_prompt is empty, too short, or matches generic patterns
  duplicate      — same image concept repeated verbatim

Auto-fix (LLM):
  cut_off + missing_cta → claude-sonnet-4-5: generates continuation blocks + CTA
  bad_prompt            → gpt-4.1-mini: rewrites flagged image_prompts in one batch
  (duplicate prompts are logged but not auto-fixed — content is likely intentional)

Saves fixed script.json in-place. Returns ValidationResult.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent.parent

import sys
sys.path.insert(0, str(ROOT))

from modules.common import load_env, setup_logging

log = setup_logging("script_validator")

# ─── Constants ────────────────────────────────────────────────────────────────

VOIDAI_BASE  = "https://api.voidai.app/v1"
DETECT_MODEL = "gpt-4.1-mini"
FIX_MODEL    = "claude-sonnet-4-5-20250929"

# Patterns that indicate a generic / placeholder image prompt
_IMAGE_TAG_IN_NARRATION_RE = re.compile(r"\[IMAGE_PROMPT:", re.IGNORECASE)
# Matches unclosed [IMAGE_PROMPT: tag up to the next blank line (paragraph boundary) or end-of-string.
# Using non-greedy + lookahead stops at \n\n so we don't accidentally eat valid narration below the tag.
_UNCLOSED_IMAGE_TAG_RE    = re.compile(r"\[IMAGE_PROMPT:\s*(.*?)(?=\n\n|\Z)", re.IGNORECASE | re.DOTALL)
_CLOSED_IMAGE_INLINE_RE   = re.compile(r"\[IMAGE_PROMPT:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)

# Other parser artifact tags that may bleed into narration
_OTHER_TAG_IN_NARRATION_RE = re.compile(
    r"\[(SECTION\s+\d+|CTA_SUBSCRIBE_(?:MID|FINAL)|HOOK\s+TYPE|IMAGE\s+PROMPT)\b",
    re.IGNORECASE,
)

# Script length thresholds (in total word count across all narrations)
TOO_LONG_WORDS  = 2500   # >2500 words ≈ >18 min @ 140 wpm → warning
TOO_SHORT_WORDS = 80     # <80 total words → script is basically empty
# Per-block thresholds
BLOCK_SHORT_WORDS = 15   # narration with <15 words → likely placeholder

_GENERIC_RE = re.compile(
    r"^(abstract\s+(light|concept|background|theme|imagery)|"
    r"philosophical\s+(concept|imagery)|"
    r"dark\s+(mood|atmosphere|background|tone)|"
    r"misty\s+(light|background|scene)|"
    r"cinematic\s+(scene|background)\s*$|"
    r"symbolic\s+(image|scene|concept|representation)\s*$|"
    r"visual\s+(metaphor|concept|representation)\s*$|"
    r"atmospheric\s+(scene|image|visual)\s*$)",
    re.IGNORECASE,
)

# Words that, when they are the last word of a narration, suggest a cut-off
_CONNECTOR_WORDS = frozenset([
    "and", "but", "or", "because", "when", "that", "this", "the",
    "a", "an", "in", "on", "of", "to", "for", "with", "as", "by",
    "is", "are", "was", "were", "have", "has", "will", "would", "so",
    "yet", "nor", "at", "if", "then", "however", "also", "while",
    "which", "who", "what", "where", "how", "its", "their", "your",
])


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ScriptIssue:
    type: str           # cut_off | missing_cta | bad_prompt | duplicate_prompt
    block_id: str = ""
    severity: str = "warning"   # critical | warning
    reason: str = ""
    fixed: bool = False


@dataclass
class ValidationResult:
    ok: bool = True
    issues: list[ScriptIssue] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def has_critical(self) -> bool:
        return any(i.severity == "critical" for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "critical": sum(1 for i in self.issues if i.severity == "critical"),
            "warnings": sum(1 for i in self.issues if i.severity == "warning"),
            "issues": [
                {
                    "type": i.type,
                    "block_id": i.block_id,
                    "severity": i.severity,
                    "reason": i.reason,
                    "fixed": i.fixed,
                }
                for i in self.issues
            ],
            "fixes_applied": self.fixes_applied,
            "elapsed": round(self.elapsed, 2),
        }


# ─── Structural checks (no API) ───────────────────────────────────────────────

def _structural_checks(blocks: list[dict[str, Any]]) -> list[ScriptIssue]:
    """Fast checks that require no API call."""
    issues: list[ScriptIssue] = []

    if not blocks:
        return [ScriptIssue(type="no_blocks", severity="critical", reason="Script has no blocks")]

    # ── CTA / outro check ──
    has_cta = any(b.get("type") in ("cta", "outro") for b in blocks)
    if not has_cta:
        issues.append(ScriptIssue(
            type="missing_cta",
            block_id=blocks[-1].get("id", ""),
            severity="critical",
            reason="No CTA/outro block found in script",
        ))

    # ── Cut-off check: last NON-CTA block ends abruptly ──
    # Check the final block; also scan all blocks for internal cut-offs (mid-script truncation)
    check_cutoff = [blocks[-1]]
    # Also check the block just before the CTA if it's the penultimate
    if len(blocks) >= 2 and blocks[-1].get("type") in ("cta", "outro"):
        check_cutoff.append(blocks[-2])
    for blk in check_cutoff:
        narration = (blk.get("narration") or "").strip()
        if narration:
            ends_with_punct = narration.endswith((".", "!", "?", "…"))
            words = narration.split()
            last_word = re.sub(r"[^\w]", "", words[-1]).lower() if words else ""
            if not ends_with_punct or last_word in _CONNECTOR_WORDS:
                issues.append(ScriptIssue(
                    type="cut_off",
                    block_id=blk.get("id", ""),
                    severity="critical",
                    reason=f"Block ends abruptly: '…{narration[-80:]}'",
                ))

    # ── Per-block narration quality checks ──
    for block in blocks:
        bid  = block.get("id", "")
        narr = (block.get("narration") or "").strip()

        # Empty narration after cleaning
        if not narr and block.get("type") not in ("cta",):
            issues.append(ScriptIssue(
                type="empty_narration", block_id=bid, severity="critical",
                reason="Block has no narration text",
            ))
            continue

        # Parser artifact tags in narration ([IMAGE_PROMPT:], [SECTION], [CTA_SUBSCRIBE])
        if _IMAGE_TAG_IN_NARRATION_RE.search(narr):
            issues.append(ScriptIssue(
                type="bad_narration", block_id=bid, severity="critical",
                reason="Narration contains raw [IMAGE_PROMPT:] tag — parser artifact, will be read aloud by TTS",
            ))
        elif _OTHER_TAG_IN_NARRATION_RE.search(narr):
            m = _OTHER_TAG_IN_NARRATION_RE.search(narr)
            tag = m.group(0) if m else "unknown tag"
            issues.append(ScriptIssue(
                type="bad_narration", block_id=bid, severity="critical",
                reason=f"Narration contains raw parser tag '{tag}' — will be read aloud by TTS",
            ))

        # Too-short narration (likely placeholder or truncated)
        word_count = len(narr.split())
        if narr and word_count < BLOCK_SHORT_WORDS and block.get("type") not in ("cta",):
            issues.append(ScriptIssue(
                type="short_block", block_id=bid, severity="warning",
                reason=f"Narration too short ({word_count} words) — may be truncated or placeholder",
            ))

    # ── Script-level length check ──
    total_words = sum(len((b.get("narration") or "").split()) for b in blocks)
    if total_words > TOO_LONG_WORDS:
        est_min = round(total_words / 140, 0)
        issues.append(ScriptIssue(
            type="too_long", block_id="",
            severity="warning",
            reason=f"Script is {total_words} words (~{int(est_min)} min) — YouTube optimal is 8-15 min (1100-2100 words). Possible duplicate content.",
        ))
    elif total_words < TOO_SHORT_WORDS:
        issues.append(ScriptIssue(
            type="too_short", block_id="", severity="critical",
            reason=f"Script has only {total_words} words total — likely generation failure",
        ))

    # ── Duplicate section titles (strong signal of doubled LLM output) ──
    seen_labels: dict[str, str] = {}  # normalized_label → block_id
    for block in blocks:
        bid   = block.get("id", "")
        label = (block.get("timestamp_label") or "").strip().lower()
        if not label or label in ("hook", "subscribe", "intro", "outro"):
            continue
        # Normalize: remove "the", "a", extra spaces, punctuation
        norm = re.sub(r"\b(the|a|an)\b", "", label)
        norm = re.sub(r"[^\w\s]", "", norm).strip()
        if norm in seen_labels:
            issues.append(ScriptIssue(
                type="duplicate_section",
                block_id=bid,
                severity="warning",
                reason=f"Section title '{block.get('timestamp_label')}' duplicates block {seen_labels[norm]} — possible doubled LLM output",
            ))
        else:
            seen_labels[norm] = bid

    # ── Image prompt checks ──
    seen_prompts: dict[str, str] = {}  # prompt_key → block_id
    for block in blocks:
        bid    = block.get("id", "")
        prompt = (block.get("image_prompt") or "").strip()
        narr   = (block.get("narration") or "").strip()

        # CTA blocks don't need images
        if block.get("type") in ("cta",):
            continue

        if not prompt and narr:
            issues.append(ScriptIssue(
                type="bad_prompt", block_id=bid, severity="warning",
                reason="Missing image_prompt",
            ))
            continue

        if prompt and len(prompt) < 15:
            issues.append(ScriptIssue(
                type="bad_prompt", block_id=bid, severity="warning",
                reason=f"Image prompt too short ({len(prompt)} chars): '{prompt}'",
            ))
            continue

        if prompt and _GENERIC_RE.match(prompt):
            issues.append(ScriptIssue(
                type="bad_prompt", block_id=bid, severity="warning",
                reason=f"Generic image prompt: '{prompt[:60]}'",
            ))

        if prompt:
            key = prompt.lower()[:80]
            if key in seen_prompts:
                issues.append(ScriptIssue(
                    type="duplicate_prompt", block_id=bid, severity="warning",
                    reason=f"Duplicate of block {seen_prompts[key]}: '{prompt[:60]}'",
                ))
            else:
                seen_prompts[key] = bid

    return issues


# ─── LLM helpers ──────────────────────────────────────────────────────────────

def _api_key() -> str:
    key = os.environ.get("VOIDAI_API_KEY", "")
    if not key:
        raise RuntimeError("VOIDAI_API_KEY not set")
    return key


async def _llm(
    model: str,
    messages: list[dict],
    *,
    max_tokens: int = 2000,
    json_mode: bool = False,
) -> str:
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{VOIDAI_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ─── Auto-fix functions ───────────────────────────────────────────────────────

async def _fix_cut_off(script: dict[str, Any], image_style: str) -> list[dict[str, Any]]:
    """Generate continuation blocks for a cut-off script (adds synthesis + CTA)."""
    blocks  = script.get("blocks", [])
    title   = script.get("title", "Unknown")
    niche   = script.get("niche", "")
    language = script.get("language", "en")

    # Pass last 5 blocks for richer context (was 3)
    context_blocks = blocks[-5:] if len(blocks) >= 5 else blocks
    tail = "\n\n".join(
        f"[{b.get('type', 'section').upper()} — {b.get('timestamp_label', b.get('id', ''))}]\n"
        f"{(b.get('narration') or '')[:400]}"
        for b in context_blocks
    )

    prompt = f"""A YouTube video script was cut off before completion. Continue it naturally and close it properly.

VIDEO TITLE: "{title}"
NICHE: {niche or "general"}
LANGUAGE: {language}
IMAGE STYLE: {image_style}

SCRIPT ENDS WITH (last {len(context_blocks)} blocks):
{tail}

Requirements:
- Continue EXACTLY from where the text cuts off — no repetition of what was said
- Write 2-4 continuation section blocks that complete the argument/story arc
- Synthesize the key insight into a memorable closing takeaway
- The FINAL block must be type "outro" with a 30-50 word subscribe CTA
- Match tone, vocabulary, and depth of the original script precisely
- Each narration must end with proper punctuation (period/exclamation/question mark)

Return ONLY a JSON array (no markdown, no explanation):
[
  {{
    "id": "block_cont_001",
    "type": "section",
    "timestamp_label": "Section Title",
    "narration": "continuation text — complete sentences only, ending with punctuation.",
    "image_prompt": "specific cinematic visual (15-40 words) that matches exactly what is being said",
    "animation": "zoom_in"
  }},
  {{
    "id": "block_cont_NNN",
    "type": "outro",
    "timestamp_label": "Subscribe",
    "narration": "If this resonated with you... compelling 30-50 word CTA with specific question for comments.",
    "image_prompt": "warm, hopeful closing visual matching {image_style[:60]}",
    "animation": "zoom_in"
  }}
]"""
    raw = await _llm(FIX_MODEL, [{"role": "user", "content": prompt}], max_tokens=3000)
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON array in cut_off fix response")
    return json.loads(match.group())


async def _fix_missing_cta(image_style: str) -> dict[str, Any]:
    """Generate a standalone CTA outro block."""
    prompt = f"""Generate a single YouTube CTA outro block for a video.
IMAGE STYLE: {image_style}

Return ONLY a JSON object:
{{
  "id": "block_outro",
  "type": "outro",
  "narration": "compelling 40-60 word CTA — ask viewers to subscribe and drop a comment with a specific question",
  "image_prompt": "warm, inspiring closing visual, 20-30 words, matches the style: {image_style}",
  "animation": "zoom_in"
}}"""
    raw = await _llm(DETECT_MODEL, [{"role": "user", "content": prompt}], max_tokens=400, json_mode=True)
    return json.loads(raw)


async def _fix_bad_prompts(bad_blocks: list[dict[str, Any]]) -> dict[str, str]:
    """Rewrite bad image_prompts in a single batch LLM call."""
    if not bad_blocks:
        return {}
    blocks_info = "\n\n".join(
        f"Block {b['id']}:\n  narration: \"{(b.get('narration') or '')[:200]}\"\n  bad_prompt: \"{b.get('image_prompt', '')}\""
        for b in bad_blocks
    )
    prompt = f"""Rewrite these image prompts. Each must directly illustrate exactly what the narration says — not the general theme.

{blocks_info}

Rules:
- 15-50 words per prompt
- Must be specific to that block's exact narration content
- Cinematic, atmospheric, suitable for AI image generation
- No generic phrases ("abstract concept", "dark mood", "philosophical", "misty light")

Return ONLY valid JSON: {{"block_id": "new_prompt", ...}}"""
    raw = await _llm(DETECT_MODEL, [{"role": "user", "content": prompt}], max_tokens=1500, json_mode=True)
    return json.loads(raw)


# ─── Main validator ───────────────────────────────────────────────────────────

async def validate_and_fix_script(
    script_path: str | Path,
    channel_config: dict[str, Any] | None = None,
    *,
    auto_fix: bool = True,
    progress_callback: Any = None,
) -> ValidationResult:
    """
    Validate script.json and auto-fix critical issues via LLM.

    Args:
        script_path:      Path to script.json.
        channel_config:   Channel config dict (used for image_style context).
        auto_fix:         If True, call LLM to fix detected issues.
        progress_callback: Optional callable({type, pct, message}).

    Returns:
        ValidationResult with list of issues and applied fixes.
    """
    load_env()
    t0 = time.monotonic()
    script_path = Path(script_path)

    def _emit(msg: str, pct: float = 0.0) -> None:
        if progress_callback:
            try:
                progress_callback({"type": "sub_progress", "pct": pct, "message": msg})
            except Exception:
                pass

    _emit("Validating script…", 5.0)
    script = json.loads(script_path.read_text(encoding="utf-8"))
    blocks = script.get("blocks", [])
    image_style = (channel_config or {}).get("image_style", "cinematic, photorealistic, dramatic lighting")

    # ── Structural checks (no API) ──
    _emit("Checking structure…", 15.0)
    issues = _structural_checks(blocks)
    result = ValidationResult(
        ok=not any(i.severity == "critical" for i in issues),
        issues=issues,
    )

    if not issues:
        log.info("Script validation: OK — no issues")
        result.elapsed = time.monotonic() - t0
        _emit("Script OK ✓", 100.0)
        return result

    log.info(
        "Script validation: %d issues (%d critical, %d warnings)",
        len(issues),
        sum(1 for i in issues if i.severity == "critical"),
        sum(1 for i in issues if i.severity == "warning"),
    )
    for issue in issues:
        log.info("  [%s] %s — %s", issue.severity.upper(), issue.type, issue.reason)

    if not auto_fix:
        result.elapsed = time.monotonic() - t0
        return result

    modified = False

    # ── Fix 0: bad_narration — strip embedded [IMAGE_PROMPT:] tags (no API needed) ──
    bad_narration_issues = [i for i in issues if i.type == "bad_narration" and i.block_id]
    if bad_narration_issues:
        _emit(f"Cleaning {len(bad_narration_issues)} narrations with embedded tags…", 20.0)
        bad_narr_ids = {i.block_id for i in bad_narration_issues}
        cleaned = 0
        for block in blocks:
            if block.get("id") not in bad_narr_ids:
                continue
            narr = (block.get("narration") or "").strip()
            # Salvage image_prompt from the tag if block is missing one
            if not block.get("image_prompt"):
                for m in _CLOSED_IMAGE_INLINE_RE.finditer(narr):
                    block["image_prompt"] = m.group(1).strip()
                    break
                if not block.get("image_prompt"):
                    um = _UNCLOSED_IMAGE_TAG_RE.search(narr)
                    if um:
                        salvaged = um.group(1).strip(" ,")
                        if salvaged:
                            block["image_prompt"] = salvaged
            # Strip all [IMAGE_PROMPT:...] (closed and unclosed) from narration
            narr = _CLOSED_IMAGE_INLINE_RE.sub("", narr)
            # _UNCLOSED_IMAGE_TAG_RE stops at \n\n, so text after a blank line is preserved
            narr = _UNCLOSED_IMAGE_TAG_RE.sub("", narr).strip()
            narr = re.sub(r"\n{3,}", "\n\n", narr)
            block["narration"] = narr
            cleaned += 1
        if cleaned:
            script["blocks"] = blocks
            for iss in bad_narration_issues:
                iss.fixed = True
            result.fixes_applied.append(f"Cleaned {cleaned} narrations with embedded [IMAGE_PROMPT:] tags")
            modified = True
            log.info("Fixed %d bad_narration blocks (stripped embedded tags)", cleaned)

    # ── Fix 1: cut_off (also covers missing_cta since continuation includes CTA) ──
    has_cut_off = any(i.type == "cut_off" for i in issues)
    has_no_cta  = any(i.type == "missing_cta" for i in issues)

    if has_cut_off:
        _emit("Generating continuation via LLM…", 30.0)
        try:
            new_blocks = await _fix_cut_off(script, image_style)
            base = len(blocks)
            for j, nb in enumerate(new_blocks):
                nb["id"] = f"block_{base + j + 1:03d}"
                nb.setdefault("animation", "zoom_in")
            blocks.extend(new_blocks)
            script["blocks"] = blocks
            for iss in issues:
                if iss.type in ("cut_off", "missing_cta"):
                    iss.fixed = True
            result.fixes_applied.append(
                f"Extended script with {len(new_blocks)} continuation blocks (including CTA)"
            )
            modified = True
            log.info("Fixed cut_off: added %d continuation blocks", len(new_blocks))
        except Exception as exc:
            log.warning("Failed to fix cut_off: %s", exc)

    elif has_no_cta:
        _emit("Adding CTA block…", 30.0)
        try:
            cta = await _fix_missing_cta(image_style)
            cta["id"] = f"block_{len(blocks) + 1:03d}"
            blocks.append(cta)
            script["blocks"] = blocks
            for iss in issues:
                if iss.type == "missing_cta":
                    iss.fixed = True
            result.fixes_applied.append("Appended CTA/outro block")
            modified = True
            log.info("Fixed missing_cta: added outro block")
        except Exception as exc:
            log.warning("Failed to fix missing_cta: %s", exc)

    # ── Fix 2: bad image prompts (batch rewrite) ──
    bad_prompt_issues = [i for i in issues if i.type == "bad_prompt" and i.block_id]
    if bad_prompt_issues:
        _emit(f"Rewriting {len(bad_prompt_issues)} image prompts…", 60.0)
        bad_blocks = [b for b in blocks if b.get("id") in {i.block_id for i in bad_prompt_issues}]
        if bad_blocks:
            try:
                fixed_prompts = await _fix_bad_prompts(bad_blocks)
                for bid, new_prompt in fixed_prompts.items():
                    for block in blocks:
                        if block.get("id") == bid:
                            block["image_prompt"] = new_prompt
                            break
                for iss in issues:
                    if iss.type == "bad_prompt" and iss.block_id in fixed_prompts:
                        iss.fixed = True
                script["blocks"] = blocks
                result.fixes_applied.append(f"Rewrote {len(fixed_prompts)} image prompts")
                modified = True
                log.info("Fixed %d bad image prompts", len(fixed_prompts))
            except Exception as exc:
                log.warning("Failed to fix bad_prompts: %s", exc)

    # ── Save fixed script ──
    if modified:
        _emit("Saving fixed script…", 90.0)
        script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Saved fixed script: %s", script_path)

        # ── Post-fix re-check: verify fixes actually resolved issues ──
        remaining = _structural_checks(script.get("blocks", []))
        still_critical = [i for i in remaining if i.severity == "critical"]
        if still_critical:
            log.warning(
                "Post-fix re-check: %d critical issue(s) remain after fixes:",
                len(still_critical),
            )
            for iss in still_critical:
                log.warning("  [REMAIN] %s — %s", iss.type, iss.reason)
            result.ok = False
        else:
            result.ok = True
            log.info("Post-fix re-check: all critical issues resolved ✓")
    else:
        result.ok = not any(i.severity == "critical" and not i.fixed for i in issues)

    result.elapsed = time.monotonic() - t0
    _emit("Validation complete", 100.0)
    log.info(
        "Script validation done (%.1fs): ok=%s, fixes=%s",
        result.elapsed, result.ok, result.fixes_applied,
    )
    return result


# ─── CLI self-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Script Validator — self-test")
    parser.add_argument("script", help="Path to script.json")
    parser.add_argument("--no-fix", action="store_true", help="Detect issues only, don't fix")
    args = parser.parse_args()

    load_env()
    result = asyncio.run(validate_and_fix_script(
        args.script,
        auto_fix=not args.no_fix,
    ))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
