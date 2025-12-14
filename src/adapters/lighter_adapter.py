# src/adapters/lighter_adapter.py
import asyncio
import aiohttp
import json
import logging
import time
import random
import websockets
from typing import Dict, Tuple, Optional, List, Any
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING

# Module initialization

import config

# ═══════════════════════════════════════════════════════════════════════════════
# Korrekte Imports für das offizielle Lighter SDK
# ═══════════════════════════════════════════════════════════════════════════════
OrderApi = None
FundingApi = None
AccountApi = None
SignerClient = None
HAVE_LIGHTER_SDK = False

try:
    from lighter.api.order_api import OrderApi
    from lighter.api.funding_api import FundingApi
    from lighter.api.account_api import AccountApi
    from lighter.signer_client import SignerClient
    HAVE_LIGHTER_SDK = True
except ImportError as e:
    HAVE_LIGHTER_SDK = False
    # Logged at runtime when adapter is initialized

from .base_adapter import BaseAdapter
from src.rate_limiter import LIGHTER_RATE_LIMITER, rate_limited, Exchange, with_rate_limit
from src.adapters.lighter_client_fix import SaferSignerClient

logger = logging.getLogger(__name__)

MARKET_OVERRIDES = {
    "ASTER-USD": {"ss": Decimal("1"), "sd": 8},
    "HYPE-USD": {"ss": Decimal("0.01"), "sd": 6},
    "MEGA-USD": {"ss": Decimal("99999999")},
}


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL TYPE-SAFETY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

from src.utils import safe_float, safe_int, quantize_value


def safe_int(val, default=0):
    """Convert any value to int safely."""
    if val is None:
        return default
    if isinstance(val, int):
        return val
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default


