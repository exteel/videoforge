# Поточна задача

## Задача №10 — Video Compiler
- images/ + audio/ + subtitles.ass + анімації → final.mp4 (1080p H.264)
- Ken Burns per block, duration = audio_duration
- **Crossfade:** 0.5с плавний перехід між блоками (параметр в конфігу каналу)
- **Background music:** мікшує royalty-free трек з config/channels/assets/music/ на -20dB під голос
- **Intro/Outro:** якщо в конфігу каналу є intro_video/outro_video — клеїть на початок/кінець
- **--draft** — швидка зборка 480p без Ken Burns/crossfade для перевірки структури
- Залежить від: 5, 9

## Наступна задача
№11 — Thumbnail Generator

---
Після виконання: `python dev.py next -md` → git commit
