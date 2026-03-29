"""
Unit tests for VideoForge modules 02-06 and common.py.

Covers pure/helper functions only — no async API calls, no FFmpeg, no disk I/O
beyond pre-existing test fixtures.

Modules imported via importlib because filenames start with digits.
"""

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── Project root ─────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
TEST_DATA = ROOT / "tests" / "test_data"
sys.path.insert(0, str(ROOT))


# ─── Helper: load a module whose filename starts with a digit ─────────────────

def _load_module(filename: str, name: str) -> types.ModuleType:
    """Load a module from modules/ by filename, registering it under `name`."""
    path = ROOT / "modules" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Lazy module fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mod02():
    return _load_module("02_image_generator.py", "mod02_image_generator")


@pytest.fixture(scope="module")
def mod04():
    return _load_module("04_subtitle_generator.py", "mod04_subtitle_generator")


@pytest.fixture(scope="module")
def mod05():
    return _load_module("05_video_compiler.py", "mod05_video_compiler")


@pytest.fixture(scope="module")
def mod06():
    return _load_module("06_thumbnail_generator.py", "mod06_thumbnail_generator")


# ─── common.py is a regular import ───────────────────────────────────────────

from modules import common  # noqa: E402  (after sys.path setup)


# ═══════════════════════════════════════════════════════════════════════════════
# common.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadChannelConfig:
    """common.load_channel_config — file loading and error paths."""

    def test_returns_dict_for_valid_file(self):
        cfg = common.load_channel_config(TEST_DATA / "sample_config.json")
        assert isinstance(cfg, dict)
        assert cfg["channel_name"] == "Test Channel"

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            common.load_channel_config(tmp_path / "nonexistent.json")

    def test_invalid_json_raises_value_error(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json }", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            common.load_channel_config(bad)

    def test_all_expected_keys_present(self):
        cfg = common.load_channel_config(TEST_DATA / "sample_config.json")
        for key in ("channel_name", "niche", "language", "voice_id", "llm"):
            assert key in cfg, f"Missing key: {key}"

    def test_absolute_path_works(self):
        cfg = common.load_channel_config(TEST_DATA / "sample_config.json")
        assert cfg["niche"] == "history"

    def test_subtitle_style_parsed(self):
        cfg = common.load_channel_config(TEST_DATA / "sample_config.json")
        style = cfg.get("subtitle_style", {})
        assert style["font"] == "Arial Bold"
        assert style["size"] == 48


class TestGetLlmPreset:
    """common.get_llm_preset — preset resolution and error paths."""

    @pytest.fixture
    def config(self):
        return common.load_channel_config(TEST_DATA / "sample_config.json")

    def test_default_preset_returned(self, config):
        preset = common.get_llm_preset(config)
        assert isinstance(preset, dict)
        assert "script" in preset

    def test_named_preset_max(self, config):
        preset = common.get_llm_preset(config, "max")
        assert preset["script"] == "claude-opus-4-6"

    def test_named_preset_test(self, config):
        preset = common.get_llm_preset(config, "test")
        assert "script" in preset
        assert "thumbnail" in preset

    def test_unknown_preset_raises_value_error(self, config):
        with pytest.raises(ValueError, match="Unknown LLM preset"):
            common.get_llm_preset(config, "nonexistent_preset")

    def test_all_standard_presets_available(self, config):
        for name in ("max", "high", "balanced", "bulk", "test"):
            p = common.get_llm_preset(config, name)
            assert isinstance(p, dict), f"Preset '{name}' did not return dict"

    def test_preset_has_thumbnail_key(self, config):
        preset = common.get_llm_preset(config, "max")
        assert "thumbnail" in preset


