"""
Unit tests for utils/cost_tracker.py.

Covers: MODEL_PRICING constants, ModelPrice dataclass, CostTracker methods
(add_llm, add_images, add_voice, summary_table, total), and the
estimate_cost() offline estimation function.

No real API calls — all channel configs are created in-memory via tmp_path.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.cost_tracker import (
    MODEL_PRICING,
    VOICEAPI_COST_PER_CHAR,
    VOIDAI_TTS_COST_PER_CHAR,
    VOIDAI_IMAGE_COST,
    WAVESPEED_IMAGE_COST,
    CostEntry,
    CostTracker,
    _DEFAULT_MODEL_PRICE,
    estimate_cost,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def channel_cfg(tmp_path) -> Path:
    """Write a minimal channel config JSON and return its path."""
    cfg = {
        "channel_name": "Test",
        "llm": {
            "default_preset": "max",
            "presets": {
                "max":      {"script": "claude-opus-4-6",           "metadata": "gpt-4.1-mini", "thumbnail": "gpt-4.1"},
                "high":     {"script": "claude-sonnet-4-5-20250929","metadata": "gpt-4.1-mini", "thumbnail": "gpt-4.1"},
                "balanced": {"script": "gpt-5.2",                   "metadata": "gpt-4.1-nano", "thumbnail": "gemini-2.5-flash"},
                "bulk":     {"script": "deepseek-v3.1",             "metadata": "gpt-4.1-nano", "thumbnail": "gemini-2.5-flash"},
                "test":     {"script": "mistral-small-latest",      "metadata": "gemma-3n-e4b-it", "thumbnail": "gemini-2.5-flash"},
            },
        },
    }
    p = tmp_path / "channel.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


@pytest.fixture
def tracker() -> CostTracker:
    return CostTracker()


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL_PRICING constants
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelPricingConstants:

    def test_all_models_have_nonnegative_prices(self):
        for model, price in MODEL_PRICING.items():
            assert price.input_per_1m >= 0, f"{model}: negative input price"
            assert price.output_per_1m >= 0, f"{model}: negative output price"

    def test_opus_models_are_zero_cost(self):
        """Opus models are covered by subscription — marginal cost is zero."""
        assert MODEL_PRICING["claude-opus-4-6"].input_per_1m == 0.0
        assert MODEL_PRICING["claude-opus-4-6"].output_per_1m == 0.0
        assert MODEL_PRICING["claude-opus-4-5"].input_per_1m == 0.0

    def test_sonnet_pricing(self):
        price = MODEL_PRICING["claude-sonnet-4-6"]
        assert price.input_per_1m == 3.0
        assert price.output_per_1m == 15.0

    def test_gpt41_mini_cheaper_than_gpt41(self):
        mini = MODEL_PRICING["gpt-4.1-mini"]
        full = MODEL_PRICING["gpt-4.1"]
        assert mini.input_per_1m < full.input_per_1m
        assert mini.output_per_1m < full.output_per_1m

    def test_default_price_is_gpt41_mini_rate(self):
        assert _DEFAULT_MODEL_PRICE.input_per_1m == 0.400
        assert _DEFAULT_MODEL_PRICE.output_per_1m == 1.600

    def test_deepseek_cheaper_than_gpt4(self):
        ds = MODEL_PRICING["deepseek-v3.1"]
        gpt = MODEL_PRICING["gpt-4.1"]
        assert ds.input_per_1m < gpt.input_per_1m

    def test_model_price_frozen(self):
        """ModelPrice is a frozen dataclass — cannot mutate."""
        price = MODEL_PRICING["gpt-4.1-nano"]
        with pytest.raises((AttributeError, TypeError)):
            price.input_per_1m = 999.0  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════════
# CostTracker._model_price
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelPriceLookup:

    def test_exact_match(self, tracker):
        price = tracker._model_price("gpt-4.1-nano")
        assert price == MODEL_PRICING["gpt-4.1-nano"]

    def test_unknown_model_returns_default(self, tracker):
        price = tracker._model_price("unknown-model-xyz-999")
        assert price == _DEFAULT_MODEL_PRICE

    def test_partial_prefix_match(self, tracker):
        """A model name that starts with a known key should resolve to that key's price."""
        price = tracker._model_price("claude-opus-4-6-special-variant")
        assert price == MODEL_PRICING["claude-opus-4-6"]

    def test_opus_zero_cost_lookup(self, tracker):
        price = tracker._model_price("claude-opus-4-6")
        assert price.input_per_1m == 0.0
        assert price.output_per_1m == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# CostTracker.add_llm
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddLLM:

    def test_opus_has_zero_cost(self, tracker):
        cost = tracker.add_llm("Script", "claude-opus-4-6", input_tokens=2500, output_tokens=3000)
        assert cost == 0.0
        assert tracker.total == 0.0

    def test_sonnet_cost_calculation(self, tracker):
        # claude-sonnet-4-6: $3/M input, $15/M output
        cost = tracker.add_llm("Script", "claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
        assert abs(cost - 18.0) < 1e-9  # 3 + 15

    def test_cost_accumulates(self, tracker):
        tracker.add_llm("Step1", "gpt-4.1", input_tokens=10_000, output_tokens=5_000)
        tracker.add_llm("Step2", "gpt-4.1", input_tokens=10_000, output_tokens=5_000)
        # gpt-4.1: $2/M input, $8/M output
        expected_per_call = (10_000 * 2.0 + 5_000 * 8.0) / 1_000_000
        assert abs(tracker.total - expected_per_call * 2) < 1e-9

    def test_returns_cost_value(self, tracker):
        cost = tracker.add_llm("Meta", "gpt-4.1-nano", input_tokens=800, output_tokens=400)
        assert isinstance(cost, float)
        assert cost >= 0.0

    def test_entry_stored_correctly(self, tracker):
        tracker.add_llm("MyModule", "gpt-4.1-mini", input_tokens=100, output_tokens=50)
        assert len(tracker.entries) == 1
        e = tracker.entries[0]
        assert e.module == "MyModule"
        assert e.model == "gpt-4.1-mini"
        assert e.input_tokens == 100
        assert e.output_tokens == 50

    def test_zero_tokens_produces_zero_cost(self, tracker):
        cost = tracker.add_llm("NullStep", "gpt-4.1", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_unknown_model_uses_default_pricing(self, tracker):
        cost = tracker.add_llm("Step", "my-custom-model", input_tokens=1_000_000, output_tokens=0)
        expected = 1_000_000 * 0.400 / 1_000_000
        assert abs(cost - expected) < 1e-9


# ═══════════════════════════════════════════════════════════════════════════════
# CostTracker.add_images
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddImages:

    def test_wavespeed_cost(self, tracker):
        cost = tracker.add_images("Images", "wavespeed", count=10)
        assert abs(cost - WAVESPEED_IMAGE_COST * 10) < 1e-9

    def test_voidai_cost(self, tracker):
        cost = tracker.add_images("Thumbnail", "voidai", count=2)
        assert abs(cost - VOIDAI_IMAGE_COST * 2) < 1e-9

    def test_provider_case_insensitive(self, tracker):
        cost_lower = tracker.add_images("Img", "wavespeed", count=1)
        tracker2 = CostTracker()
        cost_upper = tracker2.add_images("Img", "WaveSpeed", count=1)
        assert abs(cost_lower - cost_upper) < 1e-9

    def test_wavespeed_label_in_entry(self, tracker):
        tracker.add_images("Images", "wavespeed", count=5)
        e = tracker.entries[0]
        assert "WaveSpeed" in e.model or "wavespeed" in e.model.lower()
        assert e.unit_label == "images"
        assert e.units == 5.0

    def test_zero_count_produces_zero_cost(self, tracker):
        cost = tracker.add_images("Empty", "wavespeed", count=0)
        assert cost == 0.0

    def test_voidai_model_label(self, tracker):
        tracker.add_images("Thumb", "voidai", count=1)
        e = tracker.entries[0]
        assert "gpt-image" in e.model.lower() or "voidai" in e.model.lower() or "imagen" in e.model.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# CostTracker.add_voice
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddVoice:

    def test_voiceapi_cost(self, tracker):
        chars = 6500
        cost = tracker.add_voice("Voice", chars=chars)
        assert abs(cost - chars * VOICEAPI_COST_PER_CHAR) < 1e-12

    def test_fallback_tts_cost(self, tracker):
        chars = 6500
        cost = tracker.add_voice("Voice", chars=chars, fallback=True)
        assert abs(cost - chars * VOIDAI_TTS_COST_PER_CHAR) < 1e-12

    def test_fallback_more_expensive_than_voiceapi(self, tracker):
        """VoidAI TTS is more expensive per character than VoiceAPI."""
        assert VOIDAI_TTS_COST_PER_CHAR > VOICEAPI_COST_PER_CHAR

    def test_entry_unit_label(self, tracker):
        tracker.add_voice("Voice", chars=1000)
        e = tracker.entries[0]
        assert e.unit_label == "chars"
        assert e.units == 1000.0

    def test_fallback_flag_changes_model_label(self, tracker):
        tracker.add_voice("Voice", chars=100, fallback=False)
        tracker.add_voice("Voice", chars=100, fallback=True)
        labels = [e.model for e in tracker.entries]
        # Fallback label should differ from primary
        assert labels[0] != labels[1]

    def test_zero_chars_produces_zero_cost(self, tracker):
        cost = tracker.add_voice("Silence", chars=0)
        assert cost == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# CostTracker.total
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrackerTotal:

    def test_empty_tracker_has_zero_total(self, tracker):
        assert tracker.total == 0.0

    def test_total_sums_all_entries(self, tracker):
        tracker.add_llm("S", "claude-sonnet-4-6", input_tokens=100_000, output_tokens=10_000)
        tracker.add_images("I", "wavespeed", count=5)
        tracker.add_voice("V", chars=5000)
        assert tracker.total == sum(e.cost for e in tracker.entries)

    def test_total_after_multiple_llm_calls(self, tracker):
        tracker.add_llm("A", "gpt-4.1-nano", input_tokens=500, output_tokens=200)
        tracker.add_llm("B", "gpt-4.1-nano", input_tokens=500, output_tokens=200)
        expected = sum(e.cost for e in tracker.entries)
        assert abs(tracker.total - expected) < 1e-12


# ═══════════════════════════════════════════════════════════════════════════════
# CostTracker.summary_table
# ═══════════════════════════════════════════════════════════════════════════════

class TestSummaryTable:

    def test_empty_tracker_returns_no_costs_message(self, tracker):
        result = tracker.summary_table()
        assert "no costs" in result.lower()

    def test_contains_header_columns(self, tracker):
        tracker.add_llm("Script", "gpt-4.1-nano", input_tokens=100, output_tokens=50)
        result = tracker.summary_table()
        assert "Module" in result
        assert "Model" in result
        assert "Cost" in result

    def test_contains_module_name(self, tracker):
        tracker.add_llm("MyModule", "gpt-4.1-nano", input_tokens=100, output_tokens=50)
        result = tracker.summary_table()
        assert "MyModule" in result

    def test_contains_total_row(self, tracker):
        tracker.add_llm("Script", "gpt-4.1-nano", input_tokens=100, output_tokens=50)
        result = tracker.summary_table()
        assert "TOTAL" in result

    def test_title_appears_when_provided(self, tracker):
        tracker.add_llm("S", "gpt-4.1-nano", input_tokens=1, output_tokens=1)
        result = tracker.summary_table(title="My Video Title")
        assert "My Video Title" in result

    def test_no_title_when_not_provided(self, tracker):
        tracker.add_llm("S", "gpt-4.1-nano", input_tokens=1, output_tokens=1)
        result = tracker.summary_table()
        assert "Cost estimate:" not in result

    def test_image_entry_shows_units(self, tracker):
        tracker.add_images("Images", "wavespeed", count=7)
        result = tracker.summary_table()
        assert "images" in result.lower()
        assert "7" in result

    def test_voice_entry_shows_chars(self, tracker):
        tracker.add_voice("Voice", chars=8000)
        result = tracker.summary_table()
        assert "chars" in result.lower()

    def test_cost_formatted_with_dollar_sign(self, tracker):
        tracker.add_llm("Step", "gpt-4.1-mini", input_tokens=10000, output_tokens=5000)
        result = tracker.summary_table()
        assert "$" in result

    def test_multiple_entries_all_present(self, tracker):
        tracker.add_llm("Script",    "claude-opus-4-6", input_tokens=2500, output_tokens=3000)
        tracker.add_images("Images", "wavespeed",       count=9)
        tracker.add_voice("Voice",   chars=7200)
        result = tracker.summary_table()
        assert "Script" in result
        assert "Images" in result
        assert "Voice"  in result


# ═══════════════════════════════════════════════════════════════════════════════
# estimate_cost()
# ═══════════════════════════════════════════════════════════════════════════════

class TestEstimateCost:

    def test_returns_cost_tracker(self, channel_cfg):
        result = estimate_cost(None, channel_cfg)
        assert isinstance(result, CostTracker)

    def test_has_entries(self, channel_cfg):
        result = estimate_cost(None, channel_cfg)
        assert len(result.entries) > 0

    def test_total_is_positive(self, channel_cfg):
        result = estimate_cost(None, channel_cfg, quality_preset="balanced")
        assert result.total >= 0.0

    def test_with_n_blocks_override(self, channel_cfg):
        result = estimate_cost(None, channel_cfg, n_blocks=5)
        # 5 blocks → 4 image blocks (5 - 1 CTA), plus standard steps
        image_entries = [e for e in result.entries if e.unit_label == "images"]
        assert len(image_entries) >= 1

    def test_with_n_chars_override(self, channel_cfg):
        r1 = estimate_cost(None, channel_cfg, n_chars=1000)
        r2 = estimate_cost(None, channel_cfg, n_chars=10_000)
        # More chars → higher voice cost → higher total
        voice1 = sum(e.cost for e in r1.entries if e.unit_label == "chars")
        voice2 = sum(e.cost for e in r2.entries if e.unit_label == "chars")
        assert voice2 > voice1

    def test_multilang_adds_more_voice_entries(self, channel_cfg):
        r1 = estimate_cost(None, channel_cfg, n_langs=1)
        r2 = estimate_cost(None, channel_cfg, n_langs=3)
        voice1 = [e for e in r1.entries if e.unit_label == "chars"]
        voice2 = [e for e in r2.entries if e.unit_label == "chars"]
        assert len(voice2) == len(voice1) * 3

    def test_no_validate_images_skips_validation_entry(self, channel_cfg):
        result_with    = estimate_cost(None, channel_cfg, validate_images=True,  n_blocks=5)
        result_without = estimate_cost(None, channel_cfg, validate_images=False, n_blocks=5)
        # With validate: should have an "Image validation" LLM entry
        has_validate = any("validation" in e.module.lower() and e.input_tokens > 0
                           for e in result_with.entries)
        skip_validate = not any("image validation" in e.module.lower()
                                for e in result_without.entries)
        assert has_validate
        assert skip_validate

    def test_thumb_attempts_zero_produces_zero_cost_images(self, channel_cfg):
        """avg_thumb_attempts=0 adds an image entry with 0 units and 0 cost (not skipped entirely)."""
        result = estimate_cost(None, channel_cfg, avg_thumb_attempts=0)
        thumb_images = [e for e in result.entries
                        if "thumb" in e.module.lower() and e.unit_label == "images"]
        # Entry is created but cost is zero and units is 0
        assert all(e.cost == 0.0 for e in thumb_images)
        assert all(e.units == 0.0 for e in thumb_images)

    def test_max_preset_uses_opus_for_script(self, channel_cfg):
        result = estimate_cost(None, channel_cfg, quality_preset="max")
        script_entries = [e for e in result.entries if e.module == "Script"]
        assert len(script_entries) >= 1
        assert script_entries[0].model == "claude-opus-4-6"

    def test_bulk_preset_uses_deepseek(self, channel_cfg):
        result = estimate_cost(None, channel_cfg, quality_preset="bulk")
        script_entries = [e for e in result.entries if e.module == "Script"]
        assert script_entries[0].model == "deepseek-v3.1"

    def test_with_script_json_reads_blocks(self, tmp_path, channel_cfg):
        """When a real script.json is provided, block and char counts are derived from it."""
        script = {
            "title": "Test Video",
            "blocks": [
                {"narration": "Hello world " * 10, "image_prompt": "A sunrise"},
                {"narration": "Goodbye world " * 8, "image_prompt": "A sunset"},
                {"narration": "End.",               "image_prompt": ""},
            ],
        }
        script_path = tmp_path / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")

        result = estimate_cost(script_path, channel_cfg)

        # 2 blocks have non-empty image_prompt
        image_entries = [e for e in result.entries
                         if e.unit_label == "images" and e.module == "Images"]
        assert len(image_entries) == 1
        assert image_entries[0].units == 2.0

    def test_nonexistent_script_path_uses_defaults(self, tmp_path, channel_cfg):
        """A script_path that doesn't exist falls back to n_blocks/n_chars defaults."""
        missing = tmp_path / "nonexistent.json"
        result = estimate_cost(missing, channel_cfg, n_blocks=5)
        assert isinstance(result, CostTracker)
        assert len(result.entries) > 0

    def test_metadata_entry_present(self, channel_cfg):
        result = estimate_cost(None, channel_cfg)
        meta_entries = [e for e in result.entries if e.module == "Metadata"]
        assert len(meta_entries) == 1

    def test_hook_validation_entry_present(self, channel_cfg):
        result = estimate_cost(None, channel_cfg)
        hook_entries = [e for e in result.entries if "hook" in e.module.lower()]
        assert len(hook_entries) >= 1
