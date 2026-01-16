"""
Telegram Adapter.

Sends notifications via Telegram Bot API.
"""

from __future__ import annotations

from html import escape as _html_escape

import aiohttp

from funding_bot.config.settings import TelegramSettings
from funding_bot.observability.logging import get_logger
from funding_bot.ports.notification import NotificationPort

logger = get_logger(__name__)

_ALLOWED_HTML_TAGS: tuple[str, ...] = (
    "<b>", "</b>",
    "<i>", "</i>",
    "<u>", "</u>",
    "<s>", "</s>",
    "<code>", "</code>",
    "<pre>", "</pre>",
)


def _sanitize_html_message(message: str) -> str:
    """
    Escape any user/exception content so Telegram HTML parse_mode can't break.

    We intentionally allow a very small tag set used by NotificationService.
    Everything else is treated as plain text.
    """
    escaped = _html_escape(message, quote=False)
    for tag in _ALLOWED_HTML_TAGS:
        escaped = escaped.replace(_html_escape(tag, quote=False), tag)
    return escaped


def _looks_like_html_parse_error(response_text: str) -> bool:
    return "can't parse entities" in response_text.lower()


class TelegramAdapter(NotificationPort):
    """Telegram implementation of NotificationPort."""

    def __init__(self, settings: TelegramSettings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._base_url = f"https://api.telegram.org/bot{settings.bot_token}"

    async def start(self) -> None:
        """Initialize HTTP session."""
        if not self.settings.enabled:
            return

        self._session = aiohttp.ClientSession()
        # Verify token by calling getMe
        try:
            async with self._session.get(f"{self._base_url}/getMe") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    bot_name = data.get("result", {}).get("username", "Unknown")
                    logger.info(f"Telegram connected as @{bot_name}")
                else:
                    logger.error(f"Telegram auth failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Telegram connection check failed: {e}")

    async def stop(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def send_message(self, message: str) -> bool:
        """Send message via Telegram."""
        if not self.settings.enabled or not self.settings.bot_token or not self.settings.chat_id:
            return False

        if not self._session or self._session.closed:
             # Lazy init or restart if closed
            self._session = aiohttp.ClientSession()

        try:
            safe_message = _sanitize_html_message(message)
            payload = {
                "chat_id": self.settings.chat_id,
                "text": safe_message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }

            async with self._session.post(f"{self._base_url}/sendMessage", json=payload) as resp:
                if resp.status == 200:
                    return True
                else:
                    text = await resp.text()
                    # If HTML parsing fails due to unexpected '<...>' in exception strings,
                    # retry as plain text so alerts still arrive.
                    if resp.status == 400 and _looks_like_html_parse_error(text):
                        logger.warning("Telegram HTML parse failed (400). Retrying as plain text.")
                        fallback_payload = {
                            "chat_id": self.settings.chat_id,
                            "text": message,
                            "disable_web_page_preview": True,
                        }
                        async with self._session.post(
                            f"{self._base_url}/sendMessage",
                            json=fallback_payload,
                        ) as resp2:
                            if resp2.status == 200:
                                return True
                            text2 = await resp2.text()
                            logger.error(f"Telegram send failed ({resp2.status}): {text2}")
                            return False

                    logger.error(f"Telegram send failed ({resp.status}): {text}")
                    return False

        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False
