"""
VideoForge Backend — Pydantic models for request/response schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ─── Request schemas ──────────────────────────────────────────────────────────

class PipelineRunRequest(BaseModel):
    """Start a single-video pipeline run."""
    source_dir: str = Field(..., description="Transcriber output directory path")
    channel: str = Field("config/channels/history.json", description="Channel config JSON path")
    quality: str = Field("max", description="LLM quality preset (max/high/balanced/bulk/test)")
    template: str = Field("auto", description="Content template (auto/documentary/listicle/tutorial/comparison)")
    draft: bool = Field(False, description="Generate 480p preview without effects")
    from_step: int = Field(1, ge=1, le=6, description="Resume from step N (1-6)")
    budget: float | None = Field(None, description="Max spend in USD")
    langs: list[str] | None = Field(None, description="Language codes for multilingual output")
    dry_run: bool = Field(False, description="Estimate costs only, no API calls")
    background_music: bool = Field(False, description="Mix royalty-free background music at -20dB")
    no_ken_burns: bool = Field(False, description="Static slideshow instead of Ken Burns (1 FFmpeg call, much faster)")
    image_style: str | None = Field(None, description="Override image generation style prompt")
    voice_id: str | None = Field(None, description="Override voice ID from channel config")
    master_prompt: str | None = Field(None, description="Override master prompt path (e.g. 'prompts/master_script_v2.txt')")


class BatchRunRequest(BaseModel):
    """Start a batch run over a Transcriber output directory."""
    input_dir: str = Field(..., description="Root directory containing Transcriber output subdirs")
    channel: str = Field("config/channels/history.json", description="Channel config JSON path")
    quality: str = Field("bulk", description="LLM quality preset (default: bulk for batch)")
    parallel: int = Field(1, ge=1, le=8, description="Max simultaneous pipeline runs")
    draft: bool = Field(False, description="Generate 480p previews")
    from_step: int = Field(1, ge=1, le=6, description="Resume all videos from step N")
    budget_per_video: float | None = Field(None, description="Max spend per video in USD")
    budget_total: float | None = Field(None, description="Max total batch spend in USD")
    skip_done: bool = Field(True, description="Skip videos that already have final.mp4")
    dry_run: bool = Field(False, description="Estimate costs only, no API calls")


# ─── Job response ─────────────────────────────────────────────────────────────

class JobResponse(BaseModel):
    """Status of a pipeline or batch job."""
    job_id: str
    kind: str            # "pipeline" | "batch"
    status: str          # "queued" | "running" | "done" | "failed" | "cancelled"
    source: str
    channel: str
    quality: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    elapsed: float | None
    step: int
    step_name: str
    error: str
    logs: list[str]
    db_video_id: int | None


# ─── Video list / detail ──────────────────────────────────────────────────────

class VideoItem(BaseModel):
    id: int
    status: str
    source_title: str | None
    source_dir: str
    channel: str
    quality_preset: str
    template: str | None
    created_at: str
    elapsed_seconds: float | None
    youtube_url: str | None
    error_message: str | None


class CostItem(BaseModel):
    step: str
    model: str
    input_tokens: int
    output_tokens: int
    units: float
    unit_label: str
    cost_usd: float
    recorded_at: str


class VideoDetail(BaseModel):
    video: dict
    costs: list[dict]
    total_cost_usd: float


class StatsResponse(BaseModel):
    total_videos: int
    done: int
    failed: int
    running: int
    avg_elapsed: float | None
    cost_total_usd: float
    by_model: list[dict]
    by_preset: list[dict]
