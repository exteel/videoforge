# Railway Simple Deploy

## 🎯 Що буде працювати:

✅ Бот завжди онлайн на Railway (24/7)
✅ Всі команди VideoForge (backend, tunnel, тощо)
⚠️ Claude кнопки **НЕ працюють** на Railway (потрібен локальний ПК)

## 🚀 Деплой на Railway

### Крок 1: Підготовка

Файли готові:
- `Dockerfile.railway` - Docker конфігурація
- `requirements_railway.txt` - Залежності
- `tg_bot.py` - Бот

### Крок 2: Railway Setup

1. Відкрий [railway.app](https://railway.app)
2. **New Project** → **Empty Project**
3. **Deploy from GitHub** або **Deploy from local**

### Крок 3: Змінні оточення

В Railway Dashboard → Variables:

```env
TG_BOT_TOKEN=твій_telegram_bot_token
TG_ALLOWED_CHAT_ID=твій_telegram_user_id
```

### Крок 4: Dockerfile

В Railway Settings:
- **Dockerfile Path:** `Dockerfile.railway`

### Крок 5: Deploy!

Railway автоматично збере і запустить бота.

## ⚠️ Важливо про Claude кнопки

Claude кнопки (🧠 Sonnet, 💎 Opus) **не працюватимуть** на Railway тому що:
- Вони запускають PowerShell (тільки Windows)
- Railway це Linux сервер

**Рішення:**
- Запускай бота **локально** коли хочеш використовувати Claude
- Або видали Claude кнопки з Railway версії

## 🔧 Видалити Claude кнопки (опціонально)

Якщо хочеш щоб на Railway не було Claude кнопок:

1. Закоментуй в `tg_bot.py`:
```python
# from claude_module import register_claude_commands
# claude_ui = register_claude_commands(bot, lambda m: _auth(m))
```

2. Видали Claude кнопки з `_keyboard()`:
```python
# kb.row("🧠 Claude Sonnet", "💎 Claude Opus")
# kb.row("🗑️ Очистити історію", "💰 Токени Opus")
```

## ✅ Готово!

Бот працює 24/7 на Railway, але без Claude функцій.
