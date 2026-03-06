# Session Log — VideoForge

> Оновлюється після кожної відповіді. Нова сесія: `@session_log.md`

---

## 2026-03-07 — WebP bug fix, image duration fix, відео "If 2025 Felt Like a Failure"

### Питання
Перша картинка у статичному відео тримається 40-45 секунд — діагностика і виправлення.

### Виконано

**Root cause**: WaveSpeed `flux-dev-ultra-fast` повертає первинні зображення у форматі **WebP** (ігноруючи `output_format='png'`), зберігаючи їх з розширенням `.png`. FFmpeg concat demuxer декодує WebP лише 1 кадром → зображення не може зациклюватись на задану тривалість → "перша картинка 40 секунд" (насправді тримається freeze останнього кадру).

**`clients/wavespeed_client.py`** — `_download()`:
- Перевірка magic bytes `RIFF....WEBP` після завантаження
- Конвертація WebP→PNG через Pillow перед збереженням
- Логує: `WebP→PNG converted: block_001.png (312 KB → 1497 KB)`

**`modules/05_video_compiler.py`** — фікс `image_word_offsets[0]`:
- Bug: перше зображення рахувалося від `offsets[0]` замість `0`
- Якщо `offsets[0]=15` при 100 словах → перший кадр отримував 35% замість 50%
- Фікс: `start_off = offsets[ii] if ii > 0 else 0`

**Конвертація існуючих зображень**:
- 15 файлів `block_NNN.png` (WebP-as-PNG) → конвертовані через Pillow в реальний PNG
- Після: всі 103 зображення у проекті — справжній PNG ✅

**Перекомпіляція** (job `3a887cd7`, step 4 only, no_ken_burns=True):
- Виконано за **275.6s** (раніше 518s з KenBurns)
- Keyframes: зображення змінюються кожні ~8.3s ✅ (а не 40-45s)
- Output: `output/final.mp4` — 126 MB, 2017s, 1920×1080

### Файли
- `clients/wavespeed_client.py` — WebP→PNG в `_download()`
- `modules/05_video_compiler.py` — `start_off = 0` для першого зображення

**Розслідування "Ken Burns vs Static"**:
- Помилковий висновок: H.264 temporal encoding (I/P/B frames) дає різні хеші навіть для статичних кадрів
- Правильний метод: порівнювати кадри на різних часових мітках (кожні 10с) — всі різні хеші = різні зображення ✅
- Підтверджено: обидва `test5min.mp4` (CLI) та `final.mp4` (server job) — в статичному режимі ✅
- "Перша картинка 45с" = старий `use_static` path брав тільки `imgs[0]` для цілого блоку (НЕ візуальна схожість)

**Root cause (фінальний)**: старий код `use_static`:
```python
image_path = imgs[0] if imgs else None  # тільки перше зображення!
frames.append((image_path, duration))   # весь блок = 1 картинка
```
Новий код розподіляє всі imgs по `image_word_offsets` = 5 зображень x ~10с.

**Доказ** (I-frame аналіз ffprobe):
- Стара компіляція: тільки periodic GOP кожні 8.33с (= 250 frames/30fps) — жодного scene-change
- Нова компіляція: scene-change I-frames на 10.17s, 20.43s, 30.63s, 40.83s — переходи картинок

**Фінальна перекомпіляція** (CLI):
- `python3 modules/05_video_compiler.py --script ... --no-ken-burns --no-music`
- `final.mp4 121.6 MB` — перевірено користувачем, картинки міняються

**CLI `--no-ken-burns`**:
- Додано CLI аргумент `--no-ken-burns` до `05_video_compiler.py`
- Тестовий скрипт: `projects/.../script_test5min.json` (перші 4 блоки ~6хв для швидких тестів)
- CLI тест: `python modules/05_video_compiler.py script_test5min.json config/... --no-ken-burns` → `mode=static` ✅
- Output: `output/test5min.mp4` (18MB, 377.5s)

**Також реалізовано (вже були в коді)**:
- `_image_for_segment()` safety fallback для Ken Burns mode — вже є ✅
- `burn_subtitles` checkbox — вже реалізований у backend + frontend ✅

### Файли
- `clients/wavespeed_client.py` — WebP→PNG в `_download()` + repr() paths
- `modules/05_video_compiler.py` — multi-image static path + `--no-ken-burns` CLI
- `utils/ffmpeg_utils.py` — repr() шляхи в concat.txt (Windows cross-drive fix)
- `pipeline.py` — VOIDAI_PER_TOKEN константа для cost tracking
- `modules/01c_image_planner.py` — 'script' preset key для моделі

### Далі
- Повна перекомпіляція `final.mp4` якщо потрібно (образи вже правильні PNG)
- Перегляд через UI/плеєр фінального відео

---

## 2026-03-05 — Full batch run: 8 відео 25-33хв, subtitle fix, no music

### Питання
Запустити 8 відео від початку до кінця: 25-33 хвилини, без музики, субтитри макс 2 рядки, BetaTest Max якість.

### Виконано

**`modules/04_subtitle_generator.py`** — ліміт 2 рядки на субтитр:
- `MAX_CHARS_PER_LINE`: 32 → **45** (менше переносів)
- `MAX_LINES_PER_ENTRY = 2` — нова константа
- `_split_to_fit()` — рекурсивна функція яка ділить текст при ком/союзах до ≤2 рядків
- `_block_to_entries()` → замінено `sentences` на `chunks = _split_to_fit(sentence)` для кожного речення

**Генерація 8 відео** (25-33 хв, no music, BetaTest Max):
- `3941464a` — 6 Dangerous Signs — 26.7хв, 32 img ✅
- `e5c87f8f` — 8 Things Sophia — 15.5хв ⚠️ (короткий оригінал)
- `dcb35b1e` — Focus Only Yourself — 27.8хв, 31 img ✅
- `9a45bc52` — How To Begin Again — 32.4хв, 41 img ✅
- `3a432ab4` — No Healing (Mother) — 28.6хв, 34 img ✅
- `2a2c4675` — Psychology Of Men — 36хв, 42 img ✅
- `84a178bb` — Why Awakened (Love) — 34хв, 43 img ✅
- `5db2bcb9` — Why Staying Home — 28хв, 28 img ✅

**VoiceAPI баланс**: 444k символів = 22год аудіо. Вартість 30хв відео: ~$0.04

**Проблеми вирішені**:
- 404 rate limit burst → `parallel=1` для повторного запуску 5 джобів
- `e5c87f8f` вийшов 15.5хв → потребує перегенерації окремо

### Файли
- `modules/04_subtitle_generator.py` — subtitle 2-line limit

### Далі
- Перегенерувати `e5c87f8f` (8 Things Sophia) з більш тривалим контентом

---

## 2026-03-05 — image_backend + vision_model (full stack UI selection)

### Питання
Додати вибір backend для генерації картинок і моделі для аналізу картинок (UI dropdowns)

### Виконано

**`02_image_generator.py`** — додано `image_backend` param + виправлено broken `_run_coros`:
- `_backend = (image_backend or "wavespeed").lower()` → роутинг до `PrimaryClient`
- `betatest` → `BetaImageClient`, `image_size = "16:9"` (BetaImage не підтримує custom size)
- `voidai` → `PrimaryClient = None`, `_ws_global_failed = [True]`
- `wavespeed` → `WaveSpeedClient` (default)
- Backend-aware `async with`: `PrimaryClient() + VoidAIClient()` або тільки `VoidAIClient()`
- `primary_cost` замість `wavespeed_cost` у GenerationSummary (backward-compatible)

**`02b_image_validator.py`** — додано `vision_model` param:
- `_score_image(...)` — новий kwarg `vision_model: str | None = None`, `model = vision_model or SCORE_MODEL`
- `validate_and_fix_images(...)` — новий kwarg `vision_model`, передається до обох `_score_image` calls
- Log показує ефективну модель (`_eff_model = vision_model or SCORE_MODEL`)

**`backend/models.py`** — `image_backend: str | None` + `vision_model: str | None` в `PipelineRunRequest`

**`backend/routes/pipeline.py`** — передає обидва нових params до `manager.start_pipeline()`

**`pipeline.py`** — `image_backend` + `vision_model` у сигнатурі, передає до `generate_images()` / `validate_and_fix_images()`

**`frontend/src/api.ts`** — `image_backend?: string | null` + `vision_model?: string | null` в `PipelineRunRequest`

**`frontend/src/components/JobList.tsx`** — `PFormState` + initial state + два `<select>` dropdown'и після `burn_subtitles`

### Файли
- `modules/02_image_generator.py` — backend routing + async with fix
- `modules/02b_image_validator.py` — vision_model param
- `backend/models.py` — new fields
- `backend/routes/pipeline.py` — pass-through
- `pipeline.py` — pass-through
- `frontend/src/api.ts` — interface update
- `frontend/src/components/JobList.tsx` — PFormState + dropdowns

---

## 2026-03-05 — TTS Sanitizer + META_TEXT_CLEANED + Rule 5 TRUNCATED_NARRATION

