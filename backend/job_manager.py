"""
VideoForge Backend — Async job manager.

Runs pipeline/batch jobs as asyncio Tasks, tracks state, and fans out
progress events to WebSocket subscribers via per-job asyncio.Queues.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── Job dataclass ────────────────────────────────────────────────────────────

@dataclass
class Job:
    job_id: str
    kind: str          # "pipeline" | "batch"
    status: str        # "queued" | "running" | "waiting_review" | "done" | "failed" | "cancelled"
    source: str        # source_dir.name or input_dir.name
    source_dir: str    # full absolute path to transcriber output dir
    channel: str
    quality: str
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed: float | None = None
    step: int = 0
    step_name: str = ""
    pct: float = 0.0
    error: str = ""
    logs: list[str] = field(default_factory=list)
    db_video_id: int | None = None
    review_stage: str | None = None
    review_data: dict = field(default_factory=dict, repr=False, compare=False)
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    subscribers: list[asyncio.Queue] = field(
        default_factory=list, repr=False, compare=False,
    )
    _review_events: dict[str, asyncio.Event] = field(
        default_factory=dict, repr=False, compare=False,
    )

    # ── Fan-out ───────────────────────────────────────────────────────────────

    def emit(self, **event: Any) -> None:
        """Push an event dict to all active WebSocket subscribers."""
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def log(self, message: str) -> None:
        self.logs.append(message)
        self.emit(type="log", message=message)

    # ── Review checkpoints ────────────────────────────────────────────────────

    async def approve(self, stage: str) -> bool:
        """Unblock a review checkpoint. Returns False if no pending review for stage."""
        event = self._review_events.get(stage)
        if event is None:
            return False
        event.set()
        return True

    def to_response(self) -> dict:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "source": self.source,
            "source_dir": self.source_dir,
            "project_dir": str(ROOT / "projects" / self.source) if self.source else "",
            "channel": self.channel,
            "quality": self.quality,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": self.elapsed,
            "step": self.step,
            "step_name": self.step_name,
            "pct": self.pct,
            "error": self.error,
            "logs": self.logs,
            "db_video_id": self.db_video_id,
            "review_stage": self.review_stage,
        }


# ─── Job Manager ──────────────────────────────────────────────────────────────

class JobManager:
    """
    In-memory registry of running/completed pipeline and batch jobs.

    Jobs run as asyncio Tasks in the same event loop as the FastAPI app.
    Progress events are pushed to WebSocket subscriber queues.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self, *, limit: int = 100) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        if job.task:
            job.task.cancel()
        job.status = "cancelled"
        job.finished_at = _now()
        job.emit(type="cancelled")
        return True

    # ── Pipeline ──────────────────────────────────────────────────────────────

    async def start_pipeline(
        self,
        source_dir: Path | None,
        channel_config_path: Path,
        **kwargs: Any,
    ) -> str:
        job_id = uuid.uuid4().hex[:8]
        _source_label = (
            source_dir.name
            if source_dir is not None
            else (kwargs.get("custom_topic") or "topic-only")[:40]
        )
        job = Job(
            job_id=job_id,
            kind="pipeline",
            status="queued",
            source=_source_label,
            source_dir=str(source_dir) if source_dir is not None else "",
            channel=channel_config_path.stem,
            quality=kwargs.get("quality", "max"),
        )
        self._jobs[job_id] = job
        job.task = asyncio.create_task(
            self._run_pipeline_job(job, source_dir, channel_config_path, **kwargs),
            name=f"pipeline-{job_id}",
        )
        return job_id

    async def _run_pipeline_job(
        self,
        job: Job,
        source_dir: Path,
        channel_config_path: Path,
        **kwargs: Any,
    ) -> None:
        import sys
        sys.path.insert(0, str(ROOT))

        from modules.common import load_env
        from pipeline import run_pipeline
        from utils.db import VideoTracker

        load_env()
        job.status = "running"
        job.started_at = _now()
        job.emit(type="status", status="running")

        t0 = time.monotonic()

        # Always record to DB
        dry_run = kwargs.get("dry_run", False)
        db_tracker: VideoTracker | None = None
        db_video_id: int | None = None
        if not dry_run:
            custom_topic = kwargs.get("custom_topic", "") or ""
            if custom_topic.strip():
                _safe = re.sub(r'[\\/:*?"<>|]', "_", custom_topic.strip())[:200].strip(". ")
                _folder = _safe or source_dir.name
            else:
                _folder = source_dir.name
            db_tracker = VideoTracker()
            db_video_id = db_tracker.create_video(
                source_dir=source_dir,
                channel=channel_config_path.stem,
                quality_preset=kwargs.get("quality", "max"),
                template=kwargs.get("template", "auto"),
                from_step=kwargs.get("from_step", 1),
                project_dir=ROOT / "projects" / _folder,
            )
            job.db_video_id = db_video_id

        def progress_callback(event: dict) -> None:
            evt_type = event.get("type", "")
            if evt_type == "step_start":
                job.step = event.get("step", job.step)
                job.step_name = event.get("name", "")
                if "pct" in event:
                    job.pct = float(event["pct"])
                job.log(f"[Step {job.step}] {job.step_name}")
            elif evt_type == "step_done":
                elapsed_s = event.get("elapsed", 0.0)
                if "pct" in event:
                    job.pct = float(event["pct"])
                job.log(f"[Step {event.get('step', job.step)} done] {elapsed_s:.1f}s")
            elif evt_type == "sub_progress":
                # Fine-grained within-step progress (e.g. FFmpeg block-by-block)
                if "pct" in event:
                    job.pct = float(event["pct"])
            job.emit(**event)

        async def review_callback(stage: str, data: dict) -> None:
            """Pause pipeline at a review checkpoint until approved via REST."""
            ev = asyncio.Event()
            job._review_events[stage] = ev
            job.review_stage = stage
            job.review_data = data  # stored for late WS joiners
            job.status = "waiting_review"
            job.emit(type="review_required", stage=stage, data=data)
            job.log(f"[Review] Waiting for approval at stage: {stage}")
            await ev.wait()
            job._review_events.pop(stage, None)
            job.review_stage = None
            job.status = "running"
            job.emit(type="review_approved", stage=stage)
            job.log(f"[Review] Approved: {stage}")

        try:
            await run_pipeline(
                source_dir=source_dir,
                channel_config_path=channel_config_path,
                db_tracker=db_tracker,
                db_video_id=db_video_id,
                progress_callback=progress_callback,
                review_callback=review_callback,
                **kwargs,
            )
            elapsed = time.monotonic() - t0
            job.status = "done"
            job.finished_at = _now()
            job.elapsed = elapsed
            job.log(f"Done in {elapsed:.1f}s")
            job.emit(type="done", elapsed=elapsed, db_video_id=db_video_id)
        except asyncio.CancelledError:
            elapsed = time.monotonic() - t0
            job.status = "cancelled"
            job.finished_at = _now()
            job.elapsed = elapsed
            if db_tracker and db_video_id is not None:
                db_tracker.set_failed(db_video_id, "cancelled", elapsed_seconds=elapsed)
            job.emit(type="cancelled")
            raise
        except Exception as exc:
            elapsed = time.monotonic() - t0
            job.status = "failed"
            job.finished_at = _now()
            job.elapsed = elapsed
            job.error = str(exc)[:500]
            if db_tracker and db_video_id is not None:
                db_tracker.set_failed(db_video_id, str(exc), elapsed_seconds=elapsed)
            job.log(f"Error: {exc}")
            job.emit(type="error", message=str(exc)[:500])

    # ── Batch ─────────────────────────────────────────────────────────────────

    async def start_batch(
        self,
        input_dir: Path,
        channel_config_path: Path,
        **kwargs: Any,
    ) -> str:
        job_id = uuid.uuid4().hex[:8]
        job = Job(
            job_id=job_id,
            kind="batch",
            status="queued",
            source=input_dir.name,
            source_dir=str(input_dir),
            channel=channel_config_path.stem,
            quality=kwargs.get("quality", "bulk"),
        )
        self._jobs[job_id] = job
        job.task = asyncio.create_task(
            self._run_batch_job(job, input_dir, channel_config_path, **kwargs),
            name=f"batch-{job_id}",
        )
        return job_id

    async def _run_batch_job(
        self,
        job: Job,
        input_dir: Path,
        channel_config_path: Path,
        **kwargs: Any,
    ) -> None:
        import sys
        sys.path.insert(0, str(ROOT))

        from batch_runner import run_batch
        from modules.common import load_env
        from utils.db import DEFAULT_DB_PATH

        load_env()
        job.status = "running"
        job.started_at = _now()
        job.emit(type="status", status="running")

        t0 = time.monotonic()
        dry_run = kwargs.get("dry_run", False)

        try:
            summary = await run_batch(
                input_dir=input_dir,
                channel_config_path=channel_config_path,
                db_path=None if dry_run else str(DEFAULT_DB_PATH),
                **kwargs,
            )
            elapsed = time.monotonic() - t0
            job.status = "done"
            job.finished_at = _now()
            job.elapsed = elapsed
            msg = (
                f"Batch done in {elapsed:.1f}s: "
                f"{summary.done} done, {summary.failed} failed, {summary.skipped} skipped"
            )
            job.log(msg)
            job.emit(
                type="done", elapsed=elapsed,
                done=summary.done, failed=summary.failed, skipped=summary.skipped,
            )
        except asyncio.CancelledError:
            elapsed = time.monotonic() - t0
            job.status = "cancelled"
            job.finished_at = _now()
            job.elapsed = elapsed
            job.emit(type="cancelled")
            raise
        except Exception as exc:
            elapsed = time.monotonic() - t0
            job.status = "failed"
            job.finished_at = _now()
            job.elapsed = elapsed
            job.error = str(exc)[:500]
            job.log(f"Error: {exc}")
            job.emit(type="error", message=str(exc)[:500])


    # ── Multi-Topic Batch ──────────────────────────────────────────────────────

    async def start_multi_batch(
        self,
        items: list[dict],   # each: {source_dir: Path, channel_config_path: Path, kwargs: dict}
        parallel: int = 2,
    ) -> list[str]:
        """
        Start N independent pipeline jobs from a topic queue.

        All N jobs are created immediately (visible in UI) but only `parallel`
        run at a time — controlled by a shared asyncio.Semaphore.

        Each job tracks progress independently (own WebSocket, own DB record).

        Args:
            items: List of dicts with keys:
                - source_dir (Path): Transcriber output dir
                - channel_config_path (Path): Channel config
                - kwargs (dict): Extra args forwarded to run_pipeline()
            parallel: Max simultaneous pipelines (1–8).

        Returns:
            List of job_ids (one per item).
        """
        sem = asyncio.Semaphore(parallel)
        job_ids: list[str] = []

        for item in items:
            source_dir: Path | None = item["source_dir"]  # None = topic-only mode
            channel_config_path: Path = item["channel_config_path"]
            kwargs: dict = item.get("kwargs", {})

            _source_label = (
                source_dir.name
                if source_dir is not None
                else (kwargs.get("custom_topic") or "topic-only")[:40]
            )
            job_id = uuid.uuid4().hex[:8]
            job = Job(
                job_id=job_id,
                kind="pipeline",
                status="queued",
                source=_source_label,
                source_dir=str(source_dir) if source_dir is not None else "",
                channel=channel_config_path.stem,
                quality=kwargs.get("quality", "max"),
            )
            self._jobs[job_id] = job
            job.task = asyncio.create_task(
                self._run_with_semaphore(sem, job, source_dir, channel_config_path, **kwargs),
                name=f"pipeline-{job_id}",
            )
            job_ids.append(job_id)

        return job_ids

    async def _run_with_semaphore(
        self,
        sem: asyncio.Semaphore,
        job: Job,
        source_dir: Path,
        channel_config_path: Path,
        **kwargs: Any,
    ) -> None:
        """Acquire semaphore slot then run pipeline — queued jobs wait their turn."""
        async with sem:
            await self._run_pipeline_job(job, source_dir, channel_config_path, **kwargs)


# ── Singleton ─────────────────────────────────────────────────────────────────
manager = JobManager()
