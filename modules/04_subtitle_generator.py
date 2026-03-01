"""
VideoForge — Module 04: Subtitle Generator.

script.json (with audio_duration per block) → subtitles.srt + subtitles.ass.

Features:
- Generates SRT and ASS subtitle files from script.json blocks
- Timing: uses audio_duration per block (from Voice Generator) for exact timestamps
- ASS style from channel config (font, size, color, outline, position)
- Option: --from-transcript uses original transcript.srt from Transcriber as timing base
  (re-segments the SRT to match block narrations — preserves word-level timing)
- Both SRT and ASS output (ASS for FFmpeg burn-in, SRT as fallback / upload)

CLI:
    python modules/04_subtitle_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json

    # Use original Transcriber SRT for timing:
    python modules/04_subtitle_generator.py \\
        --script projects/my_video/script.json \\
        --channel config/channels/history.json \\
        --from-transcript "D:/transscript batch/output/output/MyVideo/transcript.srt"
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from modules.common import load_channel_config, load_env, setup_logging

log = setup_logging("subtitle_gen")

# ─── Constants ────────────────────────────────────────────────────────────────

# Default ASS style (overridden by channel config subtitle_style)
DEFAULT_ASS_STYLE = {
    "font":          "Arial Bold",
    "size":          48,
    "color":         "#FFFFFF",   # white
    "outline_color": "#000000",   # black outline
    "outline_width": 3,
    "position":      "bottom",
    "margin_v":      60,
}

# Characters per subtitle line before wrapping
MAX_CHARS_PER_LINE = 42

# Max subtitle duration for very long blocks (split into multiple entries)
MAX_SUBTITLE_DURATION = 8.0   # seconds


# ─── Timing helpers ───────────────────────────────────────────────────────────

def _fmt_srt(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    s = max(0.0, seconds)
    h  = int(s // 3600)
    m  = int((s % 3600) // 60)
    sec = int(s % 60)
    ms  = int(round((s % 1) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _fmt_ass(seconds: float) -> str:
    """Format seconds as ASS timestamp: H:MM:SS.cc"""
    s = max(0.0, seconds)
    h  = int(s // 3600)
    m  = int((s % 3600) // 60)
    sec = int(s % 60)
    cs  = int(round((s % 1) * 100))
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def _wrap_text(text: str, max_chars: int = MAX_CHARS_PER_LINE) -> str:
    """Wrap subtitle text at max_chars, returning \\N-joined lines (ASS) or \\n (SRT)."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# ─── Subtitle entry ───────────────────────────────────────────────────────────

class SubEntry:
    """One subtitle display entry with start/end timestamps."""

    def __init__(self, index: int, start: float, end: float, text: str) -> None:
        self.index = index
        self.start = start
        self.end   = end
        self.text  = text.strip()

    def to_srt(self) -> str:
        lines = "\n".join(_wrap_text(self.text))
        return f"{self.index}\n{_fmt_srt(self.start)} --> {_fmt_srt(self.end)}\n{lines}\n"

    def to_ass_event(self) -> str:
        lines = _wrap_text(self.text)
        ass_text = r"\N".join(lines)
        return (
            f"Dialogue: 0,{_fmt_ass(self.start)},{_fmt_ass(self.end)},"
            f"Default,,0,0,0,,{ass_text}"
        )


# ─── ASS header ───────────────────────────────────────────────────────────────

def _hex_to_ass_color(hex_color: str) -> str:
    """Convert #RRGGBB to ASS color &HAABBGGRR (alpha=00)."""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H00{b}{g}{r}"
    return "&H00FFFFFF"


def _build_ass_header(style: dict[str, Any]) -> str:
    """Build ASS [Script Info] + [V4+ Styles] header from style config."""
    font         = style.get("font", DEFAULT_ASS_STYLE["font"])
    size         = style.get("size", DEFAULT_ASS_STYLE["size"])
    color        = _hex_to_ass_color(str(style.get("color", DEFAULT_ASS_STYLE["color"])))
    outline_col  = _hex_to_ass_color(str(style.get("outline_color", DEFAULT_ASS_STYLE["outline_color"])))
    outline_w    = style.get("outline_width", DEFAULT_ASS_STYLE["outline_width"])
    margin_v     = style.get("margin_v", DEFAULT_ASS_STYLE["margin_v"])
    position     = style.get("position", DEFAULT_ASS_STYLE["position"])

    # ASS alignment: 2 = bottom-center, 8 = top-center, 5 = middle-center
    alignment = {"bottom": 2, "top": 8, "middle": 5}.get(str(position).lower(), 2)

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1280\n"
        "PlayResY: 720\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{color},&H000000FF,{outline_col},"
        f"&H00000000,-1,0,0,0,100,100,0,0,1,{outline_w},0,{alignment},"
        f"10,10,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


