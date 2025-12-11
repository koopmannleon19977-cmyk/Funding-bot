"""
Orderbook Data Provider - Aggregates orderbook data from WebSocket and REST

This module provides a unified interface for orderbook data, handling:
- WebSocket orderbook updates (real-time)
- REST API fallback when WebSocket data is stale
- Staleness tracking and caching

References:
- Lighter WebSocket: https://apidocs.lighter.xyz/docs/websocket-reference
- Lighter REST OrderApi: getOrderBookDetails(marketIndex)
- X10 WebSocket: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
from decimal import Decimal
import time
import asyncio
import logging

logger = logging.getLogger(__name__)


@dataclass
class OrderbookSnapshot:
    """Complete orderbook snapshot for a symbol"""
    symbol: str
    exchange: str  # "lighter" or "x10"
    bids: List[Tuple[Decimal, Decimal]]  # [(price, size), ...] sorted desc by price
    asks: List[Tuple[Decimal, Decimal]]  # [(price, size), ...] sorted asc by price
    timestamp: float  # Unix timestamp
    sequence: Optional[int] = None  # Sequence number for detecting gaps
    
    @property
    def is_valid(self) -> bool:
        """Check if snapshot has minimum data"""
        return len(self.bids) > 0 or len(self.asks) > 0
        
    @property
    def age_seconds(self) -> float:
        """Get age of snapshot in seconds"""
        return time.time() - self.timestamp
        
    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0][0] if self.bids else None
        
    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0][0] if self.asks else None
        
    @property
    def spread_percent(self) -> Optional[Decimal]:
        if self.best_bid and self.best_ask and self.best_bid > 0:
            return ((self.best_ask - self.best_bid) / self.best_bid) * 100
        return None
        
    @property
    def bid_depth_usd(self) -> Decimal:
        return sum(p * s for p, s in self.bids)
        
    @property
    def ask_depth_usd(self) -> Decimal:
        return sum(p * s for p, s in self.asks)


class OrderbookProvider:
    """
    Provides orderbook data with staleness tracking and validation.
    
    Integrates with WebSocket manager for real-time updates and
    falls back to REST API when WebSocket data is stale.
    
    CRITICAL: After WebSocket reconnect, orderbooks MUST be fetched via REST
    before processing WebSocket deltas. WebSocket only sends deltas, not snapshots!
    
    Reference: https://apidocs.lighter.xyz/docs/websocket-reference
    """
    
    # ═══════════════════════════════════════════════════════════════
    # Reconnect cooldown constants
    # ═══════════════════════════════════════════════════════════════
    RECONNECT_COOLDOWN_SECONDS = 8.0  # Wait time after reconnect before allowing trades (increased from 5)
    STALENESS_THRESHOLD_SECONDS = 10.0  # Orderbook considered stale after this time
    
    # ═══════════════════════════════════════════════════════════════
    # Crossed Book Detection
    # ═══════════════════════════════════════════════════════════════
    MAX_CROSSED_BOOK_RETRIES = 3  # Max REST retries when crossed book detected
    CROSSED_BOOK_RETRY_DELAY = 0.5  # Delay between retries
    
    def __init__(
        self,
        lighter_adapter=None,
        x10_adapter=None,
        ws_manager=None,
        max_staleness_seconds: float = 5.0,
        rest_fallback_enabled: bool = True,
    ):
        self.lighter_adapter = lighter_adapter
        self.x10_adapter = x10_adapter
        self.ws_manager = ws_manager
        self.max_staleness_seconds = max_staleness_seconds
        self.rest_fallback_enabled = rest_fallback_enabled
        
        # Orderbook cache
        self._lighter_orderbooks: Dict[str, OrderbookSnapshot] = {}
        self._x10_orderbooks: Dict[str, OrderbookSnapshot] = {}
        
        # ═══════════════════════════════════════════════════════════════
        # Validity and reconnect tracking
        # ═══════════════════════════════════════════════════════════════
        self._is_valid: Dict[str, bool] = {}  # Per-symbol validity flags
        self._reconnect_cooldown_until: float = 0.0  # Unix timestamp when cooldown ends
        
        # ═══════════════════════════════════════════════════════════════
        # Crossed Book Tracking - prevent repeated failures on same symbol
        # ═══════════════════════════════════════════════════════════════
        self._crossed_book_counts: Dict[str, int] = {}  # Count of consecutive crossed books per symbol
        self._crossed_book_blacklist_until: Dict[str, float] = {}  # Temporary blacklist for symbols
        
        # Last REST fetch timestamps (rate limiting)
        self._last_rest_fetch: Dict[str, float] = {}
        self._rest_cooldown = 1.0  # Minimum 1s between REST calls per symbol
    
    # ═══════════════════════════════════════════════════════════════
    # Reconnect Cooldown and Invalidation Methods
    # ═══════════════════════════════════════════════════════════════
    
    def invalidate_all(self, reason: str = "reconnect", exchange: Optional[str] = None):
        """
        Invalidate all orderbook caches after reconnect.
        
        CRITICAL: After WebSocket reconnect, we MUST:
        1. Clear all cached orderbook data
        2. Set cooldown to ignore WebSocket deltas
        3. Force REST snapshot fetch on next access
        
        This prevents crossed books caused by:
        - Applying deltas without valid base snapshot
        - Missing deltas during disconnect window
        - Out-of-order messages after reconnect
        
        Args:
            reason: Reason for invalidation (for logging)
            exchange: Optional - only invalidate "lighter" or "x10" orderbooks
        """
        logger.warning(f"🔄 Invalidating orderbooks: {reason}")
        
        if exchange is None or exchange == "lighter":
            # Clear the actual cached data (not just validity flags)
            lighter_count = len(self._lighter_orderbooks)
            self._lighter_orderbooks.clear()
            for key in list(self._is_valid.keys()):
                if key.startswith("lighter:"):
                    del self._is_valid[key]
            # Also clear crossed book tracking
            for key in list(self._crossed_book_counts.keys()):
                if key.startswith("lighter:"):
                    del self._crossed_book_counts[key]
            for key in list(self._crossed_book_blacklist_until.keys()):
                if key.startswith("lighter:"):
                    del self._crossed_book_blacklist_until[key]
            logger.info(f"   🗑️ Lighter orderbooks CLEARED ({lighter_count} symbols)")
        
        if exchange is None or exchange == "x10":
            x10_count = len(self._x10_orderbooks)
            self._x10_orderbooks.clear()
            for key in list(self._is_valid.keys()):
                if key.startswith("x10:"):
                    del self._is_valid[key]
            logger.info(f"   🗑️ X10 orderbooks CLEARED ({x10_count} symbols)")
        
        # Set cooldown period - CRITICAL for preventing delta processing
        self._reconnect_cooldown_until = time.time() + self.RECONNECT_COOLDOWN_SECONDS
        logger.warning(
            f"   ⏱️ Trading cooldown set for {self.RECONNECT_COOLDOWN_SECONDS}s - "
            f"WebSocket deltas will be IGNORED until REST snapshots are fetched"
        )
    
    def is_in_cooldown(self) -> bool:
        """
        Check if we're in post-reconnect cooldown.
        
        Returns:
            True if still in cooldown period, False otherwise
        """
        return time.time() < self._reconnect_cooldown_until
    
    def get_cooldown_remaining(self) -> float:
        """Get remaining cooldown time in seconds"""
        remaining = self._reconnect_cooldown_until - time.time()
        return max(0.0, remaining)
        
    def is_orderbook_valid(self, symbol: str, exchange: str = "lighter") -> tuple:
        """
        Validate orderbook for trading.
        
        Checks:
        1. Not in post-reconnect cooldown
        2. Orderbook data exists
        3. Orderbook is marked as valid
        4. Orderbook is not stale
        5. Orderbook is not crossed (ask < bid)
        
        Args:
            symbol: Trading pair (e.g., "DOGE-USD")
            exchange: "lighter" or "x10"
            
        Returns:
            Tuple of (is_valid: bool, reason: str)
        """
        # Check 1: Cooldown
        if self.is_in_cooldown():
            remaining = self.get_cooldown_remaining()
            return False, f"Post-reconnect cooldown ({remaining:.1f}s remaining)"
        
        # Get orderbook cache based on exchange
        if exchange == "lighter":
            orderbooks = self._lighter_orderbooks
        else:
            orderbooks = self._x10_orderbooks
        
        # Check 2: Data exists
        if symbol not in orderbooks:
            return False, "No orderbook data"
        
        # Check 3: Validity flag
        validity_key = f"{exchange}:{symbol}"
        if not self._is_valid.get(validity_key, False):
            return False, "Orderbook invalidated (awaiting fresh data)"
        
        # Check 4: Staleness
        ob = orderbooks[symbol]
        age = ob.age_seconds
        if age > self.STALENESS_THRESHOLD_SECONDS:
            return False, f"Orderbook stale ({age:.1f}s old)"
        
        # Check 5: Crossed book
        if ob.best_bid and ob.best_ask:
            if ob.best_ask <= ob.best_bid:
                return False, f"Crossed book: ask ({ob.best_ask}) <= bid ({ob.best_bid})"
        
        return True, "OK"
        
    def update_lighter_orderbook(
        self,
        symbol: str,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        timestamp: Optional[float] = None,
        sequence: Optional[int] = None,
    ):
        """Update Lighter orderbook from WebSocket message"""
        self._lighter_orderbooks[symbol] = OrderbookSnapshot(
            symbol=symbol,
            exchange="lighter",
            bids=[(Decimal(str(p)), Decimal(str(s))) for p, s in bids],
            asks=[(Decimal(str(p)), Decimal(str(s))) for p, s in asks],
            timestamp=timestamp or time.time(),
            sequence=sequence,
        )
        # Mark as valid after update
        self._is_valid[f"lighter:{symbol}"] = True
        
    def update_x10_orderbook(
        self,
        symbol: str,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        timestamp: Optional[float] = None,
    ):
        """Update X10 orderbook from WebSocket message"""
        self._x10_orderbooks[symbol] = OrderbookSnapshot(
            symbol=symbol,
            exchange="x10",
            bids=[(Decimal(str(p)), Decimal(str(s))) for p, s in bids],
            asks=[(Decimal(str(p)), Decimal(str(s))) for p, s in asks],
            timestamp=timestamp or time.time(),
        )
        # Mark as valid after update
        self._is_valid[f"x10:{symbol}"] = True
        
    async def fetch_orderbook_rest_fallback(
        self,
        symbol: str,
        exchange: str,
        retry_on_crossed: bool = True,
    ) -> Optional[OrderbookSnapshot]:
        """
        Fetch fresh orderbook via REST when WebSocket data is invalid.
        
        This is called when:
        - Crossed book is detected
        - WebSocket data is stale
        - Data is invalid after reconnect
        
        IMPORTANT: Uses retry logic for crossed books, as the exchange API
        may temporarily return inconsistent data during high volatility.
        
        Args:
            symbol: Trading pair (e.g., "DOGE-USD")
            exchange: "lighter" or "x10"
            retry_on_crossed: Whether to retry if REST also returns crossed book
            
        Returns:
            Fresh OrderbookSnapshot or None if failed
        """
        logger.info(f"📡 REST Fallback: Fetching {symbol} orderbook from {exchange}")
        
        # Check if symbol is temporarily blacklisted due to repeated crossed books
        blacklist_key = f"{exchange}:{symbol}"
        blacklist_until = self._crossed_book_blacklist_until.get(blacklist_key, 0)
        if time.time() < blacklist_until:
            remaining = blacklist_until - time.time()
            logger.warning(
                f"⏸️ {symbol} temporarily blacklisted for {remaining:.1f}s due to repeated crossed books"
            )
            return None
        
        max_retries = self.MAX_CROSSED_BOOK_RETRIES if retry_on_crossed else 1
        
        for attempt in range(max_retries):
            try:
                if exchange == "lighter" and self.lighter_adapter:
                    if hasattr(self.lighter_adapter, 'fetch_orderbook'):
                        # Clear adapter's cache to force fresh fetch
                        self.lighter_adapter.orderbook_cache.pop(symbol, None)
                        self.lighter_adapter._orderbook_cache.pop(symbol, None)
                        
                        orderbook = await self.lighter_adapter.fetch_orderbook(symbol, limit=20)
                        
                        if orderbook:
                            bids = orderbook.get("bids", [])
                            asks = orderbook.get("asks", [])
                            
                            # Validate the fetched data isn't also crossed
                            if bids and asks:
                                best_bid = float(bids[0][0]) if bids else 0
                                best_ask = float(asks[0][0]) if asks else float('inf')
                                
                                if best_ask <= best_bid:
                                    # Track consecutive crossed books
                                    count = self._crossed_book_counts.get(blacklist_key, 0) + 1
                                    self._crossed_book_counts[blacklist_key] = count
                                    
                                    logger.warning(
                                        f"⚠️ REST fallback returned crossed book for {symbol} "
                                        f"(attempt {attempt+1}/{max_retries}): ask={best_ask} <= bid={best_bid}"
                                    )
                                    
                                    if attempt < max_retries - 1:
                                        # Wait before retry - give exchange time to stabilize
                                        await asyncio.sleep(self.CROSSED_BOOK_RETRY_DELAY * (attempt + 1))
                                        continue
                                    else:
                                        # Max retries reached - blacklist temporarily
                                        if count >= 3:
                                            blacklist_duration = min(30.0, count * 10.0)  # Max 30s blacklist
                                            self._crossed_book_blacklist_until[blacklist_key] = time.time() + blacklist_duration
                                            logger.error(
                                                f"❌ {symbol} blacklisted for {blacklist_duration:.0f}s "
                                                f"after {count} consecutive crossed books"
                                            )
                                        return None
                            
                            # SUCCESS - clear crossed book count
                            self._crossed_book_counts[blacklist_key] = 0
                            
                            snapshot = OrderbookSnapshot(
                                symbol=symbol,
                                exchange="lighter",
                                bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                                asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                                timestamp=time.time(),
                            )
                            self._lighter_orderbooks[symbol] = snapshot
                            self._is_valid[f"lighter:{symbol}"] = True
                            logger.info(f"✅ {symbol} REST fallback successful - orderbook restored")
                            return snapshot
                            
                elif exchange == "x10" and self.x10_adapter:
                    if hasattr(self.x10_adapter, 'fetch_orderbook'):
                        orderbook = await self.x10_adapter.fetch_orderbook(symbol)
                        
                        if orderbook:
                            bids = orderbook.get("bids", [])
                            asks = orderbook.get("asks", [])
                            
                            # Validate
                            if bids and asks:
                                best_bid = float(bids[0][0]) if bids else 0
                                best_ask = float(asks[0][0]) if asks else float('inf')
                                if best_ask <= best_bid:
                                    logger.warning(
                                        f"⚠️ REST fallback also returned crossed book for {symbol}"
                                    )
                                    if attempt < max_retries - 1:
                                        await asyncio.sleep(self.CROSSED_BOOK_RETRY_DELAY)
                                        continue
                                    return None
                            
                            snapshot = OrderbookSnapshot(
                                symbol=symbol,
                                exchange="x10",
                                bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                                asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                                timestamp=time.time(),
                            )
                            self._x10_orderbooks[symbol] = snapshot
                            self._is_valid[f"x10:{symbol}"] = True
                            logger.info(f"✅ {symbol} REST fallback successful")
                            return snapshot
                        
            except asyncio.CancelledError:
                logger.debug(f"REST fallback cancelled for {symbol}")
                return None
            except Exception as e:
                logger.error(f"❌ REST orderbook fallback failed for {symbol} on {exchange}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(self.CROSSED_BOOK_RETRY_DELAY)
        
        return None
    
    async def get_lighter_orderbook(
        self,
        symbol: str,
        force_refresh: bool = False,
    ) -> Optional[OrderbookSnapshot]:
        """
        Get Lighter orderbook, using REST fallback if WebSocket data is stale or crossed.
        
        CRITICAL: After WebSocket reconnect, this will automatically fetch REST snapshot
        since WebSocket deltas are invalid without a base snapshot.
        
        Args:
            symbol: Trading pair (e.g., "DOGE-USD")
            force_refresh: Force REST API call
            
        Returns:
            OrderbookSnapshot or None if unavailable
        """
        # ═══════════════════════════════════════════════════════════════
        # CHECK 1: Post-reconnect cooldown - MUST fetch REST snapshot
        # ═══════════════════════════════════════════════════════════════
        if self.is_in_cooldown():
            remaining = self.get_cooldown_remaining()
            # During cooldown, we MUST fetch from REST to get a valid base snapshot
            logger.debug(f"⏸️ [{symbol}] In post-reconnect cooldown ({remaining:.1f}s) - forcing REST fetch")
            force_refresh = True
        
        # Check WebSocket cache first (if not in cooldown)
        cached = self._lighter_orderbooks.get(symbol)
        
        if cached and not force_refresh:
            # Check for crossed book condition
            if cached.best_bid and cached.best_ask and cached.best_ask <= cached.best_bid:
                logger.warning(
                    f"⚠️ CROSSED BOOK in provider cache for {symbol}: "
                    f"ask={cached.best_ask} <= bid={cached.best_bid} - triggering REST fallback"
                )
                # Try REST fallback with retry logic
                fresh = await self.fetch_orderbook_rest_fallback(symbol, "lighter", retry_on_crossed=True)
                if fresh:
                    return fresh
                # Invalidate the cached data
                self._is_valid[f"lighter:{symbol}"] = False
                return None
            
            if cached.age_seconds < self.max_staleness_seconds:
                return cached
            else:
                logger.debug(f"⚠️ {symbol} Lighter orderbook stale ({cached.age_seconds:.1f}s)")
                
        # Try to get from adapter's cache (populated by WebSocket)
        if self.lighter_adapter and hasattr(self.lighter_adapter, '_orderbook_cache') and not force_refresh:
            adapter_cache = self.lighter_adapter._orderbook_cache.get(symbol)
            if adapter_cache:
                cache_time = self.lighter_adapter._orderbook_cache_time.get(symbol, 0)
                age = time.time() - cache_time
                if age < self.max_staleness_seconds:
                    # Convert adapter cache format to OrderbookSnapshot
                    bids = adapter_cache.get('bids', [])
                    asks = adapter_cache.get('asks', [])
                    
                    # ═══════════════════════════════════════════════════════════════
                    # CRITICAL: Check for crossed book in adapter cache
                    # ═══════════════════════════════════════════════════════════════
                    if bids and asks:
                        best_bid = float(bids[0][0]) if bids else 0
                        best_ask = float(asks[0][0]) if asks else float('inf')
                        if best_ask <= best_bid:
                            logger.warning(
                                f"⚠️ CROSSED BOOK in adapter cache for {symbol}: "
                                f"ask={best_ask} <= bid={best_bid} - triggering REST fallback"
                            )
                            # Clear the adapter's corrupted cache
                            self.lighter_adapter._orderbook_cache.pop(symbol, None)
                            self.lighter_adapter.orderbook_cache.pop(symbol, None)
                            return await self.fetch_orderbook_rest_fallback(symbol, "lighter", retry_on_crossed=True)
                    
                    snapshot = OrderbookSnapshot(
                        symbol=symbol,
                        exchange="lighter",
                        bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                        asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                        timestamp=cache_time,
                    )
                    self._lighter_orderbooks[symbol] = snapshot
                    self._is_valid[f"lighter:{symbol}"] = True
                    return snapshot
                
        # REST fallback
        if self.rest_fallback_enabled and self.lighter_adapter:
            # Rate limiting (skip if force_refresh is True)
            last_fetch = self._last_rest_fetch.get(f"lighter:{symbol}", 0)
            if time.time() - last_fetch < self._rest_cooldown and not force_refresh:
                return cached  # Return stale data rather than spam API
                
            try:
                self._last_rest_fetch[f"lighter:{symbol}"] = time.time()
                
                # Clear adapter cache before fetching to ensure fresh data
                if force_refresh:
                    self.lighter_adapter.orderbook_cache.pop(symbol, None)
                    self.lighter_adapter._orderbook_cache.pop(symbol, None)
                
                # Use Lighter adapter's fetch_orderbook method
                if hasattr(self.lighter_adapter, 'fetch_orderbook'):
                    orderbook = await self.lighter_adapter.fetch_orderbook(symbol, limit=20)
                    
                    if orderbook:
                        bids = orderbook.get("bids", [])
                        asks = orderbook.get("asks", [])
                        
                        # ═══════════════════════════════════════════════════════════════
                        # VALIDATE REST response for crossed book
                        # ═══════════════════════════════════════════════════════════════
                        if bids and asks:
                            best_bid = float(bids[0][0]) if bids else 0
                            best_ask = float(asks[0][0]) if asks else float('inf')
                            if best_ask <= best_bid:
                                logger.warning(
                                    f"⚠️ REST returned crossed book for {symbol}: "
                                    f"ask={best_ask} <= bid={best_bid}"
                                )
                                return None
                        
                        snapshot = OrderbookSnapshot(
                            symbol=symbol,
                            exchange="lighter",
                            bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                            asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                            timestamp=time.time(),
                        )
                        self._lighter_orderbooks[symbol] = snapshot
                        self._is_valid[f"lighter:{symbol}"] = True
                        logger.debug(f"📚 {symbol} Lighter orderbook refreshed via REST")
                        return snapshot
                    
            except asyncio.CancelledError:
                logger.debug(f"Lighter orderbook fetch cancelled for {symbol}")
                return cached
            except Exception as e:
                logger.warning(f"⚠️ {symbol} Lighter orderbook REST fallback failed: {e}")
                
        return cached  # Return potentially stale data as last resort
        
    async def get_x10_orderbook(
        self,
        symbol: str,
        force_refresh: bool = False,
    ) -> Optional[OrderbookSnapshot]:
        """Get X10 orderbook with REST fallback"""
        cached = self._x10_orderbooks.get(symbol)
        
        if cached and not force_refresh:
            if cached.age_seconds < self.max_staleness_seconds:
                return cached
                
        # Try to get from adapter's cache
        if self.x10_adapter and hasattr(self.x10_adapter, '_orderbook_cache'):
            adapter_cache = self.x10_adapter._orderbook_cache.get(symbol)
            if adapter_cache:
                cache_time = self.x10_adapter._orderbook_cache_time.get(symbol, 0)
                age = time.time() - cache_time
                if age < self.max_staleness_seconds:
                    bids = adapter_cache.get('bids', [])
                    asks = adapter_cache.get('asks', [])
                    snapshot = OrderbookSnapshot(
                        symbol=symbol,
                        exchange="x10",
                        bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                        asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                        timestamp=cache_time,
                    )
                    self._x10_orderbooks[symbol] = snapshot
                    return snapshot
                
        # REST fallback for X10
        if self.rest_fallback_enabled and self.x10_adapter:
            last_fetch = self._last_rest_fetch.get(f"x10:{symbol}", 0)
            if time.time() - last_fetch < self._rest_cooldown and not force_refresh:
                return cached
                
            try:
                self._last_rest_fetch[f"x10:{symbol}"] = time.time()
                
                if hasattr(self.x10_adapter, 'fetch_orderbook'):
                    orderbook = await self.x10_adapter.fetch_orderbook(symbol)
                    
                    if orderbook:
                        bids = orderbook.get("bids", [])
                        asks = orderbook.get("asks", [])
                        snapshot = OrderbookSnapshot(
                            symbol=symbol,
                            exchange="x10",
                            bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                            asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                            timestamp=time.time(),
                        )
                        self._x10_orderbooks[symbol] = snapshot
                        return snapshot
                    
            except Exception as e:
                logger.warning(f"⚠️ {symbol} X10 orderbook REST fallback failed: {e}")
                
        return cached
        
    def get_orderbook_stats(self, exchange: str = "lighter") -> Dict:
        """Get statistics about cached orderbooks"""
        orderbooks = self._lighter_orderbooks if exchange == "lighter" else self._x10_orderbooks
        
        now = time.time()
        total = len(orderbooks)
        fresh = sum(1 for ob in orderbooks.values() if now - ob.timestamp < self.max_staleness_seconds)
        stale = total - fresh
        
        return {
            "total": total,
            "fresh": fresh,
            "stale": stale,
            "staleness_threshold": self.max_staleness_seconds,
        }
    
    def clear_cache(self, exchange: Optional[str] = None):
        """Clear orderbook cache"""
        if exchange is None or exchange == "lighter":
            self._lighter_orderbooks.clear()
        if exchange is None or exchange == "x10":
            self._x10_orderbooks.clear()


# Singleton instance
_default_provider: Optional[OrderbookProvider] = None


def get_orderbook_provider() -> OrderbookProvider:
    """Get singleton orderbook provider instance"""
    global _default_provider
    if _default_provider is None:
        try:
            import config
            _default_provider = OrderbookProvider(
                max_staleness_seconds=getattr(config, 'OB_MAX_STALENESS_SECONDS', 5.0),
                rest_fallback_enabled=getattr(config, 'OB_REST_FALLBACK_ENABLED', True),
            )
        except ImportError:
            _default_provider = OrderbookProvider()
    return _default_provider


def init_orderbook_provider(lighter_adapter, x10_adapter, ws_manager=None) -> OrderbookProvider:
    """Initialize orderbook provider with adapters"""
    global _default_provider
    try:
        import config
        _default_provider = OrderbookProvider(
            lighter_adapter=lighter_adapter,
            x10_adapter=x10_adapter,
            ws_manager=ws_manager,
            max_staleness_seconds=getattr(config, 'OB_MAX_STALENESS_SECONDS', 5.0),
            rest_fallback_enabled=getattr(config, 'OB_REST_FALLBACK_ENABLED', True),
        )
    except ImportError:
        _default_provider = OrderbookProvider(
            lighter_adapter=lighter_adapter,
            x10_adapter=x10_adapter,
            ws_manager=ws_manager,
        )
    return _default_provider

