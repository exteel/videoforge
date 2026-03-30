"""
VideoForge — Pipeline Runner (Task #16).

Chains all modules in one command:
  source → script → images+audio (parallel) → subs → video → thumbnail → metadata

Steps:
  1  Script    — LLM generates script.json from Transcriber output
  2  Media     — Images (WaveSpeed) + Voice (VoiceAPI) in parallel
  3  Subtitles — SRT + ASS from script.json timing
  4  Video     — FFmpeg assembles final.mp4 (1080p or 480p draft)
  5  Thumbnail — WaveSpeed 1280×720 with VoidAI validation
  6  Metadata  — LLM generates SEO title, description, tags

Usage:
    python pipeline.py \\
        --source "D:/transscript batch/output/output/Video Title" \\
        --channel config/channels/history.json

    # Review mode: pause after script for approval
    python pipeline.py --source ... --channel ... --review

    # Dry run (cost estimate, no API calls)
    python pipeline.py --source ... --channel ... --dry-run

    # Resume from step 3 (uses cached images/audio)
    python pipeline.py --source ... --channel ... --from-step 3

    # Multi-language voices + subs
    python pipeline.py --source ... --channel ... --lang en,de,es

    # Budget limit + draft mode
    python pipeline.py --source ... --channel ... --budget 3.00 --draft
"""

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 stdout so argparse --help and print() work on Windows (cp1252 default)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from modules.common import get_llm_preset, load_channel_config, load_env, setup_logging
from modules.constants import TTS_WPM
from utils.cost_tracker import estimate_cost

log = setup_logging("pipeline")

# ─── Step constants ────────────────────────────────────────────────────────────

STEP_SCRIPT    = 1
STEP_MEDIA     = 2
STEP_SUBTITLES = 3
STEP_VIDEO     = 4
STEP_THUMBNAIL = 5
STEP_METADATA  = 6
TOTAL_STEPS    = 6

STEP_NAMES = {
    STEP_SCRIPT:    "Script",
    STEP_MEDIA:     "Images + Voices",
    STEP_SUBTITLES: "Subtitles",
    STEP_VIDEO:     "Video",
    STEP_THUMBNAIL: "Thumbnail",
    STEP_METADATA:  "Metadata",
}

# Global progress % range (start, end) for each step.
# Calibrated to reflect typical step duration ratios.
STEP_WEIGHTS: dict[int, tuple[float, float]] = {
    STEP_SCRIPT:    (0.0,  15.0),
    STEP_MEDIA:     (15.0, 55.0),
    STEP_SUBTITLES: (55.0, 60.0),
    STEP_VIDEO:     (60.0, 80.0),
    STEP_THUMBNAIL: (80.0, 93.0),
    STEP_METADATA:  (93.0, 100.0),
}

# ─── Module loading ────────────────────────────────────────────────────────────

_module_cache: dict[str, Any] = {}


def _load_module(rel_path: str) -> Any:
    """Import a module by relative path (handles numeric-prefix filenames).

    Registers the module in sys.modules BEFORE exec_module so that Python 3.13+
    dataclasses._is_type can resolve cls.__module__ lookups without getting None.
    (Python 3.13 changed _is_type to call sys.modules.get(cls.__module__).__dict__
    without a None guard — omitting sys.modules registration causes AttributeError.)
    """
    if rel_path in _module_cache:
        return _module_cache[rel_path]
    full = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(full.stem, str(full))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module: {full}")
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so @dataclass and other class decorators can find the module
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(spec.name, None)  # clean up on failure
        raise
    _module_cache[rel_path] = mod
    return mod


def _fn(rel_path: str, fn_name: str) -> Any:
    """Get a function from a module by relative path."""
    return getattr(_load_module(rel_path), fn_name)


# ─── VoidAI subscription cost rate ───────────────────────────────────────────
# Plan: $47/month → 8,500,000 tokens/day × 30 days = 255,000,000 tokens/month
# Rate: $47 / 255,000,000 = $0.000000184/token ≈ $0.184/1M tokens
# All LLM calls (claude-opus, claude-sonnet, gpt-5.2) share this flat rate.
VOIDAI_PER_TOKEN = 47 / (8_500_000 * 30)   # ~$0.000000184

# ─── Cost budget tracker ───────────────────────────────────────────────────────

@dataclass
class CostBudget:
    limit: float | None = None
    spent: float = 0.0
    breakdown: list[tuple[str, float]] = field(default_factory=list)
    _warned: bool = False

    def add(self, label: str, amount: float) -> None:
        """Record a cost entry."""
        if amount <= 0:
            return
        self.spent += amount
        self.breakdown.append((label, amount))
        log.info("[COST] %s: $%.4f  (running total: $%.4f)", label, amount, self.spent)

    def over_budget(self) -> bool:
        """Return True if the budget limit has been exceeded."""
        if self.limit is None:
            return False
        return self.spent > self.limit

    def check(self, progress_callback: Any = None) -> None:
        """Raise RuntimeError if over budget; emit a warning event at 80% threshold.

        Replaces the old pattern of `if cost.over_budget(): sys.exit(1)` so that
        budget violations raise an exception (caught by the API layer) instead of
        killing the entire uvicorn server process.
        """
        if self.limit is None:
            return
        if self.spent >= self.limit:
            raise RuntimeError(
                f"Budget exceeded: ${self.spent:.2f} / ${self.limit:.2f}"
            )
        if self.spent >= self.limit * 0.8 and not self._warned:
            _pct = (self.spent / self.limit) * 100
            log.warning(
                "[COST] Budget warning: $%.4f of $%.2f spent (%.0f%%).",
                self.spent, self.limit, _pct,
            )
            _emit(progress_callback, type="cost_warning", spent=self.spent, limit=self.limit, pct=round(_pct, 1))
            self._warned = True

    def summary(self) -> str:
        if not self.breakdown:
            return "  No costs recorded."
        lines = ["  Cost breakdown:"]
        for label, amount in self.breakdown:
            lines.append(f"    {label:<28} ${amount:.4f}")
        lines.append(f"    {'Total':<28} ${self.spent:.4f}")
        if self.limit is not None:
            remaining = self.limit - self.spent
            lines.append(
                f"    Budget: ${self.limit:.2f}  "
                f"({'EXCEEDED' if remaining < 0 else f'${remaining:.4f} remaining'})"
            )
        return "\n".join(lines)


# ─── Progress callback helper ─────────────────────────────────────────────────

def _emit(callback: Any, **event: Any) -> None:
    """Safely invoke a progress callback without disrupting the pipeline."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass


# ─── Balance snapshot helpers ─────────────────────────────────────────────────

async def _fetch_balances() -> dict[str, Any]:
    """
    Fetch current balances from VoiceAPI and VoidAI (best-effort, never raises).

    Returns a dict with keys:
        voiceapi_chars  – remaining characters on VoiceAPI (int or None)
        voidai_credits  – remaining daily credits on VoidAI (int or None)
        timestamp       – unix time of snapshot (float)
    """
    import os as _os
    result: dict[str, Any] = {"voiceapi_chars": None, "voidai_credits": None, "timestamp": time.monotonic()}

    # ── VoiceAPI balance ──────────────────────────────────────────────────────
    try:
        import httpx as _httpx
        _key = _os.getenv("VOICEAPI_KEY", "")
        if _key:
            async with _httpx.AsyncClient(base_url="https://voiceapi.csv666.ru", timeout=8) as _c:
                _r = await _c.get("/balance", headers={"X-API-Key": _key})
                if _r.status_code == 200:
                    _d = _r.json()
                    result["voiceapi_chars"] = int(_d.get("balance", _d.get("characters", 0)) or 0)
    except Exception as _e:
        log.debug("VoiceAPI balance fetch failed: %s", _e)

    # ── VoidAI balance (best-effort — endpoint may not be available) ──────────
    try:
        import httpx as _httpx
        _key = _os.getenv("VOIDAI_API_KEY", "")
        _base = _os.getenv("VOIDAI_BASE_URL", "https://api.voidai.app/v1").rstrip("/")
        if _key:
            for _path in ("/dashboard/billing/credit_grants", "/usage", "/credits"):
                try:
                    async with _httpx.AsyncClient(timeout=8) as _c:
                        _r = await _c.get(
                            f"{_base}{_path}",
                            headers={"Authorization": f"Bearer {_key}"},
                        )
                        if _r.status_code == 200:
                            _d = _r.json()
                            _rem = (_d.get("remaining") or _d.get("credits_remaining")
                                    or _d.get("balance") or _d.get("daily_remaining"))
                            if _rem is not None:
                                result["voidai_credits"] = int(_rem)
                                break
                except Exception:
                    continue
    except Exception as _e:
        log.debug("VoidAI balance fetch failed: %s", _e)

    return result


def _write_cost_report(
    proj: Path,
    start: dict[str, Any],
    end: dict[str, Any],
    elapsed_s: float,
    *,
    voiceapi_rate_per_char: float = 0.0000038,
) -> None:
    """
    Calculate per-video costs from balance snapshots and write cost_report.json.
    Also prints a human-readable summary to stdout.
    """
    # ── VoiceAPI ──────────────────────────────────────────────────────────────
    va_start = start.get("voiceapi_chars")
    va_end   = end.get("voiceapi_chars")
    va_used: int | None  = None
    va_cost: float | None = None
    if va_start is not None and va_end is not None:
        va_used = va_start - va_end
        va_cost = max(0, va_used) * voiceapi_rate_per_char

    # ── VoidAI ────────────────────────────────────────────────────────────────
    vi_start = start.get("voidai_credits")
    vi_end   = end.get("voidai_credits")
    vi_used: int | None  = None
    # VoidAI is flat $35/month — credits are informational (daily quota, not $)
    if vi_start is not None and vi_end is not None:
        vi_used = vi_start - vi_end

    # ── Fixed monthly costs (pro-rated per video, amortized over 30 days) ────
    # VoidAI $35 + BetaTest $15 = $50/month fixed
    # Assumes ~1 video generated today; caller can override with actual count.
    fixed_monthly = 50.0   # $35 VoidAI + $15 BetaTest
    fixed_per_video_day = fixed_monthly / 30  # $1.67/day if 1 video/day

    # ── Report dict ───────────────────────────────────────────────────────────
    report = {
        "elapsed_seconds":    round(elapsed_s, 1),
        "voiceapi": {
            "chars_start":  va_start,
            "chars_end":    va_end,
            "chars_used":   va_used,
            "cost_usd":     round(va_cost, 6) if va_cost is not None else None,
            "rate_per_char": voiceapi_rate_per_char,
        },
        "voidai": {
            "credits_start": vi_start,
            "credits_end":   vi_end,
            "credits_used":  vi_used,
            "note":          "Flat $35/month plan — daily quota, not per-call billing",
        },
        "fixed_costs": {
            "voidai_monthly_usd":    35.0,
            "betatest_monthly_usd":  15.0,
            "total_monthly_usd":     fixed_monthly,
            "pro_rated_per_video_if_1_per_day": round(fixed_per_video_day, 4),
        },
        "snapshots": {"start": start, "end": end},
    }

    # ── Write JSON ────────────────────────────────────────────────────────────
    report_path = proj / "cost_report.json"
    try:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as _e:
        log.warning("Could not write cost_report.json: %s", _e)

    # ── Print summary ─────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  COST REPORT")
    print("=" * 60)
    print(f"  Generation time : {elapsed_s/60:.1f} min")
    print()
    print("  VoiceAPI (variable):")
    if va_used is not None:
        print(f"    Chars used  : {va_used:,}")
        print(f"    Cost        : ${va_cost:.4f}")
    else:
        print("    Balance unavailable (check voiceapi.csv666.ru)")
    print()
    print("  VoidAI (fixed $35/month, 4M credits/day):")
    if vi_used is not None:
        print(f"    Credits used: {vi_used:,}")
    else:
        print("    Credits tracking unavailable via API")
    print()
    print("  Fixed monthly costs:")
    print(f"    VoidAI  : $35.00/month")
    print(f"    BetaTest: $15.00/month  (unlimited images)")
    print(f"    Total   : $50.00/month")
    print()
    if va_cost is not None:
        print(f"  Variable cost this video : ${va_cost:.4f}")
    print(f"  Report saved: {report_path.name}")
    print("=" * 60)


# ─── Validation helpers ────────────────────────────────────────────────────────

def _require_files(paths: list[Path], *, min_bytes: int = 0, step: str = "") -> None:
    """Raise FileNotFoundError / ValueError if any path is missing or too small."""
    prefix = f"[Step {step}] " if step else ""
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(f"{prefix}Missing expected file: {p}")
        if min_bytes > 0 and p.stat().st_size < min_bytes:
            raise ValueError(
                f"{prefix}File too small ({p.stat().st_size} B < {min_bytes} B): {p}"
            )


def _load_script(script_path: Path) -> dict[str, Any]:
    """Load and sanity-check script.json."""
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found: {script_path}")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    if not script.get("blocks"):
        raise ValueError(f"script.json has no blocks: {script_path}")
    return script


# ─── UI helpers ───────────────────────────────────────────────────────────────

def _step_header(step: int, name: str) -> None:
    log.info("-" * 60)
    log.info("STEP %d/%d — %s", step, TOTAL_STEPS, name.upper())
    log.info("-" * 60)


def _review_pause(script_path: Path) -> None:
    """Print script summary and wait for user approval before continuing."""
    script = _load_script(script_path)
    blocks = script.get("blocks", [])
    title = script.get("title", "(untitled)")
    total_dur = sum(b.get("audio_duration") or 0.0 for b in blocks)

    print()
    print("=" * 60)
    print(f"  REVIEW — {title}")
    print("=" * 60)
    print(f"  Blocks   : {len(blocks)}")
    print(f"  Duration : {total_dur / 60:.1f} min  ({total_dur:.0f}s)")
    print()
    for i, b in enumerate(blocks[:5], 1):
        narr = (b.get("narration") or "")
        preview = narr[:80] + ("..." if len(narr) > 80 else "")
        print(f"  [{i}] {preview}")
    if len(blocks) > 5:
        print(f"  ... ({len(blocks) - 5} more blocks)")
    print()
    print(f"  Script path: {script_path}")
    print()

    try:
        resp = input("  Continue with images + voice generation? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        resp = "n"

    if resp not in ("y", "yes"):
        print()
        print("  Aborted by user. Edit script.json and re-run with --from-step 2.")
        sys.exit(0)
    print()


# ─── Step retry helper ────────────────────────────────────────────────────────

async def _retry_step(
    step_name: str,
    fn,
    *,
    max_retries: int = 1,
    progress_callback=None,
):
    """Run an async step function with retry. Don't retry budget/cancel errors."""
    for attempt in range(1, max_retries + 2):  # 1 initial + max_retries
        try:
            return await fn()
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except RuntimeError as exc:
            if "budget" in str(exc).lower() or "quality gate" in str(exc).lower():
                raise  # don't retry budget exceeded or quality gate failures
            if attempt > max_retries:
                raise
            log.warning(
                "Step '%s' failed (attempt %d/%d): %s — retrying in 3s",
                step_name, attempt, max_retries + 1, exc,
            )
            if progress_callback:
                _emit(progress_callback, type="step_retry", step_name=step_name, attempt=attempt)
            await asyncio.sleep(3)
        except Exception as exc:
            if attempt > max_retries:
                raise
            log.warning(
                "Step '%s' failed (attempt %d/%d): %s — retrying in 3s",
                step_name, attempt, max_retries + 1, exc,
            )
            if progress_callback:
                _emit(progress_callback, type="step_retry", step_name=step_name, attempt=attempt)
            await asyncio.sleep(3)


