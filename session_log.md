# Session Log — VideoForge

> Оновлюється після кожної відповіді. Нова сесія: `@session_log.md`

---

## 2026-03-02 — №31 Fix Ken Burns (zoompan fps=30) + fix music mixing codec

### utils/ffmpeg_utils.py — ken_burns()
- Dynamic crop w/h REMOVED: changing crop dimensions per frame → FFmpeg reinit error (-22)
- REPLACED with zoompan d=1:fps={fps} — one unique frame per `on` counter → smooth 30fps
- zoom_in: z=1+0.15*on/N, zoom_out: z=1.15-0.15*on/N, pans: constant z=1.15
- Seamless chain preserved: zoom_in end (z≈1.15) = zoom_out start (z=1.15)

### utils/ffmpeg_utils.py — mix_audio()
- Bug: -c:a aac written to .mp3 container → "Invalid audio stream" / Error -22
- Fix: .mp3 output → libmp3lame; others → aac

### tests/test_components.py (new)
- Isolated tests: --ken-burns, --music, --freq-tiers, --video-info

---

## 2026-03-02 — №30 Image frequency tiers sync with master_script_v2.txt

### modules/05_video_compiler.py
- `_DEFAULT_FREQ_TIERS` оновлено: 2 зони → 4 зони, відповідно до master_script_v2.txt density model
- Tier 1 (0–3 хв): кожні 10s | Tier 2 (3–6 хв): кожні 20s | Tier 3 (6–15 хв): кожні 60s | Tier 4 (15+ хв): кожні 120s
- Логіка cross-tier: сегмент починається з інтервалу активного на старті, ділення відбувається поступово
- Тест cross-boundary: блок 160s+60s → [10,10,20,20]✓; блок 870s+80s → [60,20]✓

---

## 2026-03-02 — №29 Bug fixes: smooth Ken Burns, image_style override, image frequency

### utils/ffmpeg_utils.py
- `ken_burns()` — замінено zoompan (ZOOMPAN_FPS=6 → frame duplication статтер на довгих блоках 130-240s) на dynamic crop filter з `t` time variable
- Новий підхід: motion обчислюється per-frame при 30fps (без дублювання) → ідеально плавний zoom/pan
- zoom_in: crop window shrinks overscan→output (15% → 0%); zoom_out: навпаки; pan_left/right: x позиція по `t`
- `T = max(duration, 0.001)` — безпечний знаменник

### modules/02_image_generator.py
- `generate_images()` — додано `image_style: str | None = None` параметр
- Якщо передано — перевизначає `channel_config["image_style"]`; якщо None — fallback на channel_config

### pipeline.py
- `generate_images()` тепер отримує `image_style=image_style or None` з `run_pipeline()` параметрів
- Видалено TODO коментар (`# TODO: image_style override`)

### modules/05_video_compiler.py
- Додано `_DEFAULT_FREQ_TIERS`, `_get_interval_for_time()`, `_split_duration_to_segments()`
- Ken Burns шлях: кожен блок ділиться на 10s сегменти (перші 3 хв відео) або 20s сегменти (після 3 хв)
- Кожен сегмент — окремий `ken_burns()` кліп з тим самим зображенням але іншою анімацією з `_KB_CYCLE`
- `elapsed_video_time` треків позицію відео для правильного вибору тиру; `_kb_idx` — глобальний індекс
- Конфігурується через `channel_config.image_frequency.{enabled, tiers}`; default = вбудований

**Commit:** `116460e`

---

## 2026-03-02 — №28 Validator improvements (10 changes)

### modules/01b_script_validator.py
- `_GENERIC_RE` розширено з 8 до 16+ паттернів: "A person thinking", "Concept of X", "Scene depicting", `generic\b`, "moody atmosphere" тощо — 12/12 тестів
- Cut-off detection: тепер сканує ВСІ блоки (не лише останні 2); mid-script → warning, кінець → critical
- Language heuristic: < 40% кирилиці для uk/ru/be/bg/sr/mk/kk/mn → `wrong_language` warning
- Dynamic `short_block` threshold: `max(15, duration_min × 2)` замість фіксованого 15
- Required fields check: відсутній `id` або `type` → critical
- Block order check: CTA/outro як перший блок → warning
- `_structural_checks` тепер приймає `language: str | None`

### modules/02b_image_validator.py
- Pre-regen: відсутні/пошкоджені зображення з `image_prompt` → авто-генерація перед scoring (не просто `failed`)
- WaveSpeed semaphore тепер тримається тільки під час POST, не під час 3-хв polling → справжній паралелізм
- Scoring failure → `skipped=True` замість фейкового `score=10.0`
- `_regen_one`: рекурсія замінена на `for attempt in range(1, max_attempts + 1)`

**Commits:** `e29e537` (10 покращень)

---

## 2026-03-02 — №27 Browser push notifications

