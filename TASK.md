# Поточна задача

## Задача №2 — VoidAI клієнт
- OpenAI-сумісний (base_url від VoidAI). ОДИН клієнт для chat, vision, tts, image gen.
- chat_completion(model, messages, **kwargs) — текстова генерація
- vision_completion(model, messages_with_images) — аналіз зображень
- generate_tts(model, text, voice) — text-to-speech (backup для VoiceAPI)
- generate_image(model, prompt) — image gen (backup для WaveSpeed)
- **Smart fallback chain:** якщо модель fail → автоматично наступна з fallback_chain конфігу (Opus → Sonnet → GPT)
- Async httpx. Retry з exponential backoff. Cost tracking per model.
- Результат: `python clients/voidai_client.py` тестує chat з gpt-4.1-nano
- Залежить від: 1

## Наступна задача
№3 — WaveSpeed клієнт

---
Після виконання: `python dev.py next -md` → git commit
