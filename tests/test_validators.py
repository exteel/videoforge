"""Unit tests for script and image validators.

Covers _structural_checks() from 01b_script_validator — pure function,
zero API calls. All tests run in < 1 second total.
"""
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ─── Import helper ────────────────────────────────────────────────────────────
# Module file is named "01b_script_validator.py" — the leading digit and dot in
# the filename make it an invalid Python identifier, so we cannot use a normal
# `import` statement. importlib.util handles this cleanly.

def _load_module(filename: str, module_name: str) -> ModuleType:
    """Load a module from an arbitrary filename that is not a valid Python identifier.

    The module is registered in sys.modules under `module_name` before execution
    so that @dataclass forward-reference resolution works correctly on Python 3.13+.
    """
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "modules" / filename
    )
    assert spec is not None and spec.loader is not None, (
        f"Could not find module file: modules/{filename}"
    )
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module — required for @dataclass on Python 3.13
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_sv = _load_module("01b_script_validator.py", "script_validator_01b")
_structural_checks: callable = _sv._structural_checks
ScriptIssue: type = _sv.ScriptIssue


# ─── Test fixture ─────────────────────────────────────────────────────────────

def _make_blocks(count: int = 5, words_per: int = 200, has_cta: bool = True) -> list[dict]:
    """Generate well-formed test blocks with configurable properties.

    All blocks end with a sentence-terminating period and contain no parser
    artefacts, so a call with the defaults returns zero issues from a healthy
    script.
    """
    blocks = []
    for i in range(count):
        blocks.append({
            "id": f"block_{i + 1:03d}",
            "type": "intro" if i == 0 else "section",
            "narration": " ".join(["word"] * (words_per - 1)) + " end.",
            "image_prompt": f"a detailed cinematic scene of topic number {i + 1}",
            "timestamp_label": f"Section {i + 1}",
        })
    if has_cta:
        blocks.append({
            "id": "block_cta",
            "type": "cta",
            "narration": " ".join(["word"] * 49) + " end.",
            "image_prompt": "closing cinematic image of the channel logo",
            "timestamp_label": "Subscribe",
        })
    return blocks


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _issue_types(issues: list) -> list[str]:
    return [i.type for i in issues]


def _critical(issues: list) -> list:
    return [i for i in issues if i.severity == "critical"]


def _warnings(issues: list) -> list:
    return [i for i in issues if i.severity == "warning"]


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestValidScript:
    """A properly constructed script should produce no critical issues."""

    def test_valid_script_no_critical_issues(self):
        blocks = _make_blocks(count=5, words_per=200, has_cta=True)
        issues = _structural_checks(blocks)
        criticals = _critical(issues)
        assert criticals == [], (
            f"Expected no critical issues for a valid script, got: "
            f"{[(i.type, i.reason) for i in criticals]}"
        )

    def test_valid_script_no_no_blocks_issue(self):
        blocks = _make_blocks()
        issues = _structural_checks(blocks)
        assert "no_blocks" not in _issue_types(issues)

    def test_valid_script_no_missing_cta(self):
        blocks = _make_blocks(has_cta=True)
        issues = _structural_checks(blocks)
        assert "missing_cta" not in _issue_types(issues)


class TestNoBlocks:
    """Empty block list must immediately return a single critical 'no_blocks' issue."""

    def test_empty_list_returns_critical_no_blocks(self):
        issues = _structural_checks([])
        assert len(issues) == 1
        assert issues[0].type == "no_blocks"
        assert issues[0].severity == "critical"

    def test_no_blocks_stops_processing(self):
        """The function returns early — no other issue types mixed in."""
        issues = _structural_checks([])
        types = _issue_types(issues)
        assert types == ["no_blocks"]


class TestMissingCTA:
    """Scripts without a cta or outro block must raise critical 'missing_cta'."""

    def test_no_cta_block_raises_critical(self):
        blocks = _make_blocks(has_cta=False)
        issues = _structural_checks(blocks)
        cta_issues = [i for i in issues if i.type == "missing_cta"]
        assert len(cta_issues) == 1
        assert cta_issues[0].severity == "critical"

    def test_outro_type_satisfies_cta_requirement(self):
        blocks = _make_blocks(has_cta=False)
        blocks.append({
            "id": "block_outro",
            "type": "outro",
            "narration": "Thank you for watching this video.",
            "image_prompt": "outro cinematic shot",
            "timestamp_label": "Outro",
        })
        issues = _structural_checks(blocks)
        assert "missing_cta" not in _issue_types(issues)


