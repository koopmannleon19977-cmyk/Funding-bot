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
from enum import IntEnum

# Module initialization

import config

# ═══════════════════════════════════════════════════════════════════════════════
# Q1: Transaction Status Enums (from lighter-ts-main/src/signer/wasm-signer-client.ts)
# ═══════════════════════════════════════════════════════════════════════════════
class TransactionStatus(IntEnum):
    """Lighter transaction status codes."""
    PENDING = 0
    QUEUED = 1
    COMMITTED = 2
    EXECUTED = 3
    FAILED = 4
    REJECTED = 5


# ═══════════════════════════════════════════════════════════════════════════════
# Q2: CancelAll TimeInForce Enums (from lighter-ts-main API)
# ═══════════════════════════════════════════════════════════════════════════════
class CancelAllTimeInForce(IntEnum):
    """TimeInForce options for CancelAllOrders."""
    IMMEDIATE = 0      # Cancel immediately
    SCHEDULED = 1      # Schedule cancellation
    ABORT = 2          # Abort pending scheduled cancellation


# ═══════════════════════════════════════════════════════════════════════════════
# Additional Lighter Enums for completeness (Q6, Q7, Q8)
# ═══════════════════════════════════════════════════════════════════════════════
class LighterOrderType(IntEnum):
    """Lighter order types."""
    LIMIT = 0
    MARKET = 1
    STOP_LOSS = 2
    TAKE_PROFIT = 4
    TWAP = 6


class LighterTimeInForce(IntEnum):
    """Lighter TimeInForce options."""
    IOC = 0           # Immediate or Cancel
    GTT = 1           # Good Till Time
    POST_ONLY = 2     # Post Only (Maker)


class LighterMarginMode(IntEnum):
    """Lighter margin modes."""
    CROSS = 0         # Cross Margin
    ISOLATED = 1      # Isolated Margin


class LighterTransactionType(IntEnum):
    """Lighter transaction types."""
    TRANSFER = 12
    WITHDRAW = 13
    CREATE_ORDER = 14
    CANCEL_ORDER = 15
    CANCEL_ALL_ORDERS = 16
    MODIFY_ORDER = 17
    MINT_SHARES = 18
    BURN_SHARES = 19
    UPDATE_LEVERAGE = 20
    CREATE_GROUPED_ORDERS = 28
    UPDATE_MARGIN = 29


def _normalize_lighter_orders_response(resp) -> list:
    """Normalize Lighter /api/v1/orders payloads into a flat list of order dicts.

    Observed shapes:
    - [Order, ...]
    - {"orders": [Order, ...], ...}
    - {"orders": {"<MARKET_INDEX>": [Order, ...]}, ...}
    - {"data": [...]} / {"result": [...]} (fallbacks)
    """
    if not resp:
        return []
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []

    payload = resp.get("orders") or resp.get("data") or resp.get("result")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        nested_list = payload.get("orders")
        if isinstance(nested_list, list):
            return nested_list

        flattened = []
        for v in payload.values():
            if isinstance(v, list):
                flattened.extend(v)
        return flattened

    return []


# ═══════════════════════════════════════════════════════════════════════════════
# Korrekte Imports für das offizielle Lighter SDK
# ═══════════════════════════════════════════════════════════════════════════════
OrderApi = None
FundingApi = None
AccountApi = None
SignerClient = None
CreateOrderTxReq = None
HAVE_LIGHTER_SDK = False

try:
    from lighter.api.order_api import OrderApi
    from lighter.api.funding_api import FundingApi
    from lighter.api.account_api import AccountApi
    from lighter.signer_client import SignerClient, CreateOrderTxReq
    HAVE_LIGHTER_SDK = True
except ImportError as e:
    HAVE_LIGHTER_SDK = False
    # Logged at runtime when adapter is initialized

from .base_adapter import BaseAdapter, Position, OrderResult
from src.infrastructure.rate_limiter import LIGHTER_RATE_LIMITER, rate_limited, Exchange, with_rate_limit
from src.adapters.lighter_client_fix import SaferSignerClient
from src.application.batch_manager import LighterBatchManager
from src.adapters.ws_order_client import WebSocketOrderClient, WsOrderConfig
from .lighter_stream_client import LighterStreamClient


logger = logging.getLogger(__name__)

MARKET_OVERRIDES = {
    "ASTER-USD": {"ss": Decimal("1"), "sd": 8},
    "HYPE-USD": {"ss": Decimal("0.01"), "sd": 6},
    "MEGA-USD": {"ss": Decimal("99999999")},
}


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL TYPE-SAFETY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