### Питання
1. Технічний markup не повинен потрапляти в озвучку, але блоки — не видаляти
2. META_TEXT: спочатку стрипати мета-префікс, видаляти тільки якщо немає реального контенту
3. Захист від generation cutoff → truncated sentence у TTS

### Виконано

**`modules/script_validator.py`** — 3 покращення:

`sanitize_narration_for_tts(text)` — нова публічна функція:
- Стрипає `[IMAGE_PROMPT:]`, `[SECTION N:]`, `[CTA_SUBSCRIBE_*]`, markdown (`**`, `*`, `__`, `#`)
- НЕ змінює script.json — тільки TTS runtime safety net

Rule 1 META_TEXT — strip-first замість remove-always:
- `_extract_real_content_after_meta()` знаходить реальний контент після мета-префіксу
- Якщо ≥30 слів → `META_TEXT_CLEANED` (блок зберігається з cleaned narration)
- Якщо <30 слів → `META_TEXT_REMOVED` (блок видаляється як раніше)

Rule 5 TRUNCATED_NARRATION (нове):
- Detects narration що не закінчується на `.!?"'…)`
- `_last_sentence_end()` знаходить останній sentence boundary
- Trim incomplete fragment, блок НІКОЛИ не видаляється
- Захист від: LLM hit max_tokens mid-sentence → TTS озвучує обірване речення

**`modules/03_voice_generator.py`** — 1 рядок:
- `narration = sanitize_narration_for_tts(...)` замість `.strip()`

### Файли
- `modules/script_validator.py` — 3 покращення + 1 нова публічна функція
- `modules/03_voice_generator.py` — sanitize call before TTS

---

## 2026-03-05 — Script Validator + _call_llm Patches (system-level quality fixes)

### Питання
Виправити 4 баги на системному рівні, щоб більше не повторювались:
1. Meta-text в continuation chunks ("I need to reassess...")
2. Double ending (секції після першого outro)
3. Duplicate CTAs (два однакових CTA блоки)
4. Duplicate practices (той самий список практик у 2 блоках)

### Виконано
**`modules/script_validator.py`** — новий модуль (260 рядків):
- `validate_and_fix(script_dict)` → `(fixed_dict, list[ValidationIssue])`
- Rule 1 `META_TEXT`: regex з 20+ LLM self-reference phrases → видаляє блок
- Rule 2 `SECTIONS_AFTER_OUTRO`: зберігає перший outro + CTA, видаляє всі решта типи після нього
- Rule 3 `DUPLICATE_CTA`: порівнює перші 50 символів narration; лишає останній CTA
- Rule 4 `DUPLICATE_PRACTICES`: frozenset named practices (Practice One/Two, Shadow Dialogue…); лишає останній блок
- `_reindex()`: переіменовує block_id + order після видалень
- Self-test: 7 блоків → 3 виправлення → 4 чистих блоки ✅

**`modules/01_script_generator.py`** — 3 зміни:
1. `_FINAL_CTA_MARKER_RE` (новий) — тільки `[CTA_SUBSCRIBE_FINAL]`; замінює `_FINAL_CTA_RE` в chunking loop (рядки 900-905) → більше не спрацьовує на "Thank you for being here" в звичайному outro блоці
2. `_FINAL_CTA_RE` — лишається для post-loop CTA repair/dedup (рядки 930+)
3. Continuation instruction (рядки 852-864): додано CRITICAL-заборону meta-text ("do NOT write 'I need to reassess'", "Start directly with [SECTION N+1]")
4. Інтеграція validator у `generate_scripts()`: `validate_and_fix()` між `model_dump()` та `write_text()` — автоматично фіксує і логує проблеми

### Кореневі причини виправлено
- **Double ending**: `_FINAL_CTA_RE` матчив "Thank you for being here" у regular outro → falsely triggered CTA-strip → continuation → дублікат контент. FIXED: strict marker regex in loop.
- **Meta-text**: Continuation instruction не забороняв LLM писати meta-commentary. FIXED: explicit CRITICAL instruction.
- **Duplicate CTA/practices**: Відсутність post-generation validator. FIXED: `validate_and_fix()` called before save.

### Файли
- `modules/script_validator.py` — NEW
- `modules/01_script_generator.py` — 4 edits (_FINAL_CTA_MARKER_RE, loop fix, continuation instruction, validator integration)

---

## 2026-03-05 — BetaImage Client + Quality Test (5 images)

### Питання
Взяти промпти з останнього сценарію, адаптувати під master rules (New AI), згенерувати та глянути якість

### Виконано
**`clients/betaimage_client.py`** — новий клієнт для csv666.ru image API:
- `POST /api/v2/image/generate` → poll status → `GET /download?format=png`
- Semaphore MAX_CONCURRENT=3, fast mode ~8-21s per image
- Auth: `X-API-Key` (same `VOICEAPI_KEY` env var)
- Interface сумісний з WaveSpeedClient (`generate_text2img()`)
- `aspect_ratio="16:9"` → 1360×768 PNG output

**`tools/test_newai_quality.py`** — тест адаптованих промптів:
- 5 промптів з last script.json (block_001/003/006/008/012)
- Адаптація під master rules: explicit camera angle (low angle 35mm), механічні деталі, atmospheric effects
- FINAL = SCENE BLOCK + IMAGE_STYLE (epic sci-fi concept art, crimson/gold, etc.)
- Паралельна генерація → saved to `test_newai/` в project dir

**Результати генерації:**
- block_001: Staircase (1354 KB, 14s) — кінематографічний силует знизу вгору ✅
- block_003: Clock mechanism (1503 KB, 9.7s) — кристальні шестерні, frozen mid-turn ✅
- block_006: Clockwork opposite gear (1412 KB, 8.1s) — НАЙКРАЩЕ: massive gold+crimson gear ✅
- block_008: Gravitational center (1132 KB, 15.6s) — sci-fi платформа над crimson void ✅
- block_012: Shadow retrieval (1475 KB, 21s) — golden threads, 3 figures ✅

**Спостереження про якість:**
- Стиль: game art / illustrated (не фотореалістичний), але дуже детальний
- Колір: crimson + gold дотримуються відмінно
- Композиція: симетрична, кінематографічна, інструкції слухаються добре
- Механічні елементи: block_006 (clockwork) — просто ідеальний
- Швидкість: 8-21с (паралельно) vs 30-60с у WaveSpeed

### Далі
- Інтеграція betaimage_client в 02_image_generator.py як 3-й backend
- Додати `image_backend` selector в pipeline + UI dropdown
- Оновити style analyzer з 3 варіантами prompts (generic/wavespeed/newai)

---

## 2026-03-05 — End-to-End Test Run (2×31-min videos) + Bug Fixes

### Питання
Повний е2е тест: 2 відео 25-30 хв, субтитри, музика -38dB, custom image style

### Виконано
**Bug fixes (продовження з попередньої сесії):**

**`modules/02b_image_validator.py`** — 3 виправлення:
1. Видалено `result.total += len(newly_skipped)` (double-count — вже в result.scores)
2. Замінено `len(ok_scores)` на `_pre_ok_count = len([s for s in scored_list if s.ok and not s.skipped])` (ok_scores була видалена, NameError)
3. Після regen loop: `result.ok_count = sum(1 for s in result.scores if s.ok and not s.skipped)` — правильний підрахунок після регенерації

**Тест (2 відео):**
- Video 1: "Why Women With Dark Feminine Energy ALWAYS Succeed Alone | Carl Jung"
  - Source: `How To Succeed With Zero Support - Carl Jung`
  - Result: `final.mp4` **530 MB, 31.1 хв** ✅
  - Pipeline: Script(231s) + Images+Voices(548s) + Subs(0s) + Video(2238s) = **52 хв**
- Video 2: "6 RARE Things That Make People Mentally Obsessed With You | Carl Jung"
  - Source: `Make Them Mentally Obsessed With You _ Machiavelli's Dark Psychology`
  - Result: `final.mp4` **512 MB, 31.2 хв** ✅
  - Pipeline: Script(240s) + Images+Voices(681s) + Subs(0s) + Video(2181s) = **53 хв**

**Підтверджено:**
- subtitles.ass + subtitles.srt генеруються та записуються в відео ✅
- Музика -38dB мікшується ✅
- 40 і 44 PNGs відповідно ✅
- image_style прийнятий та використаний ✅
- Review workflow: auto-approve script → auto-approve images ✅

### Рішення
- Path з Unicode `'` (U+2019 APOSTROPHE) в імені папки — POST через Python urllib, не curl
- Backend busy during FFmpeg encode → timeout=30s для polling
- FFmpeg temp files в sys temp (не в project dir) — перевіряти `tempfile.gettempdir()`

---

## 2026-03-05 — Multi-Topic Batch Queue

### Питання
Хочу мати можливість запускати кілька відео з різними темами одночасно.

### Виконано
**5 файлів, нова фіча "Multi-Topic" queue:**

**`backend/models.py`** — 2 нові Pydantic схеми:
- `MultiTopicItem` — одне відео: `source_dir, channel, custom_topic, quality, image_style`
- `MultiBatchRequest` — черга: `items[], parallel, image_style, dry_run, draft, from_step, budget_per_video`

