# src/adapters/x10_adapter.py
import asyncio
import logging
import inspect  # WICHTIG für aclose Prüfung
import json
import websockets
import aiohttp
import time
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional

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
        self.price_update_event = None  # Will be set by main loop

        # WebSocket Streaming Support
        self._ws_message_queue = asyncio.Queue()

        self.rate_limiter = X10_RATE_LIMITER

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
                        
                        fee_abs = order.get("fee")
                        filled = order.get("filled_amount_of_synthetic")
                        price = order.get("price")
                        
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
                        
                        time_in_force = order.get("time_in_force")
                        if time_in_force == "POST_ONLY":
                            return config.MAKER_FEE_X10
                        else:
                            return config.TAKER_FEE_X10
                    else:
                        return config.TAKER_FEE_X10
                        
        except Exception as e:
            logger.error(f"X10 Fee Fetch Error for {order_id}: {e}")
            return config.TAKER_FEE_X10

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
        if self.market_info and not force:
            return

        client = PerpetualTradingClient(self.client_env)
        try:
            await self.rate_limiter.acquire()
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
        if symbol in self.price_cache:
            return self.price_cache[symbol]
        m = self.market_info.get(symbol)
        if m and hasattr(m, 'market_stats') and hasattr(m.market_stats, 'mark_price'):
            price = getattr(m.market_stats, 'mark_price', None)
            if price is not None:
                price_float = float(price)
                self.price_cache[symbol] = price_float
                return price_float
        return None

    def fetch_funding_rate(self, symbol: str):
        if symbol in self.funding_cache:
            return self.funding_cache[symbol]
        m = self.market_info.get(symbol)
        if m and hasattr(m, 'market_stats') and hasattr(m.market_stats, 'funding_rate'):
            rate = getattr(m.market_stats, 'funding_rate', None)
            if rate is not None:
                rate_float = float(rate)
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
        
        # Return cached or empty
        return self.orderbook_cache.get(symbol, {"bids": [], "asks": [], "timestamp": 0})

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
        HARD_MAX_USD = 25.0
        SAFETY_BUFFER = 1.05
        
        m = self.market_info.get(symbol)
        if not m:
            return HARD_MIN_USD

        try:
            price = self.fetch_mark_price(symbol)
            if not price or price <= 0:
                return HARD_MIN_USD
            
            min_size = Decimal(getattr(m.trading_config, "min_order_size", "0"))
            api_min_usd = float(min_size * Decimal(str(price)))
            safe_min = api_min_usd * SAFETY_BUFFER
            return max(HARD_MIN_USD, min(safe_min, HARD_MAX_USD))
        except Exception:
            return HARD_MIN_USD

    async def fetch_open_positions(self) -> list:
        if not self.stark_account:
            logger.warning("X10: No stark_account configured")
            return []

        try:
            client = await self._get_auth_client()
            await self.rate_limiter.acquire()
            resp = await client.account.get_positions()

            if not resp or not resp.data:
                return []

            positions = []
            for p in resp.data:
                status = getattr(p, 'status', 'UNKNOWN')
                size = float(getattr(p, 'size', 0))
                symbol = getattr(p, 'market', 'UNKNOWN')

                if status == "OPENED" and abs(size) > 1e-8:
                    entry_price = float(p.open_price) if hasattr(p, 'open_price') and p.open_price else 0.0
                    positions.append({
                        "symbol": symbol,
                        "size": size,
                        "entry_price": entry_price
                    })
            return positions

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            logger.error(f"X10 Positions Error: {e}")
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
        post_only: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """Mit manuellem Rate Limiting"""
        if not config.LIVE_TRADING:
            return True, None
        
        # Warte auf Token BEVOR Request gesendet wird
        await self.rate_limiter.acquire()
        
        client = await self._get_trading_client()
        market = self.market_info.get(symbol)
        if not market:
            return False, None

        price = Decimal(str(self.fetch_mark_price(symbol) or 0))
        if price <= 0:
            return False, None

        slippage = Decimal(str(config.X10_MAX_SLIPPAGE_PCT)) / 100
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
            tick_size = Decimal(getattr(cfg, "min_price_change", "0.01"))
            if side == "BUY":
                limit_price = ((raw_price + tick_size - Decimal('1e-12')) // tick_size) * tick_size
            else:
                limit_price = (raw_price // tick_size) * tick_size

        qty = Decimal(str(notional_usd)) / limit_price
        step = Decimal(getattr(cfg, "min_order_size_change", "0"))
        min_size = Decimal(getattr(cfg, "min_order_size", "0"))
        
        if step > 0:
            qty = (qty // step) * step
            if qty < min_size:
                qty = ((qty // step) + 1) * step

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        tif = TimeInForce.GTT
        
        if post_only:
            if hasattr(TimeInForce, "POST_ONLY"):
                tif = TimeInForce.POST_ONLY
            elif hasattr(TimeInForce, "PostOnly"):
                tif = TimeInForce.PostOnly
            else:
                post_only = False

        expire = datetime.now(timezone.utc) + timedelta(seconds=30 if post_only else 600)

        try:
            await self.rate_limiter.acquire()
            resp = await client.place_order(
                market_name=symbol,
                amount_of_synthetic=qty,
                price=limit_price,
                side=order_side,
                time_in_force=tif,
                expire_time=expire,
                reduce_only=reduce_only,
            )
            
            if resp.error:
                err_msg = str(resp.error)
                if reduce_only and ("1137" in err_msg or "1138" in err_msg):
                    return True, None
                
                logger.error(f" X10 Order Fail: {resp.error}")
                if post_only and "post only" in err_msg.lower():
                    logger.info(" Retry ohne PostOnly...")
                    return await self.open_live_position(
                        symbol, side, notional_usd, reduce_only, post_only=False
                    )
                return False, None
                
            logger.info(f" X10 Order: {resp.data.id}")
            self.rate_limiter.on_success()
            return True, str(resp.data.id)
        except Exception as e:
            err_str = str(e)
            if reduce_only and ("1137" in err_str or "1138" in err_str):
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
        max_retries = 3
        for attempt in range(max_retries):
            try:
                positions = await self.fetch_open_positions()
                actual_pos = next(
                    (p for p in (positions or []) if p.get('symbol') == symbol),
                    None
                )

                if not actual_pos or abs(actual_pos.get('size', 0)) < 1e-8:
                    logger.info(f"✅ X10 {symbol} already closed")
                    return True, None

                actual_size = actual_pos.get('size', 0)
                actual_size_abs = abs(actual_size)

                if actual_size > 0:
                    close_side = "SELL"
                else:
                    close_side = "BUY"

                price = self.fetch_mark_price(symbol)
                if not price or price <= 0:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
                    return False, None

                actual_notional = actual_size_abs * price

                logger.info(f"🔻 X10 CLOSE {symbol}: size={actual_size_abs:.6f}, side={close_side}")

                success, order_id = await self.open_live_position(
                    symbol,
                    close_side,
                    actual_notional,
                    reduce_only=True,
                    post_only=False
                )

                if not success:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 + attempt)
                        continue
                    return False, None

                await asyncio.sleep(2 + attempt)
                updated_positions = await self.fetch_open_positions()
                still_open = any(
                    p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8
                    for p in (updated_positions or [])
                )

                if not still_open:
                    return True, order_id
                else:
                    if attempt < max_retries - 1:
                        continue
                    return False, order_id

            except Exception as e:
                logger.error(f"X10 Close exception for {symbol}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False, None

        return False, None
    
    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            if not self.stark_account:
                return False
            
            client = await self._get_auth_client()
            orders_resp = None
            candidate_methods = ['get_open_orders', 'list_orders', 'get_orders']
            
            for method_name in candidate_methods:
                if hasattr(client.account, method_name):
                    method = getattr(client.account, method_name)
                    try:
                        try:
                            await self.rate_limiter.acquire()
                            orders_resp = await method(market_name=symbol)
                        except TypeError:
                            orders_resp = await method()
                        break
                    except Exception:
                        continue
            
            if not orders_resp or not getattr(orders_resp, 'data', None):
                return True
            
            for order in orders_resp.data:
                if getattr(order, 'status', None) in ["PENDING", "OPEN"]:
                    try:
                        await self.rate_limiter.acquire()
                        await client.cancel_order(getattr(order, 'id', order))
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass
            return True
        except Exception as e:
            logger.debug(f"X10 cancel_all_orders error: {e}")
            return False
    
    async def get_real_available_balance(self) -> float:
        await self.rate_limiter.acquire()
        try:
            client = await self._get_auth_client()
            
            # METHODE 1: SDK get_balance()
            for method_name in ['get_balance', 'get_account_balance', 'get_balances']:
                if hasattr(client.account, method_name):
                    try:
                        method = getattr(client.account, method_name)
                        resp = await method()
                        if resp and hasattr(resp, 'data'):
                            data = resp.data if isinstance(resp.data, dict) else resp.data.__dict__
                            for field in ['available_margin', 'collateral_balance', 'equity', 'balance']:
                                val = data.get(field)
                                if val is not None:
                                    try:
                                        balance = float(val)
                                        if balance > 0.01:
                                            return balance
                                    except:
                                        continue
                    except Exception:
                        continue

            # METHODE 2: REST API Fallback
            if self.stark_account:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                endpoints = ['/api/v1/user/balance', '/api/v1/user/account/balance']
                headers = {"X-Api-Key": self.stark_account.api_key, "Accept": "application/json"}
                
                async with aiohttp.ClientSession() as session:
                    for endpoint in endpoints:
                        try:
                            url = f"{base_url}{endpoint}"
                            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if isinstance(data, dict):
                                        if 'data' in data and isinstance(data['data'], dict):
                                            data = data['data']
                                        for field in ['available_margin', 'collateral_balance', 'equity', 'balance']:
                                            if field in data:
                                                try:
                                                    return float(data[field])
                                                except:
                                                    continue
                        except Exception:
                            continue

            # METHODE 3: Legacy Fallback
            try:
                resp = await client.account.get_account()
                if resp and resp.data:
                    data = resp.data if isinstance(resp.data, dict) else resp.data.__dict__
                    for field in ['available_margin', 'collateral_balance']:
                        val = data.get(field)
                        if val is not None:
                            return float(val)
            except Exception:
                pass
            
            logger.error("X10: ALLE Balance-Methoden fehlgeschlagen!")
            return 0.0
            
        except Exception as e:
            logger.error(f"X10 Balance fetch komplett fehlgeschlagen: {e}")
            return 0.0

    async def aclose(self):
        """Cleanup all resources including SDK clients"""
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