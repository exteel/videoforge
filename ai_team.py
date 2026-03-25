"""AI Team 777 — Telegram bot extension for VideoForge bot.

Imported at the BOTTOM of tg_bot.py (after all VideoForge handlers are
registered), so VideoForge handlers always take priority.  This module has
zero side-effects on VideoForge functionality.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests
import telebot

# ---------------------------------------------------------------------------
# Bootstrap — pull shared objects from tg_bot.
# Safe because tg_bot.py does `import ai_team` near its own end, so every
# VideoForge symbol is fully defined by the time Python executes this block.
# ---------------------------------------------------------------------------
import sys as _sys
# When tg_bot.py runs as `python tg_bot.py`, it is __main__, NOT 'tg_bot'.
# Doing `import tg_bot` would create a second bot instance (re-import), so
# ai_team handlers would register on the wrong bot and never fire.
# sys.modules['__main__'] is the already-running tg_bot with the bot used for polling.
_vf = _sys.modules.get("tg_bot") or _sys.modules["__main__"]

bot             = _vf.bot
_auth           = _vf._auth
_keyboard       = _vf._keyboard
_get_ngrok_url  = _vf._get_ngrok_url
_proc_alive     = _vf._proc_alive
_wait_ngrok_url = _vf._wait_ngrok_url

# Cloudflared tunnel management for N8N
from tunnel_utils import (
    start_tunnel as _start_tunnel_cf,
    stop_tunnel as _stop_tunnel,
    get_tunnel_url as _get_tunnel_url,
    wait_tunnel_url as _wait_tunnel_url,
    tunnel_check as _tunnel_check,
)
ROOT            = _vf.ROOT
log             = _vf.log
N8N_URL         = _vf.N8N_URL
BTN_AI          = _vf.BTN_AI

# ── Session-based AI Team chat ────────────────────────────────────────────────
from ai_team_chat import AITeamChat  # noqa: E402

_ai_chat = AITeamChat(
    bot=bot,
    director_url=f"{N8N_URL}/webhook/director",
    main_keyboard_fn=_keyboard,
    session_timeout_minutes=30,
    n8n_api_key=os.getenv("N8N_API_KEY", ""),
)

# ── AI Team 777 ───────────────────────────────────────────────────────────────

_OFFICE_URL = "https://ai-office-deploy.vercel.app"

# Per-chat state for multi-step interactions: {chat_id: {"step": str}}
_ai_state: dict[int, dict] = {}

# N8N tunnel is managed via tunnel_utils with name="n8n" (port 5678).
# Separate from VideoForge tunnel (name="videoforge", port 8000).


def _ai_menu_keyboard() -> telebot.types.InlineKeyboardMarkup:
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("🏢 Офіс", url=_OFFICE_URL),
        telebot.types.InlineKeyboardButton("📋 Статус агентів", callback_data="ai_agent_status"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("💬 Задача команді", callback_data="ai_task"),
        telebot.types.InlineKeyboardButton("💬 Чат з командою", callback_data="ai_chat"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🔍 Code Review",    callback_data="ai_code_review"),
        telebot.types.InlineKeyboardButton("📝 Контент",        callback_data="ai_content"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("👤 Онбординг клієнта", callback_data="ai_onboarding"),
        telebot.types.InlineKeyboardButton("🐛 Bug Report",        callback_data="ai_bug_report"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("📊 Аналітика",  callback_data="ai_analytics"),
        telebot.types.InlineKeyboardButton("⚙️ N8N Статус", callback_data="ai_n8n_status"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🔗 Tunnel URL",           callback_data="ai_ngrok_url"),
        telebot.types.InlineKeyboardButton("🔄 Перезапустити тунель", callback_data="ai_ngrok_restart"),
    )
    return kb


def _auth_cb(call: telebot.types.CallbackQuery) -> bool:
    if _vf.ALLOWED_ID and call.message.chat.id != _vf.ALLOWED_ID:
        bot.answer_callback_query(call.id, "⛔ Немає доступу")
        return False
    return True


def _n8n_post(endpoint: str, payload: dict) -> dict:
    try:
        r = requests.post(f"{N8N_URL}{endpoint}", json=payload, timeout=30)
        r.raise_for_status()
        try:
            return {"ok": True, "data": r.json()}
        except Exception:
            return {"ok": True, "data": {"text": r.text}}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "N8N не відповідає. Перевір чи запущений сервіс."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "N8N не відповів вчасно (timeout 30s)."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


def _n8n_get(endpoint: str) -> dict:
    try:
        r = requests.get(f"{N8N_URL}{endpoint}", timeout=10)
        r.raise_for_status()
        try:
            return {"ok": True, "data": r.json()}
        except Exception:
            return {"ok": True, "data": {"text": r.text}}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "N8N не відповідає. Перевір чи запущений сервіс."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "N8N не відповів вчасно (timeout 10s)."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


def _extract_text(data: object, *keys: str) -> str:
    """Try common keys to get a text value from a dict, else stringify."""
    if isinstance(data, dict):
        for k in keys:
            if k in data:
                return str(data[k])
    return str(data)


# ── /ai — main menu ───────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == BTN_AI)
@bot.message_handler(commands=["ai"])
def cmd_ai(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    bot.send_message(
        message.chat.id,
        "🤖 *AI Team 777* — головне меню\nОбери дію:",
        parse_mode="Markdown",
        reply_markup=_ai_menu_keyboard(),
    )


# ── Callback: Статус агентів ──────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_agent_status")
def cb_agent_status(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    bot.answer_callback_query(call.id, "⏳ Запит до агентів…")
    result = _n8n_get("/webhook/agent-status")
    if not result["ok"]:
        bot.send_message(call.message.chat.id, f"❌ {result['error']}")
        return
    data = result["data"]
    # Structured response: {agents: [...], total, working, idle}
    _WORKING = {"active", "ok", "running", "online", "working", "busy"}

    if isinstance(data, dict) and "agents" in data:
        agents  = data["agents"]
        total   = data.get("total", len(agents))
        working = data.get("working", 0)
        idle    = data.get("idle", total - working)

        lines = [
            "📋 *Статус AI Team 777:*",
            f"👥 Всього: {total} агентів",
            f"🟢 Працюють: {working}",
            f"⏸ Очікують: {idle}",
            "",
        ]

        # Group agents by department
        depts: dict[str, list[dict]] = {}
        for a in agents:
            depts.setdefault(a.get("dept", "Інше"), []).append(a)

        for dept, members in depts.items():
            lines.append(f"*{dept}*")
            for a in members:
                status = str(a.get("status", "idle")).lower()
                icon   = "🟢" if status in _WORKING else "⏸"
                name   = a.get("name", a.get("id", "?"))
                task   = a.get("task", "")
                suffix = f" — _{task}_" if task else ""
                lines.append(f"  {icon} {name}{suffix}")

        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:4000] + "\n…"
        bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")

    elif isinstance(data, list):
        working = sum(1 for a in data if str(a.get("status", "")).lower() in _WORKING)
        lines = [
            "📋 *Статус AI Team 777:*",
            f"👥 Всього: {len(data)} агентів",
            f"🟢 Працюють: {working}",
            f"⏸ Очікують: {len(data) - working}",
            "",
        ]
        for a in data:
            status = str(a.get("status", "?")).lower()
            icon   = "🟢" if status in _WORKING else "⏸"
            name   = a.get("name") or a.get("agent") or "?"
            lines.append(f"  {icon} {name}")
        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:4000] + "\n…"
        bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")

    elif isinstance(data, dict) and "text" in data:
        bot.send_message(call.message.chat.id, f"📋 *Статус агентів:*\n{data['text']}", parse_mode="Markdown")
    else:
        bot.send_message(call.message.chat.id, f"📋 Відповідь N8N:\n{data}")


# ── Callbacks that start multi-step flows ─────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_task")
def cb_task(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    _ai_state[call.message.chat.id] = {"step": "task"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "💬 Напиши задачу для команди:")


@bot.callback_query_handler(func=lambda c: c.data == "ai_code_review")
def cb_code_review(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    _ai_state[call.message.chat.id] = {"step": "code_review"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🔍 Надішли код або назву файлу для review:")


@bot.callback_query_handler(func=lambda c: c.data == "ai_content")
def cb_content(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    _ai_state[call.message.chat.id] = {"step": "content"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📝 Напиши тему для контенту:")


@bot.callback_query_handler(func=lambda c: c.data == "ai_onboarding")
def cb_onboarding(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    _ai_state[call.message.chat.id] = {"step": "onboarding"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "👤 Введи дані клієнта для онбордингу:")


@bot.callback_query_handler(func=lambda c: c.data == "ai_bug_report")
def cb_bug_report(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    _ai_state[call.message.chat.id] = {"step": "bug_report"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🐛 Опиши баг:")


# ── Callback: Чат з командою (session-based) ──────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_chat")
def cb_chat(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    bot.answer_callback_query(call.id)
    # Clear any pending single-step state so it doesn't intercept first chat message
    _ai_state.pop(call.message.chat.id, None)
    _ai_chat.start_session(call.from_user.id, call.message.chat.id)


@bot.message_handler(commands=["stop"])
def cmd_stop(message: telebot.types.Message) -> None:
    if not _auth(message):
        return
    if _ai_chat.is_in_session(message.from_user.id):
        _ai_chat.end_session(message.from_user.id, message.chat.id)
    else:
        bot.send_message(message.chat.id, "ℹ️ Активної сесії немає.", reply_markup=_keyboard())


# ── Callback: ngrok URL ───────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_ngrok_url")
def cb_ngrok_url(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    bot.answer_callback_query(call.id, "⏳ Читаю тунель…")
    url = _get_ngrok_url()
    if url:
        bot.send_message(
            call.message.chat.id,
            f"🔗 *Поточний Tunnel URL:*\n`{url}`",
            parse_mode="Markdown",
        )
    else:
        bot.send_message(
            call.message.chat.id,
            "❌ Тунель не запущено.\nСпробуй *🔄 Перезапустити тунель*.",
            parse_mode="Markdown",
        )


# ── Callback: Перезапустити ngrok (port 5678 for N8N) ────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_ngrok_restart")
def cb_ngrok_restart(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    bot.answer_callback_query(call.id, "⏳ Перезапускаю тунель…")
    bot.send_message(call.message.chat.id, "🔄 Перезапускаю тунель для порту 5678…")
    threading.Thread(target=_do_ai_tunnel_restart, args=(call.message.chat.id,), daemon=True).start()


def _do_ai_tunnel_restart(chat_id: int) -> None:
    """Start/restart a SEPARATE cloudflared tunnel on port 5678 for N8N.
    Uses tunnel_utils with name='n8n' — never touches VideoForge's tunnel.
    """
    try:
        _start_tunnel_cf(port=5678, name="n8n")
        log.info("AI tunnel started on 5678")
    except Exception as exc:
        bot.send_message(chat_id, f"❌ Не вдалось запустити тунель: `{exc}`", parse_mode="Markdown")
        return

    bot.send_message(chat_id, "⏳ Чекаю на тунель…")
    url = _wait_tunnel_url("n8n", attempts=20)
    if url:
        bot.send_message(
            chat_id,
            f"✅ *Тунель запущено*\n• URL: `{url}`\n• Порт: 5678",
            parse_mode="Markdown",
        )
    else:
        bot.send_message(
            chat_id,
            "⚠️ Тунель запущено, але URL ще не доступний. Спробуй *🔗 Tunnel URL* через кілька секунд.",
            parse_mode="Markdown",
        )


# ── Callback: Аналітика ───────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_analytics")
def cb_analytics(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    bot.answer_callback_query(call.id, "⏳ Генерую звіт…")
    result = _n8n_post("/webhook/analytics-report", {"type": "default", "chat_id": call.message.chat.id})
    if not result["ok"]:
        bot.send_message(call.message.chat.id, f"❌ {result['error']}")
        return
    reply = _extract_text(result["data"], "report", "message", "text")
    bot.send_message(call.message.chat.id, f"📊 *Аналітика:*\n{reply}", parse_mode="Markdown")


# ── Callback: N8N Статус ──────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "ai_n8n_status")
def cb_n8n_status(call: telebot.types.CallbackQuery) -> None:
    if not _auth_cb(call):
        return
    bot.answer_callback_query(call.id, "⏳ Перевіряю N8N…")
    try:
        r = requests.get(N8N_URL, timeout=5)
        bot.send_message(
            call.message.chat.id,
            f"✅ *N8N активний*\n• URL: `{N8N_URL}`\n• HTTP: {r.status_code}",
            parse_mode="Markdown",
        )
    except requests.exceptions.ConnectionError:
        bot.send_message(
            call.message.chat.id,
            f"❌ *N8N не відповідає*\n• URL: `{N8N_URL}`\nПеревір чи запущений сервіс на порту 5678.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        bot.send_message(call.message.chat.id, f"⚠️ N8N: `{exc}`", parse_mode="Markdown")


# ── Single-shot task handler ──────────────────────────────────────────────────

def _do_task(chat_id: int, text: str) -> None:
    """Send one task to Director pipeline and return the result to the user."""
    n8n_api_key = os.getenv("N8N_API_KEY", "")
    payload = {
        "task": text,
        "priority": "normal",
        "source": "telegram",
    }
    try:
        resp = requests.post(
            f"{N8N_URL}/webhook/director",
            json=payload,
            timeout=120,
        )

        if resp.status_code in (200, 201):
            data = resp.json()
            exec_id = data.get("executionId", "") if isinstance(data, dict) else ""
            # N8N "Respond immediately" mode: small dict with only executionId
            if exec_id and len(data) <= 2 and n8n_api_key:
                reply = _ai_chat._poll_execution(exec_id)
            elif exec_id and len(data) <= 2:
                reply = (
                    f"⏳ N8N повернув executionId={exec_id}, "
                    "але N8N_API_KEY не налаштовано для отримання результату."
                )
            else:
                reply = _ai_chat._extract(data)

        elif resp.status_code == 202:
            try:
                exec_id = resp.json().get("executionId", "")
            except Exception:
                exec_id = ""
            if exec_id and n8n_api_key:
                reply = _ai_chat._poll_execution(exec_id)
            elif exec_id:
                reply = (
                    f"⏳ Завдання прийнято (execution #{exec_id}), "
                    "але N8N_API_KEY не налаштовано для отримання результату."
                )
            else:
                reply = "⏳ Завдання прийнято. Результат недоступний (немає executionId у відповіді)."

        else:
            reply = f"❌ Director pipeline: HTTP {resp.status_code}\n{resp.text[:300]}"

    except requests.exceptions.Timeout:
        reply = "⏰ Таймаут (120 с). Завдання, можливо, прийнято — перевір N8N."
    except requests.exceptions.ConnectionError:
        reply = "❌ N8N не відповідає (localhost:5678). Перевір чи запущений сервіс."
    except Exception as exc:
        reply = f"❌ Помилка: {str(exc)[:300]}"

    _ai_chat._send_split(chat_id, f"📋 *Відповідь команди:*\n{reply}")


# ── Message handler for multi-step AI state inputs ────────────────────────────
# Registered LAST so it never intercepts VideoForge button presses.

@bot.message_handler(func=lambda m: m.chat.id in _ai_state and not _ai_chat.is_in_session(m.from_user.id))
def handle_ai_input(message: telebot.types.Message) -> None:
    if not _auth(message):
        _ai_state.pop(message.chat.id, None)
        return
    state = _ai_state.pop(message.chat.id, None)
    if not state:
        return
    step = state["step"]
    text = message.text or ""

    if step == "task":
        bot.send_message(message.chat.id, "⏳ Надсилаю задачу команді…")
        threading.Thread(
            target=_do_task,
            args=(message.chat.id, text),
            daemon=True,
        ).start()
        return

    elif step == "code_review":
        bot.send_message(message.chat.id, "⏳ Запускаю code review…")
        result = _n8n_post("/webhook/auto-review", {"code": text, "chat_id": message.chat.id})
        if not result["ok"]:
            bot.send_message(message.chat.id, f"❌ {result['error']}")
        else:
            reply = _extract_text(result["data"], "review", "message", "text")
            bot.send_message(message.chat.id, f"🔍 *Code Review:*\n{reply}", parse_mode="Markdown")

    elif step == "content":
        bot.send_message(message.chat.id, "⏳ Генерую контент…")
        result = _n8n_post("/webhook/content-pipeline", {"topic": text, "chat_id": message.chat.id})
        if not result["ok"]:
            bot.send_message(message.chat.id, f"❌ {result['error']}")
        else:
            reply = _extract_text(result["data"], "content", "message", "text")
            bot.send_message(message.chat.id, f"📝 *Контент:*\n{reply}", parse_mode="Markdown")

    elif step == "onboarding":
        bot.send_message(message.chat.id, "⏳ Запускаю онбординг…")
        result = _n8n_post("/webhook/client-onboarding", {"client": text, "chat_id": message.chat.id})
        if not result["ok"]:
            bot.send_message(message.chat.id, f"❌ {result['error']}")
        else:
            reply = _extract_text(result["data"], "plan", "message", "text")
            bot.send_message(message.chat.id, f"👤 *Онбординг клієнта:*\n{reply}", parse_mode="Markdown")

    elif step == "bug_report":
        bot.send_message(message.chat.id, "⏳ Відправляю bug report…")
        result = _n8n_post("/webhook/bug-triage", {"bug": text, "chat_id": message.chat.id})
        if not result["ok"]:
            bot.send_message(message.chat.id, f"❌ {result['error']}")
        else:
            reply = _extract_text(result["data"], "triage", "message", "text")
            bot.send_message(message.chat.id, f"🐛 *Bug Triage:*\n{reply}", parse_mode="Markdown")

    # "chat" step is no longer used — handled by _ai_chat session system


# ── Register session handler LAST (must be after all other handlers) ──────────
# This handler intercepts every message from users in an active AI chat session.
# It is registered last so that VideoForge keyboard buttons still take priority.
_ai_chat.register_handlers()
