# Поточна задача

## Задача №3 — WaveSpeed клієнт
- Async, промпт → URL → файл, retry, $0.005/запит tracking
- **Rate limiter:** asyncio.Semaphore (max 5 одночасних запитів) — захист від 429 при batch
- Результат: генерує тестову картинку
- Залежить від: 1

## Наступна задача
№4 — VoiceAPI клієнт

---
Після виконання: `python dev.py next -md` → git commit
