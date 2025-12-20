# Compatibility shim - This file has been moved to infrastructure/messaging/
# Import from new location for better organization
from src.infrastructure.messaging.telegram_bot import (
    TelegramBot,
    get_telegram_bot,
)

__all__ = ['TelegramBot', 'get_telegram_bot']
