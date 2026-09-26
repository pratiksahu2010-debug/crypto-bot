"""
tg_core.py - robust Telegram sender shared by every bot.

FIXES
  * Markdown parse errors (symbols such as M&M, MCDOWELL-N, names with '_') used to make
    Telegram reply 400 -> the alert was silently lost. Now retries as plain text.
  * 429 flood-control is honoured (retry_after).
  * A global spacing gate keeps at least ALERT_SPACING_SECONDS between two messages, so a
    burst of signals arrives one-by-one instead of all at the same second.
"""

import logging
import threading
import time

import requests

import config

log = logging.getLogger("telegram")
_lock = threading.Lock()
_last_sent = 0.0
URL = "https://api.telegram.org/bot{token}/sendMessage"


def _post(token, chat_id, text, retries):
    last_err = None
    parse_mode = "Markdown"
    for attempt in range(retries + 1):
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(URL.format(token=token), json=payload, timeout=10)
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            if resp.status_code == 200 and data.get("ok"):
                return str(data["result"]["message_id"])
            desc = str(data.get("description", resp.text[:200]))
            last_err = f"HTTP {resp.status_code}: {desc}"
            if resp.status_code == 400 and "parse entities" in desc.lower() and parse_mode:
                parse_mode = None                    # resend immediately as plain text
                continue
            if resp.status_code == 429:
                wait = float((data.get("parameters") or {}).get("retry_after", 3))
                time.sleep(min(wait, 30))
                continue
            if resp.status_code in (401, 403, 404) or (resp.status_code == 400 and "chat not found" in desc.lower()):
                break                                # misconfiguration - retrying will not help
        except Exception as e:
            last_err = e
        log.warning(f"[TELEGRAM] send attempt {attempt + 1} failed: {last_err}")
        time.sleep(1.5)
    log.error(f"[TELEGRAM] All attempts failed: {last_err}")
    return ""


def send(token: str, chat_id: str, text: str, retries: int = 2) -> str:
    """Returns message_id ('' on failure)."""
    global _last_sent
    if not token or not chat_id:
        log.warning("[TELEGRAM] Missing token/chat_id - message not sent")
        return ""
    text = text[:4000]
    spacing = getattr(config, "ALERT_SPACING_SECONDS", 8)
    with _lock:
        wait = _last_sent + spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        mid = _post(token, chat_id, text, retries)
        _last_sent = time.time()
    return mid
