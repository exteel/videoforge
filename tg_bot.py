"""
VideoForge Telegram Bot — remote control via Telegram.

Commands:
  /url        — поточне tunnel посилання
  /status     — стан backend + tunnel
  /start      — запустити backend + tunnel
  /restart    — перезапустити backend
  /starttunnel — запустити/перезапустити tunnel

Run:
  python tg_bot.py

Or via start-bot.bat
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

import httpx
import json
import requests
import telebot  # pip install pyTelegramBotAPI
from dotenv import load_dotenv

# ── Bootstrap ─────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

handler = logging.StreamHandler(
    open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
)
handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger("tg_bot")

TOKEN      = os.getenv("TG_BOT_TOKEN", "").strip()
ALLOWED_ID = int(os.getenv("TG_ALLOWED_CHAT_ID", "0"))
N8N_URL    = os.getenv("N8N_URL", "http://localhost:5678")

if not TOKEN:
    log.error("TG_BOT_TOKEN not set in .env — exiting")
    sys.exit(1)

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

# ── Process tracking ──────────────────────────────────────────────────────────

_backend_proc: subprocess.Popen | None = None  # type: ignore[type-arg]

# Tunnel management via tunnel_utils (cloudflared)
from tunnel_utils import (
    get_tunnel_url as _get_tunnel_url,
    tunnel_check as _tunnel_check,
    start_tunnel as _start_tunnel_cf,
    stop_tunnel as _stop_tunnel,
    wait_tunnel_url as _wait_tunnel_url,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth(message: telebot.types.Message) -> bool:
    if ALLOWED_ID and message.chat.id != ALLOWED_ID:
        bot.reply_to(message, "⛔ Немає доступу")
        return False
    return True


def _get_ngrok_url() -> str | None:
    """Backward-compatible alias — reads cloudflared tunnel URL."""
    return _get_tunnel_url("videoforge")


def _backend_alive() -> bool:
    try:
        r = httpx.get("http://localhost:8000/api/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _backend_check() -> dict:
    """Full backend health check with details."""
    import time as _time
    try:
        t0 = _time.monotonic()
        r = httpx.get("http://localhost:8000/api/health", timeout=5.0)
        ms = int((_time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            d = r.json()
            return {"ok": True, "ms": ms, "version": d.get("version", "?"), "service": d.get("service", "")}
        return {"ok": False, "ms": ms, "error": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "ms": None, "error": str(exc)[:80]}


def _ngrok_check() -> dict:
    """Backward-compatible alias — checks cloudflared tunnel status."""
    return _tunnel_check("videoforge")


def _proc_alive(proc: subprocess.Popen | None) -> bool:  # type: ignore[type-arg]
    return proc is not None and proc.poll() is None


def _start_backend() -> str:
    global _backend_proc
    if _proc_alive(_backend_proc):
        return "вже запущений"
    _backend_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "0.0.0.0", "--port", "8000"],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    log.info("Backend started (PID %s)", _backend_proc.pid)
    return f"запущено (PID {_backend_proc.pid})"


def _start_ngrok() -> str:
    """Backward-compatible alias — starts cloudflared tunnel on port 8000."""
    return _start_tunnel_cf(port=8000, name="videoforge")


def _kill_port_8000() -> None:
    """Kill any process listening on port 8000 (handles bot-restart edge case)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if ":8000 " in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                if pid and pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["taskkill", "/PID", pid, "/F"],
                                   capture_output=True)
                    log.info("Killed PID %s on port 8000", pid)
    except Exception as exc:
        log.warning("_kill_port_8000 failed: %s", exc)


def _restart_backend() -> str:
    global _backend_proc
    if _proc_alive(_backend_proc):
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backend_proc.kill()
        log.info("Backend stopped")
        time.sleep(1)
    _kill_port_8000()
    time.sleep(1)
    return _start_backend()


# ── Persistent reply keyboard ─────────────────────────────────────────────────

