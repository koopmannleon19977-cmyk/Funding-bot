"""
Market data service package (facade).
"""

from __future__ import annotations

from funding_bot.services.market_data.models import (
    FundingSnapshot,
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
    PriceSnapshot,
)
from funding_bot.services.market_data.service import MarketDataService

__all__ = [
    "MarketDataService",
    "PriceSnapshot",
    "FundingSnapshot",
    "OrderbookSnapshot",
    "OrderbookDepthSnapshot",
]