# ─── Step implementations ──────────────────────────────────────────────────────

async def _step_script(
    s_path: Path,
    proj: Path,
    source_dir: Path | None,
    channel_config_path: Path,
    chan_cfg: dict,
    cost: CostBudget,
    quality: str,
    template: str,
    duration_min: int,
    duration_max: int,
    *,
    progress_callback: Any = None,
    review_callback: Any = None,
    review: bool = False,
    dry_run: bool = False,
    force: bool = False,
    image_style: str = "",
    custom_topic: str = "",
    master_prompt: str | None = None,
    auto_approve: bool = False,
    db_tracker: Any = None,
    db_video_id: int | None = None,
    **kwargs: Any,
) -> tuple[Any, int]:
    """
    STEP 1 — SCRIPT.

    Returns (val_result, new_from_step).
    new_from_step is STEP_MEDIA when auto-skip fires (script.json already exists),
    otherwise STEP_SCRIPT (caller uses this to decide whether quality gate runs).
    """
    new_from_step = STEP_SCRIPT

    # Auto-skip: if script.json already exists and has valid blocks, don't re-generate.
    # This prevents burning expensive Opus credits on repeated runs.
    if s_path.exists() and not dry_run:
        try:
            _existing = json.loads(s_path.read_text(encoding="utf-8"))
            if _existing.get("blocks"):
                log.warning(
                    "script.json already exists with %d blocks — SKIPPING Step 1 to save credits. "
                    "Delete %s to force regeneration.",
                    len(_existing["blocks"]),
                    s_path,
                )
                _emit(progress_callback, type="step_start", step=STEP_SCRIPT, name=STEP_NAMES[STEP_SCRIPT], pct=STEP_WEIGHTS[STEP_SCRIPT][0])
                _emit(progress_callback, type="step_done",  step=STEP_SCRIPT, elapsed=0.0,                        pct=STEP_WEIGHTS[STEP_SCRIPT][1])
                new_from_step = STEP_MEDIA  # signal to caller: continue from step 2

                # 01c catch-up: run Image Planner if image_prompts lists are missing.
                # Happens when auto-skip fires on a fresh script that only has
                # image_prompt (singular, 1 per block from step 01) but 01c never ran.
                _needs_01c = any(
                    not _b.get("image_prompts")
                    for _b in _existing["blocks"]
                )
                if _needs_01c:
                    try:
                        _plan_images_catchup = _fn("modules/01c_image_planner.py", "plan_images")
                        _emit(progress_callback, type="sub_progress", step=STEP_SCRIPT,
                              pct=17.0, message="Art Director: planning image positions (catch-up)…")
                        await _plan_images_catchup(
                            s_path,
                            chan_cfg,
                            preset_name="high",
                            image_style=image_style or "",
                            progress_callback=progress_callback,
                        )
                        cost.add("Image Planner (Art Director catch-up)", round(17_000 * VOIDAI_PER_TOKEN, 5))
                        log.info("Image Planner catch-up: done")
                    except Exception as _iexc:
                        log.exception("Image Planner catch-up failed (non-fatal): %s", _iexc)

                # Skip generation — return early; quality gate will be bypassed by caller
                return None, new_from_step
        except Exception:
            pass  # corrupt JSON → regenerate normally

    _step_header(STEP_SCRIPT, STEP_NAMES[STEP_SCRIPT])
    _emit(progress_callback, type="step_start", step=STEP_SCRIPT, name=STEP_NAMES[STEP_SCRIPT], pct=STEP_WEIGHTS[STEP_SCRIPT][0])
    t0 = time.monotonic()

    if source_dir is None and not (custom_topic or "").strip():
        raise ValueError(
            "--source is required for step 1 unless --custom-topic is provided"
        )

    generate_scripts = _fn("modules/01_script_generator.py", "generate_scripts")

    MAX_SCRIPT_REGEN = 3
    _auto_regen_threshold = int(duration_min * TTS_WPM * 0.90)  # 90% of min target

    for _regen_attempt in range(1, MAX_SCRIPT_REGEN + 1):
        script_paths: list[Path] = await generate_scripts(
            source_dir,
            channel_config_path,
            template=template,
            preset=quality,
            dry_run=dry_run,
            output_dir=proj,
            duration_min=duration_min,
            duration_max=duration_max,
            master_prompt_path=master_prompt or None,
            image_style=image_style or "",
            custom_topic=custom_topic or "",
        )

        if dry_run:
            break

        # Check word count — auto-regen if < 90% of target
        _regen_sd = _load_script(s_path)
        _regen_blocks = _regen_sd.get("blocks", [])
        _regen_words = sum(len((b.get("narration") or "").split()) for b in _regen_blocks)

        if _regen_words >= _auto_regen_threshold:
            if _regen_attempt > 1:
                log.info(
                    "Auto-regen attempt %d/%d succeeded: %d words (threshold: %d)",
                    _regen_attempt, MAX_SCRIPT_REGEN, _regen_words, _auto_regen_threshold,
                )
            break

        if _regen_attempt < MAX_SCRIPT_REGEN:
            log.warning(
                "Auto-regen %d/%d: script too short (%d words, need %d) — regenerating",
                _regen_attempt, MAX_SCRIPT_REGEN, _regen_words, _auto_regen_threshold,
            )
            _emit(progress_callback, type="sub_progress", step=STEP_SCRIPT,
                  pct=10.0, message=f"Script too short ({_regen_words}w) — auto-regen {_regen_attempt+1}/{MAX_SCRIPT_REGEN}…")
            # Delete script.json so generate_scripts doesn't skip
            s_path.unlink(missing_ok=True)
        else:
            log.warning(
                "Auto-regen exhausted (%d attempts): %d words (need %d) — proceeding to review",
                MAX_SCRIPT_REGEN, _regen_words, _auto_regen_threshold,
            )

    val_result: Any = None

    if not dry_run:
        if not script_paths:
            raise RuntimeError("generate_scripts returned an empty list")
        # Update s_path in-place is not possible via return; caller must update s_path
        # from script_paths[0] — we signal via returned val_result which carries the path
        # HOWEVER: the caller already has s_path = proj / "script.json" and generate_scripts
        # writes there, so script_paths[0] == s_path in practice.  We keep the require-files
        # check here for safety.
        _generated_path = script_paths[0]
        _require_files([_generated_path], min_bytes=200, step="Script")
        log.info("Script saved: %s  (%.1fs)", _generated_path, time.monotonic() - t0)

        # ── Script validation + auto-fix ───────────────────────────────
        def _script_val_cb(ev: dict) -> None:
            _emit(progress_callback, **ev)
        try:
            _validate_script = _fn("modules/01b_script_validator.py", "validate_and_fix_script")
            val_result = await _validate_script(_generated_path, chan_cfg, progress_callback=_script_val_cb)
            if val_result.issues:
                log.info(
                    "Script validator: %d issues found, %d fixed",
                    len(val_result.issues), len(val_result.fixes_applied),
                )
            # Track LLM costs from auto-fix
            if val_result.fixes_applied:
                _bad_prompt_fixes = sum(1 for f in val_result.fixes_applied if "prompt" in f.lower())
                if _bad_prompt_fixes:
                    cost.add("Script validator (prompts)", _bad_prompt_fixes * round(5_000 * VOIDAI_PER_TOKEN, 6))
                if any("cont" in f.lower() for f in val_result.fixes_applied):
                    cost.add("Script validator (cut-off)", round(10_000 * VOIDAI_PER_TOKEN, 6))
        except Exception as _vexc:
            log.exception("Script validation skipped (non-fatal): %s", _vexc)

        # VoidAI subscription: ~20K input + 8K output tokens (flat rate regardless of model)
        cost.add("Script LLM", round(28_000 * VOIDAI_PER_TOKEN, 5))
        cost.check(progress_callback)

        # ── Image Planner (Art Director pass — Step 1c) ─────────────────
        # Always runs: positions are calculated algorithmically (2-tier density
        # model), then Art Director LLM writes one structured prompt per position.
        # Does NOT rely on __MARKER__ sentinels — those are legacy and ignored.
        try:
            _plan_images = _fn("modules/01c_image_planner.py", "plan_images")
            _emit(progress_callback, type="sub_progress", step=STEP_SCRIPT,
                  pct=17.0, message="Art Director: planning image positions…")
            await _plan_images(
                _generated_path,
                chan_cfg,
                preset_name="high",          # Sonnet — sufficient for visual creativity
                image_style=image_style or "",
                progress_callback=progress_callback,
            )
            # VoidAI subscription: ~7K input + 10K output tokens (flat rate)
            cost.add("Image Planner (Art Director)", round(17_000 * VOIDAI_PER_TOKEN, 5))
            log.info("Image Planner: done")
        except Exception as _iexc:
            log.exception("Image Planner failed (non-fatal, continuing): %s", _iexc)

    else:
        log.info("[DRY RUN] Script step complete (no file written)")

    _emit(progress_callback, type="step_done", step=STEP_SCRIPT, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_SCRIPT][1])

    # ── Script quality gate ─────────────────────────────────────────────
    _quality_gate_failed = False
    if not dry_run:
        _sd_gate = _load_script(s_path)
        _gate_blocks = _sd_gate.get("blocks", [])
        _gate_words = sum(len((b.get("narration") or "").split()) for b in _gate_blocks)
        _gate_threshold = int(duration_min * 130 * 0.8)  # 80% of min target at slowest rate
        if _gate_words < _gate_threshold:
            _quality_gate_failed = True
            log.warning(
                "Script quality gate WARNING: %d words (need %d for %d-min video) — "
                "will show in review for regen decision",
                _gate_words, _gate_threshold, duration_min,
            )
        else:
            log.info("Script quality gate OK: %d words (threshold: %d)", _gate_words, _gate_threshold)

    # ── Record script metrics for A/B analysis ─────────────────────────────
    if not dry_run and db_tracker and db_video_id:
        try:
            _sd_metrics = _load_script(s_path)
            _m_blocks = _sd_metrics.get("blocks", [])
            _m_words = sum(len((b.get("narration") or "").split()) for b in _m_blocks)
            _m_hook = 0
            _intro = [b for b in _m_blocks if b.get("type") == "intro"]
            if _intro:
                _hm = _intro[0].get("hook")
                if isinstance(_hm, dict):
                    _m_hook = _hm.get("validation_score", 0) or 0
            _chan_cfg_m = load_channel_config(channel_config_path)
            _prompt_ver = Path(_chan_cfg_m.get("master_prompt_path", "")).stem
            _script_model = get_llm_preset(_chan_cfg_m, quality).get("script", "unknown")
            db_tracker.record_script_metrics(
                video_id=db_video_id,
                model=_script_model,
                template=template,
                prompt_version=_prompt_ver,
                temperature=0.7,
                word_count=_m_words,
                block_count=len(_m_blocks),
                hook_score=_m_hook,
                duration_est_min=round(_m_words / 170, 1),
            )
        except Exception as _me:
            log.warning("Script metrics recording failed (non-fatal): %s", _me)

    # Review pause — CLI (--review flag) or WebSocket (review_callback)
    if not dry_run:
        if review:
            _review_pause(s_path)
        elif review_callback is not None:
            _sd = _load_script(s_path)
            _blocks = _sd.get("blocks", [])

            # ── Compute rich review stats ──────────────────────────────────
            # Word count + duration (audio_duration is null before TTS)
            _word_count = sum(len((b.get("narration") or "").split()) for b in _blocks)
            _dur_min = round(_word_count / 150, 1)   # ~150 wpm reading
            _dur_max = round(_word_count / 130, 1)   # ~130 wpm slow reading

            # Block type breakdown
            _type_counts: dict[str, int] = {}
            for _b in _blocks:
                _t = _b.get("type", "section")
                _type_counts[_t] = _type_counts.get(_t, 0) + 1

            # Total image prompts (sum image_prompts lists, fallback image_prompt)
            _total_imgs = sum(
                len(_b.get("image_prompts") or []) or (1 if (_b.get("image_prompt") or "").strip() else 0)
                for _b in _blocks
            )

            # Hook detection: prefer hook.validation_score from script (set by LLM validator).
            # Falls back to keyword heuristic only if hook metadata is absent (old scripts).
            _intro_blocks = [_b for _b in _blocks if _b.get("type") == "intro"]
            _has_hook = False
            if _intro_blocks:
                _intro = _intro_blocks[0]
                _hook_meta = _intro.get("hook")
                if isinstance(_hook_meta, dict):
                    # LLM-validated: score ≥ 3/4 = passed; score absent = just generated (trust it)
                    _score = _hook_meta.get("validation_score")
                    _has_hook = (_score is None or _score >= 3)
                else:
                    # Fallback: simple keyword heuristic (older script format)
                    _intro_text = (_intro.get("narration") or "").strip()
                    _hook_signals = [
                        "?", "Що якби", "Уявіть", "Як", "Чому",
                        "Imagine", "What if", "Why", "You've", "Most ",
                        "Nobody", "Everyone", "Here's", "The truth",
                    ]
                    _has_hook = any(sig in _intro_text[:200] for sig in _hook_signals)

            # Per-block summary (compact — title + type + word count + image count)
            _block_summaries = [
                {
                    "id":          _b.get("id", ""),
                    "type":        _b.get("type", "section"),
                    "title":       _b.get("title", ""),
                    "word_count":      len((_b.get("narration") or "").split()),
                    "image_count":     len(_b.get("image_prompts") or []) or (1 if (_b.get("image_prompt") or "").strip() else 0),
                    "narration":       (_b.get("narration") or "")[:120],
                    "est_duration_sec": round(len((_b.get("narration") or "").split()) / 170 * 60, 1),
                }
                for _b in _blocks
            ]

            _review_data = {
                "script_path":   str(s_path),
                "title":         _sd.get("title", ""),
                "block_count":   len(_blocks),
                "word_count":    _word_count,
                "duration_min":  _dur_min,
                "duration_max":  _dur_max,
                "type_counts":   _type_counts,
                "image_prompt_count": _total_imgs,
                "has_hook":      _has_hook,
                "quality_gate_failed": _quality_gate_failed if '_quality_gate_failed' in dir() else False,
                "blocks":        _block_summaries,
                # Regen params — used by /jobs/{id}/regen-script
                "_regen": {
                    "channel_config_path": str(channel_config_path),
                    "source_dir":    str(source_dir) if source_dir else "",
                    "output_dir":    str(proj),
                    "quality":       quality,
                    "template":      template,
                    "duration_min":  duration_min,
                    "duration_max":  duration_max,
                    "master_prompt": master_prompt or "",
                    "image_style":   image_style or "",
                    "custom_topic":  custom_topic or "",
                },
            }

            # Auto-approve: skip review if all quality criteria met
            _script_auto_ok = False
            if auto_approve:
                _min_words = int(duration_min * 130 * 0.9)   # slowest rate − 10% tolerance
                _max_words = int(duration_max * 170 * 1.1)   # fastest rate + 10% tolerance
                _has_critical = getattr(val_result, "has_critical", False) if val_result else False
                _script_auto_ok = (
                    _has_hook
                    and _word_count >= _min_words
                    and _word_count <= _max_words
                    and _total_imgs >= len(_blocks)
                    and not _has_critical
                    and not _quality_gate_failed
                )
                if _script_auto_ok:
                    log.warning(
                        "Auto-approve script OK: hook=%s, words=%d (%d-%d), imgs=%d, blocks=%d",
                        _has_hook, _word_count, _min_words, _max_words, _total_imgs, len(_blocks),
                    )
                    _emit(progress_callback, type="auto_approved", stage="script")
                else:
                    log.warning(
                        "Auto-approve FAILED for script (hook=%s, words=%d, range=%d-%d, imgs=%d, critical=%s)"
                        " — falling back to manual review",
                        _has_hook, _word_count, _min_words, _max_words, _total_imgs, _has_critical,
                    )

            if not _script_auto_ok:
                await review_callback("script", _review_data)

    # ── Mark script review as passed in metrics ─────────────────────────────
    if not dry_run and db_tracker and db_video_id:
        try:
            db_tracker.update_script_review(db_video_id, passed=True)
        except Exception:
            pass

    return val_result, new_from_step


