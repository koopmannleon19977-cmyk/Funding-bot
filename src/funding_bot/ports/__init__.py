"""
Ports: Abstract interfaces for external dependencies.

This follows the Ports & Adapters (Hexagonal) architecture pattern.
Business logic depends only on these interfaces, not on concrete implementations.
"""

from funding_bot.ports.event_bus import EventBusPort
from funding_bot.ports.exchange import ExchangePort
from funding_bot.ports.store import TradeStorePort

__all__ = ["ExchangePort", "TradeStorePort", "EventBusPort"]
