# Поточна задача

## Задача №4 — VoiceAPI клієнт
- voiceapi.csv666.ru, текст + voice_id → MP3, різні мови, retry
- **Rate limiter:** asyncio.Semaphore (max 3 одночасних) — VoiceAPI чутливіший до навантаження
- Fallback на VoidAI TTS (tts-1-hd) якщо VoiceAPI недоступний
- Результат: генерує тестове аудіо
- Залежить від: 1, 2

## Наступна задача
№5 — FFmpeg утиліти

---
Після виконання: `python dev.py next -md` → git commit
