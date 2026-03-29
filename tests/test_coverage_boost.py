"""
test_coverage_boost.py — targeted unit tests to push coverage from 26% toward 40%.

Covers pure/helper functions in:
  - modules/02_image_generator.py  (ImageResult, GenerationSummary, _derive_video_seed, _image_output_name)
  - modules/03_voice_generator.py  (AudioResult, VoiceSummary, _get_voice_id, _audio_dir, _update_script_durations)
  - modules/06_thumbnail_generator.py  (ValidationResult, ThumbnailResult, _build_prompt, constants)
  - modules/07_metadata_generator.py  (_fmt_timestamp, _build_timestamps, _build_outline, _timestamps_block, _parse_llm_response)
  - modules/08_youtube_uploader.py  (UploadResult.url, UploadResult.to_dict, _parse_schedule, _save_schedule_state)
  - modules/config_models.py  (ChannelConfig, SubtitleStyle, LLMPreset, full Pydantic validation)
  - utils/telegram_notify.py  (notify_telegram — no-env + mock-httpx paths)

No real API calls — everything external is mocked.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Project root on path ─────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ─── Import digit-prefixed modules via importlib ──────────────────────────────

def _import_module(filename: str, name: str):
    """Load a module whose filename starts with a digit."""
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "modules" / filename,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


img_gen  = _import_module("02_image_generator.py",  "img_gen")
voice_gen = _import_module("03_voice_generator.py", "voice_gen")
thumb_gen = _import_module("06_thumbnail_generator.py", "thumb_gen")
meta_gen  = _import_module("07_metadata_generator.py",  "meta_gen")
yt_up     = _import_module("08_youtube_uploader.py",    "yt_up")


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 02 — Image Generator
# ══════════════════════════════════════════════════════════════════════════════

class TestDeriveVideoSeed:
    """_derive_video_seed: stable, deterministic, in-range."""

    def test_same_title_same_seed(self):
        s1 = img_gen._derive_video_seed("Why Rome Really Fell")
        s2 = img_gen._derive_video_seed("Why Rome Really Fell")
        assert s1 == s2

    def test_different_titles_different_seeds(self):
        s1 = img_gen._derive_video_seed("Ancient Egypt")
        s2 = img_gen._derive_video_seed("Medieval Europe")
        assert s1 != s2

    def test_seed_in_valid_range(self):
        for title in ["", "a", "Hello World 123", "x" * 500]:
            seed = img_gen._derive_video_seed(title)
            assert 0 <= seed < 2 ** 30, f"Seed {seed} out of range for title={title!r}"

    def test_empty_string_stable(self):
        assert img_gen._derive_video_seed("") == img_gen._derive_video_seed("")

    def test_unicode_title(self):
        seed = img_gen._derive_video_seed("История Рима 🏛️")
        assert 0 <= seed < 2 ** 30

    def test_seed_is_integer(self):
        assert isinstance(img_gen._derive_video_seed("test"), int)

    def test_whitespace_title(self):
        seed = img_gen._derive_video_seed("   ")
        assert isinstance(seed, int)


class TestImageOutputName:
    """_image_output_name: naming convention for primary and additional images."""

    def test_idx_zero_gives_plain_name(self):
        assert img_gen._image_output_name("block_001", 0) == "block_001.png"

    def test_idx_one_gives_suffixed_name(self):
        assert img_gen._image_output_name("block_001", 1) == "block_001_1.png"

    def test_idx_two_gives_suffixed_name(self):
        assert img_gen._image_output_name("block_007", 2) == "block_007_2.png"

    def test_large_idx(self):
        assert img_gen._image_output_name("blk", 99) == "blk_99.png"

    def test_special_chars_in_block_id(self):
        assert img_gen._image_output_name("block-A_1", 0) == "block-A_1.png"


class TestImageResultDataclass:
    """ImageResult dataclass construction and defaults."""

    def test_minimal_construction(self):
        r = img_gen.ImageResult(
            block_id="block_001",
            order=0,
            path="/tmp/block_001.png",
            prompt="A Roman street",
        )
        assert r.block_id == "block_001"
        assert r.order == 0
        assert r.path == "/tmp/block_001.png"
        assert r.prompt == "A Roman street"

    def test_defaults(self):
        r = img_gen.ImageResult(block_id="b", order=1, path=None, prompt="")
        assert r.attempts == 1
        assert r.validation_score is None
        assert r.fallback_used is False
        assert r.skipped is False
        assert r.error is None

    def test_failed_result(self):
        r = img_gen.ImageResult(
            block_id="block_002",
            order=2,
            path=None,
            prompt="test",
            error="all attempts exhausted",
        )
        assert r.error == "all attempts exhausted"
        assert r.path is None

    def test_skipped_result(self):
        r = img_gen.ImageResult(
            block_id="block_003",
            order=3,
            path="/cached.png",
            prompt="test",
            skipped=True,
        )
        assert r.skipped is True

    def test_fallback_result(self):
        r = img_gen.ImageResult(
            block_id="b",
            order=0,
            path="/img.png",
            prompt="p",
            fallback_used=True,
            validation_score=2,
        )
        assert r.fallback_used is True
        assert r.validation_score == 2


class TestGenerationSummaryDataclass:
    """GenerationSummary dataclass construction and results list."""

    def test_construction_with_all_fields(self):
        r1 = img_gen.ImageResult(block_id="b1", order=0, path="/p1.png", prompt="p1")
        r2 = img_gen.ImageResult(block_id="b2", order=1, path=None, prompt="p2", error="fail")
        summary = img_gen.GenerationSummary(
            total=2,
            generated=1,
            skipped=0,
            failed=1,
            fallback_count=0,
            wavespeed_cost=0.01,
            voidai_cost=0.005,
            elapsed=3.14,
            results=[r1, r2],
        )
        assert summary.total == 2
        assert summary.generated == 1
        assert summary.failed == 1
        assert len(summary.results) == 2
        assert summary.elapsed == pytest.approx(3.14)

    def test_empty_results_default(self):
        summary = img_gen.GenerationSummary(
            total=0, generated=0, skipped=0, failed=0,
            fallback_count=0, wavespeed_cost=0.0, voidai_cost=0.0, elapsed=0.0,
        )
        assert summary.results == []

    def test_fallback_count(self):
        summary = img_gen.GenerationSummary(
            total=5, generated=3, skipped=1, failed=1,
            fallback_count=2, wavespeed_cost=0.015, voidai_cost=0.01, elapsed=10.0,
        )
        assert summary.fallback_count == 2


class TestMinFileSizeConstant:
    def test_constant_defined(self):
        assert img_gen.MIN_FILE_SIZE_BYTES == 5_000

    def test_default_size_format(self):
        assert "*" in img_gen.DEFAULT_SIZE  # WaveSpeed uses 1280*720


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 03 — Voice Generator
# ══════════════════════════════════════════════════════════════════════════════

class TestGetVoiceId:
    """_get_voice_id: primary language and multi-lang lookups."""

    def test_primary_language_returns_voice_id(self):
        config = {"language": "en", "voice_id": "rachel_en"}
        assert voice_gen._get_voice_id(config, "en") == "rachel_en"

    def test_secondary_language_returns_multilang_id(self):
        config = {
            "language": "en",
            "voice_id": "rachel_en",
            "voice_ids_multilang": {"de": "otto_de", "es": "sofia_es"},
        }
        assert voice_gen._get_voice_id(config, "de") == "otto_de"
        assert voice_gen._get_voice_id(config, "es") == "sofia_es"

    def test_missing_primary_voice_raises(self):
        config = {"language": "en", "voice_id": ""}
        with pytest.raises(ValueError, match="voice_id"):
            voice_gen._get_voice_id(config, "en")

    def test_missing_secondary_lang_raises(self):
        config = {
            "language": "en",
            "voice_id": "v1",
            "voice_ids_multilang": {"de": "otto_de"},
        }
        with pytest.raises(ValueError, match="fr"):
            voice_gen._get_voice_id(config, "fr")

    def test_empty_multilang_section_raises(self):
        config = {"language": "en", "voice_id": "v1", "voice_ids_multilang": {}}
        with pytest.raises(ValueError):
            voice_gen._get_voice_id(config, "ja")

    def test_no_multilang_key_raises(self):
        config = {"language": "en", "voice_id": "v1"}
        with pytest.raises(ValueError):
            voice_gen._get_voice_id(config, "fr")


class TestAudioDir:
    """_audio_dir: path construction for primary and secondary languages."""

    def test_primary_language_returns_base_audio(self):
        script_dir = Path("/project/my_video")
        result = voice_gen._audio_dir(script_dir, "en", "en")
        assert result == Path("/project/my_video/audio")

    def test_secondary_language_returns_subdir(self):
        script_dir = Path("/project/my_video")
        result = voice_gen._audio_dir(script_dir, "de", "en")
        assert result == Path("/project/my_video/audio/de")

    def test_another_secondary_language(self):
        script_dir = Path("/project/video")
        result = voice_gen._audio_dir(script_dir, "es", "en")
        assert result == Path("/project/video/audio/es")

    def test_matching_lang_gives_base(self):
        script_dir = Path("/x")
        assert voice_gen._audio_dir(script_dir, "fr", "fr") == Path("/x/audio")


class TestAudioResultDataclass:
    """AudioResult dataclass construction and defaults."""

    def test_minimal_construction(self):
        r = voice_gen.AudioResult(
            block_id="block_001",
            order=0,
            path="/audio/block_001.mp3",
            duration=9.5,
            chars=120,
        )
        assert r.block_id == "block_001"
        assert r.duration == 9.5
        assert r.chars == 120

    def test_defaults(self):
        r = voice_gen.AudioResult(block_id="b", order=0, path=None, duration=None, chars=0)
        assert r.skipped is False
        assert r.error is None

    def test_failed_result(self):
        r = voice_gen.AudioResult(
            block_id="b", order=1, path=None, duration=None, chars=50,
            error="API error",
        )
        assert r.error == "API error"
        assert r.path is None

    def test_skipped_cached(self):
        r = voice_gen.AudioResult(
            block_id="b", order=0, path="/cached.mp3", duration=5.0, chars=80, skipped=True,
        )
        assert r.skipped is True
        assert r.path == "/cached.mp3"


class TestVoiceSummaryDataclass:
    """VoiceSummary dataclass and results list default."""

    def test_full_construction(self):
        r = voice_gen.AudioResult(block_id="b1", order=0, path="/p.mp3", duration=10.0, chars=80)
        s = voice_gen.VoiceSummary(
            total=1, generated=1, skipped=0, failed=0,
            fallback_count=0, total_chars=80, total_duration=10.0,
            concat_path="/full.mp3", normalized_path="/norm.mp3", elapsed=5.0,
            results=[r],
        )
        assert s.total_duration == 10.0
        assert s.concat_path == "/full.mp3"
        assert len(s.results) == 1

    def test_empty_results_default(self):
        s = voice_gen.VoiceSummary(
            total=0, generated=0, skipped=0, failed=0,
            fallback_count=0, total_chars=0, total_duration=0.0,
            concat_path=None, normalized_path=None, elapsed=0.0,
        )
        assert s.results == []


class TestUpdateScriptDurations:
    """_update_script_durations: writes audio_duration back to script.json."""

    def test_writes_durations_to_file(self, tmp_path):
        script = {
            "title": "Test",
            "blocks": [
                {"id": "block_001", "order": 0, "narration": "Hello"},
                {"id": "block_002", "order": 1, "narration": "World"},
            ],
        }
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")

        results = [
            voice_gen.AudioResult(block_id="block_001", order=0, path="/p1.mp3", duration=9.5, chars=5),
            voice_gen.AudioResult(block_id="block_002", order=1, path="/p2.mp3", duration=28.0, chars=5),
        ]
        voice_gen._update_script_durations(script, results, script_file)

        updated = json.loads(script_file.read_text(encoding="utf-8"))
        assert updated["blocks"][0]["audio_duration"] == 9.5
        assert updated["blocks"][1]["audio_duration"] == 28.0

    def test_skips_blocks_without_duration(self, tmp_path):
        script = {
            "title": "Test",
            "blocks": [{"id": "b1", "order": 0, "narration": "Hello"}],
        }
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")

        results = [
            voice_gen.AudioResult(block_id="b1", order=0, path=None, duration=None, chars=5),
        ]
        voice_gen._update_script_durations(script, results, script_file)
        # No duration → block should NOT have audio_duration key
        updated = json.loads(script_file.read_text(encoding="utf-8"))
        assert "audio_duration" not in updated["blocks"][0]

    def test_no_results_does_nothing(self, tmp_path):
        script = {"title": "T", "blocks": [{"id": "b1", "order": 0}]}
        script_file = tmp_path / "script.json"
        original_text = json.dumps(script)
        script_file.write_text(original_text, encoding="utf-8")
        voice_gen._update_script_durations(script, [], script_file)
        # File should be unchanged (empty dur_map → early return)
        assert script_file.read_text(encoding="utf-8") == original_text

    def test_rounds_to_three_decimals(self, tmp_path):
        script = {"title": "T", "blocks": [{"id": "b", "order": 0, "narration": "Hi"}]}
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")
        results = [
            voice_gen.AudioResult(block_id="b", order=0, path="/p.mp3", duration=9.1234567, chars=2),
        ]
        voice_gen._update_script_durations(script, results, script_file)
        updated = json.loads(script_file.read_text(encoding="utf-8"))
        assert updated["blocks"][0]["audio_duration"] == round(9.1234567, 3)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 06 — Thumbnail Generator
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildPrompt:
    """_build_prompt: prompt priority, style injection, youtube keyword."""

    BASE_SCRIPT = {
        "title": "Why Rome Fell",
        "thumbnail_prompt": "Burning Rome colosseum",
    }
    BASE_CONFIG = {
        "niche": "history",
        "thumbnail_style": "cinematic, 8k",
    }

    def test_cli_override_takes_priority(self):
        prompt = thumb_gen._build_prompt(
            self.BASE_SCRIPT, self.BASE_CONFIG, None, "My override", None,
        )
        assert "My override" in prompt

    def test_transcriber_prompt_used_when_no_override(self):
        td = {"thumbnail_prompt": "Epic battle scene"}
        prompt = thumb_gen._build_prompt(self.BASE_SCRIPT, self.BASE_CONFIG, td, None, None)
        assert "Epic battle scene" in prompt

    def test_script_field_used_as_fallback(self):
        prompt = thumb_gen._build_prompt(self.BASE_SCRIPT, self.BASE_CONFIG, None, None, None)
        assert "Burning Rome colosseum" in prompt

    def test_title_fallback_when_no_prompt(self):
        script = {"title": "Ancient Secrets"}
        prompt = thumb_gen._build_prompt(script, self.BASE_CONFIG, None, None, None)
        assert "Ancient Secrets" in prompt

    def test_thumbnail_style_appended(self):
        prompt = thumb_gen._build_prompt(self.BASE_SCRIPT, self.BASE_CONFIG, None, None, None)
        assert "cinematic" in prompt

    def test_youtube_thumbnail_keyword_injected_when_missing(self):
        config = {"niche": "history", "thumbnail_style": ""}
        prompt = thumb_gen._build_prompt(
            {"title": "Test", "thumbnail_prompt": "A simple landscape"},
            config, None, None, None,
        )
        assert "youtube thumbnail" in prompt.lower()

    def test_youtube_thumbnail_not_duplicated_when_present(self):
        override = "YouTube thumbnail of ancient Rome"
        prompt = thumb_gen._build_prompt(self.BASE_SCRIPT, self.BASE_CONFIG, None, override, None)
        assert prompt.lower().count("youtube thumbnail") == 1

    def test_text_overlay_appended(self):
        prompt = thumb_gen._build_prompt(
            self.BASE_SCRIPT, self.BASE_CONFIG, None, "Rome battlefield", "THE TRUTH",
        )
        assert "THE TRUTH" in prompt
        assert "bold text overlay" in prompt

    def test_empty_style_not_appended(self):
        config = {"niche": "history", "thumbnail_style": ""}
        prompt = thumb_gen._build_prompt(
            self.BASE_SCRIPT, config, None, "Simple prompt", None,
        )
        # Should not end with ", " (no trailing comma from empty style)
        assert not prompt.endswith(", ")

    def test_transcriber_empty_prompt_falls_through_to_script(self):
        td = {"thumbnail_prompt": ""}
        prompt = thumb_gen._build_prompt(self.BASE_SCRIPT, self.BASE_CONFIG, td, None, None)
        assert "Burning Rome colosseum" in prompt


class TestThumbnailDataclasses:
    """ValidationResult and ThumbnailResult construction."""

    def test_validation_result_passed(self):
        vr = thumb_gen.ValidationResult(
            passed=True, score=6,
            criteria={"composition": True, "colors": True},
            issues=[],
        )
        assert vr.passed is True
        assert vr.score == 6
        assert vr.raw == ""

    def test_validation_result_failed(self):
        vr = thumb_gen.ValidationResult(
            passed=False, score=4,
            criteria={"composition": True, "colors": False},
            issues=["colors", "quality"],
        )
        assert vr.passed is False
        assert "colors" in vr.issues

    def test_thumbnail_result_construction(self, tmp_path):
        p = tmp_path / "thumbnail.png"
        tr = thumb_gen.ThumbnailResult(
            output_path=p,
            prompt_used="Epic battle",
            attempts=3,
            passed_validation=True,
            score=5,
            issues=["quality"],
        )
        assert tr.attempts == 3
        assert tr.score == 5
        assert tr.issues == ["quality"]

    def test_thumbnail_result_default_issues(self, tmp_path):
        p = tmp_path / "thumbnail.png"
        tr = thumb_gen.ThumbnailResult(
            output_path=p,
            prompt_used="test",
            attempts=1,
            passed_validation=True,
            score=6,
        )
        assert tr.issues == []

    def test_pass_threshold_constant(self):
        assert thumb_gen.PASS_THRESHOLD == 5

    def test_fixed_seed_constant(self):
        assert thumb_gen.FIXED_SEED == 42

    def test_variant_seeds_has_three_unique(self):
        seeds = thumb_gen.VARIANT_SEEDS
        assert len(seeds) == 3
        assert len(set(seeds)) == 3

    def test_criteria_tuple_has_six_items(self):
        assert len(thumb_gen.CRITERIA) == 6


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 07 — Metadata Generator
# ══════════════════════════════════════════════════════════════════════════════

class TestFmtTimestamp:
    """_fmt_timestamp: seconds → M:SS string."""

    def test_zero_seconds(self):
        assert meta_gen._fmt_timestamp(0) == "0:00"

    def test_exactly_one_minute(self):
        assert meta_gen._fmt_timestamp(60) == "1:00"

    def test_ninety_seconds(self):
        assert meta_gen._fmt_timestamp(90) == "1:30"

    def test_single_digit_seconds_padded(self):
        assert meta_gen._fmt_timestamp(65) == "1:05"

    def test_large_value(self):
        assert meta_gen._fmt_timestamp(3600) == "60:00"

    def test_fractional_seconds_truncated(self):
        # 75.9 → 75 → 1:15
        assert meta_gen._fmt_timestamp(75.9) == "1:15"

    def test_nine_seconds(self):
        assert meta_gen._fmt_timestamp(9) == "0:09"

    def test_fifty_nine_seconds(self):
        assert meta_gen._fmt_timestamp(59) == "0:59"


class TestBuildTimestamps:
    """_build_timestamps: cumulative cursor from audio_duration blocks."""

    def _block(self, id_: str, dur: float, narr: str, label: str) -> dict:
        return {
            "id": id_,
            "order": 0,
            "audio_duration": dur,
            "narration": narr,
            "timestamp_label": label,
        }

    def test_single_block_starts_at_zero(self):
        blocks = [self._block("b1", 10.0, "Hello world", "Intro")]
        result = meta_gen._build_timestamps(blocks)
        assert len(result) == 1
        assert result[0]["time"] == "0:00"
        assert result[0]["label"] == "Intro"

    def test_multiple_blocks_cumulative(self):
        blocks = [
            self._block("b1", 10.0, "First narration", "Section 1"),
            self._block("b2", 30.0, "Second narration", "Section 2"),
            self._block("b3", 20.0, "Third narration", "Section 3"),
        ]
        result = meta_gen._build_timestamps(blocks)
        assert result[0]["time"] == "0:00"
        assert result[1]["time"] == "0:10"
        assert result[2]["time"] == "0:40"

    def test_block_without_narration_excluded(self):
        blocks = [
            self._block("b1", 5.0, "Has text", "Section 1"),
            self._block("b2", 10.0, "", "CTA"),   # empty narration → excluded
            self._block("b3", 8.0, "More text", "Section 3"),
        ]
        result = meta_gen._build_timestamps(blocks)
        # b2 is excluded; b3 cursor = 0 + 5 = 5 (b1) + 10 (b2 still advances cursor)
        labels = [r["label"] for r in result]
        assert "Section 1" in labels
        assert "CTA" not in labels
        assert "Section 3" in labels

    def test_block_without_label_falls_back_to_id(self):
        # When timestamp_label is empty, the code falls back to block["id"] as label.
        # An entry IS produced if narration is present — label is just the block id.
        blocks = [
            {"id": "b1", "order": 0, "audio_duration": 5.0, "narration": "Text", "timestamp_label": ""},
        ]
        result = meta_gen._build_timestamps(blocks)
        assert len(result) == 1
        assert result[0]["label"] == "b1"  # fallback to block id

    def test_empty_blocks(self):
        assert meta_gen._build_timestamps([]) == []

    def test_none_duration_treated_as_zero(self):
        blocks = [
            {"id": "b1", "order": 0, "audio_duration": None, "narration": "Hello", "timestamp_label": "Intro"},
        ]
        result = meta_gen._build_timestamps(blocks)
        assert len(result) == 1
        assert result[0]["time"] == "0:00"


class TestBuildOutline:
    """_build_outline: compact LLM prompt string from blocks."""

    def _block(self, id_: str, dur: float, narr: str, label: str, btype: str = "content") -> dict:
        return {
            "id": id_,
            "order": 0,
            "audio_duration": dur,
            "narration": narr,
            "timestamp_label": label,
            "type": btype,
        }

    def test_returns_no_blocks_message_for_empty(self):
        result = meta_gen._build_outline([])
        assert "(no blocks with narration)" in result

    def test_includes_block_type(self):
        blocks = [self._block("b1", 10.0, "Some narration text", "Intro", "intro")]
        result = meta_gen._build_outline(blocks)
        assert "[INTRO]" in result

    def test_includes_timestamp_range(self):
        blocks = [self._block("b1", 30.0, "Narration content here", "Section One")]
        result = meta_gen._build_outline(blocks)
        assert "0:00" in result
        assert "0:30" in result

    def test_long_narration_truncated(self):
        long_narr = "A" * 200
        blocks = [self._block("b1", 5.0, long_narr, "Block")]
        result = meta_gen._build_outline(blocks)
        assert "..." in result

    def test_short_narration_not_truncated(self):
        short_narr = "Short text"
        blocks = [self._block("b1", 5.0, short_narr, "Block")]
        result = meta_gen._build_outline(blocks)
        assert "..." not in result

    def test_empty_narration_blocks_skipped(self):
        blocks = [
            self._block("b1", 5.0, "", "CTA"),
            self._block("b2", 10.0, "Real content", "Section"),
        ]
        result = meta_gen._build_outline(blocks)
        assert "CTA" not in result
        assert "Section" in result

    def test_cursor_advances_through_silent_blocks(self):
        blocks = [
            self._block("b1", 60.0, "", "Silent"),      # silent, cursor advances to 60
            self._block("b2", 30.0, "Narration here", "Section"),
        ]
        result = meta_gen._build_outline(blocks)
        # Section starts at 1:00 (60 seconds)
        assert "1:00" in result


class TestTimestampsBlock:
    """_timestamps_block: plain-text formatting."""

    def test_empty_list(self):
        assert meta_gen._timestamps_block([]) == ""

    def test_single_entry(self):
        result = meta_gen._timestamps_block([{"time": "0:00", "label": "Intro"}])
        assert result == "0:00 Intro"

    def test_multiple_entries_separated_by_newline(self):
        ts = [
            {"time": "0:00", "label": "Introduction"},
            {"time": "1:30", "label": "Main Content"},
            {"time": "5:00", "label": "Conclusion"},
        ]
        result = meta_gen._timestamps_block(ts)
        lines = result.split("\n")
        assert len(lines) == 3
        assert lines[0] == "0:00 Introduction"
        assert lines[2] == "5:00 Conclusion"


class TestParseLlmResponse:
    """_parse_llm_response: JSON extraction, markdown fence stripping."""

    def test_plain_json(self):
        raw = '{"title": "Rome", "tags": ["history"]}'
        result = meta_gen._parse_llm_response(raw)
        assert result["title"] == "Rome"
        assert result["tags"] == ["history"]

    def test_strips_json_markdown_fence(self):
        raw = '```json\n{"title": "Rome"}\n```'
        result = meta_gen._parse_llm_response(raw)
        assert result["title"] == "Rome"

    def test_strips_plain_markdown_fence(self):
        raw = '```\n{"score": 42}\n```'
        result = meta_gen._parse_llm_response(raw)
        assert result["score"] == 42

    def test_raises_on_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            meta_gen._parse_llm_response("not valid json")

    def test_whitespace_stripped(self):
        raw = '  \n  {"key": "value"}  \n  '
        result = meta_gen._parse_llm_response(raw)
        assert result["key"] == "value"


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 08 — YouTube Uploader
# ══════════════════════════════════════════════════════════════════════════════

class TestUploadResultClass:
    """UploadResult: url property and to_dict serialization."""

    def test_url_property(self):
        r = yt_up.UploadResult(
            video_id="abc123XYZ",
            title="My Video",
            privacy="private",
        )
        assert r.url == "https://www.youtube.com/watch?v=abc123XYZ"

    def test_to_dict_contains_expected_keys(self):
        r = yt_up.UploadResult(
            video_id="def456",
            title="Test Upload",
            privacy="public",
            publish_at="2026-04-01T18:00:00Z",
            thumbnail_ok=True,
        )
        d = r.to_dict()
        assert d["video_id"] == "def456"
        assert d["url"] == "https://www.youtube.com/watch?v=def456"
        assert d["title"] == "Test Upload"
        assert d["privacy"] == "public"
        assert d["publish_at"] == "2026-04-01T18:00:00Z"
        assert d["thumbnail_ok"] is True
        assert "uploaded_at" in d  # ISO timestamp present

    def test_to_dict_no_publish_at(self):
        r = yt_up.UploadResult(video_id="x", title="T", privacy="private")
        d = r.to_dict()
        assert d["publish_at"] is None
        assert d["thumbnail_ok"] is False

    def test_url_property_with_short_id(self):
        r = yt_up.UploadResult(video_id="z", title="T", privacy="public")
        assert "watch?v=z" in r.url


class TestParseSchedule:
    """_parse_schedule: various datetime formats, future-only enforcement."""

    def _future_str(self, days: int = 2, hour: int = 18, minute: int = 0) -> str:
        dt = datetime.now() + timedelta(days=days)
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {hour:02d}:{minute:02d}"

    def test_parses_space_format(self):
        s = self._future_str()
        result = yt_up._parse_schedule(s)
        assert "T" in result
        assert result.endswith("Z")

    def test_parses_iso_format(self):
        dt = datetime.now() + timedelta(days=3)
        s = f"{dt.year}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}"
        result = yt_up._parse_schedule(s)
        assert result.endswith("Z")

    def test_past_date_raises_value_error(self):
        with pytest.raises(ValueError, match="past"):
            yt_up._parse_schedule("2020-01-01 10:00")

    def test_invalid_format_raises_value_error(self):
        with pytest.raises(ValueError, match="parse"):
            yt_up._parse_schedule("not a date")

    def test_another_invalid_format(self):
        with pytest.raises(ValueError):
            yt_up._parse_schedule("01/01/2026 18:00")

    def test_output_format_is_iso8601_utc(self):
        result = yt_up._parse_schedule(self._future_str())
        # Must match YYYY-MM-DDTHH:MM:SSZ
        datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ")

    def test_strips_whitespace(self):
        s = "  " + self._future_str() + "  "
        result = yt_up._parse_schedule(s)
        assert result.endswith("Z")


class TestSaveScheduleState:
    """_save_schedule_state: persists last_upload_at and last_video_id."""

    def test_creates_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_up, "OAUTH2_DIR", tmp_path)
        yt_up._save_schedule_state("main", "vidABC", "2026-04-01T18:00:00Z")
        state_file = tmp_path / "main_schedule.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["last_upload_at"] == "2026-04-01T18:00:00Z"
        assert state["last_video_id"] == "vidABC"

    def test_overwrites_existing_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_up, "OAUTH2_DIR", tmp_path)
        yt_up._save_schedule_state("main", "vid1", "2026-04-01T18:00:00Z")
        yt_up._save_schedule_state("main", "vid2", "2026-04-08T18:00:00Z")
        state = json.loads((tmp_path / "main_schedule.json").read_text())
        assert state["last_video_id"] == "vid2"

    def test_different_channels_separate_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_up, "OAUTH2_DIR", tmp_path)
        yt_up._save_schedule_state("channelA", "v1", "2026-04-01T18:00:00Z")
        yt_up._save_schedule_state("channelB", "v2", "2026-04-02T18:00:00Z")
        assert (tmp_path / "channelA_schedule.json").exists()
        assert (tmp_path / "channelB_schedule.json").exists()


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: config_models.py — Pydantic ChannelConfig validation
# ══════════════════════════════════════════════════════════════════════════════

from pydantic import ValidationError
from modules.config_models import (
    BrandingConfig,
    ChannelConfig,
    ImageConfig,
    LLMConfig,
    LLMPreset,
    MusicConfig,
    SubtitleStyle,
    TTSConfig,
)


class TestSubtitleStyle:
    def test_defaults(self):
        s = SubtitleStyle()
        assert s.font == "Arial Bold"
        assert s.size == 48
        assert s.color == "#FFFFFF"
        assert s.position == "bottom"

    def test_custom_values(self):
        s = SubtitleStyle(font="Roboto", size=36, color="#FF0000")
        assert s.font == "Roboto"
        assert s.size == 36
        assert s.color == "#FF0000"


class TestLLMPreset:
    def test_defaults(self):
        p = LLMPreset()
        assert "claude" in p.script.lower() or "gpt" in p.script.lower() or p.script  # non-empty
        assert p.metadata
        assert p.thumbnail

    def test_custom(self):
        p = LLMPreset(script="gpt-4", metadata="gpt-3.5-turbo", thumbnail="gpt-4-vision")
        assert p.script == "gpt-4"


class TestTTSConfig:
    def test_defaults(self):
        t = TTSConfig()
        assert t.provider == "voiceapi"
        assert t.fallback == "tts-1-hd"


class TestImageConfig:
    def test_defaults(self):
        i = ImageConfig()
        assert i.provider == "wavespeed"


class TestMusicConfig:
    def test_defaults(self):
        m = MusicConfig()
        assert m.tracks_dir == "assets/music"
        assert m.random is False
        assert m.volume_db == -20


class TestChannelConfigValidation:
    """ChannelConfig: valid config, missing required field, invalid path."""

    MINIMAL_VALID = {
        "channel_name": "History Hub",
    }

    FULL_VALID = {
        "channel_name": "History Hub",
        "niche": "history",
        "language": "en",
        "voice_id": "rachel_v3",
        "image_style": "cinematic, photorealistic",
        "thumbnail_style": "bold text, high contrast",
        "master_prompt_path": "prompts/master_script_v4.txt",
    }

    def test_minimal_valid_config_parses(self):
        cfg = ChannelConfig(**self.MINIMAL_VALID)
        assert cfg.channel_name == "History Hub"
        assert cfg.niche == "general"  # default
        assert cfg.language == "en"

    def test_full_valid_config_parses(self):
        cfg = ChannelConfig(**self.FULL_VALID)
        assert cfg.voice_id == "rachel_v3"
        assert cfg.niche == "history"

    def test_missing_channel_name_raises(self):
        with pytest.raises(ValidationError):
            ChannelConfig()  # channel_name is required

    def test_invalid_master_prompt_path_extension_raises(self):
        data = {**self.MINIMAL_VALID, "master_prompt_path": "prompts/master.json"}
        with pytest.raises(ValidationError, match="txt"):
            ChannelConfig(**data)

    def test_txt_extension_accepted(self):
        data = {**self.MINIMAL_VALID, "master_prompt_path": "prompts/anything.txt"}
        cfg = ChannelConfig(**data)
        assert cfg.master_prompt_path == "prompts/anything.txt"

    def test_empty_master_prompt_path_accepted(self):
        # Empty string → validator returns early without raising
        data = {**self.MINIMAL_VALID, "master_prompt_path": ""}
        cfg = ChannelConfig(**data)
        assert cfg.master_prompt_path == ""

    def test_nested_subtitle_style_parsed(self):
        data = {
            **self.MINIMAL_VALID,
            "subtitle_style": {"font": "Roboto", "size": 36, "color": "#FF0000",
                                "outline_color": "#000000", "outline_width": 2,
                                "position": "top", "margin_v": 40},
        }
        cfg = ChannelConfig(**data)
        assert cfg.subtitle_style.font == "Roboto"
        assert cfg.subtitle_style.size == 36

    def test_llm_config_parsed(self):
        data = {
            **self.MINIMAL_VALID,
            "llm": {
                "default_preset": "max",
                "presets": {
                    "max": {"script": "claude-opus-4-6", "metadata": "gpt-4.1-mini", "thumbnail": "gpt-4.1"},
                },
            },
        }
        cfg = ChannelConfig(**data)
        assert cfg.llm.default_preset == "max"
        assert "max" in cfg.llm.presets

    def test_from_json_loads_sample_config(self):
        """from_json: round-trip via the test fixture file."""
        config_path = ROOT / "tests" / "test_data" / "sample_config.json"
        cfg = ChannelConfig.from_json(config_path)
        assert cfg.channel_name  # non-empty
        assert cfg.niche

    def test_extra_unknown_fields_ignored(self):
        # Pydantic v2 default is to ignore extra fields
        data = {**self.MINIMAL_VALID, "unknown_future_key": "some_value"}
        cfg = ChannelConfig(**data)
        assert cfg.channel_name == "History Hub"


# ══════════════════════════════════════════════════════════════════════════════
# utils/telegram_notify.py
# ══════════════════════════════════════════════════════════════════════════════

from utils.telegram_notify import notify_telegram


class TestNotifyTelegram:
    """notify_telegram: silent no-op without env vars, correct payload with mock."""

    @pytest.mark.asyncio
    async def test_returns_silently_without_env_vars(self, monkeypatch):
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TG_ALLOWED_CHAT_ID", raising=False)
        # Should complete without raising any exception
        await notify_telegram("Test message")

    @pytest.mark.asyncio
    async def test_returns_silently_with_only_token(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "12345:abc")
        monkeypatch.delenv("TG_ALLOWED_CHAT_ID", raising=False)
        await notify_telegram("Test")

    @pytest.mark.asyncio
    async def test_returns_silently_with_only_chat_id(self, monkeypatch):
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TG_ALLOWED_CHAT_ID", "99999")
        await notify_telegram("Test")

    @pytest.mark.asyncio
    async def test_sends_correct_payload(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "99999:TESTTOKEN")
        monkeypatch.setenv("TG_ALLOWED_CHAT_ID", "123456789")

        captured = {}

        class FakeResponse:
            status_code = 200

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        with patch("utils.telegram_notify.httpx.AsyncClient", return_value=FakeAsyncClient()):
            await notify_telegram("<b>Pipeline complete</b>")

        assert "99999:TESTTOKEN" in captured["url"]
        assert captured["json"]["chat_id"] == "123456789"
        assert captured["json"]["text"] == "<b>Pipeline complete</b>"
        assert captured["json"]["parse_mode"] == "HTML"
        assert captured["json"]["disable_web_page_preview"] is True

    @pytest.mark.asyncio
    async def test_handles_httpx_exception_silently(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "99999:TESTTOKEN")
        monkeypatch.setenv("TG_ALLOWED_CHAT_ID", "123456789")

        class BrokenClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                raise ConnectionError("Network unreachable")

        with patch("utils.telegram_notify.httpx.AsyncClient", return_value=BrokenClient()):
            # Must not raise
            await notify_telegram("Test message")

    @pytest.mark.asyncio
    async def test_handles_non_200_response_silently(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "99999:TESTTOKEN")
        monkeypatch.setenv("TG_ALLOWED_CHAT_ID", "123456789")

        class BadResponse:
            status_code = 403
            text = "Forbidden"

        class ClientThatReturns403:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                return BadResponse()

        with patch("utils.telegram_notify.httpx.AsyncClient", return_value=ClientThatReturns403()):
            # Must not raise — just logs a warning
            await notify_telegram("Test")
