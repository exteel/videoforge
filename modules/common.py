"""
VideoForge — common utilities used by all modules.

Provides:
    - Logging setup
    - .env loading
    - Channel config loader
    - Transcriber output loader
    - Project directory helpers
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ─── Root paths ──────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
PROJECTS_DIR = ROOT / "projects"
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"


# ─── Logging ─────────────────────────────────────────────────────────────────

def setup_logging(name: str = "videoforge", level: int = logging.INFO) -> logging.Logger:
    """Configure and return a module logger with consistent formatting."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured — don't add duplicate handlers

    # Use UTF-8 on Windows to avoid cp1252 UnicodeEncodeError
    stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


log = setup_logging()


# ─── Environment ─────────────────────────────────────────────────────────────

def load_env(env_path: Path | None = None) -> None:
    """Load .env file. Falls back to ROOT/.env if path not specified."""
    path = env_path or ROOT / ".env"
    if path.exists():
        load_dotenv(dotenv_path=path, override=False)
        log.debug("Loaded .env from %s", path)
    else:
        log.debug(".env not found at %s — using system env vars", path)


def require_env(key: str) -> str:
    """Return env var value or raise RuntimeError if missing/empty."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example → .env and fill in values."
        )
    return value


# ─── Channel config ───────────────────────────────────────────────────────────

def load_channel_config(path: str | Path) -> dict[str, Any]:
    """
    Load and return a channel configuration JSON file.

    Args:
        path: Path to channel config JSON (e.g. config/channels/history.json)

    Returns:
        Parsed channel config dict.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not valid JSON.
    """
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"Channel config not found: {p}")
    try:
        config = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in channel config {p}: {exc}") from exc

    log.debug("Loaded channel config: %s", p.name)
    return config


def get_llm_preset(channel_config: dict[str, Any], preset: str | None = None) -> dict[str, str]:
    """
    Return the LLM model preset dict from channel config.

    Args:
        channel_config: Loaded channel config dict.
        preset: Preset name (max/high/balanced/bulk/test). Defaults to channel default.

    Returns:
        Dict with keys: script, metadata, thumbnail.
    """
    llm = channel_config.get("llm", {})
    default_preset = llm.get("default_preset", "max")
    name = preset or default_preset
    presets = llm.get("presets", {})
    if name not in presets:
        available = list(presets.keys())
        raise ValueError(f"Unknown LLM preset '{name}'. Available: {available}")
    return presets[name]


# ─── Transcriber output ───────────────────────────────────────────────────────

# Files produced by the Transcriber tool for each video
_TRANSCRIBER_FILES = {
    "transcript": "transcript.txt",
    "transcript_srt": "transcript.srt",
    "metadata": "metadata.json",
    "title": "title.txt",
    "description": "description.txt",
    "thumbnail": "thumbnail.jpg",
    "thumbnail_prompt": "thumbnail_prompt.txt",
}


def load_transcriber_output(path: str | Path) -> dict[str, Any]:
    """
    Load all available Transcriber output files from a directory.

    Args:
        path: Path to Transcriber output directory for a single video.

    Returns:
        Dict with keys matching _TRANSCRIBER_FILES (missing files → None).
        Parsed metadata.json is returned as a dict under 'metadata'.
        All text files are stripped of leading/trailing whitespace.

    Raises:
        FileNotFoundError: If the directory does not exist.
        ValueError: If metadata.json exists but is not valid JSON.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Transcriber output directory not found: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Expected a directory, got: {p}")

    result: dict[str, Any] = {"source_dir": str(p)}

    for key, filename in _TRANSCRIBER_FILES.items():
        file_path = p / filename
        if not file_path.exists():
            result[key] = None
            log.debug("Transcriber file missing (ok): %s", filename)
            continue

        if filename.endswith(".json"):
            try:
                result[key] = json.loads(file_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {file_path}: {exc}") from exc
        elif filename.endswith((".jpg", ".png")):
            result[key] = str(file_path)  # Image → path string
        else:
            result[key] = file_path.read_text(encoding="utf-8").strip()

    log.info(
        "Loaded Transcriber output: %s (%d/%d files present)",
        p.name,
        sum(1 for v in result.values() if v is not None and v != str(p)),
        len(_TRANSCRIBER_FILES),
    )
    return result


# ─── Project directories ──────────────────────────────────────────────────────

_PROJECT_SUBDIRS = ("input", "images", "audio", "subtitles", "output")


def get_project_dir(channel: str, video_id: str) -> Path:
    """Return the project directory path for a given channel + video_id."""
    return PROJECTS_DIR / channel / video_id


def ensure_project_dirs(channel: str, video_id: str) -> Path:
    """
    Create and return the project directory with all required sub-folders.

    Sub-folders: input/, images/, audio/, subtitles/, output/

    Returns:
        Path to the project root directory.
    """
    project_dir = get_project_dir(channel, video_id)
    for sub in _PROJECT_SUBDIRS:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)
    log.debug("Project dirs ready: %s", project_dir)
    return project_dir


# ─── Settings ─────────────────────────────────────────────────────────────────

def load_settings() -> dict[str, Any]:
    """Load global settings from config/settings.json."""
    path = CONFIG_DIR / "settings.json"
    if not path.exists():
        log.warning("config/settings.json not found — using empty settings")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in settings.json: {exc}") from exc


# ─── CLI self-test ────────────────────────────────────────────────────────────

def _self_test() -> None:
    """Quick sanity-check when run directly: python modules/common.py"""
    import argparse

    parser = argparse.ArgumentParser(description="VideoForge common — self-test")
    parser.add_argument(
        "--channel",
        default="config/channels/example_history.json",
        help="Path to channel config JSON",
    )
    parser.add_argument(
        "--transcriber-dir",
        help="Path to a Transcriber output directory to test loading",
    )
    args = parser.parse_args()

    load_env()
    log.info("ROOT: %s", ROOT)

    # Settings
    settings = load_settings()
    log.info("Settings loaded: %d keys", len(settings))

    # Channel config
    try:
        cfg = load_channel_config(args.channel)
        log.info("Channel config '%s': niche=%s, language=%s",
                 cfg.get("channel_name", "?"), cfg.get("niche", "?"), cfg.get("language", "?"))

        preset = get_llm_preset(cfg)
        log.info("Default LLM preset '%s': script=%s, metadata=%s",
                 cfg["llm"]["default_preset"], preset["script"], preset["metadata"])

        for p_name in ["max", "high", "balanced", "bulk", "test"]:
            p_data = get_llm_preset(cfg, p_name)
            log.info("  preset %-8s | script=%-30s metadata=%s",
                     p_name, p_data["script"], p_data["metadata"])
    except FileNotFoundError as exc:
        log.warning("Channel config not found: %s", exc)

    # Transcriber output (optional)
    if args.transcriber_dir:
        output = load_transcriber_output(args.transcriber_dir)
        log.info("Transcriber keys: %s", [k for k, v in output.items() if v is not None])

    # Project dirs
    project = ensure_project_dirs("test_channel", "test_video_001")
    log.info("Project dir: %s (exists=%s)", project, project.exists())

    log.info("common.py self-test OK")


if __name__ == "__main__":
    _self_test()
