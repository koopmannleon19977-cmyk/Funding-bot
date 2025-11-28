import asyncio
import logging
import inspect
import json
import time
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, List, Dict, AsyncIterator

import websockets
import aiohttp

from src.rate_limiter import X10_RATE_LIMITER
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
        self.market_info: Dict[str, dict] = {}
        self.client_env = MAINNET_CONFIG
        self.stark_account = None
        self._auth_client = None
        self.trading_client = None
        self.price_cache: Dict[str, float] = {}
        self.funding_cache: Dict[str, float] = {}
        self.orderbook_cache: Dict[str, dict] = {}
        self._ws_message_queue: asyncio.Queue = asyncio.Queue()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_funding: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_running = False
        self.rate_limiter = X10_RATE_LIMITER

        try:
            self.price_update_event = asyncio.Event()
        except Exception:
            self.price_update_event = None

        try:
            if config.X10_VAULT_ID:
                vault_id = int(str(config.X10_VAULT_ID).strip())
                self.stark_account = StarkPerpetualAccount(
                    vault=vault_id,
                    private_key=config.X10_PRIVATE_KEY,
                    public_key=config.X10_PUBLIC_KEY,
                    api_key=config.X10_API_KEY,
                )
                logger.info("✅ X10 Account initialisiert.")
            else:
                logger.warning("⚠️ X10 Config fehlt (Vault ID leer).")
        except Exception as e:
            logger.error(f"❌ X10 Account Init Error: {e}")

    # ═══════════════════════════════════════════════════════════════════════════
    # WEBSOCKET
    # ═══════════════════════════════════════════════════════════════════════════

    async def start_websocket(self):
        """Start both X10 WebSockets (Orderbook & Funding)"""
        self._ws_running = True
        await asyncio.gather(
            self._run_orderbook_ws(),
            self._run_funding_ws()
        )

    async def _run_orderbook_ws(self):
        """X10 WebSocket für Orderbook-Updates"""
        ws_url = getattr(
            config, 'X10_WS_URL',
            "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks"
        )

        while self._ws_running:
            try:
                logger.info(f"🌐 {self.name}: Connecting to Orderbook Stream...")

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self._ws = ws
                    logger.info(f"✅ {self.name}: Orderbook Stream connected!")

                    # Subscribe to markets
                    await self._subscribe_to_markets(ws)

                    # Message loop
                    async for msg_raw in ws:
                        if not self._ws_running:
                            break
                        try:
                            msg = json.loads(msg_raw)
                            await self._handle_ws_message(msg)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.debug(f"{self.name} WS msg error: {e}")

            except websockets.ConnectionClosed as e:
                logger.warning(f"{self.name}: OB WS closed: {e.code}")
            except asyncio.CancelledError:
                logger.info(f"{self.name}: OB WS cancelled")
                break
            except Exception as e:
                logger.error(f"{self.name}: OB WS error: {e}")

            self._ws = None
            if self._ws_running:
                logger.info(f"{self.name}: Reconnecting OB in 5s...")
                await asyncio.sleep(5)

    async def _run_funding_ws(self):
        """X10 WebSocket für Funding Rates"""
        ws_url = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/funding"

        while self._ws_running:
            try:
                logger.info(f"🌐 {self.name}: Connecting to Funding Stream...")

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self._ws_funding = ws
                    logger.info(f"✅ {self.name}: Funding Stream connected!")

                    # Funding stream automatically sends all markets if no market specified
                    async for msg_raw in ws:
                        if not self._ws_running:
                            break
                        try:
                            msg = json.loads(msg_raw)
                            self._handle_funding_message(msg)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.debug(f"{self.name} Funding WS msg error: {e}")

            except websockets.ConnectionClosed as e:
                logger.warning(f"{self.name}: Funding WS closed: {e.code}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{self.name}: Funding WS error: {e}")

            self._ws_funding = None
            if self._ws_running:
                logger.info(f"{self.name}: Reconnecting Funding in 5s...")
                await asyncio.sleep(5)

    async def _subscribe_to_markets(self, ws):
        """Subscribe to orderbook channels"""
        markets = list(self.market_info.keys())
        if not markets:
            logger.warning(f"{self.name}: No markets to subscribe")
            return

        for symbol in markets:
            x10_sym = symbol.replace("-", "_")
            try:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": "orderbook",
                    "market": x10_sym
                }))
                await asyncio.sleep(0.01)
            except Exception:
                pass

        logger.info(f"{self.name}: Subscribed to {len(markets)} markets")

    async def _handle_ws_message(self, msg: dict):
        """Handle X10 WS message"""
        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        # Extract symbol
        symbol_raw = data.get("m") or msg.get("market") or data.get("market")
        if not symbol_raw:
            return

        symbol = symbol_raw.replace("_", "-").replace("/", "-").upper()
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"

        # Orderbook update (DELTA type)
        if msg_type == "DELTA" or "bids" in data or "asks" in data:
            bids = data.get("b", data.get("bids", []))
            asks = data.get("a", data.get("asks", []))

            if symbol not in self.orderbook_cache:
                self.orderbook_cache[symbol] = {"bids": [], "asks": []}

            # Apply updates
            self._apply_orderbook_delta(symbol, bids, "bids")
            self._apply_orderbook_delta(symbol, asks, "asks")

            # Update price
            self._update_price_from_orderbook(symbol)

            # Feed to queue
            await self._ws_message_queue.put({
                "symbol": symbol,
                "orderbook": self.orderbook_cache[symbol]
            })

    def _handle_funding_message(self, msg: dict):
        """Handle Funding Stream Message"""
        try:
            # Format: {"data": {"m": "BTC-USD", "f": "0.001", ...}}
            data = msg.get("data", {})
            symbol_raw = data.get("m")
            funding_str = data.get("f")

            if symbol_raw and funding_str:
                symbol = self._normalize_symbol(symbol_raw)
                if symbol:
                    self.funding_cache[symbol] = float(funding_str)
                    # logger.debug(f"Funding update {symbol}: {self.funding_cache[symbol]}")
        except Exception:
            pass

    def _apply_orderbook_delta(self, symbol: str, updates: list, side: str):
        """Apply orderbook delta"""
        if not updates:
            return

        existing = self.orderbook_cache[symbol].get(side, [])

        for update in updates:
            # Handle different formats: [price, size] or {"price": p, "size": s}
            if isinstance(update, (list, tuple)):
                price, size = float(update[0]), float(update[1])
            else:
                price = float(update.get("price", update.get("p", 0)))
                size = float(update.get("size", update.get("s", update.get("q", 0))))

            # Find and update existing level
            found = False
            for i, level in enumerate(existing):
                level_price = level[0] if isinstance(level, list) else level.get("price", 0)
                if abs(float(level_price) - price) < 1e-10:
                    found = True
                    if size == 0:
                        existing.pop(i)
                    else:
                        existing[i] = [price, size]
                    break

            if not found and size > 0:
                existing.append([price, size])

        # Sort
        existing.sort(key=lambda x: x[0] if isinstance(x, list) else float(x.get("price", 0)),
                      reverse=(side == "bids"))
        self.orderbook_cache[symbol][side] = existing[:50]  # Keep top 50 levels

    def _update_price_from_orderbook(self, symbol: str):
        """Update mid price from orderbook"""
        ob = self.orderbook_cache.get(symbol)
        if not ob:
            return

        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        if bids and asks:
            try:
                best_bid = bids[0][0] if isinstance(bids[0], list) else float(bids[0].get("price", 0))
                best_ask = asks[0][0] if isinstance(asks[0], list) else float(asks[0].get("price", 0))
                if best_bid > 0 and best_ask > 0:
                    self.price_cache[symbol] = (best_bid + best_ask) / 2
                    if self.price_update_event:
                        self.price_update_event.set()
            except (ValueError, IndexError):
                pass

    async def ws_message_stream(self) -> AsyncIterator[dict]:
        """Yield messages for WebSocketManager"""
        while self._ws_running:
            try:
                msg = await asyncio.wait_for(self._ws_message_queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def stop_websocket(self):
        """Stop WebSockets gracefully"""
        self._ws_running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._ws_funding:
            try:
                await self._ws_funding.close()
            except Exception:
                pass
        self._ws = None
        self._ws_funding = None

    # ═══════════════════════════════════════════════════════════════════════════
    # MARKET DATA
    # ═══════════════════════════════════════════════════════════════════════════

    async def load_market_cache(self, force: bool = False):
        """Load market metadata"""
        if self.market_info and not force:
            return

        try:
            await self.rate_limiter.acquire()
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            
            session = await self._get_session()
            async with session.get(
                f"{base_url}/api/v1/info/markets",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"{self.name}: Markets fetch failed: {resp.status}")
                    return
                data = await resp.json()

            markets = data.get("data", data.get("markets", []))
            if isinstance(markets, dict):
                markets = list(markets.values())

            for m in markets:
                try:
                    name = m.get("name", m.get("symbol", ""))
                    if not name:
                        continue

                    symbol = name.replace("_", "-").replace("/", "-").upper()
                    if not symbol.endswith("-USD"):
                        symbol = f"{symbol}-USD"

                    if symbol in getattr(config, "BLACKLIST_SYMBOLS", set()):
                        continue

                    self.market_info[symbol] = {
                        "tick_size": m.get("tick_size", "0.01"),
                        "step_size": m.get("step_size", "0.0001"),
                        "min_order": m.get("min_order_size", 0.001),
                        "max_leverage": m.get("max_leverage", 20),
                        "raw_name": name,
                    }
                except Exception as e:
                    logger.debug(f"{self.name} market parse error: {e}")

            logger.info(f"✅ {self.name}: Loaded {len(self.market_info)} markets")

        except Exception as e:
            logger.error(f"{self.name} load_market_cache error: {e}")

    async def load_funding_rates_and_prices(self):
        """REST Fallback/Init für Funding Rates & Prices"""
        try:
            # Nur Prices laden via REST - Funding kommt jetzt via WebSocket
            # Funding REST auf Starknet scheint nicht alle Raten zu liefern
            # Wir nutzen Preise als Fallback
            await self.rate_limiter.acquire()
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            session = await self._get_session()

            # Prices (tickers)
            if not self.price_cache:
                async with session.get(
                    f"{base_url}/api/v1/info/tickers",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tickers = data.get("data", [])
                        for t in tickers:
                            try:
                                symbol = self._normalize_symbol(t.get("market", ""))
                                price = float(t.get("last_price", t.get("mark_price", 0)))
                                if symbol and price > 0:
                                    self.price_cache[symbol] = price
                            except Exception:
                                continue

            logger.debug(f"{self.name}: {len(self.funding_cache)} funding (WS), {len(self.price_cache)} prices")

        except Exception as e:
            logger.error(f"{self.name} load_funding_rates_and_prices error: {e}")

    def _normalize_symbol(self, raw: str) -> str:
        """Normalize symbol format"""
        if not raw:
            return ""
        symbol = raw.replace("_", "-").replace("/", "-").upper()
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        return symbol

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        return self.price_cache.get(symbol)

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        return self.funding_cache.get(symbol)

    def min_notional_usd(self, symbol: str) -> float:
        meta = self.market_info.get(symbol)
        if not meta:
            return 10.0
        min_order = meta.get("min_order", 0.001)
        price = self.fetch_mark_price(symbol) or 100
        return max(5.0, min_order * price)

    # ═══════════════════════════════════════════════════════════════════════════
    # TRADING
    # ═══════════════════════════════════════════════════════════════════════════

    async def _get_auth_client(self):
        """Get authenticated trading client"""
        if self._auth_client is not None:
            return self._auth_client

        if not self.stark_account:
            raise RuntimeError("X10 account not configured")

        self._auth_client = PerpetualTradingClient.create(self.client_env, self.stark_account)
        return self._auth_client

    async def get_real_available_balance(self) -> float:
        """Get available balance"""
        await self.rate_limiter.acquire()

        try:
            client = await self._get_auth_client()

            # Try SDK methods
            for method_name in ['get_balance', 'get_account_balance']:
                if hasattr(client.account, method_name):
                    try:
                        method = getattr(client.account, method_name)
                        resp = await method()
                        if resp and hasattr(resp, 'data'):
                            data = resp.data if isinstance(resp.data, dict) else getattr(resp.data, '__dict__', {})
                            for field in ['available_margin', 'collateral_balance', 'equity', 'balance']:
                                val = data.get(field)
                                if val is not None:
                                    balance = float(val)
                                    if balance > 0.01:
                                        return balance
                    except Exception:
                        continue

            # REST fallback
            if self.stark_account:
                base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
                headers = {"X-Api-Key": self.stark_account.api_key, "Accept": "application/json"}

                session = await self._get_session()
                async with session.get(
                    f"{base_url}/api/v1/user/balance",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if 'data' in data:
                            data = data['data']
                        for field in ['available_margin', 'collateral_balance', 'equity']:
                            if field in data:
                                return float(data[field])

            return 0.0

        except Exception as e:
            logger.error(f"{self.name} balance error: {e}")
            return 0.0

    async def fetch_open_positions(self) -> List[dict]:
        """Fetch open positions"""
        try:
            await self.rate_limiter.acquire()
            client = await self._get_auth_client()

            resp = await client.account.get_positions()
            if not resp or not getattr(resp, 'data', None):
                return []

            positions = []
            for p in resp.data:
                try:
                    symbol = self._normalize_symbol(getattr(p, 'market', ''))
                    size = float(getattr(p, 'size', 0))
                    if abs(size) > 1e-10:
                        positions.append({
                            "symbol": symbol,
                            "size": size,
                            "entry_price": float(getattr(p, 'avg_entry_price', 0)),
                            "unrealized_pnl": float(getattr(p, 'unrealized_pnl', 0)),
                        })
                except Exception:
                    continue
            return positions

        except Exception as e:
            logger.error(f"{self.name} fetch_open_positions error: {e}")
            return []

    async def open_live_position(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Open position on X10"""
        if not getattr(config, "LIVE_TRADING", False):
            return True, "DRY_RUN"

        if not self.stark_account:
            return False, None

        price = self.fetch_mark_price(symbol)
        if not price or price <= 0:
            logger.error(f"{self.name}: No price for {symbol}")
            return False, None

        try:
            await self.rate_limiter.acquire()
            client = await self._get_auth_client()

            meta = self.market_info.get(symbol, {})
            tick_size = Decimal(str(meta.get("tick_size", "0.01")))
            step_size = Decimal(str(meta.get("step_size", "0.0001")))

            # Calculate quantity
            qty = Decimal(str(notional_usd)) / Decimal(str(price))
            qty = (qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size

            # Calculate limit price with slippage
            slippage = Decimal(str(getattr(config, "X10_MAX_SLIPPAGE_PCT", 0.5) / 100))
            if side.upper() == "BUY":
                limit_price = Decimal(str(price)) * (1 + slippage)
            else:
                limit_price = Decimal(str(price)) * (1 - slippage)
            limit_price = (limit_price / tick_size).quantize(Decimal("1"), rounding=ROUND_UP) * tick_size

            order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
            tif = TimeInForce.POST_ONLY if post_only else TimeInForce.IOC

            market_name = meta.get("raw_name", symbol.replace("-", "_"))

            resp = await client.create_order(
                market=market_name,
                side=order_side,
                size=float(qty),
                price=float(limit_price),
                time_in_force=tif,
                reduce_only=reduce_only,
            )

            if resp and getattr(resp, 'data', None):
                order_id = str(getattr(resp.data, 'id', 'OK'))
                logger.info(f"✅ {self.name} {side} {symbol} ${notional_usd:.2f} → {order_id}")
                return True, order_id

            return False, None

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            logger.error(f"{self.name} open_live_position error: {e}")
            return False, None

    async def close_live_position(
        self,
        symbol: str,
        original_side: str,
        size: float
    ) -> Tuple[bool, Optional[str]]:
        """Close position - size is in COINS, not USD!"""
        if not getattr(config, "LIVE_TRADING", False):
            return True, "DRY_RUN"

        price = self.fetch_mark_price(symbol)
        if not price:
            return False, None

        # Determine close side (opposite of position)
        close_side = "SELL" if original_side.upper() == "BUY" else "BUY"
        notional = size * price

        return await self.open_live_position(symbol, close_side, notional, reduce_only=True)

    async def get_order_fee(self, order_id: str) -> float:
        """Get fee for order"""
        if not order_id or order_id.startswith("DRY"):
            return 0.0

        try:
            base_url = getattr(config, 'X10_API_BASE_URL', 'https://api.starknet.extended.exchange')
            headers = {"X-Api-Key": self.stark_account.api_key, "Accept": "application/json"}

            session = await self._get_session()
            async with session.get(
                f"{base_url}/api/v1/user/orders/{order_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    order = data.get("data", {})
                    fee = order.get("fee")
                    filled = order.get("filled_amount_of_synthetic")
                    price = order.get("price")

                    if fee and filled and price:
                        notional = float(filled) * float(price)
                        if notional > 0:
                            return float(fee) / notional

                    if order.get("time_in_force") == "POST_ONLY":
                        return config.MAKER_FEE_X10
                    return config.TAKER_FEE_X10

        except Exception as e:
            logger.debug(f"{self.name} get_order_fee error: {e}")

        return config.TAKER_FEE_X10

    # ═══════════════════════════════════════════════════════════════════════════
    # CLEANUP - KRITISCH FÜR "UNCLOSED SESSION" FEHLER
    # ═══════════════════════════════════════════════════════════════════════════

    async def aclose(self):
        """Cleanup ALL resources properly"""
        logger.info(f"{self.name}: Starting cleanup...")

        # 1. Stop WebSocket
        await self.stop_websocket()

        # 2. Close trading client
        if self.trading_client:
            try:
                # Check for internal session
                if hasattr(self.trading_client, '_session'):
                    sess = self.trading_client._session
                    if sess and not getattr(sess, 'closed', True):
                        await sess.close()
                # Call close method if exists
                if hasattr(self.trading_client, 'close'):
                    res = self.trading_client.close()
                    if asyncio.iscoroutine(res) or inspect.isawaitable(res):
                        await res
            except Exception as e:
                logger.debug(f"{self.name} trading_client cleanup: {e}")
            self.trading_client = None

        # 3. Close auth client
        if self._auth_client:
            try:
                # Check for internal session
                if hasattr(self._auth_client, '_session'):
                    sess = self._auth_client._session
                    if sess and not getattr(sess, 'closed', True):
                        await sess.close()
                # Check for _http_client
                if hasattr(self._auth_client, '_http_client'):
                    http = self._auth_client._http_client
                    if hasattr(http, '_session'):
                        sess = http._session
                        if sess and not getattr(sess, 'closed', True):
                            await sess.close()
                # Call close method
                if hasattr(self._auth_client, 'close'):
                    res = self._auth_client.close()
                    if asyncio.iscoroutine(res) or inspect.isawaitable(res):
                        await res
            except Exception as e:
                logger.debug(f"{self.name} auth_client cleanup: {e}")
            self._auth_client = None

        # 4. Close base adapter session
        await super().aclose()

        logger.info(f"✅ {self.name} Adapter closed.")