# Project Context — VideoForge
Останнє оновлення: 2026-03-01 20:00

## Що вже зроблено
- [x] Архітектура: 8 CLI-модулів, PROJECT_PLAN, dev.py
- [x] YouTube Transcriber — працює (окремий, не чіпаємо)
- [x] Thumbnail Analyzer — працює (окремий, не чіпаємо)
- [x] VoidAI API — 100+ моделей, chat/vision/tts/image
- [x] WaveSpeed API — картинки працюють
- [x] VoiceAPI (ElevenLabs) — протестовано
- [x] Мастер-промпт — для сценаріїв (дошліфовується)
- [x] Гайд по хуках YouTube — 540 рядків, 12 секцій, 10 шаблонів (→ prompts/hooks_guide.md)
- [x] №1 Ініціалізація проекту
- [x] №2 VoidAI клієнт
- [x] №3 WaveSpeed клієнт
- [x] №4 VoiceAPI клієнт
- [x] №5 FFmpeg утиліти
- [x] №6 Script Generator
- [x] №7 Image Generator
- [x] №8 Voice Generator
- [x] №9 Subtitle Generator
- [x] №10 Video Compiler
- [x] №11 Thumbnail Generator
- [x] №12 Metadata Generator
- [x] №13 YouTube Uploader
- [x] №14 E2E тест
- [x] №15 Фікс багів
- [x] №16 Pipeline Runner
- [x] №17 Cost Tracker
- [x] №18 Batch Runner
- [x] №19 SQLite Tracker (utils/db.py + інтеграція pipeline/batch)
- [x] №20 FastAPI бекенд (REST + WebSocket прогрес)
- [x] №21 React Dashboard (Vite + React + TypeScript + TailwindCSS v4)
- [x] №22 UI покращення (ETA, live timer, крапки кроків, batch pulse)
- [x] №23 UI канали і промпти (Channels + Prompts вкладки в дашборді)
- [x] №24 Docker контейнеризація
- [x] №25 README документація
- [x] №26 Transcriber integration (YouTube URL → yt-dlp → Transcriber → pipeline)
- [x] YouTube Panel UI (вибір відео, thumbnails A/B, title variants, upload)
- [x] 3 thumbnail A/B variants + 3 title variants (pipeline.py + backend/routes)
- [x] ETA + real-time progress bar наскрізний (pipeline.py + job_manager.py + JobCard.tsx)
- [x] FFmpeg block-by-block sub-progress (05_video_compiler.py → sub_progress WS events)

## Поточний стан
Всі основні модулі та UI завершені. Ведеться покращення прогресбару (sub-progress для кроку 2).

## Відомі баги
(немає)

## Прийняті рішення
- CLI-first → pipeline → UI
- VoidAI як єдиний AI провайдер (OpenAI-сумісний, 100+ моделей)
- 5 quality presets: max (claude-opus-4-6), high (claude-sonnet-4-5), balanced (gpt-5.2), bulk (deepseek-v3.1), test (mistral-small)
- Дефолт: max — якість сценаріїв критична, оптимізація пізніше
- Smart fallback chain: Opus → Sonnet → GPT (не retry тієї ж моделі)
- WaveSpeed = основний image gen, VoidAI image = fallback
- VoiceAPI = основний TTS, VoidAI TTS = fallback
- Input від Transcriber as-is (transcript.txt, thumbnail_prompt.txt, metadata.json)
- Review checkpoint: pipeline зупиняється після script.json в --review режимі
- Dry run: рахує вартість без API викликів
- Draft mode: 480p без ефектів для швидкої перевірки
- Prompt versioning: v1, v2... в конфігу каналу вказується яка версія
- Multi-lang: один сценарій → voice + subs в en/de/es
- Script compare: генерація N варіантів для вибору найкращого
- Hook system: гайд по хуках вбудований в мастер-промпт (3-крокова формула, 6 типів, SSSQ, 10 шаблонів)
- Hook validation: 4 критерії (clarity, curiosity, relevance, interest), auto-regenerate intro якщо fail
- Content templates: documentary, listicle, tutorial, comparison — промпт під формат
- Hook validation: дешева модель перевіряє перший блок на hook (retention)
- Intro/Outro: FFmpeg клеїть відео-шаблони каналу
- Background music: royalty-free на -20dB під голос
- Crossfade: 0.5с між блоками замість hard cut
- Audio normalization: loudnorm EBU R128 після конкатенації
- Image validation: VoidAI vision перевіряє якість, auto-regenerate
- Rate limiter: semaphore (5 WaveSpeed, 3 VoiceAPI) — захист від 429
- Retry budget: ліміт $ на відео, зупинка при перевищенні
- YouTube scheduling: відкладена публікація, auto-schedule для batch
- Ken Burns анімація (не AI video)
- Аудіо → тривалість → відео
- script.json — єдиний контракт
- Субтитри hardcoded ASS
- Git commit після кожної задачі

## Лог сесій
Дивись session_log.md