# ─── Block → subtitle entries ─────────────────────────────────────────────────

def _block_to_entries(
    block: dict[str, Any],
    start_time: float,
    entry_index_start: int,
) -> tuple[list[SubEntry], float]:
    """
    Split one script block into one or more subtitle entries.

    Splits at sentence boundaries if block duration > MAX_SUBTITLE_DURATION.

    Returns: (entries, end_time)
    """
    narration = block.get("narration", "").strip()
    duration  = block.get("audio_duration")

    if not narration or not duration:
        return [], start_time

    end_time = start_time + float(duration)

    # Split narration into sentences for readability
    sentences = re.split(r"(?<=[.!?…])\s+", narration.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return [], start_time

    # If the block is short enough: one entry
    if duration <= MAX_SUBTITLE_DURATION or len(sentences) == 1:
        entry = SubEntry(entry_index_start, start_time, end_time, narration)
        return [entry], end_time

    # Split into multiple entries proportional to sentence length
    total_chars = sum(len(s) for s in sentences)
    entries: list[SubEntry] = []
    current_time = start_time
    idx = entry_index_start

    for i, sentence in enumerate(sentences):
        frac    = len(sentence) / total_chars if total_chars else 1 / len(sentences)
        seg_dur = duration * frac
        seg_end = current_time + seg_dur

        # Clamp last segment
        if i == len(sentences) - 1:
            seg_end = end_time

        entries.append(SubEntry(idx, current_time, seg_end, sentence))
        current_time = seg_end
        idx += 1

    return entries, end_time


# ─── SRT parser (for --from-transcript) ──────────────────────────────────────

def _parse_srt(srt_path: Path) -> list[SubEntry]:
    """Parse a standard SRT file into SubEntry objects."""
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    entries: list[SubEntry] = []

    def _parse_ts(ts: str) -> float:
        ts = ts.strip().replace(",", ".")
        parts = ts.split(":")
        if len(parts) == 3:
            h, m, rest = parts
            return int(h) * 3600 + int(m) * 60 + float(rest)
        return 0.0

    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            idx  = int(lines[0].strip())
            ts   = lines[1].strip()
            match = re.match(r"(.+?)\s+-->\s+(.+)", ts)
            if not match:
                continue
            start = _parse_ts(match.group(1))
            end   = _parse_ts(match.group(2))
            txt   = " ".join(lines[2:]).strip()
            entries.append(SubEntry(idx, start, end, txt))
        except (ValueError, IndexError):
            continue

    return entries


def _resegment_from_transcript(
    transcript_entries: list[SubEntry],
    blocks: list[dict[str, Any]],
    entry_offset: int = 1,
) -> list[SubEntry]:
    """
    Re-segment transcript SRT entries to align with script blocks.

    For each block, finds transcript entries that fall within the block's
    audio_duration window and merges them. Falls back to block-based timing
    if no matching transcript entries.
    """
    if not transcript_entries:
        return []

    result: list[SubEntry] = []
    current_time = 0.0
    idx = entry_offset

    for block in blocks:
        narration = block.get("narration", "").strip()
        duration  = block.get("audio_duration")
        if not narration or not duration:
            continue

        block_end = current_time + float(duration)

        # Collect transcript entries within this block's time window
        matching = [
            e for e in transcript_entries
            if e.start >= current_time - 0.1 and e.start < block_end
        ]

        if matching:
            # Use transcript timing (more accurate word-level)
            for entry in matching:
                result.append(SubEntry(idx, entry.start, entry.end, entry.text))
                idx += 1
        else:
            # Fallback: generate entry from block narration
            new_entries, _ = _block_to_entries(block, current_time, idx)
            result.extend(new_entries)
            idx += len(new_entries)

        current_time = block_end

    return result


# ─── Main generation ──────────────────────────────────────────────────────────

def generate_subtitles(
    script_path: str | Path,
    channel_config_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    from_transcript: str | Path | None = None,
    lang: str | None = None,
) -> tuple[Path, Path]:
    """
    Generate SRT and ASS subtitle files from script.json.

    Args:
        script_path: Path to script.json (must have audio_duration per block).
        channel_config_path: Path to channel config JSON.
        output_dir: Where to save subtitles/. Default: script_path.parent/subtitles/.
        from_transcript: Optional path to Transcriber's transcript.srt for word-level timing.
        lang: Language suffix for output filenames (e.g. "de" → subtitles_de.srt).

    Returns:
        (srt_path, ass_path)
    """
    load_env()

    script_path = Path(script_path)
    script = json.loads(script_path.read_text(encoding="utf-8"))
    channel_config = load_channel_config(channel_config_path)

    subtitle_style = {**DEFAULT_ASS_STYLE, **channel_config.get("subtitle_style", {})}
    blocks: list[dict[str, Any]] = script.get("blocks", [])

    # Output paths
    subs_dir = Path(output_dir) if output_dir else script_path.parent / "subtitles"
    subs_dir.mkdir(parents=True, exist_ok=True)

    suffix = f"_{lang}" if lang else ""
    srt_path = subs_dir / f"subtitles{suffix}.srt"
    ass_path = subs_dir / f"subtitles{suffix}.ass"

    # Build subtitle entries
    if from_transcript:
        transcript_path = Path(from_transcript)
        if not transcript_path.exists():
            log.warning("Transcript SRT not found: %s — using block timing", transcript_path)
            from_transcript = None
        else:
            transcript_entries = _parse_srt(transcript_path)
            log.info("Loaded %d entries from transcript SRT: %s", len(transcript_entries), transcript_path.name)

    entries: list[SubEntry] = []

    if from_transcript and transcript_entries:
        entries = _resegment_from_transcript(transcript_entries, blocks)
        log.info("Re-segmented: %d subtitle entries from transcript timing", len(entries))
    else:
        # Generate from block audio_duration
        current_time = 0.0
        idx = 1
        missing_duration = 0

        for block in blocks:
            if not block.get("narration", "").strip():
                continue
            if not block.get("audio_duration"):
                missing_duration += 1
                continue

            new_entries, end_time = _block_to_entries(block, current_time, idx)
            entries.extend(new_entries)
            idx    += len(new_entries)
            current_time = end_time

        if missing_duration:
            log.warning(
                "%d block(s) missing audio_duration — run Voice Generator first for accurate timing",
                missing_duration,
            )
        log.info("Generated %d subtitle entries from %d blocks", len(entries), len(blocks))

    if not entries:
        log.error("No subtitle entries generated — check script.json has audio_duration set")
        raise ValueError("No subtitle entries generated")

    # ── Write SRT ──
    srt_content = "\n".join(e.to_srt() for e in entries)
    srt_path.write_text(srt_content, encoding="utf-8")
    log.info("SRT written: %s (%d entries)", srt_path.name, len(entries))

    # ── Write ASS ──
    ass_header = _build_ass_header(subtitle_style)
    ass_events = "\n".join(e.to_ass_event() for e in entries)
    ass_content = ass_header + ass_events + "\n"
    ass_path.write_text(ass_content, encoding="utf-8")
    log.info("ASS written: %s", ass_path.name)

    return srt_path, ass_path


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="VideoForge — Subtitle Generator (Module 04)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python modules/04_subtitle_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json

  # Use Transcriber's SRT for word-level timing:
  python modules/04_subtitle_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json \\
      --from-transcript "D:/transscript batch/output/output/MyVideo/transcript.srt"

  # German subtitles:
  python modules/04_subtitle_generator.py \\
      --script projects/my_video/script.json \\
      --channel config/channels/example_history.json --lang de
        """,
    )

    parser.add_argument(
        "--script",
        required=True,
        help="Path to script.json (must have audio_duration per block)",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel config JSON path",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory for subtitles (default: script.json dir / subtitles/)",
    )
    parser.add_argument(
        "--from-transcript",
        default=None,
        metavar="SRT_PATH",
        help="Path to Transcriber transcript.srt for word-level timing",
    )
    parser.add_argument(
        "--lang",
        default=None,
        help="Language suffix for output filename (e.g. 'de' → subtitles_de.srt)",
    )

    args = parser.parse_args()

    srt_path, ass_path = generate_subtitles(
        script_path=args.script,
        channel_config_path=args.channel,
        output_dir=args.output,
        from_transcript=args.from_transcript,
        lang=args.lang,
    )

    log.info("Done: %s | %s", srt_path, ass_path)


if __name__ == "__main__":
    _main()
