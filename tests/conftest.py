"""Shared test fixtures for VideoForge."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure project root is in path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env for tests (optional — tests should work without real API keys)
os.environ.setdefault("VOIDAI_API_KEY", "test-key-not-real")
os.environ.setdefault("WAVESPEED_API_KEY", "test-key-not-real")
os.environ.setdefault("VOICEAPI_KEY", "test-key-not-real")


@pytest.fixture
def test_data_dir() -> Path:
    return ROOT / "tests" / "test_data"


@pytest.fixture
def sample_script(test_data_dir) -> dict:
    """Load the sample script.json fixture."""
    return json.loads((test_data_dir / "script_full.json").read_text(encoding="utf-8"))


@pytest.fixture
def sample_blocks(sample_script) -> list[dict]:
    """Get blocks from sample script."""
    return sample_script["blocks"]


@pytest.fixture
def sample_config(test_data_dir) -> dict:
    """Load sample channel config."""
    return json.loads((test_data_dir / "sample_config.json").read_text(encoding="utf-8"))


@pytest.fixture
def sample_transcriber_output(test_data_dir) -> Path:
    """Path to sample transcriber output directory."""
    return test_data_dir / "sample_transcriber_output"


@pytest.fixture
def mock_voidai_client():
    """Mock VoidAI client that returns predictable responses."""
    client = AsyncMock()
    client.last_finish_reason = "stop"
    client.chat_completion = AsyncMock(return_value="[SECTION 1: Test Hook]\n\nThis is test narration content.")
    return client


@pytest.fixture
def tmp_project(tmp_path) -> Path:
    """Create a temporary project directory structure."""
    proj = tmp_path / "test_project"
    proj.mkdir()
    (proj / "images").mkdir()
    (proj / "audio").mkdir()
    (proj / "output").mkdir()
    (proj / "subtitles").mkdir()
    return proj
