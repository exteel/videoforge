# PROJECT_PLAN.md — VideoForge

> ⚠️ **Довідник. НЕ додавай в кожен чат!** Використовуй: `@CONTEXT.md` + `@TASK.md`

---

## Огляд

VideoForge — автоматизоване створення YouTube-відео. Набір CLI-модулів які приймають output від існуючого Transcriber → генерують сценарій → картинки → озвучку → компілюють відео → upload. Масштаб: десятки відео/день.

## Що вже є і працює (НЕ чіпаємо)

### YouTube Transcriber (D:\transscript batch\Transcriber\)
- Whisper транскрибація (GPU/CPU), плейлисти, cookies
- Output на кожне відео:
  - `transcript.txt` — плоский текст (ОСНОВНИЙ INPUT для сценарію)
  - `transcript.srt` — субтитри з таймкодами
  - `metadata.json` — URL, video_id, тривалість, мова
  - `title.txt` / `description.txt` — оригінальні назва і опис
  - `thumbnail.jpg` — оригінальне прев'ю
  - `thumbnail_prompt.txt` — AI-промпт з thumbnail (вже згенеровано!)

### Thumbnail Analyzer (D:\transscript batch\Thumbnail Analyzer\)
- Аналіз thumbnail → промпт → WaveSpeed генерація → ітеративне порівняння (5 спроб)
- VoidAI gpt-4.1 для vision-аналізу
- Логіка яку можна переюзати в модулі 06

### Мастер-промпт
- Для переписування сценаріїв за референсом (дошліфовується)

---

## API через VoidAI (OpenAI-сумісний формат)

VoidAI — єдина точка входу для ВСІХ AI сервісів. base_url + api_key.

### Стратегія моделей (оптимізовано по ціні, дані з VoidAI)

**Chat / LLM:**
| Задача | Модель | In/Out | Альтернативи |
|---|---|---|---|
| **Сценарій (якість)** | `gpt-4.1` | 0.4x/1.6x | `gpt-5` (0.4x), `claude-sonnet-4-5` (0.6x/3x) |
| **Сценарій (bulk)** | `deepseek-v3.1` | 0.03x/0.15x | `gpt-4.1-mini` (0.03x/0.12x), `gemini-2.5-flash` (0.03x/0.12x) |
| **Сценарій (ultra-bulk)** | `mistral-small-latest` | 0.006x/0.02x | `ministral-8b` (0.006x), `gemma-3n` (0.01x/0.04x) |
| **Метадані/теги** | `gpt-4.1-mini` | 0.03x/0.12x | `gpt-4.1-nano` (0.02x/0.08x) |
| **Thumbnail prompt** | `gpt-4.1` (vision) | 0.4x/1.6x | `gemini-2.5-pro` (0.25x/2x) |
| **SEO** | `gpt-4.1-nano` | 0.02x/0.08x | `gpt-5-nano` (0.02x), `gemini-2.5-flash-lite` (0.015x!) |
| **Reasoning** | `o3-mini` | 0.22x/0.88x | `deepseek-r1` (0.11x/0.44x), `o4-mini` (0.22x) |
| **Швидкий і дешевий** | `grok-4-1-fast` | 0.04x/0.1x | `grok-code-fast-1` (0.04x) |

**Цінові preset'и для pipeline (--quality flag):**
| Preset | Script model | Meta model | Коли використовувати |
|---|---|---|---|
| `max` | claude-opus-4-6 (1x/5x) | gpt-4.1-mini | **Дефолт.** Найкраща якість сценарію |
| `high` | claude-sonnet-4-5 (0.6x/3x) | gpt-4.1-mini | Близько до max, 2x дешевше |
| `balanced` | gpt-5.2 (0.4x/1.6x) | gpt-4.1-nano | Хороша якість, 3x дешевше max |
| `bulk` | deepseek-v3.1 (0.03x/0.15x) | gpt-4.1-nano | Масова генерація, 30x дешевше |
| `test` | mistral-small (0.006x) | gemma-3n | Тести пайплайну, мінімальна ціна |

