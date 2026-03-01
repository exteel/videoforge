# Поточна задача

## Задача №7 — Image Generator
- script.json → WaveSpeed паралельно (asyncio.gather з semaphore) → images/
- **Image validation:** після генерації VoidAI vision (дешева модель) перевіряє "чи відповідає промпту, немає артефактів/тексту?" → auto-regenerate поганих (max 2 retry per image)
- Fallback: VoidAI image gen (gpt-image-1.5) якщо WaveSpeed fail
- Прогрес-бар (tqdm)
- Залежить від: 2, 3, 6

## Наступна задача
№8 — Voice Generator

---
Після виконання: `python dev.py next -md` → git commit
