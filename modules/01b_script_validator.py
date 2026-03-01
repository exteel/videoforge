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

    # ── Cut-off check: last narration ends abruptly ──
    last = blocks[-1]
    narration = (last.get("narration") or "").strip()
    if narration:
        ends_with_punct = narration.endswith((".", "!", "?", "…"))
        words = narration.split()
        last_word = re.sub(r"[^\w]", "", words[-1]).lower() if words else ""
        if not ends_with_punct or last_word in _CONNECTOR_WORDS:
            issues.append(ScriptIssue(
                type="cut_off",
                block_id=last.get("id", ""),
                severity="critical",
                reason=f"Script ends abruptly: '…{narration[-80:]}'",
            ))

    # ── Image prompt checks ──
    seen: dict[str, str] = {}  # prompt_key → block_id
    for block in blocks:
        bid = block.get("id", "")
        prompt = (block.get("image_prompt") or "").strip()
        narr = (block.get("narration") or "").strip()

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
            if key in seen:
                issues.append(ScriptIssue(
                    type="duplicate_prompt", block_id=bid, severity="warning",
                    reason=f"Duplicate of block {seen[key]}: '{prompt[:60]}'",
                ))
            else:
                seen[key] = bid

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
    blocks = script.get("blocks", [])
    tail = "\n\n".join(
        f"[{b.get('type', 'section')} — {b.get('id', '')}]\n{(b.get('narration') or '')[:300]}"
        for b in blocks[-3:]
    )
    prompt = f"""A video script was cut off before completion. Generate 3-6 continuation blocks.

SCRIPT ENDS WITH:
{tail}

IMAGE STYLE: {image_style}

Requirements:
- Continue naturally from where the script cut off
- Include a synthesis/climax section that ties everything together
- The LAST block must be type "outro" with a subscribe CTA in the narration
- Match the tone, depth, and style of the original exactly

Return ONLY a JSON array of new blocks:
[
  {{
    "id": "block_cont_001",
    "type": "section",
    "narration": "continuation text — complete sentences only",
    "image_prompt": "specific cinematic visual that matches exactly what is being said",
    "animation": "zoom_in"
  }},
  {{
    "id": "block_cont_NNN",
    "type": "outro",
    "narration": "If this video resonated with you... [subscribe CTA]",
    "image_prompt": "warm, hopeful closing visual",
    "animation": "zoom_in"
  }}
]"""
    raw = await _llm(FIX_MODEL, [{"role": "user", "content": prompt}], max_tokens=2500)
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
