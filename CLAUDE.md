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

## Key features
- **Review mode:** pipeline зупиняється після script.json для ревью перед витратою на images/voice
- **Dry run:** `--dry-run` рахує вартість без API викликів
- **Draft mode:** `--draft` збирає відео 480p без ефектів для швидкої перевірки
- **Prompt versioning:** prompts/master_script_v1.txt, v2.txt — конфіг каналу вказує версію
- **Hook system:** 3-крокова формула (Context Lean → "АЛЕ" → Snapback), 6 типів хуків per ніша, auto-validation intro блоку
- **Content templates:** documentary, listicle, tutorial, comparison — різні промпти під формат
- **Hook validation:** після генерації сценарію перевіряє чи перший блок має hook (retention!)
- **Multi-lang:** один сценарій → voice + subs в кількох мовах
- **Smart fallback:** Opus fail → Sonnet → GPT (не retry тієї ж моделі)
- **Кешування:** якщо images/ вже є — пропускає, генерує тільки що потрібно
- **Script compare:** `--compare 3` генерує 3 варіанти для вибору
- **Intro/Outro:** FFmpeg клеїть відео-шаблони каналу на початок/кінець
- **Background music:** royalty-free трек мікшується на -20dB під голос
- **Crossfade:** 0.5с плавний перехід між блоками (не hard cut)
- **Audio normalization:** loudnorm EBU R128 після конкатенації озвучки
- **Image validation:** VoidAI vision перевіряє якість картинок, auto-regenerate поганих
- **Rate limiter:** semaphore в клієнтах (max 5 WaveSpeed, 3 VoiceAPI) — захист від 429
- **Retry budget:** `--budget 5.00` ліміт витрат на одне відео
- **YouTube scheduling:** `--schedule` відкладена публікація, auto-schedule для batch

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
