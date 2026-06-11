"""Notifications: Telegram (reaches your phone anywhere) + macOS (local).

Telegram setup (one-time):
  1. In Telegram, message @BotFather -> /newbot -> copy the token.
  2. Message your new bot anything, then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy the chat "id".
  3. Put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
Everything no-ops cleanly if not configured.
"""
from __future__ import annotations

import subprocess
from typing import List

import requests

from . import config


def telegram_configured() -> bool:
    return bool(config.get_env("TELEGRAM_BOT_TOKEN") and config.get_env("TELEGRAM_CHAT_ID"))


def send_telegram(text: str) -> bool:
    token = config.get_env("TELEGRAM_BOT_TOKEN")
    chat_id = config.get_env("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False


def send_macos(title: str, message: str) -> bool:
    msg = message.replace('"', "'").replace("\n", " ")
    ttl = title.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{ttl}"'],
            check=False, capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, OSError):
        return False


def notify(title: str, body: str, channels: str = "auto") -> List[str]:
    """Send to the chosen channels. 'auto' = Telegram if configured, plus macOS.
    Returns the list of channels that accepted the message.
    """
    sent = []
    if channels in ("auto", "telegram") and telegram_configured():
        if send_telegram(f"*{title}*\n{body}"):
            sent.append("telegram")
    if channels in ("auto", "macos"):
        if send_macos(title, body):
            sent.append("macos")
    return sent