### frontend/src/hooks/useNotifications.ts (NEW)
- `useNotifications()` hook — тонкий wrapper над Web Notifications API
- `permission` state (default/granted/denied), читається з `Notification.permission`
- `requestPermission()` — async, оновлює state після відповіді браузера
- `notify(title, body, { tag?, icon?, onlyWhenHidden? })` — stable `useCallback` з `[]` deps
  - Читає `Notification.permission` при виклику (не stale closure)
  - `onlyWhenHidden: true` — не показує якщо `document.hasFocus() && visibilityState === 'visible'`
  - `n.onclick → window.focus(); n.close()` — фокус на таб при кліку

### frontend/src/App.tsx
- Імпорт `useNotifications`
- `🔔` кнопка в navbar справа від tabs:
  - `permission === 'default'` → amber кнопка, клік → `requestPermission()`
  - `permission === 'denied'` → `🔕`, disabled, tooltip з інструкцією
  - `permission === 'granted'` → нічого (кнопка зникає)

### frontend/src/components/JobCard.tsx
- Імпорт `useRef` + `useNotifications`
- `notifyPtrRef` — відстежує кількість оброблених подій (уникає повторних нотифікацій)
- `useEffect` на `events` — сканує тільки НОВІ події:
  - `review_required` → `notify('VideoForge — Потрібне ревью', '…: Сценарій/Зображення готовий')`
  - `done` → `notify('VideoForge — Готово ✓', '…: відео згенеровано')`
  - `error` → `notify('VideoForge — Помилка', '…: error message[:120]')`
  - Всі з `tag: review/done/error-{job_id}` + `onlyWhenHidden: true`

### Build: ✓ 40 modules (було 39), TypeScript чистий

**Далі:** тест script validator → виявлено баґ → fix

---

## 2026-03-02 — Duration range control (duration_min / duration_max)

### Мотивація
Попередній `target_duration: int = 12` був захардкодженим і навіть не передавався з pipeline у generate_scripts (тільки дефолт). Тепер можна вказати "від 25 хв до 35 хв" і LLM отримає точний діапазон + цільову кількість слів.

### modules/01_script_generator.py
- `Script` model: `duration_min: int = 8`, `duration_max: int = 12` (зберігаються в script.json)
- `_build_user_prompt()`: `[DURATION]: 25-35 minutes\n[TARGET WORDS]: 3500–5250 words`
- CLI: `--duration-min`, `--duration-max`; `--duration` (legacy alias)

### modules/01b_script_validator.py
- `_structural_checks(blocks, duration_min=None, duration_max=None)` — dynamic thresholds
- `eff_too_long = int(duration_max * 150 * 1.25)` → 5202 слів + 25-35 min → no too_long ✓

### pipeline.py + backend/models.py + backend/routes/pipeline.py
- `duration_min/max` параметри наскрізь від UI до generate_scripts()

### frontend — JobList.tsx + api.ts
- Два числових поля "від [8] до [12] хв" з hint `≈ N–M слів`
- Build: ✓ 40 modules, TypeScript clean
- git: `e59dbf4`

---

## 2026-03-02 — Validator improvements (01b + 02b round 2)

### modules/01b_script_validator.py — нові перевірки та покращений auto-fix

**Нові структурні перевірки (no API cost):**
- `empty_narration` CRITICAL — блок без тексту narration
- `other_tag_in_narration` CRITICAL — `[SECTION`, `[CTA_SUBSCRIBE` теги в narration (parser artifact)
- `short_block` WARNING — narration < 15 слів (placeholder або обрізано)
- `too_long` WARNING — > 2500 слів (~18 хв) → Possible duplicate content
- `too_short` CRITICAL — < 80 слів всього → generation failure
- `duplicate_section` WARNING — однакові `timestamp_label` після нормалізації (the/a/an + пунктуація) → doubled LLM output

**Покращено `cut_off` перевірку:** тепер також перевіряє передостанній блок якщо останній — CTA/outro.

**Покращено `_fix_cut_off`:**
- Передає `title + niche + language` (раніше не передавав)
- Останні 5 блоків замість 3
- Вимога завершувати narration пунктуацією
- `max_tokens=3000` (було 2000)

**Post-fix re-check:** після всіх авто-фіксів запускає `_structural_checks()` знову → логує які critical issues залишились → `result.ok` оновлюється відповідно.

### modules/02b_image_validator.py — pre-flight + fallback + per-block threshold

**Pre-flight перевірка (до scoring):**
- `MIN_IMAGE_BYTES = 10_240` — зображення < 10KB → `skipped=True, skip_reason=...`
- Missing image → `skipped=True, skip_reason="Image file not found"`
- `ImageScore.skipped`, `ImageScore.skip_reason` — нові поля
- `ImageValidationResult.skipped` — лічильник пропущених
- CTA блоки без `image_prompt` — тихо пропускаються (не рахуються у skipped)

