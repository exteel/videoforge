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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 stdout so argparse --help and print() work on Windows (cp1252 default)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from modules.common import load_env, setup_logging
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

# ─── Module loading ────────────────────────────────────────────────────────────

_module_cache: dict[str, Any] = {}


def _load_module(rel_path: str) -> Any:
    """Import a module by relative path (handles numeric-prefix filenames)."""
    if rel_path in _module_cache:
        return _module_cache[rel_path]
    full = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(full.stem, str(full))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module: {full}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _module_cache[rel_path] = mod
    return mod


def _fn(rel_path: str, fn_name: str) -> Any:
    """Get a function from a module by relative path."""
    return getattr(_load_module(rel_path), fn_name)


# ─── Cost budget tracker ───────────────────────────────────────────────────────

@dataclass
class CostBudget:
    limit: float | None = None
    spent: float = 0.0
    breakdown: list[tuple[str, float]] = field(default_factory=list)

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
    langs: list[str] | None = None,
    budget: float | None = None,
    project_dir: Path | None = None,
    script_path_override: Path | None = None,
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
    cost = CostBudget(limit=budget)
    t_pipeline = time.monotonic()

    # ── Resolve project directory ──────────────────────────────────────────────
    if project_dir:
        proj = project_dir
    elif source_dir:
        proj = ROOT / "projects" / source_dir.name
    else:
        raise ValueError("Either source_dir or project_dir must be provided")

    proj.mkdir(parents=True, exist_ok=True)

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
    if langs:
        log.info("Languages   : %s", ", ".join(langs))
    if budget is not None:
        log.info("Budget      : $%.2f", budget)
    if dry_run:
        log.info("Mode        : DRY RUN (no API calls)")
    elif draft:
        log.info("Mode        : DRAFT (480p, no effects)")

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

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1 — SCRIPT
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_SCRIPT:
        _step_header(STEP_SCRIPT, STEP_NAMES[STEP_SCRIPT])
        t0 = time.monotonic()

        if source_dir is None:
            raise ValueError("--source is required for step 1 (script generation)")

        generate_scripts = _fn("modules/01_script_generator.py", "generate_scripts")
        script_paths: list[Path] = await generate_scripts(
            source_dir,
            channel_config_path,
            template=template,
            preset=quality,
            dry_run=dry_run,
            output_dir=proj,
        )

        if not dry_run:
            if not script_paths:
                raise RuntimeError("generate_scripts returned an empty list")
            s_path = script_paths[0]
            _require_files([s_path], min_bytes=200, step="Script")
            log.info("Script saved: %s  (%.1fs)", s_path, time.monotonic() - t0)

            # Rough cost: ~3000 input + 1500 output tokens; max preset = Opus pricing
            cost.add("Script LLM", 0.035)
            if cost.over_budget():
                log.error("Budget exceeded after Script! %s", cost.summary())
                sys.exit(1)
        else:
            log.info("[DRY RUN] Script step complete (no file written)")

    # Review pause (only when not dry-run and not skipping step 1)
    if review and not dry_run and from_step <= STEP_SCRIPT:
        _review_pause(s_path)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 — IMAGES + VOICES (parallel)
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_MEDIA:
        _step_header(STEP_MEDIA, STEP_NAMES[STEP_MEDIA])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            # No real script — provide estimated costs for a typical 10-block video
            log.info("[DRY RUN] No script.json (step 1 was also dry-run); using typical 10-block estimate")
            n_langs = len(langs) if langs else 1
            cost.add("Images estimate (10 blocks, WaveSpeed)", 10 * 0.005)
            cost.add("Voice estimate (5000 chars/lang)", n_langs * 5000 * 0.0002)
        else:
            generate_images = _fn("modules/02_image_generator.py", "generate_images")
            generate_voices = _fn("modules/03_voice_generator.py", "generate_voices")

            # Primary language for voices
            primary_lang = langs[0] if langs else None

            # Images + primary voice in parallel
            img_task = generate_images(
                s_path,
                channel_config_path,
                dry_run=dry_run,
                skip_existing=True,
            )
            voice_task = generate_voices(
                s_path,
                channel_config_path,
                lang=primary_lang,
                dry_run=dry_run,
                skip_existing=True,
            )

            img_summary, voice_summary = await asyncio.gather(img_task, voice_task)

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

            # Validate audio files exist
            script_data = _load_script(s_path)
            blocks = script_data["blocks"]
            audio_subdir = "audio"
            audio_dir = proj / audio_subdir
            voiced_blocks = [b for b in blocks if (b.get("narration") or "").strip()]
            audio_files = [audio_dir / f"{b['id']}.mp3" for b in voiced_blocks]
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
            # Voice: rough ~$0.0002/char (ElevenLabs tier)
            cost.add("Voice (VoiceAPI)", n_chars * 0.0002)
            if langs and len(langs) > 1:
                for _ in langs[1:]:
                    cost.add(f"Voice extra lang", n_chars * 0.0002)

            if cost.over_budget():
                log.error("Budget exceeded after Media! %s", cost.summary())
                sys.exit(1)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3 — SUBTITLES
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_SUBTITLES:
        _step_header(STEP_SUBTITLES, STEP_NAMES[STEP_SUBTITLES])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            log.info("[DRY RUN] Subtitles: no script.json — step skipped (no API cost)")
        else:
            generate_subtitles = _fn("modules/04_subtitle_generator.py", "generate_subtitles")

            # Transcriber SRT for word-level timing (optional)
            transcript_srt: Path | None = None
            if source_dir:
                candidate = source_dir / "transcript.srt"
                if candidate.exists():
                    transcript_srt = candidate
                    log.info("Using Transcriber SRT for word-level timing: %s", candidate.name)

            # Primary language subtitles
            srt_path, ass_path = generate_subtitles(
                s_path,
                channel_config_path,
                from_transcript=transcript_srt,
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

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4 — VIDEO
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_VIDEO:
        _step_header(STEP_VIDEO, STEP_NAMES[STEP_VIDEO])
        t0 = time.monotonic()

        # compile_video needs full_narration.mp3 even in dry_run
        audio_dir_path = proj / "audio"
        narration_candidates = list(audio_dir_path.glob("full_narration*.mp3")) if audio_dir_path.exists() else []
        if dry_run and not narration_candidates:
            log.info("[DRY RUN] Video: no full_narration.mp3 yet — step skipped (FFmpeg, no API cost)")
        else:
            compile_video = _fn("modules/05_video_compiler.py", "compile_video")

            # compile_video is synchronous — run in executor to avoid blocking the loop
            loop = asyncio.get_event_loop()
            video_path: Path = await loop.run_in_executor(
                None,
                lambda: compile_video(
                    s_path,
                    channel_config_path,
                    draft=draft,
                    dry_run=dry_run,
                ),
            )

            if not dry_run:
                _require_files([video_path], min_bytes=50_000, step="Video")
                size_mb = video_path.stat().st_size / 1_048_576
                log.info(
                    "Video: %s  (%.1f MB, %.1fs)",
                    video_path.name, size_mb, time.monotonic() - t0,
                )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5 — THUMBNAIL
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_THUMBNAIL:
        _step_header(STEP_THUMBNAIL, STEP_NAMES[STEP_THUMBNAIL])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            log.info("[DRY RUN] Thumbnail estimate: ~$0.010 (2 WaveSpeed attempts avg)")
            cost.add("Thumbnail estimate (WaveSpeed)", 2 * 0.005)
        else:
            generate_thumbnail = _fn("modules/06_thumbnail_generator.py", "generate_thumbnail")

            # Use Transcriber thumbnail_prompt.txt if available
            transcriber_dir: Path | None = None
            if source_dir and (source_dir / "thumbnail_prompt.txt").exists():
                transcriber_dir = source_dir

            thumb_result = await generate_thumbnail(
                s_path,
                channel_config_path,
                transcriber_dir=transcriber_dir,
                dry_run=dry_run,
                preset=quality,
            )

            if not dry_run:
                thumb_path = getattr(thumb_result, "output_path", None)
                if thumb_path:
                    _require_files([Path(thumb_path)], min_bytes=1_000, step="Thumbnail")
                    attempts = getattr(thumb_result, "attempts", 1)
                    score = getattr(thumb_result, "score", -1)
                    log.info(
                        "Thumbnail: %s  (score=%d, attempts=%d, %.1fs)",
                        Path(thumb_path).name, score, attempts, time.monotonic() - t0,
                    )
                    # ~$0.005/image × avg attempts
                    cost.add("Thumbnail (WaveSpeed)", attempts * 0.005)
                    if cost.over_budget():
                        log.error("Budget exceeded after Thumbnail! %s", cost.summary())
                        sys.exit(1)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6 — METADATA
    # ══════════════════════════════════════════════════════════════════════════
    if from_step <= STEP_METADATA:
        _step_header(STEP_METADATA, STEP_NAMES[STEP_METADATA])
        t0 = time.monotonic()

        if dry_run and not s_path.exists():
            log.info("[DRY RUN] Metadata estimate: ~$0.003 (gpt-4.1-mini)")
            cost.add("Metadata LLM estimate", 0.003)
        else:
            generate_metadata = _fn("modules/07_metadata_generator.py", "generate_metadata")

            meta_path: Path = await generate_metadata(
                s_path,
                channel_config_path,
                preset=quality,
                dry_run=dry_run,
            )

            if not dry_run:
                _require_files([meta_path], min_bytes=50, step="Metadata")
                log.info("Metadata: %s  (%.1fs)", meta_path.name, time.monotonic() - t0)
                cost.add("Metadata LLM", 0.003)
                if cost.over_budget():
                    log.error("Budget exceeded after Metadata! %s", cost.summary())
                    sys.exit(1)

    # ══════════════════════════════════════════════════════════════════════════
    # DONE
    # ══════════════════════════════════════════════════════════════════════════
    elapsed_total = time.monotonic() - t_pipeline

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
        "--budget", type=float, metavar="USD",
        help="Maximum spend per video in USD (pipeline halts if exceeded)",
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

    load_env()

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
            langs=langs,
            budget=args.budget,
            project_dir=project_dir,
            script_path_override=script_path_override,
        )
    )


if __name__ == "__main__":
    main()
