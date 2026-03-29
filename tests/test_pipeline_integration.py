"""Integration test — pipeline flow with mocked APIs."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def mock_channel_config(tmp_path):
    """Create a minimal channel config for testing."""
    config = {
        "channel_name": "Test",
        "niche": "history",
        "language": "en",
        "voice_id": "test_voice",
        "image_style": "cinematic test style",
        "master_prompt_path": "prompts/master_script_v4.txt",
        "llm": {
            "default_preset": "test",
            "presets": {
                "test": {
                    "script": "test-model",
                    "metadata": "test-model",
                    "thumbnail": "test-model",
                }
            },
        },
        "tts": {"provider": "voiceapi"},
        "images": {"provider": "wavespeed"},
        "subtitle_style": {
            "font": "Arial",
            "size": 48,
            "color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 3,
            "position": "bottom",
            "margin_v": 60,
        },
    }
    config_path = tmp_path / "test_channel.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


@pytest.fixture
def mock_script_json():
    """Return a valid script.json content with enough blocks."""
    return json.loads(
        (ROOT / "tests" / "test_data" / "script_full.json").read_text(encoding="utf-8")
    )


class TestPipelineQualityGate:
    """Test the script quality gate catches short scripts."""

    @pytest.mark.asyncio
    async def test_quality_gate_rejects_short_script(self, tmp_path, mock_channel_config):
        """
        Quality gate should raise RuntimeError when the generated script is too short.

        The gate fires after Step 1 runs (not auto-skipped).  We mock
        generate_scripts to write a deliberately tiny script into the project dir,
        then let the gate evaluate it.
        """
        from pipeline import run_pipeline

        short_script = {
            "blocks": [
                {
                    "id": "block_001",
                    "type": "intro",
                    "narration": "Short intro.",
                    "image_prompt": "test",
                },
                {
                    "id": "block_002",
                    "type": "cta",
                    "narration": "Subscribe.",
                    "image_prompt": "test",
                },
            ],
            "duration_min": 30,
            "duration_max": 40,
        }

        proj = tmp_path / "project"
        proj.mkdir()

        # generate_scripts must write script.json and return the path.
        # We write it ourselves inside the mock to simulate what the real module does.
        async def _fake_generate(source_dir, channel_cfg_path, **kwargs):
            out = proj / "script.json"
            out.write_text(json.dumps(short_script), encoding="utf-8")
            return [out]

        # Also mock the downstream sub-steps that run before the quality gate:
        # 01b_script_validator and 01c_image_planner.
        async def _fake_validate(script_path, chan_cfg, **kwargs):
            result = MagicMock()
            result.issues = []
            result.fixes_applied = []
            return result

        async def _fake_plan_images(script_path, chan_cfg, **kwargs):
            return None

        def _fn_patcher(rel_path, fn_name):
            mapping = {
                ("modules/01_script_generator.py", "generate_scripts"): _fake_generate,
                ("modules/01b_script_validator.py", "validate_and_fix_script"): _fake_validate,
                ("modules/01c_image_planner.py", "plan_images"): _fake_plan_images,
            }
            return mapping.get((rel_path, fn_name), MagicMock())

        with patch("pipeline._fn", side_effect=_fn_patcher):
            with pytest.raises(RuntimeError, match="quality gate"):
                await run_pipeline(
                    source_dir=tmp_path / "source",  # exists check not needed for custom_topic path
                    channel_config_path=mock_channel_config,
                    project_dir=proj,
                    from_step=1,
                    to_step=1,
                    duration_min=30,
                    duration_max=40,
                    image_style="test style",
                    custom_topic="Test Topic",
                )


class TestCostBudgetIntegration:
    """Test cost budget stops pipeline cleanly."""

    @pytest.mark.asyncio
    async def test_budget_exceeded_raises_not_exits(self, tmp_path, mock_channel_config):
        """Budget exceeded should raise RuntimeError, NOT call sys.exit."""
        from pipeline import CostBudget

        budget = CostBudget(limit=0.01)
        budget.add("test", 100.0)  # way over budget

        with pytest.raises(RuntimeError, match="Budget exceeded"):
            budget.check()

    def test_budget_not_exceeded_within_limit(self):
        """Budget check should be silent when under limit."""
        from pipeline import CostBudget

        budget = CostBudget(limit=10.0)
        budget.add("test", 5.0)
        # Should not raise
        budget.check()

    def test_budget_no_limit_never_raises(self):
        """With no limit set, budget.check() should never raise regardless of spend."""
        from pipeline import CostBudget

        budget = CostBudget(limit=None)
        budget.add("test", 999.0)
        budget.check()  # must not raise

    def test_over_budget_flag(self):
        """over_budget() reflects spend vs limit correctly."""
        from pipeline import CostBudget

        budget = CostBudget(limit=1.0)
        assert budget.over_budget() is False
        budget.add("x", 2.0)
        assert budget.over_budget() is True


class TestTranscriptCache:
    """Test transcript caching by video_id."""

    def test_cache_miss_returns_none(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")
        result = db.get_cached_transcription("nonexistent_id_12345")
        assert result is None

    def test_cache_roundtrip(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")

        # Directory must exist for get_cached_transcription to return a value
        out_dir = tmp_path / "cached_output"
        out_dir.mkdir()

        db.cache_transcription(
            "test_vid_001", "https://youtube.com/test", "Test Video", str(out_dir)
        )
        result = db.get_cached_transcription("test_vid_001")
        assert result == str(out_dir)

    def test_cache_stale_dir_returns_none(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")

        # Cache a path that doesn't exist — should return None
        db.cache_transcription(
            "test_vid_stale",
            "https://youtube.com/test",
            "Stale",
            "/nonexistent/path/xyz",
        )
        result = db.get_cached_transcription("test_vid_stale")
        assert result is None  # dir doesn't exist → cache miss

    def test_cache_upsert_overwrites(self, tmp_path):
        """Re-caching same video_id should update the record."""
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")

        dir_v1 = tmp_path / "v1"
        dir_v1.mkdir()
        dir_v2 = tmp_path / "v2"
        dir_v2.mkdir()

        db.cache_transcription("vid_upsert", "https://youtube.com/u", "Title", str(dir_v1))
        db.cache_transcription("vid_upsert", "https://youtube.com/u", "Title", str(dir_v2))

        result = db.get_cached_transcription("vid_upsert")
        assert result == str(dir_v2)


class TestScriptMetrics:
    """Test A/B metrics recording."""

    def test_record_and_retrieve_metrics(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")

        mid = db.record_script_metrics(
            video_id=None,
            model="test-model",
            template="auto",
            prompt_version="v4",
            temperature=0.7,
            word_count=5000,
            block_count=20,
            hook_score=4,
            duration_est_min=29.4,
        )
        assert mid > 0

        metrics = db.get_script_metrics(limit=5)
        assert len(metrics) >= 1
        latest = metrics[0]
        assert latest["model"] == "test-model"
        assert latest["word_count"] == 5000

    def test_metrics_limit_respected(self, tmp_path):
        """get_script_metrics(limit=N) should return at most N rows."""
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")

        for i in range(10):
            db.record_script_metrics(
                video_id=None,
                model=f"model-{i}",
                template="auto",
                prompt_version="v4",
                temperature=0.7,
                word_count=100 * i,
                block_count=i,
            )

        result = db.get_script_metrics(limit=3)
        assert len(result) == 3

    def test_metrics_ordered_newest_first(self, tmp_path):
        """Returned rows should be in descending id order (newest first)."""
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")

        db.record_script_metrics(
            video_id=None, model="first", template="auto",
            prompt_version="v4", temperature=0.7,
            word_count=100, block_count=5,
        )
        db.record_script_metrics(
            video_id=None, model="second", template="auto",
            prompt_version="v4", temperature=0.7,
            word_count=200, block_count=10,
        )

        metrics = db.get_script_metrics(limit=10)
        ids = [m["id"] for m in metrics]
        assert ids == sorted(ids, reverse=True)


class TestJobPersistence:
    """Test job state persistence columns exist and operate correctly."""

    def test_update_job_progress(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")
        vid = db.create_video(
            source_dir=Path("/test"),
            channel="test",
            quality_preset="test",
            template="auto",
            from_step=1,
            project_dir=Path("/test/proj"),
        )
        # Should not raise
        db.update_job_progress(vid, step=2, step_name="Voice", pct=50.0)

    def test_save_pipeline_kwargs(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")
        vid = db.create_video(
            source_dir=Path("/test"),
            channel="test",
            quality_preset="test",
            template="auto",
            from_step=1,
            project_dir=Path("/test/proj"),
        )
        db.save_pipeline_kwargs(vid, '{"quality": "max"}')

    def test_get_resumable_jobs(self, tmp_path):
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")
        jobs = db.get_resumable_jobs()
        assert isinstance(jobs, list)

    def test_resumable_jobs_includes_running_video(self, tmp_path):
        """A video set to running status should appear in get_resumable_jobs."""
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")
        vid = db.create_video(
            source_dir=Path("/test/src"),
            channel="history",
            quality_preset="max",
            template="auto",
            from_step=1,
            project_dir=Path("/test/proj"),
        )
        db.set_running(vid)
        db.update_job_progress(vid, step=2, step_name="Images + Voices", pct=20.0)
        db.save_pipeline_kwargs(vid, '{"draft": false}')

        jobs = db.get_resumable_jobs()
        ids = [j["id"] for j in jobs]
        assert vid in ids

    def test_done_job_not_in_resumable(self, tmp_path):
        """Completed jobs must not appear in the resumable list."""
        from utils.db import VideoTracker

        db = VideoTracker(db_path=tmp_path / "test.db")
        vid = db.create_video(
            source_dir=Path("/test/src"),
            channel="history",
            quality_preset="max",
            template="auto",
            from_step=1,
            project_dir=Path("/test/proj"),
        )
        db.set_running(vid)
        db.set_done(vid, elapsed_seconds=42.0)

        jobs = db.get_resumable_jobs()
        ids = [j["id"] for j in jobs]
        assert vid not in ids
