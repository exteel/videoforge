"""Pydantic models for VideoForge configuration validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SubtitleStyle(BaseModel):
    font: str = "Arial Bold"
    size: int = 48
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 3
    position: str = "bottom"
    margin_v: int = 60


class LLMPreset(BaseModel):
    script: str = "claude-opus-4-6"
    metadata: str = "gpt-4.1-mini"
    thumbnail: str = "gpt-4.1"


class LLMConfig(BaseModel):
    default_preset: str = "max"
    presets: dict[str, LLMPreset] = Field(default_factory=dict)


class TTSConfig(BaseModel):
    provider: str = "voiceapi"
    fallback: str = "tts-1-hd"


class ImageConfig(BaseModel):
    provider: str = "wavespeed"
    fallback: str = "gpt-image-1.5"


class MusicConfig(BaseModel):
    tracks_dir: str = "assets/music"
    random: bool = False
    volume_db: float = -20


class BrandingConfig(BaseModel):
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    country: str = "UA"
    banner_path: str = ""
    trailer_video_id: str = ""


class ChannelConfig(BaseModel):
    """Validated channel configuration."""

    channel_name: str
    niche: str = "general"
    language: str = "en"
    voice_id: str = ""
    image_style: str = ""
    thumbnail_style: str = ""
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    default_animation: str = "zoom_in"
    master_prompt_path: str = "prompts/master_script_v4.txt"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    images: ImageConfig = Field(default_factory=ImageConfig)
    transcriber_output_dir: str = ""
    background_music: MusicConfig = Field(default_factory=MusicConfig)
    intro_video: str = ""
    outro_video: str = ""
    branding: BrandingConfig = Field(default_factory=BrandingConfig)

    @field_validator("master_prompt_path")
    @classmethod
    def validate_prompt_path(cls, v: str) -> str:
        if v and not Path(v).suffix == ".txt":
            raise ValueError(f"master_prompt_path must be a .txt file: {v}")
        return v

    @classmethod
    def from_json(cls, path: str | Path) -> "ChannelConfig":
        """Load and validate channel config from JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)
