"""
VideoForge -- Cost Tracker (Task #17).

Tracks API costs per model per module and provides cost estimation for dry-run.

Features:
- Per-model token pricing for all VoidAI models
- Per-unit pricing for WaveSpeed images and VoiceAPI TTS
- estimate_cost(script_json, quality_preset) -- fast offline estimation
- CostTracker class for runtime tracking across pipeline steps
- CLI: outputs table "module | model | tokens | price"

CLI:
    python utils/cost_tracker.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        --quality max

    # Estimate only (no script needed):
    python utils/cost_tracker.py \\
        --channel config/channels/history.json \\
        --quality max \\
        --blocks 10 --chars 8000
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from modules.common import get_llm_preset, load_channel_config, load_env, setup_logging

log = setup_logging("cost_tracker")

# ─── Model pricing ($ per 1K tokens) ─────────────────────────────────────────
# Prices reflect VoidAI rates as of 2026-03 (approximately)

@dataclass(frozen=True)
class ModelPrice:
    input_per_1m: float   # $ per 1,000,000 input tokens
    output_per_1m: float  # $ per 1,000,000 output tokens


MODEL_PRICING: dict[str, ModelPrice] = {
    # Claude models (Anthropic/VoidAI rates, approximate)
    "claude-opus-4-6":           ModelPrice(15.000, 75.000),
    "claude-opus-4-5":           ModelPrice(15.000, 75.000),
    "claude-sonnet-4-6":         ModelPrice(3.000,  15.000),
    "claude-sonnet-4-5":         ModelPrice(3.000,  15.000),
    "claude-sonnet-4-5-20250929":ModelPrice(3.000,  15.000),
    "claude-haiku-4-5-20251001": ModelPrice(0.800,  4.000),
    # OpenAI models
    "gpt-5.2":                   ModelPrice(5.000,  25.000),
    "gpt-4.1":                   ModelPrice(2.000,  8.000),
    "gpt-4.1-mini":              ModelPrice(0.400,  1.600),
    "gpt-4.1-nano":              ModelPrice(0.100,  0.400),
    "gpt-4o-mini":               ModelPrice(0.150,  0.600),
    # DeepSeek
    "deepseek-v3.1":             ModelPrice(0.140,  0.280),
    "deepseek-v3":               ModelPrice(0.140,  0.280),
    # Mistral
    "mistral-small-latest":      ModelPrice(0.100,  0.300),
    "mistral-medium-latest":     ModelPrice(0.400,  1.200),
    # Google
    "gemini-2.5-flash":          ModelPrice(0.075,  0.300),
    "gemini-2.5-pro":            ModelPrice(1.250,  5.000),
    "gemma-3n-e4b-it":           ModelPrice(0.020,  0.020),
}

# Fallback for unknown models: assume gpt-4.1-mini pricing
_DEFAULT_MODEL_PRICE = ModelPrice(0.400, 1.600)

# ─── Per-unit pricing ─────────────────────────────────────────────────────────

WAVESPEED_IMAGE_COST   = 0.005   # $ per image (z-image/turbo)
VOIDAI_IMAGE_COST      = 0.040   # $ per image (gpt-image-1.5 approximate)
VOICEAPI_COST_PER_CHAR = 0.00030 # $ per character (ElevenLabs Creator tier)
VOIDAI_TTS_COST_PER_CHAR = 0.000015  # $ per character (tts-1-hd: ~$15/1M chars)

# ─── Token estimates per pipeline step ────────────────────────────────────────
# Used for offline cost estimation when actual token counts are unavailable.

# Script generation: ~2500 input (prompt + transcript) + 3000 output (script blocks)
SCRIPT_INPUT_TOKENS  = 2_500
SCRIPT_OUTPUT_TOKENS = 3_000

# Hook validation (cheap model check of intro block)
HOOK_VALIDATE_INPUT  = 500
HOOK_VALIDATE_OUTPUT = 100

# Metadata generation: ~800 input (script summary) + 400 output (title/desc/tags)
META_INPUT_TOKENS    = 800
META_OUTPUT_TOKENS   = 400

# Image validation (VoidAI vision per image): ~300 input + 150 output
IMG_VALIDATE_INPUT   = 300
IMG_VALIDATE_OUTPUT  = 150

# Thumbnail vision validation per attempt
THUMB_VALIDATE_INPUT  = 500
THUMB_VALIDATE_OUTPUT = 200

# ─── CostEntry ────────────────────────────────────────────────────────────────

@dataclass
class CostEntry:
    """A single cost record for one API call or unit-priced operation."""
    module:        str
    model:         str
    input_tokens:  int   = 0
    output_tokens: int   = 0
    units:         float = 0.0   # Count of images / characters / etc.
    unit_label:    str   = ""    # "images", "chars", etc.
    cost:          float = 0.0


# ─── CostTracker ─────────────────────────────────────────────────────────────

class CostTracker:
    """
    Accumulates cost entries during pipeline execution.

    Usage:
        tracker = CostTracker()
        tracker.add_llm("Script", "claude-opus-4-6", input_tokens=2500, output_tokens=3000)
        tracker.add_images("Thumbnail", "wavespeed", count=2)
        tracker.add_voice("Voice", chars=6500)
        print(tracker.summary_table())
    """

    def __init__(self) -> None:
        self.entries: list[CostEntry] = []

    @property
    def total(self) -> float:
        return sum(e.cost for e in self.entries)

    def _model_price(self, model: str) -> ModelPrice:
        """Return pricing for the model, falling back to default if unknown."""
        # Try exact match first, then prefix match
        if model in MODEL_PRICING:
            return MODEL_PRICING[model]
        for key in MODEL_PRICING:
            if model.startswith(key) or key.startswith(model):
                return MODEL_PRICING[key]
        log.debug("Unknown model '%s' — using default pricing", model)
        return _DEFAULT_MODEL_PRICE

    def add_llm(
        self,
        module: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Record an LLM call cost. Returns the cost in USD."""
        price = self._model_price(model)
        cost = (input_tokens * price.input_per_1m + output_tokens * price.output_per_1m) / 1_000_000
        self.entries.append(CostEntry(
            module=module, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost=cost,
        ))
        return cost

    def add_images(
        self,
        module: str,
        provider: str,  # "wavespeed" or "voidai"
        count: int,
    ) -> float:
        """Record image generation costs. Returns cost in USD."""
        unit_cost = WAVESPEED_IMAGE_COST if provider.lower() == "wavespeed" else VOIDAI_IMAGE_COST
        cost = count * unit_cost
        model_label = "WaveSpeed z-image/turbo" if provider.lower() == "wavespeed" else "gpt-image-1.5"
        self.entries.append(CostEntry(
            module=module, model=model_label,
            units=float(count), unit_label="images",
            cost=cost,
        ))
        return cost

    def add_voice(
        self,
        module: str,
        chars: int,
        fallback: bool = False,
    ) -> float:
        """Record TTS voice generation cost. Returns cost in USD."""
        rate = VOIDAI_TTS_COST_PER_CHAR if fallback else VOICEAPI_COST_PER_CHAR
        cost = chars * rate
        model_label = "tts-1-hd (fallback)" if fallback else "VoiceAPI (ElevenLabs)"
        self.entries.append(CostEntry(
            module=module, model=model_label,
            units=float(chars), unit_label="chars",
            cost=cost,
        ))
        return cost

    def summary_table(self, title: str = "") -> str:
        """
        Return a formatted cost table string.

        Example output:
            Module        Model                  Input  Output  Units     Cost
            Script        claude-opus-4-6        2,500   3,000  -         $0.2325
            Images (7)    WaveSpeed z-image       -       -     7 images  $0.0350
            ...
            TOTAL                                                          $0.3012
        """
        if not self.entries:
            return "  (no costs recorded)"

        col_w = [16, 24, 7, 7, 12, 8]  # column widths
        header = (
            f"{'Module':<{col_w[0]}} {'Model':<{col_w[1]}} "
            f"{'Input':>{col_w[2]}} {'Output':>{col_w[3]}} "
            f"{'Units':<{col_w[4]}} {'Cost':>{col_w[5]}}"
        )
        sep = "-" * sum(col_w + [5])

        lines: list[str] = []
        if title:
            lines.append(f"  Cost estimate: {title}")
            lines.append("")
        lines.append("  " + header)
        lines.append("  " + sep)

        for e in self.entries:
            in_tok  = f"{e.input_tokens:,}"  if e.input_tokens  else "-"
            out_tok = f"{e.output_tokens:,}" if e.output_tokens else "-"
            units   = f"{e.units:.0f} {e.unit_label}" if e.unit_label else "-"
            lines.append(
                f"  {e.module:<{col_w[0]}} {e.model:<{col_w[1]}} "
                f"{in_tok:>{col_w[2]}} {out_tok:>{col_w[3]}} "
                f"{units:<{col_w[4]}} ${e.cost:>{col_w[5]-1}.4f}"
            )

        lines.append("  " + sep)
        lines.append(
            f"  {'TOTAL':<{col_w[0]}} {'':<{col_w[1]}} "
            f"{'':{col_w[2]}} {'':{col_w[3]}} "
            f"{'':<{col_w[4]}} ${self.total:>{col_w[5]-1}.4f}"
        )
        return "\n".join(lines)


