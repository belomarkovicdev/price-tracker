"""Telegram channel notifier (and a LogNotifier fallback for dry runs)."""

from __future__ import annotations

import logging

import requests

from ..config import TelegramConfig
from .base import Notification, Notifier

log = logging.getLogger(__name__)


class LogNotifier(Notifier):
    """Fallback when Telegram isn't configured: just log the alert. Handy for
    dry runs / testing without a bot token."""

    def send(self, note: Notification) -> bool:
        log.info("ALERT (dry-run):\n%s", note.as_text())
        return True


class TelegramNotifier(Notifier):
    def __init__(self, cfg: TelegramConfig) -> None:
        self.cfg = cfg
        self._url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"

    def send(self, note: Notification) -> bool:
        try:
            resp = requests.post(
                self._url,
                json={
                    "chat_id": self.cfg.chat_id,
                    "text": note.as_text(),
                    "disable_web_page_preview": False,
                },
                timeout=20,
            )
            if resp.status_code != 200:
                log.error("Telegram send failed: HTTP %s %s",
                          resp.status_code, resp.text[:300])
                return False
            return True
        except requests.RequestException as exc:
            log.error("Telegram send error: %s", exc)
            return False


def build_notifier(cfg: TelegramConfig) -> Notifier:
    if cfg.configured:
        log.info("Telegram notifier enabled (chat %s).", cfg.chat_id)
        return TelegramNotifier(cfg)
    log.warning(
        "Telegram not configured (missing token/chat or disabled) — "
        "using dry-run LogNotifier. Set TELEGRAM_BOT_TOKEN and "
        "TELEGRAM_CHAT_ID in .env to send real messages."
    )
    return LogNotifier()
