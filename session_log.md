# Session Log — VideoForge

> Оновлюється після кожної відповіді. Нова сесія: `@session_log.md`

---

## 2026-03-01 — ETA + Smooth Progress Bar (наскрізний від транскрибації до відео)

### backend/job_manager.py
- `pct: float = 0.0` — нове поле в `Job` dataclass
- `to_response()` тепер повертає `pct`
- `progress_callback`: оновлює `job.pct` з `step_start`, `step_done`, `sub_progress` подій

### pipeline.py
- `STEP_WEIGHTS` dict — вагові частки кожного кроку: Script 0-15%, Media 15-55%, Subs 55-60%, Video 60-80%, Thumb 80-93%, Meta 93-100%
- Кожен `_emit(step_start/step_done)` тепер включає `pct=STEP_WEIGHTS[step][0/1]`
- Крок 4 (Video): `_video_sub_cb` — wrapper що транслює локальний pct `compile_video` у глобальний діапазон 60-80%
- Thread-safe: `_loop.call_soon_threadsafe(lambda e=ev: _emit(...))` для sub_progress з executor thread

### modules/05_video_compiler.py
- `progress_callback: Any | None = None` — новий параметр
- `_emit_progress(pct, message)` — внутрішній helper
- Після кожного блоку (Ken Burns): `i/n_blocks * 75%` → рух бару всередині кроку
- Після concat: 76% → 82%, music mix: 84%, add_audio: 88% → 94%

### frontend/src/api.ts
- `Job.pct: number` — нове поле (0-100)

### frontend/src/components/JobCard.tsx
- `calcETAfromPct(pct, elapsedSec)` — замість старого step-based ETA; точніший розрахунок на базі реального %
- `livePctFromWS` + `liveSubMsg` — з WS подій новішого типу `sub_progress/step_start/step_done`
- Priority: WS pct > snapshot `job.pct` > fallback `calcPct()`
- Sub-message рядок під прогресбаром: показує "Block 3/10", "Concat done" тощо

**Далі:** git commit

---

## 2026-03-01 — UI покращення (продовження сесії)

### api.ts
- Додано `VoiceMeta` interface
- Оновлено `PipelineRunRequest`: `background_music`, `image_style`, `voice_id`, `master_prompt`
- Додано `api.voices.list()` — `GET /api/voices`
- Додано `api.youtube.*` — status, auth, revoke, ready, upload, uploads

### JobCard.tsx (ETA + live timer)
- `useEffect` + `setInterval` — liveSec таймер поки job активний
- `calcPct()` — % прогресу з урахуванням оцінки часу per step
- `calcETA()` — ETA на основі avg seconds/step × remaining steps
- 7 крапок-індикаторів кроків (з `animate-pulse` на активному)
- Batch jobs: просто `animate-pulse` bar (немає step info)

### App.tsx
- `TAB_DESC` — описи для кожної вкладки під навбаром
- Нова вкладка `▲ YouTube`

### backend/models.py
- `PipelineRunRequest` + `master_prompt: str | None`

### backend/routes/pipeline.py
- Передає `background_music`, `image_style`, `voice_id`, `master_prompt` в `manager.start_pipeline()`

### backend/routes/youtube.py (новий)
- `GET /api/youtube/status` — перевірка OAuth2 токену
- `POST /api/youtube/auth` — запуск browser OAuth2 flow (thread)
- `POST /api/youtube/auth/revoke` — видалення токену
- `GET /api/youtube/ready` — список projects/ з final.mp4
- `POST /api/youtube/upload` — запуск завантаження (async task)
- `GET /api/youtube/uploads` — список upload jobs

### frontend/src/components/YoutubePanel.tsx (новий)
- Auth card: статус підключення, кнопка connect/disconnect, tips безпеки
- Список відео готових до завантаження (projects/ scan)
- VideoRow: вибір каналу, privacy (private/unlisted/public), datetime picker, auto-schedule, dry run
- Polling upload job status кожні 2с

### backend/main.py
- Зареєстровано `youtube_router`

**Далі:** git commit; Transcriber integration (YouTube URL → Transcriber → pipeline)

---