**`backend/job_manager.py`** — 2 нові методи:
- `start_multi_batch(items, parallel)` — створює N Job об'єктів, кожен зі своїм asyncio.Task + семафором
- `_run_with_semaphore(sem, job, ...)` — чекає слот семафора → запускає `_run_pipeline_job()`

**`backend/routes/pipeline.py`** — новий endpoint:
- `POST /api/batch/multi` → `list[JobResponse]` (один JobResponse per video)
- Валідація всіх source_dir перед стартом будь-якого job
- Per-item style overrides global style

**`frontend/src/api.ts`** — нові типи + метод:
- `MultiTopicItem`, `MultiBatchRequest` interfaces
- `api.batch.runMulti(body: MultiBatchRequest) → Job[]`

**`frontend/src/components/JobList.tsx`** — новий таб "Multi-Topic":
- Таб переключений: `'pipeline' | 'batch' | 'multi'`
- Динамічна таблиця рядків: Source dir | Topic override | Channel | Quality | ×
- "+ Додати відео" button + глобальні налаштування: image_style, parallel, dry_run
- Submit: `🚀 Запустити чергу (N відео)` — фіолетова кнопка
- Кожне відео → окремий Job card

### Ключові рішення
- Семафор `asyncio.Semaphore(parallel)` — контроль паралелізму без складних черг
- Кожне відео = окремий Job (не umbrella job) → повний progress/WS/cancel per video
- Валідація source_dir перед стартом будь-якого job → fail fast, не половину запустити
- TypeScript 0 errors, Python syntax OK

---

## 2026-03-05 — Project analysis + image style consistency fix

### Питання
Аналіз проекту (сильні/слабкі сторони) + виправлення 4 проблем:
1. Стилістична консистентність між кадрами
2. Дублікати зображень (відомий баг)
3. Subtitles hardcoded no_subs=True
4. Один канал одночасно

