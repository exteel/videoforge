"""
VideoForge Backend — Pipeline & Batch routes.

POST /api/pipeline/run   → start single-video job
POST /api/batch/run      → start batch job
GET  /api/jobs           → list jobs
GET  /api/jobs/{job_id}  → job status
DELETE /api/jobs/{job_id} → cancel job
"""

import json
import re as _re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.auth import check_rate_limit
from backend.job_manager import manager
from backend.models import BatchRunRequest, JobResponse, MultiBatchRequest, PipelineRunRequest, QuickBatchRequest, QuickRunRequest


class BlockEditRequest(BaseModel):
    block_id: str
    narration: str

router = APIRouter(tags=["jobs"])


def _sanitize_input(text: str, max_length: int = 500) -> str:
    """Sanitize user input — strip control chars, limit length."""
    # Remove control characters (except newline \n=0x0a and tab \t=0x09)
    cleaned = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return cleaned[:max_length].strip()

ROOT = Path(__file__).parent.parent.parent


# ── Pipeline ──────────────────────────────────────────────────────────────────

@router.post(
    "/pipeline/run",
    response_model=JobResponse,
    status_code=202,
    summary="Start single-video pipeline",
    description="Start a full video-generation pipeline job (script → voice → images → compile → upload). Returns a job_id for polling or WebSocket streaming.",
)
async def run_pipeline(request: Request, req: PipelineRunRequest) -> dict:
    """Start a single-video pipeline job. Returns a job_id for polling / WebSocket."""
    check_rate_limit(request.client.host if request.client else "unknown")

    source_dir = Path(req.source_dir)
    if not source_dir.is_dir():
        raise HTTPException(400, f"source_dir not found: {source_dir}")

    channel_path = _resolve_channel(req.channel)

    # Fallback to channel config image_style when not provided in request
    _image_style = (req.image_style or "").strip()
    if not _image_style:
        try:
            _cfg = json.loads(channel_path.read_text(encoding="utf-8"))
            _image_style = (_cfg.get("image_style") or "").strip()
        except Exception:
            _image_style = ""

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
        image_style=_image_style,
        voice_id=req.voice_id,
        master_prompt=req.master_prompt,
        duration_min=req.duration_min if req.duration_min is not None else 8,
        duration_max=req.duration_max if req.duration_max is not None else 12,
        music_volume=req.music_volume,
        music_track=req.music_track,
        custom_topic=_sanitize_input(req.custom_topic or ""),
        image_backend=req.image_backend,
        vision_model=req.vision_model,
        auto_approve=req.auto_approve,
        force=req.force,
    )
    return manager.get(job_id).to_response()  # type: ignore[union-attr]


# ── Quick ─────────────────────────────────────────────────────────────────────

@router.post(
    "/pipeline/quick",
    response_model=JobResponse,
    status_code=202,
    summary="Start quick job",
    description="Start a lightweight job that generates script + voice + a single thumbnail image. Skips full video compilation — useful for content previews.",
)
async def run_quick(request: Request, req: QuickRunRequest) -> dict:
    """Start a quick job: script + voice + 1 thumbnail image. No full video compilation."""
    check_rate_limit(request.client.host if request.client else "unknown")

    topic = _sanitize_input(req.topic)
    if not topic:
        raise HTTPException(400, "topic is required")

    channel_path = _resolve_channel(req.channel)

    job_id = await manager.start_quick(
        transcription_url=req.transcription_url.strip(),
        topic=topic,
        channel_config_path=channel_path,
        quality=req.quality,
        voice_id=req.voice_id,
        image_backend=req.image_backend,
        duration_min=req.duration_min if req.duration_min is not None else 25,
        duration_max=req.duration_max if req.duration_max is not None else 30,
        force=req.force,
    )
    return manager.get(job_id).to_response()  # type: ignore[union-attr]