# ─── estimate_cost() ──────────────────────────────────────────────────────────

def estimate_cost(
    script_path: str | Path | None,
    channel_config_path: str | Path,
    *,
    quality_preset: str | None = None,
    n_blocks: int | None = None,
    n_chars: int | None = None,
    n_langs: int = 1,
    validate_images: bool = True,
    avg_thumb_attempts: int = 2,
) -> CostTracker:
    """
    Estimate pipeline cost without making any API calls.

    Reads script.json (if available) for exact block/char counts; otherwise
    uses n_blocks/n_chars overrides or sensible defaults (10 blocks, 8000 chars).

    Args:
        script_path: Path to script.json (optional — for exact counts).
        channel_config_path: Path to channel config JSON.
        quality_preset: LLM preset name (max/high/balanced/bulk/test).
        n_blocks: Override block count (used if no script.json).
        n_chars: Override total narration char count (used if no script.json).
        n_langs: Number of languages for voice + subtitle generation.
        validate_images: Whether image validation via VoidAI vision is counted.
        avg_thumb_attempts: Average WaveSpeed attempts for thumbnail.

    Returns:
        CostTracker with estimated entries populated.
    """
    channel_config = load_channel_config(channel_config_path)
    llm_preset = get_llm_preset(channel_config, quality_preset)

    script_model    = llm_preset.get("script",    "claude-opus-4-6")
    metadata_model  = llm_preset.get("metadata",  "gpt-4.1-mini")
    thumbnail_model = llm_preset.get("thumbnail", "gpt-4.1")

    # ── Load actual counts from script.json if available ──────────────────────
    blocks_with_img = 0
    total_chars = 0

    if script_path and Path(script_path).exists():
        script = json.loads(Path(script_path).read_text(encoding="utf-8"))
        raw_blocks: list[dict[str, Any]] = script.get("blocks", [])
        blocks_with_img = sum(1 for b in raw_blocks if (b.get("image_prompt") or "").strip())
        total_chars = sum(len(b.get("narration") or "") for b in raw_blocks)
        n_blocks_actual = len(raw_blocks)
    else:
        n_blocks_actual = n_blocks or 10
        blocks_with_img = n_blocks_actual - 1  # assume 1 CTA block without image
        total_chars = n_chars or (n_blocks_actual * 800)  # ~800 chars/block default

    if n_chars is not None:
        total_chars = n_chars

    tracker = CostTracker()

    # ── Step 1: Script LLM ────────────────────────────────────────────────────
    tracker.add_llm(
        "Script",
        script_model,
        input_tokens=SCRIPT_INPUT_TOKENS,
        output_tokens=SCRIPT_OUTPUT_TOKENS,
    )
    # Hook validation (cheap)
    validator_model = "gpt-4.1-nano"
    tracker.add_llm(
        "Hook validation",
        validator_model,
        input_tokens=HOOK_VALIDATE_INPUT,
        output_tokens=HOOK_VALIDATE_OUTPUT,
    )

    # ── Step 2a: Images ───────────────────────────────────────────────────────
    tracker.add_images("Images", "wavespeed", count=blocks_with_img)
    if validate_images and blocks_with_img > 0:
        tracker.add_llm(
            "Image validation",
            "gpt-4.1-mini",
            input_tokens=blocks_with_img * IMG_VALIDATE_INPUT,
            output_tokens=blocks_with_img * IMG_VALIDATE_OUTPUT,
        )

    # ── Step 2b: Voice (per language) ─────────────────────────────────────────
    for lang_i in range(n_langs):
        label = "Voice" if lang_i == 0 else f"Voice (lang {lang_i + 1})"
        tracker.add_voice(label, chars=total_chars)

    # ── Step 3: Subtitles — no API cost ───────────────────────────────────────
    # (free — pure text processing)

    # ── Step 4: Video — no API cost ───────────────────────────────────────────
    # (FFmpeg only)

    # ── Step 5: Thumbnail ─────────────────────────────────────────────────────
    tracker.add_images("Thumbnail", "wavespeed", count=avg_thumb_attempts)
    if avg_thumb_attempts > 0:
        tracker.add_llm(
            "Thumb validation",
            thumbnail_model,
            input_tokens=avg_thumb_attempts * THUMB_VALIDATE_INPUT,
            output_tokens=avg_thumb_attempts * THUMB_VALIDATE_OUTPUT,
        )

    # ── Step 6: Metadata LLM ─────────────────────────────────────────────────
    tracker.add_llm(
        "Metadata",
        metadata_model,
        input_tokens=META_INPUT_TOKENS,
        output_tokens=META_OUTPUT_TOKENS,
    )

    return tracker


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cost_tracker",
        description="VideoForge Cost Tracker -- estimate pipeline API costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--script",  metavar="JSON", help="Path to script.json (for exact block/char counts)")
    parser.add_argument("--channel", metavar="JSON", required=True, help="Channel config JSON")
    parser.add_argument(
        "--quality", metavar="PRESET",
        choices=["max", "high", "balanced", "bulk", "test"],
        default=None,
        help="LLM quality preset (default: channel config default)",
    )
    parser.add_argument("--blocks", type=int, metavar="N", help="Override block count (if no script)")
    parser.add_argument("--chars",  type=int, metavar="N", help="Override total narration char count")
    parser.add_argument("--langs",  type=int, metavar="N", default=1, help="Number of languages (default: 1)")
    parser.add_argument("--no-validate", action="store_true", help="Exclude image validation cost")
    parser.add_argument("--thumb-attempts", type=int, default=2, metavar="N", help="Avg thumbnail attempts (default: 2)")
    parser.add_argument("--all-presets", action="store_true", help="Show estimate for all 5 quality presets")

    args = parser.parse_args()

    load_env()

    if args.all_presets:
        presets = ["max", "high", "balanced", "bulk", "test"]
        print()
        print("=" * 78)
        print("  Cost comparison across all quality presets")
        print("=" * 78)
        for preset in presets:
            tracker = estimate_cost(
                script_path=args.script,
                channel_config_path=args.channel,
                quality_preset=preset,
                n_blocks=args.blocks,
                n_chars=args.chars,
                n_langs=args.langs,
                validate_images=not args.no_validate,
                avg_thumb_attempts=args.thumb_attempts,
            )
            channel_config = load_channel_config(args.channel)
            llm = get_llm_preset(channel_config, preset)
            print()
            print(f"  Preset: {preset}  (script={llm['script']})")
            print(tracker.summary_table())
        print()
        print(f"  (Subtitles and Video: $0.0000 — no API cost)")
        print()
        return

    # Single preset
    tracker = estimate_cost(
        script_path=args.script,
        channel_config_path=args.channel,
        quality_preset=args.quality,
        n_blocks=args.blocks,
        n_chars=args.chars,
        n_langs=args.langs,
        validate_images=not args.no_validate,
        avg_thumb_attempts=args.thumb_attempts,
    )

    channel_config = load_channel_config(args.channel)
    llm = get_llm_preset(channel_config, args.quality)
    title = ""
    if args.script and Path(args.script).exists():
        try:
            script_data = json.loads(Path(args.script).read_text(encoding="utf-8"))
            title = script_data.get("title", "")
        except Exception:
            pass

    preset_label = args.quality or channel_config.get("llm", {}).get("default_preset", "max")
    print()
    print("=" * 78)
    if title:
        print(f"  Script : {title}")
    print(f"  Preset : {preset_label}  (script={llm['script']})")
    if args.langs > 1:
        print(f"  Langs  : {args.langs}")
    print("=" * 78)
    print()
    print(tracker.summary_table())
    print()
    print(f"  (Subtitles and Video compilation: $0.0000 -- no API cost)")
    print()


if __name__ == "__main__":
    main()