*Ціна LLM тільки, без image/voice

**Image generation:**
| Модель | Провайдер | Призначення |
|---|---|---|
| **WaveSpeed z-image/turbo** | WaveSpeed | Основний ($0.005/шт, швидкий) |
| `gpt-image-1.5` | VoidAI/OpenAI | Backup 1 (якісний) |
| `imagen-4.0` | VoidAI/Google | Backup 2 |
| `flux-kontext-pro` | VoidAI/BFL | Для стилізації/edit |
| `recraft-v3` | VoidAI/Recraft | Альтернатива |

**TTS:**
| Модель | Провайдер | Призначення |
|---|---|---|
| **VoiceAPI** | csv666.ru→ElevenLabs | Основний (клоновані голоси) |
| `tts-1-hd` | VoidAI/OpenAI | Backup 1 (якісний) |
| `gpt-4o-mini-tts` | VoidAI/OpenAI | Backup 2 (дешевий) |

**Інше через VoidAI:**
| Сервіс | Модель | Призначення |
|---|---|---|
| Transcription | `whisper-1` / `gpt-4o-transcribe` | Cloud backup для локального Whisper |
| Search | `sonar` / `sonar-pro` (Perplexity) | Пошук трендових тем для контенту |
| Moderation | `omni-moderation-latest` | Перевірка контенту перед upload |

### Зовнішні API (основні)

| Сервіс | Призначення | Вартість |
|---|---|---|
| **WaveSpeed** (z-image/turbo) | Картинки основний | $0.005/шт |
| **VoiceAPI** (csv666.ru → ElevenLabs) | TTS основний | За тарифом |
| **YouTube Data API v3** | Upload | Безкоштовно (квота) |

---

## Архітектура

```
[Transcriber output]              [VideoForge pipeline]
  transcript.txt      ──┐
  transcript.srt        │       ┌───────────────────┐
  metadata.json         ├──────▶│ 01_script_gen     │──▶ script.json
  title.txt             │       │ (VoidAI LLM)      │
  description.txt     ──┘       └─────────┬─────────┘
  thumbnail_prompt.txt ──────────────┐     │
                                     │     ├──────────┐
                                     │     ▼          ▼
                                     │  ┌────────┐ ┌────────┐
                                     │  │02_image│ │03_voice│
                                     │  │WaveSp. │ │VoiceAPI│
                                     │  └───┬────┘ └───┬────┘
                                     │      │          │
                                     │      │   ┌──────▼──┐
                                     │      │   │04_subs  │
                                     │      │   └──────┬──┘
                                     │      │          │
                                     │  ┌───▼──────────▼──┐
                                     │  │ 05_video_compile │
                                     │  │    (FFmpeg)      │
                                     │  └────────┬────────┘
                                     │           │
                              ┌──────▼──┐ ┌──────▼──┐ ┌─────────┐
                              │06_thumb │ │07_meta  │ │08_upload│
                              │WaveSp.  │ │VoidAI   │ │YouTube  │
                              └─────────┘ └─────────┘ └─────────┘
```

---

## Формат script.json (контракт)

```json
{
  "title": "How Ancient Rome Built Roads",
  "description": "Discover the engineering genius...",
  "tags": ["history", "rome"],
  "language": "en",
  "niche": "history",
  "source": {
    "video_id": "abc123",
    "original_title": "Roman Roads Documentary",
    "transcription_path": "path/to/transcript.txt",
    "thumbnail_prompt": "from thumbnail_prompt.txt if exists"
  },
  "blocks": [
    {
      "id": "block_001",
      "order": 1,
      "type": "intro",
      "narration": "Two thousand years ago...",
      "image_prompt": "Aerial view of Roman road..., cinematic, 8k",
      "animation": "zoom_out",
      "timestamp_label": "Introduction",
      "audio_duration": null,
      "hook": {
        "type": "curiosity",
        "formula": "context_lean + scroll_stop + snapback",
        "validation_score": null
      }
    }
  ],
  "thumbnail_prompt": "Roman road dramatic sunset...",
  "channel_config": {
    "name": "History Channel",
    "voice_id": "voice_abc123",
    "image_style": "cinematic, photorealistic, 8k",
    "subtitle_style": "white_bold_outline"
  }
}
```

