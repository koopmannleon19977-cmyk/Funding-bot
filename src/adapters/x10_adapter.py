# src/adapters/x10_adapter.py
import asyncio
import logging
import json
import aiohttp
import time
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional

from src.rate_limiter import AdaptiveRateLimiter
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
        self.price_cache = {}
        self.funding_cache = {}
        self.orderbook_cache = {}

        self.rate_limiter = AdaptiveRateLimiter(
            initial_rate=10.0,
            min_rate=3.0,
            max_rate=20.0,
            name="X10"
        )

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
        """
        Fetch real fee from X10 order
        
        Returns:
            Fee rate (e.g. 0.00025 = 0.025%)
        """
        if not order_id or order_id == "DRY_RUN_ORDER_123":
            return 0.0
        
        try:
            # Convert to int
            try:
                order_id_int = int(order_id)
            except ValueError:
                logger.error(f"X10: Invalid order_id format: {order_id}")
                return config.TAKER_FEE_X10
            
            # API call
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
                        
                        # Extract fee data
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
                                    
                                    # Sanity check
                                    if 0 <= fee_rate <= 0.01:
                                        return fee_rate
                            except (ValueError, TypeError):
                                pass
                        
                        # Fallback: Check order status
                        status = order.get("status", "UNKNOWN")
                        if status in ["PENDING", "OPEN"]:
                            return 0.0
                        
                        # Fallback: Check if maker/taker
                        time_in_force = order.get("time_in_force")
                        if time_in_force == "POST_ONLY":
                            return config.MAKER_FEE_X10
                        else:
                            return config.TAKER_FEE_X10
                            
                    elif resp.status == 404:
                        # Order not found yet
                        return 0.0
                    elif resp.status == 429:
                        # Rate limited
                        return config.TAKER_FEE_X10
                    else:
                        return config.TAKER_FEE_X10
                        
        except asyncio.TimeoutError:
            return config.TAKER_FEE_X10
        except Exception as e:
            logger.error(f"X10 Fee Fetch Error for {order_id}: {e}")
            return config.TAKER_FEE_X10

    async def start_websocket(self):
        urls = await self._get_ws_urls()
        orderbook_url = urls.get("orderbook")
        trades_url = urls.get("trades")
        
        retry_delay = 2
        max_delay = 60
        
        trades_task = asyncio.create_task(self._ws_trades(trades_url, retry_delay, max_delay))
        orderbook_task = asyncio.create_task(self._ws_orderbook(orderbook_url, retry_delay, max_delay))
        
        await asyncio.gather(trades_task, orderbook_task)

    async def _ws_trades(self, url, retry_delay, max_delay):
        while True:
            session = None
            try:
                session = aiohttp.ClientSession()
                async with session.ws_connect(
                    url,
                    heartbeat=10,
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=30)
                ) as ws:
                    logger.info(" X10 Trades WS connected")
                    retry_delay = 5
                    
                    if not self.market_info:
                        await self.load_market_cache(force=True)
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            trigger_update = False
                            
                            if "data" in data:
                                trades = data["data"]
                                if isinstance(trades, list):
                                    for trade in trades:
                                        sym = trade.get("m")
                                        px_str = trade.get("p")
                                        if sym and px_str:
                                            try:
                                                self.price_cache[sym] = float(px_str)
                                                trigger_update = True
                                            except ValueError:
                                                pass
                            
                            if trigger_update and hasattr(self, 'price_update_event'):
                                self.price_update_event.set()

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f" X10 Trades WS Error: {msg}")
                            break
                            
            except asyncio.CancelledError:
                logger.info(" X10 Trades WS stopped")
                break
            except Exception as e:
                logger.error(f" X10 Trades WS error: {e}. Reconnect in {retry_delay}s")
                await asyncio.sleep(retry_delay)
            finally:
                if session and not session.closed:
                    await session.close()
    
    async def _ws_orderbook(self, url, retry_delay, max_delay):
        while True:
            session = None
            try:
                session = aiohttp.ClientSession()
                async with session.ws_connect(
                    url,
                    heartbeat=10,
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=30)
                ) as ws:
                    logger.info(" X10 Orderbook WS connected")
                    retry_delay = 5
                    
                    if not self.market_info:
                        await self.load_market_cache(force=True)
                    
                    priority_symbols = list(self.market_info.keys())[:5]
                    
                    for symbol in priority_symbols:
                        sub_msg = {
                            "method": "subscribe",
                            "params": {
                                "channel": "orderbook",
                                "market": symbol
                            }
                        }
                        await ws.send_json(sub_msg)
                        await asyncio.sleep(0.05)
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            
                            if data.get("channel") == "orderbook":
                                symbol = data.get("market")
                                ob_data = data.get("data", {})
                                
                                if symbol and ob_data:
                                    bids = []
                                    asks = []
                                    
                                    for bid in ob_data.get("bids", []):
                                        try:
                                            bids.append([float(bid[0]), float(bid[1])])
                                        except:
                                            pass
                                    
                                    for ask in ob_data.get("asks", []):
                                        try:
                                            asks.append([float(ask[0]), float(ask[1])])
                                        except:
                                            pass
                                    
                                    self.orderbook_cache[symbol] = {
                                        'bids': bids,
                                        'asks': asks,
                                        'timestamp': time.time()
                                    }
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f" X10 Orderbook WS Error: {msg}")
                            break
            except asyncio.CancelledError:
                logger.info(" X10 Orderbook WS stopped")
                break
            except Exception as e:
                logger.error(f" X10 Orderbook WS error: {e}")
                await asyncio.sleep(retry_delay)
            finally:
                if session and not session.closed:
                    await session.close()

    async def _get_ws_urls(self) -> dict:
        return {
            "orderbook": "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks",
            "trades": "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/publicTrades",
            "funding": None
        }

    async def _get_auth_client(self) -> PerpetualTradingClient:
        if not self._auth_client:
            if not self.stark_account:
                raise RuntimeError("X10 Stark account missing")
            self._auth_client = PerpetualTradingClient(self.client_env, self.stark_account)
        return self._auth_client

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
            logger.info(f" X10: {len(self.market_info)} Mrkte geladen")
            self.rate_limiter.on_success()
        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.on_429()
            logger.error(f" X10 Market Cache Error: {e}")
        finally:
            await client.close()

    def fetch_mark_price(self, symbol: str):
        if symbol in self.price_cache:
            return self.price_cache[symbol]
        m = self.market_info.get(symbol)
        return float(m.market_stats.mark_price) if m and hasattr(m.market_stats, "mark_price") else None

    def fetch_funding_rate(self, symbol: str):
        m = self.market_info.get(symbol)
        return float(m.market_stats.funding_rate) if m and hasattr(m.market_stats, "funding_rate") else None

    def fetch_24h_vol(self, symbol: str) -> float:
        try:
            m = self.market_info.get(symbol)
            if m:
                return abs(float(getattr(m.market_stats, "price_change_24h_pct", 0)))
        except:
            pass
        return 0.0

    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> dict:
        try:
            x10_symbol = symbol.replace("-", "_")
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            url = f"{base_url}/api/v1/info/orderbook"
            params = {"market": x10_symbol, "depth": limit}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {
                            "bids": [[float(b["price"]), float(b["size"])] for b in data.get("bids", [])[:limit]],
                            "asks": [[float(a["price"]), float(a["size"])] for a in data.get("asks", [])[:limit]]
                        }
        except Exception:
            pass
        return {"bids": [], "asks": []}

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
        HARD_MIN_USD = 15.0
        SAFETY_BUFFER = 1.10
        
        m = self.market_info.get(symbol)
        if not m:
            return HARD_MIN_USD

        try:
            price = self.fetch_mark_price(symbol)
            if not price or price <= 0:
                return HARD_MIN_USD
            
            min_size = Decimal(getattr(m.trading_config, "min_order_size", "0"))
            api_min = float(min_size * Decimal(str(price)))
            safe_min = api_min * SAFETY_BUFFER
            result = max(safe_min, HARD_MIN_USD)
            return result
        except Exception:
            return HARD_MIN_USD

    async def fetch_open_positions(self) -> list:
        if not self.stark_account:
            return []
        
        try:
            client = await self._get_auth_client()
            resp = await client.account.get_positions()
            positions = []
            if resp and resp.data:
                for p in resp.data:
                    if p.status == "OPENED" and abs(float(p.size)) > 1e-8:
                        positions.append({
                            "symbol": p.market,
                            "size": float(p.size),
                            "entry_price": float(p.open_price) if p.open_price else 0.0
                        })
            return positions
        except Exception as e:
            logger.error(f" X10 Positions Error: {e}")
            return []

    async def open_live_position(
        self, 
        symbol: str, 
        side: str, 
        notional_usd: float, 
        reduce_only: bool = False, 
        post_only: bool = True
    ) -> Tuple[bool, Optional[str]]:
        if not config.LIVE_TRADING:
            return True, None
        
        client = await self._get_auth_client()
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
                if reduce_only and (
                    "1137" in err_msg or 
                    "1138" in err_msg or 
                    "Position is missing" in err_msg or 
                    "Position is same side" in err_msg
                ):
                    logger.warning(f" X10: Position {symbol} bereits zu (1137/1138). Erfolg.")
                    return True, None
                
                logger.error(f" X10 Order Fail: {resp.error}")
                if post_only and "post only" in err_msg.lower():
                    logger.info(" Retry ohne PostOnly...")
                    return await self.open_live_position(
                        symbol, side, notional_usd, reduce_only, post_only=False
                    )
                return False, None
                
            logger.info(f" X10 Order: {resp.data.id}")
            return True, str(resp.data.id)
        except Exception as e:
            err_str = str(e)
            if reduce_only and (
                "1137" in err_str or 
                "1138" in err_str or 
                "Position is missing" in err_str or 
                "Position is same side" in err_str
            ):
                return True, None
            logger.error(f" X10 Order Exception: {e}")
            return False, None

    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        close_side = "SELL" if original_side == "BUY" else "BUY"
        return await self.open_live_position(
            symbol, close_side, notional_usd, reduce_only=True, post_only=False
        )
    
    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol"""
        try:
            if not self.stark_account:
                return False
            
            client = await self._get_auth_client()
            
            # Get open orders
            orders_resp = await client.account.get_orders(market_name=symbol)
            if not orders_resp or not orders_resp.data:
                return True
            
            # Cancel each order
            for order in orders_resp.data:
                if getattr(order, 'status', None) in ["PENDING", "OPEN"]:
                    try:
                        await client.cancel_order(order.id)
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.debug(f"Cancel order {getattr(order, 'id', 'unknown')}: {e}")
            
            return True
        except Exception as e:
            logger.debug(f"X10 cancel_all_orders error: {e}")
            return False
    
    async def get_real_available_balance(self) -> float:
        try:
            client = await self._get_auth_client()
            resp = await client.account.get_balance()
            if hasattr(resp, 'data'):
                available = getattr(resp.data, 'available_for_trade', None)
                if available:
                    return float(str(available))
            return 0.0
        except Exception as e:
            logger.error(f" X10 Balance Error: {e}")
            return 0.0

    async def aclose(self):
        if self._auth_client:
            await self._auth_client.close()