async def _step_media(
    s_path: Path,
    proj: Path,
    source_dir: Path | None,
    channel_config_path: Path,
    chan_cfg: dict,
    cost: CostBudget,
    quality: str,
    *,
    progress_callback: Any = None,
    review_callback: Any = None,
    voice_id: str | None = None,
    image_style: str = "",
    image_backend: str | None = None,
    vision_model: str | None = None,
    auto_approve: bool = False,
    dry_run: bool = False,
    langs: list[str] | None = None,
    **kwargs: Any,
) -> None:
    """STEP 2 — IMAGES + VOICES (parallel)."""
    _step_header(STEP_MEDIA, STEP_NAMES[STEP_MEDIA])
    _emit(progress_callback, type="step_start", step=STEP_MEDIA, name=STEP_NAMES[STEP_MEDIA], pct=STEP_WEIGHTS[STEP_MEDIA][0])
    t0 = time.monotonic()

    if dry_run and not s_path.exists():
        # No real script — provide estimated costs for a typical 10-block video
        log.info("[DRY RUN] No script.json (step 1 was also dry-run); using typical 10-block estimate")
        n_langs = len(langs) if langs else 1
        cost.add("Images estimate (10 blocks, WaveSpeed)", 10 * 0.005)
        cost.add("Voice estimate (5000 chars/lang)", n_langs * 5000 * 0.0000038)
    else:
        generate_images = _fn("modules/02_image_generator.py", "generate_images")
        generate_voices = _fn("modules/03_voice_generator.py", "generate_voices")

        # Primary language for voices
        primary_lang = langs[0] if langs else None

        # ── Sub-progress for Images + Voices (step 2, global 15-55%) ──
        # Images track local 0-100%, voices track local 0-100%.
        # Combined bar = avg(img_pct, voice_pct) mapped to global range.
        _m_start, _m_end = STEP_WEIGHTS[STEP_MEDIA]
        _media_local: dict[str, float] = {"img": 0.0, "voice": 0.0}

        def _emit_media_pct(msg: str = "") -> None:
            combined = (_media_local["img"] + _media_local["voice"]) / 2.0
            global_pct = _m_start + (combined / 100.0) * (_m_end - _m_start)
            _emit(
                progress_callback,
                type="sub_progress",
                step=STEP_MEDIA,
                pct=round(global_pct, 1),
                message=msg,
            )

        def _img_sub_cb(event: dict) -> None:
            if event.get("type") == "sub_progress":
                _media_local["img"] = float(event.get("pct", 0.0))
                _emit_media_pct(event.get("message", ""))

        def _voice_sub_cb(event: dict) -> None:
            if event.get("type") == "sub_progress":
                _media_local["voice"] = float(event.get("pct", 0.0))
                _emit_media_pct(event.get("message", ""))

        # Images + primary voice in parallel
        img_task = generate_images(
            s_path,
            channel_config_path,
            dry_run=dry_run,
            skip_existing=True,
            image_style=image_style or None,
            validate=False,   # Inline validation disabled — 02b handles all validation/regen
            image_backend=image_backend or None,
            progress_callback=_img_sub_cb,
        )
        voice_task = generate_voices(
            s_path,
            channel_config_path,
            lang=primary_lang,
            voice_id_override=voice_id or None,
            dry_run=dry_run,
            skip_existing=True,
            progress_callback=_voice_sub_cb,
        )

        img_summary, voice_summary = await asyncio.gather(img_task, voice_task)

        # ── Image validation + auto-regen ──────────────────────────────
        _img_val_data: dict = {}
        try:
            _validate_images = _fn("modules/02b_image_validator.py", "validate_and_fix_images")
            _images_dir = proj / "images"
            _img_threshold = float(chan_cfg.get("image_validation_threshold", 7.0))
            _img_val = await _validate_images(
                s_path, _images_dir, chan_cfg,
                threshold=_img_threshold,
                vision_model=vision_model or None,
                progress_callback=_img_sub_cb,
            )
            _img_val_data = _img_val.to_dict()
            # Add image URLs for frontend preview (/projects is a static mount)
            # Use relative path from projects root to include channel subfolder
            # e.g. proj = .../projects/1/VideoTitle → rel = "1/VideoTitle"
            try:
                _rel_parts = proj.relative_to(ROOT / "projects").parts
                _enc_name = "/".join(_url_quote(p, safe="") for p in _rel_parts)
            except ValueError:
                _enc_name = _url_quote(proj.name, safe="")
            for _sc in _img_val_data.get("scores", []):
                _idx = _sc.get("image_index", 0)
                _fname = f"{_sc['block_id']}.png" if _idx == 0 else f"{_sc['block_id']}_{_idx}.png"
                _sc["image_url"] = f"/projects/{_enc_name}/images/{_fname}"
            # Track scoring + regen costs
            # VoidAI vision: ~2K tokens per image scored (flat rate)
            cost.add("Image validator (scoring)", _img_val.total * round(2_000 * VOIDAI_PER_TOKEN, 6))
            if _img_val.regenerated > 0:
                cost.add("Image validator (regen)", _img_val.regenerated * 0.005)
            log.info(
                "Image validator: %d/%d OK, %d regen, %d failed",
                _img_val.ok_count, _img_val.total,
                _img_val.regenerated, _img_val.failed,
            )
        except Exception as _ivexc:
            log.exception("Image validation skipped (non-fatal): %s", _ivexc)

        # Review checkpoint after images
        _img_review_data = {
            "images_dir": str(proj / "images"),
            "script_path": str(s_path),
            "source_name": proj.name,
            "validation": _img_val_data,
        }

        _img_auto_ok = False
        if auto_approve and _img_val_data:
            _iv = _img_val_data
            _total = max(_iv.get("total", 1), 1)
            _ok = _iv.get("ok", _iv.get("ok_count", 0))  # to_dict() uses "ok"
            _ok_pct = (_ok + _iv.get("regenerated", 0)) / _total
            _fail_pct = _iv.get("failed", 0) / _total
            _img_auto_ok = (
                _ok_pct >= 0.90          # at least 90% OK/regenerated
                and _fail_pct <= 0.10    # at most 10% failed
            )
            if _img_auto_ok:
                log.warning(
                    "Auto-approve images OK: %d/%d OK, %d regen, %d failed, %d skipped",
                    _ok, _iv.get("total", 0),
                    _iv.get("regenerated", 0), _iv.get("failed", 0), _iv.get("skipped", 0),
                )
                _emit(progress_callback, type="auto_approved", stage="images")
            else:
                log.warning(
                    "Auto-approve FAILED for images (ok=%d, total=%d, failed=%d, skipped=%d)"
                    " — falling back to manual review",
                    _ok, _iv.get("total", 0),
                    _iv.get("failed", 0), _iv.get("skipped", 0),
                )

        if not _img_auto_ok and review_callback is not None:
            await review_callback("images", _img_review_data)

        # Additional language voices (sequential to respect rate limits)
        if langs and len(langs) > 1:
            for lang_code in langs[1:]:
                log.info("Generating voice for language: %s", lang_code)
                await generate_voices(
                    s_path,
                    channel_config_path,
                    lang=lang_code,
                    dry_run=dry_run,
                    skip_existing=True,
                )

    if not dry_run:
        elapsed = time.monotonic() - t0

        # Validate audio files exist — retry missing ones up to 2 times
        script_data = _load_script(s_path)
        blocks = script_data["blocks"]
        audio_subdir = "audio"
        audio_dir = proj / audio_subdir
        voiced_blocks = [b for b in blocks if (b.get("narration") or "").strip()]
        audio_files = [audio_dir / f"{b['id']}.mp3" for b in voiced_blocks]

        _missing = [p for p in audio_files if not p.exists() or p.stat().st_size < 500]
        if _missing and not dry_run:
            _max_voice_retries = 2
            # Re-load helpers that were defined in the else branch above.
            # They may not exist if dry_run=True branched away, but we're inside
            # `if not dry_run` so they are guaranteed to be defined.
            primary_lang = langs[0] if langs else None
            generate_voices = _fn("modules/03_voice_generator.py", "generate_voices")
            _media_local_retry: dict[str, float] = {"img": 100.0, "voice": 0.0}
            _m_start_r, _m_end_r = STEP_WEIGHTS[STEP_MEDIA]

            def _voice_sub_cb_retry(event: dict) -> None:
                if event.get("type") == "sub_progress":
                    _media_local_retry["voice"] = float(event.get("pct", 0.0))
                    combined = (_media_local_retry["img"] + _media_local_retry["voice"]) / 2.0
                    global_pct = _m_start_r + (combined / 100.0) * (_m_end_r - _m_start_r)
                    _emit(progress_callback, type="sub_progress", step=STEP_MEDIA,
                          pct=round(global_pct, 1), message=event.get("message", ""))

            for _retry_i in range(1, _max_voice_retries + 1):
                log.warning(
                    "Audio retry %d/%d — %d missing block(s): %s",
                    _retry_i, _max_voice_retries,
                    len(_missing), [p.stem for p in _missing],
                )
                _emit(
                    progress_callback, type="sub_progress",
                    step=STEP_MEDIA, pct=85.0,
                    message=f"Retrying {len(_missing)} failed audio block(s) ({_retry_i}/{_max_voice_retries})…",
                )
                # Re-run voice generation (skip_existing=True will only redo missing)
                await generate_voices(
                    s_path, channel_config_path,
                    lang=primary_lang,
                    voice_id_override=voice_id or None,
                    dry_run=False,
                    skip_existing=True,
                    progress_callback=_voice_sub_cb_retry,
                )
                _missing = [p for p in audio_files if not p.exists() or p.stat().st_size < 500]
                if not _missing:
                    log.info("Audio retry %d: all blocks recovered", _retry_i)
                    break

        _require_files(audio_files, min_bytes=500, step="Media/Audio")

        n_gen = getattr(img_summary, "generated", 0)
        n_chars = getattr(voice_summary, "total_chars", 0)
        log.info(
            "Media done (%.1fs) | Images generated: %d | Voice chars: %d",
            elapsed, n_gen, n_chars,
        )

        # Use actual costs from summaries where available
        ws_cost = getattr(img_summary, "wavespeed_cost", n_gen * 0.005)
        va_cost = getattr(img_summary, "voidai_cost", 0.0)
        cost.add("Images (WaveSpeed)", ws_cost)
        if va_cost > 0:
            cost.add("Images (VoidAI fallback)", va_cost)
        # Voice: VoiceAPI rate = $19 / 5,000,000 chars = $0.0000038/char
        cost.add("Voice (VoiceAPI)", n_chars * 0.0000038)
        if langs and len(langs) > 1:
            for _ in langs[1:]:
                cost.add("Voice extra lang", n_chars * 0.0000038)

        cost.check(progress_callback)
    _emit(progress_callback, type="step_done", step=STEP_MEDIA, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_MEDIA][1])