---

## Конфігурація каналу (channel_config.json)

```json
{
  "channel_name": "History Explained",
  "niche": "history",
  "language": "en",
  "additional_languages": ["de", "es"],
  "voice_id": "voice_abc123",
  "voice_ids_multilang": {
    "de": "voice_de_456",
    "es": "voice_es_789"
  },
  "image_style": "cinematic, photorealistic, dramatic lighting, 8k",
  "thumbnail_style": "bold text, dramatic imagery, high contrast",
  "subtitle_style": {
    "font": "Arial Bold",
    "size": 48,
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 3,
    "position": "bottom"
  },
  "default_animation": "zoom_in",
  "intro_video": "config/channels/assets/history_intro.mp4",
  "outro_video": "config/channels/assets/history_outro.mp4",
  "background_music": {
    "tracks_dir": "config/channels/assets/music/",
    "volume_db": -20,
    "random": true
  },
  "crossfade_duration": 0.5,
  "default_template": "documentary",
  "hooks": {
    "default_type": "curiosity",
    "per_template": {
      "documentary": "curiosity",
      "listicle": "negative",
      "tutorial": "challenge",
      "comparison": "comparison"
    },
    "auto_validate": true,
    "max_regenerate_intro": 2
  },
  "master_prompt_path": "prompts/master_script_v2.txt",
  "master_prompt_version": "v2",
  "budget_per_video": 5.00,
  "schedule": {
    "frequency_days": 2,
    "publish_time": "18:00",
    "timezone": "UTC"
  },
  "llm": {
    "presets": {
      "max":      { "script": "claude-opus-4-6",       "metadata": "gpt-4.1-mini",   "thumbnail": "gpt-4.1" },
      "high":     { "script": "claude-sonnet-4-5-20250929", "metadata": "gpt-4.1-mini", "thumbnail": "gpt-4.1" },
      "balanced": { "script": "gpt-5.2",               "metadata": "gpt-4.1-nano",   "thumbnail": "gemini-2.5-flash" },
      "bulk":     { "script": "deepseek-v3.1",         "metadata": "gpt-4.1-nano",   "thumbnail": "gemini-2.5-flash" },
      "test":     { "script": "mistral-small-latest",   "metadata": "gemma-3n-e4b-it","thumbnail": "gemini-2.5-flash" }
    },
    "default_preset": "max",
    "fallback_chain": ["claude-sonnet-4-5-20250929", "gpt-5.2", "gpt-4.1"]
  },
  "tts": {
    "provider": "voiceapi",
    "fallback": "tts-1-hd"
  },
  "images": {
    "provider": "wavespeed",
    "fallback": "gpt-image-1.5"
  },
  "transcriber_output_dir": "D:/transscript batch/Transcriber/output"
}
```

---

## Файлова структура

