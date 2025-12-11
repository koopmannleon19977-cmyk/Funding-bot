# src/data/__init__.py
"""
Data management module for market data aggregation.

This module provides:
- Orderbook data aggregation from WebSocket and REST
- Price data caching and staleness tracking
"""

from src.data.orderbook_provider import (
    OrderbookProvider,
    OrderbookSnapshot,
    get_orderbook_provider,
)

__all__ = [
    "OrderbookProvider",
    "OrderbookSnapshot",
    "get_orderbook_provider",
]