async def _step_subtitles(
    s_path: Path,
    proj: Path,
    channel_config_path: Path,
    *,
    progress_callback: Any = None,
    dry_run: bool = False,
    langs: list[str] | None = None,
    **kwargs: Any,
) -> None:
    """STEP 3 — SUBTITLES."""
    _step_header(STEP_SUBTITLES, STEP_NAMES[STEP_SUBTITLES])
    _emit(progress_callback, type="step_start", step=STEP_SUBTITLES, name=STEP_NAMES[STEP_SUBTITLES], pct=STEP_WEIGHTS[STEP_SUBTITLES][0])
    t0 = time.monotonic()

    if dry_run and not s_path.exists():
        log.info("[DRY RUN] Subtitles: no script.json — step skipped (no API cost)")
    else:
        generate_subtitles = _fn("modules/04_subtitle_generator.py", "generate_subtitles")

        # Always use block audio_duration timing (TTS-accurate).
        # from_transcript (original video's transcript.srt) is intentionally NOT used here:
        # it contains the source video's text & timing, not the new AI-generated narration,
        # so it would show wrong text at wrong timestamps.
        # Primary language subtitles
        srt_path, ass_path = generate_subtitles(
            s_path,
            channel_config_path,
        )

        # Additional language subtitles
        if langs and len(langs) > 1:
            for lang_code in langs[1:]:
                log.info("Generating subtitles for language: %s", lang_code)
                generate_subtitles(s_path, channel_config_path, lang=lang_code)

        if not dry_run:
            _require_files([srt_path, ass_path], min_bytes=10, step="Subtitles")
            log.info(
                "Subtitles: %s, %s  (%.1fs)",
                srt_path.name, ass_path.name, time.monotonic() - t0,
            )
    _emit(progress_callback, type="step_done", step=STEP_SUBTITLES, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_SUBTITLES][1])


async def _step_video(
    s_path: Path,
    proj: Path,
    channel_config_path: Path,
    *,
    progress_callback: Any = None,
    dry_run: bool = False,
    draft: bool = False,
    burn_subtitles: bool = False,
    background_music: bool = True,
    no_ken_burns: bool = False,
    music_volume: float | None = None,
    music_track: str | None = None,
    **kwargs: Any,
) -> "Path | None":
    """STEP 4 — VIDEO. Returns the output video path, or None on dry-run / skip."""
    _step_header(STEP_VIDEO, STEP_NAMES[STEP_VIDEO])
    _emit(progress_callback, type="step_start", step=STEP_VIDEO, name=STEP_NAMES[STEP_VIDEO], pct=STEP_WEIGHTS[STEP_VIDEO][0])
    t0 = time.monotonic()

    video_path_result: "Path | None" = None

    # compile_video needs full_narration.mp3 even in dry_run
    audio_dir_path = proj / "audio"
    narration_candidates = list(audio_dir_path.glob("full_narration*.mp3")) if audio_dir_path.exists() else []
    if dry_run and not narration_candidates:
        log.info("[DRY RUN] Video: no full_narration.mp3 yet — step skipped (FFmpeg, no API cost)")
    else:
        compile_video = _fn("modules/05_video_compiler.py", "compile_video")

        # Sub-progress: map local 0–100 from compile_video → global pct range for step 4
        _vid_start, _vid_end = STEP_WEIGHTS[STEP_VIDEO]
        _loop = asyncio.get_event_loop()

        def _video_sub_cb(event: dict) -> None:
            if event.get("type") == "sub_progress" and "pct" in event:
                local_pct = float(event["pct"])
                global_pct = _vid_start + (local_pct / 100.0) * (_vid_end - _vid_start)
                new_ev = {
                    "type":    "sub_progress",
                    "pct":     round(global_pct, 1),
                    "step":    STEP_VIDEO,
                    "message": event.get("message", ""),
                }
                # Thread-safe: schedule emit back on the event loop
                _loop.call_soon_threadsafe(lambda e=new_ev: _emit(progress_callback, **e))

        # compile_video is synchronous — run in executor to avoid blocking the loop
        video_path: Path = await _loop.run_in_executor(
            None,
            lambda: compile_video(
                s_path,
                channel_config_path,
                draft=draft,
                dry_run=dry_run,
                no_subs=not burn_subtitles,
                no_music=not background_music,
                no_ken_burns=no_ken_burns,
                music_volume_override=music_volume,
                music_track_override=music_track,
                progress_callback=_video_sub_cb,
            ),
        )

        if not dry_run:
            _require_files([video_path], min_bytes=50_000, step="Video")
            video_path_result = video_path
            size_mb = video_path.stat().st_size / 1_048_576
            log.info(
                "Video: %s  (%.1f MB, %.1fs)",
                video_path.name, size_mb, time.monotonic() - t0,
            )
    _emit(progress_callback, type="step_done", step=STEP_VIDEO, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_VIDEO][1])
    return video_path_result


async def _step_thumbnail(
    s_path: Path,
    proj: Path,
    channel_config_path: Path,
    cost: CostBudget,
    quality: str,
    *,
    progress_callback: Any = None,
    dry_run: bool = False,
    skip_thumbnail: bool = False,
    source_dir: Path | None = None,
    **kwargs: Any,
) -> "Path | None":
    """STEP 5 — THUMBNAIL. Returns the best thumbnail path, or None on skip/dry-run."""
    _step_header(STEP_THUMBNAIL, STEP_NAMES[STEP_THUMBNAIL])
    _emit(progress_callback, type="step_start", step=STEP_THUMBNAIL, name=STEP_NAMES[STEP_THUMBNAIL], pct=STEP_WEIGHTS[STEP_THUMBNAIL][0])
    t0 = time.monotonic()

    thumb_path_result: "Path | None" = None

    if skip_thumbnail:
        log.info("Thumbnail generation SKIPPED (skip_thumbnail=True)")
    elif dry_run and not s_path.exists():
        log.info("[DRY RUN] Thumbnail estimate: ~$0.010 (2 WaveSpeed attempts avg)")
        cost.add("Thumbnail estimate (WaveSpeed)", 2 * 0.005)
    else:
        generate_thumbnail_variants = _fn(
            "modules/06_thumbnail_generator.py", "generate_thumbnail_variants"
        )

        # Use Transcriber thumbnail_prompt.txt if available
        transcriber_dir: Path | None = None
        if source_dir and (source_dir / "thumbnail_prompt.txt").exists():
            transcriber_dir = source_dir

        # Generate 3 thumbnail variants for A/B testing
        thumb_results = await generate_thumbnail_variants(
            s_path,
            channel_config_path,
            count=3,
            transcriber_dir=transcriber_dir,
            dry_run=dry_run,
            preset=quality,
        )

        if not dry_run and thumb_results:
            # Best is already copied to thumbnail.png by generate_thumbnail_variants
            best = max(thumb_results, key=lambda r: getattr(r, "score", -1))
            thumb_path = getattr(best, "output_path", None)
            if thumb_path:
                _require_files([Path(thumb_path)], min_bytes=1_000, step="Thumbnail")
                thumb_path_result = Path(thumb_path)
                total_attempts = sum(getattr(r, "attempts", 1) for r in thumb_results)
                best_score = getattr(best, "score", -1)
                log.info(
                    "Thumbnails: %d variants | best score=%d | total_attempts=%d | %.1fs",
                    len(thumb_results), best_score, total_attempts, time.monotonic() - t0,
                )
                cost.add("Thumbnails (WaveSpeed ×3)", len(thumb_results) * 0.005)
                cost.check(progress_callback)

    if not skip_thumbnail:
        _emit(progress_callback, type="step_done", step=STEP_THUMBNAIL, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_THUMBNAIL][1])
    else:
        _emit(progress_callback, type="step_done", step=STEP_THUMBNAIL, elapsed=0.0, pct=STEP_WEIGHTS[STEP_THUMBNAIL][1])

    return thumb_path_result