## 2026-03-01 — Opus timeout fix
- **Проблема:** claude-opus-4-6 тайм-аут при генерації скрипту — 21KB transcript + 34KB hooks_guide = ~43K токенів промпт
- **Fix 1:** `MAX_TRANSCRIPT_CHARS = 14_000` у `modules/01_script_generator.py` → truncate `transcript.txt` перед вставкою в промпт
- **Fix 2:** `MAX_HOOKS_GUIDE_CHARS = 6_000` → truncate `prompts/hooks_guide.md` при завантаженні (specific hook instruction вже є в user prompt через `HOOK_INSTRUCTIONS`)
- **Fix 3 (попередня сесія):** `DEFAULT_TIMEOUT = 300.0` у `clients/voidai_client.py` (120s → 300s)
- **Результат:** Total prompt ~30KB (~22K токенів) — Opus має впоратися в межах 300s
- **Далі:** Тест пайплайну з реальним відео; задача №23

---

## 2026-03-01 — №21 React Dashboard
- **Зроблено:** `frontend/` — Vite + React + TypeScript + TailwindCSS v4
- **Структура:** `api.ts` (HTTP клієнт), `hooks/useWebSocket.ts`, 4 компоненти + App.tsx
- **Jobs tab:** форма запуску Single Video / Batch; активні jobs з real-time step progress bar через WS; логи (розгортаються); Cancel кнопка
- **History tab:** таблиця відео з SQLite; фільтр по статусу; клік → модальне вікно з cost breakdown
- **Stats tab:** 4 stat cards (total, done, failed, total cost); таблиця by model; прогрес-бари by preset
- **WS:** `useWebSocket` hook → live step updates → `JobCard` progress bar 1/6 → 6/6
- **Проксі:** vite.config.ts `/api` → `localhost:8000`, `/ws` → `ws://localhost:8000`
- **Build:** `npm run build` ✓ (35 modules, 215kB JS, 14.7kB CSS)
- **Далі:** №22 UI ревью і створення

---

## 2026-03-01 — №20 FastAPI бекенд
- **Зроблено:** `backend/` пакет — REST API + WebSocket прогрес
- **pipeline.py:** `progress_callback` параметр + `_emit()` helper; виклики на початку/кінці кожного з 6 кроків
- **backend/models.py:** Pydantic схеми (PipelineRunRequest, BatchRunRequest, JobResponse, VideoDetail, StatsResponse)
- **backend/job_manager.py:** JobManager singleton; Job dataclass з asyncio.Queue subscribers; start_pipeline/start_batch → asyncio.Task; progress_callback → fan-out до WS; set_failed при помилці
- **backend/routes/pipeline.py:** POST /api/pipeline/run, POST /api/batch/run, GET /api/jobs, GET /api/jobs/{id}, DELETE /api/jobs/{id}
- **backend/routes/videos.py:** GET /api/videos, GET /api/videos/{id}, GET /api/videos/{id}/costs, PUT /api/videos/{id}/youtube, GET /api/stats
- **backend/routes/ws.py:** WebSocket /ws/{job_id} — поточний стан + логи при підключенні, потім live stream; heartbeat 25s
- **backend/main.py:** FastAPI app + CORS + lifespan; старт: `uvicorn backend.main:app --reload`
- **Тест:** 15 routes ✓, import OK ✓
- **Далі:** №21 React Dashboard

---

## 2026-03-01 — №19 SQLite Tracker (продовження після context compaction)
- **Зроблено:** Інтеграція `utils/db.py` у `pipeline.py` та `batch_runner.py`
- **Що зроблено в utils/db.py:** VideoTracker (create_video, set_running, set_done, set_failed, set_skipped, set_youtube_url, record_cost, record_costs_from_tracker, list_videos, get_costs, session_stats) — було готово в попередній сесії
- **Інтеграція pipeline.py:** `db_tracker` + `db_video_id` параметри; set_running після setup; _video_path/_thumb_path захоплюються в steps 4-5; set_done + record_costs в DONE section; `--track` / `--db` CLI flags; main() з try/except для set_failed
- **Інтеграція batch_runner.py:** `db_tracker` в `_process_one`; create_video перед run_pipeline; set_failed в обох exception handlers; `db_path` в run_batch; `--track` / `--db` CLI flags
- **Тест:** lifecycle test (create→running→done + costs) ✓
- **Далі:** №20 FastAPI бекенд

---

