# src/adapters/lighter_adapter.py - KOMPLETT NEU MIT KORREKTEM WS-PROTOKOLL

import asyncio
import json
import logging
import time
from typing import Optional, List, Tuple, Any, Dict, AsyncIterator
from decimal import Decimal

import websockets
import aiohttp

import config
from src.account_manager import get_account_manager
from src.rate_limiter import LIGHTER_RATE_LIMITER
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

# Optional SDK imports (best-effort)
OrderApi = FundingApi = AccountApi = SignerClient = None
try:
    try:
        from lighter.lighter_api.order_api import OrderApi
        from lighter.lighter_api.funding_api import FundingApi
        from lighter.lighter_api.account_api import AccountApi
        from lighter.signer_client import SignerClient
    except Exception:
        from lighter.api.order_api import OrderApi
        from lighter.api.funding_api import FundingApi
        from lighter.api.account_api import AccountApi
        from lighter.signer_client import SignerClient
except Exception:
    logger.info("Optional Lighter SDK not available; using REST fallbacks.")


class LighterAdapter(BaseAdapter):
    def __init__(self):
        super().__init__("Lighter")
        self.market_info: Dict[str, dict] = {}
        self.funding_cache: Dict[str, float] = {}
        self.price_cache: Dict[str, float] = {}
        self.orderbook_cache: Dict[str, dict] = {}
        self.price_update_event: Optional[asyncio.Event] = None
        self._ws_message_queue: asyncio.Queue = asyncio.Queue()
        self._signer = None
        self._resolved_account_index = None
        self._resolved_api_key_index = None
        self.rate_limiter = LIGHTER_RATE_LIMITER
        self._balance_cache = 0.0
        self._last_balance_update = 0.0
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_running = False

    # ═══════════════════════════════════════════════════════════════════════════
    # WEBSOCKET - KORREKTES PROTOKOLL (aus offiziellem SDK)
    # ═══════════════════════════════════════════════════════════════════════════

    async def start_websocket(self):
        """
        WebSocket nach offiziellem Lighter SDK Protokoll:
        1. Connect → warte auf {"type": "connected"}
        2. Subscribe mit {"type": "subscribe", "channel": "order_book/{market_id}"}
        3. Empfange subscribed/order_book, update/order_book, ping
        """
        ws_url = getattr(config, "LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
        self._ws_running = True

        while self._ws_running:
            try:
                logger.info(f"🌐 {self.name}: Connecting to {ws_url}...")
                
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self._ws = ws
                    logger.info(f"✅ {self.name}: WebSocket connected")

                    # 1. Warte auf "connected" Message
                    try:
                        init_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        init_msg = json.loads(init_raw)
                        logger.info(f"🔍 {self.name} Init: {init_msg}")
                        
                        if init_msg.get("type") != "connected":
                            logger.warning(f"{self.name}: Unexpected init: {init_msg}")
                    except asyncio.TimeoutError:
                        logger.error(f"{self.name}: Timeout waiting for connected message")
                        continue

                    # 2. Subscribe zu allen Markets
                    await self._subscribe_to_markets(ws)

                    # 3. Message Loop
                    async for raw in ws:
                        if not self._ws_running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle_ws_message(ws, msg)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.debug(f"{self.name} WS message error: {e}")

            except websockets.ConnectionClosed as e:
                logger.warning(f"{self.name}: WS closed: {e.code} {e.reason}")
            except asyncio.CancelledError:
                logger.info(f"{self.name}: WS cancelled")
                break
            except Exception as e:
                logger.error(f"{self.name}: WS error: {e}")

            self._ws = None
            if self._ws_running:
                logger.info(f"{self.name}: Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _subscribe_to_markets(self, ws):
        """Subscribe zu order_book und account channels"""
        markets = list(self.market_info.keys())
        if not markets:
            logger.warning(f"{self.name}: No markets to subscribe")
            return

        subscribed = 0
        for sym in markets:
            info = self.market_info.get(sym)
            if not info or 'i' not in info:
                continue
            
            market_id = info['i']
            if market_id is None or int(market_id) < 0:
                continue

            # Subscribe order_book channel
            sub_msg = {"type": "subscribe", "channel": f"order_book/{market_id}"}
            await ws.send(json.dumps(sub_msg))
            subscribed += 1
            await asyncio.sleep(0.01)

        # Subscribe account channel if we have account index
        if self._resolved_account_index:
            acc_msg = {"type": "subscribe", "channel": f"account_all/{self._resolved_account_index}"}
            await ws.send(json.dumps(acc_msg))
            logger.info(f"{self.name}: Subscribed to account {self._resolved_account_index}")

        logger.info(f"{self.name}: Subscribed to {subscribed} order_book channels")

    async def _handle_ws_message(self, ws, msg: dict):
        """Handle incoming WS message nach SDK-Protokoll"""
        msg_type = msg.get("type", "")

        # Ping/Pong
        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            return

        # Order Book Snapshot
        if msg_type == "subscribed/order_book":
            await self._handle_orderbook_snapshot(msg)
            return

        # Order Book Update
        if msg_type == "update/order_book":
            await self._handle_orderbook_update(msg)
            return

        # Account Updates
        if msg_type in ("subscribed/account_all", "update/account_all"):
            await self._handle_account_update(msg)
            return

        # Debug log unknown types (once)
        if not hasattr(self, f"_logged_{msg_type}"):
            logger.debug(f"{self.name} WS unknown type: {msg_type}: {str(msg)[:200]}")
            setattr(self, f"_logged_{msg_type}", True)

    async def _handle_orderbook_snapshot(self, msg: dict):
        """Handle initial orderbook snapshot"""
        channel = msg.get("channel", "")
        # Format: "order_book:0" after subscription
        parts = channel.split(":")
        if len(parts) < 2:
            return

        market_id = int(parts[1])
        symbol = self._market_id_to_symbol(market_id)
        if not symbol:
            return

        ob = msg.get("order_book", {})
        self.orderbook_cache[symbol] = {
            "bids": ob.get("bids", []),
            "asks": ob.get("asks", [])
        }

        # Update price from best bid/ask
        self._update_price_from_orderbook(symbol)

        # Feed to prediction engine
        await self._ws_message_queue.put({"symbol": symbol, "orderbook": self.orderbook_cache[symbol]})

    async def _handle_orderbook_update(self, msg: dict):
        """Handle orderbook delta update"""
        channel = msg.get("channel", "")
        parts = channel.split(":")
        if len(parts) < 2:
            return

        market_id = int(parts[1])
        symbol = self._market_id_to_symbol(market_id)
        if not symbol or symbol not in self.orderbook_cache:
            return

        ob_update = msg.get("order_book", {})
        
        # Apply deltas
        self._apply_orderbook_delta(symbol, ob_update.get("bids", []), "bids")
        self._apply_orderbook_delta(symbol, ob_update.get("asks", []), "asks")

        # Update price
        self._update_price_from_orderbook(symbol)

        # Feed to queue
        await self._ws_message_queue.put({"symbol": symbol, "orderbook": self.orderbook_cache[symbol]})

    def _apply_orderbook_delta(self, symbol: str, updates: list, side: str):
        """Apply orderbook delta updates"""
        if symbol not in self.orderbook_cache:
            return

        existing = self.orderbook_cache[symbol].get(side, [])
        
        for update in updates:
            price = update.get("price")
            size = update.get("size")
            if price is None:
                continue

            # Find existing level
            found = False
            for i, level in enumerate(existing):
                if level.get("price") == price:
                    found = True
                    if float(size or 0) == 0:
                        existing.pop(i)
                    else:
                        level["size"] = size
                    break

            if not found and float(size or 0) > 0:
                existing.append({"price": price, "size": size})

        # Sort: bids descending, asks ascending
        if side == "bids":
            existing.sort(key=lambda x: float(x.get("price", 0)), reverse=True)
        else:
            existing.sort(key=lambda x: float(x.get("price", 0)))

        self.orderbook_cache[symbol][side] = existing

    def _update_price_from_orderbook(self, symbol: str):
        """Update mid price from orderbook"""
        ob = self.orderbook_cache.get(symbol)
        if not ob:
            return

        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        if bids and asks:
            try:
                best_bid = float(bids[0].get("price", 0))
                best_ask = float(asks[0].get("price", 0))
                if best_bid > 0 and best_ask > 0:
                    self.price_cache[symbol] = (best_bid + best_ask) / 2
                    if self.price_update_event:
                        self.price_update_event.set()
            except (ValueError, IndexError):
                pass

    async def _handle_account_update(self, msg: dict):
        """Handle account balance/position updates"""
        # Extract balance if present
        try:
            data = msg.get("data", msg)
            balance = data.get("total_asset_value") or data.get("balance")
            if balance:
                self._balance_cache = float(balance) * 0.95
                self._last_balance_update = time.time()
        except Exception:
            pass

    def _market_id_to_symbol(self, market_id: int) -> Optional[str]:
        """Reverse lookup: market_id → symbol"""
        for sym, info in self.market_info.items():
            if info.get('i') == market_id:
                return sym
        return None

    async def ws_message_stream(self) -> AsyncIterator[dict]:
        """Yield messages from WS queue for WebSocketManager"""
        while self._ws_running:
            try:
                msg = await asyncio.wait_for(self._ws_message_queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def stop_websocket(self):
        """Gracefully stop WebSocket"""
        self._ws_running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    # ═══════════════════════════════════════════════════════════════════════════
    # MARKET DATA LOADING
    # ═══════════════════════════════════════════════════════════════════════════

    async def load_market_cache(self, force: bool = False):
        """Load all market metadata from Lighter API"""
        if self.market_info and not force:
            return

        try:
            # Lighter uses /api/v1/orderBooks for market list
            data = await self._rest_get("/api/v1/orderBooks")
            if not data:
                # Fallback: try /info endpoint
                data = await self._rest_get("/info")
                if not data:
                    logger.error(f"{self.name}: Failed to load markets")
                    return

            # orderBooks response has different structure
            markets = data.get("order_books", data.get("markets", data.get("data", [])))
            if isinstance(markets, dict):
                markets = list(markets.values())

            for m in markets:
                try:
                    symbol = m.get("ticker", m.get("symbol", ""))
                    if not symbol:
                        # Try market_index as fallback identifier
                        symbol = f"MARKET-{m.get('market_index', m.get('i', 'X'))}"

                    # Normalize symbol format
                    symbol = symbol.replace("_", "-").replace("/", "-").upper()
                    if not symbol.endswith("-USD"):
                        symbol = f"{symbol}-USD"

                    # Skip blacklisted
                    if symbol in getattr(config, "BLACKLIST_SYMBOLS", set()):
                        continue

                    self.market_info[symbol] = {
                        "i": m.get("market_index", m.get("i", m.get("index"))),
                        "sd": m.get("size_decimals", m.get("base_decimals", 8)),
                        "pd": m.get("price_decimals", m.get("quote_decimals", 6)),
                        "min_notional": m.get("min_order_size_usd", m.get("min_notional", 10)),
                        "tick_size": m.get("tick_size", "0.01"),
                        "lot_size": m.get("lot_size", "0.0001"),
                    }
                except Exception as e:
                    logger.debug(f"{self.name} market parse error: {e}")

            logger.info(f"✅ {self.name}: Loaded {len(self.market_info)} markets")

            # Resolve account index
            await self._resolve_account_index()

        except Exception as e:
            logger.error(f"{self.name} load_market_cache error: {e}")

    async def _resolve_account_index(self):
        """Resolve account and API key indices"""
        try:
            acc = get_account_manager().get_next_lighter_account()
            self._resolved_account_index = acc.get("account_index", config.LIGHTER_ACCOUNT_INDEX)
            self._resolved_api_key_index = acc.get("api_key_index", config.LIGHTER_API_KEY_INDEX)
            logger.info(f"{self.name}: Using account index {self._resolved_account_index}")
        except Exception as e:
            logger.debug(f"{self.name} resolve account error: {e}")
            self._resolved_account_index = getattr(config, "LIGHTER_ACCOUNT_INDEX", 0)
            self._resolved_api_key_index = getattr(config, "LIGHTER_API_KEY_INDEX", 0)

    async def load_funding_rates_and_prices(self):
        """Load funding rates and prices via REST"""
        try:
            # Funding rates
            # Try multiple endpoints
            funding_data = await self._rest_get("/api/v1/fundingRates")
            if not funding_data:
                funding_data = await self._rest_get("/api/v1/orderBooks")

            if funding_data:
                # orderBooks contains funding_rate per market
                rates = funding_data.get(
                    "funding_rates",
                    funding_data.get("order_books", funding_data.get("data", [])),
                )
                if isinstance(rates, dict):
                    rates = list(rates.values())

                for r in rates:
                    try:
                        symbol = self._normalize_symbol(
                            r.get("ticker", r.get("symbol", r.get("market", "")))
                        )
                        # funding_rate can be in different fields
                        rate = float(
                            r.get("funding_rate", r.get("rate", r.get("funding", 0)))
                        )
                        if symbol and symbol in self.market_info:
                            self.funding_cache[symbol] = rate
                    except Exception:
                        continue

            # Prices (if not from WS)
            price_data = await self._rest_get("/api/v1/orderBooks")
            if price_data:
                tickers = price_data.get(
                    "order_books", price_data.get("tickers", price_data.get("data", []))
                )
                if isinstance(tickers, dict):
                    tickers = list(tickers.values())
                for t in tickers:
                    try:
                        symbol = self._normalize_symbol(
                            t.get("ticker", t.get("symbol", t.get("market", "")))
                        )
                        # Try multiple price fields
                        price = float(
                            t.get(
                                "last_price",
                                t.get("mark_price", t.get("price", t.get("index_price", 0))),
                            )
                        )
                        if symbol and price > 0:
                            self.price_cache[symbol] = price
                            # Also get funding from same response
                            fr = t.get("funding_rate", t.get("rate"))
                            if fr is not None:
                                try:
                                    self.funding_cache[symbol] = float(fr)
                                except Exception:
                                    pass
                    except Exception:
                        continue

            logger.debug(
                f"{self.name}: Loaded {len(self.funding_cache)} funding rates, {len(self.price_cache)} prices"
            )

        except Exception as e:
            logger.error(f"{self.name} load_funding_rates_and_prices error: {e}")

    def _normalize_symbol(self, raw: str) -> str:
        """Normalize symbol to standard format"""
        if not raw:
            return ""
        symbol = raw.replace("_", "-").replace("/", "-").upper()
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        return symbol

    # ═══════════════════════════════════════════════════════════════════════════
    # REST API HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    async def _rest_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        """REST GET with rate limiting"""
        base = getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        url = f"{base.rstrip('/')}{path}"

        try:
            await self.rate_limiter.acquire()
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    self.rate_limiter.penalize_429()
                    return None
                if resp.status >= 400:
                    return None
                data = await resp.json()
                self.rate_limiter.on_success()
                return data
        except Exception as e:
            logger.debug(f"{self.name} REST GET {path} error: {e}")
            return None

    async def _get_signer(self):
        """Get or create SignerClient"""
        if self._signer is not None:
            return self._signer

        if SignerClient is None:
            raise RuntimeError("Lighter SDK not available")

        acc = get_account_manager().get_next_lighter_account()
        priv = acc.get("private_key", config.LIGHTER_PRIVATE_KEY)

        self._signer = SignerClient(
            url=getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
            private_key=priv,
            api_key_index=self._resolved_api_key_index,
            account_index=self._resolved_account_index,
        )
        return self._signer

    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════════

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        return self.price_cache.get(symbol)

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        return self.funding_cache.get(symbol)

    def min_notional_usd(self, symbol: str) -> float:
        meta = self.market_info.get(symbol)
        return float(meta.get("min_notional", 10.0)) if meta else 10.0

    async def get_real_available_balance(self) -> float:
        if time.time() - self._last_balance_update < 2.0:
            return self._balance_cache

        try:
            # REST fallback
            data = await self._rest_get(f"/api/v1/accounts/{self._resolved_account_index}")
            if data:
                acc = data.get("account", data)
                val = float(acc.get("total_asset_value", 0) or 0)
                self._balance_cache = val * 0.95
                self._last_balance_update = time.time()
                return self._balance_cache
        except Exception as e:
            logger.debug(f"{self.name} balance error: {e}")

        return self._balance_cache

    async def fetch_open_positions(self) -> List[dict]:
        """Fetch open positions"""
        try:
            data = await self._rest_get(f"/api/v1/accounts/{self._resolved_account_index}/positions")
            if not data:
                return []

            positions = data.get("positions", data.get("data", []))
            result = []
            for p in positions:
                try:
                    symbol = self._normalize_symbol(p.get("ticker", p.get("symbol", "")))
                    size = float(p.get("size", p.get("position_size", 0)))
                    if abs(size) > 1e-10:
                        result.append({
                            "symbol": symbol,
                            "size": size,
                            "entry_price": float(p.get("entry_price", p.get("avg_entry_price", 0))),
                            "unrealized_pnl": float(p.get("unrealized_pnl", 0)),
                        })
                except Exception:
                    continue
            return result
        except Exception as e:
            logger.debug(f"{self.name} fetch_open_positions error: {e}")
            return []

    async def open_live_position(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Open position on Lighter"""
        if not getattr(config, "LIVE_TRADING", False):
            return True, "DRY_RUN"

        price = self.fetch_mark_price(symbol)
        if not price or price <= 0:
            logger.error(f"{self.name}: No price for {symbol}")
            return False, None

        try:
            meta = self.market_info.get(symbol)
            if not meta:
                return False, None

            market_id = meta.get("i")
            sd = meta.get("sd", 8)
            pd = meta.get("pd", 6)

            qty = Decimal(str(notional_usd)) / Decimal(str(price))
            base_amount = int((qty * (Decimal(10) ** sd)).quantize(Decimal("1")))
            price_int = int(Decimal(str(price)) * (Decimal(10) ** pd))

            signer = await self._get_signer()
            await self.rate_limiter.acquire()

            order_type = SignerClient.ORDER_TYPE_LIMIT if post_only else SignerClient.ORDER_TYPE_MARKET

            tx, resp, err = await signer.create_order(
                market_index=market_id,
                base_amount=base_amount,
                price=price_int,
                is_ask=(side.upper() == "SELL"),
                order_type=order_type,
            )

            if err:
                if "429" in str(err):
                    self.rate_limiter.penalize_429()
                logger.error(f"{self.name} order error: {err}")
                return False, None

            self.rate_limiter.on_success()
            order_id = getattr(resp, "tx_hash", None) or getattr(resp, "order_id", "OK")
            logger.info(f"✅ {self.name} {side} {symbol} ${notional_usd:.2f} → {order_id}")
            return True, str(order_id)

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            logger.error(f"{self.name} open_live_position error: {e}")
            return False, None

    async def close_live_position(
        self,
        symbol: str,
        original_side: str,
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        """Close position"""
        positions = await self.fetch_open_positions()
        pos = next((p for p in positions if p.get("symbol") == symbol), None)

        if not pos:
            return True, None

        size = abs(pos.get("size", 0))
        side = "SELL" if pos.get("size", 0) > 0 else "BUY"
        price = self.fetch_mark_price(symbol)

        if not price:
            return False, None

        return await self.open_live_position(symbol, side, size * price, reduce_only=True)

    async def get_order_fee(self, order_id: str) -> float:
        return 0.0

    # ═══════════════════════════════════════════════════════════════════════════
    # CLEANUP
    # ═══════════════════════════════════════════════════════════════════════════

    async def aclose(self):
        """Cleanup all resources"""
        # Stop WebSocket
        await self.stop_websocket()

        # Close signer
        if self._signer:
            try:
                api_client = getattr(self._signer, "api_client", None)
                if api_client:
                    if hasattr(api_client, "_session") and not api_client._session.closed:
                        await api_client._session.close()
                    if hasattr(api_client, "close"):
                        maybe = api_client.close()
                        if asyncio.iscoroutine(maybe):
                            await maybe
            except Exception as e:
                logger.debug(f"{self.name} signer cleanup: {e}")
            self._signer = None

        # Close base session
        await super().aclose()
        logger.info(f"✅ {self.name} Adapter closed.")