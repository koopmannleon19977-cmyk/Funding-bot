# src/adapters/x10_adapter.py
import asyncio
import logging
import inspect  # WICHTIG für aclose Prüfung
import json
import random  # Für sporadisches Logging in handle_orderbook_snapshot
import websockets
import aiohttp
import time
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, List, Any, Dict, Callable
from enum import Enum

from src.rate_limiter import X10_RATE_LIMITER, get_rate_limiter, Exchange
import config
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide

try:
    from x10.perpetual.orders import OrderTimeInForce as TimeInForce
except ImportError:
    from x10.perpetual.orders import TimeInForce

from x10.perpetual.accounts import StarkPerpetualAccount
from .base_adapter import BaseAdapter
from .x10_stream_client import X10StreamClient

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Q3: Self-Trade Protection Level (SDK enum)
# Observed accepted values in logs:
# - DISABLED / ACCOUNT / CLIENT
# (see logs/funding_bot_LEON_20251218_115320_FULL.log:238)
# ═══════════════════════════════════════════════════════════════════════════════
class SelfTradeProtectionLevel(str, Enum):
    DISABLED = "DISABLED"
    ACCOUNT = "ACCOUNT"
    CLIENT = "CLIENT"


# ═══════════════════════════════════════════════════════════════════════════════
# Q4: Order Status Reason (from Extended-TS-SDK/src/perpetual/orders.ts)
# ═══════════════════════════════════════════════════════════════════════════════
class OrderStatusReason(str, Enum):
    """
    Detailed reasons why an order was rejected, cancelled, or expired.
    Essential for proper error handling and debugging.
    """
    UNKNOWN = "UNKNOWN"
    NONE = "NONE"
    UNKNOWN_MARKET = "UNKNOWN_MARKET"
    DISABLED_MARKET = "DISABLED_MARKET"
    NOT_ENOUGH_FUNDS = "NOT_ENOUGH_FUNDS"
    NO_LIQUIDITY = "NO_LIQUIDITY"
    INVALID_FEE = "INVALID_FEE"
    INVALID_QTY = "INVALID_QTY"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_VALUE = "INVALID_VALUE"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    SELF_TRADE_PROTECTION = "SELF_TRADE_PROTECTION"
    POST_ONLY_FAILED = "POST_ONLY_FAILED"
    REDUCE_ONLY_FAILED = "REDUCE_ONLY_FAILED"
    INVALID_EXPIRE_TIME = "INVALID_EXPIRE_TIME"
    POSITION_TPSL_CONFLICT = "POSITION_TPSL_CONFLICT"
    INVALID_LEVERAGE = "INVALID_LEVERAGE"
    PREV_ORDER_NOT_FOUND = "PREV_ORDER_NOT_FOUND"
    PREV_ORDER_TRIGGERED = "PREV_ORDER_TRIGGERED"
    TPSL_OTHER_SIDE_FILLED = "TPSL_OTHER_SIDE_FILLED"
    PREV_ORDER_CONFLICT = "PREV_ORDER_CONFLICT"
    ORDER_REPLACED = "ORDER_REPLACED"
    POST_ONLY_MODE = "POST_ONLY_MODE"
    REDUCE_ONLY_MODE = "REDUCE_ONLY_MODE"
    TRADING_OFF_MODE = "TRADING_OFF_MODE"


# ═══════════════════════════════════════════════════════════════════════════════
# Additional X10 Enums for completeness
# ═══════════════════════════════════════════════════════════════════════════════
class X10OrderStatus(str, Enum):
    """X10 order status values."""
    UNKNOWN = "UNKNOWN"
    NEW = "NEW"
    UNTRIGGERED = "UNTRIGGERED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class X10OrderType(str, Enum):
    """X10 order types."""
    LIMIT = "LIMIT"
    CONDITIONAL = "CONDITIONAL"
    MARKET = "MARKET"
    TPSL = "TPSL"


class X10PositionSide(str, Enum):
    """X10 position sides."""
    LONG = "LONG"
    SHORT = "SHORT"


class X10ExitType(str, Enum):
    """X10 position exit types."""
    TRADE = "TRADE"
    LIQUIDATION = "LIQUIDATION"
    ADL = "ADL"


from src.utils import safe_float, safe_decimal, quantize_value


