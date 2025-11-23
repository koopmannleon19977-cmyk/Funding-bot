# src/telegram_bot.py
import asyncio
import logging
import aiohttp
from typing import Optional
import config

logger = logging.getLogger(__name__)

class TelegramBot:
    """
    Async Telegram Alerting.
    Sends critical errors and trade notifications to your phone.
    """
    def __init__(self):
        self.token = getattr(config, 'TELEGRAM_BOT_TOKEN', '')
        self.chat_id = getattr(config, 'TELEGRAM_CHAT_ID', '')
        self.enabled = bool(self.token and self.chat_id)
        self.queue = asyncio.Queue()
        self.running = False
        self._worker_task = None
        
        if not self.enabled:
            logger.info(" Telegram Bot disabled (Token/ChatID missing).")

    async def start(self):
        if not self.enabled: return
        self.running = True
        self._worker_task = asyncio.create_task(self._worker())
        await self.send_message("🤖 **Funding Bot V4 Started**\nMulti-Account & Realtime active.")

    async def stop(self):
        self.running = False
        if self._worker_task:
            await self.queue.put(None)
            await self._worker_task

    async def send_message(self, text: str, vital: bool = False):
        if not self.enabled: return
        try:
            # Drop non-vital messages if queue is full to prevent memory leak
            if self.queue.qsize() > 50 and not vital:
                return
            await self.queue.put(text)
        except Exception:
            pass

    async def send_trade_alert(self, symbol: str, side: str, size: float, pnl: float = 0.0):
        if not self.enabled: return
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} **TRADE CLOSED: {symbol}**\n"
            f"Side: {side}\n"
            f"Size: ${size:.0f}\n"
            f"PnL: ${pnl:.2f}\n"
        )
        await self.send_message(msg, vital=True)

    async def send_error(self, error_msg: str):
        if not self.enabled: return
        await self.send_message(f"⚠️ **ERROR**\n`{error_msg}`", vital=True)

    async def _worker(self):
        while self.running:
            try:
                msg = await self.queue.get()
                if msg is None: break
                
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": msg,
                    "parse_mode": "Markdown"
                }
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=5) as resp:
                        if resp.status != 200:
                            logger.error(f"Telegram Send Failed: {resp.status}")
                
                await asyncio.sleep(0.5) # Rate limit
                self.queue.task_done()
                
            except Exception as e:
                logger.error(f"Telegram Worker Error: {e}")
                await asyncio.sleep(5)

_bot = None

def get_telegram_bot() -> TelegramBot:
    global _bot
    if _bot is None:
        _bot = TelegramBot()
    return _bot