```
videoforge/
├── CLAUDE.md / .claudeignore / .gitignore
├── CONTEXT.md / TASK.md / session_log.md / dev.py
├── PROJECT_PLAN.md / README.md
├── .env.example / .env
├── requirements.txt
├── config/
│   ├── channels/
│   │   ├── *.json                # Конфіги каналів
│   │   └── assets/               # Intro/outro відео + музика per канал
│   └── settings.json             # VoidAI base_url, defaults, fallbacks
├── prompts/
│   ├── master_script_v1.txt      # Перша версія мастер-промпту
│   ├── master_script_v2.txt      # Поточна версія
│   ├── hooks_guide.md            # Гайд по хуках (вбудовується в мастер-промпт)
│   ├── hook_validator.txt        # Промпт для валідації intro блоку (4 критерії)
│   ├── templates/                # Content templates per format
│   │   ├── documentary.txt       # Лінійна розповідь
│   │   ├── listicle.txt          # "10 facts about..."
│   │   ├── tutorial.txt          # Покрокова інструкція
│   │   └── comparison.txt        # "X vs Y"
│   ├── hook_validator.txt        # Промпт для перевірки першого блоку
│   ├── thumbnail_default.txt
│   └── metadata_default.txt
├── modules/
│   ├── common.py                 # Логування, конфіг, load_transcriber_output()
│   ├── 01_script_generator.py
│   ├── 02_image_generator.py
│   ├── 03_voice_generator.py
│   ├── 04_subtitle_generator.py
│   ├── 05_video_compiler.py
│   ├── 06_thumbnail_generator.py
│   ├── 07_metadata_generator.py
│   └── 08_youtube_uploader.py
├── clients/
│   ├── voidai_client.py          # Єдиний LLM (chat, vision, tts, image)
│   ├── wavespeed_client.py
│   ├── voiceapi_client.py
│   └── youtube_client.py
├── utils/
│   ├── ffmpeg_utils.py
│   ├── file_utils.py
│   └── cost_tracker.py
├── tests/test_data/
├── pipeline.py / batch.py        # Фаза 2
└── projects/                     # gitignored
```

---

## Повний роадмап

### ЕТАП 1 — Фундамент

**№1 — Ініціалізація проекту**
- Структура папок, requirements.txt (httpx, python-dotenv, pydantic, tqdm), .env.example (VOIDAI_API_KEY, VOIDAI_BASE_URL, WAVESPEED_API_KEY, VOICEAPI_KEY, DEFAULT_VOICE_ID, TRANSCRIBER_OUTPUT_DIR), .gitignore, .claudeignore
- modules/common.py: логування, .env + channel_config loader, project-path helpers, load_transcriber_output(path) — читає всі файли Transcriber output в dict
- config/channels/example_history.json, config/settings.json
- Результат: `pip install -r requirements.txt` + `python modules/common.py` працює
- Залежить від: —

**№2 — VoidAI клієнт** (clients/voidai_client.py)
- OpenAI-сумісний (base_url від VoidAI). ОДИН клієнт для chat, vision, tts, image gen.
- chat_completion(model, messages, **kwargs) — текстова генерація
- vision_completion(model, messages_with_images) — аналіз зображень
- generate_tts(model, text, voice) — text-to-speech (backup для VoiceAPI)
- generate_image(model, prompt) — image gen (backup для WaveSpeed)
- **Smart fallback chain:** якщо модель fail → автоматично наступна з fallback_chain конфігу (Opus → Sonnet → GPT)
- Async httpx. Retry з exponential backoff. Cost tracking per model.
- Результат: `python clients/voidai_client.py` тестує chat з gpt-4.1-nano
- Залежить від: 1

**№3 — WaveSpeed клієнт** (clients/wavespeed_client.py)
- Async, промпт → URL → файл, retry, $0.005/запит tracking
- **Rate limiter:** asyncio.Semaphore (max 5 одночасних запитів) — захист від 429 при batch
- Результат: генерує тестову картинку
- Залежить від: 1

**№4 — VoiceAPI клієнт** (clients/voiceapi_client.py)
- voiceapi.csv666.ru, текст + voice_id → MP3, різні мови, retry
- **Rate limiter:** asyncio.Semaphore (max 3 одночасних) — VoiceAPI чутливіший до навантаження
- Fallback на VoidAI TTS (tts-1-hd) якщо VoiceAPI недоступний
- Результат: генерує тестове аудіо
- Залежить від: 1, 2

**№5 — FFmpeg утиліти** (utils/ffmpeg_utils.py)
- get_duration(), resize(), ken_burns(zoom_in/out/pan_left/right), concat(), add_subs(), add_audio()
- **loudnorm()** — нормалізація гучності після конкатенації (EBU R128, target -16 LUFS)
- **mix_background_music(voice, music, music_db=-20)** — мікшує фонову музику під озвучку
- **crossfade(clip1, clip2, duration=0.5)** — плавний перехід між блоками замість hard cut
- **prepend_intro() / append_outro()** — клеїть intro/outro відео-шаблони з конфігу каналу
- Результат: демонструє ефекти на тестових файлах
- Залежить від: 1