@router.post(
    "/pipeline/quick-batch",
    response_model=list[JobResponse],
    status_code=202,
    summary="Start N quick jobs in parallel",
    description="Submit multiple quick jobs at once. All items are visible immediately; only `parallel` run concurrently. Returns one JobResponse per item.",
)
async def run_quick_batch(request: Request, req: QuickBatchRequest) -> list[dict]:
    """Start N quick jobs with parallel limit. All items visible immediately; only `parallel` run at once."""
    check_rate_limit(request.client.host if request.client else "unknown")

    if not req.items:
        raise HTTPException(400, "items is required")

    specs = []
    for it in req.items:
        topic = _sanitize_input(it.topic)
        if not topic:
            raise HTTPException(400, "topic is required for every item (got empty)")
        specs.append({
            "transcription_url": it.transcription_url.strip(),
            "topic":             topic,
            "channel_config_path": _resolve_channel(it.channel),
            "quality":           it.quality,
            "voice_id":          req.voice_id,
            "image_backend":     req.image_backend,
            "duration_min":      req.duration_min if req.duration_min is not None else 25,
            "duration_max":      req.duration_max if req.duration_max is not None else 30,
            "force":             req.force,
        })

    job_ids = await manager.start_quick_batch(specs, parallel=req.parallel)
    return [manager.get(jid).to_response() for jid in job_ids]  # type: ignore[union-attr]


# ── Batch ─────────────────────────────────────────────────────────────────────

@router.post(
    "/batch/run",
    response_model=JobResponse,
    status_code=202,
    summary="Start batch pipeline from directory",
    description="Start a batch job that runs the full pipeline over all source directories inside `input_dir`. Returns a single parent job_id.",
)
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


# ── Multi-Topic Batch ──────────────────────────────────────────────────────────

@router.post(
    "/batch/multi",
    response_model=list[JobResponse],
    status_code=202,
    summary="Start N independent pipeline jobs from a topic queue",
    description="Each item in `items` becomes a separate full pipeline job. Jobs run in parallel up to `parallel` at a time. Returns one JobResponse per item.",
)
async def run_multi_batch(request: Request, req: MultiBatchRequest) -> list[dict]:
    """
    Start N independent pipeline jobs from a topic queue.

    Each item in `req.items` becomes a separate pipeline job.
    Jobs run in parallel up to `req.parallel` at a time.
    Returns a list of JobResponse objects — one per item.
    """
    check_rate_limit(request.client.host if request.client else "unknown")

    if not req.items:
        raise HTTPException(400, "items list is empty")

    # Build per-job spec list (validate all dirs/channels before creating any job)
    job_specs: list[dict] = []
    for i, item in enumerate(req.items):
        source_dir_str = (item.source_dir or "").strip()
        if source_dir_str:
            source_dir = Path(source_dir_str)
            if not source_dir.is_dir():
                raise HTTPException(400, f"Item {i}: source_dir not found: {source_dir}")
        else:
            # Topic-only mode — no reference video needed
            if not (item.custom_topic or "").strip():
                raise HTTPException(
                    400,
                    f"Item {i}: either source_dir or custom_topic is required",
                )
            source_dir = None  # type: ignore[assignment]

        channel_path = _resolve_channel(item.channel)

        # Per-item style overrides global; fallback to channel config image_style
        resolved_style = item.image_style.strip() or req.image_style.strip()
        if not resolved_style:
            try:
                _cfg = json.loads(channel_path.read_text(encoding="utf-8"))
                resolved_style = (_cfg.get("image_style") or "").strip()
            except Exception:
                resolved_style = ""

        job_specs.append({
            "source_dir": source_dir,
            "channel_config_path": channel_path,
            "kwargs": {
                # Per-item overrides
                "quality":            item.quality,
                "custom_topic":       _sanitize_input(item.custom_topic or ""),
                "image_style":        resolved_style,
                # Pipeline control
                "dry_run":            req.dry_run,
                "draft":              req.draft,
                "from_step":          req.from_step,
                "to_step":            req.to_step,
                "budget":             req.budget_per_video,
                # Script settings
                "template":           req.template,
                "duration_min":       req.duration_min,
                "duration_max":       req.duration_max,
                "master_prompt":      req.master_prompt,
                # Voice / audio
                "voice_id":           req.voice_id,
                "background_music":   req.background_music,
                "music_volume":       req.music_volume,
                "music_track":        req.music_track,
                "burn_subtitles":     req.burn_subtitles,
                # Video
                "skip_thumbnail":     req.skip_thumbnail,
                "no_ken_burns":       req.no_ken_burns,
                # Image
                "image_backend":      req.image_backend,
                "vision_model":       req.vision_model,
                "auto_approve":       req.auto_approve,
                "force":              req.force,
            },
        })

    job_ids = await manager.start_multi_batch(job_specs, parallel=req.parallel)
    return [manager.get(jid).to_response() for jid in job_ids]  # type: ignore[union-attr]


