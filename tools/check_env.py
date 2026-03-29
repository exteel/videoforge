"""Validate required environment variables are set.

Usage:
    python tools/check_env.py

Exits with code 1 if any required variable is missing, 0 otherwise.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

REQUIRED = {
    "VOIDAI_API_KEY": "VoidAI LLM API",
    "WAVESPEED_API_KEY": "WaveSpeed image generation",
    "VOICEAPI_KEY": "VoiceAPI TTS",
}

OPTIONAL = {
    "ACCESS_CODE": "API access protection",
    "TG_BOT_TOKEN": "Telegram bot notifications",
    "TG_ALLOWED_CHAT_ID": "Telegram chat ID",
    "TRANSCRIBER_OUTPUT": "Transcriber output directory",
}


def check() -> None:
    missing = []

    print("Required variables:")
    for var, desc in REQUIRED.items():
        val = os.getenv(var, "").strip()
        if not val:
            missing.append(f"  MISSING  {var} — {desc}")
            print(f"  [MISSING] {var} — {desc}")
        else:
            # Show only the first 4 chars to confirm it is set without leaking the secret
            preview = val[:4] + "..." if len(val) > 4 else "***"
            print(f"  [OK]      {var} ({preview}) — {desc}")

    print()
    print("Optional variables:")
    for var, desc in OPTIONAL.items():
        val = os.getenv(var, "").strip()
        if val:
            preview = val[:4] + "..." if len(val) > 4 else "***"
            print(f"  [OK]      {var} ({preview}) — {desc}")
        else:
            print(f"  [not set] {var} — {desc}")

    if missing:
        print(f"\nFAIL — {len(missing)} required variable(s) not set:")
        for m in missing:
            print(m)
        sys.exit(1)
    else:
        print("\nPASS — all required environment variables are set")


if __name__ == "__main__":
    print("VideoForge — Environment Check\n")
    check()
