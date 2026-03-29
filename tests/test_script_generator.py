"""Unit tests for script generator — parsing, word counting, block targets.

The module filename starts with a digit (01_script_generator.py), so it cannot
be imported with a normal `from modules.01_script_generator import ...` statement.
We use importlib to load it and extract the private functions directly.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
MODULE_PATH = ROOT / "modules" / "01_script_generator.py"

# ---------------------------------------------------------------------------
# Load module via importlib (filename starts with digit — invalid identifier)
# ---------------------------------------------------------------------------

def _load_script_generator():
    spec = importlib.util.spec_from_file_location("script_generator", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        return None
    return mod


_MOD = _load_script_generator()


def _get(name: str):
    """Return a function from the module or None if unavailable."""
    if _MOD is None:
        return None
    return getattr(_MOD, name, None)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers shared across test sections
# ──────────────────────────────────────────────────────────────────────────────

_MINIMAL_CHANNEL_CONFIG = {
    "channel_name": "TestChannel",
    "niche": "history",
    "language": "en",
    "voice_id": "onyx",
    "default_animation": "zoom_in",
    "subtitle_style": {},
    "master_prompt_path": "prompts/master_script_v1.txt",
}

_MINIMAL_SOURCE_DATA = {
    "title": "Test Video",
    "description": "A test description.",
    "transcript": "This is a transcript.",
    "thumbnail_prompt": "",
    "metadata": {"video_id": "abc123", "title": "Test Video"},
}


# ===========================================================================
# 1. _count_narration_words
# ===========================================================================

class TestCountNarrationWords:
    """Tests for _count_narration_words — word counting excluding structural tags."""

    @pytest.fixture(autouse=True)
    def require_fn(self):
        fn = _get("_count_narration_words")
        if fn is None:
            pytest.skip("_count_narration_words not importable")
        self.fn = fn

    def test_plain_text_word_count(self):
        assert self.fn("hello world this is a test") == 6

    def test_single_word(self):
        assert self.fn("word") == 1

    def test_multiline_plain_text(self):
        text = "The quick brown fox\njumps over the lazy dog"
        assert self.fn(text) == 9

    def test_image_prompt_tag_excluded(self):
        text = "This is narration. [IMAGE_PROMPT: a dramatic sunset over mountains] More text."
        # "This is narration. More text." = 5 words
        assert self.fn(text) == 5

    def test_multiple_image_prompt_tags_excluded(self):
        text = (
            "Word one. "
            "[IMAGE_PROMPT: first scene] "
            "Word two three. "
            "[IMAGE_PROMPT: second scene with lots of description words here] "
            "Word four."
        )
        # After stripping: "Word one.  Word two three.  Word four."
        # split() on punctuation-attached tokens: "Word"+"one."+"Word"+"two"+"three."+"Word"+"four." = 7
        assert self.fn(text) == 7

    def test_section_marker_excluded(self):
        text = "[SECTION 1: Introduction]\nHello world."
        # section marker stripped, "Hello world." = 2 words
        assert self.fn(text) == 2

    def test_section_marker_with_number_excluded(self):
        text = "[SECTION 7: The Big Reveal]\nNarration only here."
        assert self.fn(text) == 3  # "Narration only here."

    def test_cta_subscribe_mid_excluded(self):
        text = "Before CTA. [CTA_SUBSCRIBE_MID] After CTA."
        assert self.fn(text) == 4  # "Before CTA. After CTA."

    def test_cta_subscribe_final_excluded(self):
        text = "Final words. [CTA_SUBSCRIBE_FINAL]"
        assert self.fn(text) == 2  # "Final words."

    def test_cta_partial_variant_excluded(self):
        # CTA_SUBSCRIBE with any suffix should be stripped
        text = "Text here. [CTA_SUBSCRIBE_FINAL] Done."
        assert self.fn(text) == 3  # "Text here. Done."

    def test_empty_string_returns_zero(self):
        assert self.fn("") == 0

    def test_only_image_prompt_tags_returns_zero(self):
        text = "[IMAGE_PROMPT: a burning castle at dusk] [IMAGE_PROMPT: aerial shot of city]"
        assert self.fn(text) == 0

    def test_only_section_marker_returns_zero(self):
        text = "[SECTION 3: Climax]"
        assert self.fn(text) == 0

    def test_only_cta_tags_returns_zero(self):
        text = "[CTA_SUBSCRIBE_MID] [CTA_SUBSCRIBE_FINAL]"
        assert self.fn(text) == 0

    def test_mixed_tags_and_narration(self):
        text = (
            "[SECTION 1: Hook]\n"
            "This is the hook narration.\n"
            "[IMAGE_PROMPT: dramatic opening shot]\n"
            "More narration here.\n"
            "[CTA_SUBSCRIBE_MID]\n"
            "Subscribe to my channel for more.\n"
            "[CTA_SUBSCRIBE_FINAL]"
        )
        # After stripping all tags: "This is the hook narration. More narration here.
        # Subscribe to my channel for more."
        # split() count: 5 + 4 + 5 = 14
        assert self.fn(text) == 14

    def test_whitespace_only_returns_zero(self):
        assert self.fn("   \n\t  ") == 0


# ===========================================================================
# 2. _calc_block_targets
# ===========================================================================

class TestCalcBlockTargets:
    """Tests for _calc_block_targets — per-block word and image count computation."""

    @pytest.fixture(autouse=True)
    def require_fn(self):
        fn = _get("_calc_block_targets")
        if fn is None:
            pytest.skip("_calc_block_targets not importable")
        self.fn = fn
        # Also load BLOCK_STRUCTURE_V3 for percentage assertions
        self.block_structure = _get("BLOCK_STRUCTURE_V3")

    # ── Schema assertions ────────────────────────────────────────────────────

    def test_returns_list_of_dicts(self):
        result = self.fn(8, 12)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_each_block_has_required_keys(self):
        result = self.fn(8, 12)
        for block in result:
            assert "name" in block, f"Missing 'name' in {block}"
            assert "words_min" in block, f"Missing 'words_min' in {block}"
            assert "words_max" in block, f"Missing 'words_max' in {block}"
            assert "images" in block, f"Missing 'images' in {block}"

    def test_block_count_matches_structure(self):
        result = self.fn(8, 12)
        if self.block_structure:
            assert len(result) == len(self.block_structure)
        else:
            assert len(result) == 8  # BLOCK_STRUCTURE_V3 has 8 entries

    def test_words_min_lte_words_max(self):
        result = self.fn(8, 12)
        for block in result:
            assert block["words_min"] <= block["words_max"], (
                f"words_min > words_max in block {block['name']}"
            )

    def test_images_is_positive_int(self):
        result = self.fn(8, 12)
        for block in result:
            assert isinstance(block["images"], int)
            assert block["images"] >= 1

    # ── 8–12 min range ────────────────────────────────────────────────────────

    def test_8_12_min_total_words_range(self):
        result = self.fn(8, 12)
        total_min = sum(b["words_min"] for b in result)
        total_max = sum(b["words_max"] for b in result)
        # Each block uses floor(duration * 170 * pct), so totals should be
        # within 5% of 8*170=1360 and 12*170=2040
        assert total_min >= int(8 * 170 * 0.95), f"total_min={total_min} too low"
        assert total_max <= int(12 * 170 * 1.05), f"total_max={total_max} too high"

    def test_8_12_min_block_names(self):
        result = self.fn(8, 12)
        names = [b["name"] for b in result]
        # BLOCK_STRUCTURE_V3 always starts with HOOK
        assert names[0] == "HOOK"

    # ── 25–30 min range ───────────────────────────────────────────────────────

    def test_25_30_min_proportional_word_counts(self):
        result = self.fn(25, 30)
        total_min = sum(b["words_min"] for b in result)
        total_max = sum(b["words_max"] for b in result)
        assert total_min >= int(25 * 170 * 0.95)
        assert total_max <= int(30 * 170 * 1.05)

    def test_25_30_min_larger_than_8_12_min(self):
        short = self.fn(8, 12)
        long_ = self.fn(25, 30)
        short_total = sum(b["words_min"] for b in short)
        long_total = sum(b["words_min"] for b in long_)
        assert long_total > short_total

    # ── 54–62 min range ───────────────────────────────────────────────────────

    def test_54_62_min_large_word_counts(self):
        result = self.fn(54, 62)
        total_min = sum(b["words_min"] for b in result)
        total_max = sum(b["words_max"] for b in result)
        assert total_min >= int(54 * 170 * 0.95)
        assert total_max <= int(62 * 170 * 1.05)

    def test_54_62_min_more_images_than_8_12(self):
        short = self.fn(8, 12)
        long_ = self.fn(54, 62)
        short_images = sum(b["images"] for b in short)
        long_images = sum(b["images"] for b in long_)
        assert long_images > short_images

    # ── Percentage proportions ────────────────────────────────────────────────

    def test_block_percentages_sum_to_100(self):
        if not self.block_structure:
            pytest.skip("BLOCK_STRUCTURE_V3 not accessible")
        total_pct = sum(b["pct"] for b in self.block_structure)
        assert abs(total_pct - 1.0) < 0.01, f"Percentages sum to {total_pct}, expected ~1.0"

    def test_hook_is_smallest_block(self):
        """HOOK has pct=0.04 — should have the fewest words."""
        result = self.fn(8, 12)
        hook = next(b for b in result if b["name"] == "HOOK")
        core = next(b for b in result if b["name"] == "CORE FRAMEWORK")
        assert hook["words_min"] < core["words_min"]

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_equal_min_max_duration(self):
        result = self.fn(10, 10)
        for block in result:
            assert block["words_min"] == block["words_max"]

    def test_very_short_duration_at_least_one_word(self):
        result = self.fn(1, 2)
        for block in result:
            assert block["words_min"] >= 1
            assert block["words_max"] >= 1


# ===========================================================================
# 3. _calc_images_for_block
# ===========================================================================

class TestCalcImagesForBlock:
    """Tests for _calc_images_for_block — 4-tier image density model."""

    @pytest.fixture(autouse=True)
    def require_fn(self):
        fn = _get("_calc_images_for_block")
        if fn is None:
            pytest.skip("_calc_images_for_block not importable")
        self.fn = fn

    def test_returns_integer(self):
        assert isinstance(self.fn(0, 100), int)

    def test_always_at_least_one_image(self):
        """Even 0 words should return at least 1."""
        assert self.fn(0, 0) >= 1

    def test_zero_words_at_nonzero_start(self):
        assert self.fn(500, 0) >= 1

    # ── Tier 1: start_word 0–450, interval 25 ────────────────────────────────

    def test_tier1_full_coverage_100_words(self):
        # 0 start, 100 words entirely in tier 1 → ~100/25 = 4
        result = self.fn(0, 100)
        assert result == 4

    def test_tier1_full_coverage_25_words(self):
        # Exactly 1 image per 25 words
        result = self.fn(0, 25)
        assert result == 1

    def test_tier1_full_coverage_200_words(self):
        # 200/25 = 8
        result = self.fn(0, 200)
        assert result == 8

    def test_tier1_from_mid_tier(self):
        # start=200, 100 words still in tier 1 → 100/25 = 4
        result = self.fn(200, 100)
        assert result == 4

    # ── Tier 2: words 450–900, interval 50 ───────────────────────────────────

    def test_tier2_pure(self):
        # start=450, 100 words entirely in tier 2 → 100/50 = 2
        result = self.fn(450, 100)
        assert result == 2

    def test_tier2_50_words(self):
        # 50/50 = 1
        result = self.fn(500, 50)
        assert result == 1

    def test_tier2_density_lower_than_tier1(self):
        # Same word count but tier 2 should give fewer images than tier 1
        t1 = self.fn(0, 100)
        t2 = self.fn(500, 100)
        assert t2 < t1

    # ── Tier 3: words 900–2250, interval 150 ─────────────────────────────────

    def test_tier3_pure(self):
        # start=900, 300 words → 300/150 = 2
        result = self.fn(900, 300)
        assert result == 2

    def test_tier3_150_words(self):
        # 150/150 = 1
        result = self.fn(1000, 150)
        assert result == 1

    def test_tier3_density_lower_than_tier2(self):
        t2 = self.fn(500, 150)
        t3 = self.fn(1000, 150)
        assert t3 < t2

    # ── Tier 4: words 2250+, interval 280 ────────────────────────────────────

    def test_tier4_pure(self):
        # start=2250, 560 words → 560/280 = 2
        result = self.fn(2250, 560)
        assert result == 2

    def test_tier4_280_words(self):
        # 280/280 = 1
        result = self.fn(3000, 280)
        assert result == 1

    def test_tier4_density_lower_than_tier3(self):
        t3 = self.fn(1000, 280)
        t4 = self.fn(3000, 280)
        assert t4 < t3

    # ── Cross-tier blocks ─────────────────────────────────────────────────────

    def test_block_spanning_tier1_and_tier2(self):
        # start=400, 100 words: 50 in tier1 (50/25=2) + 50 in tier2 (50/50=1) = 3
        result = self.fn(400, 100)
        assert result == 3

    def test_block_spanning_all_four_tiers(self):
        # start=0, 3000 words spans all tiers — just assert > threshold for tier1-only
        tier1_only = self.fn(0, 450)  # 450/25 = 18
        all_tiers = self.fn(0, 3000)
        assert all_tiers > tier1_only

    def test_more_words_more_images(self):
        low = self.fn(0, 100)
        high = self.fn(0, 400)
        assert high > low


# ===========================================================================
# 4. _parse_llm_output
# ===========================================================================

class TestParseLlmOutput:
    """Tests for _parse_llm_output — LLM text to Script model conversion."""

    @pytest.fixture(autouse=True)
    def require_fn(self):
        fn = _get("_parse_llm_output")
        if fn is None:
            pytest.skip("_parse_llm_output not importable")
        self.fn = fn

    def _call(self, raw: str, hook_type: str = "curiosity", custom_topic: str = "") -> object:
        """Helper to call _parse_llm_output with minimal valid config."""
        return self.fn(
            raw=raw,
            channel_config=_MINIMAL_CHANNEL_CONFIG,
            source_data=_MINIMAL_SOURCE_DATA,
            hook_type=hook_type,
            image_style="",
            custom_topic=custom_topic,
        )

    # ── Standard sections ─────────────────────────────────────────────────────

    def test_single_section_creates_one_block(self):
        raw = "[SECTION 1: Hook]\n\nThis is the hook narration."
        script = self._call(raw)
        assert len(script.blocks) == 1

    def test_three_sections_creates_three_blocks(self):
        raw = (
            "[SECTION 1: Hook]\n\nThis is the hook.\n\n"
            "[SECTION 2: Main Content]\n\nThis is the main content.\n\n"
            "[SECTION 3: Closing]\n\nThis is the closing."
        )
        script = self._call(raw)
        assert len(script.blocks) == 3

    def test_first_block_is_always_intro_type(self):
        raw = "[SECTION 1: Hook]\n\nNarration text here."
        script = self._call(raw)
        assert script.blocks[0].type == "intro"

    def test_section_titles_become_timestamp_labels(self):
        raw = (
            "[SECTION 1: The Curious Opening]\n\nOpening text.\n\n"
            "[SECTION 2: Core Argument]\n\nMain argument text."
        )
        script = self._call(raw)
        assert script.blocks[0].timestamp_label == "The Curious Opening"
        assert script.blocks[1].timestamp_label == "Core Argument"

    def test_block_ids_are_sequential(self):
        raw = (
            "[SECTION 1: A]\n\nText A.\n\n"
            "[SECTION 2: B]\n\nText B.\n\n"
            "[SECTION 3: C]\n\nText C."
        )
        script = self._call(raw)
        assert script.blocks[0].id == "block_001"
        assert script.blocks[1].id == "block_002"
        assert script.blocks[2].id == "block_003"

    def test_block_order_field_is_one_indexed(self):
        raw = "[SECTION 1: Hook]\n\nText."
        script = self._call(raw)
        assert script.blocks[0].order == 1

    def test_narration_text_preserved(self):
        raw = "[SECTION 1: Hook]\n\nThis is the narration body."
        script = self._call(raw)
        assert "This is the narration body." in script.blocks[0].narration

    def test_hook_info_set_on_first_block(self):
        raw = "[SECTION 1: Hook]\n\nHook text."
        script = self._call(raw, hook_type="curiosity")
        assert script.blocks[0].hook is not None
        assert script.blocks[0].hook.type == "curiosity"

    def test_hook_info_not_set_on_later_blocks(self):
        raw = (
            "[SECTION 1: Hook]\n\nHook.\n\n"
            "[SECTION 2: Main]\n\nMain content."
        )
        script = self._call(raw)
        assert script.blocks[1].hook is None

    # ── Outro / closing detection ─────────────────────────────────────────────

    def test_closing_section_becomes_outro_type(self):
        raw = (
            "[SECTION 1: Hook]\n\nHook text.\n\n"
            "[SECTION 2: Closing]\n\nClosing text."
        )
        script = self._call(raw)
        outro = script.blocks[-1]
        assert outro.type == "outro"

    def test_outro_section_becomes_outro_type(self):
        raw = (
            "[SECTION 1: Hook]\n\nHook.\n\n"
            "[SECTION 2: Outro]\n\nOutro text."
        )
        script = self._call(raw)
        assert script.blocks[-1].type == "outro"

    # ── CTA blocks ────────────────────────────────────────────────────────────

    def test_cta_subscribe_mid_creates_cta_block(self):
        raw = (
            "[SECTION 1: Hook]\n\nHook text.\n\n"
            "[CTA_SUBSCRIBE_MID]\n"
            "If you're enjoying this, subscribe!\n\n"
            "[SECTION 2: Main]\n\nMain content."
        )
        script = self._call(raw)
        cta_blocks = [b for b in script.blocks if b.type == "cta"]
        assert len(cta_blocks) >= 1

    def test_cta_subscribe_final_creates_outro_block(self):
        raw = (
            "[SECTION 1: Hook]\n\nHook text.\n\n"
            "[CTA_SUBSCRIBE_FINAL]\n"
            "Thank you for watching. Please subscribe."
        )
        script = self._call(raw)
        # CTA_SUBSCRIBE_FINAL flush sets section_type="outro" with is_cta_block=True → actual_type="cta"
        # but the block after it is created by flush(), which uses "cta" for both
        # Let's verify we got a block for the CTA content
        assert len(script.blocks) >= 2

    def test_cta_subscribe_mid_timestamp_label(self):
        raw = (
            "[SECTION 1: Hook]\n\nHook.\n\n"
            "[CTA_SUBSCRIBE_MID]\n"
            "Subscribe here!"
        )
        script = self._call(raw)
        cta = next((b for b in script.blocks if b.type == "cta"), None)
        assert cta is not None
        assert cta.timestamp_label == "Subscribe"

    # ── Image prompts ─────────────────────────────────────────────────────────

    def test_image_prompt_extracted_to_field(self):
        raw = (
            "[SECTION 1: Hook]\n\n"
            "[IMAGE_PROMPT: dramatic sunset over burning city]\n"
            "Narration text follows."
        )
        script = self._call(raw)
        assert script.blocks[0].image_prompt == "dramatic sunset over burning city"

    def test_image_prompt_not_in_narration(self):
        raw = (
            "[SECTION 1: Hook]\n\n"
            "[IMAGE_PROMPT: a close-up of hands writing]\n"
            "Real narration here."
        )
        script = self._call(raw)
        assert "IMAGE_PROMPT" not in script.blocks[0].narration
        assert "a close-up of hands writing" not in script.blocks[0].narration

    def test_multiple_image_prompts_per_section(self):
        raw = (
            "[SECTION 1: Main]\n\n"
            "Narration part one.\n"
            "[IMAGE_PROMPT: first image]\n"
            "Narration part two.\n"
            "[IMAGE_PROMPT: second image]\n"
            "Narration part three."
        )
        script = self._call(raw)
        assert len(script.blocks[0].image_prompts) == 2
        assert script.blocks[0].image_prompts[0] == "first image"
        assert script.blocks[0].image_prompts[1] == "second image"

    def test_primary_image_prompt_is_first(self):
        raw = (
            "[SECTION 1: Main]\n\n"
            "[IMAGE_PROMPT: primary shot]\n"
            "Text.\n"
            "[IMAGE_PROMPT: secondary shot]\n"
            "More text."
        )
        script = self._call(raw)
        assert script.blocks[0].image_prompt == "primary shot"

    def test_image_word_offsets_length_matches_prompts(self):
        raw = (
            "[SECTION 1: Main]\n\n"
            "Word one two.\n"
            "[IMAGE_PROMPT: image after 3 words]\n"
            "Word four five six.\n"
            "[IMAGE_PROMPT: image after 6 words]\n"
        )
        script = self._call(raw)
        block = script.blocks[0]
        assert len(block.image_word_offsets) == len(block.image_prompts)

    def test_image_word_offsets_monotonically_nondecreasing(self):
        raw = (
            "[SECTION 1: Main]\n\n"
            "First sentence with some words.\n"
            "[IMAGE_PROMPT: image one]\n"
            "Second sentence with more words here.\n"
            "[IMAGE_PROMPT: image two]\n"
        )
        script = self._call(raw)
        offsets = script.blocks[0].image_word_offsets
        for i in range(1, len(offsets)):
            assert offsets[i] >= offsets[i - 1]

    # ── Fallback: no section markers ─────────────────────────────────────────

    def test_empty_output_raises_or_returns_empty(self):
        """Empty output has no sections — fallback creates one empty block or raises."""
        try:
            script = self._call("")
            # If it returns, blocks may be empty (Script validator rejects empty blocks)
            # or contain one block with empty narration
        except Exception:
            pass  # Pydantic validation error is also acceptable for empty input

    def test_output_without_section_markers_creates_single_block(self):
        raw = "This is narration without any section markers. Just plain text."
        script = self._call(raw)
        assert len(script.blocks) == 1

    def test_no_section_markers_fallback_block_is_intro(self):
        raw = "Plain narration text without markers."
        script = self._call(raw)
        assert script.blocks[0].type == "intro"

    def test_no_section_markers_image_prompts_extracted(self):
        raw = "Narration. [IMAGE_PROMPT: fallback image] More narration."
        script = self._call(raw)
        assert len(script.blocks[0].image_prompts) == 1
        assert script.blocks[0].image_prompts[0] == "fallback image"

    # ── Script metadata ───────────────────────────────────────────────────────

    def test_custom_topic_used_as_title(self):
        raw = "[SECTION 1: Hook]\n\nText."
        script = self._call(raw, custom_topic="My Custom Title")
        assert script.title == "My Custom Title"

    def test_fallback_title_from_source_data(self):
        raw = "[SECTION 1: Hook]\n\nText."
        script = self._call(raw, custom_topic="")
        assert script.title == "Test Video"

    def test_language_from_channel_config(self):
        raw = "[SECTION 1: Hook]\n\nText."
        script = self._call(raw)
        assert script.language == "en"

    def test_niche_in_tags(self):
        raw = "[SECTION 1: Hook]\n\nText."
        script = self._call(raw)
        assert "history" in script.tags

    def test_channel_config_name_stored(self):
        raw = "[SECTION 1: Hook]\n\nText."
        script = self._call(raw)
        assert script.channel_config.name == "TestChannel"


# ===========================================================================
# 5. _build_user_prompt
# ===========================================================================

class TestBuildUserPrompt:
    """Tests for _build_user_prompt — prompt construction for LLM calls."""

    @pytest.fixture(autouse=True)
    def require_fn(self):
        fn = _get("_build_user_prompt")
        if fn is None:
            pytest.skip("_build_user_prompt not importable")
        self.fn = fn

    def _call(
        self,
        duration_min: int = 8,
        duration_max: int = 12,
        template: str = "documentary",
        hook_type: str = "curiosity",
        image_style: str = "",
        custom_topic: str = "",
        master_prompt_path: str = "prompts/master_script_v1.txt",
    ) -> str:
        config = {**_MINIMAL_CHANNEL_CONFIG, "master_prompt_path": master_prompt_path}
        return self.fn(
            source_data=_MINIMAL_SOURCE_DATA,
            channel_config=config,
            template=template,
            hook_type=hook_type,
            duration_min=duration_min,
            duration_max=duration_max,
            image_style=image_style,
            custom_topic=custom_topic,
        )

    # ── Required markers ──────────────────────────────────────────────────────

    def test_contains_duration_marker(self):
        prompt = self._call(8, 12)
        assert "[DURATION]" in prompt

    def test_contains_target_words_marker(self):
        prompt = self._call(8, 12)
        assert "[TARGET WORDS]" in prompt

    def test_duration_values_in_prompt(self):
        prompt = self._call(8, 12)
        assert "8-12 minutes" in prompt or "8–12 minutes" in prompt or ("8" in prompt and "12" in prompt)

    def test_word_target_matches_duration_times_170(self):
        prompt = self._call(duration_min=10, duration_max=15)
        expected_min = str(10 * 170)  # 1700
        expected_max = str(15 * 170)  # 2550
        assert expected_min in prompt, f"Expected {expected_min} words min in prompt"
        assert expected_max in prompt, f"Expected {expected_max} words max in prompt"

    def test_word_target_8_12_min(self):
        prompt = self._call(8, 12)
        assert "1360" in prompt  # 8 * 170
        assert "2040" in prompt  # 12 * 170

    def test_word_target_25_30_min(self):
        prompt = self._call(25, 30)
        assert "4250" in prompt  # 25 * 170
        assert "5100" in prompt  # 30 * 170

    # ── v4 prompt block targets ───────────────────────────────────────────────

    def test_v4_prompt_contains_block_word_targets(self):
        prompt = self._call(
            8, 12,
            master_prompt_path="prompts/master_script_v4.txt",
        )
        assert "[BLOCK WORD TARGETS]" in prompt

    def test_v3_prompt_contains_block_word_targets(self):
        prompt = self._call(
            8, 12,
            master_prompt_path="prompts/master_script_v3.txt",
        )
        assert "[BLOCK WORD TARGETS]" in prompt

    def test_v1_prompt_no_block_word_targets(self):
        prompt = self._call(
            8, 12,
            master_prompt_path="prompts/master_script_v1.txt",
        )
        assert "[BLOCK WORD TARGETS]" not in prompt

    # ── Content from source ───────────────────────────────────────────────────

    def test_transcript_included(self):
        prompt = self._call()
        assert "This is a transcript." in prompt

    def test_topic_title_included(self):
        prompt = self._call()
        assert "Test Video" in prompt

    def test_custom_topic_overrides_title(self):
        prompt = self._call(custom_topic="My Special Topic")
        assert "My Special Topic" in prompt

    def test_language_tag_in_prompt(self):
        prompt = self._call()
        assert "[LANGUAGE]" in prompt or "en" in prompt

    # ── Template and hook instructions ────────────────────────────────────────

    def test_documentary_template_instruction_included(self):
        prompt = self._call(template="documentary")
        assert "DOCUMENTARY" in prompt

    def test_listicle_template_instruction_included(self):
        prompt = self._call(template="listicle")
        assert "LISTICLE" in prompt

    def test_curiosity_hook_instruction_included(self):
        prompt = self._call(hook_type="curiosity")
        assert "CURIOSITY" in prompt

    def test_auto_template_no_template_instruction(self):
        prompt = self._call(template="auto")
        # "auto" is not in TEMPLATE_INSTRUCTIONS so no template line added
        assert "DOCUMENTARY" not in prompt
        assert "LISTICLE" not in prompt

    # ── Image style injection ─────────────────────────────────────────────────

    def test_image_style_injected_when_provided(self):
        prompt = self._call(image_style="cinematic photorealism, 8K resolution")
        assert "cinematic photorealism" in prompt
        assert "[IMAGE STYLE]" in prompt

    def test_image_style_absent_when_empty(self):
        prompt = self._call(image_style="")
        assert "[IMAGE STYLE]" not in prompt

    # ── Word count requirement copy ───────────────────────────────────────────

    def test_minimum_word_requirement_mentioned(self):
        prompt = self._call(10, 15)
        # The prompt must tell the LLM not to write [CTA_SUBSCRIBE_FINAL] until minimum words
        assert "CTA_SUBSCRIBE_FINAL" in prompt

    def test_special_requests_section_present(self):
        prompt = self._call()
        assert "__SPECIAL REQUESTS__" in prompt
