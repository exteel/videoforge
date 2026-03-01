# Project: VideoForge

## What this is
Автоматизоване створення YouTube-відео. 8 CLI-модулів:
Transcriber output → сценарій (VoidAI) → картинки (WaveSpeed) → озвучка (VoiceAPI) → відео (FFmpeg) → YouTube upload.

## Tech Stack
Python 3.11+, httpx (async), FFmpeg, pydantic, python-dotenv

## Structure
modules/01-08 — CLI модулі | clients/ — API клієнти | utils/ — FFmpeg, файли | config/ — канали | prompts/ — промпти

## Existing tools (тільки читаємо output, НЕ чіпаємо код)
- Transcriber (D:\transscript batch\Transcriber\) — Whisper транскрибація (GPU/CPU), yt-dlp, CustomTkinter GUI
  Output: transcript.txt, transcript.srt, metadata.json, title.txt, description.txt, thumbnail.jpg, thumbnail_prompt.txt
- Thumbnail Analyzer (D:\transscript batch\Thumbnail Analyzer\) — ітеративна генерація (5 спроб, 5/6 критеріїв)

## API — все через VoidAI (OpenAI-сумісний)
- **LLM presets** (channel_config.json → llm.presets):
  - max: claude-opus-4-6 (1x/5x) — **дефолт**, найкраща якість
  - high: claude-sonnet-4-5 (0.6x/3x) — близько до max, 2x дешевше
  - balanced: gpt-5.2 (0.4x/1.6x) — хороша якість
  - bulk: deepseek-v3.1 (0.03x) — масова генерація
  - test: mistral-small (0.006x) — тести пайплайну
- **Vision**: gpt-4.1 (thumbnail аналіз)
- **TTS backup**: tts-1-hd, gpt-4o-mini-tts
- **Image backup**: gpt-image-1.5, imagen-4.0
- **WaveSpeed** (z-image/turbo) — основний image gen, $0.005/шт
- **VoiceAPI** (voiceapi.csv666.ru) — основний TTS, ElevenLabs

## Rules
- Англійська для коду, type hints, docstrings
- Async/await для зовнішніх API
- Кожен модуль — standalone CLI з argparse + `if __name__ == "__main__"`
- Logging (не print), конфіг через .env
- script.json — єдиний формат обміну
- Аудіо ПЕРЕД відео (тривалість аудіо = тривалість картинки)
- Fallback pattern: WaveSpeed fail → VoidAI image, VoiceAPI fail → VoidAI TTS

## НЕ робити
- НЕ хардкодити API ключі/URLs
- НЕ moviepy — тільки FFmpeg subprocess
- НЕ sync API
- НЕ чіпати Transcriber / Thumbnail Analyzer код
- НЕ змінювати тести щоб проходили
- НЕ запускати dev-сервери
- НЕ масивні зміни в кількох модулях одночасно

## Session log
Після кожної відповіді → стислий запис в session_log.md (що, файли, рішення, далі)

## Проактивні нагадування
Задача велика → декомпозиція | Значна зміна → git commit | Безпека → повідом | Простіше → запропонуй | Неповний промпт → уточни
Формат: "> 💡 [текст]"

## Після сесії
1. Онови CONTEXT.md 2. Онови session_log.md 3. `python dev.py next -md` 4. Нагадай git commit

## Current focus
Задача №1 — Ініціалізація проекту