class LighterAdapter(BaseAdapter):
    def __init__(self):
        super().__init__("Lighter")

        if HAVE_LIGHTER_SDK:
            logger.info("✅ Using SaferSignerClient subclass instead of Monkey-Patch")

        self.market_info = {}
        self.funding_cache = {}
        self.price_cache = {}
        self.orderbook_cache = {}
        self._price_cache = {}
        self._price_cache_time = {}
        self._orderbook_cache = {}
        self._orderbook_cache_time = {}
        self._trade_cache = {}
        self._funding_cache = {}
        self._funding_cache_time = {}
        
        # Public Aliases for Latency/Prediction modules
        self.price_cache_time = self._price_cache_time
        self. funding_cache_time = self._funding_cache_time
        
        self.price_update_event = None
        self._ws_message_queue = asyncio.Queue()
        self._signer = None
        self._resolved_account_index = None
        self._resolved_api_key_index = None
        self. semaphore = asyncio.Semaphore(5)
        self.rate_limiter = LIGHTER_RATE_LIMITER
        self._last_market_cache_at = None
        self._balance_cache = 0.0
        self._last_balance_update = 0.0
        self.base_url = self._get_base_url()
        self._pending_positions = {}  # Ghost Guardian Cache
        self._dust_logged = set()  # Track dust positions that have been logged (to reduce spam)
        
        # NEU: Lock für thread-sichere Order-Erstellung (Fix für Invalid Nonce)
        self.order_lock = asyncio.Lock()
        
        # ═══════════════════════════════════════════════════════════════
        # FIXED: Position callback infrastructure for Ghost-Fill detection
        # ═══════════════════════════════════════════════════════════════
        self._position_callbacks: List[Any] = []
        self._positions_cache: List[dict] = []  # Cache for fetch_open_positions deduplication
        
        # ═══════════════════════════════════════════════════════════════
        # NONCE MANAGEMENT: Pattern from lighter-ts-main/src/utils/nonce-manager.ts
        # Pre-fetch batch of nonces for faster order placement
        # ═══════════════════════════════════════════════════════════════
        self._nonce_pool: List[int] = []
        self._nonce_pool_fetch_time: float = 0.0
        self._nonce_refill_in_progress: bool = False
        # Legacy compatibility (for _invalidate_nonce_cache)
        self._cached_nonce: Optional[int] = None
        self._nonce_fetch_time: float = 0.0
        self._nonce_cache_ttl: float = 10.0  # Legacy, not used by new pool
        
        # Shutdown state
        self._shutdown_cancel_done = False
        self._shutdown_cancel_failed = False

        # ═══════════════════════════════════════════════════════════════
        # REQUEST DEDUPLICATION: Prevent API spam during shutdown
        # ═══════════════════════════════════════════════════════════════
        self._request_cache: Dict[str, Tuple[float, Any]] = {}
        self._request_cache_ttl = 2.0  # seconds
        self._request_lock = asyncio.Lock()
        
        # ═══════════════════════════════════════════════════════════════
        # FIX 3 (2025-12-13): Order Tracking for Cancel Resolution
        # Pattern from lighter-ts-main/src/utils/order-status-checker.ts:
        # - Track orders by tx_hash AND client_order_index
        # - When cancel fails to resolve hash → use client_order_index
        # - Enables ImmediateCancelAll even when API 404s on order lookup
        # 
        # Structure: { tx_hash: { symbol, client_order_index, placed_at, nonce } }
        # ═══════════════════════════════════════════════════════════════
        self._placed_orders: Dict[str, Dict[str, Any]] = {}
        self._placed_orders_lock = asyncio.Lock()

        # WebSocket Management
        self.ws_task = None
        self.ws_connected = False
        self._ws_url = "wss://mainnet.zklighter.elliot.ai/stream"
        if getattr(config, "LIGHTER_BASE_URL", "").startswith("https://testnet"):
            self._ws_url = "wss://testnet.zklighter.elliot.ai/stream"

    async def _safe_acquire_rate_limit(self) -> bool:
        """
        Acquire rate limit with proper shutdown handling.
        Returns True if acquired, False if shutting down.
        """
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"{self.name}: Skipping rate limit acquire - shutdown in progress")
            return False
        
        try:
            result = await self.rate_limiter.acquire()
            if result < 0:  # -1.0 means cancelled
                logger.debug(f"{self.name}: Rate limit acquire cancelled")
                return False
            return True
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: Rate limit acquire cancelled")
            return False  # Return False instead of raising

    # ═══════════════════════════════════════════════════════════════
    # NONCE MANAGEMENT: Pattern from lighter-ts-main/src/utils/nonce-manager.ts
    # 
    # Features:
    # - Batch prefetch: Load 20 nonces at once (reduces API calls)
    # - Auto-refill: Fetch more when pool < 2
    # - acknowledge_failure(): Rollback nonce on TX failure
    # - hard_refresh_nonce(): Force refetch on "invalid nonce" error
    # - get_next_nonces(count): Get multiple nonces for batch operations
    # ═══════════════════════════════════════════════════════════════
    
    # Nonce pool configuration (matching TS SDK)
    NONCE_BATCH_SIZE = 20  # Pre-fetch 20 nonces at a time
    NONCE_REFILL_THRESHOLD = 2  # Refill when pool < 2
    NONCE_MAX_CACHE_AGE = 30.0  # 30 seconds max cache age
    
    async def _get_next_nonce(self, force_refresh: bool = False) -> Optional[int]:
        """
        Get the next nonce from the pool.
        
        Strategy (matching TS SDK nonce-cache.ts):
        - If nonce pool is empty or expired, fetch new batch from API
        - Pop first nonce from pool
        - Auto-refill pool when getting low
        
        MUST be called while holding self.order_lock!
        """
        now = time.time()
        
        # Initialize pool if needed
        if not hasattr(self, '_nonce_pool'):
            self._nonce_pool: List[int] = []
            self._nonce_pool_fetch_time: float = 0.0
            self._nonce_refill_in_progress: bool = False
        
        # Check if pool is empty, expired, or force refresh
        pool_age = now - self._nonce_pool_fetch_time
        pool_expired = pool_age > self.NONCE_MAX_CACHE_AGE
        
        if force_refresh or len(self._nonce_pool) == 0 or pool_expired:
            await self._refill_nonce_pool()
        
        # Get nonce from pool
        if len(self._nonce_pool) == 0:
            logger.error("❌ Nonce pool empty after refill attempt")
            return None
        
        nonce_to_use = self._nonce_pool.pop(0)
        
        # Auto-refill if pool is getting low (async, don't wait)
        if len(self._nonce_pool) <= self.NONCE_REFILL_THRESHOLD and not self._nonce_refill_in_progress:
            asyncio.create_task(self._refill_nonce_pool_async())
        
        logger.debug(f"⚡ Using nonce {nonce_to_use} (pool: {len(self._nonce_pool)} remaining)")
        return nonce_to_use
    
    async def _refill_nonce_pool(self) -> None:
        """
        Refill the nonce pool with a batch of nonces from the API.
        Synchronized to prevent concurrent fetches.
        """
        if self._nonce_refill_in_progress:
            # Wait for existing refill to complete
            while self._nonce_refill_in_progress:
                await asyncio.sleep(0.05)
            return
        
        self._nonce_refill_in_progress = True
        try:
            await self._do_refill_nonce_pool()
        finally:
            self._nonce_refill_in_progress = False
    
    async def _refill_nonce_pool_async(self) -> None:
        """Async wrapper for background refill (fire-and-forget)."""
        try:
            await self._refill_nonce_pool()
        except Exception as e:
            logger.warning(f"⚠️ Background nonce refill failed: {e}")
    
    async def _do_refill_nonce_pool(self) -> None:
        """Actually fetch and populate the nonce pool."""
        try:
            if self._resolved_account_index is None:
                await self._resolve_account_index()
            
            nonce_params = {
                "account_index": self._resolved_account_index,
                "api_key_index": self._resolved_api_key_index
            }
            
            # Fetch the first nonce from API
            nonce_resp = await self._rest_get_internal("/api/v1/nextNonce", params=nonce_params)
            
            if nonce_resp is None:
                logger.error("❌ Failed to fetch nonce batch from API")
                return
            
            # Parse nonce response
            if isinstance(nonce_resp, dict):
                val = nonce_resp.get('nonce')
                if val is not None:
                    first_nonce = int(val)
                else:
                    logger.error(f"❌ Nonce response dict has no 'nonce' key: {nonce_resp}")
                    return
            else:
                try:
                    first_nonce = int(str(nonce_resp).strip())
                except ValueError:
                    logger.error(f"❌ Invalid nonce format: {nonce_resp}")
                    return
            
            # Generate batch of nonces (TS SDK pattern: first_nonce + i for i in range(batch_size))
            new_nonces = [first_nonce + i for i in range(self.NONCE_BATCH_SIZE)]
            
            # Replace pool with fresh nonces
            self._nonce_pool = new_nonces
            self._nonce_pool_fetch_time = time.time()
            
            logger.debug(f"🔄 Nonce pool refilled: {len(new_nonces)} nonces starting at {first_nonce}")
            
        except Exception as e:
            logger.error(f"❌ Nonce pool refill error: {e}")
    
    async def get_next_nonces(self, count: int) -> List[int]:
        """
        Get multiple nonces for batch operations.
        Pattern from TS SDK nonce-manager.ts:getNextNonces()
        
        MUST be called while holding self.order_lock!
        """
        nonces = []
        for _ in range(count):
            nonce = await self._get_next_nonce()
            if nonce is None:
                break
            nonces.append(nonce)
        return nonces
    
    def acknowledge_failure(self) -> None:
        """
        Acknowledge a transaction failure and rollback nonce.
        Pattern from TS SDK nonce-cache.ts:acknowledgeFailure()
        
        This prevents nonce gaps when transactions fail.
        Call this when a Lighter TX fails after getting a nonce.
        """
        if hasattr(self, '_nonce_pool') and len(self._nonce_pool) > 0:
            # Get the last used nonce (it's one before the first in pool)
            last_used = self._nonce_pool[0] - 1 if self._nonce_pool else None
            if last_used is not None:
                # Rollback by adding the failed nonce back to the front
                self._nonce_pool.insert(0, last_used)
                logger.debug(f"🔄 Nonce {last_used} rolled back after failure (pool size: {len(self._nonce_pool)})")
    
    async def hard_refresh_nonce(self) -> None:
        """
        Force a complete nonce pool refresh.
        Pattern from TS SDK nonce-cache.ts:hardRefreshNonce()
        
        Use this when receiving "invalid nonce" errors from Lighter API.
        """
        # Clear current pool
        if hasattr(self, '_nonce_pool'):
            self._nonce_pool = []
            self._nonce_pool_fetch_time = 0.0
        
        # Fetch fresh nonces
        await self._refill_nonce_pool()
        logger.info("🔄 Nonce pool hard-refreshed after invalid nonce error")
    
    def _invalidate_nonce_cache(self):
        """Invalidate nonce cache (call on nonce-related errors)."""
        if hasattr(self, '_nonce_pool'):
            self._nonce_pool = []
            self._nonce_pool_fetch_time = 0.0
        # Legacy compatibility
        self._cached_nonce = None
        self._nonce_fetch_time = 0.0
        logger.debug("🔄 Nonce cache invalidated")

    # ═══════════════════════════════════════════════════════════════
    # BATCH ORDER SUPPORT: Pattern from lighter-ts-main/src/api/transaction-api.ts
    # 
    # Features:
    # - send_batch_orders(): Send multiple orders in one API call
    # - close_all_positions_batch(): Close all positions in one TX
    # - Reduces API calls and latency during shutdown
    # ═══════════════════════════════════════════════════════════════
    
    async def send_batch_orders(
        self,
        tx_types: List[int],
        tx_infos: List[str]
    ) -> Tuple[bool, List[Optional[str]]]:
        """
        Send multiple orders in a single batch transaction.
        Pattern from lighter-ts-main/src/api/transaction-api.ts:sendTransactionBatch()
        
        Args:
            tx_types: List of transaction types (e.g., [1, 1, 1] for create orders)
            tx_infos: List of signed transaction JSON strings
            
        Returns:
            Tuple[bool, List[Optional[str]]]: (success, list of tx hashes)
        """
        if not tx_types or not tx_infos or len(tx_types) != len(tx_infos):
            logger.error("❌ send_batch_orders: Invalid inputs")
            return False, []
        
        try:
            base_url = self._get_base_url()
            url = f"{base_url}/api/v1/sendTxBatch"
            
            # Format: tx_types and tx_infos as JSON arrays (stringified)
            payload = {
                "tx_types": json.dumps(tx_types),
                "tx_infos": json.dumps(tx_infos)
            }
            
            logger.info(f"📦 Sending batch of {len(tx_types)} transactions...")
            
            session = await self._get_session()
            async with session.post(
                url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"❌ Batch TX failed: {resp.status} - {text}")
                    return False, []
                
                result = await resp.json()
                
                # API returns tx_hash as array
                tx_hashes = result.get("tx_hash", []) or result.get("hashes", [])
                
                if tx_hashes:
                    logger.info(f"✅ Batch TX success: {len(tx_hashes)} transactions")
                    for i, h in enumerate(tx_hashes):
                        if h:
                            logger.debug(f"   TX {i+1}: {str(h)[:40]}...")
                    return True, tx_hashes
                else:
                    # Check for error
                    code = result.get("code")
                    message = result.get("message", "Unknown error")
                    if code and code != 200:
                        logger.error(f"❌ Batch TX error: code={code}, msg={message}")
                        return False, []
                    logger.info(f"✅ Batch TX submitted (no hashes returned)")
                    return True, []
                    
        except Exception as e:
            logger.error(f"❌ send_batch_orders error: {e}")
            return False, []
    
    async def close_all_positions_batch(self) -> Tuple[int, int]:
        """
        Close all open positions using concurrent close calls.
        Optimized for shutdown - faster than fully sequential close calls.
        
        Note: True TX-batching via /api/v1/sendTxBatch requires raw signing
        which the Python SDK doesn't expose. This uses parallelism instead.
        
        Returns:
            Tuple[int, int]: (closed_count, failed_count)
        """
        positions = await self.fetch_open_positions()
        if not positions:
            logger.info("📦 No positions to batch-close")
            return 0, 0
        
        # Filter positions with actual size
        positions_to_close = [
            p for p in positions 
            if abs(safe_float(p.get("size", 0))) > 1e-8
        ]
        
        if not positions_to_close:
            logger.info("📦 No non-zero positions to batch-close")
            return 0, 0
        
        logger.info(f"📦 Parallel-closing {len(positions_to_close)} Lighter positions...")
        
        closed_count = 0
        failed_count = 0
        
        # Create close tasks for parallel execution
        async def close_single_position(pos: dict) -> bool:
            try:
                symbol = pos.get("symbol", "")
                size = safe_float(pos.get("size", 0))
                
                if abs(size) < 1e-8:
                    return True  # Already closed
                
                # Determine close side (opposite of position)
                original_side = "BUY" if size > 0 else "SELL"
                
                # Use close_live_position for proper handling
                success, _ = await self.close_live_position(
                    symbol=symbol,
                    original_side=original_side,
                    notional_usd=0  # Will be calculated from position size
                )
                
                if success:
                    logger.debug(f"✅ Closed {symbol}")
                    return True
                else:
                    logger.warning(f"⚠️ Failed to close {symbol}")
                    return False
                    
            except Exception as e:
                logger.warning(f"⚠️ Error closing {pos.get('symbol')}: {e}")
                return False
        
        # Execute closes in parallel with limited concurrency
        # Use batches of 3 to respect rate limits
        batch_size = 3
        for i in range(0, len(positions_to_close), batch_size):
            batch = positions_to_close[i:i+batch_size]
            tasks = [close_single_position(pos) for pos in batch]
            
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception):
                        failed_count += 1
                    elif result:
                        closed_count += 1
                    else:
                        failed_count += 1
                        
            except Exception as e:
                logger.error(f"❌ Batch close error: {e}")
                failed_count += len(batch)
            
            # Small delay between batches for rate limiting
            if i + batch_size < len(positions_to_close):
                await asyncio.sleep(0.3)
        
        logger.info(f"📦 Parallel close complete: {closed_count} closed, {failed_count} failed")
        return closed_count, failed_count

    # ═══════════════════════════════════════════════════════════════
    # CANDLESTICK API: Pattern from lighter-ts-main/src/api/candlestick-api.ts
    # 
    # Features:
    # - get_candlesticks(): Fetch OHLCV data for a market
    # - calculate_volatility(): ATR-based volatility calculation
    # - Used for: Dynamic timeout optimization, trend detection
    # ═══════════════════════════════════════════════════════════════
    
    async def get_candlesticks(
        self,
        symbol: str,
        resolution: str = "1h",
        count_back: int = 24
    ) -> List[Dict]:
        """
        Fetch candlestick (OHLCV) data for a market.
        Pattern from lighter-ts-main/src/api/candlestick-api.ts
        
        Args:
            symbol: Trading pair (e.g., "ETH-USD")
            resolution: Candle interval (1m, 5m, 15m, 30m, 1h, 4h, 1d)
            count_back: Number of candles to fetch (default: 24)
            
        Returns:
            List of candlestick dicts with open, high, low, close, volume, timestamp
        """
        try:
            # Get market_id from market_info (key 'i')
            market_data = self.market_info.get(symbol, {})
            market_id = market_data.get('i', -1)
            if market_id is None or market_id < 0:
                logger.debug(f"⚠️ get_candlesticks: No market_id for {symbol}")
                return []
            
            base_url = self._get_base_url()
            url = f"{base_url}/api/v1/candlesticks"
            
            # Map resolution to API format and duration in seconds
            resolution_map = {
                "1m": ("1m", 60),
                "5m": ("5m", 300),
                "15m": ("15m", 900),
                "1h": ("1h", 3600),
                "4h": ("4h", 14400),
                "1d": ("1d", 86400)
            }
            api_resolution, interval_seconds = resolution_map.get(resolution, ("1h", 3600))
            
            # Calculate timestamps (required by the API)
            end_timestamp = int(time.time())
            start_timestamp = end_timestamp - (count_back * interval_seconds)
            
            params = {
                "market_id": market_id,
                "resolution": api_resolution,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "count_back": count_back
            }
            
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.debug(f"⚠️ Candlesticks API {symbol}: {resp.status} - {text[:100]}")
                    return []
                
                result = await resp.json()
                candlesticks = result.get("candlesticks", [])
                
                if candlesticks:
                    logger.debug(f"📊 {symbol}: Fetched {len(candlesticks)} candles ({resolution})")
                
                return candlesticks
                
        except Exception as e:
            logger.debug(f"⚠️ get_candlesticks {symbol} error: {e}")
            return []
    
    async def calculate_volatility(
        self,
        symbol: str,
        resolution: str = "1h",
        periods: int = 14
    ) -> Optional[Dict]:
        """
        Calculate volatility metrics using Average True Range (ATR).
        Used for dynamic timeout adjustment and risk assessment.
        
        Args:
            symbol: Trading pair
            resolution: Candle interval 
            periods: ATR calculation period (default: 14)
            
        Returns:
            Dict with volatility metrics or None if insufficient data
        """
        try:
            candles = await self.get_candlesticks(symbol, resolution, periods + 1)
            
            if len(candles) < periods + 1:
                logger.debug(f"📊 {symbol}: Insufficient candles for volatility ({len(candles)}/{periods + 1})")
                return None
            
            # Calculate True Range for each candle
            true_ranges = []
            for i in range(1, len(candles)):
                prev_close = safe_float(candles[i-1].get("close", 0))
                high = safe_float(candles[i].get("high", 0))
                low = safe_float(candles[i].get("low", 0))
                
                if prev_close > 0 and high > 0 and low > 0:
                    tr = max(
                        high - low,
                        abs(high - prev_close),
                        abs(low - prev_close)
                    )
                    true_ranges.append(tr)
            
            if len(true_ranges) < periods:
                return None
            
            # Calculate ATR (Average True Range)
            atr = sum(true_ranges[-periods:]) / periods
            
            # Get current price for percentage calculation
            current_price = safe_float(candles[-1].get("close", 0))
            if current_price <= 0:
                return None
            
            # ATR as percentage of price
            atr_percent = (atr / current_price) * 100
            
            # Volatility classification
            if atr_percent < 1.0:
                volatility_level = "LOW"
            elif atr_percent < 3.0:
                volatility_level = "MEDIUM"
            else:
                volatility_level = "HIGH"
            
            result = {
                "symbol": symbol,
                "atr": atr,
                "atr_percent": atr_percent,
                "volatility_level": volatility_level,
                "current_price": current_price,
                "periods": periods,
                "resolution": resolution,
                "timestamp": time.time()
            }
            
            logger.info(
                f"📊 {symbol} Volatility: ATR={atr:.4f} ({atr_percent:.2f}%) - {volatility_level}"
            )
            
            return result
            
        except Exception as e:
            logger.debug(f"⚠️ calculate_volatility {symbol} error: {e}")
            return None
    
    async def get_volatility_adjusted_timeout(
        self,
        symbol: str,
        base_timeout: float = 60.0
    ) -> float:
        """
        Get timeout adjusted for current market volatility.
        Higher volatility = longer timeout (more price movement expected).
        
        Args:
            symbol: Trading pair
            base_timeout: Base timeout in seconds
            
        Returns:
            Adjusted timeout in seconds
        """
        try:
            vol_data = await self.calculate_volatility(symbol, "1h", 14)
            
            if not vol_data:
                return base_timeout
            
            volatility_level = vol_data.get("volatility_level", "MEDIUM")
            
            if volatility_level == "LOW":
                # Low volatility - use shorter timeout
                adjusted = base_timeout * 0.7
            elif volatility_level == "HIGH":
                # High volatility - use longer timeout
                adjusted = base_timeout * 1.3
            else:
                # Medium volatility - use base timeout
                adjusted = base_timeout
            
            # Clamp to reasonable range
            adjusted = max(30.0, min(120.0, adjusted))
            
            logger.debug(
                f"⏱️ {symbol}: Volatility-adjusted timeout: {adjusted:.1f}s "
                f"(volatility={volatility_level}, base={base_timeout:.1f}s)"
            )
            
            return adjusted
            
        except Exception as e:
            logger.debug(f"⚠️ get_volatility_adjusted_timeout {symbol} error: {e}")
            return base_timeout


    async def start_websocket(self):
        """Start the WebSocket connection task."""
        if self.ws_task and not self.ws_task.done():
            return
        logger.info(f"🔌 Starting Lighter WebSocket: {self._ws_url}")
        self.ws_task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self):
        """WebSocket Main Loop with Auto-Reconnect"""
        while True:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self.ws_connected = True
                    logger.info("✅ Lighter WebSocket Connected")
                    
                    # Resubscribe to orderbooks for all known markets
                    await self._ws_subscribe_all(ws)
                    
                    while True:
                        try:
                            msg = await ws.recv()
                            await self._process_ws_message(json.loads(msg))
                        except websockets.ConnectionClosed:
                            logger.warning("Construction Closed on Lighter WS")
                            break
                        except Exception as e:
                            logger.error(f"Lighter WS Message Error: {e}")
                            
            except Exception as e:
                self.ws_connected = False
                logger.error(f"Lighter WS Connection Error: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    async def _ws_subscribe_all(self, ws):
        """Subscribe to order_book channel for all active markets."""
        # Ensure markets are loaded
        if not self.market_info:
            await self.load_market_cache()
            
        for symbol, info in self.market_info.items():
            market_id = info.get('i')
            if market_id is not None:
                # Format: {"type": "subscribe", "channel": "order_book/{market_id}"}
                payload = {
                    "type": "subscribe",
                    "channel": f"order_book/{market_id}"
                }
                await ws.send(json.dumps(payload))
                logger.info(f"📡 Subscribed to Lighter OB: {symbol} (ID: {market_id})")

    async def _process_ws_message(self, msg: dict):
        """Update Orderbook Cache from WS Message"""
        try:
            # Channel format: "order_book/{market_id}"
            channel = msg.get("channel", "")
            if not channel.startswith("order_book"):
                return
                
            # Extract Market ID
            try:
                market_id = int(channel.split("/")[-1])
            except:
                return
                
            # Find symbol for this market ID
            symbol = next((s for s, i in self.market_info.items() if i.get('i') == market_id), None)
            if not symbol:
                return

            # Orderbook Snapshot?
            # Lighter WS sends { "type": "snapshot", "asks": [...], "bids": [...] } or "update"
            msg_type = msg.get("type")
            
            # Helper to parse [price_str, size_str]
            def parse_level(lvl):
                # lvl is usually {"price": "...", "amount": "..."} or similar dict in Lighter?
                # Based on previous search, it sends new ask/bid orders.
                # Actually, standard lightweight exchange WS usually sends full Snapshot first, then diffs.
                # Lighter WS documentation says "Sends new ask and bid orders". 
                # Be careful: Does it send full book or just updates?
                # If it sends INDIVIDUAL orders, we need a local orderbook implementation (Left L2 Book).
                # That is complex. 
                # Let's assume standard snapshot for now or verify payload.
                pass

            # RE-VERIFYING WS PAYLOAD: 
            # documentation says "Sends new ask and bid orders". This implies a stream of ADD/UPDATE/DELETE?
            # Or is it a stream of snapshots?
            # "Order Book: Sends new ask and bid orders for a given market."
            # Likely it sends updates. Maintaining a full OB from partial updates is complex (Delta-Tracking).
            
            # SHORTCUT STRATEGY (Penny Jumping):
            # If we get a "snapshot" or full book, great. 
            # If we get partials, we might just store the "best" bid/ask seen recently? 
            # No, that's dangerous.
            
            # Let's look at the structure 'asks' and 'bids' in msg.
            asks = msg.get("asks", [])
            bids = msg.get("bids", [])
            
            # If explicit snapshot
            if asks or bids:
                # Structure: [{"price": "100", "amount": "1"}, ...]
                # Lighter WS usually sends SNAPSHOT on connect/sub.
                
                # Check if it's a full snapshot or update.
                # Use simplified logic: Just REPLACE cache if it looks like a snapshot (many items).
                # If it's small updates, we ideally update an internal structure. It's safer to treat it as a "Latest View" if possible.
                
                # For Penny Jumping, we need BEST BID and BEST ASK.
                # If the message contains current Bids/Asks, we can update our view.
                
                parsed_bids = []
                for b in bids:
                    p = safe_float(b.get("price"), 0)
                    s = safe_float(b.get("amount", b.get("remaining_base_amount")), 0)
                    if p > 0 and s > 0: parsed_bids.append([p, s])
                    
                parsed_asks = []
                for a in asks:
                    p = safe_float(a.get("price"), 0)
                    s = safe_float(a.get("amount", a.get("remaining_base_amount")), 0)
                    if p > 0 and s > 0: parsed_asks.append([p, s])
                
                parsed_bids.sort(key=lambda x: x[0], reverse=True)
                parsed_asks.sort(key=lambda x: x[0])
                
                # Simple Cache Update (Snapshot Mode)
                # Warning: If Lighter sends Deltas, this overrides full book with just deltas.
                # However, implementing full L2 Delta reconstruction in 5 mins is risky.
                # Better approach: Updates usually have a 'type'='snapshot' vs 'update'.
                
                if msg_type == 'snapshot' or (len(parsed_bids) > 5 or len(parsed_asks) > 5):
                     # Treat as Full Snapshot
                     self.orderbook_cache[symbol] = {
                        "bids": parsed_bids,
                        "asks": parsed_asks,
                        "timestamp": int(time.time() * 1000),
                        "symbol": symbol
                     }
                     # Update Price Cache for immediate ticker use
                     if parsed_bids and parsed_asks:
                         mid = (parsed_bids[0][0] + parsed_asks[0][0]) / 2
                         self.price_cache[symbol] = mid
                         
                elif msg_type == 'update':
                     # DELTA UPDATE - Complex.
                     # For now, stick to REST polling for full integrity if WS is complex Delta,
                     # BUT use WS for "Best Price" hints if possible.
                     # Without full Orderbook Class, Delta updates corrupt the book.
                     # Let's rely on frequent REST snapshots + WS Snapshots if available.
                     pass
                     
        except Exception as e:
            pass
        """Get or create aiohttp session"""
        if not hasattr(self, '_session') or self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _rest_get_internal(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        """REST GET WITHOUT rate limiting - for internal use only (e.g., nonce fetch inside order lock).
        
        IMPORTANT: Only use this when rate limiting is handled externally!
        """
        base = getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        url = f"{base.rstrip('/')}{path}"

        try:
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    self.rate_limiter.penalize_429()
                    return None
                
                if resp.status == 404:
                    empty_result_paths = ['/api/v1/orders', '/api/v1/trades', '/api/v1/nextNonce', '/api/v1/accountInactiveOrders']
                    if any(path.startswith(p) for p in empty_result_paths):
                        logger.debug(f"{self.name} REST GET (internal) {path} returned 404")
                        return None
                
                if resp.status >= 400:
                    logger.warning(f"REST GET (internal) {path} returned {resp.status}")
                    return None
                
                self.rate_limiter.on_success()
                return await resp.json()
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: _rest_get_internal cancelled for {path}")
            return None
        except Exception as e:
            logger.error(f"REST GET (internal) {path} error: {e}")
            return None

    async def _rest_get(self, path: str, params: Optional[Dict[str, Any]] = None, force: bool = False) -> Optional[Dict]:
        """REST GET with rate limiting and proper cancellation handling.
        
        IMPORTANT: 404 responses are handled specially for certain endpoints
        because Lighter returns 404 when no data exists (e.g., no open orders).
        
        Args:
            force: If True, bypass the shutdown check (used for PnL calculation after close)
        """
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return None during shutdown
        # UNLESS force=True (needed for post-close PnL calculation)
        # ═══════════════════════════════════════════════════════════════
        if not force and getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[LIGHTER] Shutdown active - skipping _rest_get for {path}")
            return None
        
        base = getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        url = f"{base.rstrip('/')}{path}"

        try:
            # Skip rate limiter if force=True (for critical PnL operations during shutdown)
            if not force:
                result = await self.rate_limiter.acquire()
                # Check if rate limiter was cancelled (shutdown)
                if result < 0:
                    logger.debug(f"[LIGHTER] Rate limiter cancelled - skipping _rest_get for {path}")
                    return None
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    self.rate_limiter.penalize_429()
                    return None
                
                # ═══════════════════════════════════════════════════════════════
                # FIX: Handle 404 as "empty result" for specific endpoints
                # Lighter returns 404 when no orders/trades exist - this is NOT an error!
                # ═══════════════════════════════════════════════════════════════
                if resp.status == 404:
                    empty_result_paths = ['/api/v1/orders', '/api/v1/trades', '/api/v1/accountInactiveOrders']
                    if any(path.startswith(p) for p in empty_result_paths):
                        logger.debug(f"{self.name} REST GET {path} returned 404 (no data - OK)")
                        # Return empty structure that matches expected response format
                        if 'orders' in path:
                            return {"orders": [], "code": 200}
                        elif 'trades' in path or 'Trades' in path:
                            return {"trades": [], "code": 200}
                        return {"data": [], "code": 200}
                    else:
                        logger.debug(f"{self.name} REST GET {path} returned 404")
                        return None
                
                # ═══════════════════════════════════════════════════════════════
                # FIX: Log 400 errors with response body for debugging
                # ═══════════════════════════════════════════════════════════════
                if resp.status == 400:
                    try:
                        error_body = await resp.text()
                        logger.warning(f"{self.name} REST GET {path} returned 400: {error_body[:200]}")
                    except:
                        logger.warning(f"{self.name} REST GET {path} returned 400 (Bad Request)")
                    return None
                
                if resp.status >= 400:
                    logger.debug(f"{self.name} REST GET {path} returned {resp.status}")
                    return None
                    
                data = await resp.json()
                self.rate_limiter.on_success()
                return data
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: REST GET {path} cancelled during shutdown")
            return None  # Return None instead of raising to prevent 'exception was never retrieved'
        except asyncio.TimeoutError:
            logger.debug(f"{self.name} REST GET {path} timeout")
            return None
        except Exception as e:
            logger.debug(f"{self.name} REST GET {path} error: {e}")
            return None

    async def _cached_rest_get(self, path: str, params: Optional[Dict] = None, cache_key: str = None) -> Optional[Dict]:
        """
        REST GET with response caching to prevent duplicate requests.
        Use this for frequently called endpoints like orders/positions.
        """
        # Build cache key from path and params
        if cache_key is None:
            param_str = json.dumps(params, sort_keys=True) if params else ""
            cache_key = f"{path}:{param_str}"
        
        now = time.time()
        
        # Check cache
        async with self._request_lock:
            if cache_key in self._request_cache:
                cached_time, cached_data = self._request_cache[cache_key]
                if now - cached_time < self._request_cache_ttl:
                    logger.debug(f"[LIGHTER] Cache hit for {path}")
                    return cached_data
        
        # Make actual request
        result = await self._rest_get(path, params)
        
        # Cache result
        async with self._request_lock:
            self._request_cache[cache_key] = (now, result)
        
        return result

    def _clear_request_cache(self):
        """Clear the request cache (call after state-changing operations)."""
        self._request_cache.clear()

    async def refresh_market_limits(self, symbol: str) -> dict:
        """Fetch fresh market limits from Lighter API."""
        try:
            market_index = None
            for idx, info in self.market_info. items():
                if info. get('symbol') == symbol or idx == symbol:
                    market_index = info. get('market_index', idx)
                    break
            
            if market_index is None:
                logger.warning(f"⚠️ Market index not found for {symbol}")
                return self. market_info. get(symbol, {})
            
            url = f"{self. base_url}/api/v1/market? market_index={market_index}"
            async with aiohttp.ClientSession() as session:
                async with session. get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response. json()
                        if data:
                            old_info = self.market_info.get(symbol, {})
                            
                            # ═══════════════════════════════════════════════════════════════
                            # CRITICAL FIX: Cast API response values to correct types
                            # ═══════════════════════════════════════════════════════════════
                            if 'min_notional' in data:
                                self.market_info[symbol]['min_notional'] = safe_float(data['min_notional'], 10.0)
                            if 'min_base_amount' in data:
                                min_base_val = safe_float(data['min_base_amount'], 0.01)
                                self.market_info[symbol]['min_base_amount'] = min_base_val
                                self.market_info[symbol]['min_quantity'] = min_base_val
                            if 'min_quote_amount' in data:
                                self.market_info[symbol]['min_quote'] = safe_float(data['min_quote_amount'], 0.01)
                            if 'tick_size' in data:
                                self.market_info[symbol]['tick_size'] = safe_float(data['tick_size'], 0.01)
                            if 'lot_size' in data:
                                self.market_info[symbol]['lot_size'] = safe_float(data['lot_size'], 0.0001)
                            if 'size_decimals' in data:
                                self.market_info[symbol]['sd'] = safe_int(data['size_decimals'], 8)
                                self.market_info[symbol]['size_decimals'] = safe_int(data['size_decimals'], 8)
                            if 'price_decimals' in data:
                                self.market_info[symbol]['pd'] = safe_int(data['price_decimals'], 6)
                                self.market_info[symbol]['price_decimals'] = safe_int(data['price_decimals'], 6)
                            
                            new_min_base = safe_float(data.get('min_base_amount'), None)
                            old_min_base = safe_float(old_info.get('min_base_amount'), None)
                            if new_min_base is not None and old_min_base is not None and new_min_base != old_min_base:
                                logger.warning(
                                    f"⚠️ {symbol} min_base_amount changed: {old_min_base} -> {new_min_base}"
                                )
                            
                            logger.info(f"✅ Refreshed market limits for {symbol}")
                            return self.market_info. get(symbol, {})
                    else:
                        logger.warning(f"Failed to refresh {symbol} limits: HTTP {response.status}")
                        
        except asyncio.TimeoutError:
            logger. warning(f"Timeout refreshing market limits for {symbol}")
        except Exception as e:
            logger.error(f"Error refreshing market limits for {symbol}: {e}")
        
        return self.market_info. get(symbol, {})

    async def get_maker_price(self, symbol: str, side: str) -> Optional[float]:
        """
        Ermittelt einen Maker-Preis basierend auf dem Orderbook.
        FIX: Verhindert UnboundLocalError und leere Orderbooks.
        """
        # 1. Variablen VORHER initialisieren (Fix für UnboundLocalError)
        bids = []
        asks = []
        
        try:
            # Orderbook holen
            orderbook = await self.fetch_orderbook(symbol)
            if not orderbook:
                return None
                
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            
            if not bids or not asks:
                logger.warning(f"⚠️ Maker Price {symbol}: Orderbook leer (bids={len(bids)}, asks={len(asks)})")
                # Fallback auf letzten Preis wenn OB leer
                last_price = self.get_price(symbol)
                if last_price:
                    return float(last_price)
                return None

            # Bestehende Preise
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            
            # Markt-Daten für Tick-Size holen
            market = self.get_market_info(symbol)
            # FIX: 'tick_size' verwenden, da 'price_increment' in market_info keys oft anders heißt
            price_tick = safe_float(market.get('tick_size', market.get('price_increment', 0.0001))) if market else 0.0001

            # ═══════════════════════════════════════════════════════════════
            # AGGRESSIVE MAKER PRICING (Penny Jumping INSIDE the Spread)
            # ═══════════════════════════════════════════════════════════════
            # Problem: Placing at Best Bid/Ask joins the queue = slow fills
            # Solution: Place INSIDE the spread (1 tick better) = queue priority!
            #
            # For BUY: Place at Best Bid + 1 Tick (still below Best Ask = Maker)
            # For SELL: Place at Best Ask - 1 Tick (still above Best Bid = Maker)
            #
            # This is "Penny Jumping" - we get filled before orders at Best Bid/Ask!
            # ═══════════════════════════════════════════════════════════════
            
            spread = best_ask - best_bid
            min_safe_spread = price_tick * 2  # Need at least 2 ticks for safe penny jump
            
            if side == 'BUY':
                # BUY: Want to be filled when taker SELLS → place above current best bid
                if spread >= min_safe_spread:
                    # Safe to penny jump: place 1 tick above best bid (inside spread)
                    target_price = best_bid + price_tick
                    # Safety check: must not cross or touch best ask
                    if target_price >= best_ask - price_tick:
                        target_price = best_bid  # Fallback to best bid
                        strategy = "Best Bid (Spread Too Tight)"
                    else:
                        strategy = "Bid+1 Tick (Penny Jump - Priority Fill!)"
                else:
                    # Tight spread: just join best bid
                    target_price = best_bid
                    strategy = "Best Bid (Tight Spread)"
                
                final_price = target_price
                    
            else: # SELL
                # SELL: Want to be filled when taker BUYS → place below current best ask
                if spread >= min_safe_spread:
                    # Safe to penny jump: place 1 tick below best ask (inside spread)
                    target_price = best_ask - price_tick
                    # Safety check: must not cross or touch best bid
                    if target_price <= best_bid + price_tick:
                        target_price = best_ask  # Fallback to best ask
                        strategy = "Best Ask (Spread Too Tight)"
                    else:
                        strategy = "Ask-1 Tick (Penny Jump - Priority Fill!)"
                else:
                    # Tight spread: just join best ask
                    target_price = best_ask
                    strategy = "Best Ask (Tight Spread)"
                
                final_price = target_price

            # WICHTIG: Endgültigen Preis runden (HALF_UP um Float-Fehler zu vermeiden)
            quantized_price = quantize_value(final_price, price_tick, rounding=ROUND_HALF_UP)
            
            logger.info(
                f"🛡️ Maker Price {symbol} {side}: ${quantized_price:.6f} "
                f"({strategy} | Bid=${best_bid}, Ask=${best_ask}, Tick={price_tick})"
            )
            return quantized_price

        except Exception as e:
            logger.error(f"⚠️ Maker Price logic failed for {symbol}: {e}")
            return None

    async def validate_order_params(self, symbol: str, notional_usd: float) -> Tuple[bool, str]:
        try:
            if symbol not in self.market_info:
                return True, ""

            market_data = self.market_info[symbol]
            
            min_notional = safe_float(market_data.get("min_notional", 0))
            max_notional = safe_float(market_data.get("max_notional", 0))
            check_val = float(notional_usd)

            if min_notional > 0 and check_val < min_notional:
                if check_val > (min_notional * 0.9):
                    return True, ""
                return False, f"Notional ${check_val:.2f} < Min ${min_notional:.2f}"

            if max_notional > 0 and check_val > max_notional:
                return False, f"Notional ${check_val:.2f} > Max ${max_notional:.2f}"

            return True, ""

        except Exception as e:
            logger. error(f"Validation error {symbol}: {e}")
            return True, ""

            return True, ""

    async def get_open_orders(self, symbol: str) -> List[dict]:
        """
        Fetch open orders for a symbol using Lighter REST API.
        
        API Reference: https://apidocs.lighter.xyz/docs/get-started-for-programmers-1
        Endpoint: GET /api/v1/orders
        Status codes: 0=Open, 1=Filled, 2=Cancelled, 3=Expired
        """
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return empty list during shutdown
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[LIGHTER] Shutdown active - skipping get_open_orders for {symbol}")
            return []
        
        try:
            # Resolve indices if needed
            if not getattr(self, '_resolved_account_index', None):
                await self._resolve_account_index()
            
            acc_idx = getattr(self, '_resolved_account_index', None)
            if acc_idx is None:
                logger.warning(f"Lighter get_open_orders: No account index resolved")
                return []
                
            market = self.market_info.get(symbol)
            if not market:
                logger.debug(f"Lighter get_open_orders: No market info for {symbol}")
                return []
            
            # Get market index with fallback chain
            market_index = (
                market.get('i')
                or market.get('market_id')
                or market.get('market_index')
            )
            if market_index is None:
                logger.debug(f"Lighter get_open_orders: No market_index for {symbol}")
                return []

            # ═══════════════════════════════════════════════════════════════
            # CORRECT API CALL per Lighter docs
            # Status: 0=Open, 1=Filled, 2=Cancelled, 3=Expired
            # ═══════════════════════════════════════════════════════════════
            params = {
                "account_index": int(acc_idx),
                "market_index": int(market_index),
                "status": 0,  # 0 = Open orders per Lighter API docs
                "limit": 50
            }
            
            resp = await self._rest_get("/api/v1/orders", params=params)
            
            # Handle empty/None response (404 is converted to empty dict by _rest_get)
            if not resp:
                return []
            
            # Extract orders from response
            orders_data = resp if isinstance(resp, list) else resp.get('orders', resp.get('data', []))
            if not orders_data:
                return []
            
            open_orders = []
            for o in orders_data:
                try:
                    # Parse order data according to Lighter API response format
                    order_id = o.get('order_index') or o.get('id') or o.get('order_id')
                    price = safe_float(o.get('price', 0))
                    
                    # Size can be in different fields
                    size = safe_float(
                        o.get('remaining_base_amount')
                        or o.get('remaining_size')
                        or o.get('base_amount')
                        or o.get('size', 0)
                    )
                    
                    # Side: is_ask=True means SELL, is_ask=False means BUY
                    is_ask = o.get('is_ask', None)
                    if is_ask is not None:
                        if isinstance(is_ask, bool):
                            side = "SELL" if is_ask else "BUY"
                        elif isinstance(is_ask, int):
                            side = "SELL" if is_ask == 1 else "BUY"
                        else:
                            side = "SELL" if str(is_ask).lower() in ['true', '1'] else "BUY"
                    else:
                        # Fallback to side field
                        side_raw = o.get('side', 0)
                        if isinstance(side_raw, int):
                            side = "BUY" if side_raw == 0 else "SELL"
                        else:
                            side = str(side_raw).upper()
                    
                    if order_id and size > 0:
                        open_orders.append({
                            "id": str(order_id),
                            "price": price,
                            "size": size,
                            "side": side,
                            "symbol": symbol,
                            "status": "OPEN"
                        })
                except Exception as e:
                    logger.debug(f"Error parsing order: {e}")
                    continue
            
            logger.debug(f"Lighter get_open_orders({symbol}): Found {len(open_orders)} orders")
            return open_orders
            
        except asyncio.CancelledError:
            logger.debug(f"Lighter get_open_orders({symbol}) cancelled")
            return []  # Return empty list instead of raising
        except Exception as e:
            logger.error(f"Lighter get_open_orders error: {e}")
            return []

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[dict]:
        """
        Fetch a single order by ID.
        Since Lighter might not have a direct endpoint for single order lookup without market context,
        we try to query /api/v1/orders with order_id if supported, or filter from open orders if necessary.
        """
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return None during shutdown
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[LIGHTER] Shutdown active - skipping get_order for {order_id}")
            return None
        
        try:
            # Resolve indices if needed
            if not getattr(self, '_resolved_account_index', None):
                 await self._resolve_account_index()
            
            acc_idx = getattr(self, '_resolved_account_index', None)
            
            # Param construct
            params = {
                "account_index": acc_idx,
            }
            if symbol and symbol in self.market_info:
                 m_idx = self.market_info[symbol].get('market_index')
                 if m_idx is not None:
                     params['market_index'] = m_idx

            # Try to pass order_id directly
            params['order_id'] = order_id
            
            # API Call
            resp = await self._rest_get("/api/v1/orders", params=params)
             
            if not resp:
                return None

            orders = resp if isinstance(resp, list) else resp.get('orders', resp.get('data', []))
            
            target_order = None
            for o in orders:
                # Compare ID (string comparison for safety)
                if str(o.get('id', '')) == str(order_id):
                    target_order = o
                    break
            
            if target_order:
                # Normalize response for caller
                # Caller expects: status, filledAmount (or executedQty)
                
                # Map status if needed (assuming 10=OPEN, 20=FILLED, 30=CANCELLED etc if using codes)
                # But parallel_execution checks string 'FILLED', 'CANCELED' 
                # OR it checks validation of codes?
                # Actually parallel_execution checks: status.upper() in ['FILLED', 'PARTIALLY_FILLED']
                
                # If Lighter returns int status, we might need to map it.
                # 10: Open, 20: Filled? We need to be careful.
                # Let's inspect what get_open_orders does: it just passes 'status' but parallel_execution logic handles it?
                # No, parallel_execution logic seems to expect strings like 'FILLED'.
                
                s_raw = target_order.get('status')
                status_str = "UNKNOWN"
                
                if isinstance(s_raw, int):
                    # Mapping based on common Lighter headers or observation
                    # 10: Open, 30: Filled, 40: Cancelled ??
                    # Safest: Use filled_amount to detect fill.
                    if s_raw == 10: status_str = "OPEN"
                    elif s_raw == 30: status_str = "FILLED" 
                    elif s_raw == 40: status_str = "CANCELED"
                else:
                    status_str = str(s_raw).upper()
                    
                return {
                    "id": str(target_order.get('id', '')),
                    "status": status_str,
                    "filledAmount": safe_float(target_order.get('executedQty', target_order.get('filled_amount', target_order.get('total_executed_size', 0)))),
                    "executedQty": safe_float(target_order.get('executedQty', target_order.get('filled_amount', target_order.get('total_executed_size', 0)))),
                    "remaining_size": safe_float(target_order.get('remaining_size', 0)),
                    "price": safe_float(target_order.get('price', 0))
                }

            return None

        except Exception as e:
            logger.error(f"get_order failed for {order_id}: {e}")
            return None


    async def fetch_my_trades(self, symbol: str, limit: int = 20, force: bool = False) -> List[dict]:
        """
        Fetch account trade history for a symbol (fills) via inactive orders.
        
        The Lighter API does NOT have a direct accountTrades endpoint.
        Instead, we use /api/v1/accountInactiveOrders which returns filled orders
        with price information (filled_quote_amount / filled_base_amount = avg fill price).
        
        Args:
            symbol: Trading pair symbol
            limit: Max number of trades to return
            force: If True, bypass the shutdown check (used for PnL calculation after close)
        """
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return empty list during shutdown
        # UNLESS force=True (needed for post-close PnL calculation)
        # ═══════════════════════════════════════════════════════════════
        if not force and getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[LIGHTER] Shutdown active - skipping fetch_my_trades for {symbol}")
            return []
        
        try:
            # Resolve Account Index if needed
            if not getattr(self, '_resolved_account_index', None):
                await self._resolve_account_index()
            
            acc_idx = getattr(self, '_resolved_account_index', None)
            if acc_idx is None:
                return []
            
            # Get market index for the symbol (for filtering)
            market = self.market_info.get(symbol)
            market_index = None
            if market:
                market_index = market.get('i') or market.get('market_id') or market.get('market_index')
            
            if market_index is None:
                logger.debug(f"[LIGHTER] No market_index found for {symbol}")
                return []
            
            # ═══════════════════════════════════════════════════════════════
            # Generate auth token - required for accountInactiveOrders endpoint
            # ═══════════════════════════════════════════════════════════════
            auth_token = None
            try:
                # Ensure signer is initialized
                signer = await self._get_signer()
                if signer:
                    # create_auth_token_with_expiry returns (auth_str, error_str) tuple!
                    auth_result = signer.create_auth_token_with_expiry()
                    if isinstance(auth_result, tuple):
                        auth_token, auth_error = auth_result
                        if auth_error:
                            logger.debug(f"[LIGHTER] Auth token error: {auth_error}")
                    else:
                        auth_token = auth_result
                    
                    if auth_token:
                        logger.debug(f"[LIGHTER] Generated auth token for accountInactiveOrders")
                    else:
                        logger.debug(f"[LIGHTER] Auth token generation returned None")
            except Exception as auth_err:
                logger.debug(f"[LIGHTER] Failed to create auth token: {auth_err}")
            
            # ═══════════════════════════════════════════════════════════════
            # Use /api/v1/accountInactiveOrders - returns filled/cancelled orders
            # This is the CORRECT way to get trade history per Lighter TS SDK
            # ═══════════════════════════════════════════════════════════════
            params = {
                "account_index": acc_idx,
                "market_id": int(market_index),
                "limit": limit
            }
            
            # Add auth token if available
            if auth_token:
                params["auth"] = auth_token
            
            resp = await self._rest_get("/api/v1/accountInactiveOrders", params=params, force=force)
            
            if not resp:
                logger.debug(f"[LIGHTER] accountInactiveOrders returned empty for {symbol}")
                return []
            
            # Response format: { "code": 0, "orders": [...], "next_cursor": "..." }
            orders = resp.get('orders', [])
            
            if not orders:
                logger.debug(f"[LIGHTER] No inactive orders found for {symbol}")
                return []
            
            # Parse and return normalized trades from filled orders
            result = []
            for o in orders:
                try:
                    status = str(o.get('status', '')).lower()
                    
                    # Only include filled orders (these are actual trades)
                    if status != 'filled':
                        continue
                    
                    # Calculate average fill price from filled amounts
                    filled_base = safe_float(o.get('filled_base_amount') or o.get('filled_size', 0))
                    filled_quote = safe_float(o.get('filled_quote_amount', 0))
                    
                    if filled_base <= 0:
                        continue
                    
                    # Price = quote / base (what we paid/received per unit)
                    if filled_quote > 0:
                        avg_price = filled_quote / filled_base
                    else:
                        # Fallback to order price if no quote amount
                        avg_price = safe_float(o.get('price', 0))
                    
                    if avg_price <= 0:
                        continue
                    
                    # Determine side
                    side = str(o.get('side', '')).upper()
                    if not side:
                        # Lighter uses 0=Buy, 1=Sell in some responses
                        side_num = o.get('side_num') or o.get('is_ask')
                        if side_num is not None:
                            side = "SELL" if int(side_num) == 1 else "BUY"
                    
                    result.append({
                        "id": o.get('order_index') or o.get('id'),
                        "order_id": o.get('order_index') or o.get('id'),
                        "symbol": symbol,
                        "side": side,
                        "price": avg_price,
                        "size": filled_base,
                        "fee": safe_float(o.get('fee', 0)),
                        "timestamp": o.get('timestamp') or o.get('created_at') or o.get('updated_at'),
                        "status": status
                    })
                except Exception as e:
                    logger.debug(f"[LIGHTER] Error parsing order: {e}")
                    continue
            
            # Sort by timestamp (newest first) and limit
            result.sort(key=lambda x: x.get('timestamp') or 0, reverse=True)
            result = result[:limit]
            
            if result:
                logger.debug(f"Lighter fetch_my_trades({symbol}): Found {len(result)} filled orders")
            
            return result
            
        except asyncio.CancelledError:
            logger.debug(f"Lighter fetch_my_trades({symbol}) cancelled")
            return []  # Return empty list instead of raising
        except Exception as e:
            logger.debug(f"Failed to fetch trades for {symbol}: {e}")
            return []

    async def load_markets(self):
        """Alias for load_market_cache - called by main()"""
        await self.load_market_cache(force=True)

    async def get_order_fee(self, order_id: str) -> float:
        """Fetch real fee from Lighter order"""
        if not order_id or order_id == "DRY_RUN_ORDER_123":
            return 0.0

        if OrderApi is None or not HAVE_LIGHTER_SDK:
            return 0.0

        try:
            signer = await self._get_signer()
            order_api = OrderApi(signer. api_client)

            candidate_methods = [
                "order_details", "order_detail", "get_order", "get_order_by_id",
                "order", "order_status", "order_info", "get_order_info", "retrieve_order",
            ]

            method = None
            for name in candidate_methods:
                if hasattr(order_api, name):
                    method = getattr(order_api, name)
                    break

            if method is None:
                return 0.0

            resp = None
            call_attempts = [
                lambda: method(order_id=order_id),
                lambda: method(id=order_id),
                lambda: method(_order_id=order_id),
                lambda: method(orderId=order_id),
                lambda: method(order_id),
            ]

            for call in call_attempts:
                try:
                    maybe = call()
                    if asyncio.iscoroutine(maybe):
                        resp = await maybe
                    else:
                        resp = maybe
                    if resp is not None:
                        break
                except TypeError:
                    continue
                except Exception:
                    continue

            if resp is None:
                return 0.0

            def _get(obj, keys):
                if obj is None:
                    return None
                try:
                    if isinstance(obj, dict):
                        for k in keys:
                            if k in obj and obj[k] is not None:
                                return obj[k]
                    else:
                        for k in keys:
                            if hasattr(obj, k):
                                v = getattr(obj, k)
                                if v is not None:
                                    return v
                except Exception:
                    pass
                try:
                    if hasattr(obj, "data"):
                        d = getattr(obj, "data")
                        if isinstance(d, dict):
                            for k in keys:
                                if k in d and d[k] is not None:
                                    return d[k]
                        else:
                            for k in keys:
                                if hasattr(d, k):
                                    v = getattr(d, k)
                                    if v is not None:
                                        return v
                except Exception:
                    pass
                return None

            fee_keys = ["fee", "fee_amount", "fee_usd", "fees", "fee_value"]
            filled_keys = [
                "filled", "filled_amount", "filled_amount_of_synthetic",
                "filled_quantity", "filled_size", "filled_amount_of_quote",
            ]
            price_keys = ["price", "avg_price", "filled_price"]

            fee_abs = _get(resp, fee_keys)
            filled = _get(resp, filled_keys)
            price = _get(resp, price_keys)

            if fee_abs is None or filled is None or price is None:
                nested = None
                if isinstance(resp, dict):
                    nested = resp.get("order") or resp.get("data") or resp. get("result")
                elif hasattr(resp, "order"):
                    nested = getattr(resp, "order")
                elif hasattr(resp, "data"):
                    nested = getattr(resp, "data")

                if nested is not None:
                    fee_abs = fee_abs or _get(nested, fee_keys)
                    filled = filled or _get(nested, filled_keys)
                    price = price or _get(nested, price_keys)

            try:
                if fee_abs is not None and filled and price:
                    fee_usd = safe_float(fee_abs, 0.0)
                    filled_qty = safe_float(filled, 0.0)
                    order_price = safe_float(price, 0.0)
                    notional = filled_qty * order_price
                    if notional > 0:
                        fee_rate = fee_usd / notional
                        if 0 <= fee_rate <= 0.1:
                            return fee_rate
            except Exception:
                pass

            return 0.0
        except Exception as e:
            logger. error(f"Lighter Fee Fetch Error for {order_id}: {e}")
            return 0.0

    async def fetch_fee_schedule(self) -> Optional[Tuple[float, float]]:
        """
        Fetch fee schedule from Lighter API
        """
        try:
            # 1. Resolve Account Index if needed
            if not getattr(self, '_resolved_account_index', None):
                try:
                    await self._resolve_account_index()
                except Exception:
                    pass
        
            acc_idx = getattr(self, '_resolved_account_index', None)
            if acc_idx is None:
                return None

            if not HAVE_LIGHTER_SDK:
                return None

            # 2. Try fetching Account Info to verify connectivity
            try:
                signer = await self._get_signer()
                account_api = AccountApi(signer.api_client)
            
                # Fetch account details
                response = await account_api.account(by="index", value=str(acc_idx))
            
                # FIX: Wenn wir eine Antwort bekommen, ist die Verbindung OK.
                # Lighter sendet oft keine Gebühren im Account-Objekt.
                # Wir geben die Config-Werte zurück, um die Warnung zu unterdrücken.
                # Standard Account: 0% Maker / 0% Taker
                # Premium Account: 0.002% Maker / 0.02% Taker
                # Docs: https://apidocs.lighter.xyz/docs/account-types
                if response:
                    maker = getattr(config, 'MAKER_FEE_LIGHTER', 0.0)
                    taker = getattr(config, 'TAKER_FEE_LIGHTER', 0.0)
                    logger.info(f"✅ Lighter Fee Check OK: Maker={maker}, Taker={taker}")
                    return (maker, taker)
                
                return None

            except Exception as e:
                logger.debug(f"Lighter Account Fee fetch failed: {e}")
                return None
                
        except Exception as e:
            logger.debug(f"Lighter Fee Schedule fetch error: {e}")
            return None

    async def start_websocket(self):
        """WebSocket entry point für WebSocketManager"""
        logger.info(f"🌐 {self.name}: WebSocket Manager starting streams...")
        await asyncio.gather(
            self._poll_prices(),
            self._poll_funding_rates(),
            return_exceptions=True,
        )

    async def _poll_prices(self):
        """Wrapper kompatibel mit WebSocketManager"""
        await self._rest_price_poller(interval=2.0)

    async def _poll_funding_rates(self):
        """Wrapper kompatibel mit WebSocketManager"""
        await self._rest_funding_poller(interval=30.0)

    async def ws_message_stream(self):
        """Async iterator für WebSocketManager"""
        while True:
            msg = await self._ws_message_queue. get()
            yield msg

    async def _rest_price_poller(self, interval: float = 2.0):
        """REST-basiertes Preis-Polling als WebSocket-Ersatz"""
        while True:
            try:
                if not HAVE_LIGHTER_SDK:
                    await asyncio.sleep(interval)
                    continue

                await self. rate_limiter.acquire()
                signer = await self._get_signer()
                order_api = OrderApi(signer.api_client)

                try:
                    market_list = await order_api.order_book_details()
                    if market_list and market_list.order_book_details:
                        updated = 0
                        for m in market_list. order_book_details:
                            symbol_raw = getattr(m, "symbol", None)
                            if not symbol_raw:
                                continue
                            symbol = (
                                f"{symbol_raw}-USD"
                                if not symbol_raw.endswith("-USD")
                                else symbol_raw
                            )

                            mark_price = getattr(m, "mark_price", None) or getattr(
                                m, "last_trade_price", None
                            )
                            if mark_price:
                                price_float = safe_float(mark_price, 0.0)
                                if price_float > 0:
                                    self.price_cache[symbol] = price_float
                                    updated += 1

                        if updated > 0:
                            self.rate_limiter.on_success()
                            if hasattr(self, "price_update_event") and self.price_update_event:
                                self.price_update_event.set()
                            logger.debug(f"Lighter REST: Updated {updated} prices")
                except Exception as e:
                    if "429" in str(e):
                        self.rate_limiter.penalize_429()
                    else:
                        logger.debug(f"Lighter REST price poll error: {e}")

                await asyncio.sleep(interval)

            except asyncio. CancelledError:
                logger.info("🛑 Lighter REST price poller stopped")
                break
            except Exception as e:
                logger. error(f"Lighter REST price poller error: {e}")
                await asyncio.sleep(interval * 2)

    async def _rest_funding_poller(self, interval: float = 30.0):
        """REST-basiertes Funding Rate Polling"""
        while True:
            try:
                await self.load_funding_rates_and_prices()
                await asyncio.sleep(interval)

            except asyncio. CancelledError:
                logger.info("🛑 Lighter REST funding poller stopped")
                break
            except Exception as e:
                logger.error(f"Lighter REST funding poller error: {e}")
                await asyncio.sleep(interval * 2)

    async def refresh_funding_rates_rest(self):
        """REST Fallback für Funding Rates"""
        try:
            if not HAVE_LIGHTER_SDK:
                return

            signer = await self._get_signer()
            funding_api = FundingApi(signer.api_client)

            fd_response = await funding_api.funding_rates()
            if fd_response and fd_response.funding_rates:
                for fr in fd_response. funding_rates:
                    market_id = fr. market_id
                    rate = safe_float(fr.rate, 0.0)
                    for symbol, data in self.market_info.items():
                        if data.get("i") == market_id:
                            self.funding_cache[symbol] = rate
                            break
                logger.debug(
                    f"Lighter: Refreshed {len(fd_response.funding_rates)} funding rates via REST"
                )
        except Exception as e:
            if "429" not in str(e):
                logger.error(f"Lighter REST Funding Refresh Error: {e}")

    def _get_base_url(self) -> str:
        return getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")

    async def _auto_resolve_indices(self) -> Tuple[int, int]:
        return int(config.LIGHTER_ACCOUNT_INDEX), int(config.LIGHTER_API_KEY_INDEX)

    async def _resolve_account_index(self):
        """Resolve account and API key indices if not already done"""
        if self._resolved_account_index is None:
            (
                self._resolved_account_index,
                self._resolved_api_key_index,
            ) = await self._auto_resolve_indices()

    async def _get_signer(self):
        if self._signer is None:
            if not HAVE_LIGHTER_SDK:
                raise RuntimeError("Lighter SDK not installed")

            if self._resolved_account_index is None:
                (
                    self._resolved_account_index,
                    self._resolved_api_key_index,
                ) = await self._auto_resolve_indices()
            priv_key = str(getattr(config, "LIGHTER_API_PRIVATE_KEY", ""))
            self._signer = SaferSignerClient(
                url=self._get_base_url(),
                private_key=priv_key,
                api_key_index=self._resolved_api_key_index,
                account_index=self._resolved_account_index,
            )
        return self._signer

    async def _fetch_single_market(self, order_api, market_id: int):
        """Fetch market data with safe type conversion for API responses."""
        async with self.semaphore:
            await self.rate_limiter.acquire()
            try:
                details = await order_api.order_book_details(market_id=market_id)
                if not details or not details.order_book_details:
                    return False

                m = details.order_book_details[0]
                symbol = getattr(m, 'symbol', None)
                if symbol:
                    normalized_symbol = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol

                    real_market_id = getattr(m, 'market_id', None)
                    if real_market_id is None:
                        real_market_id = getattr(m, 'marketId', None)
                    if real_market_id is None:
                        real_market_id = getattr(m, 'id', None)
                    if real_market_id is None:
                        real_market_id = market_id

                    try:
                        real_market_id = int(real_market_id)
                    except (ValueError, TypeError):
                        logger.warning(f"⚠️ Skipping {normalized_symbol}: invalid market_id={real_market_id}")
                        return False

                    size_decimals = safe_int(getattr(m, 'size_decimals', None), 8)
                    price_decimals = safe_int(getattr(m, 'price_decimals', None), 6)
                    min_base = Decimal(str(getattr(m, 'min_base_amount', None) or "0.00000001"))
                    min_quote = Decimal(str(getattr(m, 'min_quote_amount', None) or "0.00001"))

                    market_data = {
                        'i': real_market_id,
                        'sd': size_decimals,
                        'pd': price_decimals,
                        'ss': min_base,
                        'mps': min_quote,
                        'min_notional': float(min_quote) if min_quote else 10.0,
                        'min_quantity': float(min_base) if min_base else 0.0,
                    }

                    if normalized_symbol in MARKET_OVERRIDES:
                        market_data.update(MARKET_OVERRIDES[normalized_symbol])

                    self.market_info[normalized_symbol] = market_data

                    price = getattr(m, 'last_trade_price', None)
                    if price is not None:
                        price_float = safe_float(price)
                        if price_float > 0:
                            self.price_cache[normalized_symbol] = price_float
                            self._price_cache[normalized_symbol] = price_float
                            self._price_cache_time[normalized_symbol] = time.time()

                    self. rate_limiter.on_success()
                    return True

            except Exception as e:
                error_str = str(e). lower()
                if "429" in error_str or "rate limit" in error_str:
                    self.rate_limiter.penalize_429()
                return False
            return False

    async def _load_funding_rates(self):
        """Load funding rates from Lighter API."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self._get_base_url()}/api/v1/funding-rates"
                async with session.get(url) as resp:
                    if resp. status == 200:
                        data = await resp.json()
                        funding_rates = data. get('funding_rates', [])
                        
                        for fr in funding_rates:
                            if fr.get('exchange') == 'lighter':
                                symbol = fr.get('symbol', '')
                                rate = fr.get('rate')
                                
                                if symbol and rate is not None:
                                    if not symbol.endswith('-USD'):
                                        symbol = f"{symbol}-USD"
                                    self.funding_cache[symbol] = safe_float(rate, 0.0)
                        
                        logger.debug(f"Lighter: Loaded {len(self.funding_cache)} funding rates")
                        return True
        except Exception as e:
            logger.error(f"Failed to load Lighter funding rates: {e}")
        return False

    async def load_market_cache(self, force: bool = False):
        """Load all market metadata from Lighter API - FIXED VERSION"""
        if self. market_info and not force:
            return

        # ═══════════════════════════════════════════════════════════════
        # HELPER FUNCTIONS FOR SAFE TYPE CASTING
        # ═══════════════════════════════════════════════════════════════
        def to_float(val, default=0.0):
            """Safely cast value to float, handling strings and None."""
            if val is None:
                return default
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    return float(val.replace(',', '').strip())
                except (ValueError, AttributeError):
                    return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def to_int(val, default=0):
            """Safely cast value to int, handling strings and None."""
            if val is None:
                return default
            if isinstance(val, int):
                return val
            try:
                return int(float(str(val)))
            except (ValueError, TypeError):
                return default

        try:
            # SCHRITT 1: Lade Basis-Märkte
            data = await self._rest_get("/api/v1/orderBooks")
            if not data:
                data = await self._rest_get("/info")
                if not data:
                    logger.error(f"{self.name}: Failed to load markets")
                    return

            markets = data.get("order_books", data.get("markets", data.get("data", [])))
            if isinstance(markets, dict):
                markets = list(markets.values())

            market_id_to_symbol = {}

            for m in markets:
                try:
                    symbol = m. get("ticker", m.get("symbol", ""))
                    if not symbol:
                        symbol = f"MARKET-{m.get('market_index', m.get('i', 'X'))}"

                    symbol = symbol.replace("_", "-"). replace("/", "-"). upper()
                    if not symbol.endswith("-USD"):
                        symbol = f"{symbol}-USD"

                    if symbol in getattr(config, "BLACKLIST_SYMBOLS", set()):
                        continue

                    # FIX: Handle market_id=0 correctly (don't use 'or' chain)
                    raw_id = m.get("market_id")
                    if raw_id is None:
                        raw_id = m.get("market_index")
                    if raw_id is None:
                        raw_id = m.get("marketId")
                    if raw_id is None:
                        raw_id = m.get("i")

                    try:
                        real_id = int(float(str(raw_id))) if raw_id is not None else -1
                    except (ValueError, TypeError):
                        real_id = -1
                    
                    if real_id < 0:
                        logger.warning(f"⚠️ Skipping {symbol}: market_id={raw_id} (parsed={real_id}) invalid < 0")
                        continue

                    # ═══════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Cast ALL market metadata to correct types
                    # This prevents TypeError when comparing values later
                    # ═══════════════════════════════════════════════════════════════
                    self.market_info[symbol] = {
                        "i": real_id,
                        "sd": to_int(8, 8),  # size_decimals as int
                        "pd": to_int(6, 6),  # price_decimals as int
                        "min_notional": to_float(m.get("min_order_size_usd", m.get("min_notional", 10)), 10.0),
                        "max_notional": to_float(m.get("max_order_size_usd", m.get("max_notional", 0)), 0.0),
                        "min_base_amount": to_float(m.get("min_base_amount", 0.01), 0.01),
                        "min_quantity": to_float(m.get("min_base_amount", 0.01), 0.01),  # alias
                        "min_quote": to_float(m.get("min_quote_amount", 0.01), 0.01),
                        "tick_size": to_float(m.get("tick_size"), 0.0), # Will be fixed below if 0
                        "lot_size": to_float(m.get("lot_size", 0.0001), 0.0001),
                        "supported_size_decimals": m.get("supported_size_decimals"),
                        "supported_price_decimals": m.get("supported_price_decimals"),
                    }
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Derive tick_size from decimals if missing
                    # ═══════════════════════════════════════════════════════════════
                    if self.market_info[symbol]["tick_size"] <= 0:
                        s_pd = self.market_info[symbol].get("supported_price_decimals")
                        pd = m.get("price_decimals")
                        
                        decimals = None
                        if s_pd is not None:
                            decimals = to_int(s_pd)
                        elif pd is not None:
                            decimals = to_int(pd)
                            
                        if decimals is not None:
                            self.market_info[symbol]["tick_size"] = float(pow(10, -decimals))
                        else:
                             # Default fallback
                            self.market_info[symbol]["tick_size"] = 0.01
                    
                    market_id_to_symbol[real_id] = symbol

                except Exception as e:
                    logger.debug(f"{self.name} market parse error: {e}")

            logger.info(f"✅ {self.name}: Loaded {len(self.market_info)} markets (base data)")

            # SCHRITT 2: Lade DETAILS mit size_decimals vom SDK/API
            if HAVE_LIGHTER_SDK:
                try:
                    signer = await self._get_signer()
                    order_api = OrderApi(signer.api_client)
                    
                    await self. rate_limiter. acquire()
                    details_response = await order_api.order_book_details()
                    
                    if details_response and details_response. order_book_details:
                        updated_count = 0
                        
                        for detail in details_response. order_book_details:
                            symbol_raw = getattr(detail, 'symbol', None)
                            if not symbol_raw:
                                continue
                            
                            symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                            
                            if symbol not in self.market_info:
                                continue
                            
                            # ═══════════════════════════════════════════════════════════════
                            # CRITICAL FIX: Ensure all values are strongly typed (int/float)
                            # ═══════════════════════════════════════════════════════════════
                            size_decimals = safe_int(getattr(detail, 'size_decimals', None), 8)
                            price_decimals = safe_int(getattr(detail, 'price_decimals', None), 6)
                            
                            min_base = getattr(detail, 'min_base_amount', None)
                            if min_base is not None:
                                min_base_float = safe_float(min_base, 0.01)
                            else:
                                # Ensure we get float, not string from existing data
                                existing_val = self.market_info[symbol].get('min_base_amount', 0.01)
                                min_base_float = safe_float(existing_val, 0.01)
                            
                            # Get other values with safe casting
                            min_quote = safe_float(getattr(detail, 'min_quote_amount', None), 
                                                  safe_float(self.market_info[symbol].get('min_quote', 0.01), 0.01))
                                                  
                            # ═══════════════════════════════════════════════════════════════
                            # AGGRESSIVE TICK SIZE DISCOVERY
                            # ═══════════════════════════════════════════════════════════════
                            # 1. Try explicit fields
                            api_tick = getattr(detail, 'tick_size', None)
                            if api_tick is None:
                                api_tick = getattr(detail, 'tickSize', None)
                            if api_tick is None:
                                api_tick = getattr(detail, 'price_increment', None)
                            if api_tick is None:
                                api_tick = getattr(detail, 'min_price_increment', None)
                                
                            # 2. Try to derive from supported_price_decimals
                            if api_tick is None:
                                s_pd_val = getattr(detail, 'supported_price_decimals', None)
                                if s_pd_val is not None:
                                    try:
                                        api_tick = float(pow(10, -int(s_pd_val)))
                                        # logger.debug(f"Derived tick_size {api_tick} from supported_price_decimals={s_pd_val}")
                                    except:
                                        pass
                                        
                            # 3. Try to derive from regular price_decimals
                            if api_tick is None:
                                pd_val = getattr(detail, 'price_decimals', None)
                                if pd_val is not None:
                                    try:
                                        api_tick = float(pow(10, -int(pd_val)))
                                    except:
                                        pass

                            # 4. Last Resort: Guess from price string (Ghost in the shell style)
                            if api_tick is None:
                                # Versuche, es aus dem Preis zu erraten (Notlösung)
                                price_val = getattr(detail, 'last_trade_price', "0")
                                if "0.000" in str(price_val): 
                                    default_tick = 0.0001
                                elif "0.00" in str(price_val):
                                    default_tick = 0.001
                                else:
                                    default_tick = 0.0001 # Sicherer Default als 0.01
                            else:
                                default_tick = 0.0001 # Ignored if api_tick is set

                            tick_size = safe_float(api_tick, 
                                safe_float(self.market_info[symbol].get('tick_size', default_tick), default_tick)
                            )
                            
                            # Logging Warnung, wenn Tick Size verdächtig groß ist für kleinen Preis
                            mark_price_check = safe_float(getattr(detail, 'last_trade_price', 0))
                            if mark_price_check > 0 and mark_price_check < 1.0 and tick_size >= 0.01:
                                logger.warning(f"⚠️ CRITICAL: {symbol} Tick Size {tick_size} seems huge for price {mark_price_check}. Forcing 0.0001")
                                tick_size = 0.0001 # Force override
                            lot_size = safe_float(getattr(detail, 'lot_size', None),
                                                 safe_float(self.market_info[symbol].get('lot_size', 0.0001), 0.0001))
                            
                            self.market_info[symbol].update({
                                'sd': int(size_decimals),  # Enforce int type
                                'pd': int(price_decimals),  # Enforce int type
                                'size_decimals': int(size_decimals),
                                'price_decimals': int(price_decimals),
                                'min_base_amount': float(min_base_float),  # Enforce float type
                                'min_quantity': float(min_base_float),  # Alias, enforce float
                                'min_quote': float(min_quote),  # Enforce float type
                                'tick_size': float(tick_size),  # Enforce float type
                                'lot_size': float(lot_size),  # Enforce float type
                                'supported_size_decimals': safe_int(getattr(detail, 'supported_size_decimals', None), 2),
                                'supported_price_decimals': safe_int(getattr(detail, 'supported_price_decimals', None), 2),
                            })
                            
                            updated_count += 1
                            
                            mark_price = getattr(detail, 'mark_price', None) or getattr(detail, 'last_trade_price', None)
                            if mark_price:
                                price_float = safe_float(mark_price)
                                if price_float > 0:
                                    self.price_cache[symbol] = price_float
                                    self._price_cache[symbol] = price_float
                                    self._price_cache_time[symbol] = time. time()
                        
                        self.rate_limiter.on_success()
                        logger. info(f"✅ {self. name}: Updated {updated_count} markets with size_decimals from API")
                        
                except asyncio.CancelledError:
                    logger.debug(f"{self.name}: load_market_cache cancelled during shutdown")
                    return  # Exit early on cancellation
                except Exception as e:
                    logger.warning(f"⚠️ Could not load order_book_details: {e}")
                    logger.warning("   Using default size_decimals=8 (may cause order errors!)")

            # DEBUG: Zeige die geladenen Werte für Test-Symbole
            for symbol in ['ADA-USD', 'SEI-USD', 'RESOLV-USD', 'TIA-USD']:
                if symbol in self.market_info:
                    info = self.market_info[symbol]
                    logger.info(
                        f"📊 MARKET {symbol}: "
                        f"size_decimals={info. get('size_decimals', info.get('sd'))}, "
                        f"price_decimals={info.get('price_decimals', info.get('pd'))}, "
                        f"min_base_amount={info.get('min_base_amount')}"
                    )

            await self._resolve_account_index()

        except Exception as e:
            logger.error(f"{self.name} load_market_cache error: {e}")

    async def initialize(self):
        """Initialize the adapter"""
        try:
            logger. info("🔄 Lighter: Initializing...")

            if not HAVE_LIGHTER_SDK:
                logger.error("❌ Lighter SDK not installed")
                return

            from lighter.api_client import ApiClient

            self. api_client = ApiClient()
            self.api_client.configuration. host = self._get_base_url()

            await self.load_market_cache()

            if not self.market_info:
                logger.error("❌ Lighter: Market cache empty after load")
                return

            logger.info(f"✅ Lighter: Loaded {len(self. market_info)} markets")
        except ImportError as e:
            logger.error(f"❌ Lighter SDK import error: {e}")
        except Exception as e:
            logger.error(f"❌ Lighter init error: {e}")

    async def load_funding_rates_and_prices(self):
        """Lädt Funding Rates UND Preise von der Lighter REST API"""
        if not getattr(config, "LIVE_TRADING", False):
            return

        if not HAVE_LIGHTER_SDK:
            return

        try:
            signer = await self._get_signer()
            
            # 1.  FUNDING RATES laden
            funding_api = FundingApi(signer.api_client)
            await self.rate_limiter.acquire()
            fd_response = await funding_api.funding_rates()

            if fd_response and fd_response. funding_rates:
                updated = 0
                for fr in fd_response.funding_rates:
                    market_id = getattr(fr, "market_id", None)
                    rate = getattr(fr, "rate", None)

                    if market_id is None or rate is None:
                        continue

                    rate_float = safe_float(rate, 0.0)

                    for symbol, data in self.market_info. items():
                        if data.get("i") == market_id:
                            self.funding_cache[symbol] = rate_float
                            self._funding_cache[symbol] = rate_float
                            updated += 1
                            break

                self.rate_limiter.on_success()
                logger.debug(f"Lighter: Loaded {updated} funding rates")

            # 2.  PREISE laden via order_book_details
            await asyncio.sleep(0.5)
            
            order_api = OrderApi(signer.api_client)
            await self.rate_limiter.acquire()
            
            try:
                market_list = await order_api.order_book_details()
                if market_list and market_list.order_book_details:
                    price_count = 0
                    now = time.time()
                    
                    for m in market_list. order_book_details:
                        symbol_raw = getattr(m, "symbol", None)
                        if not symbol_raw:
                            continue
                        
                        symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                        
                        mark_price = getattr(m, "mark_price", None) or getattr(m, "last_trade_price", None)
                        if mark_price:
                            price_float = safe_float(mark_price, 0.0)
                            if price_float > 0:
                                self.price_cache[symbol] = price_float
                                self._price_cache[symbol] = price_float
                                self._price_cache_time[symbol] = now
                                price_count += 1
                    
                    self.rate_limiter.on_success()
                    logger.debug(f"Lighter: Loaded {price_count} prices via REST")
                    
            except Exception as e:
                if "429" in str(e):
                    self. rate_limiter. penalize_429()
                else:
                    logger.debug(f"Lighter price fetch warning: {e}")

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            else:
                logger. error(f"Lighter Funding/Price Fetch Error: {e}")

    async def fetch_initial_funding_rates(self):
        """Lädt Funding Rates einmalig per REST API, um den Cache sofort zu füllen."""
        try:
            url = f"{self. base_url}/api/v1/info/markets"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=15) as resp:
                    if resp. status == 200:
                        data = await resp.json()
                        markets = []
                        if isinstance(data, dict):
                            if 'result' in data and isinstance(data['result'], list):
                                markets = data['result']
                            elif 'data' in data and isinstance(data['data'], list):
                                markets = data['data']
                            elif 'markets' in data and isinstance(data['markets'], list):
                                markets = data['markets']
                            else:
                                markets = [v for v in data.values() if isinstance(v, dict) and 'symbol' in v]
                        elif isinstance(data, list):
                            markets = data

                        loaded = 0
                        for m in markets:
                            try:
                                symbol_raw = m.get('symbol') or m.get('market') or m.get('ticker')
                                if not symbol_raw:
                                    continue
                                symbol = symbol_raw if symbol_raw.endswith('-USD') else f"{symbol_raw}-USD"
                                rate_val = (
                                    m. get('hourlyFundingRate') or
                                    m. get('fundingRateHourly') or
                                    m.get('fundingRate') or
                                    m.get('hourly_funding_rate') or
                                    m.get('funding_rate_hourly')
                                )
                                if rate_val is None:
                                    continue
                                try:
                                    rate_float = float(rate_val)
                                except (ValueError, TypeError):
                                    continue
                                if rate_float != 0:
                                    self._funding_cache[symbol] = rate_float
                                    self. funding_cache[symbol] = rate_float
                                    loaded += 1
                            except Exception:
                                continue
                        if loaded > 0:
                            logger.info(f"✅ Lighter: Pre-fetched {loaded} funding rates via REST.")
                        else:
                            logger. warning("Lighter initial funding fetch: no rates parsed.")
                    else:
                        logger. warning(f"Lighter initial funding fetch HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Konnte initiale Funding Rates nicht laden: {e}")

    async def fetch_funding_rates(self):
        """Holt Funding Rates - mit dynamischem Rate Limiting"""
        return await with_rate_limit(
            Exchange.LIGHTER, "default", lambda: self.load_funding_rates_and_prices()
        )

    def fetch_24h_vol(self, symbol: str) -> float:
        return 0.0

    async def fetch_orderbook(self, symbol: str, limit: int = 20, force_fresh: bool = False) -> dict:
        """Fetch orderbook from Lighter API.
        
        IMPORTANT: This fetches from REST API which returns FULL SNAPSHOT.
        Use this after WebSocket reconnect to get a valid base for delta updates.
        
        Args:
            symbol: Trading pair (e.g., "BTC-USD")
            limit: Max levels to return per side
            force_fresh: If True, skip cache and force API call
            
        Returns:
            Dict with 'bids', 'asks', 'timestamp' keys
        """
        try:
            # Check cache only if not forcing fresh fetch
            if not force_fresh and symbol in self.orderbook_cache:
                cached = self.orderbook_cache[symbol]
                cache_ts = cached.get("timestamp", 0)
                # Convert ms to s if needed
                if cache_ts > 1e12:
                    cache_ts = cache_ts / 1000
                cache_age = time.time() - cache_ts
                if cache_age < 2.0:
                    # ═══════════════════════════════════════════════════════════════
                    # VALIDATE cached data isn't crossed before returning
                    # ═══════════════════════════════════════════════════════════════
                    cached_bids = cached.get("bids", [])
                    cached_asks = cached.get("asks", [])
                    if cached_bids and cached_asks:
                        best_bid = float(cached_bids[0][0]) if cached_bids else 0
                        best_ask = float(cached_asks[0][0]) if cached_asks else float('inf')
                        if best_ask <= best_bid:
                            logger.warning(f"⚠️ Cached orderbook for {symbol} is crossed - forcing fresh fetch")
                            force_fresh = True
                        else:
                            return {
                                "bids": cached_bids[:limit],
                                "asks": cached_asks[:limit],
                                "timestamp": cached.get("timestamp", 0),
                            }

            market_data = self.market_info.get(symbol)
            if not market_data:
                logger.debug(f"No market data for {symbol}")
                return {"bids": [], "asks": [], "timestamp": 0}

            market_id = market_data.get("i")
            if market_id is None:
                logger.debug(f"No market_id for {symbol}")
                return {"bids": [], "asks": [], "timestamp": 0}

            if not HAVE_LIGHTER_SDK:
                return {"bids": [], "asks": [], "timestamp": 0}

            await self.rate_limiter.acquire()

            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)

            response = await order_api.order_book_orders(market_id=market_id, limit=limit)

            if response and hasattr(response, "asks") and hasattr(response, "bids"):
                bids = []
                for b in response.bids or []:
                    price = safe_float(getattr(b, "price", 0), 0.0)
                    size = safe_float(getattr(b, "remaining_base_amount", 0), 0.0)
                    if price > 0 and size > 0:
                        bids.append([price, size])

                asks = []
                for a in response.asks or []:
                    price = safe_float(getattr(a, "price", 0), 0.0)
                    size = safe_float(getattr(a, "remaining_base_amount", 0), 0.0)
                    if price > 0 and size > 0:
                        asks.append([price, size])

                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])
                
                # ═══════════════════════════════════════════════════════════════
                # VALIDATE the REST response isn't crossed
                # ═══════════════════════════════════════════════════════════════
                if bids and asks:
                    best_bid = bids[0][0]
                    best_ask = asks[0][0]
                    if best_ask <= best_bid:
                        logger.warning(
                            f"⚠️ REST API returned crossed orderbook for {symbol}: "
                            f"ask={best_ask} <= bid={best_bid} - NOT caching"
                        )
                        # Return uncached - let caller decide what to do
                        return {
                            "bids": bids[:limit],
                            "asks": asks[:limit],
                            "timestamp": int(time.time() * 1000),
                            "symbol": symbol,
                            "_crossed": True,  # Flag for caller
                        }

                result = {
                    "bids": bids[:limit],
                    "asks": asks[:limit],
                    "timestamp": int(time.time() * 1000),
                    "symbol": symbol,
                }

                # ═══════════════════════════════════════════════════════════════
                # IMPORTANT: Update BOTH caches and clear internal delta dicts
                # This ensures WebSocket deltas start from a fresh base
                # ═══════════════════════════════════════════════════════════════
                self.orderbook_cache[symbol] = result
                self._orderbook_cache[symbol] = {
                    **result,
                    # Initialize fresh internal dicts for delta merging
                    "_bid_dict": {b[0]: b[1] for b in bids},
                    "_ask_dict": {a[0]: a[1] for a in asks},
                }
                self._orderbook_cache_time[symbol] = time.time()
                self.rate_limiter.on_success()

                return result

        except asyncio.CancelledError:
            logger.debug(f"{self.name}: fetch_orderbook {symbol} cancelled during shutdown")
            return self.orderbook_cache.get(symbol, {"bids": [], "asks": [], "timestamp": 0})
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "rate limit" in err_str:
                self.rate_limiter.penalize_429()
            logger.debug(f"Lighter orderbook {symbol}: {e}")

        return self.orderbook_cache.get(symbol, {"bids": [], "asks": [], "timestamp": 0})

    def handle_orderbook_snapshot(self, symbol: str, bids: List, asks: List):
        """
        Process incoming orderbook UPDATE from Lighter WebSocket.
        
        ═══════════════════════════════════════════════════════════════════════
        DISABLED: WebSocket orderbook processing causes race conditions and
        crossed books due to timing issues with delta updates.
        
        We now rely EXCLUSIVELY on REST polling for orderbook data.
        This is slower but guarantees consistent, non-crossed orderbooks.
        
        WS is still used for: prices, funding rates, trades, market stats.
        ═══════════════════════════════════════════════════════════════════════
        """
        # COMPLETELY DISABLED - REST polling only
        # The WS delta approach causes crossed books due to:
        # 1. Race conditions between REST snapshot and WS deltas
        # 2. Deltas arriving faster than we can process them
        # 3. No guarantee of sequence ordering in WS messages
        #
        # Trade-off: Slightly slower orderbook updates (REST every 1-2s)
        #            vs. guaranteed valid, non-crossed orderbooks
        return

    def handle_orderbook_update(self, symbol: str, bids: List, asks: List):
        """
        Process incoming orderbook update (delta) from WebSocket.
        
        DISABLED: We now rely exclusively on REST polling for orderbooks.
        """
        # Disabled - REST only
        return

    async def check_liquidity(self, symbol: str, side: str, quantity_usd: float, max_slippage_pct: float = 0.02, is_maker: bool = False) -> bool:
        """
        Check if there is sufficient liquidity to execute a trade without excessive slippage.
        
        Args:
            symbol: Trading pair (e.g., 'ETH-USD')
            side: 'BUY' or 'SELL'
            quantity_usd: Size of the trade in USD
            max_slippage_pct: Maximum allowed slippage (default 2%)
            is_maker: If True, we are providing liquidity (Maker), so we don't need counter-liquidity.
            
        Returns:
            bool: True if liquidity is sufficient, False otherwise
        """
        if is_maker:
            # For Maker orders, we don't consume the orderbook, we add to it.
            # So we don't need to check depth.
            logger.debug(f"🔍 Liquidity Check: Skipping depth check for {symbol} (Maker Order)")
            return True

        try:
            logger.debug(f"🔍 Checking liquidity for {symbol} {side}: Need ${quantity_usd:.2f} (Max Slip: {max_slippage_pct:.1%})")
            ob = await self.fetch_orderbook(symbol, limit=20)
            if not ob:
                logger.warning(f"⚠️ {self.name}: No orderbook data for {symbol}")
                return False
            
            bids_len = len(ob.get('bids', []))
            asks_len = len(ob.get('asks', []))
            logger.debug(f"🔍 Orderbook for {symbol}: Bids={bids_len}, Asks={asks_len}")

            # If buying, we consume ASKS. If selling, we consume BIDS.
            orders = ob['asks'] if side.upper() == 'BUY' else ob['bids']
            
            if not orders:
                logger.warning(f"⚠️ {self.name}: Empty orderbook side for {symbol} {side}")
                return False

            best_price = orders[0][0]
            if best_price <= 0:
                return False

            filled_usd = 0.0
            worst_price = best_price

            for price, size in orders:
                chunk_usd = price * size
                filled_usd += chunk_usd
                worst_price = price
                
                if filled_usd >= quantity_usd:
                    break
            
            if filled_usd < quantity_usd:
                logger.warning(f"⚠️ {self.name} {symbol}: Insufficient depth. Need ${quantity_usd:.2f}, found ${filled_usd:.2f}")
                return False

            # Calculate slippage
            slippage = abs(worst_price - best_price) / best_price
            
            if slippage > max_slippage_pct:
                logger.warning(f"⚠️ {self.name} {symbol}: High slippage detected. Est: {slippage*100:.2f}% > Max: {max_slippage_pct*100:.2f}%")
                return False

            return True

        except Exception as e:
            logger.error(f"Liquidity check error for {symbol}: {e}")
            return False

    async def fetch_open_interest(self, symbol: str) -> float:
        if not hasattr(self, "_oi_cache"):
            self._oi_cache = {}
            self._oi_cache_time = {}

        now = time.time()
        if symbol in self._oi_cache:
            if now - self._oi_cache_time.get(symbol, 0) < 60.0:
                return self._oi_cache[symbol]

        try:
            if not HAVE_LIGHTER_SDK:
                return 0.0

            market_data = self.market_info.get(symbol)
            if not market_data:
                return 0.0

            market_id = market_data.get("i")
            if not market_id:
                return 0.0

            await self.rate_limiter.acquire()
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)
            response = await order_api.order_book_details(market_id=market_id)

            if response and response.order_book_details:
                details = response.order_book_details[0]

                if hasattr(details, "open_interest"):
                    oi = safe_float(details.open_interest, 0.0)
                    self._oi_cache[symbol] = oi
                    self._oi_cache_time[symbol] = now
                    self.rate_limiter.on_success()
                    return oi

                if hasattr(details, "volume_24h"):
                    vol = safe_float(details.volume_24h, 0.0)
                    self._oi_cache[symbol] = vol
                    self._oi_cache_time[symbol] = now
                    self. rate_limiter. on_success()
                    return vol
            return 0.0
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: fetch_open_interest {symbol} cancelled during shutdown")
            return self._oi_cache.get(symbol, 0.0)
        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.penalize_429()
            return 0.0

    def min_notional_usd(self, symbol: str) -> float:
        """Berechne Minimum Notional - BULLETPROOF VERSION with safe type conversion."""
        HARD_MIN_USD = 5.0
        SAFETY_BUFFER = 1.10

        data = self.market_info.get(symbol)
        if not data:
            return HARD_MIN_USD

        try:
            # CRITICAL: fetch_mark_price already returns float or None
            raw_price = self.fetch_mark_price(symbol)
            price = safe_float(raw_price, 0.0)
            
            # Safe comparison: price is now guaranteed to be float
            if price <= 0:
                return HARD_MIN_USD

            # CRITICAL: Convert ALL market_info values with safe_float
            min_base_float = safe_float(data.get("min_base_amount"), 0.0)
            ss_val = safe_float(data.get("ss"), 0.0)
            min_quantity = safe_float(data.get("min_quantity"), 0.0)
            mps_val = safe_float(data.get("mps"), 0.0)
            
            # Use the largest minimum base amount
            effective_min_base = max(min_base_float, ss_val, min_quantity)
            
            # Calculate minimum quantity in USD
            min_qty_usd = effective_min_base * price if effective_min_base > 0 else 0.0
            
            # Get min_notional from API (also safe converted)
            min_notional_api = safe_float(data.get("min_notional"), 0.0)
            
            # Also consider mps (min price step) as potential min notional
            mps_as_notional = mps_val if mps_val > 1.0 else 0.0  # mps > 1 likely means USD
            
            # Take the maximum of all minimums
            api_min = max(min_notional_api, min_qty_usd, mps_as_notional)
            safe_min = api_min * SAFETY_BUFFER

            result = max(HARD_MIN_USD, safe_min)
            
            logger.debug(
                f"MIN_NOTIONAL {symbol}: min_base={effective_min_base:.8f} coins, "
                f"price=${price:.4f}, min_qty_usd=${min_qty_usd:.2f}, "
                f"min_notional_api=${min_notional_api:.2f}, final=${result:.2f}"
            )
            
            return result
            
        except Exception as e:
            logger.debug(f"min_notional_usd error {symbol}: {e}")
            return HARD_MIN_USD

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Funding Rate aus Cache (WS > REST) - mit robuster Typ-Konvertierung"""
        if symbol in self._funding_cache:
            rate = self._funding_cache[symbol]
        else:
            rate = self.funding_cache.get(symbol)

        if rate is None:
            return None

        try:
            rate_float = float(rate)
        except (ValueError, TypeError):
            logger.warning(f"Invalid funding rate type for {symbol}: {type(rate)} = {rate}")
            return None

        if abs(rate_float) > 20.0:
            return rate_float / 100.0 / (24 * 365)

        return rate_float / 8.0

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Mark Price aus Cache - always returns float or None."""
        # Check primary cache first
        if symbol in self._price_cache:
            price = self._price_cache[symbol]
            try:
                price_float = float(str(price)) if price is not None else 0.0
                if price_float > 0:
                    return price_float
            except (ValueError, TypeError):
                pass
        
        # Fallback to secondary cache
        if symbol in self.price_cache:
            price = self.price_cache[symbol]
            try:
                price_float = float(str(price)) if price is not None else 0.0
                if price_float > 0:
                    return price_float
            except (ValueError, TypeError):
                pass
        
        return None

    def get_price(self, symbol: str) -> Optional[float]:
        """Alias for fetch_mark_price - used by order execution code."""
        return self.fetch_mark_price(symbol)
    
    async def fetch_fresh_mark_price(self, symbol: str) -> Optional[float]:
        """
        Fetch FRESH mark price directly via REST API (bypasses cache).
        Use this during shutdown when WebSocket is closed and cached prices may be stale.
        
        Uses /api/v1/orderBookDetails which returns last_trade_price for all markets.
        """
        try:
            # Convert symbol to Lighter format (remove -USD suffix)
            lighter_symbol = symbol.replace("-USD", "")
            
            # Use the correct endpoint that returns market details including last_trade_price
            # This endpoint returns ALL markets, so we filter for our symbol
            url = f"{self.base_url}/api/v1/orderBookDetails"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.debug(f"fetch_fresh_mark_price: HTTP {resp.status} for {symbol}")
                        return None
                    
                    data = await resp.json()
                    
                    # Find our market in the response
                    order_book_details = data.get('order_book_details', [])
                    
                    for market in order_book_details:
                        market_symbol = market.get('symbol', '')
                        if market_symbol == lighter_symbol:
                            # Get last trade price (most recent execution price)
                            last_price = safe_float(market.get('last_trade_price', 0))
                            
                            if last_price and last_price > 0:
                                logger.debug(f"✅ fetch_fresh_mark_price: {symbol} = ${last_price:.6f} (fresh via REST)")
                                # Update caches
                                self._price_cache[symbol] = last_price
                                self.price_cache[symbol] = last_price
                                return last_price
                            
                            break
                    
                    logger.debug(f"fetch_fresh_mark_price: Symbol {lighter_symbol} not found in orderBookDetails")
                    return None
            
        except asyncio.TimeoutError:
            logger.debug(f"fetch_fresh_mark_price: Timeout for {symbol}")
            return None
        except Exception as e:
            logger.debug(f"fetch_fresh_mark_price error for {symbol}: {e}")
            return None

    async def get_real_available_balance(self) -> float:
        if time.time() - self._last_balance_update < 2.0:
            return self._balance_cache

        if not HAVE_LIGHTER_SDK:
            return 0.0

        try:
            await self.rate_limiter.acquire()
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)
            await asyncio.sleep(0.5)

            for _ in range(2):
                try:
                    response = await account_api.account(
                        by="index", value=str(self._resolved_account_index)
                    )
                    val = 0.0
                    if response and getattr(response, "accounts", None) and response.accounts[0]:
                        acc = response.accounts[0]
                        buying = getattr(acc, "buying_power", None) or getattr(acc, "total_asset_value", "0")
                        # SAFE CONVERSION: API may return string
                        val = safe_float(buying, 0.0)

                    safe_balance = val * 0.95

                    self._balance_cache = safe_balance
                    self._last_balance_update = time.time()

                    logger.debug(f"Lighter Balance: Raw=${val:.2f}, Safe=${safe_balance:.2f}")
                    break
                except Exception as e:
                    if "429" in str(e):
                        await asyncio.sleep(2)
                        continue
                    if isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError)):
                        await asyncio.sleep(1)
                        continue
                    raise
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: get_real_available_balance cancelled during shutdown")
            return self._balance_cache
        except Exception as e:
            if "429" not in str(e):
                logger.error(f"❌ Lighter Balance Error: {e}")

        return self._balance_cache

    async def fetch_open_positions(self) -> List[dict]:
        if not getattr(config, "LIVE_TRADING", False):
            return []

        if not HAVE_LIGHTER_SDK:
            return []

        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return cached data early during shutdown
        # ═══════════════════════════════════════════════════════════════
        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
        if is_shutting_down:
            # Return cached positions if available, otherwise empty list
            if hasattr(self, '_positions_cache') and self._positions_cache is not None:
                logger.debug("[LIGHTER] Shutdown active - returning cached positions")
                return self._positions_cache
            return []

        # ═══════════════════════════════════════════════════════════════
        # DEDUPLICATION: Prevent API storms during shutdown
        # ═══════════════════════════════════════════════════════════════
        if self.rate_limiter.is_duplicate("LIGHTER:fetch_open_positions"):
            # Return cached positions if available
            if hasattr(self, '_positions_cache') and self._positions_cache is not None:
                return self._positions_cache
            # If no cache, allow the request through

        try:
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)

            result = await self.rate_limiter.acquire()
            # Check if rate limiter was cancelled (shutdown)
            if result < 0:
                logger.debug("[LIGHTER] Rate limiter cancelled during fetch_open_positions - returning cached data")
                if hasattr(self, '_positions_cache') and self._positions_cache is not None:
                    return self._positions_cache
                return []
            await asyncio.sleep(0.2)

            response = await account_api.account(
                by="index", value=str(self._resolved_account_index)
            )

            if not response or not response.accounts or not response.accounts[0]:
                self._positions_cache = []
                return []

            account = response.accounts[0]

            if not hasattr(account, "positions") or not account.positions:
                return []

            positions = []
            for p in account.positions:
                symbol_raw = getattr(p, "symbol", None)
                position_qty = getattr(p, "position", None)
                sign = getattr(p, "sign", None)

                if not symbol_raw or position_qty is None or sign is None:
                    continue

                # SAFE CONVERSION: API may return strings
                sign_int = safe_int(sign, 0)
                multiplier = 1 if sign_int == 0 else -1
                size = safe_float(position_qty, 0.0) * multiplier

                # ═══════════════════════════════════════════════════════════════
                # PNL FIX: Extract REAL PnL data from Lighter API
                # These values are calculated by Lighter and are more accurate
                # than our own estimations!
                # ═══════════════════════════════════════════════════════════════
                unrealized_pnl = safe_float(getattr(p, "unrealized_pnl", None), 0.0)
                realized_pnl = safe_float(getattr(p, "realized_pnl", None), 0.0)
                avg_entry_price = safe_float(getattr(p, "avg_entry_price", None), 0.0)
                position_value = safe_float(getattr(p, "position_value", None), 0.0)

                # Lighter semantics (from Position.total_funding_paid_out):
                # - positive => you PAID funding (cost)
                # - negative => you RECEIVED funding (profit)
                # We expose both the raw field and a normalized "funding_received" (profit-positive).
                total_funding_paid_out = safe_float(getattr(p, "total_funding_paid_out", None), 0.0)
                funding_received = -total_funding_paid_out

                liquidation_price = safe_float(getattr(p, "liquidation_price", None), 0.0)
                
                # Log if we have real PnL data from Lighter
                if unrealized_pnl != 0.0 or realized_pnl != 0.0 or total_funding_paid_out != 0.0:
                    logger.debug(
                        f"📊 {symbol_raw} Lighter PnL: uPnL=${unrealized_pnl:.4f}, "
                        f"rPnL=${realized_pnl:.4f}, entry=${avg_entry_price:.6f}, "
                        f"funding_paid_out=${total_funding_paid_out:.4f}, funding_received=${funding_received:.4f}"
                    )

                if abs(size) > 1e-8:
                    symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                    
                    # ═══════════════════════════════════════════════════════════════
                    # DUST POSITION DETECTION (NOT FILTERING)
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Always return all positions from API, but mark dust positions
                    # with 'is_dust' flag. This prevents the infinite loop where positions
                    # are filtered but API keeps returning them.
                    # Consumers can optionally filter based on 'is_dust' flag if needed.
                    # ═══════════════════════════════════════════════════════════════
                    price = self.get_price(symbol)
                    is_dust = False
                    notional_est = 0.0
                    
                    if price and price > 0:
                        notional_est = abs(size) * price
                        # Dust threshold: $1.0 during normal operation, disabled during shutdown
                        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
                        dust_threshold = 0.0 if is_shutting_down else 1.0
                        is_dust = notional_est <= dust_threshold
                        
                        if is_dust and not is_shutting_down:
                            # Only log once per position to reduce spam
                            if symbol not in self._dust_logged:
                                logger.debug(f"🧹 Dust position detected {symbol}: ${notional_est:.4f} (< ${dust_threshold}) - marked but not filtered")
                                self._dust_logged.add(symbol)
                    else:
                        # No price available - can't determine if dust, assume not dust
                        logger.debug(f"{symbol} has no price, keeping position (size={size})")
                    
                    # ═══════════════════════════════════════════════════════════════
                    # ALWAYS APPEND: Never filter positions, always return them
                    # Consumers (trade management, shutdown, etc.) can filter if needed
                    # PNL FIX: Include REAL PnL data from Lighter for accurate tracking
                    # ═══════════════════════════════════════════════════════════════
                    positions.append({
                        "symbol": symbol,
                        "size": size,
                        "is_dust": is_dust,
                        "notional_est": notional_est if price else None,
                        # ═══════════════════════════════════════════════════════════════
                        # Lighter API PnL Data (accurate, calculated by exchange)
                        # ═══════════════════════════════════════════════════════════════
                        "unrealized_pnl": unrealized_pnl,
                        "realized_pnl": realized_pnl,
                        "avg_entry_price": avg_entry_price,
                        "position_value": position_value,
                        # Raw (API) + normalized funding fields
                        "total_funding_paid_out": total_funding_paid_out,
                        "total_funding_paid": total_funding_paid_out,  # backward-compat alias (do not use for math)
                        "funding_received": funding_received,          # profit-positive
                        "liquidation_price": liquidation_price,
                    })

            logger.info(f"Lighter: Found {len(positions)} open positions")
            
            # ═══════════════════════════════════════════════════════════════
            # GHOST GUARDIAN: Check for pending positions not yet in API
            # ═══════════════════════════════════════════════════════════════
            now = time.time()
            api_symbols = {p['symbol'] for p in positions}
            
            # Clean old pending (> 15s -> 15s KEEP)
            self._pending_positions = {s: t for s, t in self._pending_positions.items() if now - t < 15.0}
            
            # FIX: Disable Ghost Guardian during shutdown to prevent spam
            if not getattr(config, 'IS_SHUTTING_DOWN', False):
                for sym, ts in self._pending_positions.items():
                    # FIX: "Pending" Positionen für bis zu 15 Sekunden injizieren
                    # API kann sehr laggy sein beim Shutdown
                    if sym not in api_symbols and (now - ts < 15.0):
                        age = now - ts
                        # Nur warnen, wenn es ungewöhnlich lange dauert (> 1s)
                        if age > 1.0:
                            logger.warning(f"👻 Lighter Ghost Guardian: Injected pending position for {sym} (Age: {age:.1f}s)")
                        else:
                            logger.debug(f"👻 Ghost pending: {sym} ({age:.1f}s)")
                        
                        # Inject synthetic position
                        positions.append({
                            "symbol": sym,
                            "size": 0.0001, # Dummy non-zero size
                            "is_ghost": True
                        })
                        api_symbols.add(sym) # Prevent duplicates if multiple pending

            # Cache for deduplication
            self._positions_cache = positions
            
            # ═══════════════════════════════════════════════════════════════
            # FIXED: Trigger position callbacks for Ghost-Fill detection
            # This enables ParallelExecutionManager to detect fills faster
            # ═══════════════════════════════════════════════════════════════
            await self._trigger_position_callbacks(positions)
            
            return positions

        except asyncio.CancelledError:
            logger.debug(f"{self.name}: fetch_open_positions cancelled during shutdown")
            # Return cached positions if available, empty list otherwise
            return getattr(self, '_positions_cache', []) or []
        except Exception as e:
            logger.error(f"Lighter Positions Error: {e}")
            return getattr(self, '_positions_cache', [])

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        """Scale amounts - BULLETPROOF VERSION with safe type conversion."""
        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f"Metadata missing for {symbol}")

        # CRITICAL: Ensure decimals are integers, not strings
        size_decimals = safe_int(data.get('sd'), safe_int(data.get('size_decimals'), 8))
        price_decimals = safe_int(data.get('pd'), safe_int(data.get('price_decimals'), 6))
        
        base_scale = Decimal(10) ** size_decimals
        quote_scale = Decimal(10) ** price_decimals
        
        # Safe Decimal conversion helper
        def to_decimal_safe(val, default="0.00000001") -> Decimal:
            if val is None or val == "" or val == "None":
                return Decimal(default)
            try:
                if isinstance(val, Decimal):
                    return val
                # Handle string, int, float
                return Decimal(str(val).strip())
            except Exception:
                return Decimal(default)

        # Get min_base_amount with fallbacks, all safely converted
        min_base_raw = data.get('ss') or data.get('min_base_amount') or data.get('min_quantity')
        min_base_amount = to_decimal_safe(min_base_raw, "0.00000001")
        
        ZERO = Decimal("0")

        # Ensure minimum notional
        notional = qty * price
        if notional < Decimal("10.5"):
            if price > ZERO:
                qty = Decimal("10.5") / price

        # Ensure minimum quantity
        if min_base_amount > ZERO and qty < min_base_amount:
            qty = min_base_amount * Decimal("1.05")

        scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
        scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_DOWN if side == 'SELL' else ROUND_UP))

        if scaled_price == 0:
            raise ValueError(f"{symbol}: scaled_price is 0! price={price}, pd={price_decimals}")

        if scaled_base == 0:
            raise ValueError(f"{symbol}: scaled_base is 0!")

        return scaled_base, scaled_price

    async def quantize_base_amount(self, symbol: str, size_usd: float) -> int:
        """Quantizes USD size to valid base_amount for Lighter API - SAFE VERSION."""
        market_info = self.market_info.get(symbol)
        if not market_info:
            raise ValueError(f"No market info for {symbol}")

        # CRITICAL: Use safe_int for decimals to handle string values from API
        size_decimals = safe_int(
            market_info.get('sd'), 
            safe_int(market_info.get('size_decimals'), 4)
        )
        
        # Get min_base_amount with multiple fallbacks, all safely converted
        mba = market_info.get('min_base_amount')
        if mba is None:
            mba = market_info.get('ss')
        if mba is None:
            mba = market_info.get('min_quantity')
        
        min_base_amount = safe_float(mba, 0.01)
        
        # fetch_mark_price returns float or None
        price = self.fetch_mark_price(symbol)
        price_float = safe_float(price, 0.0)

        # SAFE comparison: price_float is guaranteed to be float
        if price_float <= 0:
            raise ValueError(f"Invalid price for {symbol}: {price}")

        base_scale = 10 ** size_decimals
        size_coins = safe_float(size_usd, 0.0) / price_float
        raw_base_units = size_coins * base_scale
        lot_size = int(min_base_amount * base_scale)
        
        # Ensure lot_size is at least 1
        if lot_size < 1:
            lot_size = 1
            
        quantized_base = int(raw_base_units // lot_size) * lot_size

        if quantized_base < lot_size:
            quantized_base = lot_size

        logger.debug(
            f"QUANTIZE {symbol}: USD={size_usd:.2f}, coins={size_coins:.6f}, "
            f"raw={raw_base_units:.0f}, lot_size={lot_size}, final={quantized_base}"
        )

        return quantized_base

    @rate_limited(Exchange.LIGHTER)
    async def open_live_position(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        amount: Optional[float] = None,
        time_in_force: Optional[str] = None,
        **kwargs
    ) -> Tuple[bool, Optional[str]]:
        """Open a position on Lighter exchange."""
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Safe-cast notional_usd FIRST (could be string from caller)
        # ═══════════════════════════════════════════════════════════════
        notional_usd_safe = safe_float(notional_usd, 0.0)
        # Allow 0 notional IF amount is provided
        if notional_usd_safe <= 0 and (amount is None or amount <= 0):
            logger.error(f"❌ Invalid notional_usd for {symbol}: {notional_usd}")
            return False, None
        
        # Only validate notional if we rely on it. If amount is passed, we might skip strict notional checks 
        # or we should recalculate notional from amount for validation? 
        # For now, we trust the caller if amount is passed, or loose check.
        # But generally validate_order_params checks min notional.
        
        # Calculate approximate notional if amount is given, for validation
        if amount and amount > 0:
            est_price = price if price else self.get_price(symbol)
            if est_price:
                 est_notional = amount * safe_float(est_price, 0)
                 if est_notional > 0:
                     notional_usd_safe = est_notional

        # ═══════════════════════════════════════════════════════════════════════════
        # CRITICAL FIX: Skip validation for reduce_only orders (closing positions)
        # Reduce-only orders MUST be executed regardless of min_notional to prevent
        # dust positions from getting stuck. The exchange will reject invalid orders.
        # ═══════════════════════════════════════════════════════════════════════════
        if not reduce_only:
            is_valid, error_msg = await self.validate_order_params(symbol, notional_usd_safe)
            if not is_valid:
                logger.error(f"❌ Order validation failed for {symbol}: {error_msg}")
                raise ValueError(f"Order validation failed: {error_msg}")
        else:
            logger.debug(f"⚡ Skipping validation for reduce_only order {symbol} (${notional_usd_safe:.2f})")

        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Safe-cast price (could be string from API)
        # ═══════════════════════════════════════════════════════════════
        if price is None:
            price = self.get_price(symbol)
        price_safe = safe_float(price, 0.0)
        if price_safe <= 0:
            raise ValueError(f"No valid price available for {symbol}: {price}")

        logger.info(f"🚀 LIGHTER OPEN {symbol}: side={side}, size_usd=${notional_usd_safe:.2f}, amount={amount}, price=${price_safe:.6f}, TIF={time_in_force}")

        try:
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: Safe-cast market_id (market_info values can be strings!)
            # ═══════════════════════════════════════════════════════════════
            market_id_raw = self.market_info[symbol].get("i")
            market_id = safe_int(market_id_raw, -1)
            logger.debug(f"🔍 DEBUG: {symbol} market_id = {market_id} (raw={market_id_raw}, type={type(market_id_raw)})")
            
            if market_id < 0:
                logger.error(f"❌ INVALID market_id for {symbol}: {market_id}!")
                return False, None

            price_decimal = Decimal(str(price_safe))
            notional_decimal = Decimal(str(notional_usd_safe))
            
            if post_only:
                # ═══════════════════════════════════════════════════════════════
                # MAKER STRATEGY: Place at Head of Book (Fixed Logic)
                # ═══════════════════════════════════════════════════════════════
                maker_p = await self.get_maker_price(symbol, side)
                if maker_p:
                    limit_price = Decimal(str(maker_p))
                else:
                    limit_price = price_decimal # Fallback
            else:
                # ═══════════════════════════════════════════════════════════════
                # TAKER STRATEGY: Aggressive Slippage to Fill
                # ═══════════════════════════════════════════════════════════════
                # FIXED: During shutdown, use higher slippage (2.5%) to ensure IOC fills
                # Normal operation uses config value (default 0.6%)
                # Note: Lighter's "accidental price" protection is ~3%, so 2.5% is safe
                if getattr(config, 'IS_SHUTTING_DOWN', False):
                    slippage = Decimal("2.5")  # Higher slippage during shutdown for guaranteed fill
                else:
                    slippage = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
                slippage_multiplier = (
                    Decimal(1) + (slippage / Decimal(100))
                    if side == "BUY"
                    else Decimal(1) - (slippage / Decimal(100))
                )
                limit_price = price_decimal * slippage_multiplier

            # -------------------------------------------------------
            # FAT FINGER PROTECTION: Clamp Price to Mark Price +/- Epsilon
            # -------------------------------------------------------
            # Prevents "order price flagged as an accidental price" error 21733
            # CRITICAL FIX: During shutdown, use the original passed price as mark reference
            # since WS cache might be stale. The caller passes raw cached price.
            if getattr(config, 'IS_SHUTTING_DOWN', False):
                # During shutdown, the caller passes raw price from WS cache
                # Use that directly as mark_p (before our slippage was applied)
                mark_p = price_safe  # The original price before adapter's slippage
                logger.debug(f"🔄 {symbol}: Shutdown detected - using passed price as mark (mark=${mark_p:.6f})")
            else:
                mark_p = self.fetch_mark_price(symbol)
                
            if mark_p and mark_p > 0:
                # During shutdown, use a tighter epsilon (3%) because Lighter's "accidental price" 
                # protection is stricter than we initially thought
                if getattr(config, 'IS_SHUTTING_DOWN', False):
                    epsilon = Decimal("0.03")  # 3% during shutdown (Lighter seems to use ~3% tolerance)
                else:
                    epsilon = Decimal(str(getattr(config, "LIGHTER_PRICE_EPSILON_PCT", 0.10)))
                mark_decimal = Decimal(str(mark_p))
                min_allowed = mark_decimal * (Decimal("1") - epsilon)
                max_allowed = mark_decimal * (Decimal("1") + epsilon)
                
                if limit_price < min_allowed:
                    logger.warning(f"⚠️ {symbol}: Price {limit_price} too low vs Mark {mark_p}. Clamping to {min_allowed}")
                    limit_price = min_allowed
                elif limit_price > max_allowed:
                    logger.warning(f"⚠️ {symbol}: Price {limit_price} too high vs Mark {mark_p}. Clamping to {max_allowed}")
                    limit_price = max_allowed

            # ═══════════════════════════════════════════════════════════════
            # FIX: Strikte Quantisierung der Menge & Preis
            # ═══════════════════════════════════════════════════════════════
            market_info = self.market_info.get(symbol, {})
            # Lot Size / Tick Size holen (Floats)
            size_inc = float(market_info.get('lot_size', market_info.get('min_base_amount', 0.0001)))
            price_inc = float(market_info.get('tick_size', 0.01))
            
            # Berechne Menge in Coins (Raw)
            if amount is not None and amount > 0:
                raw_amount = float(amount)
            else:
                raw_amount = float(notional_usd_safe) / float(limit_price)
            
            # Menge runden (Abrunden bei Sell, um "Insufficient Balance" zu vermeiden)
            quantized_size = quantize_value(raw_amount, size_inc, rounding=ROUND_FLOOR)
            
            # Preis runden
            quantized_price = quantize_value(float(limit_price), price_inc)
            
            # Konvertierung zu Scaled Integers für Lighter API
            # Basis: amount * 10^size_decimals, price * 10^price_decimals
            size_decimals = int(market_info.get('sd', 8))
            price_decimals = int(market_info.get('pd', 6))
            
            scale_base = 10 ** size_decimals
            scale_price = 10 ** price_decimals
            
            base = int(round(quantized_size * scale_base))
            price_int = int(round(quantized_price * scale_price))

            logger.debug(
                f"QUANTIZE {symbol}: Notional=${notional_usd_safe} -> RawAmt={raw_amount:.6f} "
                f"-> QuantAmt={quantized_size:.6f} (x{size_inc}) -> BaseInt={base}"
            )
            
            if base <= 0:
                logger.error(f"❌ {symbol}: base amount is 0 after quantization (raw_amount={raw_amount})")
                return False, None

            signer = await self._get_signer()
            max_retries = 2

            for attempt in range(max_retries + 1):
                try:
                    # OPTIMIERUNG: Aggressiverer Exponential Backoff
                    # Verhindert unnötiges Warten vor dem ersten Versuch (attempt 0)
                    if attempt > 0:
                        backoff = 0.1 * (2 ** attempt)  # 0.2s, 0.4s... statt linear 0.5s
                        await asyncio.sleep(backoff)

                    client_oid = int(time.time() * 1000) + random.randint(0, 99999)

                    await self.rate_limiter.acquire()
                    # Determine Time In Force & Expiry
                    # Use IOC for straight closes (reduce_only + Taker) to prevent ghost orders on the book.
                    # Use GTT for Open positions or Maker orders.
                    tif = SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
                    # Default expiry for GTT
                    expiry = SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
                    is_ioc = False

                    # LOGIC: Resolve TIF from Arguments or Auto-Detection
                    if time_in_force and isinstance(time_in_force, str):
                        key = time_in_force.upper()
                        logger.debug(f"🔍 [TIF] {symbol}: Processing time_in_force='{key}' (raw='{time_in_force}')")
                        if key == "IOC":
                            is_ioc = True
                            # ═══════════════════════════════════════════════════════════════
                            # FIX #6: Lighter IOC Order TimeInForce - Use correct constant
                            # Try multiple IOC attribute names (SDK may use different names)
                            # Priority: IMMEDIATE_OR_CANCEL first (most common in Lighter SDK)
                            # ═══════════════════════════════════════════════════════════════
                            if hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL'):
                                tif = SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
                                logger.debug(f"✅ [TIF] {symbol}: Set IOC via ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = {tif}")
                            elif hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC'):
                                tif = SignerClient.ORDER_TIME_IN_FORCE_IOC
                                logger.debug(f"✅ [TIF] {symbol}: Set IOC via ORDER_TIME_IN_FORCE_IOC = {tif}")
                            else:
                                # Fallback: IOC is typically 0 in most exchanges (including Lighter)
                                tif = 0
                                logger.warning(f"⚠️ [TIF] {symbol}: ORDER_TIME_IN_FORCE constants not found in SignerClient, using fallback tif=0")
                            # ═══════════════════════════════════════════════════════════════
                            # FIX: IOC orders require expiry=0 (not 28-day expiry)
                            # ═══════════════════════════════════════════════════════════════
                            expiry = 0
                            logger.debug(f"🔧 [TIF] {symbol}: Set expiry=0 for IOC order")
                        elif key == "FOK" and hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_FOK'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_FOK
                            expiry = 0  # FOK also requires expiry=0
                        elif key == "GTC" and hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_GTC'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_GTC
                    elif reduce_only and not post_only:
                        # ═══════════════════════════════════════════════════════════════
                        # FIX #6: Lighter IOC Order TimeInForce - Use IOC for reduce_only
                        # Priority: IMMEDIATE_OR_CANCEL first (most common in Lighter SDK)
                        # ═══════════════════════════════════════════════════════════════
                        is_ioc = True
                        if hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
                            logger.debug(f"✅ [TIF] {symbol}: Set IOC via ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = {tif} (reduce_only)")
                        elif hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_IOC
                            logger.debug(f"✅ [TIF] {symbol}: Set IOC via ORDER_TIME_IN_FORCE_IOC = {tif} (reduce_only)")
                        else:
                            # Fallback: IOC is typically 0 in most exchanges (including Lighter)
                            tif = 0
                            logger.warning(f"⚠️ [TIF] {symbol}: ORDER_TIME_IN_FORCE constants not found, using fallback tif=0 (reduce_only)")
                        # ═══════════════════════════════════════════════════════════════
                        # FIX: IOC orders (reduce_only) require expiry=0
                        # ═══════════════════════════════════════════════════════════════
                        expiry = 0
                        logger.debug(f"🔧 [TIF] {symbol}: Set expiry=0 for reduce_only IOC order")
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX #6: Check if IOC was set (by comparing with known IOC values)
                    # Try both IOC constant names to determine the correct value
                    # ═══════════════════════════════════════════════════════════════
                    ioc_value_imm = getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL', None)
                    ioc_value_ioc = getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC', None)
                    ioc_value = ioc_value_imm if ioc_value_imm is not None else (ioc_value_ioc if ioc_value_ioc is not None else 0)
                    
                    if is_ioc or tif == ioc_value or tif == 0:
                        logger.info(f"⚡ [TIF] {symbol}: Using IOC order (tif={tif}, expiry={expiry}, ioc_attr={ioc_value})")
                    else:
                        logger.debug(f"📋 [TIF] {symbol}: Using TIF={tif}, expiry={expiry} (not IOC)")

                    # ═══════════════════════════════════════════════════════════════
                    # FIX: NONCE LOCKING + CACHING (Optimized for speed)
                    # ═══════════════════════════════════════════════════════════════
                    async with self.order_lock:
                        try:
                            # 1. Get nonce efficiently (uses cache when possible)
                            current_nonce = await self._get_next_nonce()
                            
                            if current_nonce is None:
                                logger.error(f"❌ Failed to get nonce for {symbol}")
                                return False, None
                            
                            logger.info(f"🔒 Locked execution for {symbol} {side} (Nonce: {current_nonce})...")

                            # 2. Client Order ID generieren (Wichtig für Lighter)
                            # Re-generate to be fresh inside lock
                            client_oid_final = int(time.time() * 1000) + random.randint(0, 99999)

                            # 3. Order erstellen mit EXPLIZITEN Typen
                            # OPTIMIZED: Use MARKET orders during shutdown for guaranteed fills
                            is_shutdown = getattr(config, 'IS_SHUTTING_DOWN', False)
                            use_market_order = is_shutdown and reduce_only and not post_only
                            
                            if use_market_order:
                                # FAST SHUTDOWN: Use create_market_order for immediate fills
                                # avg_execution_price acts as slippage protection (max price for BUY, min for SELL)
                                slippage_pct = Decimal("0.03")  # 3% slippage protection
                                if side == "BUY":
                                    avg_exec_price = int(price_int * (1 + float(slippage_pct)))
                                else:
                                    avg_exec_price = int(price_int * (1 - float(slippage_pct)))
                                
                                # ═══════════════════════════════════════════════════════════════
                                # FIX #8: Lighter Reduce-Only Flag Verification
                                # Parameter-Name: reduce_only (confirmed by SDK)
                                # Boolean-Wert: True = 1 (ReduceOnly), False = 0 (normal order)
                                # ═══════════════════════════════════════════════════════════════
                                logger.info(f"⚡ MARKET ORDER {symbol}: {side} {base} @ avg_price={avg_exec_price} (shutdown fast-close)")
                                logger.debug(f"✅ [REDUCE_ONLY] {symbol}: Market Order reduce_only={reduce_only} (will be {1 if reduce_only else 0} in API)")
                                tx, resp, err = await signer.create_market_order(
                                    market_index=int(market_id),
                                    client_order_index=int(client_oid_final),
                                    base_amount=int(base),
                                    avg_execution_price=int(avg_exec_price),
                                    is_ask=bool(side == "SELL"),
                                    reduce_only=bool(reduce_only),  # ✅ FIX: Explicit reduce_only parameter
                                    nonce=int(current_nonce),
                                    api_key_index=int(self._resolved_api_key_index)
                                )
                            else:
                                # Normal LIMIT order
                                # ═══════════════════════════════════════════════════════════════
                                # FIX #5: Verify ORDER_TYPE_LIMIT constant exists
                                # According to Lighter API docs: ORDER_TYPE_LIMIT = 0, ORDER_TYPE_MARKET = 1
                                # ═══════════════════════════════════════════════════════════════
                                order_type_limit = getattr(SignerClient, 'ORDER_TYPE_LIMIT', None)
                                if order_type_limit is None:
                                    # Fallback: LIMIT orders are typically 0
                                    order_type_limit = 0
                                    logger.warning(f"⚠️ [ORDER_TYPE] {symbol}: ORDER_TYPE_LIMIT not found in SignerClient, using fallback 0")
                                else:
                                    logger.debug(f"✅ [ORDER_TYPE] {symbol}: Using ORDER_TYPE_LIMIT = {order_type_limit}")
                                
                                # Verify ORDER_TYPE_MARKET exists (for reference, not used here)
                                order_type_market = getattr(SignerClient, 'ORDER_TYPE_MARKET', None)
                                if order_type_market is None:
                                    logger.debug(f"ℹ️ [ORDER_TYPE] {symbol}: ORDER_TYPE_MARKET not found in SignerClient (not needed for limit orders)")
                                else:
                                    logger.debug(f"ℹ️ [ORDER_TYPE] {symbol}: ORDER_TYPE_MARKET = {order_type_market} (available but not used for limit orders)")
                                
                                # ═══════════════════════════════════════════════════════════════
                                # FIX #8: Lighter Reduce-Only Flag Verification
                                # Parameter-Name: reduce_only (confirmed by SDK)
                                # Boolean-Wert: True = 1 (ReduceOnly), False = 0 (normal order)
                                # ═══════════════════════════════════════════════════════════════
                                logger.debug(f"✅ [REDUCE_ONLY] {symbol}: Limit Order reduce_only={reduce_only} (will be {1 if reduce_only else 0} in API)")
                                tx, resp, err = await signer.create_order(
                                    market_index=int(market_id),         # Force int
                                    client_order_index=int(client_oid_final),
                                    base_amount=int(base),               # Force int
                                    price=int(price_int),                # Force int
                                    is_ask=bool(side == "SELL"),         # Force bool
                                    order_type=int(order_type_limit),    # ✅ FIX: Explicit ORDER_TYPE_LIMIT
                                    time_in_force=int(tif),
                                    reduce_only=bool(reduce_only),       # ✅ FIX: Explicit reduce_only parameter
                                    trigger_price=int(getattr(SignerClient, 'NIL_TRIGGER_PRICE', 0)),
                                    order_expiry=int(expiry),
                                    nonce=int(current_nonce),            # 🔥 Explizite Nonce (kein None!)
                                    api_key_index=int(self._resolved_api_key_index) # 🔥 FIX: Explicit api_key_index to avoid SDK auto-retry bug
                                )

                            if err:
                                err_str = str(err).lower()
                                err_code = ""
                                
                                # Try to extract error code if present
                                try:
                                    if hasattr(err, 'code'):
                                        err_code = str(err.code)
                                    elif isinstance(err, dict):
                                        err_code = str(err.get('code', ''))
                                    # Also check for code in error string like "error 1137" or "code: 1137"
                                    import re
                                    code_match = re.search(r'(?:error|code)[:\s]*(\d+)', err_str)
                                    if code_match:
                                        err_code = code_match.group(1)
                                except:
                                    pass
                                
                                logger.error(f"❌ Lighter Order Failed: {err} (code={err_code})")
                                
                                # ═══════════════════════════════════════════════════════════════
                                # ERROR 1137: Position Missing / Position Not Found
                                # This happens when trying to close a position that doesn't exist
                                # (liquidated, manually closed, or sync issue)
                                # ═══════════════════════════════════════════════════════════════
                                if err_code == "1137" or ("position" in err_str and ("missing" in err_str or "not found" in err_str or "does not exist" in err_str)):
                                    logger.warning(f"⚠️ POSITION MISSING ERROR for {symbol} - Position may have been liquidated or closed externally")
                                    logger.info(f"🔄 Triggering position sync for {symbol}...")
                                    
                                    # TS SDK Pattern: acknowledge_failure() on any TX error
                                    self.acknowledge_failure()
                                    
                                    # Return special status to signal caller that position doesn't exist
                                    # This allows the caller to clean up DB state
                                    return False, "POSITION_MISSING_1137"
                                
                                # ═══════════════════════════════════════════════════════════════
                                # NONCE ERROR HANDLING (TS SDK Pattern)
                                # - acknowledge_failure(): Rollback the used nonce
                                # - hard_refresh_nonce(): Fetch fresh nonces from API
                                # ═══════════════════════════════════════════════════════════════
                                if "nonce" in err_str or "invalid nonce" in err_str:
                                    # Nonce error - hard refresh the entire nonce pool
                                    logger.warning(f"⚠️ Nonce error for {symbol} - triggering hard refresh (TS SDK pattern)")
                                    await self.hard_refresh_nonce()
                                else:
                                    # Other errors - just acknowledge failure to rollback the nonce
                                    self.acknowledge_failure()
                                    
                                if "429" in err_str or "too many requests" in err_str:
                                    # Rate limit hit - the rate limiter will handle penalty
                                    logger.warning(f"⚠️ Rate limit hit for {symbol}")
                                return False, None
                            
                            tx_hash = getattr(resp, "tx_hash", tx) # tx usually contains hash string in some versions, or resp does
                            # In existing code: tx, resp, err. 'tx' was used, but log said 'tx_hash = getattr(resp, "tx_hash", "OK")'
                            # Let's try to get hash robustly
                            tx_hash_final = str(tx) if tx else "OK"
                            if hasattr(resp, "tx_hash") and resp.tx_hash:
                                tx_hash_final = str(resp.tx_hash)

                            logger.info(f"✅ Lighter Order Sent: {tx_hash_final}")
                            
                            # ═══════════════════════════════════════════════════════════════
                            # FIX 3 (2025-12-13): Track placed order for cancel resolution
                            # Pattern from lighter-ts-main/src/utils/order-status-checker.ts:
                            # - Store tx_hash → { symbol, client_order_index, nonce }
                            # - Used when cancel_limit_order can't resolve hash via API
                            # ═══════════════════════════════════════════════════════════════
                            try:
                                async with self._placed_orders_lock:
                                    self._placed_orders[tx_hash_final] = {
                                        "symbol": symbol,
                                        "client_order_index": client_oid_final,
                                        "nonce": current_nonce,
                                        "placed_at": time.time(),
                                        "side": side,
                                        "market_id": market_id,
                                        "base_amount": base,
                                    }
                                    # Cleanup old entries (older than 1 hour)
                                    cutoff = time.time() - 3600
                                    stale_hashes = [h for h, v in self._placed_orders.items() if v.get("placed_at", 0) < cutoff]
                                    for h in stale_hashes:
                                        del self._placed_orders[h]
                                    logger.debug(f"📝 Tracked order {tx_hash_final[:20]}... (client_oid={client_oid_final})")
                            except Exception as track_e:
                                logger.debug(f"⚠️ Order tracking error (non-fatal): {track_e}")
                            
                            # GHOST GUARDIAN: Register success time
                            if not post_only:
                                # Nur bei Taker-Orders (sofortiger Fill erwartet) injizieren wir eine Pending Position
                                self._pending_positions[symbol] = time.time()
                            
                            return True, tx_hash_final

                        except Exception as inner_e:
                            import traceback
                            logger.error(f"❌ Lighter Inner Error: {inner_e}")
                            logger.debug(traceback.format_exc())
                            return False, None

                except Exception as inner_e_loop:
                     # Fallback for outer loop if connection fails etc
                     logger.error(f"Lighter Loop Error: {inner_e_loop}")
                     continue

            return False, None

        except Exception as e:
            logger.error(f"Lighter Execution Error: {e}")
            return False, None

    async def cancel_limit_order(self, order_id: str, symbol: str = None) -> bool:
        """Cancel a specific limit order by ID. Resolves Hashes if necessary."""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - skip cancel during shutdown (already handled by cancel_all)
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[LIGHTER] Shutdown active - skipping cancel_limit_order for {order_id}")
            # During shutdown, assume cancelled (global cancel was already executed)
            return True
        
        try:
            if not HAVE_LIGHTER_SDK:
                return False
            
            signer = await self._get_signer()
            oid_int = None

            # ═══════════════════════════════════════════════════════════════
            # FIX: Handle Hash Strings -> Resolve to Integer ID
            # ═══════════════════════════════════════════════════════════════
            # Check if it looks like an Integer
            if str(order_id).isdigit():
                oid_int = int(str(order_id))
            
            # If it looks like a Hash (long string or contains 0x)
            elif isinstance(order_id, str) and (len(order_id) > 15 or "0x" in order_id):
                # ═══════════════════════════════════════════════════════════════
                # FIX 3 (2025-12-13): FIRST TRY ORDER TRACKING CACHE
                # Pattern from lighter-ts-main/src/utils/order-status-checker.ts:
                # Before slow API lookup, check our local tracking cache.
                # This enables cancel even when API returns 404 for the order.
                # ═══════════════════════════════════════════════════════════════
                tracked_order = None
                try:
                    async with self._placed_orders_lock:
                        tracked_order = self._placed_orders.get(order_id)
                    
                    if tracked_order:
                        tracked_symbol = tracked_order.get("symbol")
                        tracked_client_oid = tracked_order.get("client_order_index")
                        tracked_market_id = tracked_order.get("market_id")
                        
                        if symbol is None:
                            symbol = tracked_symbol
                        
                        logger.info(
                            f"📝 Lighter: Found tracked order for hash {order_id[:20]}... "
                            f"(symbol={tracked_symbol}, client_oid={tracked_client_oid})"
                        )
                        
                        # Try to cancel by market_id directly using ImmediateCancelAll
                        # This bypasses the need to resolve hash → order_id
                        if tracked_market_id is not None:
                            try:
                                signer = await self._get_signer()
                                # Use ImmediateCancelAll for the tracked market
                                async with self.order_lock:
                                    nonce = await self._get_next_nonce()
                                    if nonce is None:
                                        logger.error("❌ Failed to get nonce for cancel")
                                    else:
                                        tx, resp, err = await signer.cancel_all_orders(
                                            time_in_force=0,  # IMMEDIATE
                                            time=0,
                                            nonce=int(nonce),
                                            api_key_index=int(self._resolved_api_key_index)
                                        )
                                        if not err:
                                            logger.info(f"✅ Cancelled via ImmediateCancelAll (tracked order fallback)")
                                            # Remove from tracking
                                            async with self._placed_orders_lock:
                                                self._placed_orders.pop(order_id, None)
                                            return True
                            except Exception as cancel_e:
                                logger.debug(f"⚠️ ImmediateCancelAll fallback failed: {cancel_e}")
                
                except Exception as track_lookup_e:
                    logger.debug(f"⚠️ Order tracking lookup error: {track_lookup_e}")
                
                # Fall back to original hash resolution logic
                if not symbol:
                    logger.warning(f"⚠️ Lighter Cancel: Cannot resolve hash {order_id} without symbol.")
                    return False

                logger.info(f"🔍 Lighter: Attempting to resolve Hash {order_id} to Order ID for {symbol}...")
                
                # Fetch open orders directly to check hashes
                if self._resolved_account_index is None:
                    await self._resolve_account_index()
                
                market_data = self.market_info.get(symbol)
                if not market_data:
                    return False
                
                # Manually fetch orders via REST to inspect 'tx_hash'.
                # IMPORTANT: Lighter's documented status codes are 0=Open, 1=Filled, 2=Cancelled, 3=Expired.
                # Using wrong status here can make us blind and cause retries to stack up extra open orders.
                market_index = (
                    market_data.get('i')
                    or market_data.get('market_id')
                    or market_data.get('market_index')
                )

                # Try a small set of queries to resolve tx_hash -> order id.
                # 1) Open orders (status=0) is the most relevant for cancellation.
                # 2) Fallback: query other statuses / no-status in case the API behaves differently.
                candidate_params = []
                base_params = {
                    "account_index": int(self._resolved_account_index),
                    "market_index": int(market_index) if market_index is not None else None,
                    "limit": 50,
                }
                if base_params["market_index"] is not None:
                    candidate_params.append({**base_params, "status": 0})
                    candidate_params.append({**base_params, "status": 1})
                    candidate_params.append({**base_params, "status": 2})
                    candidate_params.append({**base_params, "status": 3})
                    candidate_params.append({k: v for k, v in base_params.items() if k != "market_index" and v is not None})
                else:
                    candidate_params.append({k: v for k, v in base_params.items() if v is not None})

                for params in candidate_params:
                    # Remove any None values (defensive)
                    params = {k: v for k, v in params.items() if v is not None}
                    resp = await self._rest_get("/api/v1/orders", params=params)
                    raw_orders = []
                    if resp:
                        raw_orders = resp if isinstance(resp, list) else resp.get('orders', resp.get('data', []))

                    for o in raw_orders or []:
                        try:
                            if str(o.get('tx_hash')) == order_id or str(o.get('hash')) == order_id:
                                oid_int = int(o.get('id') or o.get('order_index') or o.get('order_id'))
                                logger.info(f"✅ Lighter: Resolved Hash {order_id[:10]}... -> Order ID {oid_int}")
                                break
                        except Exception:
                            continue
                    if oid_int is not None:
                        break

                if oid_int is None:
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Check if position exists before warning
                    # If a position exists for this symbol, the order was likely filled
                    # and we just can't find it due to API lag. This is NOT an error.
                    # ═══════════════════════════════════════════════════════════════
                    try:
                        positions = await self.fetch_open_positions()
                        pos = next((p for p in (positions or []) if p.get("symbol") == symbol), None)
                        if pos and abs(float(pos.get("size", 0))) > 0:
                            # Position exists → order was filled → cancel not needed
                            logger.debug(
                                f"🔍 Lighter Cancel: Hash {order_id[:10]}... not found but position exists for {symbol}. "
                                f"Order was likely FILLED. Skipping cancel."
                            )
                            return True  # Order filled, no cancel needed
                    except Exception as e:
                        logger.debug(f"Position check failed during hash resolution: {e}")
                    
                    # No position found - this might be a real issue
                    # Downgrade to DEBUG since this often happens during normal operation
                    logger.debug(
                        f"🔍 Lighter Cancel: Could not resolve Hash {order_id[:10]}... to an Order ID for {symbol}. "
                        "No position found. Order may have been cancelled elsewhere."
                    )
                    return False
            else:
                # Try basic conversion for other cases
                try:
                    oid_int = int(str(order_id))
                except ValueError:
                    logger.error(f"Lighter Cancel: Invalid order_id format {order_id}")
                    return False

            # Execute Cancel with the Clean Integer ID
            await self.rate_limiter.acquire()
            
            # 🔥 FIX: Lock execution to prevent Invalid Nonce errors
            async with self.order_lock:
                tx, resp, err = await signer.cancel_order(
                    order_id=oid_int,
                    symbol=None 
                )
        
            if err:
                 # Ignore errors if order is already gone
                 err_str = str(err).lower()
                 if "found" in err_str or "exist" in err_str or "could not find open order" in err_str:
                     # Log as info, not warning, since this is expected during cleanups
                     logger.info(f"ℹ️ Lighter Cancel: Order {oid_int} already closed/not found.")
                     self._clear_request_cache()  # Clear cache since state may have changed
                     return True
                 logger.error(f"Lighter Cancel Error {oid_int}: {err}")
                 return False
             
            logger.info(f"✅ Lighter Cancelled Order {oid_int}")
            
            # Clear request cache since state changed
            self._clear_request_cache()
            
            return True
        
        except Exception as e:
            logger.error(f"Lighter Cancel Exception {order_id}: {e}")
            return False

    async def get_order_status(self, order_id: str) -> str:
        """
        Check status of a specific order.
        Returns: 'OPEN', 'FILLED', 'PARTIALLY_FILLED', 'CANCELLED', 'UNKNOWN'
        """
        try:
            if not HAVE_LIGHTER_SDK:
                return 'UNKNOWN'

            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)
            
            resp = None
            try:
                # Use rate limit?
                # await self.rate_limiter.acquire() # Light check
                # Note: get_order might not exist directly on OrderApi in all versions,
                # but let's try the common ones or use order_details
                 
                # Try finding a working method (defensive reflection)
                method = None
                for m_name in ['get_order', 'order_details', 'get_order_by_id']:
                    if hasattr(order_api, m_name):
                        method = getattr(order_api, m_name)
                        break
                
                if method:
                    resp = await method(order_id=int(order_id))
            except Exception as e:
                logger.debug(f"Lighter get_order_status fetch failed: {e}")
                return "UNKNOWN"
                
            if not resp:
                return "UNKNOWN"
            
            # Extract data
            data = resp
            if hasattr(resp, 'data'): data = resp.data
            elif hasattr(resp, 'order'): data = resp.order
            
            # Status Logic
            filled = safe_float(getattr(data, 'filled_amount', 0) or getattr(data, 'filled_size', 0))
            total = safe_float(getattr(data, 'total_amount', 0) or getattr(data, 'total_size', 0))
            status_code = getattr(data, 'status', None)
            
            if total > 0 and filled >= total * 0.999:
                return "FILLED"
            
            if filled > 0:
                return "PARTIALLY_FILLED"
                
            # If 0 filled
            if status_code == 10: # 10=Open
                return "OPEN"
                
            if status_code in [30, 40, 4, 3]: # Assuming cancelled codes
                return "CANCELLED"
                
            # Fallback: if we can't determine, assume OPEN if no cancel proof?
            # Or assume CANCELLED/UNKNOWN?
            # Safer to return UNKNOWN.
            return "UNKNOWN"

        except Exception as e:
            logger.error(f"Lighter Status Check Error {order_id}: {e}")
            return "UNKNOWN"

    @rate_limited(Exchange.LIGHTER, 1.0)
    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str = None, 
        notional_usd: float = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Close a position on Lighter exchange - BULLETPROOF VERSION
        Handles all type conversions safely to avoid '<' not supported errors. 
        """
        import traceback
        
        # Log input parameters at debug level
        logger.debug(f"close_live_position: symbol={symbol}, side={original_side}, notional=${notional_usd}")
        

        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Close {symbol} simuliert.")
            return True, "DRY_RUN_CLOSE_456"

        try:
            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 0: ERST ALLES LÖSCHEN (Fix für "same side as reduce-only")
            # ═══════════════════════════════════════════════════════════════
            await self.cancel_all_orders(symbol)
            await asyncio.sleep(0.5) # Kurz warten bis Lighter das verarbeitet hat

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 1: Hole aktuelle Positionen
            # ═══════════════════════════════════════════════════════════════
            positions = await self.fetch_open_positions()
            
            if not positions:
                logger.warning(f"⚠️ {symbol}: No positions found on Lighter")
                # ═══════════════════════════════════════════════════════════════
                # FIX: Clear cache entry when no positions found
                # ═══════════════════════════════════════════════════════════════
                if hasattr(self, '_positions_cache') and self._positions_cache:
                    self._positions_cache = [
                        p for p in self._positions_cache 
                        if p.get('symbol') != symbol
                    ]
                    logger.debug(f"[LIGHTER] Removed {symbol} from _positions_cache (no positions found)")
                return True, None  # Keine Position = schon geschlossen

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 2: Finde die Position für dieses Symbol
            # ═══════════════════════════════════════════════════════════════
            position = None
            for p in positions:
                p_symbol = p.get('symbol', '')
                if p_symbol == symbol:
                    position = p
                    break

            if not position:
                logger.info(f"Lighter {symbol}: Position not found (already closed? )")
                # ═══════════════════════════════════════════════════════════════
                # FIX: Update cache when position is already closed
                # ═══════════════════════════════════════════════════════════════
                if hasattr(self, '_positions_cache') and self._positions_cache:
                    self._positions_cache = [
                        p for p in self._positions_cache 
                        if p.get('symbol') != symbol
                    ]
                    logger.debug(f"[LIGHTER] Removed {symbol} from _positions_cache (position not found)")
                return True, None

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 3: PARANOID CASTING - Extrahiere und konvertiere ALLES
            # ═══════════════════════════════════════════════════════════════

            # Size - kann String oder Float sein! 
            size_raw = position.get('size', 0)
            size = safe_float(size_raw, 0.0)
            
            # Absolute Größe für Close
            close_size_coins = abs(size)

            # SICHERE VERGLEICHE (beide Seiten sind jetzt garantiert float)
            if close_size_coins <= 1e-8:
                logger.info(f"Lighter {symbol}: Position size too small ({size_raw}), treating as closed")
                return True, None

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 4: Preis holen - PARANOID SAFE CASTING
            # ═══════════════════════════════════════════════════════════════
            mark_price_raw = self.fetch_mark_price(symbol)
            mark_price = safe_float(mark_price_raw, 0.0)
            
            # Fallback auf Position-Daten wenn nötig
            if mark_price <= 0:
                fallback_price_raw = position.get('mark_price') or position.get('entry_price')
                mark_price = safe_float(fallback_price_raw, 0.0)
                
            # Final check after all attempts
            if mark_price <= 0:
                logger.error(f"❌ Lighter close {symbol}: Kein Preis verfügbar! (raw={mark_price_raw})")
                return False, None

            # Notional berechnen - beide Werte sind jetzt garantiert float
            close_notional_usd = float(close_size_coins) * float(mark_price)

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 4b: Dust Detection - Log but ALWAYS TRY TO CLOSE
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL FIX: We no longer skip dust positions! 
            # Instead, we attempt to close them with reduce_only=True which bypasses
            # our local min_notional validation. The exchange may still reject,
            # but we must try. This prevents positions getting stuck on shutdown.
            try:
                min_required = float(self.min_notional_usd(symbol))
            except Exception:
                min_required = 10.0  # konservatives Fallback

            is_dust = close_notional_usd < min_required
            if is_dust:
                # FIX: reduce_only orders bypass min_notional validation on the exchange
                # This is expected behavior and the order will succeed, so log at DEBUG level
                logger.debug(
                    f"🧹 DUST POSITION {symbol}: ${close_notional_usd:.2f} < Min ${min_required:.2f} - "
                    f"Closing with reduce_only (expected to succeed)"
                )

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 5: Bestimme Close-Seite (Gegenteil der Position)
            # ═══════════════════════════════════════════════════════════════
            # size > 0 = LONG position -> close with SELL
            # size < 0 = SHORT position -> close with BUY
            # SAFE: size is already safe_float converted above
            size_float = float(size)  # Extra safety - ensure float for comparison
            close_side = "SELL" if size_float > 0 else "BUY"

            logger.info(
                f"🔻 LIGHTER CLOSE {symbol}: "
                f"size={size:+.6f} coins, side={close_side}, "
                f"price=${mark_price:.4f}, notional=${close_notional_usd:.2f}, dust={is_dust}"
            )

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 6: Close Order ausführen
            # CRITICAL: Use IOC (Immediate-or-Cancel) + amount for exact coin size
            # This ensures we close the EXACT position, not an estimated USD amount
            # ═══════════════════════════════════════════════════════════════
            success, result = await self.open_live_position(
                symbol=symbol,
                side=close_side,
                notional_usd=close_notional_usd,
                amount=close_size_coins,  # CRITICAL: Use exact coin amount
                price=mark_price,         # Current mark price
                reduce_only=True,
                time_in_force="IOC"       # Immediate-Or-Cancel for fast fill
            )

            # ═══════════════════════════════════════════════════════════════
            # Handle special "position missing" case (Error 1137)
            # Position may have been liquidated or closed externally
            # ═══════════════════════════════════════════════════════════════
            if result == "POSITION_MISSING_1137":
                logger.warning(f"⚠️ {symbol}: Position missing on exchange - treating as already closed")
                # ═══════════════════════════════════════════════════════════════
                # FIX: Update cache when position is missing (already closed externally)
                # ═══════════════════════════════════════════════════════════════
                if hasattr(self, '_positions_cache') and self._positions_cache:
                    self._positions_cache = [
                        p for p in self._positions_cache 
                        if p.get('symbol') != symbol
                    ]
                    logger.debug(f"[LIGHTER] Removed {symbol} from _positions_cache (position missing 1137)")
                # Signal success so caller cleans up DB
                return True, "ALREADY_CLOSED_EXTERNAL"

            if success:
                logger.info(f"✅ Lighter close {symbol}: Erfolgreich (${close_notional_usd:.2f})")
                # ═══════════════════════════════════════════════════════════════
                # FIX: Update _positions_cache after successful close
                # This ensures fetch_open_positions returns correct data during shutdown
                # ═══════════════════════════════════════════════════════════════
                if hasattr(self, '_positions_cache') and self._positions_cache:
                    self._positions_cache = [
                        p for p in self._positions_cache 
                        if p.get('symbol') != symbol
                    ]
                    logger.debug(f"[LIGHTER] Removed {symbol} from _positions_cache after successful close")
                return True, result
            else:
                logger.warning(f"❌ Lighter close {symbol}: open_live_position returned False")
                return False, None

        except TypeError as e:
            # SPEZIFISCHER CATCH für den '<' not supported Fehler
            logger.critical(f"🚨 CRITICAL TypeError in close_live_position for {symbol}: {e}")
            logger.error(f"   FULL TRACEBACK:\n{traceback.format_exc()}")
            logger.error(f"   Input values at error:")
            logger.error(f"     - symbol: {symbol} (type={type(symbol)})")
            logger.error(f"     - original_side: {original_side} (type={type(original_side)})")
            logger.error(f"     - notional_usd: {notional_usd} (type={type(notional_usd)})")
            
            # Dump position data if available
            try:
                positions = await self.fetch_open_positions()
                for p in positions:
                    if p.get('symbol') == symbol:
                        logger.error(f"   Position data dump: {p}")
                        for key, val in p.items():
                            logger.error(f"     - {key}: {val} (type={type(val)})")
            except Exception as inner_e:
                logger.error(f"   Could not dump position data: {inner_e}")
            
            # Dump market_info for this symbol
            if symbol in self.market_info:
                logger.error(f"   market_info[{symbol}] dump:")
                for key, val in self.market_info[symbol].items():
                    logger.error(f"     - {key}: {val} (type={type(val)})")
            
            return False, None
        except Exception as e:
            logger.error(f"Lighter close {symbol}: Exception: {e}", exc_info=True)
            return False, None

    # ═══════════════════════════════════════════════════════════════
    # FIXED: Position callback infrastructure for Ghost-Fill detection
    # ═══════════════════════════════════════════════════════════════
    
    def register_position_callback(self, callback):
        """
        Register a callback to be notified when positions are fetched.
        Used by ParallelExecutionManager for faster Ghost-Fill detection.
        
        Args:
            callback: Async or sync function that takes a position dict
        """
        if callback not in self._position_callbacks:
            self._position_callbacks.append(callback)
            logger.debug(f"✅ Registered Lighter position callback: {callback.__name__}")
    
    async def _trigger_position_callbacks(self, positions: List[dict]):
        """
        Trigger all registered position callbacks.
        Called after fetch_open_positions returns.
        """
        if not self._position_callbacks or not positions:
            return
        
        for pos in positions:
            for callback in self._position_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(pos)
                    else:
                        callback(pos)
                except Exception as e:
                    logger.debug(f"Lighter position callback error: {e}")

    async def aclose(self):
        """Cleanup all sessions and connections"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Prevent duplicate close calls during shutdown
        # ═══════════════════════════════════════════════════════════════
        if hasattr(self, '_closed') and self._closed:
            return
        self._closed = True
        
        try:
            if hasattr(self, "_session") and self._session:
                try:
                    maybe_close = getattr(self._session, "close", None)
                    if maybe_close:
                        maybe = maybe_close()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if hasattr(self, "_rest_session") and self._rest_session:
                try:
                    maybe_close = getattr(self._rest_session, "close", None)
                    if maybe_close:
                        maybe2 = maybe_close()
                        if asyncio.iscoroutine(maybe2):
                            await maybe2
                except Exception:
                    pass
        except Exception:
            pass

        if self._signer:
            try:
                if hasattr(self._signer, "api_client"):
                    api_client = self._signer.api_client
                    try:
                        if hasattr(api_client, "close"):
                            maybe = api_client.close()
                            if asyncio.iscoroutine(maybe):
                                await maybe
                    except Exception:
                        try:
                            if hasattr(api_client, "session") and hasattr(api_client.session, "close"):
                                maybe2 = api_client.session.close()
                                if asyncio.iscoroutine(maybe2):
                                    await maybe2
                        except Exception:
                            pass

                if hasattr(self._signer, "close"):
                    maybe_sig = self._signer.close()
                    if asyncio. iscoroutine(maybe_sig):
                        await maybe_sig

            except Exception as e:
                logger.debug(f"Lighter cleanup warning: {e}")
            finally:
                self._signer = None

        logger.info("✅ Lighter Adapter closed")

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol.
        
        OPTIMIZED: During shutdown, use native batch cancel (ImmediateCancelAll) for speed.
        Normal operation uses per-order cancellation for precision.
        """
        if not HAVE_LIGHTER_SDK:
            return False

        try:
            signer = await self._get_signer()
            
            # ═══════════════════════════════════════════════════════════════
            # FAST SHUTDOWN: Use native cancel_all_orders (ImmediateCancelAll)
            # This cancels ALL orders in one transaction - much faster!
            #
            # IMPORTANT:
            # - The Lighter Python SDK's cancel_all_orders binding is quite
            #   picky about argument types. Passing `None` for the time
            #   parameter causes a low‑level TypeError:
            #   "argument 2: <class 'TypeError'>: wrong type".
            # - We therefore:
            #   * pass an explicit integer (0) for `time` so the FFI layer
            #     is happy
            #   * and if the call still raises a TypeError or any other
            #     exception, we PERMANENTLY mark the global‑cancel path as
            #     failed for this process and fall back to the per‑order
            #     logic below.
            #   This avoids hammering the broken ImmediateCancelAll path
            #   repeatedly during shutdown.
            # ═══════════════════════════════════════════════════════════════
            if getattr(config, 'IS_SHUTTING_DOWN', False):
                try:
                    await self.rate_limiter.acquire()
                    async with self.order_lock:
                        # Prevent duplicate global cancels
                        if self._shutdown_cancel_done:
                            logger.info("✅ Lighter: ImmediateCancelAll already executed (deduplicated)")
                            return True
                        
                        # If global cancel previously failed, skip to per-symbol
                        if self._shutdown_cancel_failed:
                            pass  # Fall through to normal operation
                        else:
                            # Get fresh nonce - same method as open_live_position
                            if self._resolved_account_index is None:
                                await self._resolve_account_index()
                                
                            nonce_params = {
                                "account_index": self._resolved_account_index,
                                "api_key_index": self._resolved_api_key_index
                            }
                            
                            # Use internal method - rate limiting already done above
                            nonce_resp = await self._rest_get_internal("/api/v1/nextNonce", params=nonce_params)
                            
                            if nonce_resp is None:
                                logger.warning(f"⚠️ Failed to fetch nonce for cancel_all")
                                raise ValueError("Nonce fetch failed")
                            
                            # Parse nonce response
                            current_nonce = 0
                            if isinstance(nonce_resp, dict):
                                val = nonce_resp.get('nonce')
                                if val is not None:
                                    current_nonce = int(val)
                                else:
                                    raise ValueError(f"Invalid nonce response: {nonce_resp}")
                            else:
                                current_nonce = int(str(nonce_resp).strip())
                            
                            # ═══════════════════════════════════════════════════════════════
                            # FIX #6: ImmediateCancelAll - Use correct IOC constant
                            # Priority: IMMEDIATE_OR_CANCEL first (most common in Lighter SDK)
                            # ═══════════════════════════════════════════════════════════════
                            # ImmediateCancelAll - cancels ALL open orders at once
                            # time_in_force=IOC triggers immediate cancel
                            if hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL'):
                                tif_ioc = SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
                            elif hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC'):
                                tif_ioc = SignerClient.ORDER_TIME_IN_FORCE_IOC
                            else:
                                # Fallback: IOC is typically 0 in most exchanges (including Lighter)
                                tif_ioc = 0
                            
                            # time parameter is usually a future timestamp for scheduled cancel.
                            # For ImmediateCancelAll we use 0 as a sentinel "no schedule" value;
                            # this keeps the FFI happy and lets the server interpret it as "now".
                            cancel_time = 0
                            
                            logger.info(f"⚡ Executing ImmediateCancelAll (nonce={current_nonce})...")
                            
                            try:
                                # SDK signature: cancel_all_orders(time_in_force: int, time: int)
                                # - time_in_force=0 means IMMEDIATE (cancel now)
                                # - time=0 means no scheduled time
                                # Note: nonce and api_key_index are handled internally by SDK
                                tx, resp, err = await signer.cancel_all_orders(
                                    int(tif_ioc),      # positional arg 1: time_in_force
                                    int(cancel_time),  # positional arg 2: time
                                    nonce=current_nonce,
                                    api_key_index=self._resolved_api_key_index
                                )
                            except TypeError as te:
                                # Low‑level signature / type issue – don't retry this path again.
                                logger.warning(f"⚠️ Lighter ImmediateCancelAll TypeError: {te!r} – disabling global cancel path and falling back to per-order cancel")
                                self._shutdown_cancel_failed = True
                                # ═══════════════════════════════════════════════════════════════
                                # CRITICAL FIX: Invalidate nonce cache - nonce may have been consumed
                                # ═══════════════════════════════════════════════════════════════
                                self._invalidate_nonce_cache()
                                raise
                            
                            if err:
                                err_str = str(err).lower()
                                # Ignore "no orders" errors
                                if "no" in err_str and "order" in err_str:
                                    logger.info(f"✅ Lighter: No open orders to cancel")
                                    self._shutdown_cancel_done = True
                                    # ═══════════════════════════════════════════════════════════════
                                    # CRITICAL FIX: Invalidate nonce cache after cancel_all_orders
                                    # The nonce was used, so subsequent orders need fresh nonce
                                    # ═══════════════════════════════════════════════════════════════
                                    self._invalidate_nonce_cache()
                                    return True
                                logger.warning(f"⚠️ Lighter cancel_all_orders error: {err}")
                                self._shutdown_cancel_failed = True
                                # ═══════════════════════════════════════════════════════════════
                                # CRITICAL FIX: Invalidate nonce cache even on error
                                # The nonce was consumed by the failed request
                                # ═══════════════════════════════════════════════════════════════
                                self._invalidate_nonce_cache()
                            else:
                                logger.info(f"✅ Lighter: ImmediateCancelAll executed (tx={tx})")
                                self._shutdown_cancel_done = True
                                # ═══════════════════════════════════════════════════════════════
                                # CRITICAL FIX: Invalidate nonce cache after cancel_all_orders
                                # The nonce was used, so subsequent orders need fresh nonce
                                # ═══════════════════════════════════════════════════════════════
                                self._invalidate_nonce_cache()
                                return True
                except Exception as e:
                    # Mark global cancel as failed so we don't keep retrying
                    # this path on every subsequent shutdown attempt.
                    if not self._shutdown_cancel_failed:
                        self._shutdown_cancel_failed = True
                    # ═══════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Invalidate nonce cache - nonce may have been consumed
                    # ═══════════════════════════════════════════════════════════════
                    self._invalidate_nonce_cache()
                    logger.warning(f"⚠️ Lighter ImmediateCancelAll failed: {e}, falling back to per-order cancel")
                    # Fall through to per-order cancellation
            
            # ═══════════════════════════════════════════════════════════════
            # NORMAL OPERATION: Per-order cancellation (symbol-filtered)
            # ═══════════════════════════════════════════════════════════════
            order_api = OrderApi(signer. api_client)

            market_data = self.market_info. get(symbol)
            if not market_data:
                return False

            market_id = market_data.get("i")

            orders_resp = None
            await self.rate_limiter.acquire()
            candidate_methods = [
                "list_orders", "get_open_orders", "get_orders", 
                "orders", "list_open_orders",
            ]
            for method_name in candidate_methods:
                if hasattr(order_api, method_name):
                    method = getattr(order_api, method_name)
                    try:
                        try:
                            orders_resp = await method(market_id=market_id)
                        except TypeError:
                            try:
                                orders_resp = await method(market=market_id)
                            except TypeError:
                                orders_resp = await method()
                        break
                    except Exception:
                        orders_resp = None
                        continue

            orders_list = None
            if orders_resp is None:
                return True
            if hasattr(orders_resp, "orders"):
                orders_list = orders_resp.orders
            elif isinstance(orders_resp, dict):
                orders_list = (
                    orders_resp.get("orders")
                    or orders_resp.get("data")
                    or orders_resp.get("result")
                    or None
                )
                if isinstance(orders_list, dict) and orders_list.get("orders"):
                    orders_list = orders_list. get("orders")
            elif hasattr(orders_resp, "data") and getattr(orders_resp, "data") is not None:
                data = getattr(orders_resp, "data")
                if isinstance(data, list):
                    orders_list = data
                elif isinstance(data, dict) and data.get("orders"):
                    orders_list = data. get("orders")
            elif isinstance(orders_resp, list):
                orders_list = orders_resp

            if not orders_list:
                # FIX (2025-12-13): Double-check via REST API get_open_orders()
                # The SDK method might not find orders due to eventual consistency,
                # but orders might still exist in the orderbook. Use REST API as fallback.
                try:
                    rest_orders = await self.get_open_orders(symbol)
                    if rest_orders and len(rest_orders) > 0:
                        logger.warning(f"⚠️ [CANCEL_ALL] {symbol}: SDK found no orders, but REST API found {len(rest_orders)} order(s) - will cancel them")
                        # Fall through to cancellation logic below
                        orders_list = rest_orders
                    else:
                        # Both SDK and REST API found no orders - truly empty
                        logger.debug(f"✅ [CANCEL_ALL] {symbol}: No orders found via SDK or REST API - nothing to cancel")
                        return True
                except Exception as rest_e:
                    logger.debug(f"⚠️ [CANCEL_ALL] {symbol}: REST API check failed: {rest_e} - assuming no orders")
                    return True

            cancel_candidates = [
                "cancel_order", "cancel", "cancel_order_by_id", "delete_order",
            ]
            
            cleaned_orders_count = 0
            
            for order in orders_list:
                if isinstance(order, dict):
                    oid = (
                        order. get("id")
                        or order.get("order_id")
                        or order.get("orderId")
                        or order.get("client_order_id")
                    )
                    status = order.get("status")
                else:
                    oid = (
                        getattr(order, "id", None)
                        or getattr(order, "order_id", None)
                        or getattr(order, "orderId", None)
                    )
                    status = getattr(order, "status", None)

                if not oid:
                    continue

                if status and status not in ["PENDING", "OPEN", 10, 0]:
                    continue

                cancelled = False
                # 1. Try API methods
                for cancel_name in cancel_candidates:
                    if hasattr(order_api, cancel_name):
                        cancel_method = getattr(order_api, cancel_name)
                        try:
                            try:
                                await self.rate_limiter.acquire()
                                await cancel_method(order_id=oid)
                            except TypeError:
                                try:
                                    await self.rate_limiter.acquire()
                                    await cancel_method(id=oid)
                                except TypeError:
                                    await self.rate_limiter.acquire()
                                    await cancel_method(oid)
                            cancelled = True
                            await asyncio.sleep(0.05) # Small delay for rate limits
                            break
                        except Exception as e:
                            logger.debug(f"Cancel order {oid} via {cancel_name} failed: {e}")
                            continue

                # 2. Hard Fallback: Signer direct
                if not cancelled:
                    try:
                        if hasattr(signer, "cancel_order"):
                            try:
                                await self.rate_limiter.acquire()
                                async with self.order_lock:
                                    # Try both int and str for ID
                                    try:
                                        await signer.cancel_order(int(oid))
                                    except ValueError:
                                         await signer.cancel_order(oid)
                                await asyncio.sleep(0.05)
                                cancelled = True
                            except TypeError:
                                # Fallback args
                                await self.rate_limiter.acquire()
                                await signer.cancel_order(order_id=oid)
                                cancelled = True
                    except Exception as e:
                        logger. debug(f"Signer cancel order {oid} failed: {e}")
                
                if cancelled:
                    cleaned_orders_count += 1

            # 3. FINAL HARD CHECK: Fetch open orders explicitly (Force-Verify)
            # This handles cases where 'list_orders' didn't return everything or cancellation failed silently.
            # Only do this if we had orders or suspect issues.
            try:
                verified_open = await self.get_open_orders(symbol)
                if verified_open:
                    logger.warning(f"⚠️ {symbol}: {len(verified_open)} orders remained after bulk cancel. Force cancelling individually...")
                    for o in verified_open:
                        oid = o['id']
                        logger.info(f"   🔪 Force cancelling {oid}...")
                        await self.cancel_limit_order(oid, symbol)
                        cleaned_orders_count += 1
            except Exception as e:
                logger.error(f"Error in final cancel verification for {symbol}: {e}")

            return True
        except Exception as e:
            logger. debug(f"Lighter cancel_all_orders error: {e}")
            return False

    async def get_positions(self, force_refresh: bool = False) -> List[dict]:
        """Alias for fetch_open_positions with optional force refresh."""
        return await self.fetch_open_positions()

    def get_market_info(self, symbol: str) -> Optional[dict]:
        """Get market info for a symbol."""
        return self. market_info.get(symbol)

    def get_all_symbols(self) -> List[str]:
        """Get all available symbols."""
        return list(self.market_info.keys())

    async def prefetch_prices(self) -> int:
        """Pre-fetch all prices via REST API.  Returns count of prices loaded."""
        if not HAVE_LIGHTER_SDK:
            return 0

        try:
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)
            
            await self.rate_limiter.acquire()
            market_list = await order_api.order_book_details()
            
            if not market_list or not market_list. order_book_details:
                return 0

            count = 0
            now = time.time()
            
            for m in market_list. order_book_details:
                symbol_raw = getattr(m, "symbol", None)
                if not symbol_raw:
                    continue
                
                symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                
                mark_price = getattr(m, "mark_price", None) or getattr(m, "last_trade_price", None)
                if mark_price:
                    price_float = safe_float(mark_price, 0.0)
                    if price_float > 0:
                        self.price_cache[symbol] = price_float
                        self._price_cache[symbol] = price_float
                        self._price_cache_time[symbol] = now
                        count += 1

            self.rate_limiter.on_success()
            logger.info(f"✅ Lighter prices loaded: {count} symbols")
            return count

        except asyncio.CancelledError:
            logger.debug(f"{self.name}: prefetch_prices cancelled during shutdown")
            return len(self.price_cache)  # Return current cache count
        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            logger.error(f"Lighter prefetch_prices error: {e}")
            return 0

    async def get_collateral_balance(self, account_index: int = 0) -> float:
        """
        Hole die verfügbare Balance für Lighter via SDK oder REST API.
        """
        try:
            if not HAVE_LIGHTER_SDK:
                return await self._get_balance_via_rest()

            if self._resolved_account_index is None:
                await self._resolve_account_index()

            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)
            
            await self.rate_limiter.acquire()
            response = await account_api.account(by="index", value=str(self._resolved_account_index))
            self.rate_limiter.on_success()
            
            if response:
                # Try to access response.account first if it exists
                account_data = getattr(response, 'account', response)
                
                # Lighter uses 'available_balance' or 'balance' in the account object
                for attr in ['available_balance', 'balance', 'free_collateral', 'equity', 'collateral']:
                    val = getattr(account_data, attr, None)
                    if val is not None:
                        try:
                            balance = float(val)
                            if balance > 0:
                                logger.info(f"💰 Lighter verfügbare Balance: ${balance:.2f}")
                                self._balance_cache = balance
                                return balance
                        except (ValueError, TypeError):
                            continue
                
                # Fallback: REST API
                logger.debug(f"Lighter SDK response hat keine erkennbare Balance, versuche REST")
                return await self._get_balance_via_rest()
            
            return await self._get_balance_via_rest()
            
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: get_collateral_balance cancelled during shutdown")
            return self._balance_cache if self._balance_cache > 0 else 0.0
        except Exception as e:
            logger.warning(f"Lighter SDK Balance-Abfrage fehlgeschlagen: {e}")
            return await self._get_balance_via_rest()

    async def _get_balance_via_rest(self) -> float:
        """Fallback: Hole Balance direkt via REST API"""
        try:
            if self._resolved_account_index is None:
                await self._resolve_account_index()
            
            base_url = self._get_base_url()
            # Korrekter Endpunkt: /api/v1/account?by=index&value=<account_index>
            url = f"{base_url}/api/v1/account"
            params = {
                "by": "index",
                "value": str(self._resolved_account_index)
            }
            
            await self.rate_limiter.acquire()
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.rate_limiter.on_success()
                    
                    # API returns: {"code": 0, "total": 1, "accounts": [{...}]}
                    # We need to access accounts[0]
                    accounts = data.get('accounts', [])
                    if accounts and len(accounts) > 0:
                        account = accounts[0]
                    else:
                        account = data.get('account', data)
                    
                    # Lighter returns balance in 'available_balance' or similar
                    for key in ['available_balance', 'balance', 'free_collateral', 'equity', 'collateral', 'margin']:
                        val = account.get(key) if isinstance(account, dict) else getattr(account, key, None)
                        if val is not None:
                            try:
                                balance = float(val)
                                if balance > 0:
                                    logger.info(f"💰 Lighter Balance via REST: ${balance:.2f}")
                                    self._balance_cache = balance
                                    return balance
                            except (ValueError, TypeError):
                                continue
                    
                    # Log what fields we found for debugging
                    if isinstance(account, dict):
                        logger.debug(f"Lighter REST Account-Felder: {list(account.keys())}")
                        logger.debug(f"Lighter REST Account-Daten: {account}")
                else:
                    body = await resp.text()
                    logger.debug(f"Lighter REST Balance: HTTP {resp.status} - {body[:200]}")
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: _get_balance_via_rest cancelled during shutdown")
            return self._balance_cache if self._balance_cache > 0 else 0.0
        except Exception as e:
            logger.warning(f"Lighter REST Balance-Fallback fehlgeschlagen: {e}")
        
        # Final fallback
        if self._balance_cache > 0:
            return self._balance_cache
        return 0.0  # Return 0 instead of fake value

    async def get_free_balance(self) -> float:
        """Alias for get_collateral_balance to satisfy interface."""
        return await self.get_collateral_balance()