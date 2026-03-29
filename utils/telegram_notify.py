"""VideoForge — Telegram notifications for job events."""
import os
import logging
import httpx

log = logging.getLogger("telegram_notify")

async def notify_telegram(text: str) -> None:
    """Send a message to the operator's Telegram chat. Silently fails if not configured."""
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_ALLOWED_CHAT_ID", "").strip()
    if not token or not chat_id:
        return  # not configured — skip silently

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                log.warning("Telegram notify failed: HTTP %d — %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Telegram notify failed: %s", exc)