@router.post(
    "/batch/append",
    response_model=list[JobResponse],
    status_code=202,
    summary="Append jobs to the active queue",
    description="Add more pipeline jobs to the currently running queue. New jobs share the existing concurrency semaphore. Falls back to creating a new queue when none is active.",
)
async def append_to_queue(request: Request, req: MultiBatchRequest) -> list[dict]:
    """
    Append more jobs to the currently running queue (shared semaphore).

    Same as /batch/multi but new jobs share the concurrency limit of the
    existing active queue instead of creating a new one.
    If no active queue exists, behaves identically to /batch/multi.
    """
    check_rate_limit(request.client.host if request.client else "unknown")

    if not req.items:
        raise HTTPException(400, "items list is empty")

    job_specs: list[dict] = []
    for i, item in enumerate(req.items):
        source_dir_str = (item.source_dir or "").strip()
        if source_dir_str:
            source_dir = Path(source_dir_str)
            if not source_dir.is_dir():
                raise HTTPException(400, f"Item {i}: source_dir not found: {source_dir}")
        else:
            if not (item.custom_topic or "").strip():
                raise HTTPException(400, f"Item {i}: either source_dir or custom_topic is required")
            source_dir = None  # type: ignore[assignment]

        channel_path = _resolve_channel(item.channel)

        resolved_style = item.image_style.strip() or req.image_style.strip()
        if not resolved_style:
            try:
                _cfg = json.loads(channel_path.read_text(encoding="utf-8"))
                resolved_style = (_cfg.get("image_style") or "").strip()
            except Exception:
                resolved_style = ""

        job_specs.append({
            "source_dir": source_dir,
            "channel_config_path": channel_path,
            "kwargs": {
                "quality":            item.quality,
                "custom_topic":       _sanitize_input(item.custom_topic or ""),
                "image_style":        resolved_style,
                "dry_run":            req.dry_run,
                "draft":              req.draft,
                "from_step":          req.from_step,
                "to_step":            req.to_step,
                "budget":             req.budget_per_video,
                "template":           req.template,
                "duration_min":       req.duration_min,
                "duration_max":       req.duration_max,
                "master_prompt":      req.master_prompt,
                "voice_id":           req.voice_id,
                "background_music":   req.background_music,
                "music_volume":       req.music_volume,
                "music_track":        req.music_track,
                "burn_subtitles":     req.burn_subtitles,
                "skip_thumbnail":     req.skip_thumbnail,
                "no_ken_burns":       req.no_ken_burns,
                "image_backend":      req.image_backend,
                "vision_model":       req.vision_model,
                "auto_approve":       req.auto_approve,
            },
        })

    job_ids = await manager.append_to_queue(job_specs)
    return [manager.get(jid).to_response() for jid in job_ids]  # type: ignore[union-attr]


# ── Job management ────────────────────────────────────────────────────────────