class X10Adapter(BaseAdapter):
    def __init__(self):
        super().__init__("X10")
        self.market_info = {}
        self.client_env = MAINNET_CONFIG
        self.stark_account = None
        self._auth_client = None
        self.trading_client = None
        self.price_cache = {}
        self.funding_cache = {}
        self.orderbook_cache = {}
        # Added internal underscore-prefixed caches for parity with other adapters (e.g. LighterAdapter)
        # so shared components expecting _price_cache / _orderbook_cache / _trade_cache work.
        self._price_cache = {}
        self._price_cache_time = {}
        self._orderbook_cache = {}
        self._orderbook_cache_time = {}
        self._trade_cache = {}
        self._funding_cache = {}
        self._funding_cache_time = {}  # Ensure this exists
        self._candle_cache = {}
        self._candle_cache_time = {}
        
        # NEW: Public Aliases for Latency/Prediction modules
        self.price_cache_time = self._price_cache_time
        self.funding_cache_time = self._funding_cache_time
        
        self.price_update_event = None  # Will be set by main loop

        # WebSocket Streaming Support
        self._ws_message_queue = asyncio.Queue()

        self.rate_limiter = X10_RATE_LIMITER
        
        # ═══════════════════════════════════════════════════════════════
        # WebSocket Account Event Caches
        # These are populated by WebSocketManager callbacks
        # ═══════════════════════════════════════════════════════════════
        self._order_cache: Dict[str, dict] = {}      # order_id -> order data
        self._position_cache: Dict[str, dict] = {}   # symbol -> position data
        self._positions_cache: List[dict] = []      # List of positions with entry_price (from REST API)
        self._balance_cache: float = 0.0             # Latest balance value (for deduplication)
        self._last_balance_update: float = 0.0       # Timestamp of last balance update
        self._pending_orders: Dict[str, asyncio.Future] = {}  # order_id -> Future for async fill waiting
        
        # ═══════════════════════════════════════════════════════════════
        # Entry Price Tracking from TRADE/FILL messages
        # Track fills per symbol to calculate weighted average entry price
        # Format: {symbol: {"total_qty": float, "total_value": float, "fills": List[dict]}}
        # ═══════════════════════════════════════════════════════════════
        self._fill_tracking: Dict[str, dict] = {}  # symbol -> {total_qty, total_value, fills}
        
        # ═══════════════════════════════════════════════════════════════
        # FIX: Store recent close fills for PnL calculation
        # These are preserved even after position is closed
        # ═══════════════════════════════════════════════════════════════
        self._recent_close_fills: Dict[str, list] = {}  # symbol -> list of close fills
        
        # ═══════════════════════════════════════════════════════════════
        # FIX: Track the most recent reduce_only CLOSE order id per symbol
        # so we can attribute close fills correctly (avoid mixing entry+exit)
        # ═══════════════════════════════════════════════════════════════
        self._closing_order_ids: Dict[str, str] = {}  # symbol -> order_id (string)
        
        # Event callbacks for external subscribers
        self._order_callbacks: List[Callable] = []
        self._position_callbacks: List[Callable] = []
        self._fill_callbacks: List[Callable] = []
        
        # ═══════════════════════════════════════════════════════════════
        # X10 Stream Client for real-time updates
        # ═══════════════════════════════════════════════════════════════
        self._stream_client: Optional[X10StreamClient] = None
        self._stream_client_task: Optional[asyncio.Task] = None
        self._stream_metrics: Dict[str, Any] = {
            'account_updates': 0,
            'orderbook_updates': 0,
            'trade_updates': 0,
            'funding_updates': 0,
            'last_update_time': 0.0,
            'connection_health': 'unknown',
            'reconnect_count': 0
        }
        self._stream_health_check_task: Optional[asyncio.Task] = None
        
        # ═══════════════════════════════════════════════════════════════
        # X10 WebSocket Order Client for low-latency order submission
        # ═══════════════════════════════════════════════════════════════
        self._ws_order_enabled = getattr(config, 'X10_WS_ORDER_ENABLED', False)
        self.ws_order_client = None
        if self._ws_order_enabled:
            try:
                from src.adapters.x10_ws_order_client import X10WebSocketOrderClient, X10WsOrderConfig
                ws_order_url = getattr(config, 'X10_WS_ORDER_URL', None)
                api_key = self.stark_account.api_key if self.stark_account else None
                ws_config = X10WsOrderConfig(
                    url=ws_order_url or "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account",
                    api_key=api_key
                )
                self.ws_order_client = X10WebSocketOrderClient(ws_config)
                logger.info("✅ [X10] WebSocket Order Client initialized")
            except Exception as e:
                logger.warning(f"⚠️ [X10] Failed to initialize WebSocket Order Client: {e}")
                self._ws_order_enabled = False

        try:
            if config.X10_VAULT_ID:
                vault_id = int(str(config.X10_VAULT_ID).strip())
                self.stark_account = StarkPerpetualAccount(
                    vault=vault_id,
                    private_key=config.X10_PRIVATE_KEY,
                    public_key=config.X10_PUBLIC_KEY,
                    api_key=config.X10_API_KEY,
                )
                logger.info(" X10 Account initialisiert.")
            else:
                logger.warning(" X10 Config fehlt (Vault ID leer).")
        except Exception as e:
            logger.error(f" X10 Account Init Error: {e}")

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[dict]:
        """
        Fetch order details - FIRST checks WebSocket cache (has avgFillPrice!), 
        then falls back to REST API.
        
        FIX (2025-12-19): The REST API only returns the limit price, not the actual
        fill price. The WebSocket cache contains the real avgFillPrice from FILL events.
        """
        if not order_id or order_id == "DRY_RUN_ORDER_123":
            return None
        
        try:
            order_id_str = str(order_id)
            
            # ═══════════════════════════════════════════════════════════════
            # FIX: Check WebSocket cache FIRST - it has the real avgFillPrice!
            # The REST API only returns the limit order price, not the fill price.
            # ═══════════════════════════════════════════════════════════════
            if hasattr(self, '_order_cache') and order_id_str in self._order_cache:
                cached = self._order_cache[order_id_str]
                
                # Extract avgFillPrice from WebSocket data
                avg_fill = (
                    cached.get("avgFillPrice") or
                    cached.get("avg_fill_price") or
                    cached.get("avgPrice") or
                    cached.get("averagePrice") or
                    cached.get("fillPrice") or
                    None
                )
                
                price = safe_float(cached.get("price"), 0.0)
                avg_fill_price = safe_float(avg_fill, price)  # Fallback to limit price
                
                logger.debug(f"X10 get_order: Using cached WebSocket data (avgFillPrice=${avg_fill_price})")
                
                return {
                    "id": order_id_str,
                    "symbol": cached.get("market") or cached.get("symbol") or symbol,
                    "price": price,
                    "avgFillPrice": avg_fill_price,  # ← REAL fill price from WebSocket!
                    "filledAmount": safe_float(cached.get("filledQty") or cached.get("filled_qty"), 0.0),
                    "status": cached.get("status"),
                    "side": cached.get("side"),
                    "post_only": cached.get("post_only") or cached.get("postOnly") or False,
                }
            
            # ═══════════════════════════════════════════════════════════════
            # Fallback: REST API (Note: only returns limit price, not fill price!)
            # ═══════════════════════════════════════════════════════════════
            try:
                order_id_int = int(order_id)
            except ValueError:
                logger.debug(f"X10: Invalid order_id format for get_order: {order_id}")
                return None
            
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/user/orders/{order_id_int}"
            
            if not self.stark_account:
                return None
            
            headers = {
                "X-Api-Key": self.stark_account.api_key,
                "Accept": "application/json"
            }
            
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    order = data.get("data", {})
                    
                    # REST API only has limit price - not the actual fill price!
                    filled_amount = safe_float(order.get("filled_amount_of_synthetic"), 0.0)
                    price = safe_float(order.get("price"), 0.0)
                    
                    # Try to get average execution price if available
                    avg_exec_price = safe_float(
                        order.get("average_price") or 
                        order.get("avg_price") or 
                        order.get("avgFillPrice") or
                        price,  # Fallback to limit price
                        0.0
                    )
                    
                    logger.debug(f"X10 get_order: REST API returned price=${price}, avgFillPrice=${avg_exec_price}")
                    
                    return {
                        "id": order_id,
                        "symbol": order.get("market") or symbol,
                        "price": price,
                        "avgFillPrice": avg_exec_price,
                        "filledAmount": filled_amount,
                        "status": order.get("status"),
                        "side": order.get("side"),
                        "post_only": order.get("post_only") or order.get("postOnly") or False,
                    }
                else:
                    logger.debug(f"X10 get_order: HTTP {resp.status} for order {order_id}")
                    return None
                        
        except Exception as e:
            logger.debug(f"X10 get_order error for {order_id}: {e}")
            return None

    async def get_order_fee(self, order_id: str) -> float:
        """Fetch real fee from X10 order"""
        if not order_id or order_id == "DRY_RUN_ORDER_123":
            return 0.0
        
        try:
            try:
                order_id_int = int(order_id)
            except ValueError:
                logger.error(f"X10: Invalid order_id format: {order_id}")
                return config.TAKER_FEE_X10
            
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/user/orders/{order_id_int}"
            
            if not self.stark_account:
                return config.TAKER_FEE_X10
            
            headers = {
                "X-Api-Key": self.stark_account.api_key,
                "Accept": "application/json"
            }
            
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    order = data.get("data", {})
                    
                    fee_abs = safe_float(order.get("fee"), 0.0)
                    filled = order.get("filled_amount_of_synthetic")
                    price = safe_float(order.get("price"), 0.0)
                    
                    if fee_abs is not None and filled and price:
                        try:
                            fee_usd = float(str(fee_abs))
                            filled_qty = float(str(filled))
                            order_price = float(str(price))
                            notional = filled_qty * order_price
                            
                            if notional > 0:
                                fee_rate = fee_usd / notional
                                if 0 <= fee_rate <= 0.01:
                                    return fee_rate
                        except (ValueError, TypeError):
                            pass
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX #4: POST_ONLY is not a TimeInForce value - check post_only parameter
                    # Some APIs may return time_in_force="POST_ONLY" as string, but
                    # the correct way is to check the post_only boolean parameter
                    # ═══════════════════════════════════════════════════════════════
                    time_in_force = order.get("time_in_force")
                    post_only_flag = order.get("post_only") or order.get("postOnly") or False
                    
                    # Check if order is POST_ONLY (either via parameter or legacy string value)
                    if post_only_flag or (time_in_force and "POST_ONLY" in str(time_in_force).upper()):
                        return config.MAKER_FEE_X10
                    else:
                        return config.TAKER_FEE_X10
                else:
                    return config.TAKER_FEE_X10
                        
        except Exception as e:
            logger.error(f"X10 Fee Fetch Error for {order_id}: {e}")
            return config.TAKER_FEE_X10

    async def fetch_fee_schedule(self) -> Optional[Tuple[float, float]]:
        """
        Fetch fee schedule from X10 API via Account Info (Authenticated)
        """
        try:
            # 1. Try via Trading Client SDK (Authenticated)
            try:
                client = await self._get_trading_client()
                # Check for both get_account_info and get_profile
                method = getattr(client.account, 'get_account_info', getattr(client.account, 'get_profile', None))
                
                if method:
                    await self.rate_limiter.acquire()
                    resp = await method()
                    self.rate_limiter.on_success()
                
                    if resp and getattr(resp, 'data', None):
                        info = resp.data
                    else:
                        info = resp

                    # Parsing logic...
                    fee_schedule = None
                    if hasattr(info, 'fee_schedule'):
                        fee_schedule = info.fee_schedule
                    elif isinstance(info, dict):
                        fee_schedule = info.get('fee_schedule')
                    
                    if fee_schedule:
                        maker = safe_float(getattr(fee_schedule, 'maker', None) or fee_schedule.get('maker'), config.MAKER_FEE_X10)
                        taker = safe_float(getattr(fee_schedule, 'taker', None) or fee_schedule.get('taker'), config.TAKER_FEE_X10)
                        logger.info(f"✅ X10 Fee Schedule (SDK): Maker={maker:.6f}, Taker={taker:.6f}")
                        return (maker, taker)
            except Exception as e:
                logger.debug(f"X10 SDK Fee fetch failed: {e}")

            # 2. Fallback: Direct Authenticated REST Call
            # FIX: Try multiple endpoints since /user/account often 404s
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            
            endpoints = ["/api/v1/user/fees", "/api/v1/user/profile", "/api/v1/user", "/api/v1/user/account"]
            
            if not self.stark_account or not self.stark_account.api_key:
                return None

            headers = {
                'X-Api-Key': self.stark_account.api_key,
                'User-Agent': 'X10PythonTradingClient/0.4.5',
                'Accept': 'application/json'
            }

            session = await self._get_session()
            for endpoint in endpoints:
                url = f"{base_url}{endpoint}"
                try:
                    await self.rate_limiter.acquire()
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self.rate_limiter.on_success()
                        
                            inner = data.get('data') or data
                            
                            # Strategy 0: Handles /api/v1/user/fees (Returned a list of per-market fees)
                            if isinstance(inner, list) and len(inner) > 0:
                                first_market = inner[0]
                                # Fields are makerFeeRate / takerFeeRate
                                maker = first_market.get('makerFeeRate')
                                taker = first_market.get('takerFeeRate')
                                
                                if maker is not None and taker is not None:
                                    maker_val = safe_float(maker, config.MAKER_FEE_X10)
                                    taker_val = safe_float(taker, config.TAKER_FEE_X10)
                                    logger.info(f"✅ X10 Fee Schedule (REST {endpoint}): Maker={maker_val:.6f}, Taker={taker_val:.6f}")
                                    return (maker_val, taker_val)

                            # Strategy 1: Nested fee_schedule object (common in other endpoints)
                            elif isinstance(inner, dict):
                                fee_schedule = inner.get('fee_schedule', {})
                                maker = None
                                taker = None

                                if fee_schedule:
                                    maker = fee_schedule.get('maker')
                                    taker = fee_schedule.get('taker')
                            
                                # Strategy 2: Direct keys
                                if maker is None and taker is None:
                                    maker = inner.get('maker_fee') or inner.get('maker')
                                    taker = inner.get('taker_fee') or inner.get('taker')
                                
                                if maker is not None and taker is not None:
                                    maker_val = safe_float(maker, config.MAKER_FEE_X10)
                                    taker_val = safe_float(taker, config.TAKER_FEE_X10)
                                    logger.info(f"✅ X10 Fee Schedule (REST {endpoint}): Maker={maker_val:.6f}, Taker={taker_val:.6f}")
                                    return (maker_val, taker_val)

                        elif resp.status == 404:
                            continue # Try next endpoint
                        else:
                            logger.debug(f"X10 Fee {endpoint} failed: {resp.status}")
                except Exception:
                    pass
            
            logger.warning("X10 Fee Fetch: All endpoints failed, using config defaults.")
            return (config.MAKER_FEE_X10, config.TAKER_FEE_X10)
                
        except Exception as e:
            logger.error(f"X10 Fee Schedule fetch error: {e}")
            return None

    async def start_websocket(self):
        """WebSocket entry point für WebSocketManager"""
        logger.info(f"🌐 {self.name}: WebSocket Manager starting streams...")
        await asyncio.gather(
            self._poll_funding_rates(),
            self._poll_mark_prices(),
            return_exceptions=True
        )

    async def ws_message_stream(self):
        """WebSocketManager nutzt das"""
        while True:
            yield await self._ws_message_queue.get()
    
    # ═══════════════════════════════════════════════════════════════
    # X10 Stream Client Methods (Real-time WebSocket Updates)
    # ═══════════════════════════════════════════════════════════════
    
    async def initialize_stream_client(self, symbols: Optional[List[str]] = None) -> None:
        """
        Initialize and start the X10 Stream Client for real-time updates.
        
        Subscribes to:
        - Account updates (positions, orders, balance)
        - Funding rates (all markets)
        - Orderbooks (for specified symbols, better price data)
        - Public trades (for specified symbols, trade analysis)
        
        Args:
            symbols: List of symbols to subscribe to for orderbooks and trades.
                    If None, uses all common symbols from market_info.
        """
        if not self.stark_account or not self.stark_account.api_key:
            logger.warning("⚠️ [X10 Stream] Cannot initialize: Missing API key")
            return
        
        # Python SDK uses snake_case: stream_url instead of streamUrl
        stream_url = getattr(self.client_env, 'stream_url', None) or getattr(self.client_env, 'streamUrl', None)
        if not stream_url:
            logger.warning("⚠️ [X10 Stream] Cannot initialize: Missing stream URL in config")
            return
        
        # Determine symbols for orderbook/trade streams
        if symbols is None:
            symbols = list(self.market_info.keys())
        
        # Limit to top symbols to avoid too many connections
        # Prioritize high-volume symbols
        priority_symbols = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'TAO-USD', 'CRV-USD']
        stream_symbols = [s for s in priority_symbols if s in symbols]
        stream_symbols.extend([s for s in symbols if s not in stream_symbols][:10])  # Max 15 symbols
        
        try:
            self._stream_client = X10StreamClient(
                stream_url=stream_url,
                api_key=self.stark_account.api_key
            )
            
            # Subscribe to account updates (positions, orders, balance)
            await self._stream_client.subscribe_to_account_updates(
                message_handler=self._handle_account_stream_message,
                on_connect=self._on_stream_connect,
                on_disconnect=self._on_stream_disconnect
            )
            
            # Subscribe to funding rates (all markets)
            await self._stream_client.subscribe_to_funding_rates(
                message_handler=self._handle_funding_stream_message
            )
            
            # Subscribe to orderbooks for key symbols (better price data)
            orderbook_symbols = stream_symbols[:10]  # Limit to 10 to avoid too many connections
            logger.info(f"📊 [X10 Stream] Subscribing to orderbooks for {len(orderbook_symbols)} symbols...")
            for symbol in orderbook_symbols:
                # Create closure to properly capture symbol
                def make_orderbook_handler(sym):
                    async def handler(data):
                        await self._handle_orderbook_stream_message(data, sym)
                    return handler
                
                # Test without depth parameter first - websocat example works without depth
                # According to API docs, depth is optional and defaults to full orderbook
                await self._stream_client.subscribe_to_orderbooks(
                    message_handler=make_orderbook_handler(symbol),
                    market_name=symbol,
                    depth=None  # Don't specify depth - let server use default
                )
            
            # Subscribe to public trades for key symbols (trade analysis)
            trade_symbols = stream_symbols[:10]  # Limit to 10
            logger.info(f"📈 [X10 Stream] Subscribing to public trades for {len(trade_symbols)} symbols...")
            for symbol in trade_symbols:
                # Create closure to properly capture symbol
                def make_trade_handler(sym):
                    async def handler(data):
                        await self._handle_trade_stream_message(data, sym)
                    return handler
                
                await self._stream_client.subscribe_to_public_trades(
                    message_handler=make_trade_handler(symbol),
                    market_name=symbol
                )

            # Subscribe to candle streams (optional)
            if getattr(config, "X10_CANDLE_STREAM_ENABLED", False):
                candle_type = getattr(config, "X10_CANDLE_STREAM_TYPE", "trade")
                candle_interval = getattr(config, "X10_CANDLE_STREAM_INTERVAL", "1m")
                candle_symbols = stream_symbols[:5]
                logger.info(
                    f"[X10 Stream] Subscribing to candles for {len(candle_symbols)} symbols "
                    f"(type={candle_type}, interval={candle_interval})"
                )
                for symbol in candle_symbols:
                    def make_candle_handler(sym):
                        async def handler(data):
                            await self._handle_candle_stream_message(data, sym)
                        return handler

                    await self._stream_client.subscribe_to_candles(
                        message_handler=make_candle_handler(symbol),
                        market_name=symbol,
                        candle_type=candle_type,
                        interval=candle_interval,
                    )
            
            # Start all streams
            await self._stream_client.start()
            
            # Initialize metrics
            self._stream_metrics['start_time'] = time.time()
            self._stream_metrics['connection_health'] = 'healthy'
            
            # Start health check monitoring
            self._stream_health_check_task = asyncio.create_task(self._stream_health_check_loop())
            
            logger.info(f"✅ [X10 Stream Client] Initialized and started ({len(stream_symbols)} symbols)")
            
        except Exception as e:
            logger.error(f"❌ [X10 Stream Client] Initialization failed: {e}")
            self._stream_client = None
    
    async def stop_stream_client(self) -> None:
        """Stop the X10 Stream Client"""
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
                logger.info("🛑 [X10 Stream Client] Stopped")
            except Exception as e:
                logger.error(f"❌ [X10 Stream Client] Stop error: {e}")
            finally:
                self._stream_client = None
    
    async def _on_stream_connect(self) -> None:
        """Callback when stream connects"""
        logger.info("✅ [X10 Stream] Connection established")
    
    async def _on_stream_disconnect(self) -> None:
        """Callback when stream disconnects"""
        logger.warning("⚠️ [X10 Stream] Connection lost")
    
    async def _handle_account_stream_message(self, data: Dict[str, Any]) -> None:
        """
        Handle account update messages from stream.
        
        Message types:
        - position: Position updates
        - order: Order updates
        - balance: Balance updates
        """
        try:
            msg_type = data.get("type") or data.get("eventType") or ""
            
            # Update metrics
            self._stream_metrics['account_updates'] += 1
            self._stream_metrics['last_update_time'] = time.time()
            
            if msg_type == "position" or "position" in data:
                position_data = data.get("position") or data.get("data") or data
                await self.on_position_update(position_data)
            
            elif msg_type == "order" or "order" in data:
                order_data = data.get("order") or data.get("data") or data
                await self.on_order_update(order_data)
            
            elif msg_type == "balance" or "balance" in data:
                balance_data = data.get("balance") or data.get("data") or data
                await self._handle_balance_update(balance_data)
            
            elif msg_type == "trade" or "fill" in data or "trade" in data:
                trade_data = data.get("trade") or data.get("fill") or data.get("data") or data
                await self.on_fill_update(trade_data)
            
            else:
                # Try to infer type from data structure
                if "market" in data and ("size" in data or "quantity" in data):
                    await self.on_position_update(data)
                elif "id" in data and ("status" in data or "orderStatus" in data):
                    await self.on_order_update(data)
                elif "available" in data or "total" in data:
                    await self._handle_balance_update(data)
                else:
                    logger.debug(f"[X10 Stream] Unknown account message type: {msg_type}, keys: {list(data.keys())}")
                    
        except Exception as e:
            logger.error(f"[X10 Stream] Error handling account message: {e}")
    
    async def _handle_funding_stream_message(self, data: Dict[str, Any]) -> None:
        """
        Handle funding rate updates from stream.
        
        Updates funding_cache with real-time rates.
        """
        try:
            # Extract market and funding rate
            market = data.get("market") or data.get("marketName") or data.get("symbol")
            funding_rate = data.get("fundingRate") or data.get("funding_rate") or data.get("rate")
            
            if market and funding_rate is not None:
                rate_float = safe_float(funding_rate, 0.0)
                
                # Update cache
                self._funding_cache[market] = rate_float
                self._funding_cache_time[market] = time.time()
                self.funding_cache[market] = rate_float  # Public cache
                
                # Update metrics
                self._stream_metrics['funding_updates'] += 1
                self._stream_metrics['last_update_time'] = time.time()
                
                logger.debug(f"📊 [X10 Stream] Funding rate update: {market} = {rate_float:.6f}")
            
        except Exception as e:
            logger.error(f"[X10 Stream] Error handling funding message: {e}")
    
    async def _handle_balance_update(self, data: Dict[str, Any]) -> None:
        """Handle balance updates from stream"""
        try:
            available = safe_float(data.get("available") or data.get("availableBalance"), 0.0)
            total = safe_float(data.get("total") or data.get("totalBalance"), 0.0)
            
            # Deduplicate balance updates (avoid spam)
            current_time = time.time()
            if available != self._balance_cache or (current_time - self._last_balance_update) > 5.0:
                self._balance_cache = available
                self._last_balance_update = current_time
                
                logger.debug(f"💰 [X10 Stream] Balance update: Available=${available:.2f}, Total=${total:.2f}")
                
        except Exception as e:
            logger.error(f"[X10 Stream] Error handling balance message: {e}")
    
    async def _handle_orderbook_stream_message(self, data: Dict[str, Any], symbol: str) -> None:
        """
        Handle orderbook updates from stream.
        
        Updates orderbook_cache with real-time orderbook data.
        """
        try:
            self._stream_metrics['orderbook_updates'] += 1
            self._stream_metrics['last_update_time'] = time.time()
            
            # Extract bids and asks
            bids = data.get("bids") or data.get("bid") or []
            asks = data.get("asks") or data.get("ask") or []
            
            # Check if this is a snapshot or update
            is_snapshot = data.get("type") == "snapshot" or data.get("event") == "snapshot" or not data.get("delta")
            
            if is_snapshot:
                # Full snapshot
                self.handle_orderbook_snapshot(symbol, bids, asks)
                logger.debug(f"📊 [X10 Stream] Orderbook snapshot: {symbol} ({len(bids)} bids, {len(asks)} asks)")
            else:
                # Incremental update
                self.handle_orderbook_update(symbol, bids, asks)
                logger.debug(f"📊 [X10 Stream] Orderbook update: {symbol}")
            
            # Update mark price from best bid/ask
            if bids and asks:
                try:
                    best_bid = float(bids[0][0] if isinstance(bids[0], list) else bids[0].get('p', 0))
                    best_ask = float(asks[0][0] if isinstance(asks[0], list) else asks[0].get('p', 0))
                    if best_bid > 0 and best_ask > 0:
                        mid_price = (best_bid + best_ask) / 2.0
                        self._price_cache[symbol] = mid_price
                        self._price_cache_time[symbol] = time.time()
                        self.price_cache[symbol] = mid_price
                except (ValueError, IndexError, TypeError):
                    pass
                    
        except Exception as e:
            logger.error(f"[X10 Stream] Error handling orderbook message for {symbol}: {e}")
    
    async def _handle_trade_stream_message(self, data: Dict[str, Any], symbol: str) -> None:
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
            price = safe_float(data.get("price") or data.get("p"), 0.0)
            size = safe_float(data.get("size") or data.get("q") or data.get("quantity"), 0.0)
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
                    logger.debug(f"📈 [X10 Stream] Trade: {symbol} {side} {size:.6f} @ ${price:.2f}")
            
        except Exception as e:
            logger.error(f"[X10 Stream] Error handling trade message for {symbol}: {e}")

    async def _handle_candle_stream_message(self, data: Dict[str, Any], symbol: str) -> None:
        """
        Handle candle updates from stream.

        Stores the most recent candle per symbol for downstream analysis.
        """
        try:
            self._stream_metrics['last_update_time'] = time.time()

            candles = data.get("candles") or data.get("data") or data.get("candle") or data
            candle = None

            if isinstance(candles, list) and candles:
                candle = candles[-1]
            elif isinstance(candles, dict):
                candle = candles

            if not candle:
                return

            self._candle_cache[symbol] = candle
            self._candle_cache_time[symbol] = time.time()
        except Exception as e:
            logger.error(f"[X10 Stream] Error handling candle message for {symbol}: {e}")
    
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
                    account_rate = self._stream_metrics['account_updates'] / time_since_start
                    orderbook_rate = self._stream_metrics['orderbook_updates'] / time_since_start
                    trade_rate = self._stream_metrics['trade_updates'] / time_since_start
                    funding_rate = self._stream_metrics['funding_updates'] / time_since_start
                else:
                    account_rate = orderbook_rate = trade_rate = funding_rate = 0.0
                
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
                    'account': account_rate,
                    'orderbook': orderbook_rate,
                    'trade': trade_rate,
                    'funding': funding_rate
                }
                self._stream_metrics['time_since_last_update'] = time_since_last_update
                
                # Log health status periodically
                if random.random() < 0.1:  # 10% chance to log
                    logger.info(
                        f"📊 [X10 Stream Health] Status: {health} | "
                        f"Connected: {is_connected} | "
                        f"Rates: A={account_rate:.1f}/min, OB={orderbook_rate:.1f}/min, "
                        f"T={trade_rate:.1f}/min, F={funding_rate:.1f}/min | "
                        f"Last update: {time_since_last_update:.1f}s ago"
                    )
                
                # Warn if unhealthy
                if health in ['stale', 'disconnected']:
                    logger.warning(f"⚠️ [X10 Stream] Health check: {health} (last update: {time_since_last_update:.1f}s ago)")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[X10 Stream] Health check error: {e}")
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

    async def _get_auth_client(self) -> PerpetualTradingClient:
        """Return an authenticated client for account operations."""
        if not self._auth_client:
            if not self.stark_account:
                raise RuntimeError("X10 Stark account missing")
            # FIX: Direktaufruf des Konstruktors statt .create()
            self._auth_client = PerpetualTradingClient(
                self.client_env, 
                self.stark_account
            )
        return self._auth_client

    async def _get_trading_client(self) -> PerpetualTradingClient:
        """Return an authenticated trading client for order placement."""
        if not self.trading_client:
            if not self.stark_account:
                raise RuntimeError("X10 Stark account missing - cannot create trading client")
            # FIX: Direktaufruf des Konstruktors statt .create()
            self.trading_client = PerpetualTradingClient(
                self.client_env, 
                self.stark_account
            )
        return self.trading_client

    async def _poll_funding_rates(self):
        """Polling fallback for funding rates"""
        interval = max(5, getattr(config, 'FUNDING_CACHE_TTL', 60) // 4)
        while True:
            try:
                await self.load_market_cache(force=False)
            except Exception as e:
                logger.debug(f"X10: poll_funding_rates error: {e}")
            await asyncio.sleep(interval)

    async def _poll_mark_prices(self):
        """Polling fallback for mark prices"""
        interval = max(3, int(getattr(config, 'REFRESH_DELAY_SECONDS', 3)))
        while True:
            try:
                await self.refresh_missing_prices()
            except Exception as e:
                logger.debug(f"X10: poll_mark_prices error: {e}")
            await asyncio.sleep(interval)

    async def load_market_cache(self, force: bool = False):
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - skip during shutdown
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[X10] Shutdown active - skipping load_market_cache")
            return
            
        if self.market_info and not force:
            return

        client = PerpetualTradingClient(self.client_env)
        try:
            result = await self.rate_limiter.acquire()
            # FIX: Check if rate limiter was cancelled (shutdown)
            if result < 0:
                logger.debug(f"[X10] Rate limiter cancelled during load_market_cache - skipping")
                return
            resp = await client.markets_info.get_markets()
            if resp and resp.data:
                for m in resp.data:
                    name = getattr(m, "name", "")
                    if name.endswith("-USD"):
                        self.market_info[name] = m

                        if hasattr(m, 'market_stats') and hasattr(m.market_stats, 'funding_rate'):
                            rate = getattr(m.market_stats, 'funding_rate', None)
                            if rate is not None:
                                self.funding_cache[name] = float(rate)

                        if hasattr(m, 'market_stats'):
                            stats = m.market_stats
                            price = None
                            for field in ['mark_price', 'index_price', 'last_price', 'price']:
                                if hasattr(stats, field):
                                    val = getattr(stats, field, None)
                                    if val is not None and float(val) > 0:
                                        price = float(val)
                                        break
                            if price and price > 0:
                                if name not in self.price_cache or self.price_cache.get(name, 0) == 0:
                                    self.price_cache[name] = price

            logger.info(f" X10: {len(self.market_info)} Märkte geladen, {len(self.funding_cache)} Funding Rates, {len(self.price_cache)} Preise")
            self.rate_limiter.on_success()
        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.on_429()
            logger.error(f" X10 Market Cache Error: {e}")
        finally:
            # client.close() might be async or sync depending on version, handle safely
            try:
                if hasattr(client, 'close'):
                    res = client.close()
                    if inspect.isawaitable(res):
                        await res
            except Exception:
                pass

    def fetch_mark_price(self, symbol: str):
        """Mark Price: Prioritize WebSocket Cache (_price_cache)"""
        # 1. Zuerst im Echtzeit-Cache (WebSocket) schauen
        if symbol in self._price_cache:
            age = time.time() - self._price_cache_time.get(symbol, 0)
            if age < 60:  # Nur nutzen wenn Daten jünger als 60s
                price = self._price_cache[symbol]
                if price and price > 0:
                    return price

        # 2. Fallback auf REST Cache
        if symbol in self.price_cache:
            price = self.price_cache[symbol]
            if price and price > 0:
                return price
            
        # 3. Fallback auf Market Info Stats
        m = self.market_info.get(symbol)
        if m and hasattr(m, 'market_stats') and hasattr(m.market_stats, 'mark_price'):
            price = getattr(m.market_stats, 'mark_price', None)
            if price is not None:
                price_float = float(price)
                if price_float > 0:
                    # Cache aktualisieren für nächstes Mal
                    self.price_cache[symbol] = price_float
                    self._price_cache[symbol] = price_float
                    self._price_cache_time[symbol] = time.time()
                    return price_float
        
        # 4. Try other price fields from market_stats
        if m and hasattr(m, 'market_stats'):
            stats = m.market_stats
            for field in ['index_price', 'last_price', 'last_trade_price', 'price']:
                if hasattr(stats, field):
                    price = getattr(stats, field, None)
                    if price is not None:
                        try:
                            price_float = float(price)
                            if price_float > 0:
                                # Cache aktualisieren
                                self.price_cache[symbol] = price_float
                                self._price_cache[symbol] = price_float
                                self._price_cache_time[symbol] = time.time()
                                return price_float
                        except (ValueError, TypeError):
                            continue
        
        return None

    def fetch_funding_rate(self, symbol: str):
        """Funding Rate: Prioritize WebSocket Cache (_funding_cache)"""
        # 1. Zuerst im Echtzeit-Cache (WebSocket) schauen
        if symbol in self._funding_cache:
            return self._funding_cache[symbol]  # X10 liefert meist Hourly Rates

        # 2. Fallback auf REST Cache
        if symbol in self.funding_cache:
            return self.funding_cache[symbol]
            
        # 3. Fallback auf Market Info
        m = self.market_info.get(symbol)
        if m and hasattr(m, 'market_stats') and hasattr(m.market_stats, 'funding_rate'):
            rate = getattr(m.market_stats, 'funding_rate', None)
            if rate is not None:
                rate_float = float(rate)
                # Falls X10 Rates als 8h Rates liefert (z.B. dYdX Style), 
                # müssten wir durch 8 teilen um auf Hourly zu kommen.
                # Aktuell gehen wir von Hourly aus.
                self.funding_cache[symbol] = rate_float
                return rate_float
        return None

    def fetch_24h_vol(self, symbol: str) -> float:
        try:
            m = self.market_info.get(symbol)
            if m:
                return abs(float(getattr(m.market_stats, "price_change_24h_pct", 0)))
        except:
            pass
        return 0.0

    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> dict:
        """
        Fetch orderbook from X10 REST API
        
        API: GET /api/v1/info/markets/{market}/orderbook
        
        Returns:
            {
                'bids': [[price, qty], ...],  # sorted by price DESC
                'asks': [[price, qty], ...],  # sorted by price ASC
                'timestamp': int
            }
        """
        # 1. Prioritize WebSocket Cache if fresh
        if symbol in self.orderbook_cache:
            cache = self.orderbook_cache[symbol]
            ts = cache.get("timestamp", 0)
            # Use 2s TTL for orderbook to prefer WS but allow fast fallback
            if (time.time() * 1000) - ts < 2000:
                 return cache

        try:
            # Rate limit
            await self.rate_limiter.acquire()
            
            # X10 uses BTC-USD format
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/info/markets/{symbol}/orderbook"
            
            session = await self._get_session()
            headers = {"Accept": "application/json", "User-Agent": "FundingBot/1.0"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        if data.get("status") == "OK" and "data" in data:
                            ob_data = data["data"]
                            
                            # Parse bids: [{"qty": "0.04852", "price": "61827.7"}, ...]
                            bids = [
                                [float(b["price"]), float(b["qty"])] 
                                for b in ob_data.get("bid", [])[:limit]
                            ]
                            
                            # Parse asks: [{"qty": "0.04852", "price": "61840.3"}, ...]
                            asks = [
                                [float(a["price"]), float(a["qty"])] 
                                for a in ob_data.get("ask", [])[:limit]
                            ]
                            
                            result = {
                                "bids": bids,
                                "asks": asks,
                                "timestamp": int(time.time() * 1000),
                                "symbol": symbol
                            }
                            
                            # Cache for prediction
                            self.orderbook_cache[symbol] = result
                            self.rate_limiter.on_success()
                            
                            return result
                        else:
                            logger.debug(f"X10 orderbook {symbol}: Invalid response format")
                    else:
                        if resp.status == 429:
                            self.rate_limiter.penalize_429()
                        logger.debug(f"X10 orderbook {symbol}: HTTP {resp.status}")
                        
        except asyncio.TimeoutError:
            logger.debug(f"X10 orderbook {symbol}: Timeout")
        except Exception as e:
            logger.debug(f"X10 orderbook {symbol}: {e}")
        
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

    def handle_orderbook_snapshot(self, symbol: str, bids: List[List[float]], asks: List[List[float]]):
        """
        Process incoming orderbook snapshot from X10 WebSocket.
        Updates local cache used by fetch_orderbook.
        """
        try:
            # Safe parsing - handle both list [price, size] and dict {'p': price, 'q': size}
            clean_bids = []
            for b in bids:
                try:
                    if isinstance(b, dict):
                        price = float(b.get('p', 0))
                        size = float(b.get('q', 0)) # X10 uses 'q' for quantity
                    else:
                        price = float(b[0])
                        size = float(b[1])
                        
                    if price > 0 and size > 0:
                        clean_bids.append([price, size])
                except (ValueError, IndexError, TypeError):
                    continue
            
            clean_asks = []
            for a in asks:
                try:
                    if isinstance(a, dict):
                        price = float(a.get('p', 0))
                        size = float(a.get('q', 0)) # X10 uses 'q' for quantity
                    else:
                        price = float(a[0])
                        size = float(a[1])

                    if price > 0 and size > 0:
                        clean_asks.append([price, size])
                except (ValueError, IndexError, TypeError):
                    continue
            
            clean_bids.sort(key=lambda x: x[0], reverse=True)
            clean_asks.sort(key=lambda x: x[0])
            
            # Timestamp in milliseconds
            ts = int(time.time() * 1000)
            
            cache_entry = {
                "bids": clean_bids,
                "asks": clean_asks,
                "timestamp": ts,
                "symbol": symbol
            }
            
            # Update cache
            self.orderbook_cache[symbol] = cache_entry
            self._orderbook_cache[symbol] = cache_entry
            self._orderbook_cache_time[symbol] = time.time()
            
            # Log occasional update
            if random.random() < 0.005:
                logger.debug(f"X10 WS OB Snapshot {symbol}: {len(clean_bids)}b/{len(clean_asks)}a")
                
        except Exception as e:
            logger.error(f"X10 WS OB Snapshot Error {symbol}: {e}")

    def handle_orderbook_update(self, symbol: str, bids: List[Any], asks: List[Any]):
        """
        Process incremental orderbook update (DELTA) from X10 WebSocket.
        """
        try:
            # 1. Get current state
            current = self.orderbook_cache.get(symbol)
            if not current:
                return # Can't update what we don't have
            
            # Convert to dictionary for easy O(1) updates: {price: size}
            # Note: stored keys are floats, precise match required
            current_bids = {b[0]: b[1] for b in current['bids']}
            current_asks = {a[0]: a[1] for a in current['asks']}
            
            # 2. Apply Bid Updates
            for b in bids:
                try:
                    if isinstance(b, dict):
                        price = float(b.get('p', 0))
                        size = float(b.get('q', 0))
                    else:
                        price = float(b[0])
                        size = float(b[1])
                    
                    if size <= 0:
                        current_bids.pop(price, None)
                    else:
                        current_bids[price] = size
                except (ValueError, IndexError, TypeError):
                    continue
            
            # 3. Apply Ask Updates
            for a in asks:
                try:
                    if isinstance(a, dict):
                        price = float(a.get('p', 0))
                        size = float(a.get('q', 0))
                    else:
                        price = float(a[0])
                        size = float(a[1])
                        
                    if size <= 0:
                        current_asks.pop(price, None)
                    else:
                        current_asks[price] = size
                except (ValueError, IndexError, TypeError):
                    continue
            
            # 4. Reconstruct and Sort Lists
            # Bids: Descending
            new_bids = [[p, s] for p, s in current_bids.items()]
            new_bids.sort(key=lambda x: x[0], reverse=True)
            
            # Asks: Ascending
            new_asks = [[p, s] for p, s in current_asks.items()]
            new_asks.sort(key=lambda x: x[0])
            
            # 5. Update Cache
            ts = int(time.time() * 1000)
            cache_entry = {
                "bids": new_bids,
                "asks": new_asks,
                "timestamp": ts,
                "symbol": symbol
            }
            
            self.orderbook_cache[symbol] = cache_entry
            self._orderbook_cache[symbol] = cache_entry
            self._orderbook_cache_time[symbol] = time.time()
            
        except Exception as e:
            logger.error(f"X10 WS OB Delta Error {symbol}: {e}")

    async def fetch_open_interest(self, symbol: str) -> float:
        if not hasattr(self, '_oi_cache'):
            self._oi_cache = {}
            self._oi_cache_time = {}
        
        now = time.time()
        if symbol in self._oi_cache:
            if now - self._oi_cache_time.get(symbol, 0) < 60.0:
                return self._oi_cache[symbol]
        
        try:
            market = self.market_info.get(symbol)
            if market and hasattr(market, 'market_stats'):
                stats = market.market_stats
                
                if hasattr(stats, 'open_interest'):
                    oi = float(stats.open_interest)
                    self._oi_cache[symbol] = oi
                    self._oi_cache_time[symbol] = now
                    return oi
                
                if hasattr(stats, 'total_volume'):
                    vol = float(stats.total_volume)
                    self._oi_cache[symbol] = vol
                    self._oi_cache_time[symbol] = now
                    return vol
            return 0.0
        except Exception:
            return 0.0

    def get_24h_change_pct(self, symbol: str = "BTC-USD") -> float:
        try:
            m = self.market_info.get(symbol)
            if m and hasattr(m, "market_stats"):
                val = getattr(m.market_stats, "price_change_24h_pct", 0)
                return float(str(val)) * 100.0
        except:
            pass
        return 0.0

    def min_notional_usd(self, symbol: str) -> float:
        HARD_MIN_USD = 10.0
        SAFETY_BUFFER = 1.05
        
        m = self.market_info.get(symbol)
        if not m:
            return HARD_MIN_USD

        try:
            price = safe_decimal(self.fetch_mark_price(symbol))
            if price <= 0:
                return HARD_MIN_USD
            
            min_size = safe_decimal(getattr(m.trading_config, "min_order_size", "0"))
            # Calc in Decimal then convert to float for return
            api_min_usd = float(min_size * price)
            safe_min = api_min_usd * SAFETY_BUFFER
            # Entferne das harte Oberlimit, nutze echte API-Grenzen + Sicherheitsaufschlag
            return max(HARD_MIN_USD, safe_min)
        except Exception:
            return HARD_MIN_USD

    async def get_positions_history(
        self,
        symbol: Optional[str] = None,
        position_side: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get historical positions (closed positions).
        
        Returns a list of all closed positions with their PnL, entry/exit prices,
        and other details. Useful for:
        - Performance analysis
        - Backtesting
        - Compliance & reporting
        - Debugging trade exits
        
        Args:
            symbol: Optional market symbol to filter by (e.g., "BTC-USD")
            position_side: Optional position side filter ("LONG" or "SHORT")
            limit: Maximum number of positions to return (default: 100)
            cursor: Pagination cursor for fetching more results
        
        Returns:
            List of position dictionaries with:
            - market: Symbol
            - side: LONG or SHORT
            - size: Position size
            - entryPrice: Entry price
            - exitPrice: Exit price
            - pnl: Realized PnL
            - fundingCollected: Total funding collected
            - openedAt: Opening timestamp
            - closedAt: Closing timestamp
            - closeReason: Reason for closing
            - realisedPnlBreakdown: Detailed PnL breakdown (if available)
        """
        try:
            if not self.stark_account:
                logger.warning("⚠️ [X10 Position History] No stark_account configured")
                return []
            
            def get_val(obj, key, default=None):
                """Helper to access nested attributes or dict keys"""
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)
            
            await self.rate_limiter.acquire()
            
            # Try SDK method first
            client = await self._get_auth_client()
            data = []
            
            if client and hasattr(client, 'account'):
                # Try SDK method (may not be available in all SDK versions)
                method = None
                for method_name in ['get_positions_history', 'getPositionsHistory', 'positions_history']:
                    if hasattr(client.account, method_name):
                        method = getattr(client.account, method_name)
                        break
                
                if method:
                    try:
                        options = {'limit': limit}
                        if symbol:
                            options['marketNames'] = [symbol]
                        if position_side:
                            options['positionSide'] = position_side
                        if cursor is not None:
                            options['cursor'] = cursor
                        
                        result = await method(**options)
                        if result and hasattr(result, 'data') and result.data:
                            data = result.data
                    except Exception as e:
                        logger.debug(f"[X10 Position History] SDK method failed: {e}, trying REST fallback")
            
            # Fallback to direct REST API if SDK method not available or failed
            if not data:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                url = f"{base_url}/api/v1/user/positions/history"
                
                headers = {
                    "X-Api-Key": self.stark_account.api_key,
                    "Accept": "application/json"
                }
                
                params = {"limit": str(limit)}
                if symbol:
                    params["market"] = symbol
                if position_side:
                    params["side"] = position_side
                if cursor is not None:
                    params["cursor"] = str(cursor)
                
                session = await self._get_session()
                async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        json_resp = await resp.json()
                        data = json_resp.get('data', [])
                    else:
                        logger.warning(f"⚠️ [X10 Position History] REST API returned {resp.status}")
                        return []
            
            # Parse positions
            positions = []
            for p in data:
                try:
                    position_dict = {
                        'market': get_val(p, 'market') or get_val(p, 'marketName', 'UNKNOWN'),
                        'side': get_val(p, 'side') or get_val(p, 'positionSide', 'UNKNOWN'),
                        'size': safe_float(get_val(p, 'size'), 0.0),
                        'entryPrice': safe_float(get_val(p, 'entryPrice') or get_val(p, 'entry_price'), 0.0),
                        'exitPrice': safe_float(get_val(p, 'exitPrice') or get_val(p, 'exit_price'), 0.0),
                        'pnl': safe_float(get_val(p, 'pnl') or get_val(p, 'realisedPnl') or get_val(p, 'realised_pnl'), 0.0),
                        'fundingCollected': safe_float(
                            get_val(p, 'fundingCollected') or get_val(p, 'funding_collected'), 0.0
                        ),
                        'openedAt': get_val(p, 'openedAt') or get_val(p, 'opened_at'),
                        'closedAt': get_val(p, 'closedAt') or get_val(p, 'closed_at'),
                        'closeReason': get_val(p, 'closeReason') or get_val(p, 'close_reason', 'UNKNOWN')
                    }
                    
                    # Include PnL breakdown if available
                    breakdown = get_val(p, 'realisedPnlBreakdown') or get_val(p, 'realised_pnl_breakdown')
                    if breakdown:
                        position_dict['realisedPnlBreakdown'] = {
                            'tradePnl': safe_float(get_val(breakdown, 'tradePnl') or get_val(breakdown, 'trade_pnl'), 0.0),
                            'fundingFees': safe_float(get_val(breakdown, 'fundingFees') or get_val(breakdown, 'funding_fees'), 0.0),
                            'openFees': safe_float(get_val(breakdown, 'openFees') or get_val(breakdown, 'open_fees'), 0.0),
                            'closeFees': safe_float(get_val(breakdown, 'closeFees') or get_val(breakdown, 'close_fees'), 0.0)
                        }
                    
                    positions.append(position_dict)
                except Exception as e:
                    logger.debug(f"[X10 Position History] Error parsing position: {e}")
                    continue
            
            if positions:
                logger.info(f"✅ [X10 Position History] Retrieved {len(positions)} historical positions")
            else:
                logger.debug(f"[X10 Position History] No historical positions found")
            
            return positions
                
        except Exception as e:
            logger.error(f"❌ [X10 Position History] Exception: {e}", exc_info=True)
            return []

    async def fetch_open_positions(self) -> list:
        """
        Optimized fetch_open_positions with Pure WebSocket Cache Strategy.
        Eliminates REST Polling Lag in Main Loop.
        """
        if not self.stark_account:
            logger.warning("X10: No stark_account configured")
            return []

        # ═══════════════════════════════════════════════════════════════
        # STRATEGY: WebSocket Cache Priority
        # We trust the cache once it has been loaded (via REST or WS).
        # This prevents polling loop for users with 0 positions.
        # ═══════════════════════════════════════════════════════════════

        # 1. CACHE CHECK (with Loaded Flag)
        is_loaded = getattr(self, '_positions_loaded', False)
        
        # If we have loaded data (even if empty), return it immediately (0ms latency!)
        if is_loaded and hasattr(self, '_positions_cache'):
            if random.random() < 0.001: # Debug rare
                 logger.debug(f"[X10]Serving positions from WS Cache ({len(self._positions_cache)})")
            return self._positions_cache
            
        # 2. SHUTDOWN SAFETY
        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
        if is_shutting_down:
            return []

        # 3. INITIAL LOAD (REST FALLBACK)
        # Only reached if we have NEVER loaded positions
        if self.rate_limiter.is_duplicate("X10:fetch_open_positions"):
             return getattr(self, '_positions_cache', [])

        try:
            logger.debug("[X10] Initial Load: Fetching positions via REST...")
            client = await self._get_auth_client()
            result = await self.rate_limiter.acquire()
            
            if result < 0: return [] 

            resp = await client.account.get_positions()

            positions = []
            if resp and resp.data:
                for p in resp.data:
                    status = getattr(p, 'status', 'UNKNOWN')
                    size = float(getattr(p, 'size', 0))
                    symbol = getattr(p, 'market', 'UNKNOWN')

                    if status == "OPENED" and abs(size) > 1e-8:
                        try:
                            raw_open = getattr(p, 'open_price', None)
                            if raw_open is None:
                                raw_open = getattr(p, 'openPrice', None)
                            entry_price = float(raw_open) if raw_open else 0.0
                        except Exception:
                            entry_price = 0.0
                        positions.append({
                            "symbol": symbol,
                            "size": size,
                            "entry_price": entry_price
                        })
            
            # Update Cache & Set Loaded Flag
            self._positions_cache = positions
            self._positions_loaded = True
            
            if len(positions) > 0:
                logger.info(f"[X10] Initial Positions loaded via REST: {len(positions)}")
            else:
                logger.debug(f"[X10] Initial Positions Check: 0 positions found.")
                
            return positions

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            logger.error(f"X10 Positions Error: {e}")
            return getattr(self, '_positions_cache', [])

    async def get_orders_history(
        self,
        symbol: Optional[str] = None,
        order_type: Optional[str] = None,
        order_side: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get historical orders (all orders: filled, cancelled, rejected).
        
        Returns a list of all historical orders with their status, fill information,
        fees, and other details. Useful for:
        - Fill-rate analysis (how many orders were successfully filled?)
        - Fee tracking (actual vs expected fees)
        - Order performance (average fill time, slippage)
        - Debugging (why orders failed/rejected)
        
        Args:
            symbol: Optional market symbol to filter by (e.g., "BTC-USD")
            order_type: Optional order type filter (e.g., "LIMIT", "MARKET")
            order_side: Optional order side filter ("BUY" or "SELL")
            limit: Maximum number of orders to return (default: 100)
            cursor: Pagination cursor for fetching more results
        
        Returns:
            List of order dictionaries with:
            - id: Order ID
            - market: Symbol
            - side: BUY or SELL
            - type: Order type (LIMIT, MARKET, etc.)
            - price: Limit price
            - size: Order size
            - filledSize: Filled size
            - avgFillPrice: Average fill price
            - status: Order status (FILLED, CANCELLED, REJECTED, etc.)
            - createdAt: Creation timestamp
            - filledAt: Fill timestamp (if filled)
            - cancelledAt: Cancel timestamp (if cancelled)
            - fee: Trading fee
            - rejectReason: Reason for rejection (if rejected)
        """
        try:
            if not self.stark_account:
                logger.warning("⚠️ [X10 Orders History] No stark_account configured")
                return []
            
            def get_val(obj, key, default=None):
                """Helper to access nested attributes or dict keys"""
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)
            
            await self.rate_limiter.acquire()
            
            # Try SDK method first
            client = await self._get_auth_client()
            data = []
            
            if client and hasattr(client, 'account'):
                # Try SDK method (may not be available in all SDK versions)
                method = None
                for method_name in ['get_orders_history', 'getOrdersHistory', 'orders_history']:
                    if hasattr(client.account, method_name):
                        method = getattr(client.account, method_name)
                        break
                
                if method:
                    try:
                        options = {'limit': limit}
                        if symbol:
                            options['marketNames'] = [symbol]
                        if order_type:
                            options['orderType'] = order_type
                        if order_side:
                            options['orderSide'] = order_side
                        if cursor is not None:
                            options['cursor'] = cursor
                        
                        result = await method(**options)
                        if result and hasattr(result, 'data') and result.data:
                            data = result.data
                    except Exception as e:
                        logger.debug(f"[X10 Orders History] SDK method failed: {e}, trying REST fallback")
            
            # Fallback to direct REST API if SDK method not available or failed
            if not data:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                url = f"{base_url}/api/v1/user/orders/history"
                
                headers = {
                    "X-Api-Key": self.stark_account.api_key,
                    "Accept": "application/json"
                }
                
                params = {"limit": str(limit)}
                if symbol:
                    params["market"] = symbol
                if order_type:
                    params["type"] = order_type
                if order_side:
                    params["side"] = order_side
                if cursor is not None:
                    params["cursor"] = str(cursor)
                
                session = await self._get_session()
                async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        json_resp = await resp.json()
                        data = json_resp.get('data', [])
                    else:
                        logger.warning(f"⚠️ [X10 Orders History] REST API returned {resp.status}")
                        return []
            
            # Parse orders
            orders = []
            for o in data:
                try:
                    # Normalize side
                    side_raw = get_val(o, 'side', 'UNKNOWN')
                    side_str = str(side_raw).upper()
                    if "BUY" in side_str:
                        side = "BUY"
                    elif "SELL" in side_str:
                        side = "SELL"
                    else:
                        side = side_str
                    
                    order_dict = {
                        'id': str(get_val(o, 'id', '')),
                        'market': get_val(o, 'market') or get_val(o, 'marketName', 'UNKNOWN'),
                        'side': side,
                        'type': get_val(o, 'type') or get_val(o, 'orderType', 'UNKNOWN'),
                        'price': safe_float(get_val(o, 'price'), 0.0),
                        'size': safe_float(
                            get_val(o, 'size') or get_val(o, 'amount') or get_val(o, 'amount_of_synthetic'), 0.0
                        ),
                        'filledSize': safe_float(
                            get_val(o, 'filledSize') or get_val(o, 'filled_size') or get_val(o, 'filledQty'), 0.0
                        ),
                        'avgFillPrice': safe_float(
                            get_val(o, 'avgFillPrice') or get_val(o, 'avg_fill_price'), 0.0
                        ),
                        'status': get_val(o, 'status', 'UNKNOWN'),
                        'createdAt': get_val(o, 'createdAt') or get_val(o, 'created_at'),
                        'filledAt': get_val(o, 'filledAt') or get_val(o, 'filled_at'),
                        'cancelledAt': get_val(o, 'cancelledAt') or get_val(o, 'cancelled_at'),
                        'fee': safe_float(get_val(o, 'fee'), 0.0),
                        'rejectReason': get_val(o, 'rejectReason') or get_val(o, 'reject_reason')
                    }
                    
                    # Calculate fill percentage
                    if order_dict['size'] > 0:
                        order_dict['fillPercentage'] = (order_dict['filledSize'] / order_dict['size']) * 100.0
                    else:
                        order_dict['fillPercentage'] = 0.0
                    
                    # Calculate slippage if filled
                    if order_dict['avgFillPrice'] > 0 and order_dict['price'] > 0:
                        if side == "BUY":
                            slippage = order_dict['avgFillPrice'] - order_dict['price']  # Positive = worse
                        else:  # SELL
                            slippage = order_dict['price'] - order_dict['avgFillPrice']  # Positive = worse
                        order_dict['slippage'] = slippage
                        order_dict['slippagePercent'] = (slippage / order_dict['price']) * 100.0
                    else:
                        order_dict['slippage'] = 0.0
                        order_dict['slippagePercent'] = 0.0
                    
                    orders.append(order_dict)
                except Exception as e:
                    logger.debug(f"[X10 Orders History] Error parsing order: {e}")
                    continue
            
            if orders:
                logger.info(f"✅ [X10 Orders History] Retrieved {len(orders)} historical orders")
            else:
                logger.debug(f"[X10 Orders History] No historical orders found")
            
            return orders
                
        except Exception as e:
            logger.error(f"❌ [X10 Orders History] Exception: {e}", exc_info=True)
            return []

    async def get_trades_history(
        self,
        symbols: List[str],
        trade_side: Optional[str] = None,
        trade_type: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get historical trades (all fills/executions).
        
        Returns a list of all historical trades with their fill prices, sizes,
        fees, and other details. Useful for:
        - Precise PnL calculation (based on actual fill prices, not limit prices)
        - Slippage tracking (limit price vs fill price)
        - Trade analysis (which trades were profitable?)
        - Reconciliation (compare bot data vs exchange data)
        
        Args:
            symbols: List of market symbols to filter by (e.g., ["BTC-USD", "ETH-USD"])
            trade_side: Optional trade side filter ("BUY" or "SELL")
            trade_type: Optional trade type filter
            limit: Maximum number of trades to return (default: 100)
            cursor: Pagination cursor for fetching more results
        
        Returns:
            List of trade dictionaries with:
            - id: Trade ID
            - market: Symbol
            - side: BUY or SELL
            - price: Fill price
            - size: Trade size
            - fee: Trading fee
            - timestamp: Trade timestamp
            - orderId: Associated order ID
            - type: Trade type
        """
        try:
            if not self.stark_account:
                logger.warning("⚠️ [X10 Trades History] No stark_account configured")
                return []
            
            if not symbols or not isinstance(symbols, list):
                logger.warning("⚠️ [X10 Trades History] symbols must be a non-empty list")
                return []
            
            def get_val(obj, key, default=None):
                """Helper to access nested attributes or dict keys"""
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)
            
            await self.rate_limiter.acquire()
            
            # Try SDK method first
            client = await self._get_auth_client()
            data = []
            
            if client and hasattr(client, 'account'):
                # Try SDK method (may not be available in all SDK versions)
                method = None
                for method_name in ['get_trades', 'getTrades', 'trades']:
                    if hasattr(client.account, method_name):
                        method = getattr(client.account, method_name)
                        break
                
                if method:
                    try:
                        options = {
                            'marketNames': symbols,
                            'limit': limit
                        }
                        if trade_side:
                            options['tradeSide'] = trade_side
                        if trade_type:
                            options['tradeType'] = trade_type
                        if cursor is not None:
                            options['cursor'] = cursor
                        
                        result = await method(**options)
                        if result and hasattr(result, 'data') and result.data:
                            data = result.data
                    except Exception as e:
                        logger.debug(f"[X10 Trades History] SDK method failed: {e}, trying REST fallback")
            
            # Fallback to direct REST API if SDK method not available or failed
            if not data:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                url = f"{base_url}/api/v1/user/trades"
                
                headers = {
                    "X-Api-Key": self.stark_account.api_key,
                    "Accept": "application/json"
                }
                
                params = {
                    "market": symbols,  # API expects array
                    "limit": str(limit)
                }
                if trade_side:
                    params["side"] = trade_side
                if trade_type:
                    params["type"] = trade_type
                if cursor is not None:
                    params["cursor"] = str(cursor)
                
                session = await self._get_session()
                async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        json_resp = await resp.json()
                        data = json_resp.get('data', [])
                    else:
                        logger.warning(f"⚠️ [X10 Trades History] REST API returned {resp.status}")
                        return []
            
            # Parse trades
            trades = []
            for t in data:
                try:
                    # Normalize side
                    side_raw = get_val(t, 'side', 'UNKNOWN')
                    side_str = str(side_raw).upper()
                    if "BUY" in side_str:
                        side = "BUY"
                    elif "SELL" in side_str:
                        side = "SELL"
                    else:
                        side = side_str
                    
                    trade_dict = {
                        'id': str(get_val(t, 'id', '')),
                        'market': get_val(t, 'market') or get_val(t, 'marketName', 'UNKNOWN'),
                        'side': side,
                        'price': safe_float(get_val(t, 'price'), 0.0),
                        'size': safe_float(
                            get_val(t, 'size') or get_val(t, 'quantity') or get_val(t, 'qty'), 0.0
                        ),
                        'fee': safe_float(get_val(t, 'fee'), 0.0),
                        'timestamp': get_val(t, 'timestamp') or get_val(t, 'ts') or get_val(t, 'time'),
                        'orderId': str(get_val(t, 'orderId') or get_val(t, 'order_id', '')),
                        'type': get_val(t, 'type') or get_val(t, 'tradeType', 'UNKNOWN')
                    }
                    
                    # Calculate notional value
                    if trade_dict['price'] > 0 and trade_dict['size'] > 0:
                        trade_dict['notional'] = trade_dict['price'] * trade_dict['size']
                    else:
                        trade_dict['notional'] = 0.0
                    
                    trades.append(trade_dict)
                except Exception as e:
                    logger.debug(f"[X10 Trades History] Error parsing trade: {e}")
                    continue
            
            if trades:
                logger.info(f"✅ [X10 Trades History] Retrieved {len(trades)} historical trades")
            else:
                logger.debug(f"[X10 Trades History] No historical trades found")
            
            return trades
                
        except Exception as e:
            logger.error(f"❌ [X10 Trades History] Exception: {e}", exc_info=True)
            return []

    async def get_asset_operations(
        self,
        operation_type: Optional[List[str]] = None,
        operation_status: Optional[List[str]] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[int] = None,
        operation_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get asset operations (deposits, withdrawals, transfers).
        
        Returns a list of all asset operations with their details. Useful for:
        - Complete accounting (all money movements, not just trading)
        - Balance tracking over time
        - Compliance & audit trail
        - Debugging balance discrepancies
        
        Args:
            operation_type: Optional list of operation types to filter by
                          (e.g., ["DEPOSIT", "WITHDRAWAL", "TRANSFER"])
            operation_status: Optional list of operation statuses to filter by
                            (e.g., ["COMPLETED", "PENDING", "FAILED"])
            start_time: Optional start timestamp (Unix timestamp in milliseconds)
            end_time: Optional end timestamp (Unix timestamp in milliseconds)
            limit: Maximum number of operations to return (default: 100)
            cursor: Pagination cursor for fetching more results
            operation_id: Optional specific operation ID to fetch
        
        Returns:
            List of operation dictionaries with:
            - id: Operation ID
            - type: Operation type (DEPOSIT, WITHDRAWAL, TRANSFER, etc.)
            - status: Operation status (COMPLETED, PENDING, FAILED, etc.)
            - amount: Operation amount
            - asset: Asset symbol
            - timestamp: Operation timestamp
            - fromAddress: Source address (if applicable)
            - toAddress: Destination address (if applicable)
            - txHash: Transaction hash (if applicable)
        """
        try:
            if not self.stark_account:
                logger.warning("⚠️ [X10 Asset Operations] No stark_account configured")
                return []
            
            def get_val(obj, key, default=None):
                """Helper to access nested attributes or dict keys"""
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)
            
            await self.rate_limiter.acquire()
            
            # Try SDK method first
            client = await self._get_auth_client()
            data = []
            
            if client and hasattr(client, 'account'):
                # Try SDK method (may not be available in all SDK versions)
                method = None
                for method_name in ['asset_operations', 'assetOperations', 'get_asset_operations']:
                    if hasattr(client.account, method_name):
                        method = getattr(client.account, method_name)
                        break
                
                if method:
                    try:
                        options = {'limit': limit}
                        if operation_type:
                            options['operationsType'] = operation_type
                        if operation_status:
                            options['operationsStatus'] = operation_status
                        if start_time is not None:
                            options['startTime'] = start_time
                        if end_time is not None:
                            options['endTime'] = end_time
                        if cursor is not None:
                            options['cursor'] = cursor
                        if operation_id is not None:
                            options['id'] = operation_id
                        
                        result = await method(**options)
                        if result and hasattr(result, 'data') and result.data:
                            data = result.data
                    except Exception as e:
                        logger.debug(f"[X10 Asset Operations] SDK method failed: {e}, trying REST fallback")
            
            # Fallback to direct REST API if SDK method not available or failed
            if not data:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                url = f"{base_url}/api/v1/user/assetOperations"
                
                headers = {
                    "X-Api-Key": self.stark_account.api_key,
                    "Accept": "application/json"
                }
                
                params = {"limit": str(limit)}
                if operation_type:
                    params["type"] = operation_type  # API expects array
                if operation_status:
                    params["status"] = operation_status  # API expects array
                if start_time is not None:
                    params["startTime"] = str(start_time)
                if end_time is not None:
                    params["endTime"] = str(end_time)
                if cursor is not None:
                    params["cursor"] = str(cursor)
                if operation_id is not None:
                    params["id"] = str(operation_id)
                
                session = await self._get_session()
                async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        json_resp = await resp.json()
                        data = json_resp.get('data', [])
                    else:
                        logger.warning(f"⚠️ [X10 Asset Operations] REST API returned {resp.status}")
                        return []
            
            # Parse operations
            operations = []
            for op in data:
                try:
                    operation_dict = {
                        'id': get_val(op, 'id'),
                        'type': get_val(op, 'type') or get_val(op, 'operationType', 'UNKNOWN'),
                        'status': get_val(op, 'status') or get_val(op, 'operationStatus', 'UNKNOWN'),
                        'amount': safe_float(get_val(op, 'amount'), 0.0),
                        'asset': get_val(op, 'asset') or get_val(op, 'currency', 'UNKNOWN'),
                        'timestamp': get_val(op, 'timestamp') or get_val(op, 'time') or get_val(op, 'createdAt'),
                        'fromAddress': get_val(op, 'fromAddress') or get_val(op, 'from_address'),
                        'toAddress': get_val(op, 'toAddress') or get_val(op, 'to_address'),
                        'txHash': get_val(op, 'txHash') or get_val(op, 'tx_hash') or get_val(op, 'transactionHash'),
                        'fee': safe_float(get_val(op, 'fee'), 0.0),
                        'network': get_val(op, 'network') or get_val(op, 'chain', 'UNKNOWN')
                    }
                    
                    operations.append(operation_dict)
                except Exception as e:
                    logger.debug(f"[X10 Asset Operations] Error parsing operation: {e}")
                    continue
            
            if operations:
                logger.info(f"✅ [X10 Asset Operations] Retrieved {len(operations)} asset operations")
            else:
                logger.debug(f"[X10 Asset Operations] No asset operations found")
            
            return operations
                
        except Exception as e:
            logger.error(f"❌ [X10 Asset Operations] Exception: {e}", exc_info=True)
            return []

    async def get_open_orders(self, symbol: str) -> List[dict]:
        """SAFE open orders fetch for compliance check"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return empty list during shutdown
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[X10] Shutdown active - skipping get_open_orders for {symbol}")
            return []
        
        if not self.stark_account:
            return []
        
        try:
            client = await self._get_auth_client()
            orders_resp = None
            # Defensive SDK check (same as cancel_all_orders)
            candidate_methods = ['get_open_orders', 'list_orders', 'get_orders']
            
            for method_name in candidate_methods:
                if hasattr(client.account, method_name):
                    method = getattr(client.account, method_name)
                    try:
                        result = await self.rate_limiter.acquire()
                        # FIX: Check if rate limiter was cancelled (shutdown)
                        if result < 0:
                            logger.debug(f"[X10] Rate limiter cancelled during get_open_orders - returning empty list")
                            return []
                        try:
                            # Try with market filter
                            orders_resp = await method(market_name=symbol)
                        except TypeError:
                            # Need to check rate limiter again for fallback call
                            result = await self.rate_limiter.acquire()
                            if result < 0:
                                logger.debug(f"[X10] Rate limiter cancelled during get_open_orders (fallback) - returning empty list")
                                return []
                            # Try without arguments if filter fails
                            orders_resp = await method()
                        break
                    except Exception:
                        continue
            
            if not orders_resp or not getattr(orders_resp, 'data', None):
                return []
            
            open_orders = []
            for o in orders_resp.data:
                # Filter strict for this symbol and OPEN status
                status = getattr(o, 'status', 'UNKNOWN')
                # market might be 'market' or 'symbol' depending on API version
                market = getattr(o, 'market', getattr(o, 'symbol', ''))
                
                if market == symbol and status in ["PENDING", "OPEN", "NEW"]:
                    # Normalize fields
                    qty = float(getattr(o, 'amount_of_synthetic', getattr(o, 'amount', 0)))
                    price = float(getattr(o, 'price', 0))
                    side_raw = getattr(o, 'side', 'UNKNOWN') # "BUY" / "SELL" or Enum
                    
                    # Side normalization
                    side_str = str(side_raw).upper()
                    if "BUY" in side_str: side = "BUY"
                    elif "SELL" in side_str: side = "SELL"
                    else: side = side_str
                    
                    open_orders.append({
                        "id": str(getattr(o, 'id', '')),
                        "price": price,
                        "size": qty,
                        "side": side,
                        "symbol": symbol
                    })
            return open_orders

        except Exception as e:
            logger.error(f"X10 get_open_orders failed: {e}")
            return []

    async def check_order_filled(
        self,
        symbol: str,
        order_id: str,
        timeout: float = 10.0,
        check_interval: float = 0.5
    ) -> Tuple[bool, Optional[dict]]:
        """
        Check if an X10 order is filled using WebSocket events (EVENT-DRIVEN, not polling!).

        NEW (2025-12-19): Uses wait_for_order_update() to receive real-time WebSocket events
        instead of polling REST API. This eliminates false CANCELLED detections for slow-filling
        MAKER orders.

        Args:
            symbol: Market symbol (e.g. "ONDO-USD")
            order_id: Order ID from place_order response
            timeout: Max time to wait for fill (seconds)
            check_interval: DEPRECATED (kept for backward compatibility, not used)

        Returns:
            (is_filled, fill_info) where fill_info contains:
                - filled_qty: Amount filled
                - avg_fill_price: Average fill price
                - status: Order status (FILLED, CANCELLED, EXPIRED, etc.)
        """
        if not order_id:
            logger.warning(f"⚠️ [X10] check_order_filled: No order_id provided for {symbol}")
            return False, None

        start_time = time.time()
        logger.info(f"⏳ [X10 FILL CHECK] {symbol}: Waiting for WebSocket event (max {timeout}s, order_id={order_id[:16]}...)")
        logger.info(f"   🔌 Using EVENT-DRIVEN detection (not polling!)")

        # PHASE 1: Wait for WebSocket order update event
        try:
            update = await self.wait_for_order_update(order_id, timeout=timeout)
        except Exception as e:
            logger.error(f"❌ [X10 FILL CHECK] {symbol}: Error waiting for order update: {e}")
            update = None

        elapsed = time.time() - start_time

        # PHASE 2: Process WebSocket event result
        if update is None:
            # Timeout - no WebSocket event received
            logger.warning(f"⏳ [X10 FILL CHECK] {symbol}: WebSocket TIMEOUT after {elapsed:.1f}s")
            logger.warning(f"   No FILLED/CANCELLED/EXPIRED event received")

            # Fallback: Check position to be safe
            logger.info(f"   🔍 Fallback: Checking position API...")
            try:
                positions = await self.fetch_open_positions()
                has_position = any(p.get('symbol') == symbol for p in positions)

                if has_position:
                    logger.warning(f"⚠️ [X10 FILL CHECK] {symbol}: TIMEOUT but position exists! (WebSocket missed event?)")
                    position = next((p for p in positions if p.get('symbol') == symbol), None)
                    if position:
                        fill_info = {
                            'filled_qty': abs(float(position.get('size', 0))),
                            'avg_fill_price': float(position.get('entryPrice', 0)),
                            'status': 'FILLED',
                            'position': position
                        }
                        return True, fill_info

                # No position after timeout = order didn't fill
                logger.error(f"❌ [X10 FILL CHECK] {symbol}: TIMEOUT and no position (order likely CANCELLED or EXPIRED)")
                return False, {'status': 'TIMEOUT', 'filled_qty': 0, 'avg_fill_price': 0}

            except Exception as e:
                logger.error(f"❌ [X10 FILL CHECK] {symbol}: Fallback position check failed: {e}")
                return False, {'status': 'TIMEOUT', 'filled_qty': 0, 'avg_fill_price': 0}

        # PHASE 3: Parse WebSocket event
        success = update.get('success', False)
        event_data = update.get('data', {})
        status = (event_data.get('status') or event_data.get('orderStatus') or '').upper()

        logger.info(f"📨 [X10 FILL CHECK] {symbol}: WebSocket event received after {elapsed:.1f}s")
        logger.info(f"   Status: {status} | Success: {success}")

        if success and status in ["FILLED", "PARTIALLY_FILLED"]:
            # Order FILLED!
            logger.info(f"✅ [X10 FILL CHECK] {symbol}: Order FILLED (WebSocket event)")

            # Extract fill info from event
            filled_qty = float(event_data.get('filledQuantity') or event_data.get('filled_qty') or 0)
            avg_price = float(event_data.get('avgFillPrice') or event_data.get('avg_fill_price') or 0)

            # If WebSocket event doesn't have fill details, get from position API
            if filled_qty == 0 or avg_price == 0:
                logger.debug(f"   🔍 WebSocket event missing fill details - fetching from position API...")
                try:
                    positions = await self.fetch_open_positions()
                    position = next((p for p in positions if p.get('symbol') == symbol), None)
                    if position:
                        filled_qty = abs(float(position.get('size', 0)))
                        avg_price = float(position.get('entryPrice', 0))
                except Exception as e:
                    logger.error(f"❌ [X10 FILL CHECK] {symbol}: Failed to fetch position details: {e}")

            fill_info = {
                'filled_qty': filled_qty,
                'avg_fill_price': avg_price,
                'status': status,
                'event_data': event_data
            }
            return True, fill_info

        elif not success and status in ["CANCELLED", "REJECTED", "EXPIRED"]:
            # Order CANCELLED/REJECTED/EXPIRED
            logger.warning(f"❌ [X10 FILL CHECK] {symbol}: Order {status} (WebSocket event)")
            fill_info = {
                'filled_qty': 0,
                'avg_fill_price': 0,
                'status': status,
                'event_data': event_data
            }
            return False, fill_info

        else:
            # Unknown status
            logger.warning(f"⚠️ [X10 FILL CHECK] {symbol}: Unknown WebSocket status: {status}")
            logger.debug(f"   Event data: {event_data}")
            return False, {'status': status or 'UNKNOWN', 'filled_qty': 0, 'avg_fill_price': 0}

    async def refresh_missing_prices(self):
        try:
            missing = [s for s in self.market_info.keys()
                      if s not in self.price_cache or self.price_cache.get(s, 0) == 0.0]

            if not missing:
                return

            logger.info(f"🔄 X10: Refreshing {len(missing)} missing prices: {missing[:10]}{'...' if len(missing) > 10 else ''}")
            await self.load_market_cache(force=True)

            still_missing = [s for s in missing if self.price_cache.get(s, 0) == 0.0]
            
            if still_missing:
                logger.info(f"⚠️ X10: Still missing prices for {len(still_missing)} symbols after cache refresh: {still_missing[:10]}{'...' if len(still_missing) > 10 else ''}")

            if still_missing and len(still_missing) <= 10:
                client = PerpetualTradingClient(self.client_env)
                try:
                    for symbol in still_missing:
                        try:
                            await self.rate_limiter.acquire()
                            resp = await client.order_books.get_order_book(market=symbol)
                            if resp and resp.data:
                                bids = resp.data.bids or []
                                asks = resp.data.asks or []
                                if bids and asks:
                                    best_bid = float(bids[0].price) if bids else 0
                                    best_ask = float(asks[0].price) if asks else 0
                                    if best_bid > 0 and best_ask > 0:
                                        mid_price = (best_bid + best_ask) / 2
                                        # Update both caches
                                        self.price_cache[symbol] = mid_price
                                        self._price_cache[symbol] = mid_price
                                        self._price_cache_time[symbol] = time.time()
                        except Exception as e:
                            logger.debug(f"X10: Orderbook fallback failed for {symbol}: {e}")
                finally:
                    # Safe close
                    try:
                        if hasattr(client, 'close'):
                            res = client.close()
                            if inspect.isawaitable(res):
                                await res
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"X10 refresh_missing_prices error: {e}")

    async def open_live_position(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        reduce_only: bool = False,
        post_only: bool = True,
        amount: Optional[float] = None,
        price: Optional[float] = None,
        previous_order_id: Optional[str] = None,  # FIX: For atomic modify/replace
        external_id: Optional[str] = None,  # For order tracking with external IDs
        **kwargs
    ) -> Tuple[bool, Optional[str]]:
        """Mit manuellem Rate Limiting"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - skip during shutdown (except reduce_only close orders)
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False) and not reduce_only:
            logger.warning(f"⚠️ [X10] SHUTDOWN ACTIVE - Rejecting {symbol} {side} order (non-reduce_only, size=${notional_usd:.2f})")
            return False, None
        
        if not config.LIVE_TRADING:
            return True, None
        
        # Warte auf Token BEVOR Request gesendet wird
        result = await self.rate_limiter.acquire()
        # FIX: Check if rate limiter was cancelled (shutdown)
        # CRITICAL: During shutdown, we MUST still allow reduce_only orders to close positions
        # Regular orders are already blocked by the earlier check, but reduce_only needs to work
        if result < 0 and not reduce_only:
            logger.debug(f"[X10] Rate limiter cancelled during open_live_position - skipping (non-reduce-only)")
            return False, None
        elif result < 0 and reduce_only:
            # Rate limiter was cancelled (shutdown), but we need to close positions
            # Continue anyway - the order placement might still work
            logger.debug(f"[X10] Rate limiter cancelled during open_live_position (reduce_only={reduce_only}) - continuing for position close")
        
        client = await self._get_trading_client()
        market = self.market_info.get(symbol)
        if not market:
            logger.error(f"❌ [X10] Market info missing for {symbol} - cannot place order")
            return False, None

        # ═══════════════════════════════════════════════════════════════
        # FIX: POST_ONLY Orders should use orderbook prices to avoid Taker fills
        # For POST_ONLY BUY: Use bid price to ensure Maker fill
        # For POST_ONLY SELL: Use ask price to ensure Maker fill
        # For Market orders: Use mark price + slippage as before
        # ═══════════════════════════════════════════════════════════════
        raw_price = None

        # Explicit price override (used for shutdown reduce-only closes)
        if price is not None:
            try:
                raw_price = safe_decimal(price)
            except Exception:
                raw_price = None
        
        if raw_price is None and post_only:
            # Get orderbook prices for POST_ONLY orders
            try:
                orderbook = await self.fetch_orderbook(symbol, limit=1)
                bids = orderbook.get("bids", [])
                asks = orderbook.get("asks", [])
                
                # Get tick_size for price adjustment
                tick_size = safe_decimal(getattr(market.trading_config, "min_price_change", "0.01"))
                
                if side == "BUY" and bids and len(bids) > 0:
                    # For POST_ONLY BUY: Place ONE TICK BELOW best bid to ensure Maker fill
                    bid_data = bids[0]
                    if isinstance(bid_data, (list, tuple)) and len(bid_data) > 0:
                        best_bid = safe_decimal(bid_data[0])
                    elif isinstance(bid_data, dict):
                        best_bid = safe_decimal(bid_data.get("p", bid_data.get("price", 0)))
                    else:
                        best_bid = safe_decimal(bid_data) if isinstance(bid_data, (int, float, str)) else Decimal(0)
                    
                    if best_bid > 0:
                        # Place ONE TICK BELOW best bid - ensures Maker fill (or doesn't fill immediately)
                        raw_price = best_bid - tick_size
                        if raw_price <= 0:
                            raw_price = best_bid  # Fallback if tick_size too large
                        logger.debug(f"[X10 POST_ONLY BUY] {symbol}: Using orderbook bid=${best_bid} - 1 tick = ${raw_price} for POST_ONLY limit order (ensures Maker)")
                elif side == "SELL" and asks and len(asks) > 0:
                    # For POST_ONLY SELL: Place ONE TICK ABOVE best ask to ensure Maker fill
                    ask_data = asks[0]
                    if isinstance(ask_data, (list, tuple)) and len(ask_data) > 0:
                        best_ask = safe_decimal(ask_data[0])
                    elif isinstance(ask_data, dict):
                        best_ask = safe_decimal(ask_data.get("p", ask_data.get("price", 0)))
                    else:
                        best_ask = safe_decimal(ask_data) if isinstance(ask_data, (int, float, str)) else Decimal(0)
                    
                    if best_ask > 0:
                        # Place ONE TICK ABOVE best ask - ensures Maker fill (or doesn't fill immediately)
                        raw_price = best_ask + tick_size
                        logger.debug(f"[X10 POST_ONLY SELL] {symbol}: Using orderbook ask=${best_ask} + 1 tick = ${raw_price} for POST_ONLY limit order (ensures Maker)")
            except Exception as e:
                logger.debug(f"[X10 POST_ONLY] {symbol}: Failed to get orderbook prices: {e}, falling back to mark price")

        # For reduce-only IOC closes without explicit price, prefer top-of-book executable price.
        # ALSO for non-POST_ONLY (Taker/Market/Hedge) orders: use top-of-book to ensure execution!
        # This is a better default than mark±slippage and reduces spurious IOC cancellations.
        if raw_price is None and ((reduce_only and not post_only) or (not post_only)):
            try:
                orderbook = await self.fetch_orderbook(symbol, limit=1)
                bids = orderbook.get("bids", [])
                asks = orderbook.get("asks", [])
                tick_size = safe_decimal(getattr(market.trading_config, "min_price_change", "0.01"))

                def _best_px(levels: list) -> Decimal:
                    if not levels:
                        return Decimal(0)
                    top = levels[0]
                    if isinstance(top, (list, tuple)) and len(top) > 0:
                        return safe_decimal(top[0])
                    if isinstance(top, dict):
                        return safe_decimal(top.get("p", top.get("price", 0)))
                    return safe_decimal(top) if isinstance(top, (int, float, str)) else Decimal(0)

                best_bid = _best_px(bids)
                best_ask = _best_px(asks)

                if side == "SELL" and best_bid > 0:
                    raw_price = best_bid  # best immediately executable sell price
                elif side == "BUY" and best_ask > 0:
                    # Add one tick to avoid rounding down below ask (would not execute)
                    raw_price = best_ask + (tick_size if tick_size > 0 else Decimal(0))
                
                # Log for diagnostics
                if not post_only:
                    logger.info(f"✅ [TAKER PRICE] {symbol}: Using top-of-book ({side} @ {raw_price}) for Taker/Hedge order (post_only=False, IOC)")
            except Exception as e:
                logger.debug(f"[X10 TOP-OF-BOOK] {symbol}: Failed to get top-of-book price: {e}, falling back to mark price")
        
        # Fallback to mark price + slippage if orderbook not available or not POST_ONLY
        if raw_price is None or raw_price <= 0:
            price = safe_decimal(self.fetch_mark_price(symbol))
            if price <= 0:
                return False, None
            slippage = safe_decimal(config.X10_MAX_SLIPPAGE_PCT) / 100
            raw_price = price * (
                Decimal(1) + slippage if side == "BUY" else Decimal(1) - slippage
            )

        cfg = market.trading_config
        if hasattr(cfg, "round_price") and callable(cfg.round_price):
            try:
                limit_price = cfg.round_price(raw_price)
            except:
                limit_price = raw_price.quantize(
                    Decimal("0.01"), 
                    rounding=ROUND_UP if side == "BUY" else ROUND_DOWN
                )
        else:
            tick_size = safe_decimal(getattr(cfg, "min_price_change", "0.01"))
            if side == "BUY":
                limit_price = ((raw_price + tick_size - Decimal('1e-12')) // tick_size) * tick_size
            else:
                limit_price = (raw_price // tick_size) * tick_size

        if amount is not None and amount > 0:
            qty = safe_decimal(amount)
        else:
            # safe_decimal handles notional
            qty = safe_decimal(notional_usd) / limit_price
            
        step = safe_decimal(getattr(cfg, "min_order_size_change", "0"))
        min_size = safe_decimal(getattr(cfg, "min_order_size", "0"))
        
        if step > 0:
            qty = (qty // step) * step
            if qty < min_size:
                qty = ((qty // step) + 1) * step

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        tif = TimeInForce.GTT
        
        # ═══════════════════════════════════════════════════════════════
        # FIX #4: X10 TimeInForce Enum Werte Verification
        # Available TimeInForce values: GTT, IOC, FOK
        # POST_ONLY does NOT exist in TimeInForce enum - it's handled via post_only parameter
        # ═══════════════════════════════════════════════════════════════
        is_market_order = False
        
        # ═══════════════════════════════════════════════════════════════
        # FIX #3: X10 Order Type Verification - Use IOC for Market Orders
        # Market Orders should use TimeInForce.IOC (Immediate Or Cancel)
        # Limit Orders use TimeInForce.GTT (POST_ONLY is via post_only parameter, not TimeInForce)
        # ═══════════════════════════════════════════════════════════════
        if post_only:
            # POST_ONLY is NOT a TimeInForce value - it's a separate parameter
            # For POST_ONLY orders, we still use TimeInForce.GTT (or could use FOK for immediate fill-or-cancel)
            # The post_only=True parameter ensures the order is Maker-only
            tif = TimeInForce.GTT  # POST_ONLY orders are Limit Orders with GTT
            logger.debug(f"✅ [TIF] {symbol}: POST_ONLY order (using TimeInForce.GTT, post_only=True parameter)")
        elif reduce_only and not post_only:
            # Market Orders during shutdown (reduce_only + not post_only)
            # Use IOC for immediate fills
            tif = TimeInForce.IOC
            is_market_order = True
            logger.debug(f"✅ [ORDER_TYPE] {symbol}: Using TimeInForce.IOC for Market Order (reduce_only)")
        elif not post_only:
            # ═══════════════════════════════════════════════════════════════
            # FIX (2025-12-19): CRITICAL BUG FIX!
            # When post_only=False, this is a TAKER order (e.g., LEG2 hedge)
            # It MUST use TimeInForce.IOC (Immediate Or Cancel) for guaranteed execution!
            # Previously this was falling through to GTT (Limit), causing infinite waits!
            # ═══════════════════════════════════════════════════════════════
            tif = TimeInForce.IOC  # Taker/Hedge orders MUST be IOC, not GTT!
            is_market_order = True
            logger.info(f"✅ [TIF FIXED] {symbol}: post_only=False -> Using TimeInForce.IOC (TAKER/MARKET order)")
        else:
            # Default: Limit Orders use GTT (Good Till Time) - should not reach here
            tif = TimeInForce.GTT
            logger.warning(f"⚠️ [TIF] {symbol}: Reached default GTT fallback (unexpected!)")
        
        # ═══════════════════════════════════════════════════════════════
        # FIX #4: Log all available TimeInForce enum values for verification
        # ═══════════════════════════════════════════════════════════════
        available_tif_values = []
        for attr in ['GTT', 'IOC', 'FOK', 'GTC', 'POST_ONLY']:
            if hasattr(TimeInForce, attr):
                available_tif_values.append(f"{attr}={getattr(TimeInForce, attr)}")
        
        if available_tif_values:
            logger.debug(f"ℹ️ [TIF] {symbol}: Available TimeInForce values: {', '.join(available_tif_values)}")
        else:
            logger.warning(f"⚠️ [TIF] {symbol}: No TimeInForce enum values found!")
        
        # Log the selected TimeInForce value
        logger.debug(f"✅ [TIF] {symbol}: Selected TimeInForce={tif} (post_only={post_only}, reduce_only={reduce_only}, is_market={is_market_order})")

        expire = datetime.now(timezone.utc) + timedelta(seconds=30 if post_only else (10 if is_market_order else 600))

        try:
            # FIX: Second rate limiter check before place_order
            # CRITICAL: During shutdown, we MUST still allow reduce_only orders to close positions
            order_result = await self.rate_limiter.acquire()
            if order_result < 0 and not reduce_only:
                logger.debug(f"[X10] Rate limiter cancelled during place_order - skipping (non-reduce-only)")
                return False, None
            elif order_result < 0 and reduce_only:
                # Rate limiter was cancelled (shutdown), but we need to close positions
                # Continue anyway - the order placement might still work
                logger.debug(f"[X10] Rate limiter cancelled during place_order (reduce_only={reduce_only}) - continuing for position close")
            # ═══════════════════════════════════════════════════════════════
            # FIX #3: Set post_only parameter explicitly
            # post_only=True = Limit Order (Maker)
            # post_only=False + TimeInForce.IOC = Market Order (Taker)
            # ═══════════════════════════════════════════════════════════════
            # FIX #7: X10 Reduce-Only Flag Verification
            # Parameter-Name: reduce_only (confirmed via SDK signature)
            # Boolean-Wert: True = 1 (ReduceOnly), False = 0 (normal order)
            # ═══════════════════════════════════════════════════════════════
            place_kwargs = {}
            try:
                if getattr(config, "X10_STP_ENABLED", False):
                    level_raw = getattr(config, "X10_STP_LEVEL", None)
                    if level_raw:
                        level = str(level_raw).upper()
                        # Backward compatible aliases:
                        # - NONE -> DISABLED
                        # - MARKET -> CLIENT (closest available SDK option)
                        alias = {
                            "NONE": "DISABLED",
                            "DISABLED": "DISABLED",
                            "MARKET": "CLIENT",
                            "CLIENT": "CLIENT",
                            "ACCOUNT": "ACCOUNT",
                        }
                        stp_value = SelfTradeProtectionLevel(alias.get(level, level)).value
                        params = set(inspect.signature(client.place_order).parameters)
                        for key in ("self_trade_protection_level", "selfTradeProtectionLevel", "self_trade_protection"):
                            if key in params:
                                place_kwargs[key] = stp_value
                                break
            except Exception:
                pass

            place_order_kwargs = {
                "market_name": symbol,
                "amount_of_synthetic": qty,
                "price": limit_price,
                "side": order_side,
                "post_only": post_only,  # ✅ FIX: Explicit post_only parameter
                "previous_order_id": previous_order_id,  # ✅ FIX: Atomic modify/replace (cancels old order)
                "time_in_force": tif,
                "expire_time": expire,
                "reduce_only": reduce_only,  # ✅ FIX #7: reduce_only parameter (True=1, False=0 in API)
            }
            
            # Add external_id if provided (for order tracking)
            if external_id:
                place_order_kwargs["external_id"] = external_id
            
            # Merge with any additional kwargs (e.g., self_trade_protection_level)
            place_order_kwargs.update(place_kwargs)
            
            # ═══════════════════════════════════════════════════════════════
            # Try WebSocket first for lower latency (if enabled)
            # ═══════════════════════════════════════════════════════════════
            if (hasattr(self, 'ws_order_client') and 
                self._ws_order_enabled and 
                self.ws_order_client and
                self.ws_order_client.is_connected):
                try:
                    # Convert TimeInForce enum to string
                    tif_str = tif.value if hasattr(tif, 'value') else str(tif)
                    
                    # Place order via WebSocket
                    ws_result = await self.ws_order_client.place_order(
                        market=symbol,
                        side=side,
                        qty=float(qty),
                        price=float(limit_price),
                        order_type="MARKET" if is_market_order else "LIMIT",
                        post_only=post_only,
                        time_in_force=tif_str,
                        reduce_only=reduce_only,
                        external_id=external_id
                    )
                    
                    if ws_result.error:
                        logger.warning(f"[X10-WS-ORDER] WebSocket order failed: {ws_result.error}, falling back to REST")
                    else:
                        logger.debug(f"[X10-WS-ORDER] Order placed via WebSocket: {ws_result.order_id}")
                        self.rate_limiter.on_success()
                        return True, ws_result.order_id if ws_result.order_id else None
                except Exception as e:
                    logger.warning(f"[X10-WS-ORDER] WebSocket submission failed: {e} - falling back to REST")
            
            # REST Fallback (or if WebSocket not available)
            resp = await client.place_order(**place_order_kwargs)
            
            # ═══════════════════════════════════════════════════════════════
            # FIX #7: Log reduce_only flag for verification
            # ═══════════════════════════════════════════════════════════════
            if is_market_order:
                logger.debug(f"✅ [ORDER_TYPE] {symbol}: Market Order placed (TimeInForce={tif}, post_only={post_only}, reduce_only={reduce_only})")
            else:
                logger.debug(f"✅ [ORDER_TYPE] {symbol}: Limit Order placed (TimeInForce={tif}, post_only={post_only}, reduce_only={reduce_only})")
            
            if resp.error:
                err_msg = str(resp.error)
                # ═══════════════════════════════════════════════════════════════
                # FIX: Handle position-related errors gracefully (not as errors)
                # 1137: "Position is missing for reduce-only order" - position already closed
                # 1138: "Position is same side as reduce-only order" - side mismatch
                # These are expected during shutdown and should not be logged as errors
                # ═══════════════════════════════════════════════════════════════
                if reduce_only and ("1137" in err_msg or "1138" in err_msg):
                    logger.info(f"✅ X10 {symbol}: Position already closed or unavailable (reduce-only)")
                    return True, None
                
                logger.error(f" X10 Order Fail: {resp.error}")
                if post_only and "post only" in err_msg.lower():
                    logger.info(" Retry ohne PostOnly...")
                    return await self.open_live_position(
                        symbol, side, notional_usd, reduce_only, post_only=False, amount=amount
                    )
                return False, None
                
            logger.info(f" X10 Order: {resp.data.id}")
            self.rate_limiter.on_success()
            return True, str(resp.data.id)
        except Exception as e:
            err_str = str(e)
            # ═══════════════════════════════════════════════════════════════
            # FIX: Handle position-related errors gracefully (not as errors)
            # 1137: "Position is missing for reduce-only order" - position already closed
            # 1138: "Position is same side as reduce-only order" - side mismatch
            # ═══════════════════════════════════════════════════════════════
            if reduce_only and ("1137" in err_str or "1138" in err_str):
                logger.info(f"✅ X10 {symbol}: Position already closed (exception path)")
                return True, None
            if "429" in err_str:
                self.rate_limiter.penalize_429()
            logger.error(f" X10 Order Exception: {e}")
            return False, None

    async def close_live_position(
        self,
        symbol: str,
        original_side: str,
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        
        # ═══════════════════════════════════════════════════════════════
        # FIX: Cancel all open orders first to prevent "ReduceOnly" conflict
        # ═══════════════════════════════════════════════════════════════
        await self.cancel_all_orders(symbol)
        await asyncio.sleep(0.5)

        # ═══════════════════════════════════════════════════════════════
        # FIX 2025-12-16: AGGRESSIVE PRICING FOR ILLIQUID MARKETS
        # Problem: IOC orders at best_bid get cancelled on illiquid markets
        # because the orderbook spread is too wide (e.g., EDEN-USD ~7% spread).
        # 
        # Solution: Use percentage-based slippage instead of tick-based:
        # - Start with 2% slippage, then 5%, 8%, 10%, 15%
        # - This ensures fills even on wide-spread markets
        # ═══════════════════════════════════════════════════════════════
        max_retries = 5
        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
        # In normal operation, start with low slippage and escalate slowly.
        # During shutdown, prefer certainty of exit.
        slippage_pct_steps = (
            [0.002, 0.005, 0.01, 0.02, 0.03]  # 0.2%, 0.5%, 1%, 2%, 3%
            if not is_shutting_down
            else [0.02, 0.05, 0.08, 0.10, 0.15]  # 2%, 5%, 8%, 10%, 15%
        )
        
        for attempt in range(max_retries):
            try:
                positions = await self.fetch_open_positions()
                actual_pos = next(
                    (p for p in (positions or []) if p.get('symbol') == symbol),
                    None
                )

                if not actual_pos or abs(safe_float(actual_pos.get('size', 0))) < 1e-8:
                    logger.info(f"✅ X10 {symbol} already closed")
                    return True, None

                actual_size = safe_float(actual_pos.get('size', 0))
                actual_size_abs = abs(actual_size)

                if actual_size > 0:
                    close_side = "SELL"
                else:
                    close_side = "BUY"

                # Determine a reference price for the IOC reduce-only close.
                # Prefer top-of-book executable price (best bid for sells, best ask for buys).
                market = self.market_info.get(symbol)
                cfg = getattr(market, 'trading_config', None)
                tick_size = safe_decimal(getattr(cfg, "min_price_change", "0.01")) if cfg else Decimal("0.01")

                def _best_px(levels: list) -> Decimal:
                    if not levels:
                        return Decimal(0)
                    top = levels[0]
                    if isinstance(top, (list, tuple)) and len(top) > 0:
                        return safe_decimal(top[0])
                    if isinstance(top, dict):
                        # WS cache formats often look like {"p": "0.123", "q": "45"}
                        return safe_decimal(top.get("p", top.get("price", 0)))
                    return safe_decimal(top) if isinstance(top, (int, float, str)) else Decimal(0)

                best_bid = Decimal(0)
                best_ask = Decimal(0)
                try:
                    ob = await self.fetch_orderbook(symbol, limit=1)
                    bids = ob.get('bids', [])
                    asks = ob.get('asks', [])
                    best_bid = _best_px(bids)
                    best_ask = _best_px(asks)
                except Exception:
                    pass

                # ═══════════════════════════════════════════════════════════════
                # FIX 2025-12-16: Use PERCENTAGE-BASED slippage for reliable fills
                # on illiquid markets. Tick-based slippage is insufficient when
                # the orderbook spread is wide (e.g., EDEN-USD has ~7% spread).
                # ═══════════════════════════════════════════════════════════════
                reference_price = Decimal(0)
                slippage_pct = Decimal(str(slippage_pct_steps[min(attempt, len(slippage_pct_steps) - 1)]))

                # Prefer mark price as reference (more stable than orderbook for illiquid markets)
                mark_px = safe_decimal(self.fetch_mark_price(symbol))
                
                if close_side == "SELL":
                    # SELL to close LONG: Accept lower price (sell below mark)
                    if mark_px > 0:
                        reference_price = mark_px * (Decimal(1) - slippage_pct)
                    elif best_bid > 0:
                        reference_price = best_bid * (Decimal(1) - slippage_pct)
                elif close_side == "BUY":
                    # BUY to close SHORT: Accept higher price (buy above mark)
                    if mark_px > 0:
                        reference_price = mark_px * (Decimal(1) + slippage_pct)
                    elif best_ask > 0:
                        reference_price = best_ask * (Decimal(1) + slippage_pct)

                # Final fallback
                if reference_price <= 0:
                    if mark_px <= 0:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1 + attempt)
                            continue
                        return False, None
                    reference_price = mark_px * (Decimal(1) + slippage_pct if close_side == "BUY" else Decimal(1) - slippage_pct)

                # Round to tick size
                if tick_size > 0:
                    reference_price = (reference_price / tick_size).quantize(Decimal('1'), rounding='ROUND_DOWN') * tick_size

                try:
                    ref_price_f = float(reference_price)
                except Exception:
                    ref_price_f = 0.0

                actual_notional = actual_size_abs * (ref_price_f if ref_price_f > 0 else safe_float(self.fetch_mark_price(symbol)))

                # ═══════════════════════════════════════════════════════════════
                # FIX 2025-12-16: Enhanced logging for shutdown close debugging
                # ═══════════════════════════════════════════════════════════════
                logger.info(
                    f"🔻 X10 CLOSE {symbol}: size={actual_size_abs:.6f}, side={close_side}, "
                    f"requested=${ref_price_f:.5f} (mark=${float(mark_px):.5f}, slip={float(slippage_pct)*100:.1f}%, attempt={attempt+1}/{max_retries})"
                )

                # ═══════════════════════════════════════════════════════════════
                # CRITICAL FIX: During shutdown, ALWAYS try to close positions
                # We cannot leave positions open during shutdown just because they're
                # below min notional. The exchange may reject, but we must try.
                # ═══════════════════════════════════════════════════════════════
                # Calculate minimums for logging only (not for blocking)
                buffered_min = self.min_notional_usd(symbol)
                hard_min = buffered_min / 1.05
                is_dust = actual_notional < hard_min
                
                if is_dust:
                    if is_shutting_down:
                        # FIX: reduce_only orders bypass min_notional validation on the exchange
                        # This is expected behavior during shutdown, so log at DEBUG level
                        logger.debug(f"🧹 X10 {symbol}: DUST POSITION ${actual_notional:.2f} < ${hard_min:.2f} - "
                                   f"Closing with reduce_only during shutdown (expected to succeed)")
                    elif actual_notional < 1.0:
                        # Normal operation: Skip tiny dust (< $1)
                        logger.warning(f"⚠️ X10 {symbol}: Skipping dust position (${actual_notional:.2f}). Treating as closed.")
                        return True, "DUST_SKIPPED"
                    else:
                        # Normal operation: Block sub-minimum positions
                        logger.error(f"❌ X10 {symbol}: Value too low to close: ${actual_notional:.2f} < Limit ${hard_min:.2f}")
                        return False, None

                success, order_id = await self.open_live_position(
                    symbol,
                    close_side,
                    actual_notional,
                    reduce_only=True,
                    post_only=False,
                    amount=actual_size_abs,
                    price=ref_price_f if ref_price_f > 0 else None,
                )

                # ═══════════════════════════════════════════════════════════════
                # FIX (2025-12-18): If open_live_position returns (True, None),
                # it means the position was already closed (error 1137/1138).
                # We should return immediately without further verification loops.
                # This prevents unnecessary retries during shutdown.
                # ═══════════════════════════════════════════════════════════════
                if success and order_id is None:
                    logger.info(f"✅ X10 {symbol}: Position confirmed closed (no order needed)")
                    return True, None

                if not success:
                    # FIX: Verify position state after error (handles 1138 race condition)
                    await asyncio.sleep(1)
                    recheck = await self.fetch_open_positions()
                    if not any(p['symbol'] == symbol and abs(safe_float(p.get('size', 0))) > 1e-8 for p in (recheck or [])):
                        logger.info(f"✅ X10 {symbol}: Position verified closed (post-error)")
                        return True, None
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 + attempt)
                        continue
                    return False, None

                # Track the close order id so TRADE fills can be attributed correctly
                # (used by get_last_close_price / shutdown PnL reconciliation).
                if order_id:
                    try:
                        self._closing_order_ids[symbol] = str(order_id)
                        logger.debug(f"[X10] Marked close order for {symbol}: order_id={order_id}")
                    except Exception:
                        pass

                await asyncio.sleep(1.0 + (0.5 * attempt))
                updated_positions = await self.fetch_open_positions()
                still_open = any(
                    p['symbol'] == symbol and abs(safe_float(p.get('size', 0))) > 1e-8
                    for p in (updated_positions or [])
                )

                if not still_open:
                    # Best-effort: if we're shutting down, wait briefly for WS fill updates
                    # so get_last_close_price() has the actual avg fill/fee.
                    if is_shutting_down:
                        try:
                            for _ in range(5):
                                px, qty, fee = self.get_last_close_price(symbol)
                                if safe_float(px) > 0 and safe_float(qty) > 0:
                                    logger.info(
                                        f"✅ X10 CLOSE FILLED {symbol}: avgFill=${float(px):.5f}, qty={float(qty):.6f}, fee=${float(fee):.4f} (requested=${ref_price_f:.5f})"
                                    )
                                    break
                                await asyncio.sleep(0.2)
                        except Exception:
                            pass
                    return True, order_id
                else:
                    if attempt < max_retries - 1:
                        continue
                    return False, order_id

            except Exception as e:
                err_str = str(e)
                # FIX: Handle code 1138 "Position is same side as reduce-only order"
                if "1138" in err_str or "same side as reduce" in err_str.lower():
                    logger.warning(f"⚠️ X10 {symbol}: 1138 error - checking if position closed...")
                    await asyncio.sleep(1)
                    vpos = await self.fetch_open_positions()
                    if not any(p['symbol'] == symbol and abs(safe_float(p.get('size', 0))) > 1e-8 for p in (vpos or [])):
                        logger.info(f"✅ X10 {symbol}: Position confirmed closed (1138)")
                        return True, None
                logger.error(f"X10 Close exception for {symbol}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False, None

        return False, None

    async def safe_cancel_replace_order(
        self,
        symbol: str,
        old_order_id: str,
        new_price: float,
        side: str,
        amount: float,
        post_only: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """
        Cancel an existing order and immediately replace with a new one at updated price.

        FIX (2025-12-19): Uses X10's previous_order_id parameter for ATOMIC modify/replace.
        This prevents ghost fills by canceling and replacing in a single API call.

        Previous implementation:
        1. Snapshot position BEFORE cancel
        2. Cancel old order (via cancel_order - which doesn't work!)
        3. Wait + check position (detect ghost fills)
        4. Place new order

        New implementation (ATOMIC):
        1. Place new order with previous_order_id=old_order_id
           → X10 SDK cancels old order and places new order atomically
        2. Ghost-fill-proof: No race condition possible!

        Args:
            symbol: Trading symbol
            old_order_id: Order ID to cancel
            new_price: New limit price for replacement order
            side: BUY or SELL
            amount: Size in coins
            post_only: Use maker-only (default True)

        Returns:
            (success: bool, new_order_id: str | None)
        """
        try:
            logger.info(f"🔄 [X10 ATOMIC-REPLACE] {symbol}: OldID={old_order_id}, NewPrice=${new_price:.6f}, Side={side}, Amount={amount}")

            # FIX: Use previous_order_id for ATOMIC modify/replace
            # This cancels the old order and places the new order in a single API call
            # Calculate notional_usd from amount * price
            notional_usd = float(amount) * new_price

            success, new_order_id = await self.open_live_position(
                symbol=symbol,
                side=side,
                notional_usd=notional_usd,
                amount=amount,
                price=new_price,
                post_only=post_only,
                reduce_only=False,
                previous_order_id=old_order_id,  # ✅ ATOMIC REPLACE!
            )

            if success:
                logger.info(f"✅ [X10 ATOMIC-REPLACE] {symbol}: Success (NewID={new_order_id}, replaced OldID={old_order_id})")
                return True, new_order_id
            else:
                logger.error(f"❌ [X10 ATOMIC-REPLACE] {symbol}: Replace order failed")
                return False, None

        except Exception as e:
            logger.error(f"❌ [X10 ATOMIC-REPLACE] {symbol} exception: {e}", exc_info=True)
            return False, None

    async def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        """Cancel a single order by ID"""
        try:
            if not self.stark_account:
                logger.warning("[X10] No stark_account configured - cannot cancel")
                return False

            # ═══════════════════════════════════════════════════════════════
            # Try WebSocket first for lower latency (if enabled)
            # ═══════════════════════════════════════════════════════════════
            if (hasattr(self, 'ws_order_client') and 
                self._ws_order_enabled and 
                self.ws_order_client and
                self.ws_order_client.is_connected):
                try:
                    ws_result = await self.ws_order_client.cancel_order(
                        order_id=order_id,
                        market=symbol
                    )
                    
                    if ws_result.error:
                        logger.warning(f"[X10-WS-ORDER] WebSocket cancel failed: {ws_result.error}, falling back to REST")
                    else:
                        logger.debug(f"[X10-WS-ORDER] Order cancelled via WebSocket: {order_id}")
                        return True
                except Exception as e:
                    logger.warning(f"[X10-WS-ORDER] WebSocket cancel failed: {e} - falling back to REST")

            # REST Fallback
            client = await self._get_auth_client()
            if not client or not hasattr(client, 'cancel_order'):
                logger.warning("[X10] Auth client does not support cancel_order")
                return False

            await self.rate_limiter.acquire()
            result = await client.cancel_order(order_id=order_id)

            if result:
                logger.info(f"✅ [X10] Cancelled order {order_id}")
                return True
            else:
                logger.warning(f"⚠️ [X10] Cancel returned False for {order_id}")
                return False

        except Exception as e:
            logger.error(f"❌ [X10] cancel_order exception: {e}")
            return False
    
    async def mass_cancel_orders(
        self,
        order_ids: Optional[List[int]] = None,
        external_order_ids: Optional[List[str]] = None,
        markets: Optional[List[str]] = None,
        cancel_all: bool = False
    ) -> bool:
        """
        Cancel multiple orders in one API call (Mass Cancel).
        
        This is 10x faster than canceling orders individually and is atomic
        (all orders are canceled or none).
        
        Args:
            order_ids: List of order IDs to cancel
            external_order_ids: List of external order IDs to cancel
            markets: List of market symbols - cancels all orders for these markets
            cancel_all: If True, cancels ALL orders (use with caution)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            if not self.stark_account:
                logger.warning("⚠️ [X10 Mass Cancel] No stark_account configured")
                return False
            
            client = await self._get_auth_client()
            if not client or not hasattr(client, 'orders'):
                logger.warning("⚠️ [X10 Mass Cancel] Client does not support orders module")
                return False
            
            # Validate parameters
            if not any([order_ids, external_order_ids, markets, cancel_all]):
                logger.warning("⚠️ [X10 Mass Cancel] No cancel criteria provided")
                return False
            
            await self.rate_limiter.acquire()
            
            # Call mass_cancel via orders module (Python SDK uses snake_case for method and parameters)
            result = await client.orders.mass_cancel(
                order_ids=[int(oid) for oid in order_ids] if order_ids else None,
                external_order_ids=external_order_ids,
                markets=markets,
                cancel_all=cancel_all
            )
            
            # Check result
            if hasattr(result, 'success'):
                success = result.success
            elif isinstance(result, dict):
                success = result.get('success', False)
            else:
                success = bool(result)
            
            if success:
                cancel_type = "all orders" if cancel_all else \
                             f"{len(order_ids or [])} orders" if order_ids else \
                             f"orders in {markets}" if markets else \
                             "orders"
                logger.info(f"✅ [X10 Mass Cancel] Successfully cancelled {cancel_type}")
                return True
            else:
                logger.warning(f"⚠️ [X10 Mass Cancel] Cancel returned False")
                return False
                
        except ValueError as e:
            # Handle "Market not found" errors gracefully (delisted markets)
            error_str = str(e)
            if "Market not found" in error_str or "code 1001" in error_str or '"code":1001' in error_str:
                market_list = markets if markets else "specified markets"
                logger.debug(f"⚠️ [X10 Mass Cancel] Market not found (may be delisted): {market_list}")
                return False  # Silently fail for delisted markets
            # Re-raise other ValueError exceptions
            logger.error(f"❌ [X10 Mass Cancel] ValueError: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"❌ [X10 Mass Cancel] Exception: {e}", exc_info=True)
            return False

    async def get_order_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order by external ID for tracking.

        This allows you to query orders using your own external IDs instead of
        exchange-internal IDs, enabling better integration with external systems
        and idempotency.

        Args:
            external_id: The external ID used when placing the order

        Returns:
            Order dictionary with order details, or None if not found
        """
        if not self.stark_account:
            logger.warning("[X10] No stark_account configured - cannot get order by external ID")
            return None

        try:
            client = await self._get_auth_client()
            await self.rate_limiter.acquire()

            # Try SDK method first
            if hasattr(client.account, 'get_order_by_external_id'):
                resp = await client.account.get_order_by_external_id(external_id)
                if resp and hasattr(resp, 'success') and resp.success:
                    data = resp.data if hasattr(resp, 'data') else []
                    if data and len(data) > 0:
                        order = data[0]
                        self.rate_limiter.on_success()
                        logger.debug(f"✅ [X10] Found order by external_id={external_id}")
                        return self._parse_order_data(order)
                    else:
                        logger.debug(f"[X10] No order found for external_id={external_id}")
                        self.rate_limiter.on_success()
                        return None

            # Fallback to direct REST API call
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/user/orders/external/{external_id}"

            headers = {
                "X-Api-Key": self.stark_account.api_key,
                "Accept": "application/json"
            }

            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    json_resp = await resp.json()
                    data = json_resp.get('data', [])
                    self.rate_limiter.on_success()
                    if data and len(data) > 0:
                        logger.debug(f"✅ [X10] Found order by external_id={external_id} (REST fallback)")
                        return self._parse_order_data(data[0])
                    else:
                        logger.debug(f"[X10] No order found for external_id={external_id} (REST fallback)")
                        return None
                elif resp.status == 404:
                    logger.debug(f"[X10] Order not found for external_id={external_id} (404)")
                    self.rate_limiter.on_success()
                    return None
                else:
                    logger.warning(f"⚠️ [X10] get_order_by_external_id returned {resp.status}")
                    self.rate_limiter.on_success()
                    return None

        except Exception as e:
            logger.error(f"❌ [X10] get_order_by_external_id failed: {e}", exc_info=True)
            return None

    async def cancel_order_by_external_id(self, external_id: str) -> bool:
        """
        Cancel order by external ID.

        This allows you to cancel orders using your own external IDs instead of
        exchange-internal IDs, enabling better integration with external systems.

        Args:
            external_id: The external ID used when placing the order

        Returns:
            True if order was cancelled successfully, False otherwise
        """
        if not self.stark_account:
            logger.warning("[X10] No stark_account configured - cannot cancel order by external ID")
            return False

        try:
            client = await self._get_trading_client()
            await self.rate_limiter.acquire()

            # Try SDK method first
            if hasattr(client.orders, 'cancel_order_by_external_id'):
                resp = await client.orders.cancel_order_by_external_id(external_id)
                if resp and hasattr(resp, 'success') and resp.success:
                    self.rate_limiter.on_success()
                    logger.info(f"✅ [X10] Cancelled order by external_id={external_id}")
                    return True
                else:
                    error_msg = getattr(resp, 'error', 'Unknown error') if resp else 'No response'
                    logger.warning(f"⚠️ [X10] Cancel order by external_id failed: {error_msg}")
                    self.rate_limiter.on_success()
                    return False

            # Fallback to direct REST API call
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/user/order"

            headers = {
                "X-Api-Key": self.stark_account.api_key,
                "Accept": "application/json"
            }
            params = {
                "externalId": external_id
            }

            session = await self._get_session()
            async with session.delete(url, headers=headers, params=params, timeout=5) as resp:
                if resp.status == 200:
                    self.rate_limiter.on_success()
                    logger.info(f"✅ [X10] Cancelled order by external_id={external_id} (REST fallback)")
                    return True
                elif resp.status == 404:
                    logger.warning(f"⚠️ [X10] Order not found for external_id={external_id} (404)")
                    self.rate_limiter.on_success()
                    return False
                else:
                    error_text = await resp.text()
                    logger.warning(f"⚠️ [X10] Cancel order by external_id returned {resp.status}: {error_text}")
                    self.rate_limiter.on_success()
                    return False

        except Exception as e:
            logger.error(f"❌ [X10] cancel_order_by_external_id failed: {e}", exc_info=True)
            return False

    def _parse_order_data(self, order: Any) -> Dict[str, Any]:
        """
        Helper method to parse order data from SDK response or REST API.

        Args:
            order: Order object (dict or SDK object)

        Returns:
            Parsed order dictionary
        """
        def get_val(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        return {
            "id": get_val(order, 'id'),
            "external_id": get_val(order, 'external_id') or get_val(order, 'externalId'),
            "symbol": get_val(order, 'market') or get_val(order, 'market_name'),
            "side": get_val(order, 'side'),
            "type": get_val(order, 'type') or get_val(order, 'order_type'),
            "status": get_val(order, 'status'),
            "price": safe_float(get_val(order, 'price'), 0.0),
            "size": safe_float(get_val(order, 'size') or get_val(order, 'amount'), 0.0),
            "filled_size": safe_float(get_val(order, 'filled_size') or get_val(order, 'filledSize'), 0.0),
            "created_at": get_val(order, 'created_at') or get_val(order, 'createdAt'),
            "updated_at": get_val(order, 'updated_at') or get_val(order, 'updatedAt'),
        }

    async def cancel_all_orders(self, symbol: str = None) -> bool:
        # ═══════════════════════════════════════════════════════════════
        # OPTIMIZED: Uses Mass Cancel for 10x faster shutdown performance
        # Shutdown behavior: best-effort + bounded.
        # We still try to cancel because open orders can block reduce-only closes,
        # but we must NOT block shutdown if the exchange / rate limiter is unhappy.
        # ═══════════════════════════════════════════════════════════════
        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
        
        try:
            if not self.stark_account:
                return False
            
            # ═══════════════════════════════════════════════════════════════
            # OPTIMIZATION: Use Mass Cancel if we have a symbol
            # This is 10x faster than canceling orders individually
            # ═══════════════════════════════════════════════════════════════
            if symbol:
                # Use Mass Cancel for the specific market - much faster!
                logger.info(f"🚀 [X10] Using Mass Cancel for {symbol} (10x faster)")
                try:
                    if is_shutting_down:
                        result = await asyncio.wait_for(
                            self.mass_cancel_orders(markets=[symbol]),
                            timeout=2.0
                        )
                    else:
                        result = await self.mass_cancel_orders(markets=[symbol])
                    
                    if result:
                        logger.info(f"✅ [X10] Mass cancelled all orders for {symbol}")
                        return True
                    else:
                        logger.warning(f"⚠️ [X10] Mass cancel failed for {symbol}, falling back to individual cancels")
                        # Fall through to individual cancel fallback
                except asyncio.TimeoutError:
                    logger.warning(f"⚠️ [X10] Mass cancel timeout for {symbol}, falling back")
                except Exception as e:
                    logger.warning(f"⚠️ [X10] Mass cancel error for {symbol}: {e}, falling back")
            
            # ═══════════════════════════════════════════════════════════════
            # FALLBACK: Individual cancel (if Mass Cancel fails or no symbol)
            # ═══════════════════════════════════════════════════════════════
            client = await self._get_auth_client()
            orders_resp = None
            candidate_methods = ['get_open_orders', 'list_orders', 'get_orders']
            
            for method_name in candidate_methods:
                if hasattr(client.account, method_name):
                    method = getattr(client.account, method_name)
                    try:
                        try:
                            result = await self.rate_limiter.acquire()
                            # FIX: Check if rate limiter was cancelled (shutdown)
                            if result < 0:
                                logger.debug(f"[X10] Rate limiter cancelled during cancel_all_orders - skipping")
                                return True
                            if is_shutting_down:
                                orders_resp = await asyncio.wait_for(method(market_name=symbol), timeout=2.0)
                            else:
                                orders_resp = await method(market_name=symbol)
                        except TypeError:
                            # Need to check rate limiter again for fallback call
                            result = await self.rate_limiter.acquire()
                            if result < 0:
                                logger.debug(f"[X10] Rate limiter cancelled during cancel_all_orders (fallback) - skipping")
                                return True
                            if is_shutting_down:
                                orders_resp = await asyncio.wait_for(method(), timeout=2.0)
                            else:
                                orders_resp = await method()
                        break
                    except Exception:
                        continue
            
            if not orders_resp or not getattr(orders_resp, 'data', None):
                return True
            
            # If we have many orders, try Mass Cancel with order IDs
            open_orders = [order for order in orders_resp.data 
                          if getattr(order, 'status', None) in ["PENDING", "OPEN"]]
            
            if len(open_orders) > 1:
                # Try Mass Cancel with order IDs
                try:
                    order_ids = [int(getattr(order, 'id', order)) for order in open_orders]
                    logger.info(f"🚀 [X10] Using Mass Cancel for {len(order_ids)} orders (10x faster)")
                    if is_shutting_down:
                        result = await asyncio.wait_for(
                            self.mass_cancel_orders(order_ids=order_ids),
                            timeout=2.0
                        )
                    else:
                        result = await self.mass_cancel_orders(order_ids=order_ids)
                    
                    if result:
                        logger.info(f"✅ [X10] Mass cancelled {len(order_ids)} orders")
                        return True
                except Exception as e:
                    logger.debug(f"[X10] Mass cancel with order IDs failed: {e}, falling back to individual")
            
            # Fallback: Individual cancels (slower but reliable)
            for order in open_orders:
                try:
                    result = await self.rate_limiter.acquire()
                    # FIX: Check if rate limiter was cancelled (shutdown)
                    if result < 0:
                        logger.debug(f"[X10] Rate limiter cancelled during cancel_order - skipping remaining orders")
                        break
                    if is_shutting_down:
                        await asyncio.wait_for(client.cancel_order(getattr(order, 'id', order)), timeout=2.0)
                    else:
                        await client.cancel_order(getattr(order, 'id', order))
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
            return True
        except Exception as e:
            # Never block shutdown because cancels failed.
            if is_shutting_down:
                logger.debug(f"[X10] cancel_all_orders shutdown best-effort failed: {e}")
                return True
            logger.debug(f"X10 cancel_all_orders error: {e}")
            return False
    
    async def get_real_available_balance(self) -> float:
        """
        Korrekte Balance-Abfrage für X10 – funktioniert mit aktuellem SDK (Dezember 2025)
        Methode: trading_client.account.get_balance()
        """
        # ═══════════════════════════════════════════════════════════════
        # FIX: Balance cache to reduce redundant API calls (2s TTL)
        # ═══════════════════════════════════════════════════════════════
        import time
        now = time.time()
        if now - self._last_balance_update < 2.0 and self._balance_cache > 0:
            return self._balance_cache
        
        if not self.trading_client:
            try:
                await self._get_trading_client()
            except Exception as e:
                logger.error(f"X10 trading_client nicht initialisiert: {e}")
                return 0.0

        try:
            await self.rate_limiter.acquire()
            
            # RICHTIGE METHODE: account.get_balance()
            balance_response = await self.trading_client.account.get_balance()
            self.rate_limiter.on_success()
            
            # X10 SDK gibt Response-Objekt zurück mit .data = BalanceModel
            if balance_response:
                # Die Balance ist in response.data.balance oder response.data.available_for_trade
                data = getattr(balance_response, 'data', balance_response)
                
                # Priorität: available_for_trade > equity > balance
                for attr in ['available_for_trade', 'equity', 'balance', 'available_collateral']:
                    available = getattr(data, attr, None)
                    if available is not None:
                        try:
                            balance = float(available)
                            if balance > 0:
                                logger.info(f"💰 X10 verfügbare Balance: ${balance:.2f} USDC")
                                # Update cache
                                self._balance_cache = balance
                                self._last_balance_update = time.time()
                                return balance
                        except (ValueError, TypeError):
                            continue
                
                logger.warning(f"X10 get_balance(): Balance-Felder gefunden aber Wert=0 oder nicht parsebar")
                return 0.0
            else:
                logger.warning("X10 get_balance() lieferte keine Antwort")
                return 0.0

        except AttributeError as e:
            # Falls SDK alte Version ohne account.get_balance()
            logger.error(f"X10 SDK Methode nicht gefunden: {e}")
            # Fallback: REST API direkt
            return await self._get_balance_via_rest()
        except Exception as e:
            logger.error(f"X10 Balance-Abfrage fehlgeschlagen: {e}", exc_info=True)
            return 0.0

    async def _get_balance_via_rest(self) -> float:
        """Fallback: Hole Balance direkt via REST API"""
        try:
            if not self.stark_account:
                return 0.0
            
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/user/account"
            
            headers = {
                'X-Api-Key': self.stark_account.api_key,
                'User-Agent': 'X10PythonTradingClient/0.4.5',
                'Accept': 'application/json'
            }
            
            await self.rate_limiter.acquire()
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.rate_limiter.on_success()
                    
                    # Parse response
                    inner = data.get('data') or data
                    
                    for key in ['available_collateral', 'availableCollateral', 'balance', 'equity', 'free_collateral']:
                        val = inner.get(key)
                        if val is not None:
                            balance = float(val)
                            if balance > 0:
                                logger.info(f"💰 X10 Balance via REST: ${balance:.2f} USDC")
                                return balance
                    
                    logger.warning(f"X10 REST: Keine Balance in Antwort gefunden: {list(inner.keys())}")
                else:
                    logger.warning(f"X10 REST Balance-Abfrage: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"X10 REST Balance-Fallback fehlgeschlagen: {e}")
        
        return 0.0

    async def get_price(self, symbol: str) -> Optional[float]:
        """
        Gibt den aktuellen Mark-Preis von X10 zurück (aus Cache oder frisch via REST).
        Wird von execute_trade() und dem Predictor benötigt.
        """
        # 1. Zuerst aus dem WebSocket-Cache
        if symbol in self.price_cache and self.price_cache[symbol] > 0:
            cache_time = self._price_cache_time.get(symbol, 0)
            if time.time() - cache_time < 15:  # maximal 15 Sekunden alt
                return self.price_cache[symbol]

        # 2. Fallback: REST-Abfrage über das offizielle SDK
        try:
            if not self.trading_client:
                await self._get_trading_client()
                
            if not self.trading_client:
                return None

            await self.rate_limiter.acquire()
            # X10 SDK Methode für Mark Price
            ticker = await self.trading_client.get_market_ticker(symbol.replace("-USD", ""))  # X10 erwartet "BTC" statt "BTC-USD"
            self.rate_limiter.on_success()
            
            if ticker and hasattr(ticker, 'mark_price') and ticker.mark_price:
                price = float(ticker.mark_price)
                # Cache aktualisieren
                self.price_cache[symbol] = price
                self._price_cache[symbol] = price
                self._price_cache_time[symbol] = time.time()
                logger.debug(f"X10 Preis frisch abgefragt {symbol}: ${price}")
                return price
        except Exception as e:
            logger.debug(f"X10 Preisabfrage fehlgeschlagen {symbol}: {e}")

        # 3. Fallback: fetch_mark_price (sync cache lookup)
        cached = self.fetch_mark_price(symbol)
        if cached and cached > 0:
            return cached

        return None

    # ═══════════════════════════════════════════════════════════════
    # WebSocket Event Handlers (called by WebSocketManager)
    # ═══════════════════════════════════════════════════════════════
    
    async def on_order_update(self, data: dict):
        """
        Handle order update from WebSocket.
        
        This is called by WebSocketManager when an order event is received.
        Updates local cache and resolves any pending order futures.
        """
        try:
            order_id = str(data.get("id") or data.get("orderId") or data.get("order_id") or "")
            status = (data.get("status") or data.get("orderStatus") or "").upper()
            symbol = data.get("market") or data.get("symbol") or ""
            
            # Update cache
            if order_id:
                self._order_cache[order_id] = data
            
            # Resolve pending order futures (for async order placement)
            if order_id in self._pending_orders:
                future = self._pending_orders[order_id]
                if not future.done():
                    if status in ["FILLED", "PARTIALLY_FILLED"]:
                        future.set_result({"success": True, "data": data})
                    elif status in ["CANCELLED", "REJECTED", "EXPIRED"]:
                        future.set_result({"success": False, "data": data})
            
            # Notify registered callbacks
            for callback in self._order_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    logger.error(f"X10 order callback error: {e}")
                    
            logger.debug(f"X10 Adapter: Order update processed - {symbol} #{order_id} {status}")
            
        except Exception as e:
            logger.error(f"X10 Adapter on_order_update error: {e}")
    
    async def on_position_update(self, data: dict):
        """Handle position update from WebSocket."""
        try:
            symbol = data.get("market") or data.get("symbol") or ""
            
            # Update cache
            if symbol:
                self._position_cache[symbol] = data
                
                # ═══════════════════════════════════════════════════════════════
                # FIX: Update _positions_cache when position status changes
                # This ensures fetch_open_positions returns correct data during shutdown
                # ═══════════════════════════════════════════════════════════════
                status = data.get("status", "UNKNOWN")
                size = safe_float(data.get("size") or data.get("quantity") or data.get("qty") or 0)
                
                # Initialize _positions_cache if it doesn't exist
                if not hasattr(self, '_positions_cache'):
                    self._positions_cache = []
                
                # Remove position from cache if it's CLOSED or has zero size
                if status == "CLOSED" or abs(size) < 1e-8:
                    self._positions_cache = [
                        p for p in self._positions_cache 
                        if p.get('symbol') != symbol
                    ]
                    logger.debug(f"[X10] Removed {symbol} from _positions_cache (status={status}, size={size})")
                elif status == "OPENED" and abs(size) > 1e-8:
                    # Update or add position to cache
                    # Support multiple field names for entry price
                    entry_price = safe_float(
                        data.get("entryPrice") or 
                        data.get("entry_price") or 
                        data.get("open_price") or 
                        data.get("avgPrice") or 
                        data.get("avgEntryPrice") or
                        data.get("averageEntryPrice") or
                        0.0
                    )
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX #1: Entry Price Resolution Priority:
                    # 1. WebSocket POSITION message (if provided)
                    # 2. Calculated from TRADE/FILL messages (weighted average)
                    # 3. REST API cache (from previous fetch)
                    # 4. REST API fetch (if needed)
                    # ═══════════════════════════════════════════════════════════════
                    if entry_price <= 0.0:
                        # Priority 1: Try calculated entry price from TRADE/FILL tracking
                        if symbol in self._fill_tracking:
                            track = self._fill_tracking[symbol]
                            if track["total_qty"] > 1e-8:
                                calculated_entry = track["total_value"] / track["total_qty"]
                                if calculated_entry > 0:
                                    entry_price = calculated_entry
                                    logger.debug(f"[X10] Using calculated entry price from TRADE fills for {symbol}: ${entry_price:.6f}")
                        
                        # Priority 2: Try cached entry price (from REST API or previous calculation)
                        if entry_price <= 0.0:
                            cached_pos = next(
                                (p for p in (self._positions_cache or []) if p.get('symbol') == symbol),
                                None
                            )
                            if cached_pos and cached_pos.get('entry_price', 0) > 0:
                                entry_price = safe_float(cached_pos.get('entry_price', 0))
                                logger.debug(f"[X10] Using cached entry price for {symbol}: ${entry_price:.6f}")
                        
                        # Priority 3: Last resort - Fetch from REST API
                        if entry_price <= 0.0:
                            try:
                                logger.debug(f"[X10] Entry price missing for {symbol}, fetching from REST API...")
                                rest_positions = await self.fetch_open_positions()
                                
                                rest_pos = next(
                                    (p for p in (rest_positions or []) if p.get('symbol') == symbol),
                                    None
                                )
                                if rest_pos and rest_pos.get('entry_price', 0) > 0:
                                    entry_price = safe_float(rest_pos.get('entry_price', 0))
                                    logger.debug(f"[X10] Fetched entry price from REST API for {symbol}: ${entry_price:.6f}")
                                else:
                                    # If still not found, check cache one more time (might have been updated by concurrent call)
                                    cached_pos_retry = next(
                                        (p for p in (self._positions_cache or []) if p.get('symbol') == symbol),
                                        None
                                    )
                                    if cached_pos_retry and cached_pos_retry.get('entry_price', 0) > 0:
                                        entry_price = safe_float(cached_pos_retry.get('entry_price', 0))
                                        logger.debug(f"[X10] Found entry price in cache after REST API call for {symbol}: ${entry_price:.6f}")
                            except Exception as e:
                                logger.debug(f"[X10] Could not fetch entry price from REST API for {symbol}: {e}")
                                # Last attempt: check cache one more time
                                cached_pos_final = next(
                                    (p for p in (self._positions_cache or []) if p.get('symbol') == symbol),
                                    None
                                )
                                if cached_pos_final and cached_pos_final.get('entry_price', 0) > 0:
                                    entry_price = safe_float(cached_pos_final.get('entry_price', 0))
                                    logger.debug(f"[X10] Found entry price in cache after exception for {symbol}: ${entry_price:.6f}")
                                # Keep entry_price as 0.0 - will be updated on next REST API call or TRADE
                    
                    position_data = {
                        "symbol": symbol,
                        "size": size,
                        "entry_price": entry_price
                    }
                    
                    # Remove old entry if exists
                    self._positions_cache = [
                        p for p in self._positions_cache 
                        if p.get('symbol') != symbol
                    ]
                    # Add updated position
                    self._positions_cache.append(position_data)
                    
                    if entry_price > 0:
                        logger.debug(f"[X10] Updated {symbol} in _positions_cache (size={size}, entry=${entry_price:.6f})")
                        # ═══════════════════════════════════════════════════════════════
                        # IMPROVEMENT: Update data dict with correct entry price
                        # This ensures log messages and callbacks see the correct value
                        # ═══════════════════════════════════════════════════════════════
                        # Update all possible field names in the data dict
                        data["entryPrice"] = entry_price
                        data["entry_price"] = entry_price
                        data["open_price"] = entry_price
                        data["avgPrice"] = entry_price
                        data["avgEntryPrice"] = entry_price
                        data["averageEntryPrice"] = entry_price
                    else:
                        logger.warning(f"[X10] Updated {symbol} in _positions_cache (size={size}, entry=$0.00 - REST API will update on next fetch)")
            
            # Notify registered callbacks
            for callback in self._position_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    logger.error(f"X10 position callback error: {e}")
                    
            logger.debug(f"X10 Adapter: Position update processed - {symbol}")
            
        except Exception as e:
            logger.error(f"X10 Adapter on_position_update error: {e}")
    
    async def on_fill_update(self, data: dict):
        """Handle fill/trade update from WebSocket."""
        try:
            order_id = str(data.get("orderId") or data.get("order_id") or "")
            symbol = data.get("market") or data.get("symbol") or ""
            
            # ═══════════════════════════════════════════════════════════════
            # IMPROVEMENT: Calculate entry price from TRADE/FILL messages
            # Track fills per symbol to calculate weighted average entry price
            # This ensures we have entry price even if REST API is deduplicated
            # ═══════════════════════════════════════════════════════════════
            if symbol:
                try:
                    # Extract fill price and quantity
                    fill_price = safe_float(data.get("price") or data.get("p") or 0)
                    fill_qty = safe_float(data.get("qty") or data.get("quantity") or data.get("amount") or 0)
                    side = data.get("side") or data.get("orderSide") or ""
                    
                    # Only track if we have valid price and quantity
                    if fill_price > 0 and fill_qty > 0:
                        # Initialize tracking for this symbol if needed
                        if symbol not in self._fill_tracking:
                            self._fill_tracking[symbol] = {
                                "total_qty": 0.0,
                                "total_value": 0.0,
                                "fills": []
                            }
                        
                        track = self._fill_tracking[symbol]
                        
                        # Add this fill to tracking
                        # FIX: Also store fee for PnL calculation
                        fill_fee = safe_float(data.get("fee") or data.get("feeAmount") or 0)
                        fill_data = {
                            "order_id": str(order_id),
                            "price": fill_price,
                            "qty": fill_qty,
                            "side": side,
                            "fee": fill_fee,
                            "timestamp": time.time()
                        }
                        track["fills"].append(fill_data)
                        
                        # Calculate weighted average entry price
                        # For LONG positions: BUY increases position, SELL decreases
                        # For SHORT positions: SELL increases position, BUY decreases
                        # We track net position size and weighted average price
                        if side.upper() in ["BUY", "LONG"]:
                            # Opening or increasing LONG position
                            track["total_value"] += fill_price * fill_qty
                            track["total_qty"] += fill_qty
                        elif side.upper() in ["SELL", "SHORT"]:
                            # Closing LONG or opening SHORT position
                            # If we're closing, reduce the position
                            if track["total_qty"] > 0:
                                # Closing: reduce position proportionally
                                reduction_ratio = min(fill_qty / track["total_qty"], 1.0)
                                track["total_value"] *= (1.0 - reduction_ratio)
                                track["total_qty"] -= fill_qty
                            else:
                                # Opening SHORT position
                                track["total_value"] += fill_price * fill_qty
                                track["total_qty"] += fill_qty
                        
                        # Calculate current entry price (weighted average)
                        if track["total_qty"] > 1e-8:
                            calculated_entry_price = track["total_value"] / track["total_qty"]
                            
                            # Update _positions_cache with calculated entry price
                            cached_pos = next(
                                (p for p in (self._positions_cache or []) if p.get('symbol') == symbol),
                                None
                            )
                            
                            if cached_pos:
                                # Update existing cache entry
                                cached_pos["entry_price"] = calculated_entry_price
                                logger.debug(f"[X10] Updated entry price from TRADE for {symbol}: ${calculated_entry_price:.6f} (from {len(track['fills'])} fills)")
                            else:
                                # Create new cache entry
                                position_data = {
                                    "symbol": symbol,
                                    "size": track["total_qty"],
                                    "entry_price": calculated_entry_price
                                }
                                if not hasattr(self, '_positions_cache'):
                                    self._positions_cache = []
                                self._positions_cache.append(position_data)
                                logger.debug(f"[X10] Created entry price from TRADE for {symbol}: ${calculated_entry_price:.6f}")
                        else:
                            # Position closed - save close fills BEFORE removing from tracking
                            # ═══════════════════════════════════════════════════════════════
                            # FIX: Store close fills for PnL calculation
                            # These contain the actual close price and fees
                            # ═══════════════════════════════════════════════════════════════
                            if symbol in self._fill_tracking:
                                all_fills = self._fill_tracking[symbol].get("fills", []) or []
                                closing_oid = None
                                try:
                                    closing_oid = self._closing_order_ids.get(symbol)
                                except Exception:
                                    closing_oid = None

                                # Prefer fills from the known reduce_only close order id.
                                close_fills = []
                                if closing_oid:
                                    close_fills = [f for f in all_fills if str(f.get("order_id")) == str(closing_oid)]

                                # Fallback: use the last fill only (avoid mixing entry + close).
                                if not close_fills and all_fills:
                                    close_fills = all_fills[-1:]

                                if close_fills:
                                    # Store the last 5 fills (partial close safety)
                                    self._recent_close_fills[symbol] = close_fills[-5:]
                                    total_fee = sum(float(f.get("fee", 0) or 0) for f in close_fills[-5:])
                                    total_qty = sum(float(f.get("qty", 0) or 0) for f in close_fills[-5:])
                                    logger.debug(
                                        f"[X10] Saved {len(close_fills[-5:])} close fills for {symbol}: "
                                        f"qty={total_qty:.4f}, fee=${total_fee:.4f}, order_id={closing_oid}"
                                    )

                                # Cleanup tracking for next position cycle
                                del self._fill_tracking[symbol]
                                try:
                                    self._closing_order_ids.pop(symbol, None)
                                except Exception:
                                    pass
                            # Remove from cache
                            if hasattr(self, '_positions_cache'):
                                self._positions_cache = [
                                    p for p in self._positions_cache 
                                    if p.get('symbol') != symbol
                                ]
                except Exception as fill_err:
                    logger.debug(f"[X10] Error tracking fill for entry price calculation: {fill_err}")
            
            # Mark order as filled if we're tracking it
            if order_id in self._pending_orders:
                future = self._pending_orders[order_id]
                if not future.done():
                    future.set_result({"success": True, "filled": True, "data": data})
            
            # Notify registered callbacks
            for callback in self._fill_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    logger.error(f"X10 fill callback error: {e}")
                    
            logger.debug(f"X10 Adapter: Fill update processed - {symbol} order={order_id}")
            
        except Exception as e:
            logger.error(f"X10 Adapter on_fill_update error: {e}")
    
    # ═══════════════════════════════════════════════════════════════
    # WebSocket Cache Access Methods
    # ═══════════════════════════════════════════════════════════════
    
    def get_cached_order(self, order_id: str) -> Optional[dict]:
        """Get order from WebSocket cache."""
        return self._order_cache.get(str(order_id))
    
    def get_cached_position(self, symbol: str) -> Optional[dict]:
        """Get position from WebSocket cache."""
        return self._position_cache.get(symbol)
    
    def get_all_cached_positions(self) -> List[dict]:
        """Get all cached positions."""
        return list(self._position_cache.values())
    
    async def wait_for_order_update(
        self, 
        order_id: str, 
        timeout: float = 30.0
    ) -> Optional[dict]:
        """
        Wait for order update via WebSocket.
        
        This is more efficient than polling REST API!
        
        Args:
            order_id: The order ID to wait for
            timeout: Maximum time to wait in seconds
            
        Returns:
            Order update dict or None on timeout
        """
        order_id = str(order_id)
        
        if order_id in self._pending_orders:
            future = self._pending_orders[order_id]
        else:
            future = asyncio.get_event_loop().create_future()
            self._pending_orders[order_id] = future
        
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"X10: Timeout waiting for order {order_id} update")
            return None
        finally:
            self._pending_orders.pop(order_id, None)
    
    def register_order_callback(self, callback: Callable):
        """Register callback to be notified of order updates."""
        self._order_callbacks.append(callback)
    
    def register_position_callback(self, callback: Callable):
        """Register callback to be notified of position updates."""
        self._position_callbacks.append(callback)
    
    def register_fill_callback(self, callback: Callable):
        """Register callback to be notified of fills."""
        self._fill_callbacks.append(callback)

    def get_recent_close_fills(self, symbol: str) -> list:
        """
        Get the recent close fills for a symbol.
        
        Used for PnL calculation after a position is closed.
        Returns list of fills with price, qty, side, and timestamp.
        
        Returns:
            List of fill dicts, or empty list if no fills found
        """
        return self._recent_close_fills.get(symbol, [])

    def get_last_close_price(self, symbol: str) -> tuple:
        """
        Get the last close price and fee for a symbol.
        
        Returns:
            Tuple of (price, qty, total_fee) or (0, 0, 0) if not found
        """
        fills = self._recent_close_fills.get(symbol, [])
        if not fills:
            return (0.0, 0.0, 0.0)
        
        # Calculate weighted average close price and total fee
        total_qty = sum(f.get("qty", 0) for f in fills)
        total_value = sum(f.get("price", 0) * f.get("qty", 0) for f in fills)
        total_fee = sum(f.get("fee", 0) for f in fills)
        
        avg_price = total_value / total_qty if total_qty > 0 else 0.0
        
        return (avg_price, total_qty, total_fee)

    async def aclose(self):
        """Cleanup all resources including SDK clients"""
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
            logger.debug(f"X10: Error stopping stream client: {e}")
        
        # Close any aiohttp sessions from rate limiter or internal use
        if hasattr(self, '_session') and self._session:
            try:
                await self._session.close()
            except Exception as e:
                logger.debug(f"X10: Error closing session: {e}")
        
        # Close trading client
        if self.trading_client:
            try:
                if hasattr(self.trading_client, 'close'):
                    res = self.trading_client.close()
                    # Prüfen ob close() awaitable ist
                    if asyncio.iscoroutine(res) or inspect.isawaitable(res):
                        await res
            except Exception as e:
                logger.debug(f"X10: Error closing trading_client: {e}")
        
        # Close auth client
        if self._auth_client:
            try:
                if hasattr(self._auth_client, 'close'):
                    res = self._auth_client.close()
                    if asyncio.iscoroutine(res) or inspect.isawaitable(res):
                        await res
            except Exception as e:
                logger.debug(f"X10: Error closing auth_client: {e}")
        
        self.trading_client = None
        self._auth_client = None
        logger.info("✅ X10 Adapter geschlossen.")

    async def get_collateral_balance(self, account_index: int = 0) -> float:
        """Alias für Kompatibilität – einfach weiterleiten"""
        return await self.get_real_available_balance()


    # ═══════════════════════════════════════════════════════════════════════════════
    # H4: REALISED PNL BREAKDOWN - Exakte PnL von X10 API
    # Basiert auf Extended-TS-SDK: RealisedPnlBreakdownModel (positions.ts)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    async def get_realised_pnl_breakdown(self, symbol: str, limit: int = 10) -> Optional[Dict]:
        """
        Get exact PnL breakdown for a recently closed position.
        
        X10 provides EXACT breakdown:
        - tradePnl: Price movement profit/loss
        - fundingFees: Accumulated funding payments
        - openFees: Entry trading fees
        - closeFees: Exit trading fees
        
        Args:
            symbol: Market symbol (e.g., "BTC-USD")
            limit: How many recent closed positions to check
            
        Returns:
            Dict with exact PnL breakdown or None if not found:
            {
                "symbol": "BTC-USD",
                "trade_pnl": 1.23,
                "funding_fees": 0.05,
                "open_fees": 0.02,
                "close_fees": 0.02,
                "total_realised_pnl": 1.24,
                "side": "LONG",
                "size": 0.01,
                "open_price": 100000.0,
                "exit_price": 100123.0,
                "closed_time": 1702728000000
            }
        """
        try:
            def get_val(obj, key, default=None):
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)

            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/user/positions/history"
            
            headers = {
                "X-Api-Key": self.stark_account.api_key,
                "Accept": "application/json"
            }
            
            await self.rate_limiter.acquire()
            
            session = await self._get_session()
            params = {"market": symbol, "limit": str(limit)}
            async with session.get(url, headers=headers, params=params, timeout=5) as resp:
                if resp.status != 200:
                    logger.debug(f"X10 positions/history returned {resp.status}")
                    return None
                
                json_resp = await resp.json()
                data = json_resp.get('data', [])
            
            self.rate_limiter.on_success()
            
            if not data:
                logger.debug(f"No closed positions found for {symbol}")
                return None
            
            # Get most recent closed position for this symbol
            pos = data[0]
            
            breakdown = get_val(pos, 'realised_pnl_breakdown') or get_val(pos, 'realisedPnlBreakdown')
            
            if not breakdown:
                logger.debug(f"No PnL breakdown in position history for {symbol}")
                return None
            
            result = {
                "symbol": symbol,
                "trade_pnl": safe_float(get_val(breakdown, 'trade_pnl') or get_val(breakdown, 'tradePnl'), 0.0),
                "funding_fees": safe_float(get_val(breakdown, 'funding_fees') or get_val(breakdown, 'fundingFees'), 0.0),
                "open_fees": safe_float(get_val(breakdown, 'open_fees') or get_val(breakdown, 'openFees'), 0.0),
                "close_fees": safe_float(get_val(breakdown, 'close_fees') or get_val(breakdown, 'closeFees'), 0.0),
                "total_realised_pnl": safe_float(get_val(pos, 'realised_pnl') or get_val(pos, 'realisedPnl'), 0.0),
                "side": get_val(pos, 'side'),
                "size": safe_float(get_val(pos, 'size'), 0.0),
                "open_price": safe_float(get_val(pos, 'open_price') or get_val(pos, 'openPrice'), 0.0),
                "exit_price": safe_float(get_val(pos, 'exit_price') or get_val(pos, 'exitPrice'), 0.0),
                "closed_time": get_val(pos, 'closed_time') or get_val(pos, 'closedTime')
            }
            
            logger.info(
                f"📊 X10 {symbol} PnL Breakdown: "
                f"tradePnL=${result['trade_pnl']:.4f}, "
                f"funding=${result['funding_fees']:.4f}, "
                f"fees=${result['open_fees'] + result['close_fees']:.4f}, "
                f"total=${result['total_realised_pnl']:.4f}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"X10 get_realised_pnl_breakdown failed for {symbol}: {e}")
            return None


    async def get_funding_history(self, limit: int = 50) -> List[Dict]:
        """
        Fetch realized funding history from closed positions.
        Based on Extended-TS-SDK: /user/positions/history -> realisedPnlBreakdown.fundingFees
        """
        try:
            # Helper to access nested attributes or dict keys
            def get_val(obj, key, default=None):
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)

            client = await self._get_auth_client()
            await self.rate_limiter.acquire()
            
            # Use raw request to ensure we get the history endpoint
            # client.account.get_positions_history might not be exposed in the python sdk depending on version
            # so we try both SDK method and direct fallback
            
            data = []
            
            # Try SDK first
            if hasattr(client.account, 'get_positions_history'):
                 resp = await client.account.get_positions_history(limit=limit)
                 if resp and hasattr(resp, 'data'):
                     data = resp.data
            
            # Fallback to direct REST if SDK method missing or empty
            if not data:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                url = f"{base_url}/api/v1/user/positions/history"
                
                headers = {
                    "X-Api-Key": self.stark_account.api_key,
                    "Accept": "application/json"
                }
                
                session = await self._get_session()
                params = {"limit": str(limit)}
                async with session.get(url, headers=headers, params=params, timeout=5) as resp:
                    if resp.status == 200:
                        json_resp = await resp.json()
                        data = json_resp.get('data', [])

            funding_records = []
            for p in data:
                # Check for breakdown
                breakdown = get_val(p, 'realised_pnl_breakdown') or get_val(p, 'realisedPnlBreakdown')
                
                if breakdown:
                    funding = safe_float(get_val(breakdown, 'funding_fees') or get_val(breakdown, 'fundingFees'), 0.0)
                    
                    if funding != 0:
                        symbol = get_val(p, 'market')
                        ts = get_val(p, 'closed_time') or get_val(p, 'updated_at')
                        
                        funding_records.append({
                            "symbol": symbol,
                            "amount": funding,
                            "timestamp": ts,
                            "type": "REALIZED_FUNDING"
                        })
            
            self.rate_limiter.on_success()
            return funding_records
            
        except Exception as e:
            logger.error(f"X10 get_funding_history failed: {e}")
            return []

    async def get_free_balance(self) -> float:
        """Alias for get_collateral_balance to satisfy interface."""
        return await self.get_collateral_balance()

    async def fetch_funding_payments_batch(self, symbols: List[str], from_time: Optional[int] = None) -> Dict[str, float]:
        """
        Fetch funding payments for multiple symbols in a single call and return NET funding per symbol.
        
        FIX (2025-12-19): Prevents API-latch where individual calls miss funding payments.
        This fetches ALL funding payments at once, then groups by symbol.
        
        Args:
            symbols: List of symbols to fetch funding for
            from_time: Starting timestamp in milliseconds (defaults to 24h ago)
            
        Returns:
            Dict[symbol, net_funding_amount]
            Example: {"BTC-USD": 0.0123, "ETH-USD": -0.0045}
        """
        if not symbols:
            return {}
        
        # Fetch ALL funding payments (no symbol filter = get all)
        all_payments = await self.fetch_funding_payments(symbol=None, from_time=from_time)
        
        # Group by symbol and sum funding fees
        result: Dict[str, float] = {}
        for payment in all_payments:
            sym = payment.get("symbol")
            if sym in symbols:
                fee = safe_float(payment.get("funding_fee"), 0.0)
                result[sym] = result.get(sym, 0.0) + fee
        
        logger.debug(f"🔍 [FUNDING BATCH] Processed {len(all_payments)} payments for {len(symbols)} symbols, found {len(result)} with funding")
        return result

    async def fetch_funding_payments(self, symbol: Optional[str] = None, from_time: Optional[int] = None) -> List[dict]:
        """
        Fetch funding payment history from X10 API.
        
        Uses: GET /api/v1/user/funding/history
        
        This gives EXACT funding payments (not calculated from rate).
        Much more accurate than rate × position × time calculation.
        
        Args:
            symbol: Optional market filter (e.g., "BTC-USD")
            from_time: Starting timestamp in milliseconds (required by API, defaults to 24h ago)
            
        Returns:
            List of funding payment records:
            [
                {
                    "id": 8341,
                    "symbol": "BNB-USD",
                    "side": "LONG",
                    "size": 1.116,
                    "value": 560.77,
                    "mark_price": 502.48,
                    "funding_fee": 0.0123,  # Exact payment!
                    "funding_rate": 0.0001,
                    "paid_time": 1723147241346
                }
            ]
        """
        if not self.stark_account:
            return []
        
        # Default to 24 hours ago if no from_time specified
        if from_time is None:
            from_time = int((time.time() - 86400) * 1000)  # 24h ago in ms

        def _normalize_to_ms(ts: Any) -> int:
            """Normalize seconds/ms timestamps to milliseconds."""
            v = safe_float(ts, 0.0)
            if v <= 0:
                return 0
            if v >= 1e12:
                return int(v)
            return int(v * 1000)

        requested_from_ms = _normalize_to_ms(from_time)
        
        try:
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            
            # Build URL with query params
            params = [f"fromTime={from_time}"]
            if symbol:
                params.append(f"market={symbol}")
            
            url = f"{base_url}/api/v1/user/funding/history?{'&'.join(params)}"
            
            logger.debug(f"🔍 [X10_FUNDING_DEBUG] Requesting: {url}")

            headers = {
                'X-Api-Key': self.stark_account.api_key,
                'User-Agent': 'X10PythonTradingClient/0.4.5',
                'Accept': 'application/json'
            }
            
            await self.rate_limiter.acquire()
            
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.rate_limiter.on_success()
                    
                    # LOG RAW DATA (Truncated if too long)
                    raw_str = str(data)
                    if len(raw_str) > 2000:
                        raw_str = raw_str[:2000] + "... [TRUNCATED]"
                    # DEBUG only: dumping large payloads at INFO can starve the event loop and
                    # indirectly cause WebSocket ping timeouts (observed in long sessions).
                    logger.debug(f"🔍 [X10_FUNDING_DEBUG] Raw Response: {raw_str}")

                    if data.get("status") == "OK" and "data" in data:
                        payments = []
                        for item in data["data"]:
                            payments.append({
                                "id": item.get("id"),
                                "symbol": item.get("market"),
                                "position_id": item.get("positionId"),
                                "side": item.get("side"),
                                "size": safe_float(item.get("size"), 0.0),
                                "value": safe_float(item.get("value"), 0.0),
                                "mark_price": safe_float(item.get("markPrice"), 0.0),
                                # FIX: X10 returns PnL directly (Negative = Paid/Cost, Positive = Received/Rebate)
                                # No inversion needed.
                                "funding_fee": safe_float(item.get("fundingFee"), 0.0),
                                "funding_rate": safe_float(item.get("fundingRate"), 0.0),
                                "paid_time": item.get("paidTime")
                            })
                            
                            # DEBUG: Log raw funding fee to investigate sign issue
                            raw_fee = item.get("fundingFee")
                            parsed_fee = safe_float(raw_fee, 0.0)
                            if raw_fee and str(raw_fee).startswith("-") and parsed_fee > 0:
                                logger.error(f"🚨 SIGN MISMATCH for {item.get('market')}: Raw='{raw_fee}' -> Parsed={parsed_fee}")

                        # Defensive client-side filtering: some API responses may include older records
                        # despite fromTime being provided (or timestamps may come back in seconds).
                        pre_count = len(payments)
                        filtered = [
                            p for p in payments
                            if _normalize_to_ms(p.get("paid_time")) >= requested_from_ms
                        ]

                        logger.debug(
                            f"🔍 [X10_FUNDING_DEBUG] Filtered {pre_count} -> {len(filtered)} payments "
                            f"(fromTime={requested_from_ms})"
                        )
                        
                        for p in filtered:
                            logger.debug(
                                f"  👉 [X10_PAYMENT] {p['symbol']} Time={p['paid_time']} "
                                f"Fee={p['funding_fee']:.6f} Rate={p['funding_rate']:.6f} Side={p['side']}"
                            )

                        return filtered
                    else:
                        logger.warning(f"🔍 [X10_FUNDING_DEBUG] Unexpected response format: {data.keys()}")
                        return []
                elif resp.status == 404:
                    # No funding history found - this is OK
                    logger.info(f"🔍 [X10_FUNDING_DEBUG] HTTP 404 (No funding history found)")
                    return []
                else:
                    logger.warning(f"🔍 [X10_FUNDING_DEBUG] HTTP {resp.status}")
                    return []
                        
        except asyncio.CancelledError:
            return []
        except Exception as e:
            logger.error(f"X10 fetch_funding_payments error: {e}")
            return []

    async def get_funding_for_symbol(self, symbol: str, since_timestamp: Optional[int] = None) -> float:
        """
        Get total funding received/paid for a specific symbol since a timestamp.
        
        Args:
            symbol: Market symbol (e.g., "BTC-USD")
            since_timestamp: Start time in milliseconds (defaults to 24h ago)
            
        Returns:
            Total funding in USD (positive = received, negative = paid)
        """
        payments = await self.fetch_funding_payments(symbol=symbol, from_time=since_timestamp)
        
        total_funding = sum(p.get("funding_fee", 0.0) for p in payments)
        
        if abs(total_funding) > 0.00001:
            logger.info(f"💵 X10 {symbol}: Total funding=${total_funding:.6f} from {len(payments)} payments")
        
        return total_funding
