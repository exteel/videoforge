# Поточна задача

## Задача №16 — Pipeline Runner
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

## Наступна задача
№17 — Cost Tracker

---
Після виконання: `python dev.py next -md` → git commit