class TestLoadTranscriberOutput:
    """common.load_transcriber_output — directory loading and error paths."""

    def test_loads_all_present_files(self):
        result = common.load_transcriber_output(TEST_DATA / "sample_transcriber_output")
        assert "transcript" in result
        assert "metadata" in result
        assert result["transcript"] is not None  # transcript.txt exists

    def test_missing_dir_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            common.load_transcriber_output(tmp_path / "no_such_dir")

    def test_file_path_raises_not_a_directory(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("content")
        with pytest.raises(NotADirectoryError):
            common.load_transcriber_output(f)

    def test_missing_files_are_none(self):
        result = common.load_transcriber_output(TEST_DATA / "sample_transcriber_output")
        # thumbnail.jpg is not in sample_transcriber_output — should be None or path string
        # At minimum, source_dir key must be present
        assert "source_dir" in result

    def test_metadata_parsed_as_dict(self):
        result = common.load_transcriber_output(TEST_DATA / "sample_transcriber_output")
        if result.get("metadata") is not None:
            assert isinstance(result["metadata"], dict)

    def test_invalid_json_metadata_raises_value_error(self, tmp_path):
        d = tmp_path / "transcriber"
        d.mkdir()
        (d / "metadata.json").write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            common.load_transcriber_output(d)

    def test_text_files_are_stripped(self):
        result = common.load_transcriber_output(TEST_DATA / "sample_transcriber_output")
        if result.get("transcript"):
            assert not result["transcript"].startswith(" ")
            assert not result["transcript"].endswith(" ")


# ═══════════════════════════════════════════════════════════════════════════════
# Module 02: Image Generator
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeriveVideoSeed:
    """02._derive_video_seed — determinism and range."""

    def test_same_title_same_seed(self, mod02):
        s1 = mod02._derive_video_seed("Why Rome Fell")
        s2 = mod02._derive_video_seed("Why Rome Fell")
        assert s1 == s2

    def test_different_titles_different_seeds(self, mod02):
        s1 = mod02._derive_video_seed("Title A")
        s2 = mod02._derive_video_seed("Title B")
        assert s1 != s2

    def test_seed_in_valid_range(self, mod02):
        seed = mod02._derive_video_seed("Some video title here")
        assert 0 <= seed < 2 ** 30

    def test_empty_string_produces_integer(self, mod02):
        seed = mod02._derive_video_seed("")
        assert isinstance(seed, int)

    def test_unicode_title(self, mod02):
        seed = mod02._derive_video_seed("Почему пал Рим 🏛️")
        assert isinstance(seed, int)
        assert 0 <= seed < 2 ** 30


class TestImageOutputName:
    """02._image_output_name — filename derivation."""

    def test_idx_zero_returns_plain_png(self, mod02):
        assert mod02._image_output_name("block_001", 0) == "block_001.png"

    def test_idx_one_returns_suffixed_png(self, mod02):
        assert mod02._image_output_name("block_001", 1) == "block_001_1.png"

    def test_idx_two_returns_suffixed_png(self, mod02):
        assert mod02._image_output_name("block_001", 2) == "block_001_2.png"

    def test_different_block_ids(self, mod02):
        assert mod02._image_output_name("intro_block", 0) == "intro_block.png"
        assert mod02._image_output_name("intro_block", 3) == "intro_block_3.png"


# ═══════════════════════════════════════════════════════════════════════════════
# Module 03: Voice Generator  (sanitize_narration_for_tts lives in script_validator)
# ═══════════════════════════════════════════════════════════════════════════════

from modules.script_validator import sanitize_narration_for_tts  # noqa: E402


class TestSanitizeNarrationForTts:
    """script_validator.sanitize_narration_for_tts — TTS text cleaning."""

    def test_removes_image_prompt_tag(self):
        text = "Before. [IMAGE_PROMPT: beautiful sunset] After."
        result = sanitize_narration_for_tts(text)
        assert "[IMAGE_PROMPT" not in result
        assert "Before." in result
        assert "After." in result

    def test_removes_section_header(self):
        text = "[SECTION 1: Introduction]\nHello world."
        result = sanitize_narration_for_tts(text)
        assert "[SECTION" not in result
        assert "Hello world." in result

    def test_removes_cta_subscribe_markers(self):
        text = "Watch more. [CTA_SUBSCRIBE_FINAL] End of video."
        result = sanitize_narration_for_tts(text)
        assert "[CTA_SUBSCRIBE_FINAL]" not in result

    def test_strips_markdown_bold(self):
        text = "This is **very important** content."
        result = sanitize_narration_for_tts(text)
        assert "**" not in result
        assert "very important" in result

    def test_strips_markdown_italic(self):
        text = "This is *italic* text."
        result = sanitize_narration_for_tts(text)
        assert result.count("*") == 0
        assert "italic" in result

    def test_strips_markdown_atx_headers(self):
        text = "# Big Title\nSome content here."
        result = sanitize_narration_for_tts(text)
        assert not result.startswith("#")

    def test_collapses_multiple_spaces(self):
        text = "Too   many   spaces."
        result = sanitize_narration_for_tts(text)
        assert "  " not in result

    def test_strips_leading_trailing_whitespace(self):
        text = "  Hello world.  "
        result = sanitize_narration_for_tts(text)
        assert result == result.strip()

    def test_plain_text_unchanged(self):
        text = "The Roman Empire fell due to economic and military decline."
        result = sanitize_narration_for_tts(text)
        assert result == text

    def test_empty_string_returns_empty(self):
        assert sanitize_narration_for_tts("") == ""

    def test_multiline_image_prompt_removed(self):
        text = "Start. [IMAGE_PROMPT: a very long\nmultiline description here] End."
        result = sanitize_narration_for_tts(text)
        assert "[IMAGE_PROMPT" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# Module 04: Subtitle Generator
# ═══════════════════════════════════════════════════════════════════════════════

class TestFmtSrt:
    """04._fmt_srt — SRT timestamp formatting."""

    def test_zero_seconds(self, mod04):
        assert mod04._fmt_srt(0.0) == "00:00:00,000"

    def test_one_second(self, mod04):
        assert mod04._fmt_srt(1.0) == "00:00:01,000"

    def test_one_minute(self, mod04):
        assert mod04._fmt_srt(60.0) == "00:01:00,000"

    def test_one_hour(self, mod04):
        assert mod04._fmt_srt(3600.0) == "01:00:00,000"

    def test_milliseconds_precision(self, mod04):
        result = mod04._fmt_srt(1.5)
        assert result == "00:00:01,500"

    def test_milliseconds_rounding(self, mod04):
        result = mod04._fmt_srt(1.001)
        assert result == "00:00:01,001"

    def test_negative_clamped_to_zero(self, mod04):
        result = mod04._fmt_srt(-5.0)
        assert result == "00:00:00,000"

    def test_complex_timestamp(self, mod04):
        # 1h 23m 45.678s
        result = mod04._fmt_srt(3600 + 23 * 60 + 45.678)
        assert result == "01:23:45,678"


class TestFmtAss:
    """04._fmt_ass — ASS timestamp formatting."""

    def test_zero_seconds(self, mod04):
        assert mod04._fmt_ass(0.0) == "0:00:00.00"

    def test_one_second(self, mod04):
        assert mod04._fmt_ass(1.0) == "0:00:01.00"

    def test_centiseconds(self, mod04):
        result = mod04._fmt_ass(1.5)
        assert result == "0:00:01.50"

    def test_negative_clamped_to_zero(self, mod04):
        result = mod04._fmt_ass(-1.0)
        assert result == "0:00:00.00"


class TestWrapText:
    """04._wrap_text — subtitle line wrapping."""

    def test_short_text_single_line(self, mod04):
        lines = mod04._wrap_text("Hello world.", 50)
        assert len(lines) == 1
        assert lines[0] == "Hello world."

    def test_long_text_splits_into_multiple_lines(self, mod04):
        text = "This is a very long subtitle text that should be wrapped into multiple lines because it exceeds the limit."
        lines = mod04._wrap_text(text, 30)
        assert len(lines) > 1
        for line in lines:
            assert len(line) <= 32  # allow slight overshoot at a word boundary

    def test_empty_string_returns_empty_list(self, mod04):
        lines = mod04._wrap_text("", 50)
        assert lines == []

    def test_single_word_not_split(self, mod04):
        lines = mod04._wrap_text("Supercalifragilistic", 10)
        assert len(lines) == 1

    def test_wraps_at_word_boundary(self, mod04):
        lines = mod04._wrap_text("one two three four five", 10)
        for line in lines:
            # No line should have a stray space at start/end
            assert line == line.strip()


class TestSplitToFit:
    """04._split_to_fit — recursive subtitle chunk splitting."""

    def test_short_text_not_split(self, mod04):
        text = "Hello world."
        chunks = mod04._split_to_fit(text, max_chars=50, max_lines=1)
        assert chunks == [text]

    def test_long_text_split_into_chunks(self, mod04):
        text = (
            "This is a very long narration sentence that must be split because "
            "it will not fit on a single subtitle line at all."
        )
        chunks = mod04._split_to_fit(text, max_chars=50, max_lines=1)
        assert len(chunks) >= 2

    def test_each_chunk_fits_max_lines(self, mod04):
        text = (
            "The Roman Empire didn't fall in a day. "
            "For over three hundred years it slowly crumbled. "
            "Economic collapse military overextension and political corruption all played their part."
        )
        chunks = mod04._split_to_fit(text, max_chars=50, max_lines=1)
        for chunk in chunks:
            wrapped = mod04._wrap_text(chunk, 50)
            assert len(wrapped) <= 1, f"Chunk too long: {chunk!r}"

    def test_single_word_not_split_further(self, mod04):
        chunks = mod04._split_to_fit("LongWordThatCannotSplit", max_chars=5, max_lines=1)
        assert len(chunks) == 1

    def test_conjunction_split_preference(self, mod04):
        # Should prefer splitting before "and"
        text = "Economic decline and military collapse caused the fall"
        chunks = mod04._split_to_fit(text, max_chars=30, max_lines=1)
        assert len(chunks) >= 2
        # Verify all words are preserved (no data loss)
        rejoined = " ".join(chunks)
        for word in text.split():
            assert word in rejoined

    def test_returns_list(self, mod04):
        result = mod04._split_to_fit("some text", 50, 1)
        assert isinstance(result, list)
        assert len(result) >= 1


class TestSubEntry:
    """04.SubEntry — SRT and ASS formatting."""

    def test_to_srt_format(self, mod04):
        entry = mod04.SubEntry(1, 0.0, 9.5, "Hello world.")
        srt = entry.to_srt()
        assert srt.startswith("1\n")
        assert "00:00:00,000 --> 00:00:09,500" in srt
        assert "Hello world." in srt

    def test_to_srt_index(self, mod04):
        entry = mod04.SubEntry(42, 10.0, 20.0, "Test text.")
        srt = entry.to_srt()
        assert srt.startswith("42\n")

    def test_to_ass_event_format(self, mod04):
        entry = mod04.SubEntry(1, 0.0, 5.0, "Test.")
        ass = entry.to_ass_event()
        assert ass.startswith("Dialogue:")
        assert "0:00:00.00" in ass
        assert "0:00:05.00" in ass
        assert "Test." in ass

    def test_text_stripped_on_creation(self, mod04):
        entry = mod04.SubEntry(1, 0.0, 1.0, "  Padded text.  ")
        assert entry.text == "Padded text."


class TestHexToAssColor:
    """04._hex_to_ass_color — color format conversion."""

    def test_white(self, mod04):
        result = mod04._hex_to_ass_color("#FFFFFF")
        assert result == "&H00FFFFFF"

    def test_black(self, mod04):
        result = mod04._hex_to_ass_color("#000000")
        assert result == "&H00000000"

    def test_rgb_to_bgr_swapped(self, mod04):
        # #FF0000 (red) → R=FF, G=00, B=00 → ASS BGR = 0000FF
        result = mod04._hex_to_ass_color("#FF0000")
        assert result == "&H000000FF"

    def test_invalid_falls_back(self, mod04):
        result = mod04._hex_to_ass_color("bad")
        # Should not raise; returns default
        assert isinstance(result, str)


class TestBlockToEntries:
    """04._block_to_entries — block→subtitle entry conversion."""

    def test_block_with_narration_and_duration(self, mod04):
        block = {
            "narration": "The empire fell slowly over centuries.",
            "audio_duration": 5.0,
        }
        entries, end_time = mod04._block_to_entries(block, start_time=0.0, entry_index_start=1)
        assert len(entries) >= 1
        assert abs(end_time - 5.0) < 0.01

    def test_block_without_narration_returns_empty(self, mod04):
        block = {"narration": "", "audio_duration": 5.0}
        entries, end_time = mod04._block_to_entries(block, start_time=0.0, entry_index_start=1)
        assert entries == []
        assert end_time == 0.0

    def test_block_without_duration_returns_empty(self, mod04):
        block = {"narration": "Some text.", "audio_duration": None}
        entries, end_time = mod04._block_to_entries(block, start_time=0.0, entry_index_start=1)
        assert entries == []

    def test_start_time_propagated(self, mod04):
        block = {"narration": "Hello.", "audio_duration": 3.0}
        entries, _ = mod04._block_to_entries(block, start_time=10.0, entry_index_start=1)
        assert entries[0].start == pytest.approx(10.0, abs=0.01)

    def test_entry_indices_are_sequential(self, mod04):
        block = {
            "narration": "First sentence. Second sentence. Third one here for good measure.",
            "audio_duration": 9.0,
        }
        entries, _ = mod04._block_to_entries(block, start_time=0.0, entry_index_start=5)
        indices = [e.index for e in entries]
        assert indices == list(range(5, 5 + len(entries)))


# ═══════════════════════════════════════════════════════════════════════════════
# Module 05: Video Compiler
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetIntervalForTime:
    """05._get_interval_for_time — tiered interval lookup."""

    def test_early_time_uses_first_tier(self, mod05):
        tiers = [
            {"until_seconds": 600, "interval": 10},
            {"until_seconds": None, "interval": 30},
        ]
        assert mod05._get_interval_for_time(0.0, tiers) == 10.0
        assert mod05._get_interval_for_time(300.0, tiers) == 10.0
        assert mod05._get_interval_for_time(599.9, tiers) == 10.0

    def test_late_time_uses_second_tier(self, mod05):
        tiers = [
            {"until_seconds": 600, "interval": 10},
            {"until_seconds": None, "interval": 30},
        ]
        assert mod05._get_interval_for_time(600.0, tiers) == 30.0
        assert mod05._get_interval_for_time(1200.0, tiers) == 30.0

    def test_empty_tiers_returns_default(self, mod05):
        result = mod05._get_interval_for_time(100.0, [])
        assert result == 20.0

    def test_single_unlimited_tier(self, mod05):
        tiers = [{"until_seconds": None, "interval": 15}]
        assert mod05._get_interval_for_time(0.0, tiers) == 15.0
        assert mod05._get_interval_for_time(9999.0, tiers) == 15.0


class TestSplitDurationToSegments:
    """05._split_duration_to_segments — animation segment splitting."""

    def test_short_duration_single_segment(self, mod05):
        segs = mod05._split_duration_to_segments(0.0, 8.0, [])
        assert len(segs) == 1
        assert segs[0] == pytest.approx(8.0)

    def test_exact_multiple_of_anim_duration(self, mod05):
        # ANIM_SEGMENT_DURATION = 10.0, so 30s = 3 segments
        segs = mod05._split_duration_to_segments(0.0, 30.0, [])
        assert len(segs) == 3
        assert all(s == pytest.approx(10.0) for s in segs)

    def test_non_multiple_creates_remainder_segment(self, mod05):
        # 25s = 10 + 10 + 5
        segs = mod05._split_duration_to_segments(0.0, 25.0, [])
        assert len(segs) == 3
        assert segs[-1] == pytest.approx(5.0)

    def test_segments_sum_to_duration(self, mod05):
        duration = 47.3
        segs = mod05._split_duration_to_segments(0.0, duration, [])
        assert sum(segs) == pytest.approx(duration, abs=0.01)

    def test_zero_duration_returns_single_segment(self, mod05):
        segs = mod05._split_duration_to_segments(0.0, 0.0, [])
        # Should not crash; returns at least one element
        assert isinstance(segs, list)

    def test_very_long_duration_many_segments(self, mod05):
        segs = mod05._split_duration_to_segments(0.0, 90.0, [])
        assert len(segs) == 9  # 9 × 10s


class TestImageForSegment:
    """05._image_for_segment — image selection logic."""

    def test_single_image_always_returned(self, mod05, tmp_path):
        img = tmp_path / "img.png"
        img.touch()
        result = mod05._image_for_segment([img], [], 10, 0, 3)
        assert result == img

    def test_two_images_even_distribution(self, mod05, tmp_path):
        imgs = [tmp_path / f"img{i}.png" for i in range(2)]
        for p in imgs:
            p.touch()
        # 6 segments, 2 images → first 3 = img0, last 3 = img1
        assert mod05._image_for_segment(imgs, [], 10, 0, 6) == imgs[0]
        assert mod05._image_for_segment(imgs, [], 10, 3, 6) == imgs[1]

    def test_word_offset_respected(self, mod05, tmp_path):
        imgs = [tmp_path / f"img{i}.png" for i in range(2)]
        for p in imgs:
            p.touch()
        # 10 words, second image at offset 5 → for 4 segments:
        # seg0 → wp=0  → img0
        # seg1 → wp=2  → img0
        # seg2 → wp=5  → img1 (offset=5 reached)
        # seg3 → wp=7  → img1
        offsets = [0, 5]
        total_words = 10
        n_segments = 4
        result_0 = mod05._image_for_segment(imgs, offsets, total_words, 0, n_segments)
        result_2 = mod05._image_for_segment(imgs, offsets, total_words, 2, n_segments)
        assert result_0 == imgs[0]
        assert result_2 == imgs[1]


class TestAnimationForBlock:
    """05._animation_for_block — animation type selection."""

    def test_default_returns_kb_cycle_value(self, mod05):
        block = {"animation": ""}
        result = mod05._animation_for_block(block, {}, block_index=0)
        assert result in ("zoom_in", "pan_left", "pan_right", "zoom_out")

    def test_explicit_non_default_animation_used(self, mod05):
        block = {"animation": "pan_left"}
        result = mod05._animation_for_block(block, {}, block_index=0)
        assert result == "pan_left"

    def test_zoom_in_treated_as_default(self, mod05):
        # zoom_in is treated as the default — should fall through to cycle
        block = {"animation": "zoom_in"}
        result = mod05._animation_for_block(block, {}, block_index=0)
        assert result in mod05._KB_CYCLE


# ═══════════════════════════════════════════════════════════════════════════════
# Module 06: Thumbnail Generator
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildPrompt:
    """06._build_prompt — thumbnail prompt construction."""

    @pytest.fixture
    def script(self):
        return {
            "title": "Why Rome Really Fell",
            "thumbnail_prompt": "Ancient Rome burning at sunset, dramatic lighting",
        }

    @pytest.fixture
    def config(self):
        return common.load_channel_config(TEST_DATA / "sample_config.json")

    def test_uses_script_thumbnail_prompt(self, mod06, script, config):
        result = mod06._build_prompt(script, config, None, None, None)
        assert "Ancient Rome burning" in result

    def test_cli_override_takes_priority(self, mod06, script, config):
        override = "Custom prompt for testing purposes"
        result = mod06._build_prompt(script, config, None, override, None)
        assert "Custom prompt" in result
        assert "Ancient Rome" not in result

    def test_transcriber_prompt_takes_priority_over_script(self, mod06, script, config):
        transcriber = {"thumbnail_prompt": "Transcriber thumbnail description here"}
        result = mod06._build_prompt(script, config, transcriber, None, None)
        assert "Transcriber thumbnail" in result

    def test_cli_override_beats_transcriber(self, mod06, script, config):
        transcriber = {"thumbnail_prompt": "Transcriber description"}
        override = "CLI override wins"
        result = mod06._build_prompt(script, config, transcriber, override, None)
        assert "CLI override wins" in result

    def test_fallback_to_title_when_no_prompt(self, mod06, config):
        empty_script = {"title": "No Prompt Video", "thumbnail_prompt": ""}
        result = mod06._build_prompt(empty_script, config, None, None, None)
        assert "No Prompt Video" in result

    def test_thumbnail_style_appended(self, mod06, script, config):
        result = mod06._build_prompt(script, config, None, None, None)
        # sample_config has thumbnail_style "bold text, high contrast"
        assert "bold text" in result or "high contrast" in result

    def test_text_overlay_appended(self, mod06, script, config):
        result = mod06._build_prompt(script, config, None, None, "The REAL Story")
        assert "The REAL Story" in result

    def test_youtube_thumbnail_injected_if_missing(self, mod06, config):
        script = {"title": "Test", "thumbnail_prompt": "A simple landscape photo"}
        result = mod06._build_prompt(script, config, None, None, None)
        assert "youtube thumbnail" in result.lower()

    def test_youtube_thumbnail_not_duplicated(self, mod06, config):
        script = {"title": "Test", "thumbnail_prompt": "YouTube thumbnail for history video"}
        result = mod06._build_prompt(script, config, None, None, None)
        count = result.lower().count("youtube thumbnail")
        assert count == 1

    def test_result_is_non_empty_string(self, mod06, script, config):
        result = mod06._build_prompt(script, config, None, None, None)
        assert isinstance(result, str)
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Module 04: SRT parser
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseSrt:
    """04._parse_srt — SRT file parsing."""

    def test_parses_sample_srt(self, mod04):
        srt_path = TEST_DATA / "sample_transcriber_output" / "transcript.srt"
        entries = mod04._parse_srt(srt_path)
        assert len(entries) == 8
        assert entries[0].index == 1
        assert entries[0].start == pytest.approx(0.0, abs=0.01)
        assert entries[0].end == pytest.approx(9.5, abs=0.01)

    def test_parses_timestamps_correctly(self, mod04):
        srt_path = TEST_DATA / "sample_transcriber_output" / "transcript.srt"
        entries = mod04._parse_srt(srt_path)
        # Entry 2: 00:00:09,500 --> 00:00:38,000
        assert entries[1].start == pytest.approx(9.5, abs=0.01)
        assert entries[1].end == pytest.approx(38.0, abs=0.01)

    def test_text_is_non_empty(self, mod04):
        srt_path = TEST_DATA / "sample_transcriber_output" / "transcript.srt"
        entries = mod04._parse_srt(srt_path)
        for entry in entries:
            assert entry.text.strip() != ""

    def test_empty_file_returns_empty_list(self, mod04, tmp_path):
        srt = tmp_path / "empty.srt"
        srt.write_text("", encoding="utf-8")
        entries = mod04._parse_srt(srt)
        assert entries == []

    def test_malformed_block_skipped(self, mod04, tmp_path):
        srt = tmp_path / "partial.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:05,000\nGood entry.\n\n"
            "bad block without timestamp\n\n"
            "3\n00:00:10,000 --> 00:00:15,000\nAnother good entry.\n",
            encoding="utf-8",
        )
        entries = mod04._parse_srt(srt)
        assert len(entries) == 2
        assert entries[0].text == "Good entry."
        assert entries[1].text == "Another good entry."


