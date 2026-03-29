"""VideoForge — shared constants for pipeline and modules."""

# ─── TTS Speed (words per minute) ────────────────────────────────────────────
# Measured on real tts-1-hd/onyx output: 168-172 wpm, calibrated to 170.
TTS_WPM = 170               # narration speed for duration calculation
TTS_WPM_SLOW = 130          # slowest reading rate (conservative estimate)
TTS_WPM_FAST = 150          # fast reading rate

# ─── Script Quality Thresholds ───────────────────────────────────────────────
QUALITY_GATE_RATIO = 0.80   # script must be ≥80% of target word count
TOO_SHORT_RATIO = 0.90      # validator flags as critical below 90% of min
TOO_LONG_RATIO = 1.25       # validator warns above 125% of max

# ─── Chunked Generation ─────────────────────────────────────────────────────
MAX_FIRST_CHUNK_TOKENS = 32_000
MAX_TOKENS_PER_CHUNK = 16_000
MAX_SCRIPT_CHUNKS = 8
MAX_EXPANSION_ROUNDS = 3
TOKEN_WORD_MULTIPLIER = 2.5  # total output tokens ≈ narration words × this

# ─── Rate Limiting (concurrent API calls) ────────────────────────────────────
WAVESPEED_CONCURRENCY = 5
VOICEAPI_CONCURRENCY = 3
VOIDAI_CONCURRENCY = 10