### ЕТАП 2 — Модулі (кожен окремо)

**№6 — Script Generator** (modules/01_script_generator.py)
- INPUT: Transcriber output папка (автоматично читає transcript.txt, title.txt, description.txt, metadata.json)
- Підставляє в мастер-промпт + channel config → VoidAI (модель з конфігу) → JSON → Pydantic валідація → script.json
- Якщо є thumbnail_prompt.txt від Transcriber — вставляє в source.thumbnail_prompt
- **Content templates:** `--template documentary|listicle|tutorial|comparison` — різні промпти під формат відео, конфіг каналу має default_template
- **Prompt versioning:** промпти мають версії (master_script_v1.txt, v2.txt...), конфіг каналу вказує яку юзати
- **Hook system (prompts/hooks_guide.md):**
  - Мастер-промпт інструктує LLM генерувати перший блок за 3-кроковою формулою: Context Lean-In → Scroll-Stop ("АЛЕ...") → Contrarian Snapback
  - Вибір типу хука з конфігу каналу або автоматично per template: curiosity (освіта), negative (маркетинг), storytelling (лайфстайл), challenge (тех), comparison (огляди)
  - Правила: рівень мови 5-6 класу, "ви/ваш" замість "я/мій", біль > користь, один суб'єкт + одне питання (SSSQ)
- **Hook validation:** після генерації дешева модель (gpt-4.1-nano) + промпт з чеклисту (prompts/hook_validator.txt) перевіряє intro блок по 4 критеріях: clarity, curiosity, relevance, interest. Якщо fail — автоматична перегенерація тільки intro (не всього сценарію)
- **--compare N** — генерує N варіантів сценарію, зберігає як script_v1.json, script_v2.json
- CLI: `python modules/01_script_generator.py --source "path/to/output" --channel config/channels/history.json --template documentary`
- Залежить від: 2

**№7 — Image Generator** (modules/02_image_generator.py)
- script.json → WaveSpeed паралельно (asyncio.gather з semaphore) → images/
- **Image validation:** після генерації VoidAI vision (дешева модель) перевіряє "чи відповідає промпту, немає артефактів/тексту?" → auto-regenerate поганих (max 2 retry per image)
- Fallback: VoidAI image gen (gpt-image-1.5) якщо WaveSpeed fail
- Прогрес-бар (tqdm)
- Залежить від: 2, 3, 6

**№8 — Voice Generator** (modules/03_voice_generator.py)
- script.json → VoiceAPI → audio/block_NNN.mp3 + ffprobe duration → оновлює script.json → concat full_narration.mp3
- **Audio normalization:** після конкатенації → loudnorm (EBU R128) через FFmpeg utils
- Fallback: VoidAI TTS (tts-1-hd)
- **Multi-lang ready:** `--lang de` → юзає voice_ids_multilang з конфігу, зберігає в audio/de/
- Залежить від: 4, 5, 6

**№9 — Subtitle Generator** (modules/04_subtitle_generator.py)
- script.json (з audio_duration) → subtitles.srt + subtitles.ass (стиль з конфігу)
- Опція: використати оригінальний transcript.srt від Transcriber як базу для word-level timing
- Залежить від: 8

**№10 — Video Compiler** (modules/05_video_compiler.py)
- images/ + audio/ + subtitles.ass + анімації → final.mp4 (1080p H.264)
- Ken Burns per block, duration = audio_duration
- **Crossfade:** 0.5с плавний перехід між блоками (параметр в конфігу каналу)
- **Background music:** мікшує royalty-free трек з config/channels/assets/music/ на -20dB під голос
- **Intro/Outro:** якщо в конфігу каналу є intro_video/outro_video — клеїть на початок/кінець
- **--draft** — швидка зборка 480p без Ken Burns/crossfade для перевірки структури
- Залежить від: 5, 9

