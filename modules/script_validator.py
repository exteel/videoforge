"""
Script quality validator — post-generation structural checks and auto-fixes.
Also provides ``sanitize_narration_for_tts()`` — a TTS-safe text cleaner.

Applied after LLM output is parsed into a Script object (via model_dump()) and
before saving to disk. Works on raw dicts to avoid circular imports.

5 auto-fixable rules:
  1. META_TEXT            — narration starts with LLM self-referential meta-commentary.
                            Strategy: strip meta prefix first; if ≥30 real words remain
                            the block is KEPT with the cleaned narration.  Only removes
                            the block when there is no real content left.
  2. SECTIONS_AFTER_OUTRO — non-CTA blocks appear after the first outro block
  3. DUPLICATE_CTA        — multiple CTA blocks share the same opening line
  4. DUPLICATE_PRACTICES  — identical named practice lists appear in 2+ blocks
  5. TRUNCATED_NARRATION  — narration ends mid-sentence (LLM hit token limit).
                            Trims to the last complete sentence.  Block is kept.

Public API:
    from modules.script_validator import validate_and_fix, sanitize_narration_for_tts

    # Structural validation (called after generation, fixes script.json)
    # All 5 rules are applied automatically; truncated narrations are trimmed.
    script_dict = script.model_dump()
    script_dict, issues = validate_and_fix(script_dict)

    # TTS safety net (called in 03_voice_generator.py, does NOT touch script.json)
    clean_text = sanitize_narration_for_tts(narration)
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

log = logging.getLogger("script_validator")


# ─── TTS sanitizer patterns ────────────────────────────────────────────────────
#
# Applied in sanitize_narration_for_tts() — strips technical markup so the TTS
# API never receives [IMAGE_PROMPT:...], [SECTION N:...], markdown, etc.

# Bracket-delimited markup tags (IMAGE_PROMPT, SECTION, CTA markers)
_TTS_BRACKET_RE = re.compile(
    r"\[IMAGE_PROMPT:.*?\]"       # [IMAGE_PROMPT: long scene description]
    r"|\[SECTION\s+\d+[^\]]*\]"  # [SECTION 5: Title]
    r"|\[CTA_SUBSCRIBE[^\]]*\]"  # [CTA_SUBSCRIBE_FINAL], [CTA_SUBSCRIBE_MID]
    r"|\[CTA[^\]]*\]",            # any other [CTA_...] marker
    re.IGNORECASE | re.DOTALL,
)

# Markdown emphasis: **bold**, *italic*, __underline__, _underline_
_MD_BOLD_RE    = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE  = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_MD_ULINE_RE   = re.compile(r"__(.+?)__", re.DOTALL)
_MD_ULINE2_RE  = re.compile(r"(?<!_)_([^_\n]+?)_(?!_)")

# Markdown ATX headers: # Title, ## Title, etc. (only at line start)
_MD_HEADER_RE  = re.compile(r"^#{1,6}\s+", re.MULTILINE)


# ─── Rule 1: Meta-text detection patterns ─────────────────────────────────────
#
# Matches phrases that indicate the LLM was writing about itself, not the video.
# These appear when a continuation chunk "explains" what it is doing instead of
# immediately writing [SECTION N: ...].

_META_PHRASES = [
    r"I need to reassess",
    r"you['']ve asked me to continue",
    r"you['']ve asked me",
    r"looking at what (?:I|you)['']ve written",
    r"proper continuation",
    r"I was instructed",
    r"as per (?:your |the )?instructions?",
    r"let me continue",
    r"I['']ll continue(?: the script)?",
    r"continuing from(?: where| section)?",
    r"I (?:should|need to|must) (?:continue|start|begin|write)",
    r"I['']ve (?:already |)written(?: sections?)?",
    r"based on the (?:previous|prior|earlier) (?:section|content|output|response)",
    r"as (?:I|we) (?:discussed|noted|mentioned) (?:earlier|above|before)",
    r"this is a continuation",
    r"CONTINUATION\s*[-—]",
    r"you asked me",
    r"my previous response",
    r"the previous (?:section|block|content|output|response)",
    r"where I left off",
    r"starting with \[SECTION",
    r"looking at (?:the )?(?:outline|structure|plan)",
    r"I (?:see|notice) (?:that )?(?:you|the)",
]

_META_RE = re.compile("|".join(_META_PHRASES), re.IGNORECASE)

# Minimum real words that must remain after stripping the meta prefix for the
# block to be KEPT (with cleaned narration).  Below this the block is removed.
_META_MIN_REAL_WORDS = 30


# ─── Rule 4: Duplicate practice heading patterns ───────────────────────────────

_PRACTICE_HEADING_RE = re.compile(
    r"(?:"
    r"Practice\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|\d+)"
    r"|Shadow\s+Dialogue"
    r"|Solitude\s+Practice"
    r"|Integration\s+Work"
    r"|The\s+(.{3,40}?)\s+Practice"
    r")",
    re.IGNORECASE,
)


# ─── Validation result ─────────────────────────────────────────────────────────

class ValidationIssue:
    """A single issue detected and auto-fixed by the validator."""

    def __init__(self, rule: str, block_id: str, description: str) -> None:
        self.rule = rule
        self.block_id = block_id
        self.description = description

    def __repr__(self) -> str:
        return f"[{self.rule}] {self.block_id}: {self.description}"

    def __str__(self) -> str:
        return repr(self)


# ─── Public API ────────────────────────────────────────────────────────────────

def sanitize_narration_for_tts(text: str) -> str:
    """
    Strip technical markup from narration text before sending to a TTS API.

    Removes:
    - ``[IMAGE_PROMPT: ...]`` tags (single- or multi-line)
    - ``[SECTION N: Title]`` headers
    - ``[CTA_SUBSCRIBE_FINAL]`` / ``[CTA_SUBSCRIBE_MID]`` markers
    - Markdown bold (``**text**``), italic (``*text*``), underline (``__text__``)
    - Markdown ATX headers (``# Title``)
    - Excess blank lines and leading/trailing whitespace

    Does NOT modify ``script.json`` — call this function only at TTS generation
    time (in ``03_voice_generator.py``) as a final safety net.
    """
    # 1. Strip bracket-delimited technical tags
    text = _TTS_BRACKET_RE.sub("", text)

    # 2. Strip markdown formatting (order matters: bold before italic)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_ULINE_RE.sub(r"\1", text)
    text = _MD_ULINE2_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)

    # 3. Normalise whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)   # max 2 consecutive newlines
    text = re.sub(r" {2,}", " ", text)        # collapse multiple spaces
    return text.strip()


def validate_and_fix(
    script_dict: dict[str, Any],
) -> tuple[dict[str, Any], list[ValidationIssue]]:
    """
    Validate *script_dict* (from ``Script.model_dump()``) and apply auto-fixes.

    Rules applied in order:
      1. META_TEXT            — strip meta prefix from narration; keep block if
                                ≥30 real words remain, otherwise remove block.
      2. SECTIONS_AFTER_OUTRO — remove non-CTA blocks after the first outro
      3. DUPLICATE_CTA        — remove earlier duplicate CTAs (same opening line)
      4. DUPLICATE_PRACTICES  — remove earlier block with identical practice list
      5. TRUNCATED_NARRATION  — trim narration that ends mid-sentence to the last
                                complete sentence (block is KEPT, never removed)

    After any block removals, ``id`` and ``order`` are re-indexed sequentially
    (block_001, block_002, …) so downstream consumers stay correct.

    Returns:
        (fixed_dict, issues)  — *issues* is empty if the script was already clean.
    """
    blocks: list[dict[str, Any]] = list(script_dict.get("blocks", []))
    issues: list[ValidationIssue] = []

    blocks, iss = _fix_meta_text(blocks)
    issues.extend(iss)

    blocks, iss = _fix_sections_after_outro(blocks)
    issues.extend(iss)

    blocks, iss = _fix_duplicate_ctas(blocks)
    issues.extend(iss)

    blocks, iss = _fix_duplicate_practices(blocks)
    issues.extend(iss)

    blocks, iss = _fix_truncated_narration(blocks)
    issues.extend(iss)

    if issues:
        blocks = _reindex(blocks)
        log.info(
            "Script validator: %d issue(s) fixed, %d blocks remain",
            len(issues), len(blocks),
        )
    else:
        log.info("Script validator: clean — no issues found (%d blocks)", len(blocks))

    return {**script_dict, "blocks": blocks}, issues


# ─── Rule implementations ─────────────────────────────────────────────────────

def _fix_meta_text(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """
    Rule 1 — Handle blocks whose narration STARTS with LLM meta-commentary.

    Strategy (strip-first, remove-as-last-resort):
      a) Detect meta-text in the first 300 chars of narration.
      b) Try to extract the real content that follows the meta prefix.
      c) If real content has ≥ _META_MIN_REAL_WORDS words:
            → update block's narration to the real content (block is KEPT).
            → issue type = META_TEXT_CLEANED
      d) If real content is empty or too short:
            → remove the entire block.
            → issue type = META_TEXT_REMOVED

    Checking only the first 300 chars avoids false positives on blocks that
    legitimately discuss "looking at" something as actual content.
    """
    issues: list[ValidationIssue] = []
    clean: list[dict[str, Any]] = []

    for block in blocks:
        narration: str = block.get("narration", "")
        prefix = narration[:300]

        if not _META_RE.search(prefix):
            clean.append(block)
            continue

        block_id = block.get("id", "?")

        # Try to salvage real content after the meta prefix
        real_content = _extract_real_content_after_meta(narration)
        real_word_count = len(real_content.split())

        if real_word_count >= _META_MIN_REAL_WORDS:
            # Real content exists — keep block with cleaned narration
            updated = {**block, "narration": real_content}
            clean.append(updated)
            snippet = narration[:80].replace("\n", " ")
            issues.append(ValidationIssue(
                "META_TEXT_CLEANED", block_id,
                f"stripped meta prefix, kept {real_word_count} real words — was: {snippet!r}",
            ))
            log.warning(
                "[validator] META_TEXT_CLEANED block %s — stripped prefix, "
                "kept %d real words",
                block_id, real_word_count,
            )
        else:
            # No real content — remove entire block
            snippet = narration[:80].replace("\n", " ")
            issues.append(ValidationIssue(
                "META_TEXT_REMOVED", block_id,
                f"removed — no real content after meta prefix ({len(narration.split())} words total) — {snippet!r}",
            ))
            log.warning(
                "[validator] META_TEXT_REMOVED block %s — pure meta-commentary, "
                "nothing to salvage (%d words, real=%d)",
                block_id, len(narration.split()), real_word_count,
            )

    return clean, issues


def _extract_real_content_after_meta(narration: str) -> str:
    """
    Find the real narration content that comes AFTER a meta-text prefix.

    Tries (in order):
      1. A ``[SECTION N: ...]`` marker in the narration — real content follows it.
      2. A double newline after the first META_RE match — paragraph break signals
         the end of the meta section.
      3. The end of the first sentence (period/!?  + whitespace) after the match.

    Returns the extracted content stripped of leading/trailing whitespace,
    or an empty string if nothing substantial is found.
    """
    # Strategy 1: [SECTION N: ...] marker present → everything after it is real
    section_match = re.search(r"\[SECTION\s+\d+[^\]]*\]", narration, re.IGNORECASE)
    if section_match:
        return narration[section_match.end():].strip()

    # Strategy 2: paragraph break after the first meta phrase
    meta_match = _META_RE.search(narration[:300])
    if meta_match:
        rest = narration[meta_match.end():]
        double_nl = rest.find("\n\n")
        if double_nl != -1:
            return rest[double_nl:].strip()
        # Strategy 3: end of first sentence in the rest
        sent_end = re.search(r"[.!?]\s", rest)
        if sent_end:
            return rest[sent_end.end():].strip()

    return ""


def _fix_sections_after_outro(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """
    Rule 2 — Remove section/intro blocks that appear after the first outro block.

    Keeps:   the first outro block + any subsequent ``cta`` block.
    Removes: any other block type after the first outro (double-ending artefact).
    """
    issues: list[ValidationIssue] = []

    first_outro_idx: int | None = None
    for i, block in enumerate(blocks):
        if block.get("type") == "outro":
            first_outro_idx = i
            break

    if first_outro_idx is None:
        return blocks, issues

    # Count words before and after the first outro — don't strip content
    # if it would make the script critically short (expansion continuation blocks).
    words_before = sum(len((b.get("narration") or "").split()) for b in blocks[:first_outro_idx + 1])
    words_after = sum(len((b.get("narration") or "").split()) for b in blocks[first_outro_idx + 1:] if b.get("type") not in ("cta", "outro"))
    total_words = words_before + words_after

    # If post-outro sections contain significant content (>500 words),
    # they're likely continuation chunks, not double-ending artefacts.
    # Retype the premature outro as a section instead of removing valid content.
    if words_after > 500:
        log.warning(
            "[validator] SECTIONS_AFTER_OUTRO: %d words after first outro — "
            "keeping as continuation content (retyping outro → section)",
            words_after,
        )
        blocks[first_outro_idx] = {**blocks[first_outro_idx], "type": "section"}
        issues.append(ValidationIssue(
            "SECTIONS_AFTER_OUTRO", blocks[first_outro_idx].get("id", "?"),
            f"retyped premature outro → section (continuation has {words_after} words)",
        ))
        return blocks, issues

    clean = list(blocks[: first_outro_idx + 1])
    for block in blocks[first_outro_idx + 1:]:
        btype = block.get("type", "section")
        block_id = block.get("id", "?")
        if btype == "cta":
            clean.append(block)
        else:
            issues.append(ValidationIssue(
                "SECTIONS_AFTER_OUTRO", block_id,
                f"removed {btype!r} block after first outro (double-ending artefact)",
            ))
            log.warning(
                "[validator] SECTIONS_AFTER_OUTRO removed block %s (type=%s)",
                block_id, btype,
            )

    return clean, issues


def _fix_duplicate_ctas(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """
    Rule 3 — Keep only the LAST CTA block when multiple CTAs share the same
    opening line (first 50 chars of narration, case-insensitive).

    Earlier duplicates are removed; the last occurrence is kept because it is
    more likely to be the fully-rendered version.
    """
    issues: list[ValidationIssue] = []

    cta_indices = [i for i, b in enumerate(blocks) if b.get("type") == "cta"]
    if len(cta_indices) <= 1:
        return blocks, issues

    def _opening(block: dict[str, Any]) -> str:
        for line in block.get("narration", "").splitlines():
            line = line.strip()
            if line:
                return line[:50].lower()
        return ""

    to_remove: set[int] = set()
    for a in range(len(cta_indices)):
        if cta_indices[a] in to_remove:
            continue
        op_a = _opening(blocks[cta_indices[a]])
        for b in range(a + 1, len(cta_indices)):
            op_b = _opening(blocks[cta_indices[b]])
            if op_a == op_b:
                to_remove.add(cta_indices[a])

    if not to_remove:
        return blocks, issues

    clean: list[dict[str, Any]] = []
    for i, block in enumerate(blocks):
        if i in to_remove:
            block_id = block.get("id", "?")
            issues.append(ValidationIssue(
                "DUPLICATE_CTA", block_id,
                "removed earlier duplicate CTA (same opening line as a later CTA)",
            ))
            log.warning("[validator] DUPLICATE_CTA removed block %s", block_id)
        else:
            clean.append(block)

    return clean, issues


def _fix_duplicate_practices(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """
    Rule 4 — Remove the EARLIER of two blocks that contain the same set of
    named practice headings (≥ 2 practices in common).

    This catches the pattern where the LLM regenerates a completed section
    verbatim after a double-ending.
    """
    issues: list[ValidationIssue] = []

    def _practice_set(block: dict[str, Any]) -> frozenset[str]:
        narration: str = block.get("narration", "")
        matches = _PRACTICE_HEADING_RE.findall(narration)
        result: set[str] = set()
        for m in matches:
            if isinstance(m, tuple):
                result.update(s.lower().strip() for s in m if s)
            elif isinstance(m, str) and m:
                result.add(m.lower().strip())
        return frozenset(result)

    practice_map: dict[frozenset[str], list[int]] = defaultdict(list)
    for i, block in enumerate(blocks):
        ps = _practice_set(block)
        if len(ps) >= 2:
            practice_map[ps].append(i)

    to_remove: set[int] = set()
    for ps, indices in practice_map.items():
        if len(indices) >= 2:
            for idx in indices[:-1]:
                to_remove.add(idx)

    if not to_remove:
        return blocks, issues

    clean: list[dict[str, Any]] = []
    for i, block in enumerate(blocks):
        if i in to_remove:
            block_id = block.get("id", "?")
            ps = _practice_set(block)
            issues.append(ValidationIssue(
                "DUPLICATE_PRACTICES", block_id,
                f"removed duplicate practice block — practices: {sorted(ps)!r}",
            ))
            log.warning("[validator] DUPLICATE_PRACTICES removed block %s", block_id)
        else:
            clean.append(block)

    return clean, issues


def _fix_truncated_narration(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """
    Rule 5 — Trim narration that ends mid-sentence (LLM hit token limit).

    A narration is considered truncated when it does NOT end with one of:
        . ! ? " ' … ) (optionally preceded by a closing quote/bracket)

    Strategy:
      - Find the last complete sentence boundary (period/!/? + optional quotes/parens).
      - If ≥ _TRUNC_MIN_REMAINING_WORDS words remain after trimming, update narration.
      - Otherwise keep the block untouched (partial sentence > near-empty block).
      - Block is NEVER removed — only the incomplete trailing fragment is cut.

    Applies to ALL block types (section, intro, outro, cta).
    """
    issues: list[ValidationIssue] = []
    fixed: list[dict[str, Any]] = []

    for block in blocks:
        narration: str = block.get("narration", "")
        stripped = narration.rstrip()

        if not stripped:
            fixed.append(block)
            continue

        # Already ends with sentence-closing punctuation → nothing to do
        if stripped[-1] in '.!?"\'…)':
            fixed.append(block)
            continue

        # Find the LAST sentence-end boundary: punctuation + optional close-chars + whitespace/end
        last_end = _last_sentence_end(stripped)

        if last_end is None:
            # No sentence boundary found at all — keep block as-is
            fixed.append(block)
            continue

        remaining_words = len(stripped[last_end:].split())
        kept_words = len(stripped[:last_end].split())

        if kept_words < _TRUNC_MIN_REMAINING_WORDS:
            # After trimming, almost nothing would remain — keep untouched
            fixed.append(block)
            continue

        block_id = block.get("id", "?")
        trimmed_narration = stripped[:last_end].rstrip()
        issues.append(ValidationIssue(
            "TRUNCATED_NARRATION", block_id,
            f"trimmed {remaining_words} incomplete-sentence word(s) from end "
            f"(kept {kept_words} words)",
        ))
        log.warning(
            "[validator] TRUNCATED_NARRATION block %s — trimmed %d word(s), kept %d",
            block_id, remaining_words, kept_words,
        )
        fixed.append({**block, "narration": trimmed_narration})

    return fixed, issues


# Minimum words that must remain after trimming for the fix to be applied.
# Below this we'd make the block nearly empty, which is worse than a dangling phrase.
_TRUNC_MIN_REMAINING_WORDS = 15


def _last_sentence_end(text: str) -> int | None:
    """
    Return the character position immediately after the last complete sentence
    boundary in *text*, or ``None`` if no boundary is found.

    A boundary is: [.!?] followed by optional close-chars ( " ' ) ) then either
    whitespace, end-of-string, or a newline.
    """
    last_pos: int | None = None
    for m in re.finditer(r'[.!?]["\'\)]*(?=\s|\Z)', text):
        last_pos = m.end()
    return last_pos


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _reindex(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-assign ``id`` (block_NNN) and ``order`` sequentially after removals."""
    reindexed: list[dict[str, Any]] = []
    for new_order, block in enumerate(blocks, start=1):
        reindexed.append({
            **block,
            "id": f"block_{new_order:03d}",
            "order": new_order,
        })
    return reindexed


# ─── CLI self-test ────────────────────────────────────────────────────────────

def _self_test() -> None:
    """Quick smoke-test covering all 4 rules + sanitizer."""
    import sys
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

    # ── Test A: validate_and_fix ──────────────────────────────────────────────
    bad_script: dict[str, Any] = {
        "title": "Test Script",
        "blocks": [
            {
                "id": "block_001", "order": 1, "type": "intro",
                "narration": "You are not alone in this.",
                "image_prompts": [],
            },
            {
                # Meta prefix + real content BEFORE outro → should be CLEANED (block kept)
                "id": "block_002", "order": 2, "type": "section",
                "narration": (
                    "I'll continue the script from Section 2.\n\n"
                    "The truth about dark feminine energy is that it does not seek approval. "
                    "It operates from a place of deep knowing — a knowing that does not need "
                    "to be validated by the outside world. This is the essence of what Carl "
                    "Jung called the anima — the inner feminine force that drives the most "
                    "powerful women in history."
                ),
                "image_prompts": [],
            },
            {
                # Pure meta-text, no real content → should be REMOVED
                "id": "block_003", "order": 3, "type": "section",
                "narration": "I need to reassess — you've asked me to continue from Section 3.",
                "image_prompts": [],
            },
            {
                "id": "block_004", "order": 4, "type": "outro",
                "narration": "This is where the journey ends.",
                "image_prompts": [],
            },
            {
                # Normal section AFTER outro → should be REMOVED by Rule 2
                "id": "block_005", "order": 5, "type": "section",
                "narration": (
                    "Practice One: Shadow Dialogue. "
                    "Practice Two: Solitude Practice. "
                    "Practice Three: Integration Work."
                ),
                "image_prompts": [],
            },
            {
                # Duplicate CTA (earlier) → REMOVED
                "id": "block_006", "order": 6, "type": "cta",
                "narration": "If this resonated, subscribe and share.",
                "image_prompts": [],
            },
            {
                # Duplicate CTA (later, kept)
                "id": "block_007", "order": 7, "type": "cta",
                "narration": "If this resonated, subscribe and share.",
                "image_prompts": [],
            },
        ],
    }

    print("=== Input blocks ===")
    for b in bad_script["blocks"]:
        print(f"  {b['id']} [{b['type']}]: {b['narration'][:70]!r}")

    fixed, issues = validate_and_fix(bad_script)

    print(f"\n=== Issues ({len(issues)}) ===")
    for iss in issues:
        print(f"  {iss}")

    print(f"\n=== Fixed blocks ({len(fixed['blocks'])}) ===")
    for b in fixed["blocks"]:
        print(f"  {b['id']} [{b['type']}]: {b['narration'][:70]!r}")

    # Expected (4 blocks):
    #   block_001 intro   — unchanged
    #   block_002 section — META_TEXT_CLEANED: meta prefix stripped, real content kept
    #   block_003 outro   — was block_004, re-indexed after block_003 removed
    #   block_004 cta     — was block_007 (last CTA, earlier duplicate removed)
    # Removed: block_003 (pure meta), block_005 (section after outro), block_006 (dup CTA)
    assert len(fixed["blocks"]) == 4, f"Expected 4 blocks, got {len(fixed['blocks'])}"
    assert fixed["blocks"][0]["id"] == "block_001"
    assert "feminine energy" in fixed["blocks"][1]["narration"], \
        "Expected real content in cleaned block_002 after meta prefix strip"
    assert fixed["blocks"][2]["type"] == "outro"
    assert fixed["blocks"][3]["type"] == "cta"

    # ── Test B: _fix_truncated_narration (Rule 5) ────────────────────────────
    trunc_script: dict[str, Any] = {
        "title": "Truncation Test",
        "blocks": [
            {
                "id": "block_001", "order": 1, "type": "intro",
                "narration": "You are not alone in this journey through life.",
                "image_prompts": [],
            },
            {
                # Truncated mid-word — should be trimmed to last complete sentence
                "id": "block_002", "order": 2, "type": "section",
                "narration": (
                    "The shadow is the hidden part of the psyche that Jung described as the sum "
                    "of all the qualities we deny in ourselves. It accumulates over decades of "
                    "suppression. When we finally meet it, we discover something extraordinary. "
                    "The shadow contains not only darkness but also tremendous"
                ),
                "image_prompts": [],
            },
            {
                "id": "block_003", "order": 3, "type": "outro",
                "narration": "Thank you for being here.",
                "image_prompts": [],
            },
        ],
    }
    trunc_fixed, trunc_issues = validate_and_fix(trunc_script)
    assert len(trunc_issues) == 1, f"Expected 1 truncation issue, got {len(trunc_issues)}"
    assert trunc_issues[0].rule == "TRUNCATED_NARRATION"
    fixed_narration = trunc_fixed["blocks"][1]["narration"]
    assert fixed_narration.endswith("."), f"Should end with '.', got: {fixed_narration[-20:]!r}"
    assert "extraordinary" in fixed_narration, "Last complete sentence should be kept"
    assert "tremendous" not in fixed_narration, "Incomplete trailing fragment should be trimmed"
    print(f"\n=== Truncation test ===\nFixed narration: {fixed_narration!r}")

    # ── Test C: sanitize_narration_for_tts ───────────────────────────────────
    dirty = (
        "[SECTION 3: The Hidden Truth]\n"
        "[IMAGE_PROMPT: A lone woman stands at the edge of a cliff at sunset.]\n"
        "**The truth** is that *nothing* you do will ever be __enough__ for those\n"
        "who do not want to see you succeed.\n"
        "## A hard truth\n"
        "[CTA_SUBSCRIBE_FINAL]\n"
        "Thank you for watching."
    )
    clean = sanitize_narration_for_tts(dirty)
    assert "[SECTION" not in clean,       "SECTION tag not stripped"
    assert "[IMAGE_PROMPT" not in clean,  "IMAGE_PROMPT not stripped"
    assert "[CTA_SUBSCRIBE" not in clean, "CTA marker not stripped"
    assert "**" not in clean,             "bold markers not stripped"
    assert "__" not in clean,             "underline markers not stripped"
    assert "## " not in clean,            "header not stripped"
    assert "The truth" in clean,          "real text should remain"
    assert "Thank you for watching" in clean
    print(f"\n=== Sanitized narration ===\n{clean}")

    print("\nSelf-test PASSED")


if __name__ == "__main__":
    _self_test()
