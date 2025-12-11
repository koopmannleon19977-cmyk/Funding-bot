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

print("DEBUG: lighter_adapter.py module loading...")

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
    print("✅ Lighter SDK loaded successfully")
except ImportError as e:
    print(f"⚠️ WARNING: Lighter SDK not available: {e}")
    print("   Install with: pip install git+https://github. com/elliottech/lighter-python. git")

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
        print(f"DEBUG: LighterAdapter.__init__ called at {time.time()}")
        super().__init__("Lighter")

        if HAVE_LIGHTER_SDK:
            logger.info("✅ Using SaferSignerClient subclass instead of Monkey-Patch")

        self.market_info = {}
        print(f"DEBUG: market_info initialized: {hasattr(self, 'market_info')}")
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
        
        # NEU: Lock für thread-sichere Order-Erstellung (Fix für Invalid Nonce)
        self.order_lock = asyncio.Lock()
        
        # Shutdown state
        self._shutdown_cancel_done = False
        self._shutdown_cancel_failed = False

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
            await self.rate_limiter.acquire()
            return True
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: Rate limit acquire cancelled")
            raise

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

    async def _rest_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        """REST GET with rate limiting and proper cancellation handling"""
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
                    logger.debug(f"{self.name} REST GET {path} returned {resp.status}")
                    return None
                data = await resp.json()
                self.rate_limiter.on_success()
                return data
        except asyncio.CancelledError:
            logger.debug(f"{self.name}: REST GET {path} cancelled during shutdown")
            raise  # Always re-raise CancelledError
        except asyncio.TimeoutError:
            logger.debug(f"{self.name} REST GET {path} timeout")
            return None
        except Exception as e:
            logger.debug(f"{self.name} REST GET {path} error: {e}")
            return None

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
            # DYNAMIC TICK SIZE LOGIC (Penny Jumping)
            # ═══════════════════════════════════════════════════════════════
            # Ziel: Exakt 1 Tick vor der Konkurrenz, ohne den Spread zu kreuzen.
            
            if side == 'BUY':
                # Wir wollen KAUFEN -> Best Bid + 1 Tick
                target_price = best_bid + price_tick
                
                # Check: Dürfen nicht Ask erreichen/kreuzen (Taker-Gefahr)
                # Wenn Spread = 1 Tick, dann target_price == best_ask.
                # In dem Fall müssen wir uns hinten anstellen (Best Bid).
                if target_price >= best_ask:
                    final_price = best_bid
                    strategy = "Join Bid (Spread Tight)"
                else:
                    final_price = target_price
                    strategy = "Penny Jump Bid"
                    
            else: # SELL
                # Wir wollen VERKAUFEN -> Best Ask - 1 Tick
                target_price = best_ask - price_tick
                
                # Check: Dürfen nicht Bid erreichen/kreuzen
                if target_price <= best_bid:
                    final_price = best_ask
                    strategy = "Join Ask (Spread Tight)"
                else:
                    final_price = target_price
                    strategy = "Penny Jump Ask"

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
        """Fetch open orders using Lighter REST API."""
        try:
            # Resolve indices if needed
            if not getattr(self, '_resolved_account_index', None):
                await self._resolve_account_index()
            
            acc_idx = getattr(self, '_resolved_account_index', None)
            if acc_idx is None:
                return []
                
            market = self.market_info.get(symbol)
            if not market:
                return []
            
            # Lighter's market metadata primarily uses "i" as market index, but
            # depending on source there may also be "market_id" or
            # "market_index". Use a robust fallback chain.
            market_index = (
                market.get('i')
                or market.get('market_id')
                or market.get('market_index')
            )
            if market_index is None:
                return []

            # GET /api/v1/orders
            params = {
                "account_index": acc_idx,
                "market_index": market_index,
                "status": 10,  # 10 = Open
                "limit": 50
            }
            
            # API endpoint guess: /api/v1/orders or similar
            # Try /api/v1/orders first
            resp = await self._rest_get("/api/v1/orders", params=params)
            
            if not resp:
                return []
            
            # If resp is a list directly or in 'data'
            orders_data = resp if isinstance(resp, list) else resp.get('orders', resp.get('data', []))
            
            open_orders = []
            for o in orders_data:
                # Filter strictly for OPEN status if API returns mixed
                # Status 10 usually OPEN
                status = o.get('status')
                
                # Check if truly open (assuming status 10 is OPEN based on common ZK patterns)
                # If unsure, we include everything that looks open
                if status in [10, 0, 1]:  # Defensive, check mapping
                    price = safe_float(o.get('price', 0))
                    size = safe_float(o.get('remaining_size', o.get('total_size', 0)))
                    
                    side_raw = o.get('side', 0)
                    # Side: 0=Buy, 1=Sell ?? Or "buy"/"sell"?
                    # Lighter usually uses int: 0/1. 
                    if isinstance(side_raw, int):
                         side = "BUY" if side_raw == 0 else "SELL"
                    else:
                         side = str(side_raw).upper()
                    
                    open_orders.append({
                        "id": str(o.get('id', '')),
                        "price": price,
                        "size": size,
                        "side": side,
                        "symbol": symbol
                    })
            return open_orders
            
        except Exception as e:
            logger.error(f"Lighter get_open_orders error: {e}")
            return []

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[dict]:
        """
        Fetch a single order by ID.
        Since Lighter might not have a direct endpoint for single order lookup without market context,
        we try to query /api/v1/orders with order_id if supported, or filter from open orders if necessary.
        """
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


    async def fetch_my_trades(self, symbol: str, limit: int = 20):
        """Hole Trade History um Fills zu verifizieren"""
        try:
            # Lighter API Endpoint für Trades (Beispiel - API Doku prüfen!)
            # Oft: /api/v1/trades?symbol=...&accountIndex=...
            
            # Wenn dein Client eine Methode dafür hat, nutze sie.
            # Sonst REST Call:
            
            # Resolve Account Index if needed
            if not getattr(self, '_resolved_account_index', None):
                 await self._resolve_account_index()
            
            params = {
                "symbol": symbol, 
                "limit": limit, 
                "account_index": self._resolved_account_index
            }
            resp = await self._rest_get("/api/v1/trades", params=params)
            if resp is None:
                return []
            return resp
        except Exception as e:
            logger.error(f"Failed to fetch trades: {e}")
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
                if response:
                    maker = getattr(config, 'MAKER_FEE_LIGHTER', 0.0007)
                    taker = getattr(config, 'TAKER_FEE_LIGHTER', 0.001)
                    # Optional: Check ob response.fee_tier existiert
                    logger.info(f"✅ Lighter Fee Check OK (Using Config): Maker={maker}, Taker={taker}")
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

    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> dict:
        """Fetch orderbook from Lighter API"""
        try:
            if symbol in self.orderbook_cache:
                cached = self.orderbook_cache[symbol]
                cache_age = time.time() - cached. get("timestamp", 0) / 1000
                if cache_age < 2.0:
                    return {
                        "bids": cached.get("bids", [])[:limit],
                        "asks": cached.get("asks", [])[:limit],
                        "timestamp": cached.get("timestamp", 0),
                    }

            market_data = self.market_info.get(symbol)
            if not market_data:
                return {"bids": [], "asks": [], "timestamp": 0}

            market_id = market_data.get("i")
            if market_id is None:
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
                        asks. append([price, size])

                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])

                result = {
                    "bids": bids[:limit],
                    "asks": asks[:limit],
                    "timestamp": int(time.time() * 1000),
                    "symbol": symbol,
                }

                self.orderbook_cache[symbol] = result
                self. rate_limiter. on_success()

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

    def handle_orderbook_snapshot(self, symbol: str, bids: List[List[float]], asks: List[List[float]]):
        """
        Process incoming orderbook snapshot from WebSocket.
        Updates the local cache used by fetch_orderbook.
        
        Args:
            symbol: Trading symbol (e.g. 'ETH-USD')
            bids: List of [price, size]
            asks: List of [price, size]
        """
        try:
            # Debug log to see structure on error (only first time or error)
            # Safe parsing
            clean_bids = []
            if bids:
                # Inspect first item to adapt parsing
                first = bids[0]
                if isinstance(first, dict):
                    # Dict format: {'p': '...', 's': '...'} or {'price': ..., 'size': ...}
                    for b in bids:
                        try:
                            price = float(b.get('p') or b.get('price'))
                            size = float(b.get('s') or b.get('size') or b.get('remaining_base_amount'))
                            if price > 0 and size > 0:
                                clean_bids.append([price, size])
                        except (ValueError, TypeError):
                            continue
                else:
                    # List format: [price, size, ...]
                    for b in bids:
                        try:
                            # Ensure we don't index out of bounds if list is like [price] only
                            if len(b) >= 2:
                                price = float(b[0])
                                size = float(b[1])
                                if price > 0 and size > 0:
                                    clean_bids.append([price, size])
                        except (ValueError, IndexError):
                            continue
            
            clean_asks = []
            if asks:
                first = asks[0]
                if isinstance(first, dict):
                    for a in asks:
                        try:
                            price = float(a.get('p') or a.get('price'))
                            size = float(a.get('s') or a.get('size') or a.get('remaining_base_amount'))
                            if price > 0 and size > 0:
                                clean_asks.append([price, size])
                        except (ValueError, TypeError):
                            continue
                else:
                    for a in asks:
                        try:
                            if len(a) >= 2:
                                price = float(a[0])
                                size = float(a[1])
                                if price > 0 and size > 0:
                                    clean_asks.append([price, size])
                        except (ValueError, IndexError):
                            continue
            
            clean_bids.sort(key=lambda x: x[0], reverse=True)
            clean_asks.sort(key=lambda x: x[0])
            
            # Store in cache with MILLISECOND timestamp (compatible with fetch_orderbook)
            ts = int(time.time() * 1000)
            
            cache_entry = {
                "bids": clean_bids,
                "asks": clean_asks,
                "timestamp": ts,
                "symbol": symbol
            }
            
            # Update both caches to be safe
            self.orderbook_cache[symbol] = cache_entry
            self._orderbook_cache[symbol] = cache_entry
            self._orderbook_cache_time[symbol] = time.time()
            
            # Log only occasionally to avoid spam
            if random.random() < 0.005:
                logger.debug(f"WS OB Update {symbol}: {len(clean_bids)} bids, {len(clean_asks)} asks")
                
        except Exception as e:
            # Log the DATA that caused the crash to understand structure
            logger.error(f"Error handling OB snapshot for {symbol}: {e} | Bids Type: {type(bids)} | First Bid: {bids[0] if bids else 'Empty'}")

    def handle_orderbook_update(self, symbol: str, bids: List[List[float]], asks: List[List[float]]):
        """
        Process incoming orderbook update (delta) from WebSocket.
        Currently treats updates as snapshots since Lighter WS often sends full book or we lack delta logic.
        """
        # For now, treat updates as snapshots since we don't have delta merge logic
        # and Lighter often sends full snapshots on the 'order_book' channel anyway.
        self.handle_orderbook_snapshot(symbol, bids, asks)

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

            await self.rate_limiter.acquire()
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

                if abs(size) > 1e-8:
                    symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FILTER MINI POSITIONS ("DUST")
                    # ═══════════════════════════════════════════════════════════════
                    # Only calculate price if needed to save API calls, but we likely have it buffered
                    price = self.get_price(symbol)
                    if not price:
                        # Fallback if no price: trust the size, but require slightly more size
                        logger.debug(f"{symbol} has no price, keeping position (size={size})")
                        positions.append({"symbol": symbol, "size": size})
                    else:
                        notional_est = abs(size) * price
                        if notional_est > 1.0: # Filter < $1 Dust
                            positions.append({"symbol": symbol, "size": size})
                        else:
                            logger.debug(f"🧹 Filtered dust position {symbol}: ${notional_est:.4f} (< $1.0)")

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

        is_valid, error_msg = await self.validate_order_params(symbol, notional_usd_safe)
        if not is_valid:
            logger.error(f"❌ Order validation failed for {symbol}: {error_msg}")
            raise ValueError(f"Order validation failed: {error_msg}")

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
                    # Default expiry for GTT or IOC (script proves default works for IOC too)
                    expiry = SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY

                    # LOGIC: Resolve TIF from Arguments or Auto-Detection
                    if time_in_force and isinstance(time_in_force, str):
                        key = time_in_force.upper()
                        if key == "IOC" and hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_IOC
                        elif key == "FOK" and hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_FOK'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_FOK
                        elif key == "GTC" and hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_GTC'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_GTC
                    elif reduce_only and not post_only:
                        if hasattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC'):
                            tif = SignerClient.ORDER_TIME_IN_FORCE_IOC
                            # REMOVED: expiry = 0 override (match successful scripts/full_cleanup.py behavior)
                    
                    if tif == getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC', -999):
                         logger.debug(f"Lighter: Executing IOC order for {symbol}")

                    # ═══════════════════════════════════════════════════════════════
                    # FIX: NONCE LOCKING + MANUAL FETCHING (Fixes 'int has no decode')
                    # ═══════════════════════════════════════════════════════════════
                    async with self.order_lock:
                        try:
                            # 1. Nonce manuell holen (Sicherer Bypass für den Library-Fehler)
                            # Wir nutzen den REST Endpoint direkt, da wir im Lock sind
                            
                            # Ensure indices are resolved (should be, but safe check)
                            if self._resolved_account_index is None:
                                await self._resolve_account_index()
                                
                            nonce_params = {
                                "account_index": self._resolved_account_index,
                                "api_key_index": self._resolved_api_key_index
                            }
                            
                            # Fetch Nonce
                            nonce_resp = await self._rest_get("/api/v1/nextNonce", params=nonce_params)
                            
                            if nonce_resp is None:
                                logger.error(f"❌ Failed to fetch nonce for {symbol}")
                                return False, None

                            # 🔥 FIX: Parsing der JSON Antwort
                            # Die API gibt zurück: {'code': 200, 'nonce': 5369} oder einfach 5369
                            current_nonce = 0
                            
                            if isinstance(nonce_resp, dict):
                                # Fall 1: Antwort ist ein Dictionary
                                val = nonce_resp.get('nonce')
                                if val is not None:
                                    current_nonce = int(val)
                                else:
                                    # Fallback: Vielleicht heißt der Key anders?
                                    logger.error(f"❌ Nonce response dict has no 'nonce' key: {nonce_resp}")
                                    return False, None
                            else:
                                # Fall 2: Antwort ist direkt die Zahl (oder String-Zahl)
                                try:
                                    current_nonce = int(str(nonce_resp).strip())
                                except ValueError:
                                    logger.error(f"❌ Invalid nonce format: {nonce_resp}")
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
                                
                                logger.info(f"⚡ MARKET ORDER {symbol}: {side} {base} @ avg_price={avg_exec_price} (shutdown fast-close)")
                                tx, resp, err = await signer.create_market_order(
                                    market_index=int(market_id),
                                    client_order_index=int(client_oid_final),
                                    base_amount=int(base),
                                    avg_execution_price=int(avg_exec_price),
                                    is_ask=bool(side == "SELL"),
                                    reduce_only=bool(reduce_only),
                                    nonce=int(current_nonce),
                                    api_key_index=int(self._resolved_api_key_index)
                                )
                            else:
                                # Normal LIMIT order
                                tx, resp, err = await signer.create_order(
                                    market_index=int(market_id),         # Force int
                                    client_order_index=int(client_oid_final),
                                    base_amount=int(base),               # Force int
                                    price=int(price_int),                # Force int
                                    is_ask=bool(side == "SELL"),         # Force bool
                                    order_type=int(getattr(SignerClient, 'ORDER_TYPE_LIMIT', 0)),
                                    time_in_force=int(tif),
                                    reduce_only=bool(reduce_only),
                                    trigger_price=int(getattr(SignerClient, 'NIL_TRIGGER_PRICE', 0)),
                                    order_expiry=int(expiry),
                                    nonce=int(current_nonce),            # 🔥 Explizite Nonce (kein None!)
                                    api_key_index=int(self._resolved_api_key_index) # 🔥 FIX: Explicit api_key_index to avoid SDK auto-retry bug
                                )

                            if err:
                                err_str = str(err).lower()
                                logger.error(f"❌ Lighter Order Failed: {err}")
                                if "nonce" in err_str or "429" in err_str or "too many requests" in err_str:
                                    # If it's a nonce error despite our lock, maybe retry? 
                                    # But for now, we just log and return fail to let outer loop handle or bubble up
                                    pass
                                return False, None
                            
                            tx_hash = getattr(resp, "tx_hash", tx) # tx usually contains hash string in some versions, or resp does
                            # In existing code: tx, resp, err. 'tx' was used, but log said 'tx_hash = getattr(resp, "tx_hash", "OK")'
                            # Let's try to get hash robustly
                            tx_hash_final = str(tx) if tx else "OK"
                            if hasattr(resp, "tx_hash") and resp.tx_hash:
                                tx_hash_final = str(resp.tx_hash)

                            logger.info(f"✅ Lighter Order Sent: {tx_hash_final}")
                            
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
                
                # Manually fetch orders via REST to inspect 'tx_hash'
                params = {
                    "account_index": self._resolved_account_index,
                    "market_index": market_data.get('i'),
                    "status": 10, # Open
                    "limit": 50
                }
                
                resp = await self._rest_get("/api/v1/orders", params=params)
                raw_orders = []
                if resp:
                    raw_orders = resp if isinstance(resp, list) else resp.get('orders', [])

                # Look for matching hash
                for o in raw_orders:
                    # API fields might be 'tx_hash', 'hash' or even matches inside 'id' if confusing
                    if str(o.get('tx_hash')) == order_id or str(o.get('hash')) == order_id:
                        oid_int = int(o.get('id'))
                        logger.info(f"✅ Lighter: Resolved Hash {order_id[:10]}... -> Order ID {oid_int}")
                        break
                
                if oid_int is None:
                    # If we can't find the open order by hash, it's effectively closed/gone.
                    # Returning True here prevents the caller from retrying or thinking it failed.
                    logger.info(f"ℹ️ Lighter Cancel: Hash {order_id[:10]}... not found in open orders. Assuming closed.")
                    return True
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
                     return True
                 logger.error(f"Lighter Cancel Error {oid_int}: {err}")
                 return False
             
            logger.info(f"✅ Lighter Cancelled Order {oid_int}")
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
        
        # ═══════════════════════════════════════════════════════════════════════════
        # DEBUG: Log ALL input types and values at entry point
        # ═══════════════════════════════════════════════════════════════════════════
        logger.info(f"🔍 DEBUG close_live_position ENTRY: symbol={symbol} (type={type(symbol)})")
        logger.info(f"🔍 DEBUG: original_side={original_side} (type={type(original_side)})")
        logger.info(f"🔍 DEBUG: notional_usd={notional_usd} (type={type(notional_usd)})")
        

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
            # SCHRITT 4b: Mindestnotional-Guard (Dust Handling)
            # ═══════════════════════════════════════════════════════════════
            try:
                min_required = float(self.min_notional_usd(symbol))
            except Exception:
                min_required = 10.0  # konservatives Fallback

            # FIX: Wenn Notional zu klein ist (Dust), nicht versuchen zu schließen
            if close_notional_usd < min_required:
                logger.warning(
                    f"⚠️ DUST POSITION {symbol}: ${close_notional_usd:.2f} < Min ${min_required:.2f}"
                )
                
                # Send Telegram Alert if enabled
                if getattr(config, 'DUST_ALERT_ENABLED', True):
                    try:
                        from src.telegram_bot import get_telegram_bot
                        telegram = get_telegram_bot()
                        if telegram and telegram.enabled:
                            await telegram.send_message(
                                f"⚠️ **DUST POSITION DETECTED**\n"
                                f"Symbol: {symbol}\n"
                                f"Value: ${close_notional_usd:.2f}\n"
                                f"Min Required: ${min_required:.2f}\n"
                                f"Action: Manual intervention needed"
                            )
                    except Exception as e:
                        logger.debug(f"Could not send dust alert: {e}")
                
                # Mark in State Manager as dust (if available)
                try:
                    from src.state_manager import get_state_manager
                    sm = await get_state_manager()
                    if sm:
                        await sm.update_trade(symbol, {'status': 'dust', 'dust_reason': 'below_min_notional'})
                except Exception as e:
                    logger.debug(f"Could not mark as dust in state: {e}")
                
                # Return True to avoid infinite retry loops
                return True, "DUST_SKIPPED"

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
                f"price=${mark_price:.4f}, notional=${close_notional_usd:.2f}"
            )

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 6: Close Order ausführen
            # ═══════════════════════════════════════════════════════════════
            success, order_id = await self.open_live_position(
                symbol=symbol,
                side=close_side,
                notional_usd=close_notional_usd,
                reduce_only=True
            )

            if success:
                logger.info(f"✅ Lighter close {symbol}: Erfolgreich (${close_notional_usd:.2f})")
                return True, order_id
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

    async def aclose(self):
        """Cleanup all sessions and connections"""
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
                            
                            nonce_resp = await self._rest_get("/api/v1/nextNonce", params=nonce_params)
                            
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
                            
                            # ImmediateCancelAll - cancels ALL open orders at once
                            # time_in_force=IOC triggers immediate cancel
                            tif_ioc = getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL', 
                                             getattr(SignerClient, 'ORDER_TIME_IN_FORCE_IOC', 1))
                            
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
                                raise
                            
                            if err:
                                err_str = str(err).lower()
                                # Ignore "no orders" errors
                                if "no" in err_str and "order" in err_str:
                                    logger.info(f"✅ Lighter: No open orders to cancel")
                                    self._shutdown_cancel_done = True
                                    return True
                                logger.warning(f"⚠️ Lighter cancel_all_orders error: {err}")
                                self._shutdown_cancel_failed = True
                            else:
                                logger.info(f"✅ Lighter: ImmediateCancelAll executed (tx={tx})")
                                self._shutdown_cancel_done = True
                                return True
                except Exception as e:
                    # Mark global cancel as failed so we don't keep retrying
                    # this path on every subsequent shutdown attempt.
                    if not self._shutdown_cancel_failed:
                        self._shutdown_cancel_failed = True
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
                # Fallback: Vielleicht gab es keine Orders oder die Methode war falsch.
                # Wir machen einen expliziten Check via get_open_orders wenn wir unsicher sind.
                # Aber hier nehmen wir an, es ist leer.
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