### Результат аудиту
Після перевірки коду виявилось:
- **Дублікати (#2)** → вже виправлено (CONTEXT.md: "немає" відомих багів)
- **Субтитри (#3)** → вже виправлено: `pipeline.py:784` `no_subs=not burn_subtitles`, дефолт `burn_subtitles=True`
- **Batch (#4)** → вже є: `batch_runner.py` + UI вкладка "Batch" + `api.ts BatchRunRequest`

### Виконано
**Image style consistency — per-video seed** (`modules/02_image_generator.py`):
- `import hashlib` — стабільний хеш (не PYTHONHASHSEED)
- `_derive_video_seed(title: str) -> int` — MD5 від заголовку відео → seed в `[0, 2**30)`
- `generate_images()` — новий параметр `video_seed: int | None = None`
  - Якщо `None` → авто-деривація з `script["video_title"]`
  - Логується: `"Image style seed: 123456789 (video: Назва...)"`
- `_generate_one()` — новий параметр `block_seed: int | None = None`
  - Замість `seed = 42` → `seed = block_seed` (per-video seed + block_order)
  - Кожен блок: `block_seed = (video_seed + block["order"]) % 2**30`
  - Ретрай → `seed=None` (випадковий, щоб уникнути пов'язаних провалів)

### Ефект
- Те саме відео → ті самі картинки (відтворюваність)
- Різні відео → різні seed-сім'ї → різна візуальна ідентичність
- Блоки одного відео → суміжні seed → subtle style consistency
- `pipeline.py` не потребує змін (авто-деривація всередині `generate_images`)

### Файли
- `modules/02_image_generator.py` — `_derive_video_seed()` + `video_seed` + `block_seed`

---

## 2026-03-04 — YouTube Channel Branding API (08c_channel_setup.py)

### Питання
Що можна автоматично налаштувати на YouTube каналі через API?

### Відповідь (YouTube Data API v3)
| Параметр | API | Метод |
|----------|-----|-------|
| Назва каналу | ❌ Тільки Studio | — |
| Аватарка | ❌ Тільки Studio | — |
| Опис | ✅ | `channels.update` → `brandingSettings.channel.description` |
| Теги/ключові слова | ✅ | `channels.update` → `brandingSettings.channel.keywords` |
| Країна | ✅ | `channels.update` → `brandingSettings.channel.country` |
| Банер | ✅ | `channelBanners.insert` → URL → `channels.update` |
| Трейлер | ✅ | `channels.update` → `brandingSettings.channel.unsubscribedTrailer` |

### Виконано
- **`modules/08c_channel_setup.py`** — новий модуль:
  - Зчитує `branding` секцію з channel_config.json (або CLI overrides)
  - `_format_keywords()` — multi-word теги автоматично цитуються (world history → "world history")
  - `_upload_banner()` — розмір/формат валідація + `channelBanners.insert`
  - `apply_branding()` — один `channels.update` call (description + keywords + country + trailer + banner URL)
  - `--dry-run` — preview без API calls ✅
- **`config/channels/history.json`** — додано `branding` секцію з description/keywords/country/banner_path/trailer_video_id
- Dry-run тест: channel ID = UCwqSQyUBCocTpV0SPKwNuIg, всі поля відображаються коректно ✅

### Файли
- `modules/08c_channel_setup.py` (новий)
- `config/channels/history.json` (+branding секція)

### CLI
```
python modules/08c_channel_setup.py --channel config/channels/history.json --channel-name main
python modules/08c_channel_setup.py --channel config/channels/history.json --channel-name main --dry-run
```

---

## 2026-03-04 — Research: YouTube Data API v3 Python upload (web search)

### Запит
Веб-пошук + GitHub-аналіз рішень для модуля YouTube upload (module 08).

### Ключові результати
- **Quota cost**: `videos.insert` = **100 units** (знижено з 1600); default 10K units/day → ~100 uploads/day
- **Unverified app (post-2020)**: відео стають `private` автоматично → потрібен API Compliance Audit або Production mode
- **OAuth refresh token**: expires 7 days якщо app у "Testing" mode → fix: змінити на "In production" в Google Console
- **Service accounts**: НЕ працюють з YouTube API → тільки OAuth per-channel
- **Multi-channel**: один `credentials.json` + окремий `token_{channel}.pickle` per channel
- **Async**: `google-api-python-client` не підтримує httpx/async нативно → для async треба реалізувати resumable upload вручну через httpx
- **Scheduling**: `publishAt` ISO8601 UTC + `privacyStatus="private"` → відео публікується автоматично

### Топ бібліотеки
- [tokland/youtube-upload](https://github.com/tokland/youtube-upload) — 2.2K stars, CLI, `--credentials-file` multi-channel, не підтримується з 2020
- [pillargg/youtube-upload](https://github.com/pillargg/youtube-upload) — 108 stars, archived 2023, token-based auth
- [youtube/api-samples](https://github.com/youtube/api-samples) — офіційний Google sample, Python 2.x (застарілий)
- `google-api-python-client` — офіційна lib, sync, достатньо для CLI

### Наступні кроки
- Модуль 08 вже написаний (`modules/08_youtube_uploader.py`) — перевірити що використовує `google-api-python-client` + `InstalledAppFlow` + per-channel token.json
- Перевірити: чи app у "In production" в Google Console (щоб refresh token не вмирав через 7 днів)

---

## 2026-03-04 — Дебаг custom_topic: title fix + папка проекту

### Дебаг live генерації (job a757a478)
- Запущено pipeline step 1 через API з custom_topic='7 Signs a Man Has Reached the Sophia Stage (The Most DANGEROUS Divine Masculine)'
- Знайдено: `__NEW TOPIC__` ПРАВИЛЬНО потрапляє в промпт LLM (`_build_user_prompt` working)
- Проблема 1: script.json title = назва референс-відео → root cause: `_parse_llm_output()` завжди брав `source_data.get("title")`
- Проблема 2: вміст не "7 Signs" структура → template='auto'→'documentary'; для listicle треба template='listicle'

### Виконано
- `modules/01_script_generator.py` — `_parse_llm_output()` додано `custom_topic: str = ""` параметр; `video_title = custom_topic.strip() if custom_topic.strip() else ref_title`; виклик передає `custom_topic=custom_topic`
- Демо: WITHOUT → title=reference video title, WITH → title='7 Signs a Man Has Reached the Sophia Stage...' ✅
- `pipeline.py` + `backend/job_manager.py` — fix назви папки проекту (набуде сили після рестарту)

### Важливо
- `_module_cache` в pipeline.py кешує 01_script_generator.py → потрібен рестарт сервера
- Правильний endpoint для поллінгу: `/api/jobs/{job_id}` (не `/api/pipeline/{job_id}`)

### Наступні кроки
- Рестарт backend сервера → тест Step 1, custom_topic='7 Signs...', template='listicle'
- git commit

---

## 2026-03-04 — Відповіді на 3 питання + fix папки custom_topic

### Питання користувача
1. **Чому не зупинилось на генерації сценарію?** — Transcriber auto-pipeline завжди запускає всі 6 кроків (to_step дефолт=6). Щоб зупинитись на кроці 1 — треба використати Single Video форму і вибрати Steps "1 Script" (from=1, to=1). В Transcriber панелі step range поки відсутній.
2. **Чому назва папки ≠ темі відео?** — `project_dir = ROOT/"projects"/source_dir.name` завжди використовував назву референс-відео. custom_topic впливав тільки на промпт LLM. ВИПРАВЛЕНО (нижче).
3. **На яку тему останній сценарій?** — "The Illusion of Free Will - Carl Jung's Darkest Truth" (тема референс-відео). custom_topic поле ВІДСУТНЄ в script.json → пайплайн запустився через Transcriber auto-pipeline до того як фікс був задеплоєний.

### Fix: назва папки = custom_topic (якщо вказано)
- `pipeline.py` — додано `import re`; блок "Resolve project directory": якщо `custom_topic.strip()` → `folder_name = re.sub(r'[\\/:*?"<>|]', "_", custom_topic.strip())[:200]`, інакше `folder_name = source_dir.name`
- `backend/job_manager.py` — додано `import re`; в `_run_pipeline_job` DB tracking: аналогічна логіка для `project_dir=ROOT / "projects" / _folder`

### Підтверджено що вже реалізовано (план з попередньої сесії)
- `_image_for_segment()` fix — вже в 05_video_compiler.py (pre-compute all assignments + modulo fallback)
- Subtitles checkbox — вже в усіх 5 файлах (models.py, routes/pipeline.py, pipeline.py, api.ts, JobList.tsx)

### Файли
- `pipeline.py`, `backend/job_manager.py`

### Наступні кроки
- git commit
- Тест: запустити з custom_topic → перевірити що папка `projects/7 Signs a Man...` створилась

---

## 2026-03-04 — SILENT ANALYSIS audit + custom_topic field

### Аналіз SILENT ANALYSIS FRAMEWORK
- Фреймворк в master_script_v3.txt добре покриває все що LLM може бачити з тексту (hook mechanics, emotional triggers, retention, audience psychology)
- Gap: LLM не знає чи відео справді зайшло (немає view count). Виправляти не треба — архітектура правильна (8-Block = success framework сам по собі)
- Рекомендація: за бажанням передавати `[VIDEO CONTEXT]: ~{view_count} views` в промпт (потребує yt-dlp fetch)

### custom_topic — UI поле для теми нового відео
- Поле `Тема відео` в UI: якщо заповнене → LLM пише сценарій на нову тему, référence = структурний зразок
- Порожньо → стара поведінка (тема = назва референс-відео + description)
- Зміни в 6 файлах:
  - `modules/01_script_generator.py` — `custom_topic: str = ""` у `_build_user_prompt()`, `_generate_one_variant()`, `generate_scripts()`; hook validation topic = custom_topic ?? source_data.title
  - `pipeline.py` — `custom_topic: str | None = None` в `run_pipeline()`; передається до `generate_scripts()`
  - `backend/models.py` — `custom_topic: str | None = Field(None, ...)`
  - `backend/routes/pipeline.py` — `custom_topic=req.custom_topic` в `manager.start_pipeline()`
  - `frontend/src/api.ts` — `custom_topic?: string | null` в `PipelineRunRequest`
  - `frontend/src/components/JobList.tsx` — `custom_topic: string` в PFormState + UI input після Source dir + payload cleanup

### Наступні кроки
- git commit
- Тест: запустити pipeline з заповненим полем "Тема відео" → перевірити що `__NEW TOPIC__` в промпті = custom_topic (а не title з metadata.json)

---

## 2026-03-03 — Fix image preview gray boxes in review UI

### Причина (2 проблеми)
1. **Vite proxy** не проксував `/projects` на порт 8000 → `<img src="/projects/...">` в dev mode (порт 5173) = 404 сірий блок
2. **pipeline.py** лінія 622: URL не був закодований (`urllib.parse.quote`) — пробіли, em-dash в назві проекту → Starlette не міг знайти файл. Також для secondary images (index > 0) формувався неправильний filename (`block_001.png` замість `block_001_1.png`)

### Виконано
- `frontend/vite.config.ts` — додано `'/projects': 'http://localhost:8000'` до proxy
- `pipeline.py` — `from urllib.parse import quote as _url_quote` + `_enc_name = _url_quote(_src_name, safe="")` + правильний `_fname` для image_index > 0

### Файли
- `frontend/vite.config.ts`, `pipeline.py`

---

## 2026-03-03 — Fix duplicate images + subtitles checkbox

### Виконано
- `modules/05_video_compiler.py` — `_image_for_segment()` повністю перероблено:
  - Попередній фікс (`seg_idx * n // n_segments`) сам давав consecutive duplicates (n=2, n_segs=3 → [0,0,1])
  - **Новий підхід:** обчислюємо word_offset assignments для ВСІХ сегментів одразу → якщо кожне зображення з'являється хоча б раз — використовуємо word_offsets; якщо ні — modulo fallback (`seg_idx % n`, гарантує чергування без consecutive duplicates)
  - Виправлено typo рядок 477: `_get_block_image` → `_get_block_images`
- Subtitles checkbox (5 файлів): `backend/models.py`, `backend/routes/pipeline.py`, `pipeline.py` (no_subs=not burn_subtitles), `frontend/src/api.ts`, `frontend/src/components/JobList.tsx`

### Рішення
- word_offsets від LLM часто розміщують другу картинку пізніше середини → floor-division fallback давав [0,0,1] для 3 сегментів
- modulo (`seg_idx % n`) гарантує [0,1,0] — ніколи немає consecutive duplicates

---

## 2026-03-03 — ROOT CAUSE FIX: Python 3.13 dataclasses + sys.modules

### Причина (знайдено)
- Python 3.13 змінив `dataclasses._is_type` → `ns = sys.modules.get(cls.__module__).__dict__` без None-guard
- `_load_module` в pipeline.py завантажував модулі через `importlib.util.exec_module` БЕЗ реєстрації в `sys.modules`
- При виконанні `@dataclass` декоратора → Python 3.13 шукав модуль в `sys.modules` → `None` → `None.__dict__` → `AttributeError`
- Виникало при першому ж завантаженні 02b/01b — до будь-якого API виклику

### Виконано
- **`pipeline.py` `_load_module`**: додано `sys.modules[spec.name] = mod` ДО `spec.loader.exec_module(mod)` + cleanup при exception
- **`log.warning` → `log.exception`**: лишається — корисно для дебагу
- **Тест**: 02b на Nietzsche-проекті (30 images) → `total=31, ok=30, skipped=1, regen=0, elapsed=59s` ✓

### Файли
- `pipeline.py` — _load_module sys.modules fix + log.exception

---

## 2026-03-03 — WaveSpeed "money burned" fix: disable inline validation + global fallback

### Виконано
- **`pipeline.py`**: додано `validate=False` до виклику `generate_images()` — вимкнено inline validation в `02_image_generator.py`. Тепер генеруємо рівно **1 WaveSpeed запит per block**. Вся валідація + regeneration — тільки через `02b_image_validator.py` після генерації. Було: до 3×/block = до 111 WaveSpeed задач на 37 блоків.
- **`02_image_generator.py`**: новий параметр `wavespeed_globally_failed: list[bool]` — shared mutable flag між паралельними корутинами. Як тільки будь-який блок отримує WaveSpeed API error → прапор = True → всі наступні блоки одразу переходять на VoidAI **без спроби WaveSpeed**. Запобігає 37 failed tasks при зламаному endpoint (як в першому тесті на скрішоті — всі failed + charged).

### Причина проблеми (WaveSpeed History скрін)
- Перший тест: 37 WaveSpeed задач — всі "failed", outputs "--", але гроші списались. Endpoint `/wavespeed-ai/z-image/turbo` тепер потребує audio → API приймав задачу, вона падала під час обробки → charge без результату
- Inline validation у `02_image_generator.py` при fail → ретрай з новим WaveSpeed запитом → ще charge. До 3× per block при зламаному endpoint або поганому MATCH score

### Файли
- `pipeline.py` — `validate=False` в `generate_images()` call
- `modules/02_image_generator.py` — `wavespeed_globally_failed` global flag + updated signature

---

## 2026-03-03 — FFmpeg concat fix + image validation UI + WaveSpeed integrity checks

### Виконано
- **FFmpeg concat path fix** (CRITICAL): `Path.resolve()` повертає backslashes на Windows → concat demuxer падав з exit 4294967294. Виправлено на `.resolve().as_posix()` у двох місцях: `concat_audio` (audio) і `concat_video` fast path (video). Тепер форвард-слеші — FFmpeg їх правильно обробляє.
- **Image validation UI** (JobCard.tsx): нова секція зі списком `✗ Не пройшли валідацію (N)` — кожен рядок має score badge, block label, reason (видима, не hover), `×N спроб`. Expandable → показує `💡 improved_prompt`. Також секція `⚠ Пропущено`. `02b_image_validator.py` → `to_dict()` тепер включає `improved_prompt`. Колонка "якість" отримала підпис "поріг: 7/10".
- **WaveSpeed endpoint fix in 02b** (CRITICAL): `02b_image_validator.py` використовував старий `/wavespeed-ai/z-image/turbo` (deprecated, тепер потребує audio). Виправлено на `/wavespeed-ai/flux-dev-ultra-fast` з новим payload (`num_inference_steps: 28, guidance_scale: 3.5, output_format: png, enable_sync_mode: True`). Оновлена логіка sync/async response та download з перевіркою розміру файлу.
- **Download verification** (`wavespeed_client.py`): після `write_bytes` перевіряємо що файл > 5 KB. Якщо менше — видаляємо і кидаємо RuntimeError щоб caller знав що download фактично провалився (захист від "гроші списані, файл порожній").
- **Retry best-image tracking** (`02_image_generator.py`): на retry генеруємо до `attempt_path` замість `out_path`. Зберігаємо `best_score/best_path`. Overwrite `out_path` тільки якщо новий score вищий. Це запобігає ситуації "WaveSpeed зарядив за 3 спроби але фінальний файл гірший за перший".
- **TS type fix** (JobList.tsx): `PFormState` не відповідала `PipelineSettings` через optional fields → додані явні overrides для `channel/quality/template/draft/dry_run/background_music/skip_thumbnail/image_style/voice_id/duration_min/duration_max/master_prompt`.

### Файли
- `utils/ffmpeg_utils.py` — as_posix() fix (2 рядки)
- `frontend/src/components/JobCard.tsx` — image validation UI rewrite
- `frontend/src/components/JobList.tsx` — PFormState TS fix
- `modules/02b_image_validator.py` — WaveSpeed endpoint fix + to_dict improved_prompt
- `clients/wavespeed_client.py` — download size verification
- `modules/02_image_generator.py` — retry best-image tracking

### Рішення
- FFmpeg concat backslash: `as_posix()` vs `str()` — стандартне рішення для Windows paths у Unix-стилі CLI tools
- WaveSpeed endpoint: обидва `02_image_generator.py` (via client) і `02b_image_validator.py` (inline HTTP) тепер використовують `flux-dev-ultra-fast`

---

## 2026-03-03 — image_style UI-only + hook detect fix + image count fix

### Виконано
- **image_style = UI-only** (no channel_config fallback):
  - `01_script_generator.py`: `generate_scripts/generate_one_variant/_build_user_prompt/_parse_llm_output` — всі приймають `image_style: str = ""` замість `channel_config.get("image_style", "")`
  - `02_image_generator.py` line 337: `image_style = image_style or ""` (was: channel_config fallback)
  - `pipeline.py`: валідація `if not (image_style or "").strip(): raise ValueError(...)` + `image_style=image_style or ""` у виклику `generate_scripts`
  - `JobList.tsx`: `submitPipeline` блокує відправку якщо `pForm.image_style.trim() === ""`
- **has_hook детектор** (pipeline.py review card): тепер перевіряє `hook.validation_score` зі script.json замість вузького keyword matching — v3 хуки ("You've been told...", "Most people...") тепер правильно детектуються
- **Image count**: `~N images` → `MINIMUM N images` + новий ⚠️ рядок з total minimum у `_build_user_prompt`

### Файли
- `modules/01_script_generator.py` — image_style chain + MINIMUM images
- `modules/02_image_generator.py` — remove channel_config fallback
- `pipeline.py` — image_style validation + has_hook fix
- `frontend/src/components/JobList.tsx` — image_style required validation

### Далі
- Запустити тест з новими параметрами, перевірити hook score в review card

---

## 2026-03-03 — №45 smooth zoom scale+crop + plan verification

### Виконано
- **Відповідь на питання "чи картинки відповідають нарації"**: Так — image_prompts генеруються LLM разом зі сценарієм і точно ілюструють конкретні метафори (напр. "4 gates" → 4 кам'яні арки, Jung Red Book → сторінки Red Book + Юнг у Цюриху)
- **Заміна zoompan → scale+crop** для zoom_in/zoom_out у `ffmpeg_utils.py`:
  - zoompan: integer crop rounding → 1.67px/frame → alternates 1-2px → 60% variation → stutter
  - scale+crop (eval=frame): floating-point per-frame → CapCut-style smooth → ZOOM_SCALE=1.30 (30%)
  - max(W,...) guard: prevents underflow at last frame
  - pan_left/pan_right залишилися на zoompan (вже плавні)
- **Тест**: zoom_in 10s + zoom_out 10s → обидва OK без помилок
- **Перевірено план** duration range control: всі 6 файлів вже реалізовані раніше
  - 01_script_generator.py ✓ (duration_min/max, CLI --duration-min/max, --duration legacy)
  - 01b_script_validator.py ✓ (dynamic eff_too_long/eff_too_short thresholds)
  - pipeline.py ✓ (duration_min/max params + CLI args)
  - backend/models.py ✓ (PipelineRunRequest fields)
  - frontend/api.ts ✓ (interface fields)
  - frontend/src/components/JobList.tsx ✓ (UI з двома inputs + word count hint)

### Файли
- `utils/ffmpeg_utils.py` — scale+crop zoom implementation

### Коміти
- `22bc84b` feat: replace zoompan zoom with scale+crop for smooth Ken Burns

---

## 2026-03-03 — №44 Pipeline Steps 03-05 + animation fix

### Виконано
- **Step 03 TTS** (`03_voice_generator.py`): 17/17 блоків озвучено VoiceAPI, 25.8 хв нарації, EBU R128 нормалізація, 178.9s
- **Step 04 Subtitles**: 379 записів, SRT + ASS формати
- **Step 05 Video** (`05_video_compiler.py`): 1920x1080, crossfade, BGM -20dB, субтитри, final.mp4 476.5MB, 1469.8s
- **Animation fix**: `_KB_CYCLE = ["zoom_in", "zoom_out"]` (прибрано `pan_left`/`pan_right` — давали ривки при block transitions)
- `anim_demos/` — 5 тестових кліпів по 10s для порівняння анімацій

### Коміти
- `925c492` fix: remove pan_left/pan_right from animation cycle

---

## 2026-03-03 — №43 image_style injection in LLM prompt

### Проблема
`image_style` з channel_config (`oil painting, baroque architecture...`) не передавався у LLM при генерації. Opus генерував промпти зі своїм дефолтом ("cinematic photorealism" з прикладів master prompt). Стиль зберігався у script.json але тільки для downstream modules (image gen, validator).

### Fix — 3 файли

**`modules/01_script_generator.py`** — `_build_user_prompt()`:
- Додано `image_style_line` що інжектується після `[TARGET WORDS]`:
  `[IMAGE STYLE] — Apply to EVERY [IMAGE_PROMPT:] tag (replace the 'Style' element):`
  `{image_style}`
- Якщо `image_style` порожній — рядок не додається (backward compat)

**`modules/01b_script_validator.py`**:
- `_fix_bad_prompts()`: додано `image_style` параметр → передається у LLM prompt
- `validate_and_fix_script()`: `image_style` тепер fallback на `script["channel_config"]["image_style"]`
  якщо `channel_config` param не переданий (CLI виклик без конфігу)

**`config/channels/history.json`**:
- `image_style` оновлено до: `"oil painting, epic classical art style, baroque architecture, warm golden amber palette, dramatic cinematic lighting, backlit silhouette, ornate decorative details, grand interior columns, painterly brushstrokes, renaissance fine art, rich warm tones orange and gold, epic scale composition, atmospheric depth, museum quality illustration"`

### Перевірка
- `_build_user_prompt()` unit test: рядок 7 = `[IMAGE STYLE] — Apply to EVERY...`, рядок 8 = повний стиль ✅
- `history.json` → `image_style` ≠ "cinematic photorealistic" ✅

### Файли: `modules/01_script_generator.py`, `modules/01b_script_validator.py`, `config/channels/history.json` (commit 2fcd246)

---

## 2026-03-03 — №42 sparse_images detection + auto-fix in script validator

### Проблема
LLM (Opus) тримає щільність зображень у перших 2 блоках, потім "забуває":
- Block 3 (490w): 1 img замість ~3 | Block 7 (523w): 1 img замість ~3
- Validator не бачив проблему: `image_prompt` (single) non-empty → OK, але `image_prompts` (list) = 1 елемент

### Fix — `modules/01b_script_validator.py`
- **`_structural_checks()`**: новий `sparse_images` check
  - Для блоків type != cta/outro з narration >= 200 слів
  - `min_expected = max(1, nw // 150)` (1 img per 150w мінімум)
  - Якщо `actual < min_expected` → ScriptIssue(type="sparse_images", severity="warning")
- **`_fix_sparse_images()`**: batch LLM call (gpt-4.1-mini)
  - Отримує список sparse blocks з `_need` (скільки додати)
  - Генерує нові prompts що покривають різні моменти narration
  - Не замінює існуючі, а доповнює
- **`validate_and_fix_script()`**: Fix 3 після bad_prompt
  - Розраховує evenly-distributed word offsets для нових промптів
  - Оновлює `image_prompts`, `image_word_offsets`, `image_prompt` (if empty)

### Тест ("Tired" script, після генерації)
- Виявлено: 3 sparse blocks (490w/1img, 523w/1img, 275w/0img)
- Авто-виправлено: +5 промптів, 29 → 34 imgs total
- Всі блоки тепер OK за порогом 1img/150w

### Файли: `modules/01b_script_validator.py` (commit c4ea202)

---

## 2026-03-03 — №41 Hook regen 5+5 strategy + TTS limit 35 + test on Tired

### Зміни

**`modules/01b_script_validator.py`** — `TTS_MAX_SENTENCE_WORDS = 30 → 35`

**`modules/01_script_generator.py`** — 5+5 hook regen strategy:
- `MAX_INTRO_REGEN = 2` → `MAX_HOOK_ATTEMPTS = 10` + `HOOK_ROUND_SIZE = 5`
- Loop тепер колекціонує `candidates: list[tuple[str, int, HookInfo]]`
- Після спроби 5 (round 1): якщо best ≥3/4 → використовуємо найкращий і виходимо
- Якщо нема ≥3/4 → round 2 (спроби 6-10)
- Після спроби 10: best available незалежно від score
- Ранній вихід при ≥4/4 як завжди

### Тест: "Why You're Always Tired (Even When You Do Nothing)" (Opus, 22-25 хв)
- Hook passed **4/4** на спробі 1 ✅
- 10 блоків, **3358 narration words** (target 3080–3750) ✅
- Validator: 1 warning (missing prompt block_009 → auto-fixed) ✅
- 29 image prompts (деякі секції sparse — нормально для 1 chunk)
- Cost: $0.64 (Opus chunk) + $0.003 (hook validate)

### Файли: `modules/01_script_generator.py`, `modules/01b_script_validator.py`

---

## 2026-03-03 — №40 Accurate narration word counting + v3 script generation fixes

### Root problem
`_call_llm()` рахував total LLM output words (narration + image prompts + headers) замість narration-only.
Image prompts для 40 img × 35 слів ≈ 1400 зайвих слів — фактичні відео виходили ~19-21 хв замість 22-25 хв.
Старий хак `_IMG_OVERHEAD = 1.50` був неточним (реальний overhead = 42.7% в тест-зразку).

### Fix: `_count_narration_words()` — `modules/01_script_generator.py`
```python
_NARRATION_STRIP_RE = re.compile(
    r'\[IMAGE_PROMPT:.*?\]|\[SECTION\s+\d+[^\]]*\]|\[CTA_SUBSCRIBE[^\]]*\]',
    re.IGNORECASE | re.DOTALL,
)
def _count_narration_words(text: str) -> int: ...
```
- Стрипить IMAGE_PROMPT, SECTION headers, CTA markers перед підрахунком
- Unit test: 234 total words → 134 narration words (42.7% overhead stripped) ✓

### Оновлено `_call_llm()`:
- **Видалено** `_IMG_OVERHEAD = 1.50` (хак)
- `word_budget = int(duration_max * 150 * 1.15)` — narration-only, +15% headroom
- `min_words_for_cta = int(duration_min * 140)` — narration-only floor, 100% (без haircut)
- `current_words` → `_count_narration_words(full_output)` (в loop guard)
- `total_words` → `narration_words = _count_narration_words(full_output)` (post-chunk log + CTA check)
- `remaining_tokens` multiplier: `1.4 → 2.1` (narration → total: ÷0.66, then ×1.4 ≈ ×2.1)
- Усі log messages: "words" → "narration words"

### Також в цій сесії (Run 1–4 під час попереднього контексту)
- **Run 1**: CTA truncated — `tokens_first_chunk 1.4→2.0` multiplier fix
- **Run 2+**: CTA repair call (500 tokens) якщо CTA < 80 слів і без terminal punctuation
- **Run 1-3**: Hook validator замінював хороший Opus hook гіршим → `hook_pass_threshold=2` для v3
- `history.json` + `example_history.json`: `mistral-small-latest→deepseek-v3.1`, `gemma-3n-e4b-it→gpt-4.1-nano`

### Числа для перевірки (25 хв):
- `word_budget = int(25*150*1.15) = 4312` narration words
- `min_words_for_cta = int(22*140) = 3080` narration words
- `tokens_first_chunk = min(8000, int(25*150*2.0)) = 7500` tokens (1 chunk ✓)

### Числа для 40 хв:
- `word_budget = int(40*150*1.15) = 6900` narration words
- `min_words_for_cta = int(35*140) = 4900` narration words
- `tokens_first_chunk = min(8000, int(40*150*2.0)) = 8000` → потрібно 2-3 chunks (MAX_SCRIPT_CHUNKS=5 ✓)

### Файли: `modules/01_script_generator.py`

---

## 2026-03-03 — №39 master_script_v3.txt + v3 block architecture in code

### Що зроблено
**1. `prompts/master_script_v3.txt`** — новий системний промпт (замінює v2):
- 8 фіксованих наративних блоків із % вагами (HOOK 4% → TENSION 10% → ROOT CAUSE 12% → RECOGNITION 16% → MID-CTA → FRAMEWORK 20% → TURN 14% → PRACTICE 14% → CLOSING 10%)
- TTS writing guidelines — max 25 слів/речення, em-dash, заборона `!`, `(...)`, `...`, `%/#/&`
- Image prompt formula: Subject + Emotional State + Lighting + Style + Composition
- IMAGE-NARRATION lock rule: кожен промпт ілюструє конкретне оточуюче речення
- Block completion rule: кожен блок завершується transition sentence
- 4-tier image density model (з v2, уточнено з прикладами помилок)
- ACTIVATION секція показує що буде ін'єктовано кодом

**2. `modules/01_script_generator.py`** — нові константи та функції:
- `BLOCK_STRUCTURE_V3` — список 8 блоків з `pct`
- `_calc_images_for_block(start_word, block_words)` — 4-tier розрахунок кількості img на блок
- `_calc_block_targets(duration_min, duration_max)` — повний список `{name, words_min, words_max, images}`
- `_build_user_prompt()` — детектує "v3" в `master_prompt_path`, ін'єктує `[BLOCK WORD TARGETS]` + `[BLOCK IMAGE TARGETS]`
- **Bug fix**: `_call_llm()` мав `duration_min` у тілі але не в сигнатурі → додано `duration_min: int = 8` параметр + передача у виклику

**3. `config/channels/history.json`** — `master_prompt_path` → `"prompts/master_script_v3.txt"`

### Перевірка математики (22-25 хв)
- Block targets: 3078-3750 слів, ~40 images total
- Tier 1 (0-450w): Block 1+2 → щільні 5+13 img ✓
- Tier 2-3 (450-2250w): Block 3-5 → 8+4+5 img ✓
- Tier 4 (2250+w): Block 6-8 → 2+2+1 img ✓

### Файли: `prompts/master_script_v3.txt`, `modules/01_script_generator.py`, `config/channels/history.json`

---

## 2026-03-02 — №38 Root cause fixes: duration/cut-off/animation/hook validator

### Root causes identified та виправлені

**Bug 1 🔴 Duration (15 хв замість 22–25 хв) — два баги:**
- `MAX_TOKENS_PER_CHUNK=2500` cap → `tokens_first_chunk=2500` для 25 хв = 1785 слів ≈ 11.9 хв; LLM писав повний сценарій у першому чанку
- Не було MINIMUM floor — `⚠️ HARD WORD LIMIT` давав тільки стелю, LLM міг писати CTA коли захоче
- Fix: `MAX_FIRST_CHUNK_TOKENS=8000`, `tokens_first_chunk=min(8000, duration_max*150*1.4)` → для 25 хв=5250 токенів≈3750 слів; `min_words_for_cta=int(duration_min*140*0.9)` + CTA stripping; `_build_user_prompt()` — MINIMUM + MAXIMUM

**Bug 2 🔴 Cut-off block_006 — continuation "start Section N+1" кидав незавершений блок:**
- `is_cut_off = not any(c in last_chars[-8:] for c in ".!?\"'")` — детектить обрізану відповідь
- Cut-off mode: "Complete the interrupted sentence first, then continue" (не "start Section N+1")

**Bug 3 🟡 Animation = zoom_in для всіх блоків:**
- `flush()`: `_SECTION_ANIMS = ["pan_left","pan_right","zoom_in","zoom_out"]`; intro→zoom_in, cta/outro→zoom_out, sections rotate `(order-1)%4`

**Bug 4 🟡 Hook score=2 — валідатор оцінював проти niche="history", не теми:**
- `_validate_intro_hook(... topic=title)` — передаємо реальний заголовок
- `hook_validator.txt`: `Video topic: {topic}` в INPUT; criterion 1 — `"The SPECIFIC video topic ("{topic}") is clear"`

### Файли: `modules/01_script_generator.py`, `prompts/hook_validator.txt`

---

## 2026-03-02 — №37 `to_step` parameter + step-by-step testing workflow (commit 1730894)

### `to_step` — зупинка пайплайну після конкретного кроку
- `pipeline.py`: `to_step: int = TOTAL_STEPS` параметр; всі 7 step-gate умов: `if from_step <= STEP_X and to_step >= STEP_X:`; log при `to_step < TOTAL_STEPS`; `--to-step` CLI arg
- `backend/models.py`: `to_step: int = Field(6, ge=1, le=6, ...)`
- `backend/routes/pipeline.py`: `to_step=req.to_step` передається в `manager.start_pipeline()`
- `frontend/src/api.ts`: `to_step?: number` у `PipelineRunRequest`
- `frontend/src/components/JobList.tsx`: quick-preset кнопки [All | 1 Script | 2 Images | 4 Video | 5 Thumb | 6 Meta] + manual from/to dropdowns з mutual clamping

### Сесія завершена: контекст переповнився
- Контекст відновлено → перевірено всі 6 файлів плану duration range control → **всі вже реалізовані ✅**
- CONTEXT.md + session_log.md оновлені

### Файли: `pipeline.py`, `backend/models.py`, `backend/routes/pipeline.py`, `frontend/src/api.ts`, `frontend/src/components/JobList.tsx`

---

## 2026-03-02 — №36 Critical pipeline quality fixes (commit d15f5f4)

### Діагноз реального проекту "Become Who You Are Afraid to Be / Carl Jung"
- Total words: 2118 → 15.4 min замість 8-12 (duration ігнорується)
- Blocks: 11, images per long block: 1-3, audio_duration block_005: 173s
- Всі картинки циклювались по 10s незалежно від тексту → повтори + невідповідність

### Fix 1+3+4 — Image-narration sync (modules/01_script_generator.py + 05_video_compiler.py)
- **Parser**: при зустрічі `[IMAGE_PROMPT:]` рахуємо `_narration_word_count` і зберігаємо у `image_word_offsets: list[int]`; `ScriptBlock.image_word_offsets` → script.json
- **Compiler**: нова функція `_image_for_segment(image_list, word_offsets, total_words, seg_idx, n_segments)` — `seg_word_pos = (seg_idx/n_segments)*total_words` → знаходить image для цієї позиції; fallback до cycling якщо offsets відсутні (v1 scripts)

### Fix 5 — Duration control (modules/01_script_generator.py)
- `_call_llm(duration_max)`: `word_budget = duration_max*150*1.15`; `tokens_first_chunk = min(MAX_TOKENS_PER_CHUNK, duration_max*150*1.4*0.9)`; guard `chunk_num > 1 and current_words >= word_budget → break`; continuation prompt includes `remaining_words`
- `_build_user_prompt()`: `⚠️ HARD WORD LIMIT: {max_words} words maximum`

### Fix 6 — Smooth Ken Burns (utils/ffmpeg_utils.py)
- `scale={w*2}:{h*2}` перед zoompan (замість `{w}:{h}`) → iw=3840 → 0.84px/frame → rounded 1px → 0.5px apparent в 1080p → smooth

### Fix bonus — Skip thumbnail (pipeline.py + backend + frontend)
- `pipeline.run_pipeline(skip_thumbnail=False)`: `if skip_thumbnail: log + emit; elif ...`
- `PipelineRunRequest` + `TranscribeRequest`: поле `skip_thumbnail: bool = False`
- `backend/routes/pipeline.py + transcriber.py`: pass-through
- `frontend/src/api.ts`: `PipelineRunRequest + TranscribeRequest` interface
- `JobList.tsx + TranscriberPanel.tsx`: checkbox "Skip thumbnail"

### Файли: `utils/ffmpeg_utils.py`, `modules/01_script_generator.py`, `modules/05_video_compiler.py`, `pipeline.py`, `backend/models.py`, `backend/routes/pipeline.py`, `backend/routes/transcriber.py`, `frontend/src/api.ts`, `frontend/src/components/JobList.tsx`, `frontend/src/components/TranscriberPanel.tsx`

---

## 2026-03-02 — №35 CLI/Batch duration gaps fixed

### Gap 5 — pipeline.py CLI missing --duration-min/max
- Fix: `_build_parser()` додані `--duration-min/max/--duration`; `main()` передає до `run_pipeline()`

### Gap 6 — batch_runner.py: duration через весь ланцюг відсутній
- Fix: `_process_one(duration_min, duration_max)` → `run_pipeline()`; `run_batch(duration_min, duration_max)` → `_process_one()`; CLI args → `run_batch()`

### Файли: `pipeline.py`, `batch_runner.py`

---

## 2026-03-02 — №34 Pipeline param passthrough audit (4 gaps fixed)

### Gaps знайдені та виправлені:

**Gap 1 🔴 voice_id not wired (pipeline.py → generate_voices)**
- `generate_voices()` не мав `voice_id_override` — голос UI ігнорувався (був `# TODO`)
- Fix: `modules/03_voice_generator.py` — `voice_id_override: str | None = None` + `voice_id = voice_id_override or _get_voice_id(...)`
- Fix: `pipeline.py` — `voice_id_override=voice_id or None`

**Gap 2 🔴 master_prompt not wired (pipeline.py → generate_scripts)**
- `generate_scripts()` не мав `master_prompt_path` — prompt selector UI ігнорувався
- Fix: `modules/01_script_generator.py` — `master_prompt_path: str | None = None` + inject into `channel_config`
- Fix: `pipeline.py` — `master_prompt_path=master_prompt or None`
- Chain: `generate_scripts → channel_config override → _generate_one_variant → _build_system_prompt → _load_master_prompt` ✅

**Gap 3 🟡 background_music default mismatch**
- `PipelineRunRequest.background_music = True` vs `TranscribeRequest.background_music = False`
- Auto-pipeline без музики; Fix: `backend/routes/transcriber.py` → `True`

**Gap 4 🟢 no_ken_burns missing from UI** — без fix (minor, дефолт False = Ken Burns ON = правильно)

### Файли:
- `modules/03_voice_generator.py`, `modules/01_script_generator.py`, `pipeline.py`, `backend/routes/transcriber.py`

---

## 2026-03-02 — №33 Auto-pipeline duration gap fix

### Проблема
TranscriberPanel з увімкненим auto-pipeline запускав pipeline без `duration_min`/`duration_max` → завжди дефолт 8–12 хв.

### Зміни
- **`backend/routes/transcriber.py`**: `TranscribeRequest` — додані `duration_min: int | None` і `duration_max: int | None`; передаються в `pipeline_kwargs` (з дефолтами 8/12)
- **`frontend/src/api.ts`**: `TranscribeRequest` interface — додані `duration_min?`, `duration_max?`
- **`frontend/src/components/TranscriberPanel.tsx`**: state `durationMin=8`, `durationMax=12`; передаються в `api.transcribe.start()`; UI блок "Тривалість (хв)" з'являється тільки коли `autoPipeline=true`

### Поведінка по режимах
- **Auto-pipeline OFF**: source_dir → main pipeline form → всі налаштування з форми ✅
- **Auto-pipeline ON**: duration_min/max з TranscriberPanel → pipeline ✅

---

## 2026-03-02 — №32 Prompt v2: section/IMAGE_PROMPT separation + all 4 pipeline fixes

### prompts/master_script_v2.txt (final state)
- SECTIONS і IMAGE_PROMPTs оголошені як 2 НЕЗАЛЕЖНІ структури
- [SECTION N: Title] = наративна одиниця, довжина content-driven (80-400 слів), НЕ прив'язана до tier
- [IMAGE_PROMPT:] = inline візуальний тег, розміщується всередині narration по word-count triggers:
  - Tier 1 (0–3 хв, ~0–450 слів): кожні ~25 слів (~10с)
  - Tier 2 (3–6 хв, ~450–900 слів): кожні ~50 слів (~20с)
  - Tier 3 (6–15 хв, ~900–2250 слів): кожні ~150 слів (~60с)
  - Tier 4 (15+ хв, ~2250+ слів): кожні ~280 слів (~2 хв)
- Додано приклад: одна секція містить кілька IMAGE_PROMPTs (Tier 1) або один (Tier 3)
- Quality checklist: "IMAGE_PROMPTs placed inline at word-count intervals"
- "NOTE: These intervals are WORD-COUNT triggers, not section triggers"
- config/channels/history.json: master_script_v1.txt → master_script_v2.txt

### Всі 4 баги виправлено (готово до full pipeline test):
1. ✅ Неправильна кількість картинок → v2 промпт генерує 31-49 IMAGE_PROMPTs (не 8 блоків)
2. ✅ Тривалість відео → calibration table в v2 + [TARGET WORDS] в user prompt
3. ✅ Рвана анімація → zoompan d=1:fps=30, -r 30 на input, без dynamic crop w/h
4. ✅ Музика відсутня → libmp3lame для .mp3 output в mix_audio()

**Commits:** `87e1a30` freq tiers, `f6224c9` Ken Burns+music, `ba966d0` v2 config, `ae48526` prompt fix, `4143c6c` section/image separation

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

## 2026-03-05 — Topic-only mode + Multi-Topic повні налаштування + localStorage

### Зроблено
- **Topic-only mode** (source_dir=None): `01_script_generator.py` приймає `None`, будує синтетичний `source_data`; `pipeline.py` — `proj` з `custom_topic` коли немає `source_dir`
- **backend/models.py**: `MultiBatchRequest` отримав усі поля як у Single Video — `template`, `to_step`, `voice_id`, `music_track`, `no_ken_burns`, `duration_min/max`, `background_music`, `music_volume`, `burn_subtitles`, `skip_thumbnail`, `master_prompt`
- **backend/routes/pipeline.py**: всі нові kwargs передаються пайплайну
- **frontend JobList.tsx**: Multi-Topic UI тепер ідентичний Single Video — Template, Steps (preset кнопки + from/to), Duration, Master prompt dropdown, Voice dropdown, Image style, Budget, Draft/Dry run/Music+volume+track/Subtitles/Skip thumb/No Ken Burns
- **localStorage persistence**: `lsGet`/`lsSet` helpers, `LS_MULTI_ITEMS` + `LS_MULTI_SETTINGS`, `useEffect` auto-save при кожній зміні — після F5 всі поля відновлюються
- **405 fix**: причина — сталі `.pyc` файли у `__pycache__`. Вирішення: `Remove-Item __pycache__` + kill python + restart

### Файли
`modules/01_script_generator.py`, `pipeline.py`, `backend/models.py`, `backend/routes/pipeline.py`, `backend/job_manager.py`, `frontend/src/api.ts`, `frontend/src/components/JobList.tsx`

### Рішення
- WatchFiles не перезавантажував через те що `.pyc` були свіжіші ніж source (Power Shell підтвердив що `pipeline.cpython-313.pyc` та `models.cpython-313.pyc` новіші за source)
- HMR Vite зберігає React state при зміні файлу — localStorage persistence додавалась без втрати введених даних

### Далі
- git commit, тест Multi-Topic черги з реальними темами

---

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

---

## 2026-03-05 — Сесія: image_backend/vision_model, BetaTest fix, очищення проектів, cost fix

### Зроблено
- **02_image_generator.py**: Виправлено `_run_coros()` — замість невизначеної `wavespeed` змінної тепер backend-aware `async with` блок (PrimaryClient → betaimage/wavespeed, або тільки voidai)
- **02b_image_validator.py**: Додано `vision_model` параметр до `_score_image()` і `validate_and_fix_images()`, pass-through до обох scoring calls
- **backend/models.py + routes/pipeline.py + api.ts + JobList.tsx**: `image_backend` та `vision_model` додані для Single Video І Multi-Topic (повний full-stack)
- **clients/betaimage_client.py**: Виправлено `prompt_upsampling` формат (`.lower()` → 500 error; тепер `str(True/False)` без lower); дефолт `mode="quality"`, `prompt_upsampling=True`
- **Очищення проектів**: Видалено images/, audio/, script.json, llm_raw_output.txt з 9 проектних папок; звільнено 471 MB
- **Orphaned jobs**: DB UPDATE cancelled 14 orphанних jobs, force kill PID 88676
- **pipeline.py cost fix**: Голос `n_chars * 0.0002` (ElevenLabs rate) → `n_chars * 0.0000038` (VoiceAPI: $19/5M chars); dry-run estimate теж виправлено
- **utils/db.py**: Новий метод `cancel_orphaned_jobs()` — UPDATE running/waiting_review/queued → cancelled
- **backend/main.py**: Lifespan тепер викликає `cancel_orphaned_jobs()` при старті — auto-fix orphaned jobs після перезапуску бекенду

### Файли
`modules/02_image_generator.py`, `modules/02b_image_validator.py`, `backend/models.py`, `backend/routes/pipeline.py`, `frontend/src/api.ts`, `frontend/src/components/JobList.tsx`, `clients/betaimage_client.py`, `pipeline.py`, `utils/db.py`, `backend/main.py`

### Рішення
- BetaTest API: `str(False)` = `'False'` (OK) vs `str(False).lower()` = `'false'` (500 error) — API вимагає Python-style True/False
- Orphaned jobs pattern: backend restart очищує in-memory dict, але DB залишається `running`; тепер виправляється автоматично при наступному старті
- VoiceAPI реальна ціна 52.6x дешевше ElevenLabs — старий rate давав завищені cost estimates

### Далі
- Рестарт бекенду (VideoForge.bat)
- Запустити свіжу генерацію з image_backend=betatest
- git commit

---

## 2026-03-05 — Bug fix: 'order' KeyError + BetaTest timeouts + cleanup

### Проблема
- Jobs 65, 66 failed with error `'order'` (KeyError: 'order') в modules/02_image_generator.py:204 і 03_voice_generator.py:130
- Root cause: `01b_script_validator.py` коли fix `cut_off` або `missing_cta` — додає нові блоки через `blocks.extend(new_blocks)` але НЕ встановлює `order` field
- При наступному збереженні: нові блоки в script.json без `order` → KeyError на step 2

### Fix: 01b_script_validator.py
Перед збереженням виправленого script.json (рядок ~920) додано re-indexing:
```python
_final_blocks = script.get("blocks", [])
for _new_ord, _blk in enumerate(_final_blocks, start=1):
    _blk["id"]    = f"block_{_new_ord:03d}"
    _blk["order"] = _new_ord
script["blocks"] = _final_blocks
```
Гарантує що ВСІХ блоків є `id` і `order` перед записом на диск.

### Fix: betaimage_client.py timeouts
- `REQUEST_TIMEOUT`: 30s → 60s (quality mode повільніший при enqueue)  
- `DOWNLOAD_TIMEOUT`: 60s → 120s
- `POLL_TIMEOUT`: 120s → 300s (5 хвилин — quality mode може займати 60-90s)
- `POLL_INTERVAL`: 2s → 3s (менше запитів на сервер)
- `RETRY_BASE_DELAY`: 3s → 5s

### Cleanup
- 1564 MB звільнено з усіх project directories
- DB: jobs 64=failed, 67=done (вже в terminal state, не потребували скасування)

### Файли
`modules/01b_script_validator.py`, `clients/betaimage_client.py`

### Далі
- Рестарт бекенду (`VideoForge.bat`)
- При старті auto-cancel orphaned jobs (нова фіча) 
- Запуск 8 відео з `image_backend=betatest`, quality=max, steps 1-5

---

## 2026-03-06 — Validator fixes, master_script_v4, 2-tier image density, parallel pipeline test, FFmpeg preset optimization

### Виконано

**01b_script_validator.py — системні фікси:**
- Додано константи: `_META_TEXT_RE`, `_GENERIC_TAGS`, `IMAGE_GAP_MAX_WORDS = 70`
- `_structural_checks()`: нові параметри `tags`, `thumbnail_prompt`
- Нові перевірки: `duplicate_cta`, `meta_text`, `wrong_tags`, `thumbnail_empty`, `image_gap`
- Fix A: meta_text — regex strip, без API
- Fix B: cta_no_image — дефолтний image_prompt, без API
- Fix C: wrong_tags — cheap LLM (_fix_tags)
- sparse_images threshold: `nw//150` → `nw//70`

**prompts/master_script_v4.txt (новий):**
- Секція `⛔ ABSOLUTE PROHIBITIONS` (no meta-text, no parser labels, no duplicate sections, output-only)
- CTA блоки обов'язково мають `[IMAGE_PROMPT:]`
- 2-tier image density model в промпті

**modules/05_video_compiler.py:**
- `_DEFAULT_FREQ_TIERS`: 2-tier → 0-10 хв кожні 10с, 10+ хв кожні 30с

**config/channels/history.json:** master_prompt_path v3 → v4

**Паралельний тест (2 відео одночасно):**
- Ken Burns: "Why Smart People Doubt Themselves" → 19.9 хв, 416 MB, elapsed=2812s
- no_ken_burns: "When You Listen To The Unconscious - Carl Jung" → 31.5 хв, 161 MB, elapsed=2042s
- Step 4 compile: Ken Burns=~27 хв, no_ken_burns=~11 хв (2.5x різниця)

**Аналіз реальних витрат:**
- VoidAI: flat $35/міс, ~190k кредитів/відео 30хв
- VoiceAPI: $0.0000038/char = $19/5M chars, ~$0.09-0.10/відео 30хв
- WaveSpeed thumbnails: $0.015 (3 шт × $0.005)
- Реальна вартість відео 30 хв (betatest): ~$0.16
- При 3.5M VoidAI credits/day: ~18 відео/день, 460k VoiceAPI chars/день

**utils/ffmpeg_utils.py — FFmpeg preset оптимізація:**
- `DEFAULT_PRESET`: `"medium"` → `"veryfast"`
- Benchmark на 11 хв реального відео: 1.5x швидше compile, -27% intermediate, -21% final file size
- Якість незмінна (CRF=22 контролює якість, preset — тільки швидкість)

### Файли
- `modules/01b_script_validator.py` — нові checks + auto-fixes
- `prompts/master_script_v4.txt` — новий master prompt
- `modules/05_video_compiler.py` — 2-tier freq tiers
- `config/channels/history.json` — v3 → v4
- `utils/ffmpeg_utils.py` — DEFAULT_PRESET medium → veryfast

### Рішення
- LLM ігнорує 2-tier density: промпт каже "10с в першій зоні", LLM усереднює ~35с — потрібні жорсткіші інструкції
- Ken Burns compile 2.5x повільніше за static при меншому відео — через ~119 окремих FFmpeg сегментів
- `veryfast` виявився і швидшим і меншим за `medium` для статичного контенту (краща delta-компресія)

