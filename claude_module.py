"""
Claude Code Module для VideoForge Telegram Bot
Додає команди для запуску Claude Sonnet та Opus
"""

import os
import subprocess
import logging
import requests
from pathlib import Path

log = logging.getLogger("claude_module")

# Шляхи до скриптів
SCRIPTS_DIR = Path(r"C:\Users\User\.claude\scripts")
SONNET_SCRIPT = SCRIPTS_DIR / "sonnet.ps1"
OPUS_SCRIPT = SCRIPTS_DIR / "opus_proxy.ps1"

# Opus API конфігурація
OPUS_API_URL = "http://46.173.17.179:3188"
OPUS_API_KEY = "sk_claude46_ac345489d0751eb7ff8be1702eb881aef26a3baabaaf9f4d"

# Активні Claude процеси
_claude_processes = {}


def check_opus_quota():
    """
    Перевірка квоти токенів для Opus API

    Returns:
        dict: {"ok": bool, "spent": int, "limit": int, "remaining": int, "error": str}
    """
    try:
        response = requests.get(
            f"{OPUS_API_URL}/v1/quota",
            headers={"x-api-key": OPUS_API_KEY},
            timeout=5
        )

        if response.status_code == 200:
            data = response.json()
            user = data.get("user", {})
            return {
                "ok": True,
                "spent": user.get("tokenUsed", 0),
                "limit": user.get("tokenLimit", 0),
                "remaining": user.get("tokenRemaining", 0),
                "error": None
            }
        else:
            return {
                "ok": False,
                "spent": 0,
                "limit": 0,
                "remaining": 0,
                "error": f"HTTP {response.status_code}"
            }
    except Exception as e:
        return {
            "ok": False,
            "spent": 0,
            "limit": 0,
            "remaining": 0,
            "error": str(e)
        }


