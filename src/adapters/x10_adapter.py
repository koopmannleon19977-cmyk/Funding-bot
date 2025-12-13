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

logger = logging.getLogger(__name__)


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
            
            async with aiohttp.ClientSession() as session:
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

            async with aiohttp.ClientSession() as session:
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
                return self._price_cache[symbol]

        # 2. Fallback auf REST Cache
        if symbol in self.price_cache:
            return self.price_cache[symbol]
            
        # 3. Fallback auf Market Info Stats
        m = self.market_info.get(symbol)
        if m and hasattr(m, 'market_stats') and hasattr(m.market_stats, 'mark_price'):
            price = getattr(m.market_stats, 'mark_price', None)
            if price is not None:
                price_float = float(price)
                # Cache aktualisieren für nächstes Mal
                self.price_cache[symbol] = price_float
                return price_float
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
            
            async with aiohttp.ClientSession() as session:
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

    async def fetch_open_positions(self) -> list:
        if not self.stark_account:
            logger.warning("X10: No stark_account configured")
            return []

        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - return cached data early during shutdown
        # ═══════════════════════════════════════════════════════════════
        is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
        if is_shutting_down:
            # Return cached positions if available, otherwise empty list
            if hasattr(self, '_positions_cache') and self._positions_cache:
                logger.debug("[X10] Shutdown active - returning cached positions")
                return self._positions_cache
            return []

        # ═══════════════════════════════════════════════════════════════
        # DEDUPLICATION: Prevent API storms during shutdown
        # ═══════════════════════════════════════════════════════════════
        if self.rate_limiter.is_duplicate("X10:fetch_open_positions"):
            # Return cached positions if available
            if hasattr(self, '_positions_cache') and self._positions_cache:
                return self._positions_cache
            # If no cache, allow the request through

        try:
            client = await self._get_auth_client()
            result = await self.rate_limiter.acquire()
            # Check if rate limiter was cancelled (shutdown)
            if result < 0:
                logger.debug("[X10] Rate limiter cancelled during fetch_open_positions - returning cached data")
                if hasattr(self, '_positions_cache') and self._positions_cache:
                    return self._positions_cache
                return []
            resp = await client.account.get_positions()

            if not resp or not resp.data:
                self._positions_cache = []
                return []

            positions = []
            for p in resp.data:
                status = getattr(p, 'status', 'UNKNOWN')
                size = float(getattr(p, 'size', 0))
                symbol = getattr(p, 'market', 'UNKNOWN')

                if status == "OPENED" and abs(size) > 1e-8:
                    # Some SDK versions use open_price, others use openPrice (camelCase).
                    # Prefer whichever is present to avoid missing entry price during shutdown PnL.
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
            
            # Cache for deduplication
            self._positions_cache = positions
            return positions

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            logger.error(f"X10 Positions Error: {e}")
            return getattr(self, '_positions_cache', [])

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

    async def refresh_missing_prices(self):
        try:
            missing = [s for s in self.market_info.keys() 
                      if s not in self.price_cache or self.price_cache.get(s, 0) == 0.0]
            
            if not missing:
                return
            
            await self.load_market_cache(force=True)
            
            still_missing = [s for s in missing if self.price_cache.get(s, 0) == 0.0]
            
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
                                        self.price_cache[symbol] = mid_price
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
        **kwargs
    ) -> Tuple[bool, Optional[str]]:
        """Mit manuellem Rate Limiting"""
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - skip during shutdown (except reduce_only close orders)
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False) and not reduce_only:
            logger.debug(f"[X10] Shutdown active - skipping open_live_position for {symbol} (non-reduce_only)")
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
            return False, None

        # ═══════════════════════════════════════════════════════════════
        # FIX: POST_ONLY Orders should use orderbook prices to avoid Taker fills
        # For POST_ONLY BUY: Use bid price to ensure Maker fill
        # For POST_ONLY SELL: Use ask price to ensure Maker fill
        # For Market orders: Use mark price + slippage as before
        # ═══════════════════════════════════════════════════════════════
        raw_price = None
        
        if post_only:
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
        else:
            # Default: Limit Orders use GTT (Good Till Time)
            tif = TimeInForce.GTT
        
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
            resp = await client.place_order(
                market_name=symbol,
                amount_of_synthetic=qty,
                price=limit_price,
                side=order_side,
                post_only=post_only,  # ✅ FIX: Explicit post_only parameter
                time_in_force=tif,
                expire_time=expire,
                reduce_only=reduce_only,  # ✅ FIX #7: reduce_only parameter (True=1, False=0 in API)
            )
            
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

        max_retries = 3
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

                price = safe_float(self.fetch_mark_price(symbol))
                if price <= 0:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
                    return False, None

                actual_notional = actual_size_abs * price

                # ═══════════════════════════════════════════════════════════════
                # CRITICAL FIX: During shutdown, ALWAYS try to close positions
                # We cannot leave positions open during shutdown just because they're
                # below min notional. The exchange may reject, but we must try.
                # ═══════════════════════════════════════════════════════════════
                is_shutting_down = getattr(config, 'IS_SHUTTING_DOWN', False)
                
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

                logger.info(f"🔻 X10 CLOSE {symbol}: size={actual_size_abs:.6f}, side={close_side}")

                success, order_id = await self.open_live_position(
                    symbol,
                    close_side,
                    actual_notional,
                    reduce_only=True,
                    post_only=False,
                    amount=actual_size_abs
                )

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

                await asyncio.sleep(2 + attempt)
                updated_positions = await self.fetch_open_positions()
                still_open = any(
                    p['symbol'] == symbol and abs(safe_float(p.get('size', 0))) > 1e-8
                    for p in (updated_positions or [])
                )

                if not still_open:
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
    
    async def cancel_all_orders(self, symbol: str = None) -> bool:
        # ═══════════════════════════════════════════════════════════════
        # FIX: Shutdown check - skip during shutdown
        # ═══════════════════════════════════════════════════════════════
        if getattr(config, 'IS_SHUTTING_DOWN', False):
            logger.debug(f"[X10] Shutdown active - skipping cancel_all_orders for {symbol}")
            return True  # Return True to avoid blocking shutdown
        
        try:
            if not self.stark_account:
                return False
            
            # Fallback: Wenn kein Symbol, breche ab oder iteriere (hier Abbruch um Seiteneffekte zu vermeiden)
            if not symbol:
                return False
            
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
                            orders_resp = await method(market_name=symbol)
                        except TypeError:
                            # Need to check rate limiter again for fallback call
                            result = await self.rate_limiter.acquire()
                            if result < 0:
                                logger.debug(f"[X10] Rate limiter cancelled during cancel_all_orders (fallback) - skipping")
                                return True
                            orders_resp = await method()
                        break
                    except Exception:
                        continue
            
            if not orders_resp or not getattr(orders_resp, 'data', None):
                return True
            
            for order in orders_resp.data:
                if getattr(order, 'status', None) in ["PENDING", "OPEN"]:
                    try:
                        result = await self.rate_limiter.acquire()
                        # FIX: Check if rate limiter was cancelled (shutdown)
                        if result < 0:
                            logger.debug(f"[X10] Rate limiter cancelled during cancel_order - skipping remaining orders")
                            break
                        await client.cancel_order(getattr(order, 'id', order))
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass
            return True
        except Exception as e:
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
            async with aiohttp.ClientSession() as session:
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

    async def get_free_balance(self) -> float:
        """Alias for get_collateral_balance to satisfy interface."""
        return await self.get_collateral_balance()