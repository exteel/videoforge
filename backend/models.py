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
    to_step: int = Field(6, ge=1, le=6, description="Stop after step N (1-6). Use with from_step to run a single step")
    budget: float | None = Field(None, description="Max spend in USD")
    langs: list[str] | None = Field(None, description="Language codes for multilingual output")
    dry_run: bool = Field(False, description="Estimate costs only, no API calls")
    background_music: bool = Field(True, description="Mix royalty-free background music under voice")
    no_ken_burns: bool = Field(False, description="Static slideshow instead of Ken Burns (1 FFmpeg call, much faster)")
    skip_thumbnail: bool = Field(False, description="Skip thumbnail generation (Step 5)")
    burn_subtitles: bool = Field(True, description="Burn generated subtitles into video (Step 4 must have run)")
    image_style: str | None = Field(None, description="Override image generation style prompt")
    voice_id: str | None = Field(None, description="Override voice ID from channel config")
    master_prompt: str | None = Field(None, description="Override master prompt path (e.g. 'prompts/master_script_v2.txt')")
    duration_min: int | None = Field(None, ge=1, le=240, description="Minimum target video duration in minutes")
    duration_max: int | None = Field(None, ge=1, le=240, description="Maximum target video duration in minutes")
    music_volume: float | None = Field(
        None, ge=-60, le=0,
        description="BGM volume override in dB (e.g. -28). None = channel config default (-28).",
    )
    music_track: str | None = Field(
        None,
        description="Explicit music track path (absolute). None = channel config random pick.",
    )
    custom_topic: str | None = Field(
        None,
        description="Override topic for the new script (replaces reference video title). "
                    "Leave empty to use the reference video's title as the topic.",
    )
    image_backend: str | None = Field(
        None,
        description="Image generation provider: 'wavespeed' (default) | 'betatest' | 'voidai'",
    )
    vision_model: str | None = Field(
        None,
        description="Vision model for image analysis/validation: 'gpt-4.1' (default) | 'gpt-4.1-mini'",
    )


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


class MultiTopicItem(BaseModel):
    """One video in a multi-topic queue."""
    source_dir: str = Field(
        "",
        description="Transcriber output directory for this video. "
                    "Leave empty for topic-only mode (custom_topic required).",
    )
    channel: str = Field("config/channels/history.json", description="Channel config JSON path")
    custom_topic: str = Field("", description="Override topic (leave empty to use reference title)")
    quality: str = Field("max", description="LLM quality preset for this video")
    image_style: str = Field("", description="Image style override (empty = use global or channel default)")


class MultiBatchRequest(BaseModel):
    """Start multiple pipeline jobs from a topic queue, running up to `parallel` at once."""
    items: list[MultiTopicItem] = Field(..., min_length=1, description="List of videos to generate")
    parallel: int = Field(2, ge=1, le=8, description="Max simultaneous pipeline runs")
    image_style: str = Field("", description="Global image style (overridden by per-item style if set)")
    dry_run: bool = Field(False, description="Estimate costs only, no API calls")
    draft: bool = Field(False, description="Generate 480p previews without effects")
    from_step: int = Field(1, ge=1, le=6, description="Resume all videos from step N")
    to_step: int = Field(6, ge=1, le=6, description="Stop after step N (1-6)")
    budget_per_video: float | None = Field(None, description="Max spend per video in USD")
    # Script settings (applied to all items)
    template: str = Field("auto", description="Content template (auto/documentary/listicle/tutorial/comparison)")
    duration_min: int = Field(8, ge=1, le=240, description="Minimum target video duration in minutes")
    duration_max: int = Field(12, ge=1, le=240, description="Maximum target video duration in minutes")
    master_prompt: str | None = Field(None, description="Override master prompt path for all videos")
    # Voice / audio settings
    voice_id: str | None = Field(None, description="Override voice ID for all videos")
    background_music: bool = Field(True, description="Mix royalty-free background music under voice")
    music_volume: float | None = Field(None, ge=-60, le=0, description="BGM volume override in dB")
    music_track: str | None = Field(None, description="Explicit music track path (absolute)")
    burn_subtitles: bool = Field(True, description="Burn generated subtitles into video")
    # Video settings
    skip_thumbnail: bool = Field(False, description="Skip thumbnail generation (Step 5)")
    no_ken_burns: bool = Field(False, description="Static slideshow — no Ken Burns effect (faster)")
    # Image settings
    image_backend: str | None = Field(None, description="Image generation provider: 'wavespeed' (default) | 'betatest' | 'voidai'")
    vision_model: str | None = Field(None, description="Vision model for image analysis/validation: 'gpt-4.1' (default) | 'gpt-4.1-mini'")


# ─── Job response ─────────────────────────────────────────────────────────────

class JobResponse(BaseModel):
    """Status of a pipeline or batch job."""
    job_id: str
    kind: str            # "pipeline" | "batch"
    status: str          # "queued" | "running" | "waiting_review" | "done" | "failed" | "cancelled"
    source: str
    source_dir: str = ""       # full path to transcriber output dir
    project_dir: str = ""      # full path to videoforge project output dir
    channel: str
    quality: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    elapsed: float | None
    step: int
    step_name: str
    pct: float = 0.0
    error: str
    logs: list[str]
    db_video_id: int | None
    review_stage: str | None = None


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
