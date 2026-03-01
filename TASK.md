# Поточна задача

## Задача №8 — Voice Generator
- script.json → VoiceAPI → audio/block_NNN.mp3 + ffprobe duration → оновлює script.json → concat full_narration.mp3
- **Audio normalization:** після конкатенації → loudnorm (EBU R128) через FFmpeg utils
- Fallback: VoidAI TTS (tts-1-hd)
- **Multi-lang ready:** `--lang de` → юзає voice_ids_multilang з конфігу, зберігає в audio/de/
- Залежить від: 4, 5, 6

## Наступна задача
№9 — Subtitle Generator

---
Після виконання: `python dev.py next -md` → git commit
