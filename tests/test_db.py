"""
Unit tests for utils/db.py — VideoTracker SQLite persistence layer.

All tests use tmp_path for isolated SQLite instances (no shared state).
No real API calls; no external filesystem dependencies.
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.db import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    VideoTracker,
    _fmt_dur,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_tracker(tmp_path) -> VideoTracker:
    return VideoTracker(db_path=tmp_path / "test.db")


def _create_video(tracker: VideoTracker, **kwargs) -> int:
    defaults = dict(source_dir="/src/test", channel="history")
    defaults.update(kwargs)
    return tracker.create_video(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# _fmt_dur helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestFmtDur:

    def test_none_returns_dash(self):
        assert _fmt_dur(None) == "-"

    def test_seconds_only(self):
        assert _fmt_dur(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _fmt_dur(90) == "1m 30s"

    def test_hours_minutes_seconds(self):
        assert _fmt_dur(3661) == "1h 1m 1s"

    def test_zero_seconds(self):
        assert _fmt_dur(0) == "0s"

    def test_exactly_one_minute(self):
        assert _fmt_dur(60) == "1m 0s"

    def test_exactly_one_hour(self):
        assert _fmt_dur(3600) == "1h 0m 0s"


# ═══════════════════════════════════════════════════════════════════════════════
# VideoTracker initialisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestVideoTrackerInit:

    def test_creates_db_file(self, tmp_path):
        db = tmp_path / "sub" / "vf.db"
        VideoTracker(db_path=db)
        assert db.exists()

    def test_tables_created(self, tmp_path):
        import sqlite3
        tracker = _make_tracker(tmp_path)
        conn = sqlite3.connect(str(tracker.db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "videos" in tables
        assert "costs" in tables
        assert "transcription_cache" in tables
        assert "script_metrics" in tables

    def test_reinit_does_not_lose_data(self, tmp_path):
        """Creating a second tracker on the same DB does not wipe existing rows."""
        tracker1 = _make_tracker(tmp_path)
        vid_id = _create_video(tracker1)
        tracker2 = _make_tracker(tmp_path)
        assert tracker2.get_video(vid_id) is not None


# ═══════════════════════════════════════════════════════════════════════════════
# create_video
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateVideo:

    def test_returns_integer_id(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        assert isinstance(vid_id, int)
        assert vid_id > 0

    def test_initial_status_is_pending(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        v = tracker.get_video(vid_id)
        assert v is not None
        assert v["status"] == STATUS_PENDING

    def test_channel_stored(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker, channel="science")
        assert tracker.get_video(vid_id)["channel"] == "science"

    def test_quality_preset_stored(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = tracker.create_video("/src", "history", quality_preset="high")
        assert tracker.get_video(vid_id)["quality_preset"] == "high"

    def test_default_quality_preset_is_max(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        assert tracker.get_video(vid_id)["quality_preset"] == "max"

    def test_from_step_stored(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = tracker.create_video("/src", "ch", from_step=3)
        assert tracker.get_video(vid_id)["from_step"] == 3

    def test_source_title_explicit(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = tracker.create_video("/src", "ch", source_title="My Title")
        assert tracker.get_video(vid_id)["source_title"] == "My Title"

    def test_source_title_from_file(self, tmp_path):
        """If source_dir contains title.txt, it is auto-read."""
        src = tmp_path / "mysrc"
        src.mkdir()
        (src / "title.txt").write_text("Auto Title", encoding="utf-8")
        tracker = _make_tracker(tmp_path)
        vid_id = tracker.create_video(src, "ch")
        assert tracker.get_video(vid_id)["source_title"] == "Auto Title"

    def test_source_title_explicit_overrides_file(self, tmp_path):
        src = tmp_path / "mysrc"
        src.mkdir()
        (src / "title.txt").write_text("File Title", encoding="utf-8")
        tracker = _make_tracker(tmp_path)
        vid_id = tracker.create_video(src, "ch", source_title="Explicit Title")
        assert tracker.get_video(vid_id)["source_title"] == "Explicit Title"

    def test_multiple_videos_get_unique_ids(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        ids = [_create_video(tracker) for _ in range(5)]
        assert len(set(ids)) == 5

    def test_project_dir_stored(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        pdir = tmp_path / "projects" / "myvid"
        vid_id = tracker.create_video("/src", "ch", project_dir=pdir)
        v = tracker.get_video(vid_id)
        assert str(pdir) in (v["project_dir"] or "")


# ═══════════════════════════════════════════════════════════════════════════════
# Status transitions
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatusTransitions:

    def test_set_running(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_running(vid_id)
        v = tracker.get_video(vid_id)
        assert v["status"] == STATUS_RUNNING
        assert v["started_at"] is not None

    def test_set_done(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_running(vid_id)
        tracker.set_done(vid_id, elapsed_seconds=120.5)
        v = tracker.get_video(vid_id)
        assert v["status"] == STATUS_DONE
        assert v["finished_at"] is not None
        assert abs(v["elapsed_seconds"] - 120.5) < 1e-9

    def test_set_done_with_paths(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_done(
            vid_id,
            video_path="/out/video.mp4",
            thumbnail_path="/out/thumb.jpg",
            script_path="/proj/script.json",
        )
        v = tracker.get_video(vid_id)
        assert "/out/video.mp4" in (v["video_path"] or "")
        assert "/out/thumb.jpg" in (v["thumbnail_path"] or "")

    def test_set_failed(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_failed(vid_id, "Something blew up", elapsed_seconds=5.0)
        v = tracker.get_video(vid_id)
        assert v["status"] == STATUS_FAILED
        assert "Something blew up" in (v["error_message"] or "")

    def test_set_failed_truncates_long_message(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        long_error = "x" * 2000
        tracker.set_failed(vid_id, long_error)
        v = tracker.get_video(vid_id)
        assert len(v["error_message"]) <= 1000

    def test_set_skipped(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_skipped(vid_id, "already done")
        v = tracker.get_video(vid_id)
        assert v["status"] == STATUS_SKIPPED
        assert "already done" in (v["error_message"] or "")

    def test_set_youtube_url(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_youtube_url(vid_id, "https://youtu.be/abc123", "abc123")
        v = tracker.get_video(vid_id)
        assert v["youtube_url"] == "https://youtu.be/abc123"
        assert v["youtube_video_id"] == "abc123"

    def test_cancel_orphaned_jobs(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.set_running(vid_id)
        cancelled = tracker.cancel_orphaned_jobs()
        assert cancelled >= 1
        v = tracker.get_video(vid_id)
        assert v["status"] == "cancelled"


# ═══════════════════════════════════════════════════════════════════════════════
# get_video / list_videos
# ═══════════════════════════════════════════════════════════════════════════════

class TestQueries:

    def test_get_video_returns_none_for_missing(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        assert tracker.get_video(9999) is None

    def test_list_videos_returns_all(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        for _ in range(3):
            _create_video(tracker)
        videos = tracker.list_videos()
        assert len(videos) == 3

    def test_list_videos_filter_by_channel(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.create_video("/s", "history")
        tracker.create_video("/s", "science")
        tracker.create_video("/s", "history")
        history = tracker.list_videos(channel="history")
        assert len(history) == 2

    def test_list_videos_filter_by_status(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        v1 = _create_video(tracker)
        v2 = _create_video(tracker)
        tracker.set_running(v1)
        running = tracker.list_videos(status=STATUS_RUNNING)
        assert len(running) == 1
        assert running[0]["id"] == v1

    def test_list_videos_limit(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        for _ in range(10):
            _create_video(tracker)
        videos = tracker.list_videos(limit=3)
        assert len(videos) == 3

    def test_list_videos_newest_first(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        ids = [_create_video(tracker) for _ in range(5)]
        videos = tracker.list_videos()
        returned_ids = [v["id"] for v in videos]
        assert returned_ids == sorted(ids, reverse=True)


# ═══════════════════════════════════════════════════════════════════════════════
# record_cost / record_costs_from_tracker
# ═══════════════════════════════════════════════════════════════════════════════

class TestCostRecording:

    def test_record_cost_stored(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_cost(vid_id, "Script", "claude-opus-4-6",
                            input_tokens=2500, output_tokens=3000, cost_usd=0.0)
        costs = tracker.get_costs(vid_id)
        assert len(costs) == 1
        assert costs[0]["step"] == "Script"
        assert costs[0]["model"] == "claude-opus-4-6"

    def test_video_total_cost(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_cost(vid_id, "Step1", "gpt-4.1", cost_usd=0.10)
        tracker.record_cost(vid_id, "Step2", "gpt-4.1", cost_usd=0.25)
        assert abs(tracker.video_total_cost(vid_id) - 0.35) < 1e-9

    def test_video_total_cost_empty(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        assert tracker.video_total_cost(vid_id) == 0.0

    def test_costs_isolated_between_videos(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        v1 = _create_video(tracker)
        v2 = _create_video(tracker)
        tracker.record_cost(v1, "Step", "m", cost_usd=1.0)
        assert len(tracker.get_costs(v2)) == 0

    def test_record_costs_from_tracker(self, tmp_path):
        """record_costs_from_tracker bulk-inserts all CostTracker entries."""
        from utils.cost_tracker import CostTracker as CT
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)

        cost_tracker = CT()
        cost_tracker.add_llm("Script", "claude-opus-4-6", input_tokens=2500, output_tokens=3000)
        cost_tracker.add_images("Images", "wavespeed", count=5)
        cost_tracker.add_voice("Voice", chars=6000)

        tracker.record_costs_from_tracker(vid_id, cost_tracker)

        costs = tracker.get_costs(vid_id)
        assert len(costs) == 3
        steps = {c["step"] for c in costs}
        assert steps == {"Script", "Images", "Voice"}

    def test_record_costs_from_empty_tracker(self, tmp_path):
        from utils.cost_tracker import CostTracker as CT
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_costs_from_tracker(vid_id, CT())
        assert tracker.get_costs(vid_id) == []

    def test_record_costs_from_non_tracker_object(self, tmp_path):
        """Passing an object without .entries should not raise (graceful no-op)."""
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_costs_from_tracker(vid_id, object())  # no .entries attr
        assert tracker.get_costs(vid_id) == []


# ═══════════════════════════════════════════════════════════════════════════════
# session_stats
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionStats:

    def test_empty_db_returns_zeros(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        stats = tracker.session_stats()
        assert stats["total_videos"] == 0
        assert stats["done"] == 0
        assert stats["failed"] == 0
        assert stats["cost_total_usd"] == 0.0

    def test_counts_done_and_failed(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        v1 = _create_video(tracker)
        v2 = _create_video(tracker)
        v3 = _create_video(tracker)
        tracker.set_done(v1, elapsed_seconds=100.0)
        tracker.set_failed(v2, "err")
        stats = tracker.session_stats()
        assert stats["total_videos"] == 3
        assert stats["done"] == 1
        assert stats["failed"] == 1

    def test_cost_total_aggregated(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_cost(vid_id, "S", "m", cost_usd=0.50)
        tracker.record_cost(vid_id, "T", "m", cost_usd=0.25)
        stats = tracker.session_stats()
        assert abs(stats["cost_total_usd"] - 0.75) < 1e-9

    def test_by_model_groups_correctly(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_cost(vid_id, "S1", "model-a", cost_usd=1.0)
        tracker.record_cost(vid_id, "S2", "model-a", cost_usd=0.5)
        tracker.record_cost(vid_id, "S3", "model-b", cost_usd=2.0)
        stats = tracker.session_stats()
        by_model = {m["model"]: m for m in stats["by_model"]}
        assert abs(by_model["model-a"]["total"] - 1.5) < 1e-9
        assert by_model["model-a"]["calls"] == 2
        assert abs(by_model["model-b"]["total"] - 2.0) < 1e-9

    def test_by_preset_groups_correctly(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.create_video("/s", "ch", quality_preset="max")
        tracker.create_video("/s", "ch", quality_preset="max")
        tracker.create_video("/s", "ch", quality_preset="high")
        stats = tracker.session_stats()
        by_preset = {p["quality_preset"]: p for p in stats["by_preset"]}
        assert by_preset["max"]["total"] == 2
        assert by_preset["high"]["total"] == 1

    def test_avg_elapsed_only_counts_done(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        v1 = _create_video(tracker)
        v2 = _create_video(tracker)
        tracker.set_done(v1, elapsed_seconds=100.0)
        tracker.set_done(v2, elapsed_seconds=200.0)
        stats = tracker.session_stats()
        assert abs(stats["avg_elapsed"] - 150.0) < 1e-6


# ═══════════════════════════════════════════════════════════════════════════════
# script_metrics
# ═══════════════════════════════════════════════════════════════════════════════

class TestScriptMetrics:

    def test_record_and_retrieve(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        metric_id = tracker.record_script_metrics(
            video_id=vid_id,
            model="claude-opus-4-6",
            template="history",
            prompt_version="v3",
            temperature=0.7,
            word_count=1200,
            block_count=10,
            hook_score=8,
        )
        assert isinstance(metric_id, int)
        metrics = tracker.get_script_metrics()
        assert len(metrics) == 1
        m = metrics[0]
        assert m["model"] == "claude-opus-4-6"
        assert m["word_count"] == 1200
        assert m["hook_score"] == 8

    def test_update_script_review(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        vid_id = _create_video(tracker)
        tracker.record_script_metrics(
            vid_id, "m", "t", "v1", 0.5, 500, 5
        )
        tracker.update_script_review(vid_id, passed=True)
        metrics = tracker.get_script_metrics()
        assert metrics[0]["review_pass"] is True

    def test_get_script_metrics_limit(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        for _ in range(10):
            tracker.record_script_metrics(None, "m", "t", "v1", 0.5, 100, 5)
        metrics = tracker.get_script_metrics(limit=3)
        assert len(metrics) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# transcription cache
# ═══════════════════════════════════════════════════════════════════════════════

class TestTranscriptionCache:

    def test_cache_and_retrieve(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        out_dir = tmp_path / "transcriptions" / "vid1"
        out_dir.mkdir(parents=True)
        tracker.cache_transcription("yt-abc123", "https://youtu.be/abc", "My Video", str(out_dir))
        result = tracker.get_cached_transcription("yt-abc123")
        assert result == str(out_dir)

    def test_cache_miss_returns_none(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        assert tracker.get_cached_transcription("nonexistent-id") is None

    def test_cache_returns_none_if_dir_missing(self, tmp_path):
        """If cached dir no longer exists on disk, return None."""
        tracker = _make_tracker(tmp_path)
        missing_dir = tmp_path / "gone"
        # Store the path but don't create the directory
        tracker.cache_transcription("yt-xyz", "https://youtu.be/xyz", "Title", str(missing_dir))
        assert tracker.get_cached_transcription("yt-xyz") is None

    def test_upsert_updates_existing_entry(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        d1 = tmp_path / "d1"
        d1.mkdir()
        d2 = tmp_path / "d2"
        d2.mkdir()
        tracker.cache_transcription("yt-same", "https://youtu.be/same", "Old Title", str(d1))
        tracker.cache_transcription("yt-same", "https://youtu.be/same", "New Title", str(d2))
        result = tracker.get_cached_transcription("yt-same")
        assert result == str(d2)