@router.get(
    "/jobs",
    response_model=list[JobResponse],
    summary="List recent jobs",
    description="Return up to `limit` jobs ordered by creation time descending (newest first).",
)
async def list_jobs(limit: int = 50) -> list[dict]:
    """List recent jobs (newest first)."""
    return [j.to_response() for j in manager.list_jobs(limit=limit)]


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get job status",
    description="Return full status, progress log, and metadata for a single job. Returns 404 if the job ID is unknown.",
)
async def get_job(job_id: str) -> dict:
    """Get status and logs for a specific job."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job.to_response()


@router.delete(
    "/jobs/{job_id}",
    status_code=200,
    summary="Cancel a job",
    description="Cancel a running or queued job. Returns 404 if the job does not exist or has already finished.",
)
async def cancel_job(job_id: str) -> dict:
    """Cancel a running or queued job."""
    ok = await manager.cancel(job_id)
    if not ok:
        raise HTTPException(404, f"Job not found or already finished: {job_id}")
    return {"job_id": job_id, "status": "cancelled"}


@router.patch(
    "/jobs/{job_id}/edit-block",
    status_code=200,
    summary="Edit a script block narration",
    description="Overwrite the narration text of a single script block while the job is paused at `waiting_review`. Recalculates and returns the updated word count.",
)
async def edit_script_block(job_id: str, req: BlockEditRequest) -> dict:
    """Edit a single block's narration while job is in script review."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "waiting_review":
        raise HTTPException(400, "Job is not in review state")

    script_path = Path(job.project_dir) / "script.json"
    if not script_path.exists():
        raise HTTPException(404, "script.json not found")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    blocks = script.get("blocks", [])

    found = False
    for block in blocks:
        if block.get("id") == req.block_id:
            block["narration"] = req.narration
            found = True
            break

    if not found:
        raise HTTPException(404, f"Block {req.block_id} not found")

    script_path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")

    # Recalculate word count
    total_words = sum(len((b.get("narration") or "").split()) for b in blocks)
    return {"ok": True, "block_id": req.block_id, "total_words": total_words}


@router.post(
    "/jobs/{job_id}/approve",
    status_code=200,
    summary="Approve a review checkpoint",
    description="Signal approval for a review stage (`script` or `images`) to resume the pipeline. Returns 400 if the job is not in `waiting_review` or the stage has no pending review.",
)
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