from src.utils import safe_float, safe_int, quantize_value, safe_decimal


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
        self._ws_market_stats_last_ts = 0.0
        self._ws_market_stats_ready_event = asyncio.Event()
        self._ws_market_stats_ready_at = 0.0
        self._init_time = time.time()
        self._rest_refresh_ts = 0.0
        
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
        
        # Lock für thread-sichere Orderbook Cache Updates (WebSocket + REST können gleichzeitig schreiben)
        self._orderbook_cache_lock = asyncio.Lock()
        
        # ═══════════════════════════════════════════════════════════════
        # FIXED: Position callback infrastructure for Ghost-Fill detection
        # ═══════════════════════════════════════════════════════════════
        self._position_callbacks: List[Any] = []
        self._positions_cache: List[dict] = []  # Cache for fetch_open_positions deduplication
        self._positions_cache_time = 0.0
        self._positions_cache_ttl = float(
            getattr(config, "LIGHTER_POSITIONS_CACHE_SECONDS", 5.0)
        )
        
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

        # ═══════════════════════════════════════════════════════════════
        # BATCH ORDERS (New Implementation)
        # ═══════════════════════════════════════════════════════════════
        self.batch_manager = LighterBatchManager(self)
        
        # ═══════════════════════════════════════════════════════════════
        # B1: WebSocket Order Client for low-latency order submission
        # Pattern from lighter-ts-main/src/api/ws-order-client.ts
        # Primary: Send orders via WS (~50-100ms faster)
        # Fallback: REST API if WS not connected
        # NOTE: Uses /stream endpoint (same as market data WS)
        #       /jsonapi endpoint returns 404 on mainnet
        # ═══════════════════════════════════════════════════════════════
        ws_order_url = "wss://mainnet.zklighter.elliot.ai/stream"
        if getattr(config, "LIGHTER_BASE_URL", "").startswith("https://testnet"):
            ws_order_url = "wss://testnet.zklighter.elliot.ai/stream"
        self.ws_order_client = WebSocketOrderClient(WsOrderConfig(url=ws_order_url))
        self._ws_order_enabled = getattr(config, "LIGHTER_WS_ORDERS", True)
        
        # ═══════════════════════════════════════════════════════════════
        # Lighter Stream Client for real-time updates
        # ═══════════════════════════════════════════════════════════════
        self._stream_client: Optional[LighterStreamClient] = None
        self._stream_client_task: Optional[asyncio.Task] = None
        self._stream_metrics: Dict[str, Any] = {
            'orderbook_updates': 0,
            'trade_updates': 0,
            'funding_updates': 0,
            'last_update_time': 0.0,
            'connection_health': 'unknown',
            'reconnect_count': 0
        }
        self._stream_health_check_task: Optional[asyncio.Task] = None


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
    
    async def _submit_order_via_ws(
        self,
        tx_info_json: str,
        max_retries: Optional[int] = None
    ) -> Optional[Any]:
        """
        Submit order via WebSocket with retry logic.
        
        Args:
            tx_info_json: Signed transaction info JSON string from signer
            max_retries: Maximum retry attempts (default from config)
            
        Returns:
            WsTransaction if successful, None if all retries failed
        """
        from src.adapters.ws_order_client import TransactionType
        
        if max_retries is None:
            max_retries = getattr(config, 'WS_ORDER_MAX_RETRIES', 2)
        
        backoff_base = getattr(config, 'WS_ORDER_RETRY_BACKOFF_BASE', 0.1)
        
        for attempt in range(max_retries):
            try:
                # Check connection health before each attempt
                if not self.ws_order_client.is_connected:
                    logger.debug(f"[WS-ORDER] Attempt {attempt+1}: Reconnecting...")
                    connected = await self.ws_order_client.connect()
                    if not connected:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(backoff_base * (2 ** attempt))
                            continue
                        else:
                            logger.warning(f"[WS-ORDER] Failed to connect after {max_retries} attempts")
                            return None
                
                # Submit via WebSocket
                result = await self.ws_order_client.send_transaction(
                    tx_type=TransactionType.CREATE_ORDER,
                    tx_info=tx_info_json
                )
                
                logger.debug(f"[WS-ORDER] Order submitted successfully via WebSocket (attempt {attempt+1})")
                return result
                
            except ConnectionError as e:
                logger.warning(f"[WS-ORDER] Attempt {attempt+1}: Connection error: {e}")
                if attempt < max_retries - 1:
                    # Try to reconnect
                    try:
                        await self.ws_order_client.connect()
                    except Exception:
                        pass
                    await asyncio.sleep(backoff_base * (2 ** attempt))
                    continue
                else:
                    logger.warning(f"[WS-ORDER] All {max_retries} retries failed (ConnectionError)")
                    return None
                    
            except Exception as e:
                # Non-recoverable error (e.g., invalid signature, invalid order params)
                error_str = str(e).lower()
                if "invalid nonce" in error_str or "nonce" in error_str:
                    # Nonce error - refresh and retry once more
                    if attempt < max_retries - 1:
                        logger.warning(f"[WS-ORDER] Nonce error detected - refreshing nonce pool")
                        await self.hard_refresh_nonce()
                        await asyncio.sleep(backoff_base * (2 ** attempt))
                        continue
                
                logger.warning(f"[WS-ORDER] Attempt {attempt+1}: Error: {e}")
                if attempt == max_retries - 1:
                    # Final attempt failed
                    return None
                await asyncio.sleep(backoff_base * (attempt + 1))
        
        return None  # All retries failed
    
    def _parse_ws_error(self, error_msg: Any) -> tuple[str, int]:
        """
        Parse WebSocket error response (matching TS SDK pattern).
        
        Args:
            error_msg: Error message (dict, string, or Exception)
            
        Returns:
            Tuple of (error_message: str, error_code: int)
        """
        if isinstance(error_msg, dict):
            error_code = error_msg.get('error', {}).get('code', 'UNKNOWN')
            error_message = error_msg.get('error', {}).get('message', str(error_msg))
        elif isinstance(error_msg, Exception):
            error_str = str(error_msg).lower()
            # Try to extract error code from exception message
            import re
            code_match = re.search(r'(?:error|code)[:\s]*(\d+)', error_str)
            error_code = code_match.group(1) if code_match else 'UNKNOWN'
            error_message = str(error_msg)
        else:
            error_str = str(error_msg).lower()
            import re
            code_match = re.search(r'(?:error|code)[:\s]*(\d+)', error_str)
            error_code = code_match.group(1) if code_match else 'UNKNOWN'
            error_message = str(error_msg)
        
        # Convert error code to int if possible
        try:
            error_code_int = int(error_code) if str(error_code).isdigit() else 0
        except (ValueError, TypeError):
            error_code_int = 0
        
        # Categorize errors for better handling
        error_lower = error_message.lower()
        if 'invalid nonce' in error_lower or 'nonce' in error_lower:
            return ('NONCE_ERROR', error_code_int if error_code_int > 0 else 400)
        elif 'insufficient balance' in error_lower or 'not enough' in error_lower:
            return ('BALANCE_ERROR', error_code_int if error_code_int > 0 else 400)
        elif 'position' in error_lower and ('missing' in error_lower or 'not found' in error_lower):
            return ('POSITION_ERROR', error_code_int if error_code_int > 0 else 1137)
        elif 'invalid price' in error_lower or 'accidental price' in error_lower:
            return ('PRICE_ERROR', error_code_int if error_code_int > 0 else 21733)
        elif 'connection' in error_lower or 'timeout' in error_lower:
            return ('CONNECTION_ERROR', error_code_int if error_code_int > 0 else 0)
        else:
            return (error_message, error_code_int)
    
    def _validate_tx_info(self, tx_info_json: str) -> bool:
        """
        Validate transaction info before WebSocket submission.
        
        Args:
            tx_info_json: Signed transaction info JSON string
            
        Returns:
            True if valid, False otherwise
        """
        try:
            import json
            # Parse JSON to validate format
            if isinstance(tx_info_json, str):
                tx_info = json.loads(tx_info_json)
            else:
                tx_info = tx_info_json
            
            # Check required fields (based on Lighter API)
            required_fields = ['market_index', 'base_amount', 'price', 'nonce']
            for field in required_fields:
                if field not in tx_info:
                    logger.warning(f"[WS-ORDER] Missing required field in tx_info: {field}")
                    return False
            
            # Validate field types
            if not isinstance(tx_info.get('market_index'), (int, type(None))):
                logger.warning(f"[WS-ORDER] Invalid market_index type: {type(tx_info.get('market_index'))}")
                return False
            
            if not isinstance(tx_info.get('base_amount'), (int, type(None))):
                logger.warning(f"[WS-ORDER] Invalid base_amount type: {type(tx_info.get('base_amount'))}")
                return False
            
            if not isinstance(tx_info.get('price'), (int, type(None))):
                logger.warning(f"[WS-ORDER] Invalid price type: {type(tx_info.get('price'))}")
                return False
            
            if not isinstance(tx_info.get('nonce'), (int, type(None))):
                logger.warning(f"[WS-ORDER] Invalid nonce type: {type(tx_info.get('nonce'))}")
                return False
            
            return True
            
        except json.JSONDecodeError as e:
            logger.warning(f"[WS-ORDER] Invalid JSON in tx_info: {e}")
            return False
        except Exception as e:
            logger.warning(f"[WS-ORDER] Validation error: {e}")
            return False
    
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
        
        B1: Primary via WebSocket for lower latency (~50-100ms faster)
        Fallback: REST API if WS not connected
        
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
        
        # ═══════════════════════════════════════════════════════════════
        # B1: Try WebSocket first for lower latency
        # ═══════════════════════════════════════════════════════════════
        if (hasattr(self, 'ws_order_client') and 
            self._ws_order_enabled and 
            self.ws_order_client.is_connected):
            try:
                import time
                start = time.time()
                
                results = await self.ws_order_client.send_batch_transactions(tx_types, tx_infos)
                
                latency_ms = (time.time() - start) * 1000
                tx_hashes = [r.hash for r in results if r.hash]
                
                logger.info(f"✅ [WS-ORDER] Batch of {len(tx_types)} sent in {latency_ms:.0f}ms")
                return True, tx_hashes
                
            except Exception as e:
                logger.warning(f"⚠️ [WS-ORDER] Batch failed: {e} - falling back to REST")
        
        # ═══════════════════════════════════════════════════════════════
        # REST Fallback
        # ═══════════════════════════════════════════════════════════════
        try:
            base_url = self._get_base_url()
            url = f"{base_url}/api/v1/sendTxBatch"
            
            # Format: tx_types and tx_infos as JSON arrays (stringified)
            payload = {
                "tx_types": json.dumps(tx_types),
                "tx_infos": json.dumps(tx_infos)
            }
            
            logger.info(f"📦 [REST] Sending batch of {len(tx_types)} transactions...")
            
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
                    logger.info(f"✅ [REST] Batch TX success: {len(tx_hashes)} transactions")
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

        now = time.time()
        if self._positions_cache_ttl > 0:
            cache_age = now - self._positions_cache_time
            if cache_age < self._positions_cache_ttl and self._positions_cache is not None:
                return self._positions_cache
    
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
                    logger.warning(f"[LIGHTER] 429 from {path}")
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
                    logger.warning(f"[LIGHTER] 429 from {path} (internal)")
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
                return None
            if acc_idx is None:
                logger.warning(f"Lighter get_open_orders: No account index resolved")
                return []
                
            market = self.market_info.get(symbol)
            if not market:
                logger.debug(f"Lighter get_open_orders: No market info for {symbol}")
                return []
            
            # Resolve BOTH identifiers.
            # Some endpoints use `market_id`, others use `market_index` (or `i`).
            market_id_val = (
                market.get('market_id')
                if market.get('market_id') is not None
                else market.get('marketId')
            )
            market_index_val = (
                market.get('market_index')
                if market.get('market_index') is not None
                else market.get('marketIndex')
            )
            # Backwards-compatible fallback (older market_info stored only `i`)
            if market_id_val is None and market_index_val is None:
                market_id_val = market.get('id') if market.get('id') is not None else market.get('i')

            if market_id_val is None and market_index_val is None:
                logger.debug(f"Lighter get_open_orders: No market id/index for {symbol}")
                return []

            # ═══════════════════════════════════════════════════════════════
            # CRITICAL FIX (2025-12-19): DO NOT filter by status in API call!
            # Problem: API with status=0 might not return all "open" orders
            # Solution: Fetch ALL orders and filter client-side by status
            # Status values: 0=Open, 1=Filled, 2=Cancelled, 3=Expired, 4=Rejected
            # ═══════════════════════════════════════════════════════════════
            resp = None
            orders_data = []
            used_param = None

            # Try both param names with their correct values.
            candidate_pairs = []
            if market_id_val is not None:
                candidate_pairs.append(("market_id", int(market_id_val)))
            if market_index_val is not None:
                candidate_pairs.append(("market_index", int(market_index_val)))

            # If we only know one identifier, still try the alternate param name with same value.
            if len(candidate_pairs) == 1:
                param_name, value = candidate_pairs[0]
                other_param = "market_index" if param_name == "market_id" else "market_id"
                candidate_pairs.append((other_param, value))

            # De-duplicate while preserving order
            seen = set()
            dedup_pairs = []
            for pair in candidate_pairs:
                if pair in seen:
                    continue
                seen.add(pair)
                dedup_pairs.append(pair)

            for market_param_name, market_ref in dedup_pairs:
                used_param = market_param_name
                params = {
                    "account_index": int(acc_idx),
                    market_param_name: int(market_ref),
                    # DO NOT FILTER BY STATUS - get all and filter client-side
                    "limit": 100,
                }

                logger.info(f"🔍 [API CALL] get_open_orders({symbol}): Calling /api/v1/orders with params={params}")
                resp = await self._rest_get("/api/v1/orders", params=params)

                # Handle empty/None response (404 is converted to empty dict by _rest_get)
                if not resp:
                    logger.info(f"🔍 [API DEBUG] get_open_orders({symbol}): Empty response for {market_param_name}, trying fallback...")
                    continue

                orders_data = _normalize_lighter_orders_response(resp)
                if orders_data:
                    break

            # DEBUG: Log actual API response
            logger.info(
                f"🔍 [API RESPONSE] get_open_orders({symbol}): used_param={used_param}, type={type(resp)}, "
                f"len={len(resp) if isinstance(resp, (list, dict)) else 'N/A'}, "
                f"market_id={market_id_val}, market_index={market_index_val}"
            )

            # CRITICAL DEBUG: Log what we got from API
            logger.info(
                f"🔍 [API PARSE] get_open_orders({symbol}): used_param={used_param}, orders_data type={type(orders_data)}, "
                f"len={len(orders_data) if isinstance(orders_data, list) else 'N/A'}"
            )
            
            if not orders_data:
                # No open orders is a valid outcome. Only warn if the payload shape is unexpected.
                if isinstance(resp, dict):
                    raw_orders_payload = resp.get("orders")
                    if isinstance(raw_orders_payload, dict):
                        nested_list_count = sum(
                            len(v) for v in raw_orders_payload.values() if isinstance(v, list)
                        )
                        if nested_list_count > 0:
                            logger.warning(
                                f"⚠️ [API PARSE] get_open_orders({symbol}): Normalization returned 0 orders "
                                f"but raw nested lists contain {nested_list_count} items. Response keys: {list(resp.keys())}"
                            )
                        else:
                            logger.info(f"🔍 [API RESULT] get_open_orders({symbol}): Found 0 OPEN orders")
                    elif isinstance(raw_orders_payload, list):
                        logger.info(f"🔍 [API RESULT] get_open_orders({symbol}): Found 0 OPEN orders")
                    else:
                        logger.warning(
                            f"⚠️ [API PARSE] get_open_orders({symbol}): Unexpected 'orders' payload type={type(raw_orders_payload)}. "
                            f"Response keys: {list(resp.keys())}"
                        )
                        logger.info(f"🔍 [API RESULT] get_open_orders({symbol}): Found 0 OPEN orders")
                else:
                    logger.warning(
                        f"⚠️ [API PARSE] get_open_orders({symbol}): Unexpected response type={type(resp)}"
                    )
                    logger.info(f"🔍 [API RESULT] get_open_orders({symbol}): Found 0 OPEN orders")
                return []
            
            open_orders = []
            all_statuses = {}  # Track status distribution
            for o in orders_data:
                try:
                    # Parse order data according to Lighter API response format
                    order_id = o.get('order_index') or o.get('id') or o.get('order_id')
                    price = safe_float(o.get('price', 0))
                    
                    # CRITICAL: Get order status and filter client-side
                    order_status = o.get('status', o.get('order_status', None))
                    if order_status is not None:
                        all_statuses[order_status] = all_statuses.get(order_status, 0) + 1
                    
                    # Only include orders with status=0 (Open)
                    # Status: 0=Open, 1=Filled, 2=Cancelled, 3=Expired, 4=Rejected
                    if order_status != 0:
                        continue
                    
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
            
            logger.info(f"🔍 [API RESULT] get_open_orders({symbol}): Found {len(open_orders)} OPEN orders. Status distribution: {all_statuses}")
            
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
            
            # Resolve both identifiers when symbol is provided.
            market_id_val = None
            market_index_val = None
            if symbol and symbol in self.market_info:
                market = self.market_info.get(symbol) or {}
                market_id_val = market.get("market_id") if market.get("market_id") is not None else market.get("marketId")
                market_index_val = market.get("market_index") if market.get("market_index") is not None else market.get("marketIndex")
                if market_id_val is None and market_index_val is None:
                    market_id_val = market.get("id") if market.get("id") is not None else market.get("i")

            base_params = {
                "account_index": acc_idx,
                "order_id": order_id,
            }

            candidate_params = [base_params]
            if market_id_val is not None:
                candidate_params.insert(0, {**base_params, "market_id": int(market_id_val)})
            if market_index_val is not None:
                candidate_params.insert(0, {**base_params, "market_index": int(market_index_val)})

            # De-dup while preserving order
            seen = set()
            dedup_params = []
            for p in candidate_params:
                key = tuple(sorted(p.items()))
                if key in seen:
                    continue
                seen.add(key)
                dedup_params.append(p)

            resp = None
            orders = []
            for params in dedup_params:
                resp = await self._rest_get("/api/v1/orders", params=params)
                if not resp:
                    continue
                orders = _normalize_lighter_orders_response(resp)
                if orders:
                    break
            
            target_order = None
            for o in orders:
                # Compare ID (string comparison for safety)
                oid = o.get('order_index') or o.get('id') or o.get('order_id')
                if oid is not None and str(oid) == str(order_id):
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
                    # Lighter REST /api/v1/orders status codes:
                    # 0=Open, 1=Filled, 2=Cancelled, 3=Expired, 4=Rejected
                    if s_raw == 0:
                        status_str = "OPEN"
                    elif s_raw == 1:
                        status_str = "FILLED"
                    elif s_raw == 2:
                        status_str = "CANCELED"
                    elif s_raw == 3:
                        status_str = "EXPIRED"
                    elif s_raw == 4:
                        status_str = "REJECTED"
                else:
                    s_norm = str(s_raw or "").strip().lower()
                    if s_norm in {"open", "0"}:
                        status_str = "OPEN"
                    elif s_norm in {"filled", "1"}:
                        status_str = "FILLED"
                    elif s_norm in {"cancelled", "canceled", "2"}:
                        status_str = "CANCELED"
                    elif s_norm in {"expired", "3"}:
                        status_str = "EXPIRED"
                    elif s_norm in {"rejected", "4"}:
                        status_str = "REJECTED"
                    elif s_norm:
                        status_str = s_norm.upper()
                    
                return {
                    "id": str(target_order.get('id', '')),
                    "status": status_str,
                    "filledAmount": safe_float(target_order.get('executedQty', target_order.get('filled_amount', target_order.get('total_executed_size', 0)))),
                    "executedQty": safe_float(target_order.get('executedQty', target_order.get('filled_amount', target_order.get('total_executed_size', 0)))),
                    "remaining_size": safe_float(target_order.get('remaining_size', 0)),
                    "price": safe_float(target_order.get('price', 0)),
                    # FIX (Phantom Profit): Add average fill price for entry tracking
                    "avg_price": safe_float(target_order.get('avg_price', target_order.get('average_price', target_order.get('price', 0)))),
                    "average_price": safe_float(target_order.get('avg_price', target_order.get('average_price', target_order.get('price', 0)))),
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
            
            # Resolve both identifiers (API uses `market_id`, but some datasets differ).
            market = self.market_info.get(symbol) or {}
            market_id_val = market.get("market_id") if market.get("market_id") is not None else market.get("marketId")
            market_index_val = market.get("market_index") if market.get("market_index") is not None else market.get("marketIndex")
            if market_id_val is None and market_index_val is None:
                market_id_val = market.get("id") if market.get("id") is not None else market.get("i")

            if market_id_val is None and market_index_val is None:
                logger.debug(f"[LIGHTER] No market_id/index found for {symbol}")
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
            def _build_params(market_id_for_api: int) -> dict:
                p = {
                    "account_index": acc_idx,
                    "market_id": int(market_id_for_api),
                    "limit": limit,
                }
                if auth_token:
                    p["auth"] = auth_token
                return p

            # Primary attempt: use market_id if available, else fallback.
            candidate_market_ids = []
            if market_id_val is not None:
                candidate_market_ids.append(int(market_id_val))
            if market_index_val is not None:
                candidate_market_ids.append(int(market_index_val))
            # De-dup
            candidate_market_ids = list(dict.fromkeys(candidate_market_ids))

            resp = None
            for mid in candidate_market_ids:
                resp = await self._rest_get(
                    "/api/v1/accountInactiveOrders",
                    params=_build_params(mid),
                    force=force,
                )
                if resp:
                    break
            
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

    async def fetch_fee_schedule(self) -> Tuple[Decimal, Decimal]:
        """New Interface: fetch_fee_schedule returning Tuple[Decimal, Decimal]."""
        res = await self._fetch_fee_schedule_internal()
        if res:
            return safe_decimal(res[0]), safe_decimal(res[1])
        return safe_decimal(config.MAKER_FEE_LIGHTER), safe_decimal(config.TAKER_FEE_LIGHTER)

    async def _fetch_fee_schedule_internal(self) -> Optional[Tuple[float, float]]:
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
    
    # ═══════════════════════════════════════════════════════════════
    # Lighter Stream Client Methods (Real-time WebSocket Updates)
    # ═══════════════════════════════════════════════════════════════
    
    async def initialize_stream_client(self, symbols: Optional[List[str]] = None) -> None:
        """
        Initialize and start the Lighter Stream Client for real-time updates.
        
        Subscribes to:
        - Orderbooks (for specified symbols, better price data)
        - Public trades (for specified symbols, trade analysis)
        - Funding rates (if available)
        
        Args:
            symbols: List of symbols to subscribe to for orderbooks and trades.
                    If None, uses all common symbols from market_info.
        """
        stream_url = self._ws_url
        if not stream_url:
            logger.warning("⚠️ [Lighter Stream] Cannot initialize: Missing stream URL")
            return
        
        # Determine symbols for orderbook/trade streams
        if symbols is None:
            symbols = list(self.market_info.keys())
        
        # Limit to top symbols to avoid too many connections
        priority_symbols = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'TAO-USD', 'CRV-USD']
        stream_symbols = [s for s in priority_symbols if s in symbols]
        stream_symbols.extend([s for s in symbols if s not in stream_symbols][:10])  # Max 15 symbols
        
        try:
            self._stream_client = LighterStreamClient(stream_url=stream_url)
            
            # Subscribe to orderbooks for key symbols
            logger.info(f"📊 [Lighter Stream] Subscribing to orderbooks for {len(stream_symbols[:10])} symbols...")
            for symbol in stream_symbols[:10]:  # Limit to 10
                market_id = self.market_info.get(symbol, {}).get('i')
                if market_id is not None:
                    # Create closure to properly capture symbol and market_id
                    def make_orderbook_handler(sym, m_id):
                        async def handler(data):
                            await self._handle_orderbook_stream_message(data, sym, m_id)
                        return handler
                    
                    await self._stream_client.subscribe_to_orderbooks(
                        message_handler=make_orderbook_handler(symbol, market_id),
                        market_id=market_id
                    )
            
            # Subscribe to public trades for key symbols
            logger.info(f"📈 [Lighter Stream] Subscribing to public trades for {len(stream_symbols[:10])} symbols...")
            for symbol in stream_symbols[:10]:  # Limit to 10
                market_id = self.market_info.get(symbol, {}).get('i')
                if market_id is not None:
                    # Create closure to properly capture symbol and market_id
                    def make_trade_handler(sym, m_id):
                        async def handler(data):
                            await self._handle_trade_stream_message(data, sym, m_id)
                        return handler
                    
                    await self._stream_client.subscribe_to_trades(
                        message_handler=make_trade_handler(symbol, market_id),
                        market_id=market_id
                    )
            
            # Start all streams
            await self._stream_client.start()
            
            # Initialize metrics
            self._stream_metrics['start_time'] = time.time()
            self._stream_metrics['connection_health'] = 'healthy'
            
            # Start health check monitoring
            self._stream_health_check_task = asyncio.create_task(self._stream_health_check_loop())
            
            logger.info(f"✅ [Lighter Stream Client] Initialized and started ({len(stream_symbols[:10])} symbols)")
            
        except Exception as e:
            logger.error(f"❌ [Lighter Stream Client] Initialization failed: {e}")
            self._stream_client = None
    
    async def stop_stream_client(self) -> None:
        """Stop the Lighter Stream Client"""
        # Stop health check task
        if self._stream_health_check_task:
            self._stream_health_check_task.cancel()
            try:
                await self._stream_health_check_task
            except asyncio.CancelledError:
                pass
            self._stream_health_check_task = None
        
        if self._stream_client:
            try:
                await self._stream_client.stop()
                logger.info("🛑 [Lighter Stream Client] Stopped")
            except Exception as e:
                logger.error(f"❌ [Lighter Stream Client] Stop error: {e}")
            finally:
                self._stream_client = None
    
    async def _handle_orderbook_stream_message(self, data: Dict[str, Any], symbol: str, market_id: int) -> None:
        """
        Handle orderbook updates from stream.
        
        Updates orderbook_cache with real-time orderbook data.
        """
        try:
            self._stream_metrics['orderbook_updates'] += 1
            self._stream_metrics['last_update_time'] = time.time()
            
            # Extract bids and asks
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            msg_type = data.get("type", "")
            
            # Parse bids and asks
            parsed_bids = []
            for b in bids:
                try:
                    if isinstance(b, dict):
                        price = safe_float(b.get("price"), 0.0)
                        size = safe_float(b.get("amount") or b.get("remaining_base_amount"), 0.0)
                    else:
                        price = safe_float(b[0], 0.0)
                        size = safe_float(b[1], 0.0)
                    
                    if price > 0 and size > 0:
                        parsed_bids.append([price, size])
                except (ValueError, IndexError, TypeError):
                    continue
            
            parsed_asks = []
            for a in asks:
                try:
                    if isinstance(a, dict):
                        price = safe_float(a.get("price"), 0.0)
                        size = safe_float(a.get("amount") or a.get("remaining_base_amount"), 0.0)
                    else:
                        price = safe_float(a[0], 0.0)
                        size = safe_float(a[1], 0.0)
                    
                    if price > 0 and size > 0:
                        parsed_asks.append([price, size])
                except (ValueError, IndexError, TypeError):
                    continue
            
            # Sort
            parsed_bids.sort(key=lambda x: x[0], reverse=True)
            parsed_asks.sort(key=lambda x: x[0])
            
            # Extract server timestamp/sequence_id from WS payload for proper ordering
            server_timestamp = data.get("timestamp") or data.get("ts") or data.get("time")
            sequence_id = data.get("sequence_id") or data.get("seq") or data.get("sequence")
            
            # Convert server_timestamp to milliseconds if needed
            if server_timestamp and server_timestamp < 1e12:
                server_timestamp = server_timestamp * 1000
            
            # Call handle_orderbook_snapshot with parsed data and server metadata
            await self.handle_orderbook_snapshot(
                symbol=symbol,
                bids=parsed_bids,
                asks=parsed_asks,
                server_timestamp=server_timestamp,
                sequence_id=sequence_id
            )
                    
        except Exception as e:
            logger.error(f"[Lighter Stream] Error handling orderbook message for {symbol}: {e}")
    
    async def _handle_trade_stream_message(self, data: Dict[str, Any], symbol: str, market_id: int) -> None:
        """
        Handle public trade updates from stream.
        
        Useful for:
        - Trade analysis
        - Volume tracking
        - Price discovery
        """
        try:
            self._stream_metrics['trade_updates'] += 1
            self._stream_metrics['last_update_time'] = time.time()
            
            # Extract trade data
            # Lighter trade format may vary - handle multiple formats
            price = safe_float(
                data.get("price") or 
                data.get("p") or 
                data.get("execution_price") or 
                data.get("exec_price"),
                0.0
            )
            size = safe_float(
                data.get("size") or 
                data.get("q") or 
                data.get("quantity") or 
                data.get("amount") or
                data.get("base_amount"),
                0.0
            )
            side = data.get("side") or data.get("direction") or ""
            timestamp = data.get("timestamp") or data.get("ts") or int(time.time() * 1000)
            
            if price > 0 and size > 0:
                # Update price cache with latest trade price
                self._price_cache[symbol] = price
                self._price_cache_time[symbol] = time.time()
                self.price_cache[symbol] = price
                
                # Store in trade cache for analysis
                if symbol not in self._trade_cache:
                    self._trade_cache[symbol] = []
                
                trade_entry = {
                    'price': price,
                    'size': size,
                    'side': side,
                    'timestamp': timestamp,
                    'symbol': symbol
                }
                
                self._trade_cache[symbol].append(trade_entry)
                
                # Keep only last 100 trades per symbol
                if len(self._trade_cache[symbol]) > 100:
                    self._trade_cache[symbol] = self._trade_cache[symbol][-100:]
                
                # Log occasional trades (1% sampling)
                if random.random() < 0.01:
                    logger.debug(f"📈 [Lighter Stream] Trade: {symbol} {side} {size:.6f} @ ${price:.2f}")
            
        except Exception as e:
            logger.error(f"[Lighter Stream] Error handling trade message for {symbol}: {e}")
    
    async def _stream_health_check_loop(self) -> None:
        """
        Background task to monitor stream health and metrics.
        
        Checks:
        - Connection status
        - Message rates
        - Last update times
        - Reconnect counts
        """
        while self._stream_client is not None:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                if not self._stream_client:
                    break
                
                # Get connection metrics
                client_metrics = self._stream_client.get_metrics()
                is_connected = self._stream_client.is_connected()
                
                # Calculate message rates
                time_since_start = time.time() - (self._stream_metrics.get('start_time', time.time()))
                if time_since_start > 0:
                    orderbook_rate = self._stream_metrics['orderbook_updates'] / time_since_start
                    trade_rate = self._stream_metrics['trade_updates'] / time_since_start
                    funding_rate = self._stream_metrics['funding_updates'] / time_since_start
                else:
                    orderbook_rate = trade_rate = funding_rate = 0.0
                
                # Check health
                time_since_last_update = time.time() - self._stream_metrics.get('last_update_time', time.time())
                
                if not is_connected:
                    health = 'disconnected'
                elif time_since_last_update > 120:  # No updates for 2 minutes
                    health = 'stale'
                elif time_since_last_update > 60:  # No updates for 1 minute
                    health = 'degraded'
                else:
                    health = 'healthy'
                
                self._stream_metrics['connection_health'] = health
                self._stream_metrics['is_connected'] = is_connected
                self._stream_metrics['message_rates'] = {
                    'orderbook': orderbook_rate,
                    'trade': trade_rate,
                    'funding': funding_rate
                }
                self._stream_metrics['time_since_last_update'] = time_since_last_update
                
                # Log health status periodically
                if random.random() < 0.1:  # 10% chance to log
                    logger.info(
                        f"📊 [Lighter Stream Health] Status: {health} | "
                        f"Connected: {is_connected} | "
                        f"Rates: OB={orderbook_rate:.1f}/min, T={trade_rate:.1f}/min, "
                        f"F={funding_rate:.1f}/min | "
                        f"Last update: {time_since_last_update:.1f}s ago"
                    )
                
                # Warn if unhealthy
                if health in ['stale', 'disconnected']:
                    logger.warning(f"⚠️ [Lighter Stream] Health check: {health} (last update: {time_since_last_update:.1f}s ago)")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Lighter Stream] Health check error: {e}")
                await asyncio.sleep(30)
    
    def get_stream_metrics(self) -> Optional[Dict[str, Any]]:
        """Get comprehensive stream metrics"""
        if not self._stream_client:
            return None
        
        client_metrics = self._stream_client.get_metrics()
        
        return {
            **self._stream_metrics,
            'client_metrics': client_metrics,
            'uptime_seconds': time.time() - self._stream_metrics.get('start_time', time.time())
        }
    
    def is_stream_connected(self) -> bool:
        """Check if stream client is connected"""
        if self._stream_client:
            return self._stream_client.is_connected()
        return False
    
    def get_stream_health(self) -> str:
        """Get current stream health status"""
        return self._stream_metrics.get('connection_health', 'unknown')

    def mark_ws_market_stats(self) -> None:
        """Record a market_stats update timestamp (used to gate REST polling)."""
        now = time.time()
        self._ws_market_stats_last_ts = now
        if not self._ws_market_stats_ready_event.is_set():
            self._ws_market_stats_ready_at = now
            self._ws_market_stats_ready_event.set()

    async def wait_for_ws_market_stats_ready(self, timeout: float = 10.0) -> bool:
        if self._ws_market_stats_ready_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._ws_market_stats_ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _ws_market_stats_is_fresh(self) -> bool:
        """Check if WS market_stats updates are fresh enough to skip REST polling."""
        if not getattr(config, "LIGHTER_SKIP_REST_POLL_WHEN_WS_HEALTHY", True):
            return False
        stale_seconds = float(getattr(config, "LIGHTER_WS_MARKET_STATS_STALE_SECONDS", 15.0))
        if self._ws_market_stats_last_ts <= 0:
            return False
        return (time.time() - self._ws_market_stats_last_ts) <= stale_seconds

    async def _rest_price_poller(self, interval: float = 2.0):
        """REST-basiertes Preis-Polling als WebSocket-Ersatz"""
        while True:
            try:
                if self._ws_market_stats_is_fresh():
                    await asyncio.sleep(interval)
                    continue

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
                if self._ws_market_stats_is_fresh():
                    await asyncio.sleep(interval)
                    continue

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
            
            # New SDK API: api_private_keys is Dict[int, str] instead of single private_key
            api_key_index = self._resolved_api_key_index or 0
            api_private_keys = {api_key_index: priv_key}
            
            self._signer = SaferSignerClient(
                url=self._get_base_url(),
                account_index=self._resolved_account_index,
                api_private_keys=api_private_keys,
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
                                    # Die Rate ist bereits STÜNDLICH (Lighter verwendet 1-hour funding intervals)
                                    hourly_rate = safe_float(rate, 0.0)
                                    self.funding_cache[symbol] = hourly_rate
                                    self._funding_cache[symbol] = hourly_rate
                        
                        logger.debug(f"Lighter: Loaded {len(self.funding_cache)} funding rates")
                        return True
        except Exception as e:
            logger.error(f"Failed to load Lighter funding rates: {e}")
        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # NEW: Direct PnL API - GET /api/v1/pnl
    # Returns exchange-calculated PnL data for verification
    # ═══════════════════════════════════════════════════════════════════════════
    async def fetch_pnl_from_api(
        self,
        resolution: str = "1h",
        count_back: int = 24,
        ignore_transfers: bool = False
    ) -> Optional[dict]:
        """
        Fetch account PnL data directly from Lighter API.
        
        API: GET /api/v1/pnl
        
        This provides exchange-calculated PnL which can be used
        to verify bot's own PnL calculations.
        
        Args:
            resolution: Time interval (1m, 5m, 15m, 1h, 4h, 1d)
            count_back: Number of periods to fetch
            ignore_transfers: If true, exclude deposit/withdrawal impact
            
        Returns:
            Dict with PnL data or None if failed:
            {
                "entries": [{"timestamp": int, "pnl": float, ...}],
                "total_pnl": float,
                ...
            }
        """
        try:
            if self._resolved_account_index is None:
                await self._resolve_account_index()
            
            if self._resolved_account_index is None:
                logger.debug("Lighter PnL API: No account index resolved")
                return None
            
            # Build auth token (required for private endpoint)
            signer = await self._get_signer()
            auth_result = signer.create_auth_token_with_expiry(600)  # 10 min expiry
            
            # Handle tuple return: (auth_str, error_str)
            if isinstance(auth_result, tuple):
                auth_token, auth_error = auth_result
                if auth_error:
                    logger.warning(f"Lighter PnL API: Auth token error: {auth_error}")
                    return None
            else:
                auth_token = auth_result
            
            if not auth_token:
                logger.warning("Lighter PnL API: Failed to create auth token")
                return None
            
            now_ms = int(time.time() * 1000)
            # Calculate start time based on resolution and count_back
            resolution_seconds = {
                "1m": 60, "5m": 300, "15m": 900,
                "1h": 3600, "4h": 14400, "1d": 86400
            }.get(resolution, 3600)
            start_timestamp = now_ms - (count_back * resolution_seconds * 1000)
            
            base_url = self._get_base_url()
            url = (
                f"{base_url}/api/v1/pnl"
                f"?by=index"
                f"&value={self._resolved_account_index}"
                f"&resolution={resolution}"
                f"&start_timestamp={start_timestamp}"
                f"&end_timestamp={now_ms}"
                f"&count_back={count_back}"
                f"&ignore_transfers={str(ignore_transfers).lower()}"
            )
            
            headers = {
                "Accept": "application/json",
                "Authorization": auth_token
            }
            
            await self.rate_limiter.acquire()
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.rate_limiter.on_success()
                    
                    # Parse response
                    result = {
                        "code": data.get("code", 0),
                        "entries": [],
                        "total_pnl": 0.0
                    }
                    
                    # Extract PnL entries if available
                    if "pnl" in data and isinstance(data["pnl"], list):
                        total = 0.0
                        for entry in data["pnl"]:
                            pnl_value = safe_float(entry.get("pnl", entry.get("value", 0)), 0.0)
                            total += pnl_value
                            result["entries"].append({
                                "timestamp": entry.get("timestamp", 0),
                                "pnl": pnl_value
                            })
                        result["total_pnl"] = total
                    
                    logger.info(
                        f"📊 Lighter PnL API: Fetched {len(result['entries'])} entries, "
                        f"total_pnl=${result['total_pnl']:.4f}"
                    )
                    return result
                    
                elif resp.status == 404:
                    logger.debug("Lighter PnL API: No data found (404)")
                    return {"code": 0, "entries": [], "total_pnl": 0.0}
                else:
                    body = await resp.text()
                    logger.warning(f"Lighter PnL API: HTTP {resp.status} - {body[:200]}")
                    return None
                    
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.warning(f"Lighter PnL API error: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # NEW: Position Funding History API - GET /api/v1/positionFunding
    # Returns detailed funding payments per position
    # ═══════════════════════════════════════════════════════════════════════════
    async def fetch_position_funding(
        self,
        market_id: Optional[int] = None,
        side: str = "all",
        limit: int = 100,
        cursor: Optional[str] = None,
        max_pages: int = 10
    ) -> List[dict]:
        """
        Fetch position funding history from Lighter API.
        
        API: GET /api/v1/positionFunding
        
        This gives detailed funding payment history per position,
        more accurate than just total_funding_paid_out from position object.
        
        Args:
            market_id: Optional filter by market (default: 255 = all)
            side: Filter by position side ("long", "short", "all")
            limit: Max records to return (1-100)
            
        Returns:
            List of funding payment records:
            [
                {
                    "market_id": int,
                    "timestamp": int,
                    "funding_id": int,
                    "change": float,        # Amount (+ = paid, - = received)
                    "rate": float,          # Funding rate at the time
                    "position_size": float, # Position size
                    "position_side": str    # "long" or "short"
                }
            ]
        """
        try:
            if self._resolved_account_index is None:
                await self._resolve_account_index()
            
            if self._resolved_account_index is None:
                logger.debug("Lighter Position Funding API: No account index resolved")
                return []
            
            # Build auth token (required for private endpoint)
            signer = await self._get_signer()
            auth_result = signer.create_auth_token_with_expiry(600)  # 10 min expiry
            
            # Handle tuple return: (auth_str, error_str)
            if isinstance(auth_result, tuple):
                auth_token, auth_error = auth_result
                if auth_error:
                    logger.warning(f"Lighter Position Funding API: Auth token error: {auth_error}")
                    return []
            else:
                auth_token = auth_result
            
            if not auth_token:
                logger.warning("Lighter Position Funding API: Failed to create auth token")
                return []
            
            base_url = self._get_base_url()
            headers = {
                "Accept": "application/json",
                "Authorization": auth_token
            }

            all_fundings: List[dict] = []
            next_cursor: Optional[str] = cursor
            pages = 0
            total_received = 0.0

            session = await self._get_session()
            while pages < max_pages:
                qs = (
                    f"account_index={self._resolved_account_index}"
                    f"&market_id={market_id if market_id is not None else 255}"
                    f"&side={side}"
                    f"&limit={min(limit, 100)}"
                )
                if next_cursor:
                    qs += f"&cursor={next_cursor}"

                url = f"{base_url}/api/v1/positionFunding?{qs}"
                
                logger.info(f"🔍 [LIGHTER_FUNDING_DEBUG] Requesting: {url}")

                await self.rate_limiter.acquire()
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.rate_limiter.on_success()
                        
                        # LOG RAW DATA (Truncated)
                        raw_str = str(data)
                        if len(raw_str) > 2000:
                            raw_str = raw_str[:2000] + "... [TRUNCATED]"
                        logger.debug(f"🔍 [LIGHTER_FUNDING_DEBUG] Raw Response: {raw_str}")

                        raw_fundings = data.get("fundings", data.get("position_fundings", data.get("data", [])))
                        if not isinstance(raw_fundings, list) or len(raw_fundings) == 0:
                            logger.debug(f"🔍 [LIGHTER_FUNDING_DEBUG] No fundings in response list.")
                            break

                        for f in raw_fundings:
                            # change: positive = received, negative = paid (Balance Change)
                            # AUDIT FIX: Lighter API returns Balance Change directly.
                            # Positive = Profit (Received), Negative = Cost (Paid).
                            # No inversion needed.
                            change = safe_float(f.get("change", 0), 0.0)
                            funding_received = change
                            total_received += funding_received
                            
                            # Rate Unit Display Fix:
                            # API returns Rate as DECIMAL (e.g. 0.000012).
                            # CSV Export shows Rate as PERCENT (e.g. 0.0012%).
                            # We log both to avoid confusion.
                            rate_val = safe_float(f.get("rate", 0), 0.0)
                            rate_pct = rate_val * 100.0
                            
                            logger.debug(
                                f"  👉 [LIGHTER_PAYMENT] {f.get('symbol')} Time={f.get('timestamp')} "
                                f"Change={change:.6f} -> Received={funding_received:.6f} "
                                f"Rate={rate_val:.8f} ({rate_pct:.6f}%) Size={f.get('position_size')}"
                            )

                            all_fundings.append({
                                "market_id": f.get("market_id"),
                                "symbol": f.get("symbol", self._market_id_to_symbol(f.get("market_id"))),
                                "timestamp": f.get("timestamp"),
                                "funding_id": f.get("funding_id"),
                                "change": change,
                                "funding_received": funding_received,
                                "rate": safe_float(f.get("rate", 0), 0.0),
                                "position_size": safe_float(f.get("position_size", 0), 0.0),
                                "position_side": f.get("position_side", "unknown")
                            })

                        pages += 1
                        next_cursor = data.get("next_cursor")
                        if not next_cursor:
                            break
                    elif resp.status == 404:
                        logger.info("🔍 [LIGHTER_FUNDING_DEBUG] HTTP 404 (No data found)")
                        break
                    else:
                        body = await resp.text()
                        logger.warning(f"🔍 [LIGHTER_FUNDING_DEBUG] HTTP {resp.status} - {body[:200]}")
                        break

            # Detailed per-payment debug output
            for fp in all_fundings:
                logger.debug(
                    f"🧾 Lighter {fp.get('symbol','UNKNOWN')}: payment ts={fp.get('timestamp')} "
                    f"received={fp.get('funding_received')} rate={fp.get('rate')} "
                    f"side={fp.get('position_side')} size={fp.get('position_size')}"
                )

            if all_fundings:
                # NOTE: This is the raw response total (unfiltered). Callers like
                # get_funding_for_symbol() may apply timestamp filters afterwards.
                logger.debug(
                    f"💵 Lighter Position Funding: {len(all_fundings)} payments ({pages} pages), "
                    f"total_received=${total_received:.6f}"
                )

            return all_fundings
                    
        except asyncio.CancelledError:
            return []
        except Exception as e:
            logger.warning(f"Lighter Position Funding API error: {e}")
            return []

    def _market_id_to_symbol(self, market_id: Optional[int]) -> str:
        """Convert market_id to symbol using cached market_info."""
        if market_id is None:
            return "UNKNOWN"
        for symbol, info in self.market_info.items():
            if info.get("i") == market_id:
                return symbol
        return f"MARKET_{market_id}"

    async def get_funding_for_symbol(
        self,
        symbol: str,
        since_timestamp: Optional[int] = None
    ) -> float:
        """
        Get total funding received for a specific symbol since a timestamp.
        Uses the positionFunding API for accurate data.
        
        Args:
            symbol: Market symbol (e.g., "BTC-USD")
            since_timestamp: Start time in milliseconds (defaults to 24h ago)
            
        Returns:
            Total funding in USD (positive = received, negative = paid)
        """
        try:
            # Get market_id for symbol
            if symbol not in self.market_info:
                await self.load_market_cache()
            
            market_id = self.market_info.get(symbol, {}).get("i")
            
            # Fetch all funding for this market
            fundings = await self.fetch_position_funding(
                market_id=market_id,
                side="all",
                limit=100
            )
            
            def _normalize_to_ms(ts: Any) -> int:
                v = safe_float(ts, 0.0)
                if v <= 0:
                    return 0
                # Lighter PositionFunding timestamps are seconds (example: 1640995200)
                if v >= 1e12:
                    return int(v)
                return int(v * 1000)

            # Filter by timestamp if provided (normalize both sides to ms)
            if since_timestamp:
                since_ms = _normalize_to_ms(since_timestamp)
                pre = len(fundings)
                fundings = [f for f in fundings if _normalize_to_ms(f.get("timestamp")) >= since_ms]
                logger.debug(
                    f"Lighter {symbol}: Filtering fundings since_ts_ms={since_ms} "
                    f"(pre_filter={pre}) -> after_filter={len(fundings)}"
                )
            
            # Sum funding_received (profit-positive)
            total = sum(f.get("funding_received", 0.0) for f in fundings)
            
            if abs(total) > 0.00001 or (getattr(config, "FUNDING_TRACKER_DEBUG", False) and since_timestamp):
                logger.info(f"💵 Lighter {symbol}: Total funding=${total:.6f} from {len(fundings)} payments (since_ts={since_timestamp})")
             
            return total
            
        except Exception as e:
            logger.warning(f"Lighter get_funding_for_symbol error: {e}")
            return 0.0

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
                    # Also preserve BOTH identifiers because some REST endpoints use `market_id`
                    # while others use `market_index` (or `i`).
                    raw_market_id = m.get("market_id")
                    if raw_market_id is None:
                        raw_market_id = m.get("marketId")

                    raw_market_index = m.get("market_index")
                    if raw_market_index is None:
                        raw_market_index = m.get("i")

                    def _parse_int_or_none(v):
                        if v is None:
                            return None
                        try:
                            return int(float(str(v)))
                        except (ValueError, TypeError):
                            return None

                    market_id_int = _parse_int_or_none(raw_market_id)
                    market_index_int = _parse_int_or_none(raw_market_index)

                    # Backwards compatible primary id used throughout the codebase
                    # (historically stored under `i`). Prefer market_id when available.
                    real_id = market_id_int if market_id_int is not None else (market_index_int if market_index_int is not None else -1)
                    
                    if real_id < 0:
                        logger.warning(
                            f"⚠️ Skipping {symbol}: market_id={raw_market_id} (parsed={market_id_int}), "
                            f"market_index={raw_market_index} (parsed={market_index_int}) invalid < 0"
                        )
                        continue

                    # ═══════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Cast ALL market metadata to correct types
                    # This prevents TypeError when comparing values later
                    # ═══════════════════════════════════════════════════════════════
                    self.market_info[symbol] = {
                        "i": real_id,
                        "market_id": market_id_int,
                        "market_index": market_index_int,
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
            
            # ═══════════════════════════════════════════════════════════════
            # B1: Start WebSocket Order Client for low-latency orders
            # Called once after first market cache load
            # ═══════════════════════════════════════════════════════════════
            if hasattr(self, 'ws_order_client') and self._ws_order_enabled:
                if not self.ws_order_client.is_connected:
                    try:
                        connected = await self.ws_order_client.connect()
                        if connected:
                            logger.info("✅ [WS-ORDER] Connected for low-latency order submission")
                        else:
                            logger.warning("⚠️ [WS-ORDER] Failed to connect - using REST fallback")
                    except Exception as e:
                        logger.warning(f"⚠️ [WS-ORDER] Init error: {e} - using REST fallback")
            
            # Start Batch Manager if not already running
            if hasattr(self, 'batch_manager') and not self.batch_manager._running:
                await self.batch_manager.start()

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
            
            # Start Batch Manager
            if hasattr(self, 'batch_manager'):
                await self.batch_manager.start()
            
            # B1: Start WebSocket Order Client for low-latency orders
            if hasattr(self, 'ws_order_client') and self._ws_order_enabled:
                try:
                    connected = await self.ws_order_client.connect()
                    if connected:
                        logger.info("✅ [WS-ORDER] Connected for low-latency order submission")
                    else:
                        logger.warning("⚠️ [WS-ORDER] Failed to connect - using REST fallback")
                except Exception as e:
                    logger.warning(f"⚠️ [WS-ORDER] Init error: {e} - using REST fallback")
            
        except ImportError as e:
            logger.error(f"❌ Lighter SDK import error: {e}")
        except Exception as e:
            logger.error(f"❌ Lighter init error: {e}")

    async def load_funding_rates_and_prices(self, force: bool = False):
        """Lädt Funding Rates UND Preise von der Lighter REST API"""
        if not getattr(config, "LIVE_TRADING", False):
            return

        if not HAVE_LIGHTER_SDK:
            return

        if not force:
            min_interval = float(getattr(config, "LIGHTER_REST_REFRESH_MIN_SECONDS", 60.0))
            if min_interval > 0 and (time.time() - self._rest_refresh_ts) < min_interval:
                logger.debug("Lighter: Skipping REST refresh (min interval)")
                return

        if not force and not self._ws_market_stats_ready_event.is_set():
            grace_seconds = float(getattr(config, "LIGHTER_WS_STARTUP_GRACE_SECONDS", 15.0))
            if (time.time() - self._init_time) < grace_seconds:
                logger.debug("Lighter: Skipping REST refresh (WS not ready, startup grace)")
                return

        if not force and getattr(config, "LIGHTER_SKIP_REST_POLL_WHEN_WS_HEALTHY", False):
            if self._ws_market_stats_is_fresh():
                logger.debug("Lighter: Skipping REST funding/price refresh (WS market_stats fresh)")
                return

        if not force and self.rate_limiter.is_duplicate("LIGHTER:load_funding_rates_and_prices"):
            return

        self._rest_refresh_ts = time.time()

        try:
            signer = await self._get_signer()
            
            # 1.  FUNDING RATES laden
            funding_api = FundingApi(signer.api_client)
            await self.rate_limiter.acquire()
            fd_response = await funding_api.funding_rates()

            if fd_response and fd_response.funding_rates:
                # Ensure market_info is populated before mapping
                if not self.market_info:
                    await self.load_market_cache(force=True)
                
                updated = 0
                for fr in fd_response.funding_rates:
                    market_id = getattr(fr, "market_id", None)
                    rate = getattr(fr, "rate", None)

                    if market_id is None or rate is None:
                        continue

                    raw_rate = safe_float(rate, 0.0)
                    # REST API gibt 8-Stunden Rate zurück - teile durch 8 für stündliche Rate
                    hourly_rate = raw_rate / 8.0

                    for symbol, data in self.market_info.items():
                        if data.get("i") == market_id:
                            self.funding_cache[symbol] = hourly_rate
                            self._funding_cache[symbol] = hourly_rate
                            updated += 1
                            break

                self.rate_limiter.on_success()
                if updated > 0:
                    logger.debug(f"Lighter: Loaded {updated} funding rates from REST API")
                else:
                    logger.warning(f"⚠️ Lighter: No funding rates matched market_info")

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
                # Use lock to prevent race conditions with WS updates
                # Only update if WS is unhealthy or cache is stale (>2s)
                # ═══════════════════════════════════════════════════════════════
                async with self._orderbook_cache_lock:
                    # Check WS health
                    ws_healthy = False
                    if self._stream_client:
                        try:
                            ws_healthy = self._stream_client.is_connected()
                        except Exception:
                            pass
                    
                    # Check existing cache age
                    existing_cache = self.orderbook_cache.get(symbol)
                    existing_ts = existing_cache.get("timestamp", 0) if existing_cache else 0
                    if existing_ts > 1e12:
                        existing_ts = existing_ts / 1000
                    existing_age = time.time() - existing_ts
                    
                    # Only update if WS unhealthy or cache stale
                    if not ws_healthy or existing_age > 2.0:
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

    async def get_orderbook_mid_price(self, symbol: str) -> Optional[float]:
        """
        Get the mid price from the orderbook (Best Bid + Best Ask) / 2.
        This is more accurate than Mark Price for arbitrage/PnL calculations.
        """
        try:
            # Fetch top of book (limit=5 is enough)
            ob = await self.fetch_orderbook(symbol, limit=5)
            
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])
            
            if not bids or not asks:
                return None
                
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            
            if best_bid <= 0 or best_ask <= 0:
                return None
                
            return (best_bid + best_ask) / 2.0
            
        except Exception as e:
            logger.error(f"Error calculating mid price for {symbol}: {e}")
            return None
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "rate limit" in err_str:
                self.rate_limiter.penalize_429()
            logger.debug(f"Lighter orderbook {symbol}: {e}")

        return self.orderbook_cache.get(symbol, {"bids": [], "asks": [], "timestamp": 0})

    async def handle_orderbook_snapshot(self, symbol: str, bids: List, asks: List, server_timestamp: Optional[int] = None, sequence_id: Optional[int] = None):
        """
        Process incoming orderbook snapshot from Lighter WebSocket.
        
        Updates orderbook_cache with real-time orderbook data in a thread-safe manner.
        Uses server timestamps/sequence_id for proper ordering.
        """
        try:
            # Parse and validate bids/asks
            parsed_bids = []
            for b in bids:
                try:
                    if isinstance(b, dict):
                        price = safe_float(b.get("price"), 0.0)
                        size = safe_float(b.get("amount") or b.get("remaining_base_amount"), 0.0)
                    else:
                        price = safe_float(b[0], 0.0)
                        size = safe_float(b[1], 0.0)
                    
                    if price > 0 and size > 0:
                        parsed_bids.append([price, size])
                except (ValueError, IndexError, TypeError):
                    continue
            
            parsed_asks = []
            for a in asks:
                try:
                    if isinstance(a, dict):
                        price = safe_float(a.get("price"), 0.0)
                        size = safe_float(a.get("amount") or a.get("remaining_base_amount"), 0.0)
                    else:
                        price = safe_float(a[0], 0.0)
                        size = safe_float(a[1], 0.0)
                    
                    if price > 0 and size > 0:
                        parsed_asks.append([price, size])
                except (ValueError, IndexError, TypeError):
                    continue
            
            # Sort
            parsed_bids.sort(key=lambda x: x[0], reverse=True)
            parsed_asks.sort(key=lambda x: x[0])
            
            # Validate: Check for crossed books
            if parsed_bids and parsed_asks:
                best_bid = parsed_bids[0][0]
                best_ask = parsed_asks[0][0]
                if best_ask <= best_bid:
                    logger.warning(f"⚠️ [Lighter WS] Crossed orderbook detected for {symbol}: ask={best_ask} <= bid={best_bid} - skipping update")
                    return
            
            # Use server timestamp if available, else local time
            timestamp_ms = server_timestamp if server_timestamp is not None else int(time.time() * 1000)
            
            # Thread-safe cache update
            async with self._orderbook_cache_lock:
                # Check if WS is healthy (if stream client exists)
                ws_healthy = False
                if self._stream_client:
                    try:
                        ws_healthy = self._stream_client.is_connected()
                    except Exception:
                        pass
                
                # Check existing cache timestamp for WS vs REST priority
                existing_cache = self.orderbook_cache.get(symbol)
                existing_ts = existing_cache.get("timestamp", 0) if existing_cache else 0
                # Convert ms to s if needed for comparison
                if existing_ts > 1e12:
                    existing_ts = existing_ts / 1000
                existing_age = time.time() - existing_ts
                
                # Only update if:
                # 1. WS is healthy (fresh data), OR
                # 2. Existing cache is stale (>2s old), OR
                # 3. No existing cache
                if ws_healthy or existing_age > 2.0 or not existing_cache:
                    cache_entry = {
                        "bids": parsed_bids,
                        "asks": parsed_asks,
                        "timestamp": timestamp_ms,
                        "symbol": symbol
                    }
                    
                    self.orderbook_cache[symbol] = cache_entry
                    self._orderbook_cache[symbol] = cache_entry
                    self._orderbook_cache_time[symbol] = time.time()
                    
                    # Update price cache from best bid/ask
                    if parsed_bids and parsed_asks:
                        try:
                            best_bid = parsed_bids[0][0]
                            best_ask = parsed_asks[0][0]
                            if best_bid > 0 and best_ask > 0:
                                mid_price = (best_bid + best_ask) / 2.0
                                self._price_cache[symbol] = mid_price
                                self._price_cache_time[symbol] = time.time()
                                self.price_cache[symbol] = mid_price
                        except (ValueError, IndexError):
                            pass
                    
                    # Trigger price update event
                    if hasattr(self, "price_update_event") and self.price_update_event:
                        self.price_update_event.set()
                    
                    # Log occasional updates (0.5% sampling to reduce noise)
                    if random.random() < 0.005:
                        logger.debug(f"📊 [Lighter WS] Orderbook snapshot {symbol}: {len(parsed_bids)}b/{len(parsed_asks)}a")
                else:
                    # WS not healthy and cache is fresh - skip update to avoid overwriting REST data
                    logger.debug(f"[Lighter WS] Skipping orderbook update for {symbol} (WS unhealthy, cache fresh)")
                    
        except Exception as e:
            logger.error(f"[Lighter WS] Error in handle_orderbook_snapshot for {symbol}: {e}")

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

    def _fetch_funding_rate_legacy(self, symbol: str) -> Optional[float]:
        """
        Legacy funding rate fetch - returns hourly rate as float.
        (Internal use for transition only)
        
        FIX (2025-12-22): Simplified - rate is already stored as hourly decimal fraction.
        """
        if symbol in self._funding_cache:
            rate = self._funding_cache[symbol]
        else:
            rate = self.funding_cache.get(symbol)

        if rate is None:
            return None

        try:
            rate_float = float(rate)
        except (ValueError, TypeError):
            return None

        # Rate is already stored as hourly decimal fraction
        # e.g. 0.0001 = 0.01% per hour
        return rate_float

    async def fetch_mark_price(self, symbol: str) -> Decimal:
        """Mark Price aus Cache - always returns Decimal."""
        return self.fetch_mark_price_sync(symbol)

    def fetch_mark_price_sync(self, symbol: str) -> Decimal:
        """Synchronous version of fetch_mark_price."""
        # Check primary cache first
        if symbol in self._price_cache:
            price = self._price_cache[symbol]
            try:
                price_float = float(str(price)) if price is not None else 0.0
                if price_float > 0:
                    return safe_decimal(price_float)
            except (ValueError, TypeError):
                pass
        
        # Fallback to secondary cache
        if symbol in self.price_cache:
            price = self.price_cache[symbol]
            try:
                price_float = float(str(price)) if price is not None else 0.0
                if price_float > 0:
                    return safe_decimal(price_float)
            except (ValueError, TypeError):
                pass
        
        return Decimal(0)

    async def fetch_funding_rate(self, symbol: str) -> Decimal:
        """Interface implementation of fetch_funding_rate."""
        # Simple cache lookup
        return self.fetch_funding_rate_sync(symbol)

    def fetch_funding_rate_sync(self, symbol: str) -> Decimal:
        """
        Funding Rate: Gibt IMMER stündliche Rate als DEZIMAL-FRAKTION zurück.

        ═══════════════════════════════════════════════════════════════════════
        FIX (2025-12-22): Korrigierte Interpretation der Lighter Funding Rate
        ═══════════════════════════════════════════════════════════════════════
        
        Laut loris.tools Doku und Lighter API:
        - Lighter verwendet 1-HOUR FUNDING INTERVALS (wie Hyperliquid, Extended)
        - Die API liefert die STÜNDLICHE Rate direkt (NICHT 8-Stunden!)
        - loris.tools multipliziert Lighter Rates mit 8 um sie mit 8h-Exchanges zu vergleichen
        
        Die Rate ist eine DEZIMAL-FRAKTION (nicht Prozent!):
        - Beispiel: rate=0.00015 = 0.015%/h 
        - APY = 0.00015 * 24 * 365 = 1.314 = 131.4%
        
        Der Cache enthält direkt die stündliche Dezimal-Fraktion.
        ═══════════════════════════════════════════════════════════════════════
        """
        raw_rate = Decimal(0)
        if symbol in self._funding_cache:
            raw_rate = safe_decimal(self._funding_cache[symbol])
        elif symbol in self.funding_cache:
            raw_rate = safe_decimal(self.funding_cache[symbol])

        if raw_rate == 0:
            return Decimal(0)

        # Die Rate ist bereits als stündliche Dezimal-Fraktion im Cache gespeichert
        # z.B. 0.00015 = 0.015% pro Stunde
        # APY = 0.00015 * 24 * 365 = 1.314 = 131.4%
        return raw_rate

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: Decimal,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> OrderResult:
        """Interface implementation of place_order."""
        # Mapping to existing open_live_position
        # Note: LighterAdapter.open_live_position uses notional_usd or amount
        success, order_id = await self.open_live_position(
            symbol=symbol,
            side=side,
            notional_usd=0, # Will use amount instead
            price=float(price) if price else None,
            reduce_only=reduce_only,
            post_only=post_only,
            amount=float(size)
        )
        return OrderResult(
            success=success,
            order_id=order_id,
            error_message=None if success else "Order placement failed"
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Interface implementation of cancel_order."""
        try:
            signer = await self._get_signer()
            # Note: order_id could be tx_hash or numeric ID
            # SaferSignerClient.cancel_order handles both
            tx, resp, err = await signer.cancel_order(order_id=order_id)
            return err is None
        except Exception as e:
            logger.error(f"❌ [Lighter] cancel_order error: {e}")
            return False

    async def get_order_status(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Interface implementation of get_order_status."""
        order = await self.get_order(order_id, symbol)
        if order:
            return order
        return {}

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

    async def get_available_balance(self) -> Decimal:
        """Interface implementation of get_available_balance."""
        balance = await self.get_real_available_balance()
        return safe_decimal(balance)

    async def fetch_fee_schedule(self) -> Tuple[Decimal, Decimal]:
        """Interface implementation of fetch_fee_schedule."""
        # Note: Lighter fee schedule can be dynamic or static
        # Taker: 0.02% (0.0002), Maker: 0.0% (0.0000)
        # Using config or defaults
        maker = safe_decimal(getattr(config, "MAKER_FEE_LIGHTER", 0.0))
        taker = safe_decimal(getattr(config, "TAKER_FEE_LIGHTER", 0.0002))
        return maker, taker

    async def aclose(self):
        """Interface implementation of aclose."""
        logger.info(f"Stopping {self.name} adapter...")
        try:
            # Stop stream client
            if hasattr(self, 'stop_stream_client'):
                await self.stop_stream_client()
            
            # Close WS order client
            if hasattr(self, 'ws_order_client') and self.ws_order_client:
                await self.ws_order_client.close()
                
            # Close session via BaseAdapter
            await super().aclose()
        except Exception as e:
            logger.error(f"Error during {self.name} adapter shutdown: {e}")

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

    async def fetch_open_positions(self) -> List[Position]:
        """Fetch open positions from Lighter and return as Position objects."""
        raw_positions = await self._fetch_open_positions_internal()
        return self._to_position_objects(raw_positions)

    def _to_position_objects(self, raw_positions: List[dict]) -> List[Position]:
        """Convert raw position dicts to Position objects."""
        positions = []
        for p in (raw_positions or []):
            try:
                positions.append(Position(
                    symbol=p.get('symbol', ''),
                    side='LONG' if safe_float(p.get('size', 0)) > 0 else 'SHORT',
                    size=safe_decimal(p.get('size', 0)),
                    entry_price=safe_decimal(p.get('avg_entry_price') or p.get('entry_price') or 0),
                    mark_price=safe_decimal(self.fetch_mark_price_sync(p.get('symbol', ''))),
                    unrealized_pnl=safe_decimal(p.get('unrealized_pnl', 0)),
                    leverage=safe_decimal(p.get('leverage', 1)),
                    exchange=self.name
                ))
            except Exception as e:
                logger.error(f"Error converting Lighter position: {e}")
        return positions

    async def _fetch_open_positions_internal(self) -> List[dict]:
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
                self._positions_cache_time = time.time()
                return []

            account = response.accounts[0]

            if not hasattr(account, "positions") or not account.positions:
                self._positions_cache = []
                self._positions_cache_time = time.time()
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
                raw_qty = safe_float(position_qty, 0.0)
                
                # FIX: Robust sign handling
                if sign_int != 0:
                    # Trust explicit sign field (1=Long, -1=Short)
                    multiplier = sign_int
                    size = abs(raw_qty) * multiplier
                else:
                    # Fallback: Trust sign of position_qty if sign field is 0/missing
                    size = raw_qty

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
            ghost_positions_for_callbacks = []
            
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
                        
                        # Ghost positions should ONLY be used for fill-detection callbacks.
                        # They must NOT be returned as real open positions, otherwise
                        # reconciliation / trade management can treat them as real exposure.
                        ghost_positions_for_callbacks.append({
                            "symbol": sym,
                            "size": 0.0001,  # Dummy non-zero size
                            "is_ghost": True,
                        })
                        api_symbols.add(sym)  # Prevent duplicates if multiple pending

            # Cache for deduplication
            self._positions_cache = positions
            self._positions_cache_time = time.time()
            
            # ═══════════════════════════════════════════════════════════════
            # FIXED: Trigger position callbacks for Ghost-Fill detection
            # This enables ParallelExecutionManager to detect fills faster
            # ═══════════════════════════════════════════════════════════════
            await self._trigger_position_callbacks(positions + ghost_positions_for_callbacks)
            
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

    def _build_grouped_order_req(
        self,
        market_id: int,
        base_amount: int,
        price_int: int,
        is_ask: bool,
        order_type: int,
        time_in_force: int,
        reduce_only: bool,
        trigger_price: int,
        order_expiry: int,
    ):
        if not CreateOrderTxReq:
            raise RuntimeError("CreateOrderTxReq not available")

        return CreateOrderTxReq(
            MarketIndex=int(market_id),
            ClientOrderIndex=0,
            BaseAmount=int(base_amount),
            Price=int(price_int),
            IsAsk=1 if is_ask else 0,
            Type=int(order_type),
            TimeInForce=int(time_in_force),
            ReduceOnly=1 if reduce_only else 0,
            TriggerPrice=int(trigger_price),
            OrderExpiry=int(order_expiry),
        )

    @rate_limited(Exchange.LIGHTER)
    async def place_grouped_orders(
        self,
        grouping_type: int,
        orders: List[CreateOrderTxReq],
    ) -> Dict[str, Any]:
        """
        Place grouped orders (OTO/OCO/OTOCO) in a single transaction.
        Returns dict with success, hash, and error fields.
        """
        try:
            if not HAVE_LIGHTER_SDK or not CreateOrderTxReq:
                return {
                    "success": False,
                    "hash": "",
                    "error": "Lighter SDK not installed or missing CreateOrderTxReq",
                }

            signer = await self._get_signer()
            async with self.order_lock:
                nonce = await self._get_next_nonce()
                if nonce is None:
                    return {
                        "success": False,
                        "hash": "",
                        "error": "Failed to get nonce",
                    }

                # ═══════════════════════════════════════════════════════════════
                # Note: Grouped orders (OTO/OCO/OTOCO) may not be supported via WebSocket
                # For now, use REST API. If WebSocket support is added later, integrate here.
                # ═══════════════════════════════════════════════════════════════
                tx, resp, err = await signer.create_grouped_orders(
                    grouping_type=grouping_type,
                    orders=orders,
                    nonce=int(nonce),
                    api_key_index=int(self._resolved_api_key_index),
                )

            if err:
                return {"success": False, "hash": "", "error": str(err)}

            tx_hash = getattr(resp, "tx_hash", "") if resp else ""
            return {"success": True, "hash": tx_hash or "", "error": None, "tx": tx}

        except Exception as e:
            logger.error(f"[Lighter Grouped Orders] Exception: {e}", exc_info=True)
            return {"success": False, "hash": "", "error": str(e)}

    @rate_limited(Exchange.LIGHTER)
    async def place_twap_order(
        self,
        symbol: str,
        side: str,
        size: Decimal,
        price: Decimal,
        order_expiry_ms: Optional[int] = None,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Place a TWAP order. TWAP orders execute over time and do not support SL/TP in the same batch.
        """
        try:
            if not HAVE_LIGHTER_SDK:
                return {"success": False, "hash": "", "error": "Lighter SDK not installed"}

            market_info = self.market_info.get(symbol, {})
            market_id = safe_int(market_info.get("i"), -1)
            if market_id < 0:
                return {"success": False, "hash": "", "error": f"Invalid market_id for {symbol}"}

            base_amount, price_int = self._scale_amounts(symbol, size, price, side)

            signer = await self._get_signer()
            tif = SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            order_type = SignerClient.ORDER_TYPE_TWAP
            expiry = order_expiry_ms or int(time.time() * 1000) + (60 * 60 * 1000)

            async with self.order_lock:
                nonce = await self._get_next_nonce()
                if nonce is None:
                    return {"success": False, "hash": "", "error": "Failed to get nonce"}

                client_order_index = int(time.time() * 1000) + random.randint(0, 99999)
                
                # Try WebSocket first for lower latency
                if (hasattr(self, 'ws_order_client') and 
                    self._ws_order_enabled and 
                    self.ws_order_client.is_connected):
                    try:
                        # Use sign_create_order to get signed tx_info JSON string
                        tx_info_json = signer.sign_create_order(
                            market_index=int(market_id),
                            client_order_index=client_order_index,
                            base_amount=int(base_amount),
                            price=int(price_int),
                            is_ask=side == "SELL",
                            order_type=int(order_type),
                            time_in_force=int(tif),
                            reduce_only=bool(reduce_only),
                            trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                            order_expiry=int(expiry),
                            nonce=int(nonce),
                            api_key_index=int(self._resolved_api_key_index),
                        )
                        
                        if tx_info_json:
                            # Send via WebSocket
                            from src.adapters.ws_order_client import TransactionType
                            ws_result = await self.ws_order_client.send_transaction(
                                tx_type=TransactionType.CREATE_ORDER,  # 14
                                tx_info=tx_info_json
                            )
                            
                            logger.debug(f"[WS-ORDER] TWAP order sent via WS: {ws_result.hash}")
                            return {"success": True, "hash": ws_result.hash or "", "error": None, "tx": None}
                        else:
                            logger.warning("[WS-ORDER] sign_create_order returned empty, falling back to REST")
                    except Exception as e:
                        logger.warning(f"[WS-ORDER] WS submission failed: {e} - falling back to REST")
                
                # REST Fallback
                tx, resp, err = await signer.create_order(
                    market_index=int(market_id),
                    client_order_index=client_order_index,
                    base_amount=int(base_amount),
                    price=int(price_int),
                    is_ask=side == "SELL",
                    order_type=int(order_type),
                    time_in_force=int(tif),
                    reduce_only=bool(reduce_only),
                    trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                    order_expiry=int(expiry),
                    nonce=int(nonce),
                    api_key_index=int(self._resolved_api_key_index),
                )

                if err:
                    return {"success": False, "hash": "", "error": str(err)}

                tx_hash = getattr(resp, "tx_hash", "") if resp else ""
                return {"success": True, "hash": tx_hash or "", "error": None, "tx": tx}

        except Exception as e:
            logger.error(f"[Lighter TWAP] Exception: {e}", exc_info=True)
            return {"success": False, "hash": "", "error": str(e)}

    @rate_limited(Exchange.LIGHTER)
    async def place_order_with_sl_tp(
        self,
        symbol: str,
        side: str,
        size: Decimal,
        price: Decimal,
        stop_loss_price: Optional[Decimal] = None,
        take_profit_price: Optional[Decimal] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        time_in_force: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Place order with automatic Stop-Loss and Take-Profit (Unified Order).
        
        This is 3x faster than placing orders individually (1 API call instead of 3)
        and is atomic (all orders are created or none).
        
        Args:
            symbol: Trading symbol (e.g., "BTC-USD")
            side: BUY or SELL
            size: Order size in base currency
            price: Limit price for main order
            stop_loss_price: Optional stop-loss trigger price
            take_profit_price: Optional take-profit trigger price
            reduce_only: Whether order is reduce-only
            post_only: Whether order is post-only (maker)
            time_in_force: Optional time in force (IOC, GTC, etc.)
        
        Returns:
            Dict with:
            - success: bool
            - mainOrder: {tx, hash, error}
            - stopLoss: {tx, hash, error} (if provided)
            - takeProfit: {tx, hash, error} (if provided)
            - message: str
        """
        try:
            if not HAVE_LIGHTER_SDK:
                logger.error("❌ [Lighter Unified Order] SDK not installed")
                return {
                    'success': False,
                    'mainOrder': {'tx': None, 'hash': '', 'error': 'SDK not installed'},
                    'message': 'Lighter SDK not installed'
                }
            
            # Get market info
            market_info = self.market_info.get(symbol, {})
            market_id = safe_int(market_info.get('i'), -1)
            if market_id < 0:
                logger.error(f"❌ [Lighter Unified Order] Invalid market_id for {symbol}")
                return {
                    'success': False,
                    'mainOrder': {'tx': None, 'hash': '', 'error': 'Invalid market_id'},
                    'message': f'Invalid market_id for {symbol}'
                }
            
            # Quantize size and price
            size_inc = float(market_info.get('lot_size', market_info.get('min_base_amount', 0.0001)))
            price_inc = float(market_info.get('tick_size', 0.01))
            size_decimals = int(market_info.get('sd', 8))
            price_decimals = int(market_info.get('pd', 6))
            
            quantized_size = quantize_value(float(size), size_inc, rounding=ROUND_FLOOR)
            quantized_price = quantize_value(float(price), price_inc)
            
            scale_base = 10 ** size_decimals
            scale_price = 10 ** price_decimals
            
            base_amount = int(round(quantized_size * scale_base))
            price_int = int(round(quantized_price * scale_price))
            
            if base_amount <= 0:
                logger.error(f"❌ [Lighter Unified Order] Invalid base_amount for {symbol}")
                return {
                    'success': False,
                    'mainOrder': {'tx': None, 'hash': '', 'error': 'Invalid base_amount'},
                    'message': 'Invalid base_amount after quantization'
                }
            
            # Scale SL/TP prices if provided
            stop_loss_price_int = None
            take_profit_price_int = None
            if stop_loss_price:
                stop_loss_quantized = quantize_value(float(stop_loss_price), price_inc)
                stop_loss_price_int = int(round(stop_loss_quantized * scale_price))
            if take_profit_price:
                take_profit_quantized = quantize_value(float(take_profit_price), price_inc)
                take_profit_price_int = int(round(take_profit_quantized * scale_price))
            
            signer = await self._get_signer()
            
            # Check if createUnifiedOrder is available in the SDK
            if hasattr(signer, 'createUnifiedOrder') or hasattr(signer, 'create_unified_order'):
                # Use SDK method if available
                method = getattr(signer, 'createUnifiedOrder', None) or getattr(signer, 'create_unified_order', None)
                
                # Get order type
                order_type_limit = getattr(SignerClient, 'ORDER_TYPE_LIMIT', 0)
                
                # Get time in force
                tif = SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
                expiry = SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
                if time_in_force and time_in_force.upper() == "IOC":
                    tif = getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL', 0)
                    expiry = 0
                
                # Build params
                params = {
                    'marketIndex': market_id,
                    'clientOrderIndex': int(time.time() * 1000) + random.randint(0, 99999),
                    'baseAmount': base_amount,
                    'isAsk': side == 'SELL',
                    'orderType': order_type_limit,
                    'price': price_int,
                    'reduceOnly': reduce_only,
                    'timeInForce': tif,
                    'orderExpiry': expiry
                }
                
                if stop_loss_price_int:
                    params['stopLoss'] = {
                        'triggerPrice': stop_loss_price_int,
                        'isLimit': False
                    }
                
                if take_profit_price_int:
                    params['takeProfit'] = {
                        'triggerPrice': take_profit_price_int,
                        'isLimit': False
                    }
                
                await self.rate_limiter.acquire()
                async with self.order_lock:
                    current_nonce = await self._get_next_nonce()
                    if current_nonce is None:
                        return {
                            'success': False,
                            'mainOrder': {'tx': None, 'hash': '', 'error': 'Failed to get nonce'},
                            'message': 'Failed to get nonce'
                        }
                    
                    params['nonce'] = current_nonce
                    params['apiKeyIndex'] = int(self._resolved_api_key_index)
                    
                    try:
                        result = await method(**params)
                        
                        # Parse result
                        if hasattr(result, 'success'):
                            success = result.success
                        elif isinstance(result, dict):
                            success = result.get('success', False)
                        else:
                            success = bool(result)
                        
                        if success:
                            logger.info(f"✅ [Lighter Unified Order] Successfully placed order with SL/TP for {symbol}")
                            return {
                                'success': True,
                                'mainOrder': {
                                    'tx': getattr(result, 'mainOrder', {}).get('tx') if hasattr(result, 'mainOrder') else None,
                                    'hash': getattr(result, 'mainOrder', {}).get('hash', '') if hasattr(result, 'mainOrder') else '',
                                    'error': None
                                },
                                'stopLoss': {
                                    'tx': getattr(result, 'stopLoss', {}).get('tx') if hasattr(result, 'stopLoss') else None,
                                    'hash': getattr(result, 'stopLoss', {}).get('hash', '') if hasattr(result, 'stopLoss') else '',
                                    'error': getattr(result, 'stopLoss', {}).get('error') if hasattr(result, 'stopLoss') else None
                                } if stop_loss_price_int else None,
                                'takeProfit': {
                                    'tx': getattr(result, 'takeProfit', {}).get('tx') if hasattr(result, 'takeProfit') else None,
                                    'hash': getattr(result, 'takeProfit', {}).get('hash', '') if hasattr(result, 'takeProfit') else '',
                                    'error': getattr(result, 'takeProfit', {}).get('error') if hasattr(result, 'takeProfit') else None
                                } if take_profit_price_int else None,
                                'message': getattr(result, 'message', 'Success') if hasattr(result, 'message') else 'Success'
                            }
                        else:
                            error_msg = getattr(result, 'message', 'Unknown error') if hasattr(result, 'message') else 'Unknown error'
                            logger.error(f"❌ [Lighter Unified Order] Failed: {error_msg}")
                            return {
                                'success': False,
                                'mainOrder': {'tx': None, 'hash': '', 'error': error_msg},
                                'message': error_msg
                            }
                    except Exception as e:
                        logger.error(f"❌ [Lighter Unified Order] Exception: {e}", exc_info=True)
                        self.acknowledge_failure()
                        return {
                            'success': False,
                            'mainOrder': {'tx': None, 'hash': '', 'error': str(e)},
                            'message': f'Exception: {e}'
                        }
            else:
                # Fallback: Use grouped orders (OTO/OTOCO) when SL/TP are provided
                logger.warning("⚠️ [Lighter Unified Order] createUnifiedOrder not available in SDK, using grouped orders fallback")

                if stop_loss_price_int or take_profit_price_int:
                    grouping_type = 3 if (stop_loss_price_int and take_profit_price_int) else 1
                    main_is_ask = side == "SELL"
                    protect_is_ask = not main_is_ask

                    tif_main = SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
                    order_expiry = int(time.time() * 1000) + (28 * 24 * 60 * 60 * 1000)
                    if time_in_force and time_in_force.upper() == "IOC":
                        tif_main = SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
                        order_expiry = 0
                    elif post_only:
                        tif_main = SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY

                    grouped_orders = [
                        self._build_grouped_order_req(
                            market_id=market_id,
                            base_amount=base_amount,
                            price_int=price_int,
                            is_ask=main_is_ask,
                            order_type=SignerClient.ORDER_TYPE_LIMIT,
                            time_in_force=tif_main,
                            reduce_only=False,
                            trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                            order_expiry=order_expiry,
                        )
                    ]

                    if take_profit_price_int:
                        grouped_orders.append(
                            self._build_grouped_order_req(
                                market_id=market_id,
                                base_amount=0,
                                price_int=take_profit_price_int,
                                is_ask=protect_is_ask,
                                order_type=SignerClient.ORDER_TYPE_TAKE_PROFIT,
                                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                                reduce_only=True,
                                trigger_price=take_profit_price_int,
                                order_expiry=order_expiry,
                            )
                        )

                    if stop_loss_price_int:
                        grouped_orders.append(
                            self._build_grouped_order_req(
                                market_id=market_id,
                                base_amount=0,
                                price_int=stop_loss_price_int,
                                is_ask=protect_is_ask,
                                order_type=SignerClient.ORDER_TYPE_STOP_LOSS,
                                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                                reduce_only=True,
                                trigger_price=stop_loss_price_int,
                                order_expiry=order_expiry,
                            )
                        )

                    grouped_result = await self.place_grouped_orders(grouping_type, grouped_orders)
                    if grouped_result.get("success"):
                        tx_hash = grouped_result.get("hash", "")
                        return {
                            'success': True,
                            'mainOrder': {'tx': grouped_result.get("tx"), 'hash': tx_hash, 'error': None},
                            'stopLoss': {'tx': grouped_result.get("tx"), 'hash': tx_hash, 'error': None} if stop_loss_price_int else None,
                            'takeProfit': {'tx': grouped_result.get("tx"), 'hash': tx_hash, 'error': None} if take_profit_price_int else None,
                            'message': 'Order placed using grouped orders (OTO/OTOCO)'
                        }

                    logger.error(f"❌ [Lighter Unified Order] Grouped orders failed: {grouped_result.get('error')}")

                # Fallback: Place main order only (no SL/TP)
                success, order_id = await self.open_live_position(
                    symbol=symbol,
                    side=side,
                    notional_usd=float(size * price),
                    price=float(price),
                    reduce_only=reduce_only,
                    post_only=post_only,
                    amount=float(size),
                    time_in_force=time_in_force
                )

                if not success:
                    return {
                        'success': False,
                        'mainOrder': {'tx': None, 'hash': '', 'error': 'Main order failed'},
                        'message': 'Main order placement failed'
                    }

                return {
                    'success': True,
                    'mainOrder': {'tx': None, 'hash': order_id or '', 'error': None},
                    'stopLoss': None,
                    'takeProfit': None,
                    'message': 'Main order placed without SL/TP (grouped orders unavailable)'
                }
                
        except Exception as e:
            logger.error(f"❌ [Lighter Unified Order] Exception: {e}", exc_info=True)
            return {
                'success': False,
                'mainOrder': {'tx': None, 'hash': '', 'error': str(e)},
                'message': f'Exception: {e}'
            }

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
                                
                                # ═══════════════════════════════════════════════════════════════
                                # Try WebSocket first with retry logic (if enabled)
                                # Note: Market orders may not be supported via WebSocket, fallback to REST
                                # ═══════════════════════════════════════════════════════════════
                                if (hasattr(self, 'ws_order_client') and 
                                    self._ws_order_enabled and 
                                    self.ws_order_client):
                                    try:
                                        # For market orders, we still use create_order with IOC TIF
                                        # WebSocket may not support create_market_order directly
                                        tx_info_json = signer.sign_create_order(
                                            market_index=int(market_id),
                                            client_order_index=int(client_oid_final),
                                            base_amount=int(base),
                                            price=int(avg_exec_price),  # Use avg_exec_price as limit
                                            is_ask=bool(side == "SELL"),
                                            order_type=int(getattr(SignerClient, 'ORDER_TYPE_MARKET', 1)),
                                            time_in_force=int(getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL', 0)),
                                            reduce_only=bool(reduce_only),
                                            trigger_price=int(getattr(SignerClient, 'NIL_TRIGGER_PRICE', 0)),
                                            order_expiry=0,  # IOC requires expiry=0
                                            nonce=int(current_nonce),
                                            api_key_index=int(self._resolved_api_key_index),
                                        )
                                        
                                        if tx_info_json:
                                            ws_result = await self._submit_order_via_ws(tx_info_json)
                                            if ws_result and ws_result.hash:
                                                logger.debug(f"[WS-ORDER] Market order placed via WebSocket: {ws_result.hash}")
                                                return True, ws_result.hash
                                    except Exception as e:
                                        logger.debug(f"[WS-ORDER] Market order WebSocket attempt failed: {e} - using REST")
                                
                                # REST Fallback for market orders
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
                                
                                # ═══════════════════════════════════════════════════════════════
                                # Try WebSocket first with retry logic (if enabled)
                                # ═══════════════════════════════════════════════════════════════
                                if (hasattr(self, 'ws_order_client') and 
                                    self._ws_order_enabled and 
                                    self.ws_order_client):
                                    try:
                                        # Use sign_create_order to get signed tx_info JSON string
                                        tx_info_json = signer.sign_create_order(
                                            market_index=int(market_id),
                                            client_order_index=int(client_oid_final),
                                            base_amount=int(base),
                                            price=int(price_int),
                                            is_ask=bool(side == "SELL"),
                                            order_type=int(order_type_limit),
                                            time_in_force=int(tif),
                                            reduce_only=bool(reduce_only),
                                            trigger_price=int(getattr(SignerClient, 'NIL_TRIGGER_PRICE', 0)),
                                            order_expiry=int(expiry),
                                            nonce=int(current_nonce),
                                            api_key_index=int(self._resolved_api_key_index),
                                        )
                                        
                                        if tx_info_json:
                                            # Submit via WebSocket with retry logic
                                            ws_result = await self._submit_order_via_ws(tx_info_json)
                                            
                                            if ws_result and ws_result.hash:
                                                logger.debug(f"[WS-ORDER] Order placed via WebSocket: {ws_result.hash}")
                                                # Return success with hash
                                                return True, ws_result.hash
                                            else:
                                                logger.warning("[WS-ORDER] WebSocket submission failed, falling back to REST")
                                    except Exception as e:
                                        logger.warning(f"[WS-ORDER] WebSocket submission error: {e} - falling back to REST")
                                
                                # REST Fallback (or if WebSocket not available)
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

    async def batch_create_limit_order(
        self,
        symbol: str, 
        side: str, 
        notional_usd: float, 
        price: float = None,
        reduce_only: bool = False
    ) -> bool:
        """
        Queue a create limit order for batched execution.
        Reusable logic from open_live_position logic for sizing/pricing.
        """
        try:
            if not self.market_info or symbol not in self.market_info:
                logger.error(f"❌ Market info missing for {symbol}")
                return False

            # Validations
            if notional_usd <= 0 and not price:
                return False

            market_info = self.market_info[symbol]
            market_id = market_info.get('i') or market_info.get('market_id') or market_info.get('market_index')
            if market_id is None:
                return False

            # Price Logic (simplified from open_live_position)
            limit_price = price
            if not limit_price:
               # Must provide price for limit order
               return False

            # Quantization
            size_inc = safe_float(market_info.get('ss', 0.0001))
            if size_inc == 0: size_inc = 0.0001
            
            price_inc = safe_float(market_info.get('ts', 0.01))
            if price_inc == 0: price_inc = 0.01

            raw_amount = float(notional_usd) / float(limit_price)
            quantized_size = quantize_value(raw_amount, size_inc, rounding=ROUND_FLOOR)
            quantized_price = quantize_value(float(limit_price), price_inc)

            size_decimals = int(market_info.get('sd', 8))
            price_decimals = int(market_info.get('pd', 6))
            scale_base = 10 ** size_decimals
            scale_price = 10 ** price_decimals
            
            base = int(round(quantized_size * scale_base))
            price_int = int(round(quantized_price * scale_price))

            if base <= 0:
                logger.error(f"❌ Batch create: Base amount 0 for {symbol}")
                return False

            # Get Nonce
            async with self.order_lock:
                nonce = await self._get_next_nonce()
                if nonce is None:
                    return False
            
            client_oid = int(time.time() * 1000) + random.randint(0, 99999)
            
            # Add to Batch Manager
            if hasattr(self, 'batch_manager'):
                # ORDER_TYPE_LIMIT is usually 0
                order_type_limit = getattr(SignerClient, 'ORDER_TYPE_LIMIT', 0)
                
                # TIF: Default GTC unless specified (TODO: make params)
                # For now using GTC
                tif = getattr(SignerClient, 'ORDER_TIME_IN_FORCE_GTC', 0)
                
                success = await self.batch_manager.add_create_order(
                    market_index=int(market_id),
                    client_order_index=int(client_oid),
                    base_amount=int(base),
                    price=int(price_int),
                    is_ask=bool(side == "SELL"),
                    order_type=int(order_type_limit),
                    time_in_force=int(tif),
                    reduce_only=bool(reduce_only),
                    nonce=int(nonce),
                    api_key_index=int(self._resolved_api_key_index or 0)
                )
                return success
            else:
                logger.error("❌ BatchManager not initialized")
                return False

        except Exception as e:
            logger.error(f"❌ Batch create exception: {e}")
            return False

    async def modify_order(
        self,
        order_id: str,
        symbol: str,
        new_price: float,
        new_amount: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Modify an existing order's price (and optionally amount) atomically.

        Uses Lighter's sign_modify_order API to update price without cancel-risk.
        Falls back to cancel+replace if modify is not supported.

        Args:
            order_id: Hash or integer Order ID
            symbol: Trading symbol (e.g., "BTC-USD")
            new_price: New limit price
            new_amount: Optional new size in coins (if None, keeps original)

        Returns:
            (success: bool, new_order_id: str | None)
        """
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[LIGHTER] Shutdown active - skipping modify_order for {order_id}")
            return False, None

        try:
            if not HAVE_LIGHTER_SDK:
                logger.warning("[LIGHTER] SDK not available - cannot modify order")
                return False, None

            signer = await self._get_signer()
            market_data = self.market_info.get(symbol)
            if not market_data:
                logger.error(f"❌ modify_order: No market_data for {symbol}")
                return False, None

            market_id = safe_int(market_data.get("i"), -1)
            if market_id < 0:
                logger.error(f"❌ modify_order: Invalid market_id for {symbol}")
                return False, None

            # Quantize new price
            price_inc = float(market_data.get('tick_size', 0.01))
            price_decimals = int(market_data.get('pd', 6))
            quantized_price = quantize_value(new_price, price_inc)
            price_int = int(round(quantized_price * (10 ** price_decimals)))

            # Resolve amount if needed (keep original or use new)
            if new_amount is not None:
                size_inc = float(market_data.get('lot_size', market_data.get('min_base_amount', 0.0001)))
                size_decimals = int(market_data.get('sd', 8))
                quantized_size = quantize_value(new_amount, size_inc, rounding=ROUND_FLOOR)
                base_int = int(round(quantized_size * (10 ** size_decimals)))
            else:
                # SDK will keep original size if not provided
                base_int = None

            # Resolve order_id if it's a hash
            oid_int = None
            if str(order_id).isdigit():
                oid_int = int(str(order_id))
            elif isinstance(order_id, str) and len(order_id) > 15:
                # Try to resolve hash → integer ID
                logger.debug(f"🔍 modify_order: Resolving hash {order_id[:20]}... to Order ID")
                # (Same resolution logic as cancel_limit_order could be extracted to helper)
                # For now, skip hash resolution and try direct modify
                logger.warning(f"⚠️ modify_order: Hash resolution not implemented yet, trying direct modify")
                return False, None

            if oid_int is None:
                logger.error(f"❌ modify_order: Could not resolve order_id {order_id}")
                return False, None

            # ═══════════════════════════════════════════════════════════════
            # Call sign_modify_order (Lighter SDK API)
            # ═══════════════════════════════════════════════════════════════
            async with self.order_lock:
                nonce = await self._get_next_nonce()
                if nonce is None:
                    logger.error("❌ modify_order: Failed to get nonce")
                    return False, None

                logger.info(f"🔧 LIGHTER MODIFY {symbol}: OrderID={oid_int}, NewPrice=${quantized_price:.6f}, NewSize={new_amount or 'keep'}")

                # Check if SDK has sign_modify_order
                if not hasattr(signer, 'sign_modify_order'):
                    logger.warning("[LIGHTER] SDK does not have sign_modify_order - falling back to cancel+replace")
                    # TODO: Implement safe cancel+replace fallback
                    return False, None

                # Call modify_order
                # Expected signature: sign_modify_order(order_id, price, amount, nonce, api_key_index)
                # (Adjust based on actual SDK signature)
                modify_params = {
                    "order_id": oid_int,
                    "price": price_int,
                    "nonce": int(nonce),
                    "api_key_index": int(self._resolved_api_key_index),
                }
                if base_int is not None:
                    modify_params["amount"] = base_int

                tx, resp, err = await signer.sign_modify_order(**modify_params)

                if err:
                    logger.error(f"❌ LIGHTER MODIFY {symbol} FAILED: {err}")
                    return False, None

                # Extract new order hash from response
                new_hash = None
                if resp and hasattr(resp, 'tx_hash'):
                    new_hash = resp.tx_hash
                elif isinstance(resp, dict):
                    new_hash = resp.get('tx_hash') or resp.get('hash')

                logger.info(f"✅ LIGHTER MODIFY {symbol}: Success (new_hash={new_hash})")
                return True, new_hash

        except Exception as e:
            logger.error(f"❌ LIGHTER MODIFY {symbol} exception: {e}", exc_info=True)
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
                market_id_val = (
                    market_data.get('market_id')
                    if market_data.get('market_id') is not None
                    else market_data.get('marketId')
                )
                market_index_val = (
                    market_data.get('market_index')
                    if market_data.get('market_index') is not None
                    else market_data.get('marketIndex')
                )
                if market_id_val is None and market_index_val is None:
                    market_id_val = market_data.get('id') if market_data.get('id') is not None else market_data.get('i')

                # Try a small set of queries to resolve tx_hash -> order id.
                # 1) Open orders (status=0) is the most relevant for cancellation.
                # 2) Fallback: query other statuses / no-status in case the API behaves differently.
                # Also try both param names market_id / market_index (REST compatibility).
                candidate_params = []
                base_params = {
                    "account_index": int(self._resolved_account_index),
                    "limit": 50,
                }

                # Prefer correct param/value pairs.
                candidate_pairs = []
                if market_id_val is not None:
                    candidate_pairs.append(("market_id", int(market_id_val)))
                if market_index_val is not None:
                    candidate_pairs.append(("market_index", int(market_index_val)))

                # If we only know one identifier, still try the alternate param name with same value.
                if len(candidate_pairs) == 1:
                    p_name, p_val = candidate_pairs[0]
                    other = "market_index" if p_name == "market_id" else "market_id"
                    candidate_pairs.append((other, p_val))

                # De-dup
                seen_pairs = set()
                dedup_pairs = []
                for pair in candidate_pairs:
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    dedup_pairs.append(pair)

                if dedup_pairs:
                    for market_param_name, market_ref in dedup_pairs:
                        mp = {**base_params, market_param_name: int(market_ref)}
                        candidate_params.append({**mp, "status": 0})
                        candidate_params.append({**mp, "status": 1})
                        candidate_params.append({**mp, "status": 2})
                        candidate_params.append({**mp, "status": 3})
                        candidate_params.append(mp)
                else:
                    candidate_params.append(base_params)

                for params in candidate_params:
                    # Remove any None values (defensive)
                    params = {k: v for k, v in params.items() if v is not None}
                    resp = await self._rest_get("/api/v1/orders", params=params)
                    raw_orders = _normalize_lighter_orders_response(resp)

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
                nonce = await self._get_next_nonce()
                if nonce is None:
                    logger.error("❌ Failed to get nonce for cancel")
                    return False
                
                # ═══════════════════════════════════════════════════════════════
                # Try WebSocket first with retry logic (if enabled)
                # ═══════════════════════════════════════════════════════════════
                if (hasattr(self, 'ws_order_client') and 
                    self._ws_order_enabled and 
                    self.ws_order_client):
                    try:
                        from src.adapters.ws_order_client import TransactionType
                        
                        # Use sign_cancel_order to get signed tx_info JSON string
                        tx_info_json = signer.sign_cancel_order(
                            order_index=int(oid_int),
                            nonce=int(nonce),
                            api_key_index=int(self._resolved_api_key_index)
                        )
                        
                        if tx_info_json:
                            # Submit via WebSocket with retry logic
                            ws_result = await self._submit_order_via_ws(tx_info_json)
                            
                            if ws_result and ws_result.hash:
                                logger.debug(f"[WS-ORDER] Order cancelled via WebSocket: {ws_result.hash}")
                                return True
                            else:
                                logger.warning("[WS-ORDER] WebSocket cancel failed, falling back to REST")
                    except Exception as e:
                        logger.warning(f"[WS-ORDER] WebSocket cancel error: {e} - falling back to REST")
                
                # REST Fallback
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

    async def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> str:
        """
        Check status of a specific order.
        Returns: 'OPEN', 'FILLED', 'CANCELLED', 'UNKNOWN'
        """
        try:
            if not HAVE_LIGHTER_SDK:
                return "UNKNOWN"

            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)

            # Resolve symbol from tracked orders if not provided
            if not symbol and order_id in self._placed_orders:
                symbol = self._placed_orders[order_id].get("symbol")

            if not symbol and self._placed_orders:
                for entry in self._placed_orders.values():
                    if str(entry.get("client_order_index")) == str(order_id):
                        symbol = entry.get("symbol")
                        break

            if not symbol:
                return "UNKNOWN"

            market_id = safe_int(self.market_info.get(symbol, {}).get("i"), -1)
            if market_id < 0:
                return "UNKNOWN"

            await self._resolve_account_index()
            auth_token, auth_err = signer.create_auth_token_with_expiry()
            auth = auth_token if not auth_err else None

            def _match_order(orders):
                for order in orders or []:
                    order_id_str = str(getattr(order, "order_id", ""))
                    client_order_id = str(getattr(order, "client_order_id", ""))
                    order_index = str(getattr(order, "order_index", ""))
                    client_order_index = str(getattr(order, "client_order_index", ""))
                    if str(order_id) in (order_id_str, client_order_id, order_index, client_order_index):
                        return order
                return None

            await self.rate_limiter.acquire()
            active = await order_api.account_active_orders(
                account_index=int(self._resolved_account_index),
                market_id=int(market_id),
                auth=auth,
            )
            match = _match_order(getattr(active, "orders", None))
            if match:
                status = str(getattr(match, "status", "")).lower()
                if status in ("open", "pending", "in-progress"):
                    return "OPEN"
                if status == "filled":
                    return "FILLED"
                if status.startswith("canceled"):
                    return "CANCELLED"
                return "UNKNOWN"

            await self.rate_limiter.acquire()
            inactive = await order_api.account_inactive_orders(
                account_index=int(self._resolved_account_index),
                market_id=int(market_id),
                auth=auth,
            )
            match = _match_order(getattr(inactive, "orders", None))
            if match:
                status = str(getattr(match, "status", "")).lower()
                if status == "filled":
                    return "FILLED"
                if status.startswith("canceled"):
                    return "CANCELLED"
                return "UNKNOWN"

            return "UNKNOWN"

        except Exception as e:
            logger.error(f"Lighter Status Check Error {order_id}: {e}")
            return "UNKNOWN"

    def get_cancel_reason(self, status: str) -> Optional[str]:
        """
        Extract cancel reason from Lighter status string.
        Example: "canceled-post-only" -> "post-only"
        """
        if not status:
            return None
        status_lower = status.lower()
        if not status_lower.startswith("canceled"):
            return None
        if status_lower == "canceled":
            return "canceled"
        return status_lower.replace("canceled-", "", 1)

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
        """Cleanup all resources including SDK clients and stream client"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Prevent duplicate close calls during shutdown
        # ═══════════════════════════════════════════════════════════════
        if hasattr(self, '_closed') and self._closed:
            return
        self._closed = True
        
        # Stop Stream Client first
        try:
            await self.stop_stream_client()
        except Exception as e:
            logger.debug(f"Lighter: Error stopping stream client: {e}")
        
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

        # Stop Batch Manager
        if hasattr(self, 'batch_manager'):
            await self.batch_manager.stop()
        
        # B1: Stop WebSocket Order Client
        if hasattr(self, 'ws_order_client'):
            try:
                await self.ws_order_client.disconnect()
                logger.info("✅ [WS-ORDER] Disconnected")
            except Exception as e:
                logger.debug(f"[WS-ORDER] Disconnect warning: {e}")

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

                # Re-verify after force-cancel.
                still_open = await self.get_open_orders(symbol)
                if still_open:
                    logger.error(f"❌ {symbol}: CancelAll reported completion but {len(still_open)} open order(s) still remain")
                    return False
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
