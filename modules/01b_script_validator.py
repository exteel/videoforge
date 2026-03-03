"""
VideoForge — Module 01b: Script Validator & Auto-Fixer.

Validates script.json after LLM generation and auto-fixes common issues.

Detection (structural, no API cost):
  cut_off         — block ends mid-sentence / connector word (ALL blocks, not just last)
  missing_cta     — no outro/CTA block at end
  bad_prompt      — image_prompt is empty, too short, or matches generic patterns (expanded list)
  sparse_images   — image_prompts list < 1 per 150 narration words for blocks ≥200w
  duplicate       — same image concept repeated verbatim
  wrong_language  — narration Cyrillic ratio < 40% for declared Cyrillic-script language
  missing_field   — block missing required 'id' or 'type'
  bad_block_order — first block is CTA/outro (unexpected ordering)

Auto-fix (LLM):
  cut_off + missing_cta → claude-sonnet-4-5: generates continuation blocks + CTA
  bad_prompt            → gpt-4.1-mini: rewrites flagged image_prompts in one batch
  sparse_images         → gpt-4.1-mini: generates additional prompts for sparse blocks
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

# Generic image prompt patterns — expanded set.
# Anchored at ^ so we catch prompts that *start* with a vague/lazy description.
_GENERIC_RE = re.compile(
    r"^("
    # ── Original patterns ──────────────────────────────────────────────────────
    r"abstract\s+(light|concept|background|theme|imagery)|"
    r"philosophical\s+(concept|imagery)|"
    r"dark\s+(mood|atmosphere|background|tone)|"
    r"misty\s+(light|background|scene)|"
    r"cinematic\s+(scene|background)\s*$|"
    r"symbolic\s+(image|scene|concept|representation)\s*$|"
    r"visual\s+(metaphor|concept|representation)\s*$|"
    r"atmospheric\s+(scene|image|visual)\s*$|"
    # ── People doing vague things ───────────────────────────────────────────────
    r"a?\s*(person|human|individual|man|woman|figure)\s+"
    r"(thinking|standing|sitting|looking|walking|contemplating|pondering|reflecting)\b|"
    r"people\s+(thinking|standing|sitting|walking|looking)\b|"
    # ── "Concept / idea / symbol of X" ─────────────────────────────────────────
    r"(concept|idea|notion|theme|essence)\s+of\b|"
    r"(symbol|illustration|depiction|representation|embodiment)\s+of\b|"
    # ── "Scene / image showing X" ───────────────────────────────────────────────
    r"(scene|image|visual|picture|shot)\s+(depicting|showing|illustrating|representing)\b|"
    # ── Catch-all generic descriptors ──────────────────────────────────────────
    r"generic\b|"
    r"(simple|plain|basic|minimal)\s+(background|scene|image|visual)\s*$|"
    r"(abstract|generic|typical|standard)\s+(visual|image|scene|background)\s*$|"
    r"(moody|dramatic|emotional|powerful)\s+(atmosphere|background)\b|"
    r"(dark|light|bright|gloomy)\s+atmosphere\b|"
    r"something\s+(abstract|symbolic|metaphorical|atmospheric)\b|"
    # ── Vague camera shots ──────────────────────────────────────────────────────
    r"(wide|establishing|aerial|overhead)\s+shot\s+of\s+(something|a\s+scene|a\s+concept)\s*$"
    r")",
    re.IGNORECASE,
)

# Cyrillic-script language codes (ISO 639-1) used for language consistency check
_CYRILLIC_LANGS = frozenset(["uk", "ru", "be", "bg", "sr", "mk", "kk", "mn"])
_CYRILLIC_RE    = re.compile(r"[\u0400-\u04FF]")

# ── TTS compliance patterns (v3 narration guidelines) ─────────────────────────
# Markdown formatting that TTS reads literally ("asterisk bold asterisk")
_TTS_MARKDOWN_RE = re.compile(
    r"\*\*[^*\n]+\*\*"         # **bold**
    r"|__[^_\n]+__"             # __bold__
    r"|\*\S[^*\n]{0,40}\*"     # *italic* (non-space start to skip bullet points)
    r"|_\S[^_\n]{0,40}_"       # _italic_
    r"|^#{1,6}\s",              # # Header at line start
    re.MULTILINE,
)
_TTS_ELLIPSIS_RE   = re.compile(r"\.{2,}|…")           # ... or … — awkward TTS pause
_TTS_PARENS_RE     = re.compile(r"\([^)]{3,}\)")         # (...) parenthetical asides
_TTS_EXCLAIM_RE    = re.compile(r"!")                    # ! forbidden per v3 TTS rules
_TTS_SYMBOLS_RE    = re.compile(r"[%#&@]")              # symbols TTS reads by name
_TTS_FILLER_RE     = re.compile(                        # filler opener phrases
    r"^(in this video|today we|welcome back|hi\s+everyone|hey\s+everyone|hello\s+everyone)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE    = re.compile(r"(?<=[.?])\s+")    # split on sentence boundaries
TTS_MAX_SENTENCE_WORDS = 35  # v3 guideline is 25 words/sentence; warn at 35

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
    type: str           # cut_off | missing_cta | bad_prompt | duplicate_prompt | ...
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

def _structural_checks(
    blocks: list[dict[str, Any]],
    duration_min: int | None = None,
    duration_max: int | None = None,
    language: str | None = None,
) -> list[ScriptIssue]:
    """Fast checks that require no API call.

    Args:
        blocks:       Script blocks from script.json.
        duration_min: Target minimum duration in minutes (used for TOO_SHORT threshold).
        duration_max: Target maximum duration in minutes (used for TOO_LONG threshold).
        language:     Declared script language ISO 639-1 (e.g. 'uk', 'en').
                      Used for language consistency heuristic.
    """
    issues: list[ScriptIssue] = []

    # Dynamic word count thresholds based on target duration
    # too_long:  25% over max target (warns about duplicate/excessive content)
    # too_short: 50% under min target (critical — generation likely failed)
    eff_too_long  = int(duration_max * 150 * 1.25) if duration_max else TOO_LONG_WORDS
    eff_too_short = int(duration_min * 140 * 0.50) if duration_min else TOO_SHORT_WORDS
    # Dynamic per-block minimum: scales with video length to avoid false positives on long-form.
    # Formula: 2 words per minute of target (8 min → 16, 25 min → 50, 35 min → 70).
    eff_block_short = max(BLOCK_SHORT_WORDS, int(duration_min * 2)) if duration_min else BLOCK_SHORT_WORDS

    if not blocks:
        return [ScriptIssue(type="no_blocks", severity="critical", reason="Script has no blocks")]

    # ── [NEW] Required fields check ──────────────────────────────────────────
    for idx, block in enumerate(blocks):
        if not block.get("id"):
            issues.append(ScriptIssue(
                type="missing_field",
                block_id=f"block[{idx}]",
                severity="critical",
                reason=f"Block at index {idx} is missing required field 'id'",
            ))
        if not block.get("type"):
            issues.append(ScriptIssue(
                type="missing_field",
                block_id=block.get("id", f"block[{idx}]"),
                severity="critical",
                reason=f"Block at index {idx} is missing required field 'type'",
            ))

    # ── [NEW] Block ordering check ────────────────────────────────────────────
    first_type = blocks[0].get("type", "") if blocks else ""
    if first_type in ("cta", "outro"):
        issues.append(ScriptIssue(
            type="bad_block_order",
            block_id=blocks[0].get("id", ""),
            severity="warning",
            reason=f"First block is type '{first_type}' — expected 'intro' or 'section'",
        ))

    # ── CTA / outro check ─────────────────────────────────────────────────────
    has_cta = any(b.get("type") in ("cta", "outro") for b in blocks)
    if not has_cta:
        issues.append(ScriptIssue(
            type="missing_cta",
            block_id=blocks[-1].get("id", ""),
            severity="critical",
            reason="No CTA/outro block found in script",
        ))

    # ── [IMPROVED] Cut-off check: scan ALL blocks ─────────────────────────────
    # Previously only checked last 2 blocks; now flags mid-script abrupt endings
    # as warnings and final blocks as critical.
    for i, blk in enumerate(blocks):
        narration = (blk.get("narration") or "").strip()
        if not narration:
            continue
        ends_with_punct = narration.endswith((".", "!", "?", "…"))
        words    = narration.split()
        last_word = re.sub(r"[^\w]", "", words[-1]).lower() if words else ""
        if not ends_with_punct or last_word in _CONNECTOR_WORDS:
            # Last two positions (penultimate section + CTA/outro) → critical
            is_near_end = (i >= len(blocks) - 2)
            issues.append(ScriptIssue(
                type="cut_off",
                block_id=blk.get("id", ""),
                severity="critical" if is_near_end else "warning",
                reason=f"Block ends abruptly: '…{narration[-80:]}'",
            ))

    # ── Per-block narration quality checks ───────────────────────────────────
    for block in blocks:
        bid  = block.get("id", "")
        narr = (block.get("narration") or "").strip()

        # Empty narration
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
            m   = _OTHER_TAG_IN_NARRATION_RE.search(narr)
            tag = m.group(0) if m else "unknown tag"
            issues.append(ScriptIssue(
                type="bad_narration", block_id=bid, severity="critical",
                reason=f"Narration contains raw parser tag '{tag}' — will be read aloud by TTS",
            ))

        # [IMPROVED] Too-short narration — now uses dynamic threshold
        word_count = len(narr.split())
        if narr and word_count < eff_block_short and block.get("type") not in ("cta",):
            issues.append(ScriptIssue(
                type="short_block", block_id=bid, severity="warning",
                reason=(
                    f"Narration too short ({word_count} words, min={eff_block_short}) "
                    f"— may be truncated or placeholder"
                ),
            ))

        # ── TTS compliance (v3 narration rules) — regex only, zero API cost ──
        # Skip CTA/outro blocks — they have different formatting conventions.
        if narr and block.get("type") not in ("cta", "outro"):

            # Markdown: TTS reads "**word**" as "asterisk asterisk word asterisk asterisk"
            md_m = _TTS_MARKDOWN_RE.search(narr)
            if md_m:
                issues.append(ScriptIssue(
                    type="tts_violation", block_id=bid, severity="warning",
                    reason=f"Markdown in narration (TTS reads literally): '{md_m.group(0)[:40]}'",
                ))

            # Ellipsis: TTS reads "..." as "dot dot dot" or creates dead silence
            el_m = _TTS_ELLIPSIS_RE.search(narr)
            if el_m:
                issues.append(ScriptIssue(
                    type="tts_violation", block_id=bid, severity="warning",
                    reason=f"Ellipsis '{el_m.group(0)}' in narration (forbidden per v3 TTS rules)",
                ))

            # Parenthetical asides: break spoken rhythm, often swallowed by TTS
            par_m = _TTS_PARENS_RE.search(narr)
            if par_m:
                issues.append(ScriptIssue(
                    type="tts_violation", block_id=bid, severity="warning",
                    reason=f"Parenthetical aside (breaks TTS rhythm): '{par_m.group(0)[:50]}'",
                ))

            # Exclamation mark: TTS delivers it flat — sounds forced and unnatural
            exc_m = _TTS_EXCLAIM_RE.search(narr)
            if exc_m:
                ctx = narr[max(0, exc_m.start() - 25): exc_m.start() + 1]
                issues.append(ScriptIssue(
                    type="tts_violation", block_id=bid, severity="warning",
                    reason=f"Exclamation mark (forbidden per v3): '…{ctx}'",
                ))

            # Forbidden symbols: % → "percent", # → "hash", & → "ampersand"
            sym_m = _TTS_SYMBOLS_RE.search(narr)
            if sym_m:
                issues.append(ScriptIssue(
                    type="tts_violation", block_id=bid, severity="warning",
                    reason=f"Symbol '{sym_m.group(0)}' in narration (TTS reads symbol name aloud)",
                ))

            # Long sentences: >30 words causes listener fatigue (v3 guideline: max 25)
            for sent in _SENTENCE_SPLIT_RE.split(narr):
                sent = sent.strip()
                if not sent:
                    continue
                wc = len(sent.split())
                if wc > TTS_MAX_SENTENCE_WORDS:
                    issues.append(ScriptIssue(
                        type="tts_violation", block_id=bid, severity="warning",
                        reason=(
                            f"Sentence too long ({wc} words, max={TTS_MAX_SENTENCE_WORDS}): "
                            f"'{sent[:70]}…'"
                        ),
                    ))
                    break  # one warning per block is enough

            # Filler opener in intro block
            if block.get("type") == "intro" and _TTS_FILLER_RE.match(narr[:80]):
                issues.append(ScriptIssue(
                    type="tts_violation", block_id=bid, severity="warning",
                    reason=f"Filler opener phrase (forbidden per v3): '{narr[:50]}'",
                ))

    # ── Script-level length check ─────────────────────────────────────────────
    total_words = sum(len((b.get("narration") or "").split()) for b in blocks)
    if total_words > eff_too_long:
        est_min     = round(total_words / 140, 0)
        target_desc = f"{duration_min}-{duration_max} min" if duration_min and duration_max else "8-15 min"
        issues.append(ScriptIssue(
            type="too_long", block_id="",
            severity="warning",
            reason=(
                f"Script is {total_words} words (~{int(est_min)} min) — target is {target_desc}. "
                f"Possible duplicate content."
            ),
        ))
    elif total_words < eff_too_short:
        issues.append(ScriptIssue(
            type="too_short", block_id="", severity="critical",
            reason=f"Script has only {total_words} words total — likely generation failure",
        ))

    # ── Duplicate section titles (strong signal of doubled LLM output) ────────
    seen_labels: dict[str, str] = {}  # normalized_label → block_id
    for block in blocks:
        bid   = block.get("id", "")
        label = (block.get("timestamp_label") or "").strip().lower()
        if not label or label in ("hook", "subscribe", "intro", "outro"):
            continue
        norm = re.sub(r"\b(the|a|an)\b", "", label)
        norm = re.sub(r"[^\w\s]", "", norm).strip()
        if norm in seen_labels:
            issues.append(ScriptIssue(
                type="duplicate_section",
                block_id=bid,
                severity="warning",
                reason=(
                    f"Section title '{block.get('timestamp_label')}' duplicates "
                    f"block {seen_labels[norm]} — possible doubled LLM output"
                ),
            ))
        else:
            seen_labels[norm] = bid

    # ── [NEW] Language consistency heuristic (no API) ─────────────────────────
    # For Cyrillic-script languages: if <40% of alphabetic chars are Cyrillic,
    # the LLM likely responded in a different language (e.g. English).
    if language and language in _CYRILLIC_LANGS and blocks:
        all_narration = " ".join((b.get("narration") or "") for b in blocks)
        alpha_chars   = [c for c in all_narration if c.isalpha()]
        if alpha_chars:
            cyrillic_count = sum(1 for c in alpha_chars if _CYRILLIC_RE.match(c))
            ratio = cyrillic_count / len(alpha_chars)
            if ratio < 0.40:
                issues.append(ScriptIssue(
                    type="wrong_language",
                    severity="warning",
                    reason=(
                        f"Declared language '{language}' is Cyrillic-script "
                        f"but only {ratio:.0%} of characters are Cyrillic — "
                        f"LLM may have responded in the wrong language"
                    ),
                ))

    # ── Image prompt checks ───────────────────────────────────────────────────
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

        # [IMPROVED] Expanded generic pattern detection
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

    # ── Sparse image_prompts check ─────────────────────────────────────────────
    # For sizeable section/intro blocks, verify image_prompts list has enough entries.
    # `image_prompt` (single) may be non-empty while `image_prompts` (list) is nearly empty.
    # Threshold: at least 1 image per 150 narration words (lower than actual targets but
    # catches critical shortfalls without false-flagging tier-4 low-density blocks).
    for block in blocks:
        btype = block.get("type", "")
        if btype in ("cta", "outro"):
            continue
        narr = (block.get("narration") or "").strip()
        nw = len(narr.split())
        if nw < 200:
            continue   # short blocks (transition sentences, mid-CTA) don't need many images
        actual_imgs = len(block.get("image_prompts") or [])
        min_expected = max(1, nw // 150)
        if actual_imgs < min_expected:
            issues.append(ScriptIssue(
                type="sparse_images",
                block_id=block.get("id", ""),
                severity="warning",
                reason=(
                    f"Too few image prompts: {actual_imgs} actual, "
                    f"~{min_expected} expected for {nw}-word block "
                    f"(need {min_expected - actual_imgs} more)"
                ),
            ))

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
    blocks   = script.get("blocks", [])
    title    = script.get("title", "Unknown")
    niche    = script.get("niche", "")
    language = script.get("language", "en")

    # Pass last 5 blocks for richer context
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
    raw   = await _llm(FIX_MODEL, [{"role": "user", "content": prompt}], max_tokens=3000)
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


async def _fix_bad_prompts(bad_blocks: list[dict[str, Any]], image_style: str = "") -> dict[str, str]:
    """Rewrite bad image_prompts in a single batch LLM call."""
    if not bad_blocks:
        return {}
    blocks_info = "\n\n".join(
        f"Block {b['id']}:\n  narration: \"{(b.get('narration') or '')[:200]}\"\n  bad_prompt: \"{b.get('image_prompt', '')}\""
        for b in bad_blocks
    )
    style_line = f"\nIMAGE STYLE (apply to every prompt): {image_style}" if image_style else ""
    prompt = f"""Rewrite these image prompts. Each must directly illustrate exactly what the narration says — not the general theme.
{style_line}

