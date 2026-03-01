# VideoForge

Automated YouTube video generation pipeline. Takes Transcriber output and produces a finished MP4 with narration, Ken Burns visuals, thumbnail, and YouTube metadata.

```
Transcriber output → script (LLM) → images (WaveSpeed) → voice (VoiceAPI) → video (FFmpeg) → YouTube
```

## Stack

- **Python 3.11+** — async/await, httpx, pydantic, python-dotenv
- **FFmpeg** — video compile, Ken Burns, crossfade, loudnorm
- **FastAPI + uvicorn** — REST API + WebSocket progress
- **React + Vite + TailwindCSS** — web dashboard

## Prerequisites

- Python 3.11+
- FFmpeg in PATH (`ffmpeg --version`)
- Node.js 20+ (for frontend dev)
- API keys: VoidAI, WaveSpeed, VoiceAPI

## Installation

```bash
git clone <repo>
cd "Project video generator"

pip install -r requirements.txt

cp .env.example .env
# fill in API keys
```

## Configuration

### `.env`

```env
VOIDAI_API_KEY=sk-voidai-...
VOIDAI_BASE_URL=https://api.voidai.app/v1

WAVESPEED_API_KEY=...

VOICEAPI_KEY=...                          # X-API-Key header auth
VOICEAPI_TEMPLATE_UUID=a0c972ab-...       # optional, hardcoded default
DEFAULT_VOICE_ID=a4CnuaYbALRvW39mDitg

TRANSCRIBER_OUTPUT_DIR=D:/transscript batch/output/output
```

### Channel config (`config/channels/history.json`)

Defines niche, voice, image style, LLM presets, TTS/image providers.
Edit via UI → **Channels** tab, or directly in `config/channels/`.

## Running (local dev)

**Terminal 1 — Backend:**
```bash
python -m uvicorn backend.main:app --reload --port 8000
```

**Terminal 2 — Frontend:**
```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**

## Running (Docker)

```bash
# Production (single container, port 8000)
docker compose up

# Dev (hot reload, separate backend:8000 + frontend:5173)
docker compose --profile dev up
```

Set `TRANSCRIBER_OUTPUT_DIR` in `.env` to the host path with Transcriber output.
It will be mounted read-only into the container.

## Pipeline

6 steps, each skippable via `from_step`:

| Step | Name | What it does |
|------|------|--------------|
| 1 | Script | LLM generates script.json from transcript |
| 2 | Media | Images (WaveSpeed) + Voice (VoiceAPI) in parallel |
| 3 | Subtitles | SRT + ASS from script blocks |
| 4 | Video | FFmpeg: Ken Burns clips → crossfade concat → audio mix |
| 5 | Thumbnail | WaveSpeed generates + validates thumbnail |
| 6 | Metadata | LLM generates title, description, tags |

**Auto-skip:** if `script.json` exists with valid blocks, Step 1 is skipped automatically (saves credits).

**Caching:** images and audio files are skipped if they already exist and are > 1 KB.

### Quality presets

| Preset | Script model | Cost |
|--------|-------------|------|
| max | claude-opus-4-6 | 1x |
| high | claude-sonnet-4-5 | 0.6x |
| balanced | gpt-5.2 | 0.4x |
| bulk | deepseek-v3.1 | 0.03x |
| test | mistral-small | 0.006x |

### Dry run

```bash
python pipeline.py --source-dir "..." --dry-run
```
Prints cost estimate without making any API calls.

## Web UI

Open `http://localhost:5173` after starting both servers.

| Tab | Description |
|-----|-------------|
| Jobs | Launch pipeline / batch, monitor progress via WebSocket |
| Script | View and edit script.json blocks before generating media |
| Channels | CRUD for channel configs and prompt files |
| History | Past pipeline runs with cost breakdown |
| Stats | Aggregate stats by model and quality preset |

## Project structure

```
modules/          CLI modules (01_script_generator.py … 08_youtube_uploader.py)
clients/          API clients: voidai, wavespeed, voiceapi
backend/          FastAPI app + routes
frontend/src/     React dashboard
utils/            FFmpeg helpers, DB tracker, file utilities
config/channels/  Channel JSON configs
prompts/          LLM prompt files
projects/         Generated videos (one subfolder per video)
data/             SQLite DB (videoforge.db)
pipeline.py       Main orchestrator
batch_runner.py   Batch processing
```

## Integration with Transcriber

VideoForge reads output from [Transcriber](../Transcriber/) — do not modify it.

Expected input directory structure:
```
{TRANSCRIBER_OUTPUT_DIR}/{Video Title}/
  transcript.txt
  transcript.srt
  metadata.json
  title.txt
  description.txt
  thumbnail.jpg
  thumbnail_prompt.txt
```

Paste the path into **Source dir** in the Jobs tab.

## Fallbacks

| Primary | Fallback |
|---------|---------|
| VoiceAPI (ElevenLabs proxy) | VoidAI TTS (tts-1-hd) |
| WaveSpeed images | VoidAI image (gpt-image-1.5) |
| claude-opus-4-6 | claude-sonnet-4-5 → gpt-5.2 |

## Dev tools

```bash
python dev.py check-apis        # test all API connections
python dev.py next -md          # mark current task done + git commit
python clients/voiceapi_client.py --balance   # check TTS balance
python clients/voiceapi_client.py --output test.mp3  # TTS self-test
```
