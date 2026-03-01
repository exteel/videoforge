# Session Log — VideoForge

> Оновлюється після кожної відповіді. Нова сесія: `@session_log.md`

---

## 2026-03-01 — Планування
- **Зроблено:** Архітектура, всі стартові файли, аналіз існуючих інструментів
- **Рішення:** VoidAI єдиний AI провайдер, claude-opus-4-6 як дефолт для сценаріїв (якість критична), fallback pattern для WaveSpeed і VoiceAPI, 5 quality presets (max→test)
- **Ключове:** Transcriber вже генерує thumbnail_prompt.txt — переюзати. VoidAI має TTS і image gen як backup.
- **Далі:** №1 — ініціалізація