async def _step_metadata(
    s_path: Path,
    proj: Path,
    channel_config_path: Path,
    cost: CostBudget,
    quality: str,
    *,
    progress_callback: Any = None,
    dry_run: bool = False,
    **kwargs: Any,
) -> None:
    """STEP 6 — METADATA."""
    _step_header(STEP_METADATA, STEP_NAMES[STEP_METADATA])
    _emit(progress_callback, type="step_start", step=STEP_METADATA, name=STEP_NAMES[STEP_METADATA], pct=STEP_WEIGHTS[STEP_METADATA][0])
    t0 = time.monotonic()

    if dry_run and not s_path.exists():
        log.info("[DRY RUN] Metadata estimate: ~$0.001 (VoidAI flat rate, ~5K tokens)")
        cost.add("Metadata LLM estimate", round(5_000 * VOIDAI_PER_TOKEN, 5))
    else:
        generate_metadata = _fn("modules/07_metadata_generator.py", "generate_metadata")

        meta_path: Path = await generate_metadata(
            s_path,
            channel_config_path,
            preset=quality,
            title_count=3,   # Generate 3 title variants for A/B testing
            dry_run=dry_run,
        )

        if not dry_run:
            _require_files([meta_path], min_bytes=50, step="Metadata")
            log.info("Metadata: %s  (%.1fs)", meta_path.name, time.monotonic() - t0)
            cost.add("Metadata LLM", round(5_000 * VOIDAI_PER_TOKEN, 5))
            cost.check(progress_callback)
    _emit(progress_callback, type="step_done", step=STEP_METADATA, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_METADATA][1])