## 2026-03-01 — №16 Pipeline Runner (продовження попередньої сесії)
- **Зроблено:** `pipeline.py` (630 рядків) — повний pipeline runner
- **Кроки:** 1=Script → 2=Images+Voices (parallel) → 3=Subs → 4=Video → 5=Thumb → 6=Meta
- **Фічі:** --dry-run, --from-step N, --lang en,de,es, --budget, --review, --draft, --quality, --template
- **Рішення:** dry-run guard для steps 2-6 коли script.json ще не існує (step 1 також dry-run); compile_video потребує full_narration.mp3 навіть у dry-run → guard через glob
- **Ключове:** importlib.util для завантаження модулів з числовими префіксами; sys.stdout.reconfigure(utf-8) для argparse --help на Windows; `loop.run_in_executor` для sync compile_video
- **Тести:** --help ✓, --dry-run ✓, --from-step 2 --dry-run ✓, --from-step 3 --lang en,de --dry-run ✓
- **Далі:** №17 Cost Tracker

---

## 2026-03-01 — Планування
- **Зроблено:** Архітектура, всі стартові файли, аналіз існуючих інструментів
- **Рішення:** VoidAI єдиний AI провайдер, claude-opus-4-6 як дефолт для сценаріїв (якість критична), fallback pattern для WaveSpeed і VoiceAPI, 5 quality presets (max→test)
- **Ключове:** Transcriber вже генерує thumbnail_prompt.txt — переюзати. VoidAI має TTS і image gen як backup.
- **Покращення v2:** review checkpoint, dry run, draft mode, prompt versioning, multi-lang, smart fallback chain, script compare, step caching
- **Покращення v3:** content templates, hook validation, intro/outro, audio loudnorm, retry budget
- **Покращення v4:** background music (-20dB), crossfade 0.5с, image validation (vision), rate limiter (semaphore), YouTube scheduling
- **Покращення v5:** Hook system — гайд 540 рядків вбудований в pipeline (3-крокова формула, 6 типів хуків, SSSQ, 10 шаблонів, 4 смертельні помилки, auto-validation intro)
- **Далі:** №1 — ініціалізація

## 2026-03-01 — №14 Test fixtures + pipeline test

- **Зроблено:** tests/test_pipeline.py (330+ рядків) + tests/test_data/ fixtures
- **Fixtures:** sample_transcriber_output/ (6 файлів), script_full.json (8 блоків, 196с total)
- **Тест:** 15/15 пройшло — imports, --help, FFmpeg, timestamps unit test, subtitle_generator real run, dry-run для всіх 8 модулів
- **Реальний subtitle_generator:** генерує SRT + ASS без API; перевіряє монотонність timestamps
- **Fake files:** valid PNG (_make_png), valid ID3 MP3 header (_make_mp3) для size checks
- **Що потрібно для реального прогону:** .env з API keys + Transcriber output dir
- **Git:** feat: №14 Test fixtures + pipeline integration test (15/15 pass); dev.py next -md → №15
- **Далі:** №15 Фікс багів

## 2026-03-01 — №13 YouTube Uploader

- **Зроблено:** modules/08_youtube_uploader.py (595 рядків) + requirements.txt (google libs)
- **Логіка:** output/{final.mp4, thumbnail.png, metadata.json} → YouTube Data API v3 → video_id + url
- **OAuth2:** YOUTUBE_CLIENT_ID/SECRET з env → token.json кеш → browser auth першого разу → refresh automatic
- **Resumable upload:** 8MB chunks, MAX_RETRIES=5 для network errors
- **Schedule:** --schedule "2026-03-05 18:00" → UTC ISO8601 + privacy=private
- **Auto-schedule:** channel config {interval_days:7, time:"18:00"} → next slot; state зберігається в config/oauth2/{channel}_schedule.json
- **Dry-run тест:** schedule→2026-12-31T16:00:00Z OK; auto-schedule→+7d від сьогодні OK
- **Git:** feat: №13 YouTube Uploader; dev.py next -md → №14
- **Далі:** №14 Тестові дані і повний прогон

## 2026-03-01 — №12 Metadata Generator

- **Зроблено:** modules/07_metadata_generator.py (397 рядків)
- **Логіка:** script.json → timestamps (з audio_duration) → VoidAI gpt-4.1-mini → title + description + tags → metadata.json
- **Timestamps:** cumulative sum audio_duration, формат M:SS, skip пусті CTA блоки; вбудовуються в description
- **Output:** output/metadata.json: title, description, tags, category_id, language, timestamps, total_duration_seconds
- **Dry-run:** 3 timestamps правильні (0:00→0:45→3:05 з 45+130+55 сек)
- **Git:** feat: №12 Metadata Generator; dev.py next -md → №13
- **Далі:** №13 YouTube Uploader

