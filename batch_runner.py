"""
VideoForge -- Batch Runner (Task #18).

Scans a Transcriber output directory and runs the pipeline for each video.
Supports parallel processing and total budget limits.

Usage:
    # Process all videos in a directory (sequential, bulk quality)
    python batch_runner.py \\
        --input-dir "D:/transscript batch/output/output" \\
        --channel config/channels/history.json

    # Parallel (2 at a time), balanced quality (skip-done is the default)
    python batch_runner.py \\
        --input-dir "D:/transscript batch/output/output" \\
        --channel config/channels/history.json \\
        --parallel 2 --quality balanced

    # Dry run: estimate total cost for all videos
    python batch_runner.py \\
        --input-dir "D:/transscript batch/output/output" \\
        --channel config/channels/history.json \\
        --dry-run

    # With per-video and total budget caps
    python batch_runner.py \\
        --input-dir ... --channel ... \\
        --budget-per-video 1.50 --budget-total 10.00
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from utils.db import VideoTracker

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from modules.common import load_env, setup_logging
from pipeline import run_pipeline
from utils.cost_tracker import estimate_cost

log = setup_logging("batch_runner")

# ─── Constants ────────────────────────────────────────────────────────────────

# Files that indicate a valid Transcriber output directory
TRANSCRIBER_MARKERS = frozenset(["transcript.txt", "metadata.json", "title.txt"])

# A video is "done" if final.mp4 exists in the project output dir
DONE_MARKER = "output/final.mp4"

# ─── Result tracking ──────────────────────────────────────────────────────────

@dataclass
class VideoResult:
    name: str
    status: str      # "done" | "skipped" | "failed"
    elapsed: float = 0.0
    error: str = ""
    estimated_cost: float = 0.0


@dataclass
class BatchSummary:
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed: float = 0.0
    results: list[VideoResult] = field(default_factory=list)
    total_estimated_cost: float = 0.0


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_transcriber_dir(path: Path) -> bool:
    """Return True if the directory looks like a Transcriber output dir."""
    if not path.is_dir():
        return False
    return any((path / marker).exists() for marker in TRANSCRIBER_MARKERS)


def _is_done(source_dir: Path, proj_root: Path) -> bool:
    """Return True if the video already has a compiled final.mp4."""
    proj = proj_root / source_dir.name
    return (proj / DONE_MARKER).exists()


def _scan_input_dir(input_dir: Path) -> list[Path]:
    """Return sorted list of valid Transcriber output subdirectories."""
    dirs = [d for d in sorted(input_dir.iterdir()) if _is_transcriber_dir(d)]
    log.info("Found %d Transcriber output directories in: %s", len(dirs), input_dir)
    return dirs


# ─── Single video processing ──────────────────────────────────────────────────

async def _process_one(
    source_dir: Path,
    channel_config_path: Path,
    *,
    quality: str,
    dry_run: bool,
    draft: bool,
    from_step: int,
    template: str,
    budget_per_video: float | None,
    sem: asyncio.Semaphore,
    db_tracker: Any | None = None,
) -> VideoResult:
    """Run pipeline for one video, respecting the concurrency semaphore."""
    name = source_dir.name
    async with sem:
        log.info("[BATCH] Starting: %s", name)
        t0 = time.monotonic()

        # Create DB record before pipeline starts so we can mark failures
        vid_id: int | None = None
        if db_tracker is not None and not dry_run:
            vid_id = db_tracker.create_video(
                source_dir=source_dir,
                channel=channel_config_path.stem,
                quality_preset=quality,
                template=template,
                from_step=from_step,
                project_dir=ROOT / "projects" / name,
            )

        try:
            await run_pipeline(
                source_dir=source_dir,
                channel_config_path=channel_config_path,
                quality=quality,
                template=template,
                dry_run=dry_run,
                draft=draft,
                from_step=from_step,
                budget=budget_per_video,
                db_tracker=db_tracker,
                db_video_id=vid_id,
            )
            elapsed = time.monotonic() - t0
            log.info("[BATCH] Done: %s  (%.1fs)", name, elapsed)
            return VideoResult(name=name, status="done", elapsed=elapsed)
        except SystemExit:
            # Pipeline called sys.exit(1) on budget exceeded
            elapsed = time.monotonic() - t0
            if db_tracker and vid_id is not None:
                db_tracker.set_failed(vid_id, "budget_exceeded", elapsed_seconds=elapsed)
            return VideoResult(
                name=name, status="failed", elapsed=elapsed,
                error="budget_exceeded",
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            log.error("[BATCH] Failed: %s — %s", name, exc)
            if db_tracker and vid_id is not None:
                db_tracker.set_failed(vid_id, str(exc), elapsed_seconds=elapsed)
            return VideoResult(name=name, status="failed", elapsed=elapsed, error=str(exc))


# ─── Main batch function ──────────────────────────────────────────────────────

async def run_batch(
    input_dir: Path,
    channel_config_path: Path,
    *,
    parallel: int = 1,
    quality: str = "bulk",
    dry_run: bool = False,
    draft: bool = False,
    skip_done: bool = True,
    from_step: int = 1,
    template: str = "auto",
    budget_per_video: float | None = None,
    budget_total: float | None = None,
    db_path: str | None = None,
) -> BatchSummary:
    """
    Run VideoForge pipeline for all Transcriber output directories.

    Args:
        input_dir: Root directory containing Transcriber output subdirs.
        channel_config_path: Path to channel config JSON.
        parallel: Max simultaneous pipeline runs (default: 1 = sequential).
        quality: LLM quality preset (default: bulk for batch).
        dry_run: Estimate costs without making API calls.
        draft: Generate 480p preview videos.
        skip_done: Skip videos that already have output/final.mp4.
        from_step: Start from this step for all videos.
        template: Content template override.
        budget_per_video: Stop each video if its cost exceeds this.
        budget_total: Stop the batch if cumulative cost exceeds this.

    Returns:
        BatchSummary with per-video results.
    """
    t_batch = time.monotonic()
    summary = BatchSummary()

    # ── SQLite tracker (optional) ──────────────────────────────────────────────
    db_tracker: Any = None
    if db_path is not None and not dry_run:
        from utils.db import VideoTracker  # lazy import
        db_tracker = VideoTracker(db_path=db_path)
        log.info("DB tracking enabled: %s", db_tracker.db_path)

    source_dirs = _scan_input_dir(input_dir)
    if not source_dirs:
        log.warning("No valid Transcriber output directories found in: %s", input_dir)
        return summary

    proj_root = ROOT / "projects"
    summary.total = len(source_dirs)

    # ── Filter already-done videos ────────────────────────────────────────────
    pending: list[Path] = []
    for d in source_dirs:
        if skip_done and not dry_run and _is_done(d, proj_root):
            log.info("[BATCH] Skipping (already done): %s", d.name)
            summary.skipped += 1
            summary.results.append(VideoResult(name=d.name, status="skipped"))
        else:
            pending.append(d)

    if not pending:
        log.info("[BATCH] All %d videos already done. Use --no-skip-done to reprocess.", len(source_dirs))
        summary.elapsed = time.monotonic() - t_batch
        return summary

    # ── Dry-run: cost estimate per video + total ──────────────────────────────
    if dry_run:
        print()
        print("=" * 70)
        print(f"  BATCH DRY RUN — {len(pending)} videos")
        print("=" * 70)
        total_estimated = 0.0
        for d in pending:
            try:
                tracker = estimate_cost(
                    script_path=None,
                    channel_config_path=channel_config_path,
                    quality_preset=quality,
                )
                est = tracker.total
                total_estimated += est
                summary.total_estimated_cost += est
                print(f"  {d.name[:50]:<52} ${est:.4f}")
            except Exception as exc:
                log.debug("Cost estimate failed for %s: %s", d.name, exc)
        print()
        print(f"  {'TOTAL (estimated)':<52} ${total_estimated:.4f}")
        if budget_total:
            print(f"  Total budget limit: ${budget_total:.2f}")
            if total_estimated > budget_total:
                print(f"  WARNING: Estimated cost exceeds total budget by ${total_estimated - budget_total:.4f}")
        print()
        sys.stdout.flush()

    # ── Run pipeline for each pending video ───────────────────────────────────
    sem = asyncio.Semaphore(parallel)
    cumulative_cost = 0.0

    tasks = [
        _process_one(
            source_dir=d,
            channel_config_path=channel_config_path,
            quality=quality,
            dry_run=dry_run,
            draft=draft,
            from_step=from_step,
            template=template,
            budget_per_video=budget_per_video,
            sem=sem,
            db_tracker=db_tracker,
        )
        for d in pending
    ]

    results: list[VideoResult] = []
    # Process with gather (semaphore controls actual concurrency)
    raw_results = await asyncio.gather(*tasks, return_exceptions=False)

    for result in raw_results:
        results.append(result)
        summary.results.append(result)

        if result.status == "done":
            summary.done += 1
        elif result.status == "failed":
            summary.failed += 1

        # Check total budget (rough: assume per-video estimate for cost)
        if budget_total and cumulative_cost > budget_total:
            log.error(
                "[BATCH] Total budget of $%.2f exceeded ($%.4f spent). Stopping.",
                budget_total, cumulative_cost,
            )
            break

    summary.elapsed = time.monotonic() - t_batch
    return summary


# ─── Summary display ──────────────────────────────────────────────────────────

def _print_summary(summary: BatchSummary) -> None:
    """Print a formatted batch run summary."""
    print()
    print("=" * 70)
    print("  BATCH COMPLETE")
    print("=" * 70)
    print(f"  Total    : {summary.total}")
    print(f"  Done     : {summary.done}")
    print(f"  Skipped  : {summary.skipped}  (already had final.mp4)")
    print(f"  Failed   : {summary.failed}")
    print(f"  Elapsed  : {summary.elapsed:.1f}s")
    if summary.total_estimated_cost > 0:
        print(f"  Est. cost: ${summary.total_estimated_cost:.4f}")
    print()

    if summary.failed > 0:
        print("  Failed videos:")
        for r in summary.results:
            if r.status == "failed":
                err = f" — {r.error}" if r.error else ""
                print(f"    x  {r.name}{err}")
        print()

    sys.stdout.flush()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="batch_runner",
        description="VideoForge Batch Runner -- process multiple videos sequentially or in parallel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    io_grp = parser.add_argument_group("Input / Output")
    io_grp.add_argument(
        "--input-dir", metavar="DIR", required=True,
        help="Root directory containing Transcriber output subdirectories",
    )
    io_grp.add_argument(
        "--channel", metavar="JSON", required=True,
        help="Channel config JSON (e.g. config/channels/history.json)",
    )

    run_grp = parser.add_argument_group("Run Options")
    run_grp.add_argument(
        "--parallel", type=int, metavar="N", default=1,
        help="Max simultaneous pipeline runs (default: 1 = sequential)",
    )
    run_grp.add_argument(
        "--quality", metavar="PRESET",
        choices=["max", "high", "balanced", "bulk", "test"],
        default="bulk",
        help="LLM quality preset (default: bulk)",
    )
    run_grp.add_argument(
        "--template", metavar="TYPE",
        choices=["auto", "documentary", "listicle", "tutorial", "comparison"],
        default="auto",
        help="Content template (default: auto)",
    )
    run_grp.add_argument(
        "--from-step", type=int, metavar="N", default=1,
        choices=range(1, 7),
        help="Resume all videos from step N (1-6)",
    )
    run_grp.add_argument(
        "--dry-run", action="store_true",
        help="Estimate costs without making API calls",
    )
    run_grp.add_argument(
        "--draft", action="store_true",
        help="Generate 480p preview videos",
    )
    run_grp.add_argument(
        "--no-skip-done", action="store_true",
        help="Reprocess videos that already have output/final.mp4",
    )

    budget_grp = parser.add_argument_group("Budget")
    budget_grp.add_argument(
        "--budget-per-video", type=float, metavar="USD",
        help="Max spend per video in USD",
    )
    budget_grp.add_argument(
        "--budget-total", type=float, metavar="USD",
        help="Max total batch spend in USD",
    )

    track_grp = parser.add_argument_group("Tracking")
    track_grp.add_argument(
        "--track", action="store_true",
        help="Record all runs in the SQLite tracker (data/videoforge.db)",
    )
    track_grp.add_argument(
        "--db", metavar="PATH",
        help="Custom DB path for tracking (default: data/videoforge.db)",
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        parser.error(f"--input-dir not found: {input_dir}")

    channel_path = Path(args.channel)
    if not channel_path.exists():
        parser.error(f"--channel file not found: {channel_path}")

    if args.parallel < 1:
        parser.error("--parallel must be >= 1")

    load_env()

    log.info("Batch Runner starting")
    log.info("Input dir  : %s", input_dir)
    log.info("Channel    : %s", channel_path.name)
    log.info("Quality    : %s", args.quality)
    log.info("Parallel   : %d", args.parallel)
    log.info("Skip done  : %s", not args.no_skip_done)
    if args.budget_per_video:
        log.info("Budget/vid : $%.2f", args.budget_per_video)
    if args.budget_total:
        log.info("Budget tot : $%.2f", args.budget_total)

    db_path: str | None = None
    if args.track:
        from utils.db import DEFAULT_DB_PATH  # lazy import
        db_path = args.db or str(DEFAULT_DB_PATH)

    summary = asyncio.run(
        run_batch(
            input_dir=input_dir,
            channel_config_path=channel_path,
            parallel=args.parallel,
            quality=args.quality,
            dry_run=args.dry_run,
            draft=args.draft,
            skip_done=not args.no_skip_done,
            from_step=args.from_step,
            template=args.template,
            budget_per_video=args.budget_per_video,
            budget_total=args.budget_total,
            db_path=db_path,
        )
    )

    _print_summary(summary)

    # Non-zero exit if any videos failed
    if summary.failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