# ─── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    source_dir: Path | None,
    channel_config_path: Path,
    *,
    quality: str = "max",
    template: str = "auto",
    review: bool = False,
    dry_run: bool = False,
    draft: bool = False,
    from_step: int = 1,
    to_step: int = TOTAL_STEPS,   # Stop after this step (inclusive); default = run all
    langs: list[str] | None = None,
    budget: float | None = None,
    project_dir: Path | None = None,
    script_path_override: Path | None = None,
    db_tracker: Any | None = None,   # VideoTracker instance (optional)
    db_video_id: int | None = None,  # Pre-created video row id (passed by batch_runner)
    progress_callback: Any | None = None,  # Callable({type, step, ...}) for real-time updates
    review_callback: Any | None = None,    # async callable(stage, data) → None; WebSocket review
    background_music: bool = True,     # Mix background music (False = no_music in compile_video)
    image_style: str | None = None,    # Override image style from channel config
    voice_id: str | None = None,       # Override voice ID from channel config
    master_prompt: str | None = None,  # Override master prompt path
    no_ken_burns: bool = False,        # Skip Ken Burns — static slideshow (1 FFmpeg call, much faster)
    duration_min: int = 8,             # Minimum target video duration in minutes
    duration_max: int = 12,            # Maximum target video duration in minutes
    skip_thumbnail: bool = False,      # Skip thumbnail generation (Step 5)
    burn_subtitles: bool = False,      # Burn generated subtitles into video (Step 4 must have run)
    music_volume: float | None = None, # BGM volume in dB override; None = channel config (-28)
    music_track: str | None = None,    # Explicit music file path; None = channel config random pick
    custom_topic: str | None = None,   # Override topic for new script (replaces reference video title)
    image_backend: str | None = None,  # Image provider: "wavespeed" | "betatest" | "voidai"
    vision_model: str | None = None,   # Vision model for image analysis: "gpt-4.1" | "gpt-4.1-mini"
    auto_approve: bool = False,       # Auto-approve script/image review if quality criteria met
    force: bool = False,              # Force regenerate from scratch — nuke project dir first
) -> None:
    """
    Run the full VideoForge pipeline.

    Args:
        source_dir: Transcriber output directory (required for step 1).
        channel_config_path: Path to channel config JSON.
        quality: LLM preset (max/high/balanced/bulk/test).
        template: Content template (auto/documentary/listicle/tutorial/comparison).
        review: Pause after script generation and wait for user approval.
        dry_run: Estimate costs without making API calls.
        draft: Generate 480p preview video without effects.
        from_step: Start from this step (1–6); earlier steps are assumed cached.
        langs: Language codes for multilingual voice/subs (e.g. ["en", "de", "es"]).
        budget: Maximum spend in USD; pipeline halts if exceeded.
        project_dir: Explicit project output directory.
        script_path_override: Use this script.json directly.
    """
    # Clear module cache at the start of each run so code changes on disk
    # (e.g. bug fixes committed during a live server session) are picked up immediately.
    _module_cache.clear()

    cost = CostBudget(limit=budget)
    t_pipeline = time.monotonic()

    # ── Resolve project directory ──────────────────────────────────────────────
    _channel_name = channel_config_path.stem or "unknown"

    if project_dir:
        proj = project_dir
    elif source_dir:
        # Use custom_topic as folder name if provided, otherwise use reference video name
        if custom_topic and custom_topic.strip():
            _safe_topic = re.sub(r'[\\/:*?"<>|]', "_", custom_topic.strip())[:200].strip(". ")
            folder_name = _safe_topic or source_dir.name
        else:
            folder_name = source_dir.name
        proj = ROOT / "projects" / _channel_name / folder_name
    elif custom_topic and custom_topic.strip():
        # Topic-only mode: no reference video, derive project dir from topic name
        _safe_topic = re.sub(r'[\\/:*?"<>|]', "_", custom_topic.strip())[:200].strip(". ")
        proj = ROOT / "projects" / _channel_name / _safe_topic
    else:
        raise ValueError("Either source_dir or project_dir must be provided")

    # ── Force: nuke project dir so everything regenerates from scratch ────────
    if force and proj.exists():
        import shutil as _shutil
        log.warning("[Force] Deleting project dir for clean regeneration: %s", proj)
        _shutil.rmtree(proj, ignore_errors=True)

    proj.mkdir(parents=True, exist_ok=True)

    # ── Validate image_style (required — must be set via UI, no channel_config fallback) ──
    if not (image_style or "").strip():
        raise ValueError(
            "image_style is required. Set it in the UI (Image Style field) before running the pipeline."
        )

    # ── Channel config (read once, passed to all validators) ──────────────────
    _chan_cfg: dict[str, Any] = {}
    try:
        _chan_cfg = json.loads(channel_config_path.read_text(encoding="utf-8"))
    except Exception as _ce:
        log.warning("Could not load channel config: %s", _ce)

    # If image_backend not explicitly specified, read from channel config images.provider.
    # Allows history.json to set "voiceimage" without needing UI selection every run.
    if not image_backend:
        _chan_img_provider = (_chan_cfg.get("images") or {}).get("provider", "")
        if _chan_img_provider:
            image_backend = _chan_img_provider
            log.info("Image backend from channel config: %s", image_backend)

    # ── DB tracking variables ──────────────────────────────────────────────────
    _vid_id: int | None = None
    _video_path: Path | None = None
    _thumb_path: Path | None = None

    # ── Resolve script path ────────────────────────────────────────────────────
    if script_path_override:
        s_path = script_path_override
    else:
        s_path = proj / "script.json"

    log.info("Project dir : %s", proj)
    log.info("Channel     : %s", channel_config_path.name)
    log.info("Quality     : %s", quality)
    log.info("Template    : %s", template)
    log.info("From step   : %d (%s)", from_step, STEP_NAMES[from_step])
    if to_step < TOTAL_STEPS:
        log.info("To step     : %d (%s)  ← pipeline will STOP after this step", to_step, STEP_NAMES.get(to_step, str(to_step)))
    if langs:
        log.info("Languages   : %s", ", ".join(langs))
    if budget is not None:
        log.info("Budget      : $%.2f", budget)
    if dry_run:
        log.info("Mode        : DRY RUN (no API calls)")
    elif draft:
        log.info("Mode        : DRAFT (480p, no effects)")

    # ── DB: create/activate video record ──────────────────────────────────────
    if db_tracker is not None and not dry_run:
        if db_video_id is not None:
            _vid_id = db_video_id
        else:
            _vid_id = db_tracker.create_video(
                source_dir=source_dir or proj,
                channel=channel_config_path.stem,
                quality_preset=quality,
                template=template,
                from_step=from_step,
                project_dir=proj,
            )
        db_tracker.set_running(_vid_id)
        log.info("DB tracking : video_id=%d", _vid_id)

    # ── Balance snapshot: START ────────────────────────────────────────────────
    _balances_start: dict[str, Any] = {}
    if not dry_run:
        try:
            _balances_start = await _fetch_balances()
            va = _balances_start.get("voiceapi_chars")
            vi = _balances_start.get("voidai_credits")
            log.info(
                "[BALANCE START] VoiceAPI: %s chars | VoidAI: %s credits",
                f"{va:,}" if va is not None else "N/A",
                f"{vi:,}" if vi is not None else "N/A",
            )
        except Exception as _be:
            log.debug("Balance snapshot (start) failed: %s", _be)

    # ── Upfront cost estimate (dry-run only) ───────────────────────────────────
    if dry_run:
        try:
            script_for_estimate = s_path if s_path.exists() else (script_path_override or None)
            tracker = estimate_cost(
                script_path=script_for_estimate,
                channel_config_path=channel_config_path,
                quality_preset=quality,
                n_langs=len(langs) if langs else 1,
            )
            print()
            print("=" * 60)
            print("  COST ESTIMATE (DRY RUN)")
            print("=" * 60)
            print(tracker.summary_table())
            print()
            sys.stdout.flush()
        except Exception as exc:
            log.debug("Cost estimate failed: %s", exc)

    # Validator result — shared between script generation and review auto-approve
    _val_result: Any = None

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1 — SCRIPT
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_SCRIPT and to_step >= STEP_SCRIPT:
        # Auto-skip: if script.json already exists and has valid blocks, don't re-generate.
        # This prevents burning expensive Opus credits on repeated runs.
        if s_path.exists() and not dry_run:
            try:
                _existing = json.loads(s_path.read_text(encoding="utf-8"))
                if _existing.get("blocks"):
                    log.warning(
                        "script.json already exists with %d blocks — SKIPPING Step 1 to save credits. "
                        "Delete %s to force regeneration.",
                        len(_existing["blocks"]),
                        s_path,
                    )
                    _emit(progress_callback, type="step_start", step=STEP_SCRIPT, name=STEP_NAMES[STEP_SCRIPT], pct=STEP_WEIGHTS[STEP_SCRIPT][0])
                    _emit(progress_callback, type="step_done",  step=STEP_SCRIPT, elapsed=0.0,                        pct=STEP_WEIGHTS[STEP_SCRIPT][1])
                    from_step = STEP_MEDIA  # continue from step 2

                    # 01c catch-up: run Image Planner if image_prompts lists are missing.
                    # Happens when auto-skip fires on a fresh script that only has
                    # image_prompt (singular, 1 per block from step 01) but 01c never ran.
                    _needs_01c = any(
                        not _b.get("image_prompts")
                        for _b in _existing["blocks"]
                    )
                    if _needs_01c:
                        try:
                            _plan_images_catchup = _fn("modules/01c_image_planner.py", "plan_images")
                            _emit(progress_callback, type="sub_progress", step=STEP_SCRIPT,
                                  pct=17.0, message="Art Director: planning image positions (catch-up)…")
                            await _plan_images_catchup(
                                s_path,
                                _chan_cfg,
                                preset_name="high",
                                image_style=image_style or "",
                                progress_callback=progress_callback,
                            )
                            cost.add("Image Planner (Art Director catch-up)", round(17_000 * VOIDAI_PER_TOKEN, 5))
                            log.info("Image Planner catch-up: done")
                        except Exception as _iexc:
                            log.exception("Image Planner catch-up failed (non-fatal): %s", _iexc)

                    # Jump to step 2 — fall through to media step below
            except Exception:
                pass  # corrupt JSON → regenerate normally

    if from_step <= STEP_SCRIPT and to_step >= STEP_SCRIPT:
        _step_header(STEP_SCRIPT, STEP_NAMES[STEP_SCRIPT])
        _emit(progress_callback, type="step_start", step=STEP_SCRIPT, name=STEP_NAMES[STEP_SCRIPT], pct=STEP_WEIGHTS[STEP_SCRIPT][0])
        t0 = time.monotonic()

        if source_dir is None and not (custom_topic or "").strip():
            raise ValueError(
                "--source is required for step 1 unless --custom-topic is provided"
            )

        generate_scripts = _fn("modules/01_script_generator.py", "generate_scripts")

        MAX_SCRIPT_REGEN = 3
        _auto_regen_threshold = int(duration_min * TTS_WPM * 0.90)  # 90% of min target

        for _regen_attempt in range(1, MAX_SCRIPT_REGEN + 1):
            script_paths: list[Path] = await generate_scripts(
                source_dir,
                channel_config_path,
                template=template,
                preset=quality,
                dry_run=dry_run,
                output_dir=proj,
                duration_min=duration_min,
                duration_max=duration_max,
                master_prompt_path=master_prompt or None,
                image_style=image_style or "",
                custom_topic=custom_topic or "",
            )

            if dry_run:
                break

            if not script_paths:
                raise RuntimeError("generate_scripts returned an empty list")
            s_path = script_paths[0]
            _require_files([s_path], min_bytes=200, step="Script")

            _regen_sd = _load_script(s_path)
            _regen_blocks = _regen_sd.get("blocks", [])
            _regen_words = sum(len((b.get("narration") or "").split()) for b in _regen_blocks)

            if _regen_words >= _auto_regen_threshold:
                if _regen_attempt > 1:
                    log.info("Auto-regen attempt %d/%d succeeded: %d words (threshold: %d)",
                             _regen_attempt, MAX_SCRIPT_REGEN, _regen_words, _auto_regen_threshold)
                break

            if _regen_attempt < MAX_SCRIPT_REGEN:
                log.warning("Auto-regen %d/%d: script too short (%d words, need %d) — regenerating",
                            _regen_attempt, MAX_SCRIPT_REGEN, _regen_words, _auto_regen_threshold)
                _emit(progress_callback, type="sub_progress", step=STEP_SCRIPT,
                      pct=10.0, message=f"Script too short ({_regen_words}w) — auto-regen {_regen_attempt+1}/{MAX_SCRIPT_REGEN}…")
                s_path.unlink(missing_ok=True)
            else:
                log.warning("Auto-regen exhausted (%d attempts): %d words (need %d) — proceeding to review",
                            MAX_SCRIPT_REGEN, _regen_words, _auto_regen_threshold)
            log.info("Script saved: %s  (%.1fs)", s_path, time.monotonic() - t0)

            # ── Script validation + auto-fix ───────────────────────────────
            def _script_val_cb(ev: dict) -> None:
                _emit(progress_callback, **ev)
            try:
                _validate_script = _fn("modules/01b_script_validator.py", "validate_and_fix_script")
                _val_result = await _validate_script(s_path, _chan_cfg, progress_callback=_script_val_cb)
                if _val_result.issues:
                    log.info(
                        "Script validator: %d issues found, %d fixed",
                        len(_val_result.issues), len(_val_result.fixes_applied),
                    )
                # Track LLM costs from auto-fix
                if _val_result.fixes_applied:
                    _bad_prompt_fixes = sum(1 for f in _val_result.fixes_applied if "prompt" in f.lower())
                    if _bad_prompt_fixes:
                        cost.add("Script validator (prompts)", _bad_prompt_fixes * round(5_000 * VOIDAI_PER_TOKEN, 6))
                    if any("cont" in f.lower() for f in _val_result.fixes_applied):
                        cost.add("Script validator (cut-off)", round(10_000 * VOIDAI_PER_TOKEN, 6))
            except Exception as _vexc:
                log.exception("Script validation skipped (non-fatal): %s", _vexc)

            # VoidAI subscription: ~20K input + 8K output tokens (flat rate regardless of model)
            cost.add("Script LLM", round(28_000 * VOIDAI_PER_TOKEN, 5))
            cost.check(progress_callback)

            # ── Image Planner (Art Director pass — Step 1c) ─────────────────
            # Always runs: positions are calculated algorithmically (2-tier density
            # model), then Art Director LLM writes one structured prompt per position.
            # Does NOT rely on __MARKER__ sentinels — those are legacy and ignored.
            try:
                _plan_images = _fn("modules/01c_image_planner.py", "plan_images")
                _emit(progress_callback, type="sub_progress", step=STEP_SCRIPT,
                      pct=17.0, message="Art Director: planning image positions…")
                await _plan_images(
                    s_path,
                    _chan_cfg,
                    preset_name="high",          # Sonnet — sufficient for visual creativity
                    image_style=image_style or "",
                    progress_callback=progress_callback,
                )
                # VoidAI subscription: ~7K input + 10K output tokens (flat rate)
                cost.add("Image Planner (Art Director)", round(17_000 * VOIDAI_PER_TOKEN, 5))
                log.info("Image Planner: done")
            except Exception as _iexc:
                log.exception("Image Planner failed (non-fatal, continuing): %s", _iexc)

        else:
            log.info("[DRY RUN] Script step complete (no file written)")
        _emit(progress_callback, type="step_done", step=STEP_SCRIPT, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_SCRIPT][1])

    # ── Script quality gate ─────────────────────────────────────────────
    _quality_gate_failed = False
    if not dry_run and from_step <= STEP_SCRIPT:
        _sd_gate = _load_script(s_path)
        _gate_blocks = _sd_gate.get("blocks", [])
        _gate_words = sum(len((b.get("narration") or "").split()) for b in _gate_blocks)
        _gate_threshold = int(duration_min * 130 * 0.8)  # 80% of min target at slowest rate
        if _gate_words < _gate_threshold:
            _quality_gate_failed = True
            log.warning(
                "Script quality gate WARNING: %d words (need %d for %d-min video) — "
                "will show in review for regen decision",
                _gate_words, _gate_threshold, duration_min,
            )
        else:
            log.info("Script quality gate OK: %d words (threshold: %d)", _gate_words, _gate_threshold)

    # ── Record script metrics for A/B analysis ─────────────────────────────
    if not dry_run and db_tracker and db_video_id:
        try:
            _sd_metrics = _load_script(s_path)
            _m_blocks = _sd_metrics.get("blocks", [])
            _m_words = sum(len((b.get("narration") or "").split()) for b in _m_blocks)
            _m_hook = 0
            _intro = [b for b in _m_blocks if b.get("type") == "intro"]
            if _intro:
                _hm = _intro[0].get("hook")
                if isinstance(_hm, dict):
                    _m_hook = _hm.get("validation_score", 0) or 0
            _chan_cfg_m = load_channel_config(channel_config_path)
            _prompt_ver = Path(_chan_cfg_m.get("master_prompt_path", "")).stem
            _script_model = get_llm_preset(_chan_cfg_m, quality).get("script", "unknown")
            db_tracker.record_script_metrics(
                video_id=db_video_id,
                model=_script_model,
                template=template,
                prompt_version=_prompt_ver,
                temperature=0.7,
                word_count=_m_words,
                block_count=len(_m_blocks),
                hook_score=_m_hook,
                duration_est_min=round(_m_words / 170, 1),
            )
        except Exception as _me:
            log.warning("Script metrics recording failed (non-fatal): %s", _me)

    # Review pause — CLI (--review flag) or WebSocket (review_callback)
    if not dry_run and from_step <= STEP_SCRIPT:
        if review:
            _review_pause(s_path)
        elif review_callback is not None:
            _sd = _load_script(s_path)
            _blocks = _sd.get("blocks", [])

            # ── Compute rich review stats ──────────────────────────────────
            # Word count + duration (audio_duration is null before TTS)
            _word_count = sum(len((b.get("narration") or "").split()) for b in _blocks)
            _dur_min = round(_word_count / 150, 1)   # ~150 wpm reading
            _dur_max = round(_word_count / 130, 1)   # ~130 wpm slow reading

            # Block type breakdown
            _type_counts: dict[str, int] = {}
            for _b in _blocks:
                _t = _b.get("type", "section")
                _type_counts[_t] = _type_counts.get(_t, 0) + 1

            # Total image prompts (sum image_prompts lists, fallback image_prompt)
            _total_imgs = sum(
                len(_b.get("image_prompts") or []) or (1 if (_b.get("image_prompt") or "").strip() else 0)
                for _b in _blocks
            )

            # Hook detection: prefer hook.validation_score from script (set by LLM validator).
            # Falls back to keyword heuristic only if hook metadata is absent (old scripts).
            _intro_blocks = [_b for _b in _blocks if _b.get("type") == "intro"]
            _has_hook = False
            if _intro_blocks:
                _intro = _intro_blocks[0]
                _hook_meta = _intro.get("hook")
                if isinstance(_hook_meta, dict):
                    # LLM-validated: score ≥ 3/4 = passed; score absent = just generated (trust it)
                    _score = _hook_meta.get("validation_score")
                    _has_hook = (_score is None or _score >= 3)
                else:
                    # Fallback: simple keyword heuristic (older script format)
                    _intro_text = (_intro.get("narration") or "").strip()
                    _hook_signals = [
                        "?", "Що якби", "Уявіть", "Як", "Чому",
                        "Imagine", "What if", "Why", "You've", "Most ",
                        "Nobody", "Everyone", "Here's", "The truth",
                    ]
                    _has_hook = any(sig in _intro_text[:200] for sig in _hook_signals)

            # Per-block summary (compact — title + type + word count + image count)
            _block_summaries = [
                {
                    "id":          _b.get("id", ""),
                    "type":        _b.get("type", "section"),
                    "title":       _b.get("title", ""),
                    "word_count":      len((_b.get("narration") or "").split()),
                    "image_count":     len(_b.get("image_prompts") or []) or (1 if (_b.get("image_prompt") or "").strip() else 0),
                    "narration":       (_b.get("narration") or "")[:120],
                    "est_duration_sec": round(len((_b.get("narration") or "").split()) / 170 * 60, 1),
                }
                for _b in _blocks
            ]

            _review_data = {
                "script_path":   str(s_path),
                "title":         _sd.get("title", ""),
                "block_count":   len(_blocks),
                "word_count":    _word_count,
                "duration_min":  _dur_min,
                "duration_max":  _dur_max,
                "type_counts":   _type_counts,
                "image_prompt_count": _total_imgs,
                "has_hook":      _has_hook,
                "quality_gate_failed": _quality_gate_failed if '_quality_gate_failed' in dir() else False,
                "blocks":        _block_summaries,
                # Regen params — used by /jobs/{id}/regen-script
                "_regen": {
                    "channel_config_path": str(channel_config_path),
                    "source_dir":    str(source_dir) if source_dir else "",
                    "output_dir":    str(proj),
                    "quality":       quality,
                    "template":      template,
                    "duration_min":  duration_min,
                    "duration_max":  duration_max,
                    "master_prompt": master_prompt or "",
                    "image_style":   image_style or "",
                    "custom_topic":  custom_topic or "",
                },
            }

            # Auto-approve: skip review if all quality criteria met
            _script_auto_ok = False
            if auto_approve:
                _min_words = int(duration_min * 130 * 0.9)   # slowest rate − 10% tolerance
                _max_words = int(duration_max * 170 * 1.1)   # fastest rate + 10% tolerance
                _has_critical = getattr(_val_result, "has_critical", False) if _val_result else False
                _script_auto_ok = (
                    _has_hook
                    and _word_count >= _min_words
                    and _word_count <= _max_words
                    and _total_imgs >= len(_blocks)
                    and not _has_critical
                    and not _quality_gate_failed
                )
                if _script_auto_ok:
                    log.warning(
                        "Auto-approve script OK: hook=%s, words=%d (%d-%d), imgs=%d, blocks=%d",
                        _has_hook, _word_count, _min_words, _max_words, _total_imgs, len(_blocks),
                    )
                    _emit(progress_callback, type="auto_approved", stage="script")
                else:
                    log.warning(
                        "Auto-approve FAILED for script (hook=%s, words=%d, range=%d-%d, imgs=%d, critical=%s)"
                        " — falling back to manual review",
                        _has_hook, _word_count, _min_words, _max_words, _total_imgs, _has_critical,
                    )

            if not _script_auto_ok:
                await review_callback("script", _review_data)

    # ── Mark script review as passed in metrics ─────────────────────────────
    if not dry_run and db_tracker and db_video_id:
        try:
            db_tracker.update_script_review(db_video_id, passed=True)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 — IMAGES + VOICES (parallel)
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_MEDIA and to_step >= STEP_MEDIA:
        _step_header(STEP_MEDIA, STEP_NAMES[STEP_MEDIA])
        _emit(progress_callback, type="step_start", step=STEP_MEDIA, name=STEP_NAMES[STEP_MEDIA], pct=STEP_WEIGHTS[STEP_MEDIA][0])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            # No real script — provide estimated costs for a typical 10-block video
            log.info("[DRY RUN] No script.json (step 1 was also dry-run); using typical 10-block estimate")
            n_langs = len(langs) if langs else 1
            cost.add("Images estimate (10 blocks, WaveSpeed)", 10 * 0.005)
            cost.add("Voice estimate (5000 chars/lang)", n_langs * 5000 * 0.0000038)
        else:
            generate_images = _fn("modules/02_image_generator.py", "generate_images")
            generate_voices = _fn("modules/03_voice_generator.py", "generate_voices")

            # Primary language for voices
            primary_lang = langs[0] if langs else None

            # ── Sub-progress for Images + Voices (step 2, global 15-55%) ──
            # Images track local 0-100%, voices track local 0-100%.
            # Combined bar = avg(img_pct, voice_pct) mapped to global range.
            _m_start, _m_end = STEP_WEIGHTS[STEP_MEDIA]
            _media_local: dict[str, float] = {"img": 0.0, "voice": 0.0}

            def _emit_media_pct(msg: str = "") -> None:
                combined = (_media_local["img"] + _media_local["voice"]) / 2.0
                global_pct = _m_start + (combined / 100.0) * (_m_end - _m_start)
                _emit(
                    progress_callback,
                    type="sub_progress",
                    step=STEP_MEDIA,
                    pct=round(global_pct, 1),
                    message=msg,
                )

            def _img_sub_cb(event: dict) -> None:
                if event.get("type") == "sub_progress":
                    _media_local["img"] = float(event.get("pct", 0.0))
                    _emit_media_pct(event.get("message", ""))

            def _voice_sub_cb(event: dict) -> None:
                if event.get("type") == "sub_progress":
                    _media_local["voice"] = float(event.get("pct", 0.0))
                    _emit_media_pct(event.get("message", ""))

            # Images + primary voice in parallel
            img_task = generate_images(
                s_path,
                channel_config_path,
                dry_run=dry_run,
                skip_existing=True,
                image_style=image_style or None,
                validate=False,   # Inline validation disabled — 02b handles all validation/regen
                image_backend=image_backend or None,
                progress_callback=_img_sub_cb,
            )
            voice_task = generate_voices(
                s_path,
                channel_config_path,
                lang=primary_lang,
                voice_id_override=voice_id or None,
                dry_run=dry_run,
                skip_existing=True,
                progress_callback=_voice_sub_cb,
            )

            img_summary, voice_summary = await asyncio.gather(img_task, voice_task)

            # ── Image validation + auto-regen ──────────────────────────────
            _img_val_data: dict = {}
            try:
                _validate_images = _fn("modules/02b_image_validator.py", "validate_and_fix_images")
                _images_dir = proj / "images"
                _img_threshold = float(_chan_cfg.get("image_validation_threshold", 7.0))
                _img_val = await _validate_images(
                    s_path, _images_dir, _chan_cfg,
                    threshold=_img_threshold,
                    vision_model=vision_model or None,
                    progress_callback=_img_sub_cb,
                )
                _img_val_data = _img_val.to_dict()
                # Add image URLs for frontend preview (/projects is a static mount)
                # Use relative path from projects root to include channel subfolder
                # e.g. proj = .../projects/1/VideoTitle → rel = "1/VideoTitle"
                try:
                    _rel_parts = proj.relative_to(ROOT / "projects").parts
                    _enc_name = "/".join(_url_quote(p, safe="") for p in _rel_parts)
                except ValueError:
                    _enc_name = _url_quote(proj.name, safe="")
                for _sc in _img_val_data.get("scores", []):
                    _idx = _sc.get("image_index", 0)
                    _fname = f"{_sc['block_id']}.png" if _idx == 0 else f"{_sc['block_id']}_{_idx}.png"
                    _sc["image_url"] = f"/projects/{_enc_name}/images/{_fname}"
                # Track scoring + regen costs
                # VoidAI vision: ~2K tokens per image scored (flat rate)
                cost.add("Image validator (scoring)", _img_val.total * round(2_000 * VOIDAI_PER_TOKEN, 6))
                if _img_val.regenerated > 0:
                    cost.add("Image validator (regen)", _img_val.regenerated * 0.005)
                log.info(
                    "Image validator: %d/%d OK, %d regen, %d failed",
                    _img_val.ok_count, _img_val.total,
                    _img_val.regenerated, _img_val.failed,
                )
            except Exception as _ivexc:
                log.exception("Image validation skipped (non-fatal): %s", _ivexc)

            # Review checkpoint after images
            _img_review_data = {
                "images_dir": str(proj / "images"),
                "script_path": str(s_path),
                "source_name": proj.name,
                "validation": _img_val_data,
            }

            _img_auto_ok = False
            if auto_approve and _img_val_data:
                _iv = _img_val_data
                _total = max(_iv.get("total", 1), 1)
                _ok = _iv.get("ok", _iv.get("ok_count", 0))  # to_dict() uses "ok"
                _ok_pct = (_ok + _iv.get("regenerated", 0)) / _total
                _fail_pct = _iv.get("failed", 0) / _total
                _img_auto_ok = (
                    _ok_pct >= 0.90          # at least 90% OK/regenerated
                    and _fail_pct <= 0.10    # at most 10% failed
                )
                if _img_auto_ok:
                    log.warning(
                        "Auto-approve images OK: %d/%d OK, %d regen, %d failed, %d skipped",
                        _ok, _iv.get("total", 0),
                        _iv.get("regenerated", 0), _iv.get("failed", 0), _iv.get("skipped", 0),
                    )
                    _emit(progress_callback, type="auto_approved", stage="images")
                else:
                    log.warning(
                        "Auto-approve FAILED for images (ok=%d, total=%d, failed=%d, skipped=%d)"
                        " — falling back to manual review",
                        _ok, _iv.get("total", 0),
                        _iv.get("failed", 0), _iv.get("skipped", 0),
                    )

            if not _img_auto_ok and review_callback is not None:
                await review_callback("images", _img_review_data)

            # Additional language voices (sequential to respect rate limits)
            if langs and len(langs) > 1:
                for lang_code in langs[1:]:
                    log.info("Generating voice for language: %s", lang_code)
                    await generate_voices(
                        s_path,
                        channel_config_path,
                        lang=lang_code,
                        dry_run=dry_run,
                        skip_existing=True,
                    )

        if not dry_run:
            elapsed = time.monotonic() - t0

            # Validate audio files exist — retry missing ones up to 2 times
            script_data = _load_script(s_path)
            blocks = script_data["blocks"]
            audio_subdir = "audio"
            audio_dir = proj / audio_subdir
            voiced_blocks = [b for b in blocks if (b.get("narration") or "").strip()]
            audio_files = [audio_dir / f"{b['id']}.mp3" for b in voiced_blocks]

            _missing = [p for p in audio_files if not p.exists() or p.stat().st_size < 500]
            if _missing and not dry_run:
                _max_voice_retries = 2
                for _retry_i in range(1, _max_voice_retries + 1):
                    log.warning(
                        "Audio retry %d/%d — %d missing block(s): %s",
                        _retry_i, _max_voice_retries,
                        len(_missing), [p.stem for p in _missing],
                    )
                    _emit(
                        progress_callback, type="sub_progress",
                        step=STEP_MEDIA, pct=85.0,
                        message=f"Retrying {len(_missing)} failed audio block(s) ({_retry_i}/{_max_voice_retries})…",
                    )
                    # Re-run voice generation (skip_existing=True will only redo missing)
                    voice_summary = await generate_voices(
                        s_path, channel_config_path,
                        lang=primary_lang,
                        voice_id_override=voice_id or None,
                        dry_run=False,
                        skip_existing=True,
                        progress_callback=_voice_sub_cb,
                    )
                    _missing = [p for p in audio_files if not p.exists() or p.stat().st_size < 500]
                    if not _missing:
                        log.info("Audio retry %d: all blocks recovered ✓", _retry_i)
                        break

            _require_files(audio_files, min_bytes=500, step="Media/Audio")

            n_gen = getattr(img_summary, "generated", 0)
            n_chars = getattr(voice_summary, "total_chars", 0)
            log.info(
                "Media done (%.1fs) | Images generated: %d | Voice chars: %d",
                elapsed, n_gen, n_chars,
            )

            # Use actual costs from summaries where available
            ws_cost = getattr(img_summary, "wavespeed_cost", n_gen * 0.005)
            va_cost = getattr(img_summary, "voidai_cost", 0.0)
            cost.add("Images (WaveSpeed)", ws_cost)
            if va_cost > 0:
                cost.add("Images (VoidAI fallback)", va_cost)
            # Voice: VoiceAPI rate = $19 / 5,000,000 chars = $0.0000038/char
            cost.add("Voice (VoiceAPI)", n_chars * 0.0000038)
            if langs and len(langs) > 1:
                for _ in langs[1:]:
                    cost.add(f"Voice extra lang", n_chars * 0.0000038)

            cost.check(progress_callback)
        _emit(progress_callback, type="step_done", step=STEP_MEDIA, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_MEDIA][1])

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3 — SUBTITLES
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_SUBTITLES and to_step >= STEP_SUBTITLES:
        _step_header(STEP_SUBTITLES, STEP_NAMES[STEP_SUBTITLES])
        _emit(progress_callback, type="step_start", step=STEP_SUBTITLES, name=STEP_NAMES[STEP_SUBTITLES], pct=STEP_WEIGHTS[STEP_SUBTITLES][0])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            log.info("[DRY RUN] Subtitles: no script.json — step skipped (no API cost)")
        else:
            generate_subtitles = _fn("modules/04_subtitle_generator.py", "generate_subtitles")

            # Always use block audio_duration timing (TTS-accurate).
            # from_transcript (original video's transcript.srt) is intentionally NOT used here:
            # it contains the source video's text & timing, not the new AI-generated narration,
            # so it would show wrong text at wrong timestamps.
            # Primary language subtitles
            srt_path, ass_path = generate_subtitles(
                s_path,
                channel_config_path,
            )

            # Additional language subtitles
            if langs and len(langs) > 1:
                for lang_code in langs[1:]:
                    log.info("Generating subtitles for language: %s", lang_code)
                    generate_subtitles(s_path, channel_config_path, lang=lang_code)

            if not dry_run:
                _require_files([srt_path, ass_path], min_bytes=10, step="Subtitles")
                log.info(
                    "Subtitles: %s, %s  (%.1fs)",
                    srt_path.name, ass_path.name, time.monotonic() - t0,
                )
        _emit(progress_callback, type="step_done", step=STEP_SUBTITLES, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_SUBTITLES][1])

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4 — VIDEO
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_VIDEO and to_step >= STEP_VIDEO:
        _step_header(STEP_VIDEO, STEP_NAMES[STEP_VIDEO])
        _emit(progress_callback, type="step_start", step=STEP_VIDEO, name=STEP_NAMES[STEP_VIDEO], pct=STEP_WEIGHTS[STEP_VIDEO][0])
        t0 = time.monotonic()

        # compile_video needs full_narration.mp3 even in dry_run
        audio_dir_path = proj / "audio"
        narration_candidates = list(audio_dir_path.glob("full_narration*.mp3")) if audio_dir_path.exists() else []
        if dry_run and not narration_candidates:
            log.info("[DRY RUN] Video: no full_narration.mp3 yet — step skipped (FFmpeg, no API cost)")
        else:
            compile_video = _fn("modules/05_video_compiler.py", "compile_video")

            # Sub-progress: map local 0–100 from compile_video → global pct range for step 4
            _vid_start, _vid_end = STEP_WEIGHTS[STEP_VIDEO]
            _loop = asyncio.get_event_loop()

            def _video_sub_cb(event: dict) -> None:
                if event.get("type") == "sub_progress" and "pct" in event:
                    local_pct = float(event["pct"])
                    global_pct = _vid_start + (local_pct / 100.0) * (_vid_end - _vid_start)
                    new_ev = {
                        "type":    "sub_progress",
                        "pct":     round(global_pct, 1),
                        "step":    STEP_VIDEO,
                        "message": event.get("message", ""),
                    }
                    # Thread-safe: schedule emit back on the event loop
                    _loop.call_soon_threadsafe(lambda e=new_ev: _emit(progress_callback, **e))

            # compile_video is synchronous — run in executor to avoid blocking the loop
            video_path: Path = await _loop.run_in_executor(
                None,
                lambda: compile_video(
                    s_path,
                    channel_config_path,
                    draft=draft,
                    dry_run=dry_run,
                    no_subs=not burn_subtitles,
                    no_music=not background_music,
                    no_ken_burns=no_ken_burns,
                    music_volume_override=music_volume,
                    music_track_override=music_track,
                    progress_callback=_video_sub_cb,
                ),
            )

            if not dry_run:
                _require_files([video_path], min_bytes=50_000, step="Video")
                _video_path = video_path
                size_mb = video_path.stat().st_size / 1_048_576
                log.info(
                    "Video: %s  (%.1f MB, %.1fs)",
                    video_path.name, size_mb, time.monotonic() - t0,
                )
        _emit(progress_callback, type="step_done", step=STEP_VIDEO, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_VIDEO][1])

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5 — THUMBNAIL
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_THUMBNAIL and to_step >= STEP_THUMBNAIL:
        _step_header(STEP_THUMBNAIL, STEP_NAMES[STEP_THUMBNAIL])
        _emit(progress_callback, type="step_start", step=STEP_THUMBNAIL, name=STEP_NAMES[STEP_THUMBNAIL], pct=STEP_WEIGHTS[STEP_THUMBNAIL][0])
        t0 = time.monotonic()

        if skip_thumbnail:
            log.info("Thumbnail generation SKIPPED (skip_thumbnail=True)")
        elif dry_run and not s_path.exists():
            log.info("[DRY RUN] Thumbnail estimate: ~$0.010 (2 WaveSpeed attempts avg)")
            cost.add("Thumbnail estimate (WaveSpeed)", 2 * 0.005)
        else:
            generate_thumbnail_variants = _fn(
                "modules/06_thumbnail_generator.py", "generate_thumbnail_variants"
            )

            # Use Transcriber thumbnail_prompt.txt if available
            transcriber_dir: Path | None = None
            if source_dir and (source_dir / "thumbnail_prompt.txt").exists():
                transcriber_dir = source_dir

            # Generate 3 thumbnail variants for A/B testing
            thumb_results = await generate_thumbnail_variants(
                s_path,
                channel_config_path,
                count=3,
                transcriber_dir=transcriber_dir,
                dry_run=dry_run,
                preset=quality,
            )

            if not dry_run and thumb_results:
                # Best is already copied to thumbnail.png by generate_thumbnail_variants
                best = max(thumb_results, key=lambda r: getattr(r, "score", -1))
                thumb_path = getattr(best, "output_path", None)
                if thumb_path:
                    _require_files([Path(thumb_path)], min_bytes=1_000, step="Thumbnail")
                    _thumb_path = Path(thumb_path)
                    total_attempts = sum(getattr(r, "attempts", 1) for r in thumb_results)
                    best_score = getattr(best, "score", -1)
                    log.info(
                        "Thumbnails: %d variants | best score=%d | total_attempts=%d | %.1fs",
                        len(thumb_results), best_score, total_attempts, time.monotonic() - t0,
                    )
                    cost.add("Thumbnails (WaveSpeed ×3)", len(thumb_results) * 0.005)
                    cost.check(progress_callback)
        if not skip_thumbnail:
            _emit(progress_callback, type="step_done", step=STEP_THUMBNAIL, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_THUMBNAIL][1])
        else:
            _emit(progress_callback, type="step_done", step=STEP_THUMBNAIL, elapsed=0.0, pct=STEP_WEIGHTS[STEP_THUMBNAIL][1])

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6 — METADATA
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_METADATA and to_step >= STEP_METADATA:
        _step_header(STEP_METADATA, STEP_NAMES[STEP_METADATA])
        _emit(progress_callback, type="step_start", step=STEP_METADATA, name=STEP_NAMES[STEP_METADATA], pct=STEP_WEIGHTS[STEP_METADATA][0])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            log.info("[DRY RUN] Metadata estimate: ~$0.001 (VoidAI flat rate, ~5K tokens)")
            cost.add("Metadata LLM estimate", round(5_000 * VOIDAI_PER_TOKEN, 5))
        else:
            generate_metadata = _fn("modules/07_metadata_generator.py", "generate_metadata")

            meta_path: Path = await generate_metadata(
                s_path,
                channel_config_path,
                preset=quality,
                title_count=3,   # Generate 3 title variants for A/B testing
                dry_run=dry_run,
            )

            if not dry_run:
                _require_files([meta_path], min_bytes=50, step="Metadata")
                log.info("Metadata: %s  (%.1fs)", meta_path.name, time.monotonic() - t0)
                cost.add("Metadata LLM", round(5_000 * VOIDAI_PER_TOKEN, 5))
                cost.check(progress_callback)
        _emit(progress_callback, type="step_done", step=STEP_METADATA, elapsed=time.monotonic() - t0, pct=STEP_WEIGHTS[STEP_METADATA][1])

    # ══════════════════════════════════════════════════════════════════════════
    # DONE
    # ══════════════════════════════════════════════════════════════════════════
    elapsed_total = time.monotonic() - t_pipeline

    # ── DB: mark done ─────────────────────────────────────────────────────────
    if db_tracker and _vid_id and not dry_run:
        db_tracker.set_done(
            _vid_id,
            video_path=_video_path,
            thumbnail_path=_thumb_path,
            script_path=s_path if s_path.exists() else None,
            elapsed_seconds=elapsed_total,
        )
        for label, amount in cost.breakdown:
            db_tracker.record_cost(
                _vid_id, step=label, model="pipeline", cost_usd=amount,
            )
        log.info("DB tracking : done (video_id=%d, elapsed=%.1fs)", _vid_id, elapsed_total)

    _emit(progress_callback, type="done", elapsed=elapsed_total, dry_run=dry_run)

    # ── Balance snapshot: END + cost report ───────────────────────────────────
    if not dry_run and _balances_start:
        try:
            _balances_end = await _fetch_balances()
            va = _balances_end.get("voiceapi_chars")
            vi = _balances_end.get("voidai_credits")
            log.info(
                "[BALANCE END] VoiceAPI: %s chars | VoidAI: %s credits",
                f"{va:,}" if va is not None else "N/A",
                f"{vi:,}" if vi is not None else "N/A",
            )
            _write_cost_report(proj, _balances_start, _balances_end, elapsed_total)
        except Exception as _be:
            log.debug("Balance snapshot (end) failed: %s", _be)

    print()
    print("=" * 60)
    if dry_run:
        print(f"  DRY RUN COMPLETE ({elapsed_total:.1f}s)")
    else:
        print(f"  PIPELINE COMPLETE ({elapsed_total:.1f}s)")
    print("=" * 60)

    if not dry_run:
        print(f"  Project : {proj}")
        output_dir = proj / "output"
        if output_dir.exists():
            for f in sorted(output_dir.iterdir()):
                size = f.stat().st_size
                print(f"  Output  : {f.name}  ({size:,} B)")

    print()
    if cost.breakdown:
        print(cost.summary())

    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="VideoForge Pipeline — end-to-end YouTube video generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input
    io_group = parser.add_argument_group("Input / Output")
    io_group.add_argument(
        "--source", metavar="DIR",
        help="Transcriber output directory (required for step 1)",
    )
    io_group.add_argument(
        "--channel", metavar="JSON", required=True,
        help="Channel config JSON (e.g. config/channels/history.json)",
    )
    io_group.add_argument(
        "--project-dir", metavar="DIR",
        help="Explicit project directory (default: projects/{source.name}/)",
    )
    io_group.add_argument(
        "--script", metavar="JSON",
        help="Existing script.json path (alternative to --source for step 2+)",
    )

    # Quality / style
    quality_group = parser.add_argument_group("Quality / Style")
    quality_group.add_argument(
        "--quality", metavar="PRESET",
        choices=["max", "high", "balanced", "bulk", "test"],
        default="max",
        help="LLM quality preset (default: max)",
    )
    quality_group.add_argument(
        "--template", metavar="TYPE",
        choices=["auto", "documentary", "listicle", "tutorial", "comparison"],
        default="auto",
        help="Content template (default: auto)",
    )
    quality_group.add_argument(
        "--lang", metavar="CODES",
        help="Comma-separated language codes for multilingual output (e.g. en,de,es)",
    )
    quality_group.add_argument(
        "--duration-min", type=int, metavar="MIN", default=None, dest="duration_min",
        help="Minimum target video duration in minutes (default: 8)",
    )
    quality_group.add_argument(
        "--duration-max", type=int, metavar="MAX", default=None, dest="duration_max",
        help="Maximum target video duration in minutes (default: 12)",
    )
    quality_group.add_argument(
        "--duration", type=int, metavar="MIN",
        help="Legacy: set both --duration-min and --duration-max to the same value",
    )

    # Mode flags
    mode_group = parser.add_argument_group("Execution Mode")
    mode_group.add_argument(
        "--review", action="store_true",
        help="Pause after script generation and ask for approval",
    )
    mode_group.add_argument(
        "--auto", action="store_true",
        help="Non-interactive mode — no pauses (default behavior)",
    )
    mode_group.add_argument(
        "--dry-run", action="store_true",
        help="Estimate costs without making any API calls",
    )
    mode_group.add_argument(
        "--draft", action="store_true",
        help="Generate 480p preview video without effects (fast)",
    )
    mode_group.add_argument(
        "--from-step", type=int, metavar="N", default=1,
        choices=range(1, TOTAL_STEPS + 1),
        help=(
            "Resume from step N — assumes earlier steps are cached. "
            "1=Script, 2=Media, 3=Subs, 4=Video, 5=Thumb, 6=Meta"
        ),
    )
    mode_group.add_argument(
        "--to-step", type=int, metavar="N", default=TOTAL_STEPS,
        choices=range(1, TOTAL_STEPS + 1),
        help=(
            "Stop after step N (inclusive). Combine with --from-step to run exactly one step. "
            "E.g. --from-step 2 --to-step 2 runs only Images+Voice."
        ),
    )
    mode_group.add_argument(
        "--budget", type=float, metavar="USD",
        help="Maximum spend per video in USD (pipeline halts if exceeded)",
    )

    # Tracking
    track_group = parser.add_argument_group("Tracking")
    track_group.add_argument(
        "--track", action="store_true",
        help="Record this run in the SQLite tracker (data/videoforge.db)",
    )
    track_group.add_argument(
        "--db", metavar="PATH",
        help="Custom DB path for tracking (default: data/videoforge.db)",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # ── Validate argument combinations ────────────────────────────────────────
    if args.from_step == 1 and not args.source:
        parser.error("--source is required for step 1 (script generation)")

    if args.from_step >= 2 and not args.source and not args.script and not args.project_dir:
        parser.error(
            "--source, --script, or --project-dir is required when --from-step >= 2"
        )

    # ── Resolve paths ──────────────────────────────────────────────────────────
    source_dir: Path | None = None
    if args.source:
        source_dir = Path(args.source)
        if not source_dir.is_dir():
            parser.error(f"--source directory not found: {source_dir}")

    channel_path = Path(args.channel)
    if not channel_path.exists():
        parser.error(f"--channel file not found: {channel_path}")

    project_dir: Path | None = Path(args.project_dir) if args.project_dir else None
    script_path_override: Path | None = Path(args.script) if args.script else None

    if script_path_override and not script_path_override.exists():
        parser.error(f"--script file not found: {script_path_override}")

    # ── Parse languages ────────────────────────────────────────────────────────
    langs: list[str] | None = None
    if args.lang:
        langs = [code.strip() for code in args.lang.split(",") if code.strip()]
        if not langs:
            parser.error("--lang produced an empty language list")

    # ── Resolve duration range ─────────────────────────────────────────────────
    if args.duration is not None:
        # Legacy --duration sets both min and max to same value
        duration_min_cli = args.duration
        duration_max_cli = args.duration
    else:
        duration_min_cli = args.duration_min if args.duration_min is not None else 8
        duration_max_cli = args.duration_max if args.duration_max is not None else 12
    if duration_min_cli > duration_max_cli:
        parser.error(f"--duration-min ({duration_min_cli}) must be <= --duration-max ({duration_max_cli})")

    load_env()

    # ── Optional SQLite tracking ───────────────────────────────────────────────
    db_tracker: Any = None
    db_video_id: int | None = None
    if args.track and not args.dry_run:
        from utils.db import VideoTracker  # lazy import
        db_tracker = VideoTracker(db_path=args.db)
        db_video_id = db_tracker.create_video(
            source_dir=source_dir or Path(args.project_dir or "."),
            channel=channel_path.stem,
            quality_preset=args.quality,
            template=args.template,
            from_step=args.from_step,
            project_dir=project_dir,
        )
        log.info("DB tracking enabled: video_id=%d  db=%s", db_video_id, db_tracker.db_path)

    import time as _time
    _t0 = _time.monotonic()
    try:
        asyncio.run(
            run_pipeline(
                source_dir=source_dir,
                channel_config_path=channel_path,
                quality=args.quality,
                template=args.template,
                review=args.review,
                dry_run=args.dry_run,
                draft=args.draft,
                from_step=args.from_step,
                to_step=args.to_step,
                langs=langs,
                budget=args.budget,
                project_dir=project_dir,
                script_path_override=script_path_override,
                db_tracker=db_tracker,
                db_video_id=db_video_id,
                duration_min=duration_min_cli,
                duration_max=duration_max_cli,
            )
        )
    except Exception as exc:
        if db_tracker and db_video_id is not None:
            db_tracker.set_failed(
                db_video_id, str(exc),
                elapsed_seconds=_time.monotonic() - _t0,
            )
        raise


if __name__ == "__main__":
    main()