class TestScriptLength:
    """Length threshold tests — verifies the 90 % too_short and 125 % too_long formulas."""

    def test_too_short_with_duration_min(self):
        """100 words < 90 % of (30 min × 140 wpm) = 3 780 words → critical too_short."""
        blocks = _make_blocks(count=1, words_per=0, has_cta=False)
        # Build one block with exactly 100 words of narration (ends with period)
        blocks[0]["narration"] = " ".join(["word"] * 99) + " end."
        blocks[0]["type"] = "intro"
        # Add a CTA block so we don't conflate missing_cta with too_short
        blocks.append({
            "id": "block_cta",
            "type": "cta",
            "narration": "Subscribe now.",
            "image_prompt": "cinematic closing",
            "timestamp_label": "Subscribe",
        })
        issues = _structural_checks(blocks, duration_min=30)
        too_short_issues = [i for i in issues if i.type == "too_short"]
        assert len(too_short_issues) == 1, (
            f"Expected exactly one too_short issue; got: {_issue_types(issues)}"
        )
        assert too_short_issues[0].severity == "critical"

    def test_too_short_threshold_is_90_percent(self):
        """Verify the threshold is exactly floor(duration_min * 140 * 0.90).

        duration_min=10 → threshold = floor(10 * 140 * 0.90) = 1 260.

        total_words is the sum across ALL blocks including the CTA ("Subscribe." = 1 word).
        So the intro narration must have (threshold - 1) words for the total to hit threshold,
        and (threshold - 2) words for the total to be one below threshold.
        """
        duration_min = 10
        threshold = int(duration_min * 140 * 0.90)  # 1 260
        # CTA contributes 1 word ("Subscribe.") — account for it when building intro narration
        CTA_WORD_COUNT = 1

        def _script_with_total(total_words: int) -> list[dict]:
            intro_words = total_words - CTA_WORD_COUNT
            # Build a multi-sentence narration to avoid triggering tts_violation (long sentence)
            # and sparse_images (by keeping block under 200 words so sparse check is skipped).
            # We split into 30-word sentences to stay under TTS_MAX_SENTENCE_WORDS=35.
            sentences = []
            remaining = intro_words
            while remaining > 0:
                chunk = min(remaining, 30)
                sentences.append(" ".join(["word"] * (chunk - 1)) + " end.")
                remaining -= chunk
            narration = " ".join(sentences)
            intro = {
                "id": "block_001",
                "type": "intro",
                "narration": narration,
                "image_prompt": "cinematic landscape of the topic at sunrise",
                "timestamp_label": "Intro",
            }
            cta = {
                "id": "block_cta",
                "type": "cta",
                "narration": "Subscribe.",
                "image_prompt": "cinematic closing",
                "timestamp_label": "Subscribe",
            }
            return [intro, cta]

        # Exactly at threshold — must NOT produce too_short
        issues_at = _structural_checks(_script_with_total(threshold), duration_min=duration_min)
        assert "too_short" not in _issue_types(issues_at), (
            f"Script at exactly {threshold} total words incorrectly flagged as too_short. "
            f"Issues: {_issue_types(issues_at)}"
        )

        # One word below threshold — MUST produce too_short
        issues_below = _structural_checks(_script_with_total(threshold - 1), duration_min=duration_min)
        assert "too_short" in _issue_types(issues_below), (
            f"Script at {threshold - 1} total words (below threshold {threshold}) not flagged. "
            f"Issues: {_issue_types(issues_below)}"
        )

    def test_too_long_with_duration_max(self):
        """20 000 words > 125 % of (8 min × 150 wpm) = 1 500 words → warning too_long."""
        # Build enough blocks to reach 20 000 total narration words
        big_narration = " ".join(["word"] * 3999) + " end."  # 4 000 words per block
        blocks = []
        for i in range(5):
            blocks.append({
                "id": f"block_{i + 1:03d}",
                "type": "section",
                "narration": big_narration,
                "image_prompt": f"cinematic scene for section {i + 1}",
                "timestamp_label": f"Section {i + 1}",
            })
        blocks.append({
            "id": "block_cta",
            "type": "cta",
            "narration": "Subscribe.",
            "image_prompt": "cinematic closing",
            "timestamp_label": "Subscribe",
        })
        issues = _structural_checks(blocks, duration_max=8)
        too_long_issues = [i for i in issues if i.type == "too_long"]
        assert len(too_long_issues) == 1
        assert too_long_issues[0].severity == "warning"

    def test_too_long_without_duration_max_uses_default(self):
        """Without duration_max the fallback TOO_LONG_WORDS = 2 500 is used."""
        # 2 501 words → exceeds static fallback
        narration = " ".join(["word"] * 2500) + " end."
        blocks = [
            {
                "id": "block_001",
                "type": "intro",
                "narration": narration,
                "image_prompt": "cinematic intro shot",
                "timestamp_label": "Intro",
            },
            {
                "id": "block_cta",
                "type": "cta",
                "narration": "Subscribe.",
                "image_prompt": "closing",
                "timestamp_label": "Subscribe",
            },
        ]
        issues = _structural_checks(blocks)
        assert "too_long" in _issue_types(issues)


