# Поточна задача

## Задача №5 — FFmpeg утиліти
- get_duration(), resize(), ken_burns(zoom_in/out/pan_left/right), concat(), add_subs(), add_audio()
- **loudnorm()** — нормалізація гучності після конкатенації (EBU R128, target -16 LUFS)
- **mix_background_music(voice, music, music_db=-20)** — мікшує фонову музику під озвучку
- **crossfade(clip1, clip2, duration=0.5)** — плавний перехід між блоками замість hard cut
- **prepend_intro() / append_outro()** — клеїть intro/outro відео-шаблони з конфігу каналу
- Результат: демонструє ефекти на тестових файлах
- Залежить від: 1

## Наступна задача
№6 — Script Generator

---
Після виконання: `python dev.py next -md` → git commit
