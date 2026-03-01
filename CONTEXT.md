# Project Context — VideoForge
Останнє оновлення: 2026-03-01 15:30

## Що вже зроблено
- [x] Архітектура: 8 CLI-модулів, PROJECT_PLAN, dev.py
- [x] YouTube Transcriber — працює (окремий, не чіпаємо)
- [x] Thumbnail Analyzer — працює (окремий, не чіпаємо)
- [x] VoidAI API — 100+ моделей, chat/vision/tts/image
- [x] WaveSpeed API — картинки працюють
- [x] VoiceAPI (ElevenLabs) — протестовано
- [x] Мастер-промпт — для сценаріїв (дошліфовується)
- [x] №1 Ініціалізація проекту
- [ ] №2 VoidAI клієнт
- [ ] №3 WaveSpeed клієнт
- [ ] №4 VoiceAPI клієнт
- [ ] №5 FFmpeg утиліти
- [ ] №6 Script Generator
- [ ] №7 Image Generator
- [ ] №8 Voice Generator
- [ ] №9 Subtitle Generator
- [ ] №10 Video Compiler
- [ ] №11 Thumbnail Generator
- [ ] №12 Metadata Generator
- [ ] №13 YouTube Uploader
- [ ] №14 E2E тест
- [ ] №15 Фікс багів
- [ ] №16 Pipeline Runner
- [ ] №17 Cost Tracker
- [ ] №18 Batch Runner

## Поточний стан
Початок розробки VideoForge. Існуючі інструменти працюють окремо.

## Відомі баги
(немає)

## Прийняті рішення
- CLI-first → pipeline → UI
- VoidAI як єдиний AI провайдер (OpenAI-сумісний, 100+ моделей)
- 5 quality presets: max (claude-opus-4-6), high (claude-sonnet-4-5), balanced (gpt-5.2), bulk (deepseek-v3.1), test (mistral-small)
- Дефолт: max — якість сценаріїв критична, оптимізація пізніше
- WaveSpeed = основний image gen, VoidAI image = fallback
- VoiceAPI = основний TTS, VoidAI TTS = fallback
- Input від Transcriber as-is (transcript.txt, thumbnail_prompt.txt, metadata.json)
- Ken Burns анімація (не AI video)
- Аудіо → тривалість → відео
- script.json — єдиний контракт
- Субтитри hardcoded ASS
- Мови: en (основна), de, es
- Git commit після кожної задачі

## Лог сесій
Дивись session_log.md