{blocks_info}

Rules:
- 15-50 words per prompt
- Must be specific to that block's exact narration content
- Apply the image style to every prompt
- No generic phrases ("abstract concept", "dark mood", "philosophical", "misty light")

Return ONLY valid JSON: {{"block_id": "new_prompt", ...}}"""
    raw = await _llm(DETECT_MODEL, [{"role": "user", "content": prompt}], max_tokens=1500, json_mode=True)
    return json.loads(raw)


async def _fix_sparse_images(
    sparse_blocks: list[dict[str, Any]],
    image_style: str,
) -> dict[str, list[str]]:
    """
    For blocks with too few image prompts, generate additional ones in a batch LLM call.

    Returns dict of {block_id: [new_prompt1, new_prompt2, ...]}.
    The caller adds these to the existing image_prompts list (not replacing them).
    """
    if not sparse_blocks:
        return {}

    blocks_info = "\n\n".join(
        f"Block {b['id']} (needs {b['_need']} more):\n"
        f"  narration: \"{(b.get('narration') or '')[:300]}\"\n"
        f"  existing prompts: {json.dumps(b.get('image_prompts') or [], ensure_ascii=False)}"
        for b in sparse_blocks
    )
    prompt = f"""Generate ADDITIONAL image prompts for these narration blocks. Each block already has some prompts — add NEW ones that cover different moments in the narration.

