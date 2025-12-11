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
    """
    
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
        
        # Last REST fetch timestamps (rate limiting)
        self._last_rest_fetch: Dict[str, float] = {}
        self._rest_cooldown = 1.0  # Minimum 1s between REST calls per symbol
        
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
        
    async def get_lighter_orderbook(
        self,
        symbol: str,
        force_refresh: bool = False,
    ) -> Optional[OrderbookSnapshot]:
        """
        Get Lighter orderbook, using REST fallback if WebSocket data is stale.
        
        Args:
            symbol: Trading pair (e.g., "DOGE-USD")
            force_refresh: Force REST API call
            
        Returns:
            OrderbookSnapshot or None if unavailable
        """
        # Check WebSocket cache first
        cached = self._lighter_orderbooks.get(symbol)
        
        if cached and not force_refresh:
            if cached.age_seconds < self.max_staleness_seconds:
                return cached
            else:
                logger.debug(f"⚠️ {symbol} Lighter orderbook stale ({cached.age_seconds:.1f}s)")
                
        # Try to get from adapter's cache (populated by WebSocket)
        if self.lighter_adapter and hasattr(self.lighter_adapter, '_orderbook_cache'):
            adapter_cache = self.lighter_adapter._orderbook_cache.get(symbol)
            if adapter_cache:
                cache_time = self.lighter_adapter._orderbook_cache_time.get(symbol, 0)
                age = time.time() - cache_time
                if age < self.max_staleness_seconds:
                    # Convert adapter cache format to OrderbookSnapshot
                    bids = adapter_cache.get('bids', [])
                    asks = adapter_cache.get('asks', [])
                    snapshot = OrderbookSnapshot(
                        symbol=symbol,
                        exchange="lighter",
                        bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                        asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                        timestamp=cache_time,
                    )
                    self._lighter_orderbooks[symbol] = snapshot
                    return snapshot
                
        # REST fallback
        if self.rest_fallback_enabled and self.lighter_adapter:
            # Rate limiting
            last_fetch = self._last_rest_fetch.get(f"lighter:{symbol}", 0)
            if time.time() - last_fetch < self._rest_cooldown and not force_refresh:
                return cached  # Return stale data rather than spam API
                
            try:
                self._last_rest_fetch[f"lighter:{symbol}"] = time.time()
                
                # Use Lighter adapter's fetch_orderbook method
                if hasattr(self.lighter_adapter, 'fetch_orderbook'):
                    orderbook = await self.lighter_adapter.fetch_orderbook(symbol)
                    
                    if orderbook:
                        bids = orderbook.get("bids", [])
                        asks = orderbook.get("asks", [])
                        snapshot = OrderbookSnapshot(
                            symbol=symbol,
                            exchange="lighter",
                            bids=[(Decimal(str(b[0])), Decimal(str(b[1]))) for b in bids if len(b) >= 2],
                            asks=[(Decimal(str(a[0])), Decimal(str(a[1]))) for a in asks if len(a) >= 2],
                            timestamp=time.time(),
                        )
                        self._lighter_orderbooks[symbol] = snapshot
                        logger.debug(f"📚 {symbol} Lighter orderbook refreshed via REST")
                        return snapshot
                    
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

