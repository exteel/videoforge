# Поточна задача

## Задача №1 — Ініціалізація проекту
- Створити структуру папок (modules/, clients/, utils/, config/, prompts/, tests/)
- requirements.txt: httpx, python-dotenv, pydantic, tqdm
- .env.example: VOIDAI_API_KEY, VOIDAI_BASE_URL, WAVESPEED_API_KEY, VOICEAPI_KEY, DEFAULT_VOICE_ID, TRANSCRIBER_OUTPUT_DIR
- modules/common.py: логування (logging), load_env(), load_channel_config(path), load_transcriber_output(path) → dict з усіма файлами Transcriber, get_project_dir(channel, video_id), ensure_project_dirs()
- config/channels/example_history.json: повний конфіг з llm.presets (max/high/balanced/bulk/test, default=max), tts/images providers+fallbacks, стилі
- config/settings.json: глобальні defaults (VoidAI base_url, fallback models)
- prompts/master_script_v1.txt: заглушка
- tests/test_data/: порожня папка + sample_config.json
- Результат: `pip install -r requirements.txt` + `python modules/common.py` працює, виводить конфіг

## Наступна задача
№2 — VoidAI клієнт (clients/voidai_client.py)

---
Після виконання: `python dev.py next -md` → git commit
