"""
Refresh loop and cache update helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.observability.logging import get_logger
from funding_bot.services.market_data.models import (
    FundingSnapshot,
    OrderbookSnapshot,
    PriceSnapshot,
)

logger = get_logger(__name__)


async def _discover_common_symbols(self) -> None:
    """Find symbols available on both exchanges."""
    try:
        lighter_markets = await self.lighter.load_markets()
        x10_markets = await self.x10.load_markets()

        lighter_symbols = set(lighter_markets.keys())
        x10_symbols = set(x10_markets.keys())

        # X10 uses 'SYMBOL-USD' format, Lighter uses 'SYMBOL'
        # Normalize X10 symbols to Lighter format for comparison
        x10_base_symbols = {s.replace("-USD", "") for s in x10_symbols}

        # Find common base symbols
        common_base = lighter_symbols & x10_base_symbols

        # Store both formats for each common symbol
        # Use Lighter format as canonical (e.g., 'ETH')
        self._common_symbols = common_base

        # Build symbol mapping: lighter_symbol -> x10_symbol
        self._symbol_map_to_x10 = {}
        for symbol in common_base:
            x10_symbol = f"{symbol}-USD"
            if x10_symbol in x10_symbols:
                self._symbol_map_to_x10[symbol] = x10_symbol

        # Remove blacklisted symbols
        blacklist = self.settings.trading.blacklist_symbols
        # Also check for -USD variants in blacklist
        blacklist_normalized = {s.replace("-USD", "") for s in blacklist}
        self._common_symbols -= blacklist_normalized

        logger.info(
            f"Common symbols: {len(self._common_symbols)} "
            f"(Lighter: {len(lighter_symbols)}, X10: {len(x10_symbols)}, "
            f"Blacklisted: {len(blacklist)})"
        )

        if self._common_symbols:
            logger.debug(f"Tradeable symbols: {sorted(self._common_symbols)[:10]}...")

    except Exception as e:
        logger.exception(f"Failed to discover common symbols: {e}")


async def _refresh_loop(self) -> None:
    """Background loop to refresh market data."""
    refresh_interval = float(self.settings.websocket.health_check_interval)
    max_refresh_seconds = max(60.0, refresh_interval * 10)

    while self._running:
        started = datetime.now(UTC)
        self._last_refresh_started = started
        self._last_refresh_attempt = started
        self._refresh_in_progress = True
        try:
            await asyncio.wait_for(self.refresh_all(), timeout=max_refresh_seconds)
            self._last_refresh_error = None
        except Exception as e:
            self._last_refresh_error = str(e)
            logger.exception(f"Market data refresh failed: {e}")
        finally:
            finished = datetime.now(UTC)
            self._last_refresh_duration_seconds = (finished - started).total_seconds()
            self._refresh_in_progress = False

            # Log health changes with context (reduces "flappy" mystery).
            healthy_now = self.is_healthy()
            if self._last_reported_health is None:
                self._last_reported_health = healthy_now
            elif healthy_now != self._last_reported_health:
                age = (
                    (datetime.now(UTC) - self._last_successful_refresh).total_seconds()
                    if self._last_successful_refresh
                    else None
                )
                logger.warning(
                    "MarketData health changed: "
                    f"{self._last_reported_health} -> {healthy_now} "
                    f"(duration={self._last_refresh_duration_seconds:.2f}s, "
                    f"age={age}, lighter_ok={self._lighter_healthy}, x10_ok={self._x10_healthy}, "
                    f"err={self._last_refresh_error})"
                )
                self._last_reported_health = healthy_now

        try:
            await asyncio.sleep(refresh_interval)
        except asyncio.CancelledError:
            break


async def refresh_all(self) -> None:
    """
    Refresh all market data.

    PREMIUM-OPTIMIZED: Uses parallel symbol refresh with controlled concurrency
    to leverage Lighter Premium's 24,000 requests/minute capacity while
    respecting WebSocket limits (200 messages/minute).
    """
    if not self._common_symbols:
        await self._discover_common_symbols()

    # Step 1: Batch refresh both exchanges (single API call each)
    # This populates their internal caches for funding/prices/orderbooks
    lighter_ok, x10_ok = await self._batch_refresh_adapters()

    # Step 2: Copy cached data to our snapshots (NO API calls here)
    # This just reads from adapter caches - completely sync/fast
    # PREMIUM-OPTIMIZED: Parallelize symbol refresh with controlled concurrency
    await self._refresh_symbols_parallel(self._common_symbols)

    # Refresh is considered successful if at least one adapter refreshed its caches.
    if lighter_ok or x10_ok:
        now = datetime.now(UTC)
        self._last_successful_refresh = now
        self._last_refresh = now


async def _refresh_symbols_parallel(
    self,
    symbols: set[str],
    batch_size: int = 20,
) -> None:
    """
    Refresh multiple symbols in parallel with controlled concurrency.

    Args:
        symbols: Set of symbols to refresh
        batch_size: Maximum concurrent refresh operations (Premium-safe)

    NOTE: With Lighter Premium (24,000 req/min), we can handle much more
    concurrency than Standard (60 req/min). However, we still need to respect
    WebSocket message limits (200 msg/min) and keep some headroom.

    Conservative defaults for production safety:
    - batch_size=20: Process 20 symbols concurrently
    - Each _refresh_symbol reads from cache (no API calls)
    - Purely CPU-bound (data structure operations)
    """
    if not symbols:
        return

    # Convert to list for batch processing
    symbol_list = list(symbols)

    # Process in batches to avoid overwhelming memory/CPU
    for i in range(0, len(symbol_list), batch_size):
        batch = symbol_list[i:i + batch_size]

        # Process batch in parallel
        await asyncio.gather(*[
            self._refresh_symbol(symbol) for symbol in batch
        ], return_exceptions=True)


async def _batch_refresh_adapters(self) -> tuple[bool, bool]:
    """Batch refresh both exchange adapters."""
    now = datetime.now(UTC)
    lighter_ok = False
    x10_ok = False

    ws = self.settings.websocket
    streams_enabled = bool(getattr(ws, "market_data_streams_enabled", False))
    min_interval = float(getattr(ws, "market_data_batch_refresh_interval_seconds", Decimal("30.0")) or Decimal("30.0"))
    min_interval = max(0.0, min(min_interval, 3600.0))

    # If public streams are enabled, we can refresh adapter caches less frequently
    # (funding/metadata still need occasional refresh).
    if streams_enabled and self._last_batch_refresh_at and min_interval > 0:
        age = (now - self._last_batch_refresh_at).total_seconds()
        if age < min_interval:
            return self._lighter_healthy, self._x10_healthy

    # X10 has refresh_all_market_data that fetches everything in one call
    if hasattr(self.x10, "refresh_all_market_data"):
        try:
            await self.x10.refresh_all_market_data()
            x10_ok = True
            self._x10_healthy = True
            self._last_x10_success = now
        except Exception as e:
            self._x10_healthy = False
            logger.warning(f"X10 batch refresh failed: {e}")

    # Lighter has refresh_all_market_data (funding), but no batch prices.
    if hasattr(self.lighter, "refresh_all_market_data"):
        try:
            await self.lighter.refresh_all_market_data()
            lighter_ok = True
            self._lighter_healthy = True
            self._last_lighter_success = now
        except Exception as e:
            self._lighter_healthy = False
            logger.warning(f"Lighter batch refresh failed: {e}")

    # Optional: trickle real Lighter prices without blocking the main refresh.
    # This prevents health flapping when per-market calls get slow.
    if getattr(self.lighter, "_auth_token", None):
        self._schedule_lighter_mark_price_trickle()

    if lighter_ok or x10_ok:
        self._last_batch_refresh_at = now

    return lighter_ok, x10_ok


async def _refresh_symbol(self, symbol: str) -> None:
    """
    Refresh data for a single symbol.

    NOTE: This now reads from adapter caches (populated by batch refresh)
    instead of making individual API calls. This prevents rate limiting.
    """
    try:
        # Get X10 symbol (e.g., 'ETH' -> 'ETH-USD')
        x10_symbol = self._symbol_map_to_x10.get(symbol, f"{symbol}-USD")
        now = datetime.now(UTC)

        # Read prices from adapter caches (NO API calls - caches populated by batch refresh)
        lighter_price = self.lighter._price_cache.get(symbol, Decimal("0"))
        x10_price = self.x10._price_cache.get(x10_symbol, Decimal("0"))

        # Fallback: If Lighter has no price (because batch API doesn't return it),
        # use X10 price as proxy for scanning purposes.
        # Real trading logic calls get_fresh_price() which fetches real price.
        if lighter_price == Decimal("0") and x10_price > Decimal("0"):
            lighter_price = x10_price

        self._prices[symbol] = PriceSnapshot(
            symbol=symbol,
            lighter_price=lighter_price,
            x10_price=x10_price,
            lighter_updated=now if lighter_price else None,
            x10_updated=now if x10_price else None,
        )

        # Read funding rates from adapter caches
        lighter_funding = self.lighter._funding_cache.get(symbol)
        x10_funding = self.x10._funding_cache.get(x10_symbol)

        self._funding[symbol] = FundingSnapshot(
            symbol=symbol,
            lighter_rate=lighter_funding,
            x10_rate=x10_funding,
        )

        # Read orderbooks from adapter caches
        lighter_book = self.lighter._orderbook_cache.get(symbol, {})
        x10_book = self.x10._orderbook_cache.get(x10_symbol, {})

        prev = self._orderbooks.get(symbol)

        def _d(book: dict, key: str) -> Decimal:
            v = book.get(key, Decimal("0"))
            if isinstance(v, Decimal):
                return v
            with contextlib.suppress(Exception):
                return Decimal(str(v))
            return Decimal("0")

        def _merge_exchange(
            *,
            book: dict,
            prev_bid: Decimal,
            prev_ask: Decimal,
            prev_bid_qty: Decimal,
            prev_ask_qty: Decimal,
        ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
            bid = _d(book, "best_bid")
            ask = _d(book, "best_ask")
            bid_qty = _d(book, "bid_qty")
            ask_qty = _d(book, "ask_qty")

            # Never degrade: keep last-known-good values if the adapter cache is partial/zero.
            if bid <= 0 and prev_bid > 0:
                bid = prev_bid
            if ask <= 0 and prev_ask > 0:
                ask = prev_ask
            if bid_qty <= 0 and prev_bid_qty > 0:
                bid_qty = prev_bid_qty
            if ask_qty <= 0 and prev_ask_qty > 0:
                ask_qty = prev_ask_qty

            # Sanity: if quotes are inverted, prefer previous quotes (or drop to 0 if none).
            if bid > 0 and ask > 0 and bid >= ask:
                if prev_bid > 0 and prev_ask > 0 and prev_bid < prev_ask:
                    bid, ask = prev_bid, prev_ask
                else:
                    bid, ask = Decimal("0"), Decimal("0")
            return bid, ask, bid_qty, ask_qty

        p = prev or OrderbookSnapshot(symbol=symbol)
        l_bid, l_ask, l_bq, l_aq = _merge_exchange(
            book=lighter_book,
            prev_bid=p.lighter_bid,
            prev_ask=p.lighter_ask,
            prev_bid_qty=p.lighter_bid_qty,
            prev_ask_qty=p.lighter_ask_qty,
        )
        x_bid, x_ask, x_bq, x_aq = _merge_exchange(
            book=x10_book,
            prev_bid=p.x10_bid,
            prev_ask=p.x10_ask,
            prev_bid_qty=p.x10_bid_qty,
            prev_ask_qty=p.x10_ask_qty,
        )

        # Preserve "fresh depth" timestamps from any previous successful get_fresh_orderbook().
        self._orderbooks[symbol] = OrderbookSnapshot(
            symbol=symbol,
            lighter_bid=l_bid,
            lighter_ask=l_ask,
            x10_bid=x_bid,
            x10_ask=x_ask,
            lighter_bid_qty=l_bq,
            lighter_ask_qty=l_aq,
            x10_bid_qty=x_bq,
            x10_ask_qty=x_aq,
            lighter_updated=p.lighter_updated,
            x10_updated=p.x10_updated,
        )

    except Exception as e:
        logger.warning(f"Failed to refresh {symbol}: {e}")
