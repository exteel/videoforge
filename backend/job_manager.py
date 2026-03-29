"""
VideoForge Backend — Async job manager.

Runs pipeline/batch jobs as asyncio Tasks, tracks state, and fans out
progress events to WebSocket subscribers via per-job asyncio.Queues.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows cp1252 → UTF-8 fix for stderr progress output
_STDERR = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)


def _term_bar(pct: float, width: int = 28) -> str:
    """Return a Unicode progress bar string: [████░░░░] 62%"""
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:3.0f}%"

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
    project_dir: str = ""  # actual output directory (set when pipeline starts)
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
            "project_dir": self.project_dir or (str(ROOT / "projects" / self.channel / self.source) if (self.source and self.channel) else ""),
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

    def restore_from_db(self, limit: int = 50) -> int:
        """Load recent completed/failed jobs from DB into memory on startup."""
        try:
            from utils.db import VideoTracker
            db = VideoTracker()
            rows = db.list_videos(limit=limit)
            count = 0
            for row in rows:
                status = row.get("status", "")
                if status not in ("done", "failed", "cancelled"):
                    continue
                job_id = f"db-{row['id']}"
                if job_id in self._jobs:
                    continue
                source_dir = row.get("source_dir") or ""
                source = row.get("source_title") or Path(source_dir).name
                job = Job(
                    job_id=job_id,
                    kind="pipeline",
                    status=status,
                    source=source,
                    source_dir=source_dir,
                    channel=row.get("channel") or "",
                    quality=row.get("quality_preset") or "max",
                    created_at=row.get("created_at") or _now(),
                    started_at=row.get("started_at"),
                    finished_at=row.get("updated_at"),
                    elapsed=row.get("elapsed_seconds"),
                    step=6 if status == "done" else 0,
                    pct=100.0 if status == "done" else 0.0,
                    error=row.get("error") or "",
                    db_video_id=row.get("id"),
                    project_dir=row.get("project_dir") or "",
                )
                self._jobs[job_id] = job
                count += 1
            return count
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("restore_from_db failed: %s", exc)
            return 0

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
                _folder = _safe or (source_dir.name if source_dir else "topic")
            else:
                _folder = source_dir.name if source_dir else "topic"
            _actual_proj = ROOT / "projects" / channel_config_path.stem / _folder
            job.project_dir = str(_actual_proj)  # store real path for Drive upload
            db_tracker = VideoTracker()
            db_video_id = db_tracker.create_video(
                source_dir=source_dir,
                channel=channel_config_path.stem,
                quality_preset=kwargs.get("quality", "max"),
                template=kwargs.get("template", "auto"),
                from_step=kwargs.get("from_step", 1),
                project_dir=_actual_proj,
            )
            job.db_video_id = db_video_id

        import json as _json
        if db_tracker and db_video_id:
            try:
                _serializable = {
                    k: str(v) if isinstance(v, Path) else v
                    for k, v in kwargs.items()
                    if k not in ("progress_callback", "review_callback")
                }
                db_tracker.save_pipeline_kwargs(db_video_id, _json.dumps(_serializable, default=str))
            except Exception:
                pass

        _bar_active = [False]   # True while a \r progress line is on stderr

        def _term(text: str, *, newline: bool = True, overwrite: bool = False) -> None:
            """Write to stderr with optional \r overwrite (in-place progress bar)."""
            try:
                prefix = "\r" if overwrite else ("\n" if _bar_active[0] else "")
                suffix = "" if overwrite else "\n"
                _STDERR.write(f"{prefix}{text}{suffix}")
                _STDERR.flush()
                _bar_active[0] = overwrite
            except Exception:
                pass

        def progress_callback(event: dict) -> None:
            evt_type = event.get("type", "")
            if evt_type == "step_start":
                job.step = event.get("step", job.step)
                job.step_name = event.get("name", "")
                if "pct" in event:
                    job.pct = float(event["pct"])
                job.log(f"[Step {job.step}] {job.step_name}")
                _term(f"▶  Step {job.step}: {job.step_name}  [{job_id}]")
            elif evt_type == "step_done":
                elapsed_s = event.get("elapsed", 0.0)
                if "pct" in event:
                    job.pct = float(event["pct"])
                job.log(f"[Step {event.get('step', job.step)} done] {elapsed_s:.1f}s")
                _term(f"✓  Step {event.get('step', job.step)} done  {elapsed_s:.1f}s")
                if db_tracker and db_video_id:
                    try:
                        db_tracker.update_job_progress(
                            db_video_id,
                            event.get("step", 0),
                            event.get("name", ""),
                            event.get("pct", 0),
                        )
                    except Exception:
                        pass
            elif evt_type == "sub_progress":
                # Fine-grained within-step progress — in-place bar on stderr
                if "pct" in event:
                    job.pct = float(event["pct"])
                msg = event.get("message", "")
                bar = _term_bar(job.pct)
                _term(f"   {bar}  {msg:<35}", overwrite=True)
            elif evt_type == "error":
                msg = event.get("message", "")
                _term(f"✗  Error: {msg[:120]}")
            elif evt_type == "review_required":
                _term(f"⏸  Review required — stage: {event.get('stage', '?')}  [{job_id}]")
            elif evt_type == "done":
                elapsed_s = event.get("elapsed", 0.0)
                _term(f"✅ Done  {elapsed_s:.1f}s  [{job_id}]")
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
            try:
                from utils.telegram_notify import notify_telegram
                await notify_telegram(f"⏸ <b>Review needed</b>: {job.source}\n{job.channel} • stage: {stage}")
            except Exception:
                pass
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
            try:
                from utils.telegram_notify import notify_telegram
                await notify_telegram(f"✅ <b>{job.source}</b>\n{job.channel} • {job.elapsed:.0f}s")
            except Exception:
                pass
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
            try:
                from utils.telegram_notify import notify_telegram
                await notify_telegram(f"❌ <b>{job.source}</b>\n{job.channel} • {job.error[:200]}")
            except Exception:
                pass

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

    # Shared semaphore for the active queue — reused by append_to_queue()
    _active_sem: asyncio.Semaphore | None = None
    _active_parallel: int = 2

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
        self._active_sem = sem
        self._active_parallel = parallel
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

    async def append_to_queue(
        self,
        items: list[dict],
    ) -> list[str]:
        """
        Append jobs to the currently active queue, sharing its semaphore.

        If no active queue exists (first run or all previous jobs finished),
        creates a new semaphore with the last used parallel value.

        Returns:
            List of new job_ids.
        """
        if self._active_sem is None:
            self._active_sem = asyncio.Semaphore(self._active_parallel)

        sem = self._active_sem
        job_ids: list[str] = []

        for item in items:
            source_dir: Path | None = item["source_dir"]
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


    # ── Quick (script + voice + 1 image) ──────────────────────────────────────

    async def start_quick(
        self,
        transcription_url: str,
        topic: str,
        channel_config_path: Path,
        *,
        sem: asyncio.Semaphore | None = None,
        **kwargs: Any,
    ) -> str:
        job_id = uuid.uuid4().hex[:8]
        job = Job(
            job_id=job_id,
            kind="pipeline",
            status="queued",
            source=topic[:40] or "quick",
            source_dir="",
            channel=channel_config_path.stem,
            quality=kwargs.get("quality", "balanced"),
        )
        self._jobs[job_id] = job

        async def _run() -> None:
            if sem:
                async with sem:
                    await self._run_quick_job(job, transcription_url, topic, channel_config_path, **kwargs)
            else:
                await self._run_quick_job(job, transcription_url, topic, channel_config_path, **kwargs)

        job.task = asyncio.create_task(_run(), name=f"quick-{job_id}")
        return job_id

    async def start_quick_batch(
        self,
        items: list[dict],   # each: {transcription_url, topic, channel_config_path, **kwargs}
        parallel: int = 2,
    ) -> list[str]:
        """Start N quick jobs with a shared semaphore — only `parallel` run at a time."""
        sem = asyncio.Semaphore(parallel)
        self._active_sem = sem
        self._active_parallel = parallel
        job_ids: list[str] = []
        for item in items:
            jid = await self.start_quick(
                transcription_url=item["transcription_url"],
                topic=item["topic"],
                channel_config_path=item["channel_config_path"],
                sem=sem,
                **{k: v for k, v in item.items() if k not in ("transcription_url", "topic", "channel_config_path")},
            )
            job_ids.append(jid)
        return job_ids

    async def _run_quick_job(
        self,
        job: Job,
        transcription_url: str,
        topic: str,
        channel_config_path: Path,
        **kwargs: Any,
    ) -> None:
        import importlib.util
        import re as _re
        import sys
        sys.path.insert(0, str(ROOT))

        from modules.common import load_env
        from utils.db import VideoTracker

        import shutil as _shutil

        load_env()
        job.status = "running"
        job.started_at = _now()
        job.emit(type="status", status="running")

        t0 = time.monotonic()
        quality      = kwargs.get("quality", "balanced")
        voice_id     = kwargs.get("voice_id") or None
        duration_min = kwargs.get("duration_min", 25)
        duration_max = kwargs.get("duration_max", 30)
        force        = kwargs.get("force", False)

        _safe = _re.sub(r'[\\/:*?"<>|]', "_", topic.strip())[:200].strip(". ")
        proj = ROOT / "projects" / channel_config_path.stem / (_safe or "quick")

        # ── Force: nuke project dir + transcription dir ─────────────────────
        if force and proj.exists():
            job.log(f"[Force] Deleting project dir: {proj}")
            _shutil.rmtree(proj, ignore_errors=True)

        if force and transcription_url.startswith("http"):
            # Transcription will be re-downloaded; also nuke cached transcription
            import os as _os
            _trans_base = Path(
                _os.environ.get("TRANSCRIBER_OUTPUT", r"D:\transscript batch\output\output")
            )
            # Try to find matching dir by topic name
            for _cand in (_trans_base / _safe, _trans_base / topic.strip()):
                if _cand.exists():
                    job.log(f"[Force] Deleting transcription dir: {_cand}")
                    _shutil.rmtree(_cand, ignore_errors=True)

        proj.mkdir(parents=True, exist_ok=True)
        job.project_dir = str(proj)

        db_tracker = VideoTracker()
        db_video_id = db_tracker.create_video(
            source_dir=proj,
            channel=channel_config_path.stem,
            quality_preset=quality,
            template="quick",
            from_step=1,
            project_dir=proj,
        )
        job.db_video_id = db_video_id
        db_tracker.set_running(db_video_id)

        def _load_mod(name: str, rel_path: str) -> Any:
            full = ROOT / rel_path
            # Evict cached dependency modules so code changes take effect
            # without restarting the backend (especially clients/*).
            for _dep in list(sys.modules):
                if _dep.startswith("clients.") or _dep.startswith("modules."):
                    del sys.modules[_dep]
            spec = importlib.util.spec_from_file_location(name, str(full))
            mod  = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules[name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod

        try:
            source_dir: Path | None = None

            # ── 0: Transcription (if URL) ─────────────────────────────────────
            if transcription_url.startswith("http"):
                job.step = 0
                job.step_name = "Transcribing"
                job.pct = 5.0
                job.log("[Quick] Transcribing URL…")
                job.emit(type="step_start", step=0, name="Transcribing", pct=5.0)

                from backend.transcribe_worker import transcribe_url
                source_dir = await transcribe_url(
                    transcription_url,
                    on_progress=lambda msg: job.log(f"[Transcribe] {msg}"),
                )
                job.log(f"[Quick] Transcribed → {source_dir}")
                job.emit(type="step_done", step=0, elapsed=time.monotonic() - t0, pct=20.0)

            elif transcription_url:
                p = Path(transcription_url)
                if not p.is_dir():
                    raise ValueError(f"Transcription path not found: {p}")
                source_dir = p

            # ── 1: Script ─────────────────────────────────────────────────────
            job.step = 1
            job.step_name = "Script"
            job.pct = 20.0
            job.log("[Quick] Generating script…")
            job.emit(type="step_start", step=1, name="Script", pct=20.0)

            mod01 = _load_mod("01_script_generator", "modules/01_script_generator.py")
            await mod01.generate_scripts(
                source_dir,
                channel_config_path,
                preset=quality,
                custom_topic=topic,
                output_dir=proj,
                duration_min=duration_min,
                duration_max=duration_max,
            )
            script_path = proj / "script.json"
            job.log(f"[Quick] Script → {script_path}")
            job.emit(type="step_done", step=1, elapsed=time.monotonic() - t0, pct=50.0)

            # ── 1b: Script review ─────────────────────────────────────────────
            import json as _json
            _sd = _json.loads(script_path.read_text(encoding="utf-8"))
            _blocks = _sd.get("blocks", [])
            _word_count = sum(len((b.get("narration") or "").split()) for b in _blocks)
            _total_imgs = sum(
                len(b.get("image_prompts") or []) or (1 if (b.get("image_prompt") or "").strip() else 0)
                for b in _blocks
            )
            _block_summaries = [
                {
                    "id":          b.get("id", ""),
                    "type":        b.get("type", "section"),
                    "title":       b.get("title", ""),
                    "word_count":      len((b.get("narration") or "").split()),
                    "image_count":     len(b.get("image_prompts") or []) or (1 if (b.get("image_prompt") or "").strip() else 0),
                    "narration":       (b.get("narration") or "")[:120],
                    "est_duration_sec": round(len((b.get("narration") or "").split()) / 170 * 60, 1),
                }
                for b in _blocks
            ]
            _review_data = {
                "script_path":        str(script_path),
                "title":              _sd.get("title", ""),
                "block_count":        len(_blocks),
                "word_count":         _word_count,
                "duration_min":       round(_word_count / 150, 1),
                "duration_max":       round(_word_count / 130, 1),
                "image_prompt_count": _total_imgs,
                "blocks":             _block_summaries,
            }

            # Pause and wait for user approval via WebSocket / REST
            _rev_ev = asyncio.Event()
            job._review_events["script"] = _rev_ev
            job.review_stage = "script"
            job.review_data = _review_data
            job.status = "waiting_review"
            job.emit(type="review_required", stage="script", data=_review_data)
            job.log(f"[Quick] Waiting for script approval ({len(_blocks)} blocks, {_word_count} words)…")
            try:
                from utils.telegram_notify import notify_telegram
                await notify_telegram(f"⏸ <b>Review needed</b>: {job.source}\n{job.channel} • stage: script")
            except Exception:
                pass
            await _rev_ev.wait()
            job._review_events.pop("script", None)
            job.review_stage = None
            job.status = "running"
            job.emit(type="review_approved", stage="script")
            job.log("[Quick] Script approved — continuing")

            # ── 2: Voice ──────────────────────────────────────────────────────
            job.step = 2
            job.step_name = "Voice"
            job.pct = 50.0
            job.log("[Quick] Generating voice…")
            job.emit(type="step_start", step=2, name="Voice", pct=50.0)

            mod03 = _load_mod("03_voice_generator", "modules/03_voice_generator.py")
            await mod03.generate_voices(
                script_path,
                channel_config_path,
                voice_id_override=voice_id,
                output_dir=proj,
            )
            job.log("[Quick] Voice generated")
            job.emit(type="step_done", step=2, elapsed=time.monotonic() - t0, pct=80.0)

            # ── 3: Thumbnail image (1 image) ───────────────────────────────────
            job.step = 3
            job.step_name = "Image"
            job.pct = 80.0
            job.log("[Quick] Generating thumbnail image…")
            job.emit(type="step_start", step=3, name="Image", pct=80.0)

            (proj / "output").mkdir(exist_ok=True)
            mod06 = _load_mod("06_thumbnail_generator", "modules/06_thumbnail_generator.py")
            await mod06.generate_thumbnail(
                script_path,
                channel_config_path,
                transcriber_dir=source_dir,
                iterate=False,
            )
            job.log("[Quick] Image generated")
            job.emit(type="step_done", step=3, elapsed=time.monotonic() - t0, pct=100.0)

            # ── Done ──────────────────────────────────────────────────────────
            elapsed = time.monotonic() - t0
            db_tracker.set_done(
                db_video_id,
                thumbnail_path=proj / "output" / "thumbnail.png",
                script_path=script_path,
                elapsed_seconds=elapsed,
            )
            job.status = "done"
            job.finished_at = _now()
            job.elapsed = elapsed
            job.log(f"Done in {elapsed:.1f}s")
            job.emit(type="done", elapsed=elapsed, db_video_id=db_video_id)
            try:
                from utils.telegram_notify import notify_telegram
                await notify_telegram(f"✅ <b>{job.source}</b>\n{job.channel} • {job.elapsed:.0f}s")
            except Exception:
                pass

        except asyncio.CancelledError:
            elapsed = time.monotonic() - t0
            job.status = "cancelled"
            job.finished_at = _now()
            job.elapsed = elapsed
            db_tracker.set_failed(db_video_id, "cancelled", elapsed_seconds=elapsed)
            job.emit(type="cancelled")
            raise
        except Exception as exc:
            elapsed = time.monotonic() - t0
            job.status = "failed"
            job.finished_at = _now()
            job.elapsed = elapsed
            job.error = str(exc)[:500]
            db_tracker.set_failed(db_video_id, str(exc), elapsed_seconds=elapsed)
            job.log(f"Error: {exc}")
            job.emit(type="error", message=str(exc)[:500])
            try:
                from utils.telegram_notify import notify_telegram
                await notify_telegram(f"❌ <b>{job.source}</b>\n{job.channel} • {job.error[:200]}")
            except Exception:
                pass


# ── Singleton ─────────────────────────────────────────────────────────────────
manager = JobManager()