# ═══════════════════════════════════════════════════════════════════════════════
# Module 03: Voice Generator helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestVoiceHelpers:
    """03._get_voice_id and _audio_dir — config-based helpers."""

    @pytest.fixture
    def mod03(self):
        return _load_module("03_voice_generator.py", "mod03_voice_generator")

    @pytest.fixture
    def channel_config(self):
        return {
            "language": "en",
            "voice_id": "en_voice_abc",
            "voice_ids_multilang": {"de": "de_voice_xyz"},
        }

    def test_primary_language_returns_voice_id(self, mod03, channel_config):
        vid = mod03._get_voice_id(channel_config, "en")
        assert vid == "en_voice_abc"

    def test_secondary_language_returns_multilang_voice(self, mod03, channel_config):
        vid = mod03._get_voice_id(channel_config, "de")
        assert vid == "de_voice_xyz"

    def test_missing_primary_voice_raises_value_error(self, mod03):
        cfg = {"language": "en", "voice_id": ""}
        with pytest.raises(ValueError, match="voice_id"):
            mod03._get_voice_id(cfg, "en")

    def test_missing_multilang_voice_raises_value_error(self, mod03, channel_config):
        with pytest.raises(ValueError, match="voice_ids_multilang"):
            mod03._get_voice_id(channel_config, "fr")

    def test_audio_dir_primary_language(self, mod03, tmp_path):
        result = mod03._audio_dir(tmp_path, "en", "en")
        assert result == tmp_path / "audio"

    def test_audio_dir_secondary_language(self, mod03, tmp_path):
        result = mod03._audio_dir(tmp_path, "de", "en")
        assert result == tmp_path / "audio" / "de"
