"""
VideoForge Backend — Pipeline & Batch routes.

POST /api/pipeline/run   → start single-video job
POST /api/batch/run      → start batch job
GET  /api/jobs           → list jobs
GET  /api/jobs/{job_id}  → job status
DELETE /api/jobs/{job_id} → cancel job
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.job_manager import manager
from backend.models import BatchRunRequest, JobResponse, PipelineRunRequest

router = APIRouter(tags=["jobs"])

ROOT = Path(__file__).parent.parent.parent


# ── Pipeline ──────────────────────────────────────────────────────────────────

@router.post("/pipeline/run", response_model=JobResponse, status_code=202)
async def run_pipeline(req: PipelineRunRequest) -> dict:
    """Start a single-video pipeline job. Returns a job_id for polling / WebSocket."""
    source_dir = Path(req.source_dir)
    if not source_dir.is_dir():
        raise HTTPException(400, f"source_dir not found: {source_dir}")

    channel_path = _resolve_channel(req.channel)

    job_id = await manager.start_pipeline(
        source_dir=source_dir,
        channel_config_path=channel_path,
        quality=req.quality,
        template=req.template,
        draft=req.draft,
        from_step=req.from_step,
        to_step=req.to_step,
        budget=req.budget,
        langs=req.langs,
        dry_run=req.dry_run,
        background_music=req.background_music,
        no_ken_burns=req.no_ken_burns,
        skip_thumbnail=req.skip_thumbnail,
        burn_subtitles=req.burn_subtitles,
        image_style=req.image_style,
        voice_id=req.voice_id,
        master_prompt=req.master_prompt,
        duration_min=req.duration_min if req.duration_min is not None else 8,
        duration_max=req.duration_max if req.duration_max is not None else 12,
        music_volume=req.music_volume,
    )
    return manager.get(job_id).to_response()  # type: ignore[union-attr]


# ── Batch ─────────────────────────────────────────────────────────────────────

@router.post("/batch/run", response_model=JobResponse, status_code=202)
async def run_batch(req: BatchRunRequest) -> dict:
    """Start a batch job. Returns a job_id for polling / WebSocket."""
    input_dir = Path(req.input_dir)
    if not input_dir.is_dir():
        raise HTTPException(400, f"input_dir not found: {input_dir}")

    channel_path = _resolve_channel(req.channel)

    job_id = await manager.start_batch(
        input_dir=input_dir,
        channel_config_path=channel_path,
        quality=req.quality,
        parallel=req.parallel,
        draft=req.draft,
        from_step=req.from_step,
        budget_per_video=req.budget_per_video,
        budget_total=req.budget_total,
        skip_done=req.skip_done,
        dry_run=req.dry_run,
    )
    return manager.get(job_id).to_response()  # type: ignore[union-attr]


# ── Job management ────────────────────────────────────────────────────────────

@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(limit: int = 50) -> list[dict]:
    """List recent jobs (newest first)."""
    return [j.to_response() for j in manager.list_jobs(limit=limit)]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> dict:
    """Get status and logs for a specific job."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job.to_response()


@router.delete("/jobs/{job_id}", status_code=200)
async def cancel_job(job_id: str) -> dict:
    """Cancel a running or queued job."""
    ok = await manager.cancel(job_id)
    if not ok:
        raise HTTPException(404, f"Job not found or already finished: {job_id}")
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/jobs/{job_id}/approve", status_code=200)
async def approve_job(job_id: str, stage: str = "script") -> dict:
    """Approve a review checkpoint and continue the pipeline."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    if job.status != "waiting_review":
        raise HTTPException(400, f"Job is not waiting for review (status: {job.status})")
    ok = await job.approve(stage)
    if not ok:
        raise HTTPException(400, f"No pending review for stage: {stage}")
    return {"job_id": job_id, "stage": stage, "approved": True}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_channel(channel: str) -> Path:
    p = Path(channel)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise HTTPException(400, f"Channel config not found: {p}")
    return p