class TestEmptyNarration:
    """A non-CTA block with empty narration must produce critical 'empty_narration'."""

    def test_empty_narration_is_critical(self):
        blocks = _make_blocks()
        # Blank out a mid-script section block
        blocks[2]["narration"] = ""
        issues = _structural_checks(blocks)
        empty = [i for i in issues if i.type == "empty_narration"]
        assert len(empty) >= 1
        assert all(i.severity == "critical" for i in empty)
        assert empty[0].block_id == blocks[2]["id"]

    def test_empty_narration_on_cta_is_not_flagged(self):
        """CTA blocks with no narration are a valid — validator should not flag them."""
        blocks = _make_blocks()
        # Empty CTA narration
        cta = next(b for b in blocks if b["type"] == "cta")
        cta["narration"] = ""
        issues = _structural_checks(blocks)
        empty = [i for i in issues if i.type == "empty_narration"]
        assert all(i.block_id != cta["id"] for i in empty), (
            "CTA block with empty narration should not generate empty_narration issue"
        )


class TestMissingField:
    """Blocks missing 'id' or 'type' must produce critical 'missing_field'."""

    def test_missing_id_is_critical(self):
        blocks = _make_blocks()
        del blocks[1]["id"]
        issues = _structural_checks(blocks)
        missing = [i for i in issues if i.type == "missing_field"]
        assert len(missing) >= 1
        assert any(i.severity == "critical" for i in missing)

    def test_missing_type_is_critical(self):
        blocks = _make_blocks()
        del blocks[2]["type"]
        issues = _structural_checks(blocks)
        missing = [i for i in issues if i.type == "missing_field"]
        assert any(i.severity == "critical" for i in missing)

    def test_multiple_missing_fields_each_flagged(self):
        """Every block missing a required field gets its own issue entry."""
        blocks = _make_blocks(count=3, has_cta=True)
        del blocks[0]["id"]
        del blocks[1]["id"]
        issues = _structural_checks(blocks)
        missing = [i for i in issues if i.type == "missing_field"]
        assert len(missing) >= 2


class TestDuplicateSectionTitles:
    """Identical timestamp_label values on non-trivial sections → warning 'duplicate_section'."""

    def test_duplicate_label_raises_warning(self):
        blocks = _make_blocks(count=4, has_cta=True)
        # Force two blocks to share a non-trivial label
        blocks[1]["timestamp_label"] = "The Causes of Failure"
        blocks[2]["timestamp_label"] = "The Causes of Failure"
        issues = _structural_checks(blocks)
        dups = [i for i in issues if i.type == "duplicate_section"]
        assert len(dups) >= 1
        assert dups[0].severity == "warning"

    def test_article_normalisation_catches_near_duplicates(self):
        """'The Rise of AI' and 'Rise of AI' should both be normalised to 'rise of ai'."""
        blocks = _make_blocks(count=3, has_cta=True)
        blocks[1]["timestamp_label"] = "The Rise of AI"
        blocks[2]["timestamp_label"] = "Rise of AI"
        issues = _structural_checks(blocks)
        dups = [i for i in issues if i.type == "duplicate_section"]
        assert len(dups) >= 1

    def test_generic_labels_are_skipped(self):
        """Reserved labels like 'hook', 'subscribe', 'intro', 'outro' must not trigger duplicate_section."""
        blocks = _make_blocks(count=3, has_cta=True)
        for b in blocks:
            b["timestamp_label"] = "intro"
        issues = _structural_checks(blocks)
        assert "duplicate_section" not in _issue_types(issues)


