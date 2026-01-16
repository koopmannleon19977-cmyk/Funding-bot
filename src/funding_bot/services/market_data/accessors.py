"""
Public API helpers for MarketDataService.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import Exchange, MarketInfo
from funding_bot.services.market_data.models import (
    FundingSnapshot,
    OrderbookSnapshot,
    PriceSnapshot,
)


def get_common_symbols(self) -> list[str]:
    """Get list of symbols available on both exchanges."""
    return list(self._common_symbols)


def get_price(self, symbol: str) -> PriceSnapshot | None:
    """Get cached price snapshot."""
    with contextlib.suppress(Exception):
        self._hydrate_symbol_from_adapter_cache(symbol)
    return self._prices.get(symbol)


def get_funding(self, symbol: str) -> FundingSnapshot | None:
    """Get cached funding snapshot."""
    return self._funding.get(symbol)


def get_orderbook(self, symbol: str) -> OrderbookSnapshot | None:
    """Get cached orderbook snapshot."""
    with contextlib.suppress(Exception):
        self._hydrate_symbol_from_adapter_cache(symbol)
    return self._orderbooks.get(symbol)


def is_healthy(self) -> bool:
    """Check if market data is healthy."""
    if not self._last_successful_refresh:
        # If refresh is in progress, allow a short warmup window.
        if self._refresh_in_progress and self._last_refresh_started:
            warmup = max(15.0, float(self.settings.websocket.health_check_interval) * 3)
            age = (datetime.now(UTC) - self._last_refresh_started).total_seconds()
            return age < warmup and (self._lighter_healthy or self._x10_healthy)
        return False

    age = (datetime.now(UTC) - self._last_successful_refresh).total_seconds()
    max_age = max(30.0, float(self.settings.websocket.health_check_interval) * 6)

    return age < max_age and (self._lighter_healthy or self._x10_healthy)


def get_market_info(self, symbol: str, exchange: Exchange) -> MarketInfo | None:
    """Get market info from appropriate adapter."""
    # This is sync because market info is cached after load_markets
    if exchange == Exchange.LIGHTER:
        return self.lighter._markets.get(symbol)  # type: ignore
    # X10 uses -USD suffix
    x10_symbol = self.get_x10_symbol(symbol)
    return self.x10._markets.get(x10_symbol)  # type: ignore


def get_x10_symbol(self, lighter_symbol: str) -> str:
    """Convert Lighter symbol to X10 format (e.g., 'ETH' -> 'ETH-USD')."""
    return self._symbol_map_to_x10.get(lighter_symbol, f"{lighter_symbol}-USD")


def get_lighter_symbol(self, x10_symbol: str) -> str:
    """Convert X10 symbol to Lighter format (e.g., 'ETH-USD' -> 'ETH')."""
    return x10_symbol.replace("-USD", "")


def get_fee_schedule(self, symbol: str) -> dict[str, Decimal]:
    """
    Get fee schedule for a symbol (from cache, loaded at startup).

    Returns unified dict with keys:
    - lighter_maker, lighter_taker
    - x10_maker, x10_taker

    Note: Fees are loaded once at startup and cached.
    They don't change during runtime, so no API calls needed.
    """
    # Try cache first (loaded at startup)
    cached = self._fee_cache.get(symbol)
    if cached:
        return cached

    # Fallback to configured defaults (shouldn't happen if startup succeeded)
    return {
        "lighter_maker": self.settings.trading.maker_fee_lighter,
        "lighter_taker": self.settings.trading.taker_fee_lighter,
        "x10_maker": self.settings.trading.maker_fee_x10,
        "x10_taker": self.settings.trading.taker_fee_x10,
    }