def register_claude_commands(bot, auth_func=None):
    """
    Реєстрація Claude команд в VideoForge боті

    Args:
        bot: telebot.TeleBot instance
        auth_func: функція авторизації (опціонально)

    Usage:
        from claude_module import register_claude_commands

        # В tg_bot.py після створення bot:
        register_claude_commands(bot, _auth)
    """

    # Кнопки для клавіатури
    BTN_CLAUDE_SONNET = "🧠 Claude Sonnet"
    BTN_CLAUDE_OPUS = "💎 Claude Opus"
    BTN_CLAUDE_STATUS = "📊 Claude статус"
    BTN_CLAUDE_QUOTA = "💰 Токени Opus"

    def _auth_wrapper(func):
        """Wrapper для авторизації"""
        def wrapper(message):
            if auth_func and not auth_func(message):
                return
            return func(message)
        return wrapper

    @bot.message_handler(commands=["claude_sonnet"])
    @_auth_wrapper
    def cmd_claude_sonnet(message):
        """Запуск Claude Sonnet 4.5"""
        user_id = message.chat.id

        if user_id in _claude_processes:
            bot.reply_to(message, "⚠️ У вас вже є активна Claude сесія\nВикористайте /claude_stop")
            return

        bot.reply_to(message, "🚀 Запускаю Claude Sonnet 4.5...")

        try:
            process = subprocess.Popen(
                ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(SONNET_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )

            _claude_processes[user_id] = {
                "process": process,
                "model": "Sonnet 4.5",
                "endpoint": "Kiro"
            }

            bot.reply_to(
                message,
                "✅ *Claude Sonnet 4.5 запущено!*\n\n"
                "📍 Endpoint: Kiro proxy (localhost:20128)\n"
                "🪟 Відкрито нове вікно PowerShell\n\n"
                "Команди:\n"
                "/claude_status — перевірити статус\n"
                "/claude_stop — зупинити сесію",
                parse_mode="Markdown"
            )
            log.info(f"Claude Sonnet started for user {user_id}")

        except Exception as e:
            bot.reply_to(message, f"❌ Помилка запуску: {e}")
            log.error(f"Failed to start Sonnet: {e}")

    @bot.message_handler(commands=["claude_opus"])
    @_auth_wrapper
    def cmd_claude_opus(message):
        """Запуск Claude Opus 4"""
        user_id = message.chat.id

        if user_id in _claude_processes:
            bot.reply_to(message, "⚠️ У вас вже є активна Claude сесія\nВикористайте /claude_stop")
            return

        bot.reply_to(message, "🚀 Запускаю Claude Opus 4...")

        try:
            process = subprocess.Popen(
                ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(OPUS_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )

            _claude_processes[user_id] = {
                "process": process,
                "model": "Opus 4",
                "endpoint": "External"
            }

            bot.reply_to(
                message,
                "✅ *Claude Opus 4 запущено!*\n\n"
                "📍 Endpoint: External proxy (46.173.17.179:3188)\n"
                "🪟 Відкрито нове вікно PowerShell\n\n"
                "Команди:\n"
                "/claude_status — перевірити статус\n"
                "/claude_stop — зупинити сесію",
                parse_mode="Markdown"
            )
            log.info(f"Claude Opus started for user {user_id}")

        except Exception as e:
            bot.reply_to(message, f"❌ Помилка запуску: {e}")
            log.error(f"Failed to start Opus: {e}")

    @bot.message_handler(commands=["claude_status"])
    @_auth_wrapper
    def cmd_claude_status(message):
        """Статус Claude сесії"""
        user_id = message.chat.id

        if user_id not in _claude_processes:
            bot.reply_to(message, "ℹ️ Немає активних Claude сесій")
            return

        session = _claude_processes[user_id]
        process = session["process"]

        if process.poll() is None:
            bot.reply_to(
                message,
                f"✅ *Активна Claude сесія:*\n\n"
                f"🤖 Модель: {session['model']}\n"
                f"📍 Endpoint: {session['endpoint']}\n"
                f"🆔 PID: {process.pid}",
                parse_mode="Markdown"
            )
        else:
            del _claude_processes[user_id]
            bot.reply_to(message, "ℹ️ Сесія завершена")

    @bot.message_handler(commands=["claude_stop"])
    @_auth_wrapper
    def cmd_claude_stop(message):
        """Зупинка Claude сесії"""
        user_id = message.chat.id

        if user_id not in _claude_processes:
            bot.reply_to(message, "ℹ️ Немає активних сесій для зупинки")
            return

        try:
            process = _claude_processes[user_id]["process"]
            process.terminate()
            del _claude_processes[user_id]
            bot.reply_to(message, "✅ Claude сесію зупинено")
            log.info(f"Claude session stopped for user {user_id}")
        except Exception as e:
            bot.reply_to(message, f"❌ Помилка зупинки: {e}")
            log.error(f"Failed to stop Claude: {e}")

    @bot.message_handler(commands=["claude_quota"])
    @_auth_wrapper
    def cmd_claude_quota(message):
        """Перевірка квоти токенів Opus API"""
        bot.reply_to(message, "🔍 Перевіряю квоту токенів...")

        quota = check_opus_quota()

        if quota["ok"]:
            spent = quota["spent"]
            limit = quota["limit"]
            remaining = quota["remaining"]
            percent = (spent / limit * 100) if limit > 0 else 0

            # Форматування чисел
            spent_fmt = f"{spent:,}".replace(",", " ")
            limit_fmt = f"{limit:,}".replace(",", " ")
            remaining_fmt = f"{remaining:,}".replace(",", " ")

            # Емодзі залежно від залишку
            if percent < 50:
                emoji = "🟢"
            elif percent < 80:
                emoji = "🟡"
            else:
                emoji = "🔴"

            bot.reply_to(
                message,
                f"{emoji} *Квота токенів Opus API:*\n\n"
                f"💸 Використано: `{spent_fmt}`\n"
                f"📊 Ліміт: `{limit_fmt}`\n"
                f"✨ Залишилось: `{remaining_fmt}`\n\n"
                f"📈 Використано: {percent:.1f}%",
                parse_mode="Markdown"
            )
            log.info(f"Quota check: {spent}/{limit} tokens used")
        else:
            bot.reply_to(
                message,
                f"❌ Помилка перевірки квоти:\n`{quota['error']}`",
                parse_mode="Markdown"
            )
            log.error(f"Quota check failed: {quota['error']}")

    # Повертаємо кнопки для додавання в клавіатуру (опціонально)
    return {
        "buttons": [BTN_CLAUDE_SONNET, BTN_CLAUDE_OPUS, BTN_CLAUDE_STATUS, BTN_CLAUDE_QUOTA],
        "handlers": {
            BTN_CLAUDE_SONNET: cmd_claude_sonnet,
            BTN_CLAUDE_OPUS: cmd_claude_opus,
            BTN_CLAUDE_STATUS: cmd_claude_status,
            BTN_CLAUDE_QUOTA: cmd_claude_quota,
        }
    }