**№11 — Thumbnail Generator** (modules/06_thumbnail_generator.py)
- Якщо є thumbnail_prompt.txt від Transcriber → використати як базу
- Інакше: script.json thumbnail_prompt + стиль → WaveSpeed → thumbnail.png (1280x720)
- Опція: ітеративне покращення (логіка з Thumbnail Analyzer) — VoidAI vision порівняння
- Залежить від: 2, 3, 6

**№12 — Metadata Generator** (modules/07_metadata_generator.py)
- script.json + audio durations → metadata.json
- VoidAI (gpt-4.1-mini) для SEO-оптимізації title/description/tags
- Timestamps обчислюються з audio_duration
- Залежить від: 2, 8

**№13 — YouTube Uploader** (modules/08_youtube_uploader.py)
- video + thumb + metadata → YouTube OAuth2 → upload
- **--schedule "2026-03-05 18:00"** — відкладена публікація в оптимальний час
- **Auto-schedule:** для batch — автоматичний розклад з конфігу каналу (кожні N днів о HH:MM)
- Залежить від: 1

### ЕТАП 3 — E2E тестування

**№14 — Тестові дані і повний прогон**
- Реальний Transcriber output (коротке відео 3-5 хв) → всі модулі → готове відео
- Залежить від: 6-13

**№15 — Фікс багів**
- Ітерація після E2E
- Залежить від: 14

### ЕТАП 4 — Pipeline

**№16 — Pipeline Runner** (pipeline.py)
- source folder → script → images+audio (parallel) → subs → compile → thumb → metadata
- `--quality max|high|balanced|bulk|test` — вибір preset'у моделей (default: max)
- `--review` — зупиняється після script.json, чекає підтвердження перед витратою на images/voice
- `--auto` — без зупинок, для batch (default)
- `--dry-run` — рахує вартість без API викликів (N блоків × ціна image + voice + LLM = $X.XX)
- `--draft` — генерує все, але відео в 480p без ефектів для швидкої перевірки
- `--from-step N` — продовжити з кроку N (юзає кеш попередніх)
- `--lang en,de,es` — мультимовна генерація: один сценарій → voice + subs в кількох мовах
- `--template documentary|listicle|tutorial` — формат відео
- `--budget 5.00` — ліміт витрат на одне відео в $, якщо перевищує → зупиняється і повідомляє
- Кешування: якщо images/ вже є — пропускає крок 2, перегенерує тільки що потрібно
- Smart fallback: Opus fail → Sonnet → GPT-5 (не просто retry тієї ж моделі)
- Валідація після кожного кроку: файли існують, duration збігається, розмір > 0
- Залежить від: 15

**№17 — Cost Tracker** (utils/cost_tracker.py)
- Per-model tracking, per-video summary, total session cost
- **estimate_cost(script_json, quality_preset)** — для --dry-run: рахує приблизну вартість без API
- Вивід: таблиця "модуль | модель | токени | ціна"
- Залежить від: 16

**№18 — Batch Runner** (batch.py)
- Сканує Transcriber output dir → pipeline для кожного → --parallel N → --quality bulk (batch = дешевше)
- Залежить від: 16, 17

**№19 — SQLite трекінг** (database.py)
- Статуси, вартість per model, YouTube URLs, зв'язок з Transcriber source
- Залежить від: 16

### ЕТАП 5 — Веб-інтерфейс (опціонально)

**№20 — FastAPI бекенд** (backend/)
- REST API обгортка, WebSocket прогрес
- Залежить від: 19

**№21 — React Dashboard** (frontend/)
- Vite + React + TailwindCSS, список відео, статуси
- Залежить від: 20

**№22 — UI ревью і створення**
- Редактор сценарію, прев'ю, запуск кроків
- Залежить від: 21

**№23 — UI канали і промпти**
- CRUD каналів і промптів
- Залежить від: 21

**№24 — Docker** (docker-compose.yml)
- Контейнеризація
- Залежить від: 20, 21

**№25 — Документація** (README.md)
- Встановлення, конфігурація, інтеграція з Transcriber
- Залежить від: 1-24