@router.post(
    "/jobs/{job_id}/regen-images",
    status_code=200,
    summary="Regenerate failed images",
    description="Re-run the image validator to regenerate any images that failed quality checks. Job must be in `waiting_review` at the `images` stage.",
)
async def regen_failed_images(job_id: str) -> dict:
    """Re-run image validator (regenerate failed images) while job waits for review."""
    import importlib.util
    import json
    from urllib.parse import quote as _url_quote

    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    if job.status != "waiting_review" or job.review_stage != "images":
        raise HTTPException(400, "Job is not waiting for image review")

    rd = job.review_data
    script_path = Path(rd.get("script_path", ""))
    images_dir = Path(rd.get("images_dir", ""))
    source_name = rd.get("source_name", "")

    if not script_path.exists():
        raise HTTPException(400, f"script.json not found: {script_path}")
    if not images_dir.is_dir():
        raise HTTPException(400, f"images dir not found: {images_dir}")

    # Load channel config from script.json metadata
    script_data = json.loads(script_path.read_text(encoding="utf-8"))
    chan_path_str = script_data.get("metadata", {}).get("channel_config", "")
    chan_cfg: dict = {}
    if chan_path_str:
        chan_path = Path(chan_path_str)
        if not chan_path.is_absolute():
            chan_path = ROOT / chan_path
        if chan_path.exists():
            chan_cfg = json.loads(chan_path.read_text(encoding="utf-8"))

    # Dynamic import of 02b_image_validator
    mod_path = ROOT / "modules" / "02b_image_validator.py"
    spec = importlib.util.spec_from_file_location("img_validator", mod_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    validate_and_fix = mod.validate_and_fix_images

    threshold = float(chan_cfg.get("image_validation_threshold", 7.0))

    # Emit progress to WS subscribers
    job.emit(type="regen_started")
    job.log("[Regen] Regenerating failed images…")

    result = await validate_and_fix(
        script_path, images_dir, chan_cfg,
        threshold=threshold,
    )
    val_data = result.to_dict()

    # Add image URLs — use relative path from projects root to include channel subfolder
    try:
        _proj_dir = images_dir.parent
        _rel_parts = _proj_dir.relative_to(ROOT / "projects").parts
        enc_name = "/".join(_url_quote(p, safe="") for p in _rel_parts)
    except ValueError:
        enc_name = _url_quote(source_name, safe="")
    for sc in val_data.get("scores", []):
        idx = sc.get("image_index", 0)
        fname = f"{sc['block_id']}.png" if idx == 0 else f"{sc['block_id']}_{idx}.png"
        sc["image_url"] = f"/projects/{enc_name}/images/{fname}"

    # Update review_data in place and push to WS
    job.review_data["validation"] = val_data
    job.emit(
        type="review_required", stage="images",
        data=job.review_data,
    )
    job.log(
        f"[Regen] Done: {result.ok_count}/{result.total} OK, "
        f"{result.regenerated} regen, {result.failed} failed"
    )

    return {"job_id": job_id, "validation": val_data}


@router.post(
    "/jobs/{job_id}/regen-script",
    status_code=200,
    summary="Regenerate script",
    description="Re-run script generation (step 1) while the job is paused at the script review stage. Updates `review_data` and pushes a new `review_required` WebSocket event.",
)
async def regen_script(job_id: str) -> dict:
    """Re-run script generation (step 1) while job waits at script review."""
    import importlib.util
    import json as _json

    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    if job.status != "waiting_review" or job.review_stage != "script":
        raise HTTPException(400, "Job is not waiting for script review")

    rd = job.review_data
    regen = rd.get("_regen", {})
    script_path = Path(rd.get("script_path", ""))
    channel_config_path = Path(regen.get("channel_config_path", ""))
    source_dir_str = regen.get("source_dir", "") or job.source_dir  # fallback to job.source_dir
    output_dir_str = regen.get("output_dir", "") or job.project_dir or str(script_path.parent)
    quality = regen.get("quality", job.quality or "max")
    template = regen.get("template", "auto")
    duration_min = int(regen.get("duration_min", 8))
    duration_max = int(regen.get("duration_max", 12))
    master_prompt = regen.get("master_prompt", "") or None
    image_style = regen.get("image_style", "")
    custom_topic = regen.get("custom_topic", "") or None

    if not channel_config_path.exists():
        # Fallback: reconstruct from job.channel
        channel_config_path = ROOT / "config" / "channels" / f"{job.channel}.json"
    if not channel_config_path.exists():
        raise HTTPException(400, f"Channel config not found: {channel_config_path}")

    source_dir = Path(source_dir_str) if source_dir_str else None
    output_dir = Path(output_dir_str) if output_dir_str else Path(".")

    # Load channel config
    chan_cfg: dict = {}
    try:
        chan_cfg = _json.loads(channel_config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("regen-script: could not load channel config: %s", exc)

    # Dynamic import of 01_script_generator
    mod_path = ROOT / "modules" / "01_script_generator.py"
    spec = importlib.util.spec_from_file_location("script_gen", mod_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    generate_scripts = mod.generate_scripts

    job.emit(type="regen_started", stage="script")
    job.log("[Regen] Regenerating script…")

    try:
        script_paths = await generate_scripts(
            source_dir,
            channel_config_path,
            template=template,
            preset=quality,
            dry_run=False,
            output_dir=output_dir,
            duration_min=duration_min,
            duration_max=duration_max,
            master_prompt_path=master_prompt,
            image_style=image_style,
            custom_topic=custom_topic or "",
        )
    except Exception as exc:
        job.log(f"[Regen] Script generation failed: {exc}")
        raise HTTPException(500, f"Script generation failed: {exc}")

    if not script_paths:
        raise HTTPException(500, "generate_scripts returned empty list")

    new_script_path = script_paths[0]

    # Run validator + image planner (non-fatal)
    try:
        val_mod_path = ROOT / "modules" / "01b_script_validator.py"
        val_spec = importlib.util.spec_from_file_location("script_val", val_mod_path)
        val_mod = importlib.util.module_from_spec(val_spec)  # type: ignore
        val_spec.loader.exec_module(val_mod)  # type: ignore
        await val_mod.validate_and_fix_script(new_script_path, chan_cfg)
        job.log("[Regen] Script validated")
    except Exception as vexc:
        log.warning("regen-script: validator failed (non-fatal): %s", vexc)

    try:
        plan_mod_path = ROOT / "modules" / "01c_image_planner.py"
        plan_spec = importlib.util.spec_from_file_location("img_plan", plan_mod_path)
        plan_mod = importlib.util.module_from_spec(plan_spec)  # type: ignore
        plan_spec.loader.exec_module(plan_mod)  # type: ignore
        await plan_mod.plan_images(new_script_path, chan_cfg, preset_name="high", image_style=image_style or "")
        job.log("[Regen] Image plan updated")
    except Exception as pexc:
        log.warning("regen-script: image planner failed (non-fatal): %s", pexc)

    # Recompute review stats
    sd = _json.loads(new_script_path.read_text(encoding="utf-8"))
    blocks = sd.get("blocks", [])
    word_count = sum(len((b.get("narration") or "").split()) for b in blocks)
    dur_min = round(word_count / 150, 1)
    dur_max = round(word_count / 130, 1)
    type_counts: dict[str, int] = {}
    for b in blocks:
        t = b.get("type", "section")
        type_counts[t] = type_counts.get(t, 0) + 1
    total_imgs = sum(
        len(b.get("image_prompts") or []) or (1 if (b.get("image_prompt") or "").strip() else 0)
        for b in blocks
    )
    intro_blocks = [b for b in blocks if b.get("type") == "intro"]
    has_hook = False
    if intro_blocks:
        hook_meta = intro_blocks[0].get("hook")
        if isinstance(hook_meta, dict):
            score = hook_meta.get("validation_score")
            has_hook = score is None or score >= 3
        else:
            intro_text = (intro_blocks[0].get("narration") or "").strip()
            has_hook = bool(intro_text)

    new_review_data = {
        **rd,
        "script_path":        str(new_script_path),
        "title":              sd.get("title", ""),
        "block_count":        len(blocks),
        "word_count":         word_count,
        "duration_min":       dur_min,
        "duration_max":       dur_max,
        "type_counts":        type_counts,
        "image_prompt_count": total_imgs,
        "has_hook":           has_hook,
        "blocks": [
            {
                "id":          b.get("id", ""),
                "type":        b.get("type", "section"),
                "title":       b.get("title", ""),
                "word_count":  len((b.get("narration") or "").split()),
                "image_count": len(b.get("image_prompts") or []) or (1 if (b.get("image_prompt") or "").strip() else 0),
                "narration":   (b.get("narration") or "")[:120],
            }
            for b in blocks
        ],
    }

    job.review_data = new_review_data
    job.emit(type="review_required", stage="script", data=new_review_data)
    job.log(f"[Regen] Script regenerated: {word_count} words, {len(blocks)} blocks")

    return {"job_id": job_id, "word_count": word_count, "block_count": len(blocks)}


# ── Project Folders ───────────────────────────────────────────────────────────

@router.get(
    "/projects/folders",
    summary="List channel project folders",
    description="List top-level channel subfolders under `projects/`. Only folders that have a matching channel config are returned. Each entry includes the video count (directories containing a compiled `final.mp4`).",
)
async def list_project_folders() -> list[dict]:
    """
    List true channel subfolders in projects/ (new-style: projects/{channel}/{title}/).
    Skips old-style video folders (projects/{title}/output/final.mp4).
    Returns: name, has_config, video_count (completed videos inside).
    """
    projects_dir = ROOT / "projects"
    projects_dir.mkdir(exist_ok=True)
    result: list[dict] = []
    for d in sorted(projects_dir.iterdir()):
        if not d.is_dir():
            continue
        # Skip old-format video folders: they have output/ directly inside
        if (d / "output").is_dir():
            continue
        has_config = (ROOT / "config" / "channels" / f"{d.name}.json").exists()
        if not has_config:
            continue
        # Count sub-project folders that have a compiled final.mp4
        video_count = sum(
            1 for v in d.iterdir()
            if v.is_dir() and (v / "output" / "final.mp4").exists()
        )
        result.append({
            "name": d.name,
            "has_config": has_config,
            "video_count": video_count,
        })
    return result


# ── Metrics ───────────────────────────────────────────────────────────────────

@router.get(
    "/metrics/scripts",
    summary="Script generation metrics",
    description="Return recent script generation records for A/B analysis. Max 200 records per call.",
)
async def get_script_metrics(limit: int = 50) -> list[dict]:
    """Get recent script generation metrics for A/B analysis."""
    from utils.db import VideoTracker
    return VideoTracker().get_script_metrics(limit=min(limit, 200))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_channel(channel: str) -> Path:
    p = Path(channel)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise HTTPException(400, f"Channel config not found: {p}")
    return p