{blocks_info}

IMAGE STYLE: {image_style}

Rules:
- Each new prompt must directly illustrate a SPECIFIC moment in that block's narration
- 15-50 words per prompt, cinematic and specific
- No generic phrases ("abstract concept", "dark mood", "philosophical idea")
- New prompts must NOT duplicate existing prompts for that block
- Each prompt should cover a different part of the narration text

Return ONLY valid JSON:
{{"block_001": ["new prompt A", "new prompt B"], "block_002": ["new prompt C"]}}"""

    raw = await _llm(
        DETECT_MODEL,
        [{"role": "user", "content": prompt}],
        max_tokens=2000,
        json_mode=True,
    )
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
        script_path:       Path to script.json.
        channel_config:    Channel config dict (used for image_style context).
        auto_fix:          If True, call LLM to fix detected issues.
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
    script      = json.loads(script_path.read_text(encoding="utf-8"))
    blocks      = script.get("blocks", [])
    # image_style: prefer explicit channel_config param, fallback to script.json embedded config
    _cfg_param  = (channel_config or {}).get("image_style", "")
    _cfg_script = script.get("channel_config", {}).get("image_style", "")
    image_style = _cfg_param or _cfg_script or "cinematic, photorealistic, dramatic lighting"

    # Read target duration range from script.json (set by script generator, may be absent in old scripts)
    script_duration_min: int | None = script.get("duration_min")
    script_duration_max: int | None = script.get("duration_max")
    # Language declared in script (for language consistency check)
    script_language: str | None = script.get("language")

    # ── Structural checks (no API) ──
    _emit("Checking structure…", 15.0)
    issues = _structural_checks(
        blocks,
        duration_min=script_duration_min,
        duration_max=script_duration_max,
        language=script_language,
    )
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
    has_cut_off = any(i.type == "cut_off" and i.severity == "critical" for i in issues)
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
                fixed_prompts = await _fix_bad_prompts(bad_blocks, image_style=image_style)
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

    # ── Fix 3: sparse image_prompts (generate additional prompts) ──
    sparse_issues = [i for i in issues if i.type == "sparse_images" and i.block_id]
    if sparse_issues:
        _emit(f"Adding image prompts to {len(sparse_issues)} sparse block(s)…", 75.0)
        sparse_bid_set = {i.block_id for i in sparse_issues}
        sparse_blocks_data = []
        for block in blocks:
            bid = block.get("id", "")
            if bid not in sparse_bid_set:
                continue
            nw = len((block.get("narration") or "").split())
            actual = len(block.get("image_prompts") or [])
            need = max(1, nw // 150) - actual
            if need <= 0:
                continue
            sparse_blocks_data.append({**block, "_need": need})

        if sparse_blocks_data:
            try:
                new_prompts_map = await _fix_sparse_images(sparse_blocks_data, image_style)
                added_total = 0
                for block in blocks:
                    bid = block.get("id", "")
                    new_prompts = new_prompts_map.get(bid, [])
                    if not new_prompts:
                        continue
                    existing_prompts = list(block.get("image_prompts") or [])
                    existing_offsets = list(block.get("image_word_offsets") or [])
                    nw = len((block.get("narration") or "").split())

                    # Assign evenly-distributed word offsets for the new prompts
                    n_existing = len(existing_prompts)
                    n_total = n_existing + len(new_prompts)
                    new_offsets = [
                        int((n_existing + j + 1) * nw / (n_total + 1))
                        for j in range(len(new_prompts))
                    ]

                    block["image_prompts"] = existing_prompts + new_prompts
                    block["image_word_offsets"] = existing_offsets + new_offsets
                    if not block.get("image_prompt"):
                        block["image_prompt"] = new_prompts[0]
                    added_total += len(new_prompts)

                for iss in issues:
                    if iss.type == "sparse_images" and iss.block_id in new_prompts_map:
                        iss.fixed = True
                script["blocks"] = blocks
                result.fixes_applied.append(f"Added {added_total} image prompts to sparse blocks")
                modified = True
                log.info("Fixed sparse_images: added %d prompts across %d blocks", added_total, len(new_prompts_map))
            except Exception as exc:
                log.warning("Failed to fix sparse_images: %s", exc)

    # ── Save fixed script ──
    if modified:
        _emit("Saving fixed script…", 90.0)
        script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Saved fixed script: %s", script_path)

        # ── Post-fix re-check: verify fixes actually resolved critical issues ──
        remaining = _structural_checks(
            script.get("blocks", []),
            duration_min=script_duration_min,
            duration_max=script_duration_max,
            language=script_language,
        )
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