**Per-block threshold:** `eff_threshold = threshold - 0.5` для `intro/outro` блоків (atmospheric → менш строга оцінка).

**WaveSpeed → VoidAI fallback:** якщо WaveSpeed генерація провалилась, `_voidai_generate()` використовує `gpt-image-1.5` як fallback.

**`improved_prompt` → script.json:** після всіх рескорингів vision model's improved_prompt зберігається назад у `image_prompt` в script.json.

### Тест на реальному проекті "Why Confidence Is the Only Skill You Actually Need"
- Script validator: 6 issues (3 critical, 3 warnings) — correctly found `too_long` (5356 слів, ~38 хв) і `duplicate_section` ("Fake It Until You Become It")
- Image validator pre-flight dry-run: block_007 (CTA) правильно пропущено; block_001 (intro) threshold=6.5; missing+tiny image detection confirmed OK
- git commit: в процесі

---

## 2026-03-02 — Fix: embedded [IMAGE_PROMPT:] tags у narration

**Причина:** LLM іноді виводить `[IMAGE_PROMPT: content` без закриваючого `]` (обрізаний output або помилка форматування). Парсер мав два регекспи — обидва потребують `]` → незакриті теги потрапляли в narration і озвучувались TTS.

### modules/01_script_generator.py
- **flush()**: після `_IMAGE_INLINE_RE.sub()` додано `re.sub(r"\[IMAGE_PROMPT:.*?(?=\n\n|\Z)", "", narration, DOTALL)` — зупиняється перед `\n\n`, зберігає текст після параграфу
- **Line loop**: нова гілка — якщо рядок стартує з `[IMAGE_PROMPT:` без `]` → salvage у `image_prompt` (якщо порожній), `continue` (не додавати в narration)

### modules/01b_script_validator.py
- 3 нові regex: `_IMAGE_TAG_IN_NARRATION_RE`, `_UNCLOSED_IMAGE_TAG_RE` (зупиняється на `\n\n`), `_CLOSED_IMAGE_INLINE_RE`
- `_structural_checks()`: новий `bad_narration` → CRITICAL (блок містить `[IMAGE_PROMPT:` в narration)
- **Fix 0** (без API): очищає narration, salvage image_prompt з тегу якщо відсутній; запускається до cut_off/bad_prompt fixes
- Тест: block з тегом на початку → текст після `\n\n` збережено ✓; block з тегом в кінці → текст перед тегом збережено ✓

### Результат на реальному проекті
- До: `2 issues (1 critical)` — cut_off тільки
- Після: `4 issues (3 critical)` — cut_off + block_011 + block_014 bad_narration + duplicate_prompt
- git: `cd5da69`

---

## 2026-03-02 — Ken Burns fix + Validators + Review checkpoints

### utils/ffmpeg_utils.py
- `trunc()` у всіх 4 zoompan анімаціях (zoom_in, pan_left, pan_right) → прибрано тремтіння

### modules/05_video_compiler.py
- `_KB_CYCLE = ["zoom_in","pan_left","zoom_in","pan_right"]` — почерговий цикл анімацій
- `_animation_for_block(block, channel_config, block_index)` → `block_index % 4`

### modules/01b_script_validator.py (NEW)
- Структурні перевірки: cut_off, missing_cta, bad_prompt, duplicate_prompt
- Auto-fix: claude-sonnet-4-5 (cut_off), gpt-4.1-mini batch (bad_prompt, cta)
- Семафор: asyncio всередині функції (не module-level)

### modules/02b_image_validator.py (NEW)
- Vision scoring: gpt-4.1, concurrent (Semaphore 5), повертає score/reason/improved_prompt
- Auto-regen: WaveSpeed T2I, concurrent (Semaphore 3), max 2 attempts, re-score після
- Threshold: 7.0/10

### pipeline.py
- `review_callback: Any | None = None` — новий параметр (async callable)
- Після Step 1: `validate_and_fix_script()` + review pause (CLI: --review; WS: review_callback)
- Після images: `validate_and_fix_images()` + review_callback("images", {...})

### backend/job_manager.py
- `Job`: `review_stage: str | None`, `_review_events: dict[str, asyncio.Event]`
- `Job.approve(stage)` → sets Event → unblocks pipeline
- `review_callback` async в `_run_pipeline_job` → status="waiting_review", emit WS event, await event
- Передає `review_callback` в `run_pipeline()`

### backend/routes/pipeline.py
- `POST /api/jobs/{id}/approve?stage=script|images`

### backend/routes/ws.py
- Початковий статус включає `review_stage`
- При `waiting_review` — надсилає синтетичний `review_required` для late joiners

### backend/models.py
- `JobResponse`: додано `pct: float = 0.0`, `review_stage: str | None = None`

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