class TestCutOff:
    """Cut-off detection: narration ending without sentence-terminating punctuation."""

    def test_mid_sentence_end_triggers_cut_off(self):
        """A narration ending without '.' '!' '?' is a cut-off signal."""
        blocks = _make_blocks(count=5, has_cta=True)
        # Block at index 0 is far from end → should be "warning"
        blocks[0]["narration"] = "This sentence ends without punctuation because the LLM stopped"
        issues = _structural_checks(blocks)
        co = [i for i in issues if i.type == "cut_off" and i.block_id == blocks[0]["id"]]
        assert len(co) >= 1

    def test_cut_off_near_end_is_critical(self):
        """The last two positions (penultimate + CTA) produce severity 'critical'."""
        blocks = _make_blocks(count=4, has_cta=True)
        # Penultimate block (index len-2 = 4)
        penultimate = blocks[-2]
        penultimate["narration"] = "Something happened and then"
        issues = _structural_checks(blocks)
        co = [i for i in issues if i.type == "cut_off" and i.block_id == penultimate["id"]]
        assert len(co) >= 1
        assert co[0].severity == "critical"

    def test_cut_off_mid_script_is_warning(self):
        """A cut-off in an early block (not near end) must be severity 'warning'."""
        blocks = _make_blocks(count=6, has_cta=True)
        # Block at index 0 is 6+ positions from the end
        blocks[0]["narration"] = "This narration stops in the middle of a thought and"
        issues = _structural_checks(blocks)
        co = [
            i for i in issues
            if i.type == "cut_off" and i.block_id == blocks[0]["id"]
        ]
        assert len(co) >= 1
        assert co[0].severity == "warning"

    def test_connector_word_at_end_triggers_cut_off(self):
        """Narration ending with a connector word ('and', 'the', 'with', …) is a cut-off."""
        blocks = _make_blocks(count=5, has_cta=True)
        blocks[0]["narration"] = "It all started with"
        issues = _structural_checks(blocks)
        co = [i for i in issues if i.type == "cut_off" and i.block_id == blocks[0]["id"]]
        assert len(co) >= 1

    def test_proper_ending_no_cut_off(self):
        """Narrations that end cleanly with a period must not trigger cut_off."""
        blocks = _make_blocks(count=3, has_cta=True)
        issues = _structural_checks(blocks)
        co = [i for i in issues if i.type == "cut_off"]
        assert co == [], f"Unexpected cut_off issues: {[(i.block_id, i.reason) for i in co]}"


class TestPerBlockShortNarration:
    """Per-block narration that is too short for the target duration → warning 'short_block'."""

    def test_very_short_block_with_long_duration_min(self):
        """5-word block for a 30-min video is far below eff_block_short=max(15, 60)=60."""
        blocks = _make_blocks(count=3, has_cta=True)
        blocks[1]["narration"] = "word word word word end."  # 5 words
        issues = _structural_checks(blocks, duration_min=30)
        short = [i for i in issues if i.type == "short_block" and i.block_id == blocks[1]["id"]]
        assert len(short) >= 1
        assert short[0].severity == "warning"

    def test_short_block_cta_exempt(self):
        """CTA blocks are exempt from the short_block check (they have a different purpose)."""
        blocks = _make_blocks(count=3, has_cta=True)
        cta = next(b for b in blocks if b["type"] == "cta")
        cta["narration"] = "Subscribe."  # 1 word
        issues = _structural_checks(blocks, duration_min=30)
        short_on_cta = [i for i in issues if i.type == "short_block" and i.block_id == cta["id"]]
        assert short_on_cta == []

    def test_dynamic_threshold_scales_with_duration(self):
        """eff_block_short = max(15, duration_min * 2).

        duration_min=5  → max(15, 10) = 15
        duration_min=20 → max(15, 40) = 40

        A 20-word block that passes duration_min=5 should fail duration_min=20.
        """
        narration_20w = " ".join(["word"] * 19) + " end."  # 20 words
        blocks = [
            {
                "id": "block_001",
                "type": "intro",
                "narration": narration_20w,
                "image_prompt": "cinematic intro scene",
                "timestamp_label": "Intro",
            },
            {
                "id": "block_cta",
                "type": "cta",
                "narration": "Subscribe.",
                "image_prompt": "closing",
                "timestamp_label": "Subscribe",
            },
        ]
        # With duration_min=5: threshold = max(15, 10) = 15 → 20 words OK
        issues_5 = _structural_checks(blocks, duration_min=5)
        short_5 = [i for i in issues_5 if i.type == "short_block"]
        assert short_5 == [], "20-word block should not be flagged for duration_min=5"

        # With duration_min=20: threshold = max(15, 40) = 40 → 20 words flagged
        issues_20 = _structural_checks(blocks, duration_min=20)
        short_20 = [i for i in issues_20 if i.type == "short_block"]
        assert len(short_20) >= 1, "20-word block should be flagged for duration_min=20"


class TestReturnTypes:
    """Sanity-checks that the return value is always a list of ScriptIssue instances."""

    def test_returns_list(self):
        issues = _structural_checks(_make_blocks())
        assert isinstance(issues, list)

    def test_each_element_is_script_issue(self):
        issues = _structural_checks(_make_blocks())
        for issue in issues:
            assert isinstance(issue, ScriptIssue)

    def test_script_issue_has_required_attrs(self):
        issues = _structural_checks([])
        assert len(issues) == 1
        issue = issues[0]
        assert hasattr(issue, "type")
        assert hasattr(issue, "severity")
        assert hasattr(issue, "block_id")
        assert hasattr(issue, "reason")
        assert hasattr(issue, "fixed")