## 2026-03-01 — №11 Thumbnail Generator

- **Зроблено:** modules/06_thumbnail_generator.py (485 рядків)
- **Логіка:** thumbnail_prompt (Transcriber > script.json) + channel style → WaveSpeed "1280*720" → thumbnail.png
- **Validation:** VoidAI vision (gpt-4.1 з channel preset) → 6 критеріїв (composition, focal_point, colors, quality, topic_match, professional) → pass ≥ 5/6
- **Retry:** seed=42 спроба 1, random потім; до max_attempts (дефолт 5); зберігає найкращий результат
- **Fallback:** WaveSpeed → VoidAI gpt-image-1.5
- **--no-iterate:** skip validation (single-pass, fast); **--text:** text overlay hint в промпт
- **Dry-run тест:** пройшов — prompt = thumbnail_prompt + thumbnail_style + text overlay
- **Git:** feat: №11 Thumbnail Generator; dev.py next -md → №12
- **Далі:** №12 Metadata Generator

## 2026-03-01 — №10 Video Compiler

- **Зроблено:** modules/05_video_compiler.py (489 рядків)
- **Логіка:** images/ + audio/ + subtitles.ass → final.mp4 (1920x1080 H.264)
- **Кроки:** Ken Burns (zoompan) → concat з crossfade → add_audio → mix_audio (-20dB) → burn subs → intro/outro → final.mp4
- **Draft mode:** 854x480, без Ken Burns, без crossfade, ultrafast encode
- **Особливості:** tempfile.TemporaryDirectory для проміжних файлів, fallback на попередній image для CTA-блоків, random pick background music track
- **Dry-run тест:** пройшов — 2 voiced blocks, 1 image, 10.5s audio, output path OK
- **Git:** feat: №10 Video Compiler; dev.py next -md → №11
- **Далі:** №11 Thumbnail Generator

## 2026-03-01 — №7 Image Generator

- **Зроблено:** modules/02_image_generator.py (523 рядки)
- **Логіка:** script.json → блоки з image_prompt → WaveSpeed паралельно (asyncio.gather) → images/block_NNN.png
- **Validation:** gpt-4.1-mini vision → 3 критерії (MATCH, CLEAN, QUALITY) → auto-regenerate при fail, max 2 retries
- **Fallback:** WaveSpeed exception → VoidAI gpt-image-1.5; step caching (>5KB → skip)
- **Dry-run тест:** пройшов — 2 блоки з промптами, CTA пропущено, $0.010 оцінка
- **Git:** feat: №7 Image Generator; dev.py next -md → №8
- **Далі:** №8 Voice Generator

## 2026-03-01 — №5 FFmpeg utils + №6 Script Generator

- **Зроблено:** utils/ffmpeg_utils.py (808 рядків), modules/01_script_generator.py (847 рядків)
- **FFmpeg utils:** get_duration, resize, ken_burns (zoompan, 4 анімації + static), concat_videos (xfade crossfade), add_audio, normalize_audio (EBU R128 two-pass), mix_audio (-20dB music), concat_audio, add_subtitles (ASS/SRT burn-in), extract_audio, check_ffmpeg
- **Script Generator:** Pydantic моделі (Script, ScriptBlock, HookInfo, HookValidationResult), parser LLM output ([SECTION X:] + [IMAGE_PROMPT:] + [CTA_*]), prompt versioning, 4 templates, 5 hook types, hook validation (gpt-4.1-nano + hook_validator.txt), auto-regenerate intro, --compare N, --dry-run, dry-run OK
- **Рішення:** Fallback якщо немає [SECTION] маркерів → один блок; parser state machine (flush по секціям); suggested_rewrite від валідатора = заміна intro без extra LLM call
- **Git:** feat: №5 FFmpeg utilities + feat: №6 Script Generator
- **Далі:** №7 Image Generator

## 2026-03-01 — №1 + №2 + GitHub setup
- **Зроблено:** prompts/hooks_guide.md, prompts/hook_validator.txt (4 критерії: clarity/curiosity/relevance/interest, pass≥3/4), clients/voidai_client.py
- **VoidAI client:** async httpx, chat/vision/tts/image, smart fallback Opus→Sonnet→GPT, semaphore(10), exponential backoff, cost tracking (MODEL_COSTS table)
- **GitHub:** repo exteel/videoforge, main branch, 7 комітів запушено, master видалено
- **Далі:** №3 — WaveSpeed клієнт