BTN_URL      = "🌐 Посилання"
BTN_STATUS   = "📊 Статус"
BTN_LAUNCH   = "🚀 Запустити все"
BTN_RESTART  = "🔄 Рестарт backend"
BTN_NGROK    = "🔁 Рестарт тунель"
BTN_AI       = "🤖 AI Team 777"

def _keyboard() -> telebot.types.ReplyKeyboardMarkup:
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_URL, BTN_STATUS)
    kb.row(BTN_LAUNCH)
    kb.row(BTN_RESTART, BTN_NGROK)
    kb.row(BTN_AI)
    return kb

def _reply(message: telebot.types.Message, text: str) -> None:
    """Send reply and always attach the keyboard."""
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=_keyboard())


# ── Handlers ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
def cmd_help(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    _reply(message, "🎬 *VideoForge Bot*\nОбери дію:")


@bot.message_handler(func=lambda m: m.text == BTN_URL)
@bot.message_handler(commands=["url", "link"])
def cmd_url(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    url = _get_ngrok_url()
    if url:
        _reply(message, f"🌐 `{url}`")
    else:
        _reply(message, "❌ Тунель не запущено\nНатисни *Запустити все* або *Рестарт тунель*")


@bot.message_handler(func=lambda m: m.text == BTN_STATUS)
@bot.message_handler(commands=["status"])
def cmd_status(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    b = _backend_check()
    n = _ngrok_check()
    lines = []
    if b["ok"]:
        lines.append(f"✅ *Backend* — OK ({b['ms']} мс, v{b['version']})")
    else:
        lines.append(f"❌ *Backend* — не відповідає\n  `{b.get('error', '?')}`")
    if n["ok"]:
        lines.append(f"🌐 *Тунель* — активний\n  `{n['url']}`")
    else:
        lines.append(f"❌ *Тунель* — не запущено")
    _reply(message, "\n\n".join(lines))


def _wait_ngrok_url(attempts: int = 20) -> str | None:
    """Backward-compatible alias — waits for cloudflared URL."""
    return _wait_tunnel_url("videoforge", attempts=attempts)


@bot.message_handler(func=lambda m: m.text == BTN_LAUNCH)
@bot.message_handler(commands=["launch"])
def cmd_launch(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    bot.send_message(message.chat.id, "🚀 Запускаю сервіс…", reply_markup=_keyboard())
    threading.Thread(target=_do_launch, args=(message.chat.id,), daemon=True).start()


def _do_launch(chat_id: int) -> None:
    # 1. Backend
    _start_backend()
    bot.send_message(chat_id, "⏳ Чекаю поки backend підніметься…", reply_markup=_keyboard())
    for _ in range(15):
        time.sleep(1)
        if _backend_alive():
            break
    b = _backend_check()
    if b["ok"]:
        bot.send_message(chat_id,
            f"✅ *Backend запущено*\n"
            f"• Відповідь: {b['ms']} мс\n"
            f"• Версія: {b['version']}",
            parse_mode="Markdown", reply_markup=_keyboard())
    else:
        bot.send_message(chat_id,
            f"❌ *Backend не відповідає*\n• {b.get('error', '?')}",
            parse_mode="Markdown", reply_markup=_keyboard())
        return

    # 2. ngrok
    _start_ngrok()
    bot.send_message(chat_id, "⏳ Чекаю тунель…", reply_markup=_keyboard())
    _wait_ngrok_url()
    n = _ngrok_check()
    if n["ok"]:
        bot.send_message(chat_id,
            f"✅ *Тунель активний*\n"
            f"• URL: `{n['url']}`",
            parse_mode="Markdown", reply_markup=_keyboard())
    else:
        bot.send_message(chat_id,
            f"⚠️ *Тунель не відповідає*\n• {n.get('error', '?')}",
            parse_mode="Markdown", reply_markup=_keyboard())


@bot.message_handler(func=lambda m: m.text == BTN_RESTART)
@bot.message_handler(commands=["restart"])
def cmd_restart(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    bot.send_message(message.chat.id, "🔄 Перезапускаю backend…", reply_markup=_keyboard())
    threading.Thread(target=_do_restart, args=(message.chat.id,), daemon=True).start()


def _do_restart(chat_id: int) -> None:
    _restart_backend()
    bot.send_message(chat_id, "⏳ Чекаю поки backend підніметься…", reply_markup=_keyboard())
    for _ in range(15):
        time.sleep(1)
        if _backend_alive():
            break
    b = _backend_check()
    if b["ok"]:
        bot.send_message(chat_id,
            f"✅ *Backend перезапущено*\n"
            f"• Відповідь: {b['ms']} мс\n"
            f"• Версія: {b['version']}",
            parse_mode="Markdown", reply_markup=_keyboard())
    else:
        bot.send_message(chat_id,
            f"❌ *Backend не відповідає після рестарту*\n• {b.get('error', '?')}",
            parse_mode="Markdown", reply_markup=_keyboard())


@bot.message_handler(func=lambda m: m.text == BTN_NGROK)
@bot.message_handler(commands=["starttunnel", "startngrok"])
def cmd_starttunnel(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    bot.send_message(message.chat.id, "🔄 Запускаю тунель…", reply_markup=_keyboard())
    threading.Thread(target=_do_starttunnel, args=(message.chat.id,), daemon=True).start()


def _do_starttunnel(chat_id: int) -> None:
    try:
        _start_ngrok()
    except Exception as exc:
        bot.send_message(chat_id, f"❌ *Не вдалось запустити тунель*\n`{exc}`",
                         parse_mode="Markdown", reply_markup=_keyboard())
        return
    bot.send_message(chat_id, "⏳ Чекаю тунель…", reply_markup=_keyboard())
    _wait_ngrok_url()
    n = _ngrok_check()
    if n["ok"]:
        bot.send_message(chat_id,
            f"✅ *Тунель перезапущено*\n"
            f"• URL: `{n['url']}`",
            parse_mode="Markdown", reply_markup=_keyboard())
    else:
        bot.send_message(chat_id,
            f"❌ *Тунель не відповідає*\n• {n.get('error', '?')}",
            parse_mode="Markdown", reply_markup=_keyboard())


# ── AI Team 777 extension (handlers registered in ai_team.py) ─────────────────
import ai_team  # noqa: F401  — must stay here so VideoForge handlers register first

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Register persistent command menu (the "/" button in Telegram)
    bot.set_my_commands([
        telebot.types.BotCommand("url",         "🌐 Поточне tunnel посилання"),
        telebot.types.BotCommand("status",      "📊 Стан backend + tunnel"),
        telebot.types.BotCommand("launch",      "🚀 Запустити backend + tunnel"),
        telebot.types.BotCommand("restart",     "🔄 Перезапустити backend"),
        telebot.types.BotCommand("starttunnel", "🔁 Перезапустити tunnel"),
        telebot.types.BotCommand("ai",         "🤖 AI Team 777 меню"),
        telebot.types.BotCommand("stop",       "🔚 Завершити AI чат сесію"),
    ])
    log.info("Bot commands menu registered")

    log.info("VideoForge Telegram Bot starting (allowed chat: %s)", ALLOWED_ID)
    # Delete webhook and steal the session from any existing long-poll connections
    try:
        bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted, pending updates dropped")
    except Exception as e:
        log.warning("Could not delete webhook: %s", e)
    # Call getUpdates with timeout=0 to forcibly terminate any other active polling session
    try:
        bot.get_updates(offset=-1, timeout=0)
        log.info("Session stolen from any existing polling instance")
    except Exception as e:
        log.warning("getUpdates session steal: %s", e)
    time.sleep(1)
    if ALLOWED_ID:
        try:
            bot.send_message(ALLOWED_ID, "🤖 VideoForge Bot запущено\nНапиши /status або /url")
        except Exception as e:
            log.warning("Could not send startup message: %s", e)
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
