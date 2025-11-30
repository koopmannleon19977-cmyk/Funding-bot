# src/adapters/lighter_adapter.py
import asyncio
import aiohttp
import json
import logging
import time
import random
import websockets
from typing import Dict, Tuple, Optional, List
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP

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
    print("   Install with: pip install git+https://github.com/elliottech/lighter-python.git")

from .base_adapter import BaseAdapter
from src.rate_limiter import LIGHTER_RATE_LIMITER, rate_limited, Exchange, with_rate_limit

logger = logging.getLogger(__name__)

MARKET_OVERRIDES = {
    "ASTER-USD": {"ss": Decimal("1"), "sd": 8},
    "HYPE-USD": {"ss": Decimal("0.01"), "sd": 6},
    "MEGA-USD": {"ss": Decimal("99999999")},
}


class LighterAdapter(BaseAdapter):
    def __init__(self):
        print(f"DEBUG: LighterAdapter.__init__ called at {time.time()}")
        super().__init__("Lighter")
        self.market_info = {}
        print(f"DEBUG: market_info initialized: {hasattr(self, 'market_info')}")
        self.funding_cache = {}
        self.price_cache = {}
        self.orderbook_cache = {}
        self.price_update_event = None
        self._ws_message_queue = asyncio.Queue()
        self._signer = None
        self._resolved_account_index = None
        self._resolved_api_key_index = None
        self.semaphore = asyncio.Semaphore(10)
        self.rate_limiter = LIGHTER_RATE_LIMITER
        self._last_market_cache_at = None
        self._balance_cache = 0.0
        self._last_balance_update = 0.0

    async def refresh_market_limits(self, symbol: str) -> dict:
        """
        Fetch fresh market limits from Lighter API.
        Call this before placing orders to ensure limits are current.
        """
        try:
            # Versuche Market-Info direkt von der API zu laden
            market_index = None
            for idx, info in self.market_info.items():
                if info.get('symbol') == symbol or idx == symbol:
                    market_index = info.get('market_index', idx)
                    break
            
            if market_index is None:
                logger.warning(f"⚠️ Market index not found for {symbol}")
                return self.market_info.get(symbol, {})
            
            # API Call
            url = f"{self.base_url}/api/v1/market?market_index={market_index}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data:
                            # Update cached market info
                            old_info = self.market_info.get(symbol, {})
                            self.market_info[symbol].update(data)
                            
                            # Log changes
                            new_min_base = data.get('min_base_amount', old_info.get('min_base_amount'))
                            old_min_base = old_info.get('min_base_amount')
                            if new_min_base != old_min_base:
                                logger.warning(
                                    f"⚠️ {symbol} min_base_amount changed: {old_min_base} -> {new_min_base}"
                                )
                            
                            logger.info(f"✅ Refreshed market limits for {symbol}")
                            return self.market_info.get(symbol, {})
                    else:
                        logger.warning(f"Failed to refresh {symbol} limits: HTTP {response.status}")
                        
        except asyncio.TimeoutError:
            logger.warning(f"Timeout refreshing market limits for {symbol}")
        except Exception as e:
            logger.error(f"Error refreshing market limits for {symbol}: {e}")
        
        return self.market_info.get(symbol, {})

    async def validate_order_params(self, symbol: str, size_usd: float) -> Tuple[bool, str]:
        """
        Validate order parameters before sending to API.
        Returns (is_valid, error_message). 
        """
        market_info = self.market_info.get(symbol, {})
        
        if not market_info:
            return False, f"Market {symbol} not found"
        
        price = self.get_price(symbol)
        if not price or price <= 0:
            return False, f"Invalid price for {symbol}: {price}"
        
        # Calculate quantity
        quantity = size_usd / price
        
        # Check minimums
        min_base = float(market_info.get('min_base_amount', 0.0001))
        min_notional = float(market_info.get('min_notional', 5.0))
        
        if quantity < min_base:
            return False, f"Quantity {quantity:.6f} < min_base {min_base}"
        
        notional = quantity * price
        if notional < min_notional:
            return False, f"Notional ${notional:.2f} < min_notional ${min_notional}"
        
        return True, "OK"

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
                "order_details",
                "order_detail",
                "get_order",
                "get_order_by_id",
                "order",
                "order_status",
                "order_info",
                "get_order_info",
                "retrieve_order",
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
                "filled",
                "filled_amount",
                "filled_amount_of_synthetic",
                "filled_quantity",
                "filled_size",
                "filled_amount_of_quote",
            ]
            price_keys = ["price", "avg_price", "filled_price"]

            fee_abs = _get(resp, fee_keys)
            filled = _get(resp, filled_keys)
            price = _get(resp, price_keys)

            if fee_abs is None or filled is None or price is None:
                nested = None
                if isinstance(resp, dict):
                    nested = resp.get("order") or resp.get("data") or resp.get("result")
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
                    fee_usd = float(str(fee_abs))
                    filled_qty = float(str(filled))
                    order_price = float(str(price))
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
                                self.price_cache[symbol] = float(mark_price)
                                updated += 1

                        if updated > 0:
                            self. rate_limiter.on_success()
                            if (
                                hasattr(self, "price_update_event")
                                and self.price_update_event
                            ):
                                self.price_update_event.set()
                            logger.debug(f"Lighter REST: Updated {updated} prices")
                except Exception as e:
                    if "429" in str(e):
                        self.rate_limiter.penalize_429()
                    else:
                        logger.debug(f"Lighter REST price poll error: {e}")

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info("🛑 Lighter REST price poller stopped")
                break
            except Exception as e:
                logger. error(f"Lighter REST price poller error: {e}")
                await asyncio. sleep(interval * 2)

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
                    rate = float(fr.rate) if fr.rate else 0.0
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
        return getattr(
            config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"
        )

    async def _auto_resolve_indices(self) -> Tuple[int, int]:
        return int(config. LIGHTER_ACCOUNT_INDEX), int(config.LIGHTER_API_KEY_INDEX)

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
            self._signer = SignerClient(
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

                    # ═══════════════════════════════════════════════════════════
                    # API gibt strings/uint8 zurück - IMMER explizit konvertieren!
                    # Quelle: API_CONTEXT.md orderBookDetails.html
                    # ═══════════════════════════════════════════════════════════
                    
                    def safe_int(val, default=8):
                        """Convert uint8 or any value to int safely."""
                        if val is None:
                            return default
                        try:
                            return int(val)
                        except (ValueError, TypeError):
                            return default

                    def safe_decimal(val, default="0"):
                        """Convert string to Decimal safely."""
                        if val is None or val == "":
                            return Decimal(default)
                        try:
                            # API gibt strings wie "0.001" zurück
                            return Decimal(str(val))
                        except Exception:
                            return Decimal(default)

                    def safe_float(val, default=0.0):
                        """Convert to float safely."""
                        if val is None:
                            return default
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return default

                    # API Response Felder (aus orderBookDetails.html):
                    # - size_decimals: uint8
                    # - price_decimals: uint8
                    # - min_base_amount: string
                    # - min_quote_amount: string
                    # - last_trade_price: double
                    
                    size_decimals = safe_int(getattr(m, 'size_decimals', None), 8)
                    price_decimals = safe_int(getattr(m, 'price_decimals', None), 6)
                    min_base = safe_decimal(getattr(m, 'min_base_amount', None), "0.00000001")
                    min_quote = safe_decimal(getattr(m, 'min_quote_amount', None), "0.00001")
                    
                    market_data = {
                        'i': getattr(m, 'market_id', market_id),
                        'sd': size_decimals,
                        'pd': price_decimals,
                        'ss': min_base,           # step_size als Decimal
                        'mps': min_quote,         # min_price_step als Decimal
                        'min_notional': float(min_quote) if min_quote else 10.0,
                        'min_quantity': float(min_base) if min_base else 0.0,
                    }

                    if normalized_symbol in MARKET_OVERRIDES:
                        market_data.update(MARKET_OVERRIDES[normalized_symbol])

                    self.market_info[normalized_symbol] = market_data

                    # last_trade_price ist double laut API
                    price = getattr(m, 'last_trade_price', None)
                    if price is not None:
                        self.price_cache[normalized_symbol] = safe_float(price)
                        
                    self.rate_limiter.on_success()
                    return True
                    
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate limit" in error_str:
                    self.rate_limiter.penalize_429()
                return False
            return False

    async def _load_funding_rates(self):
        """Load funding rates from Lighter API."""
        try:
            # Endpoint: GET /api/v1/funding-rates
            # Response: funding_rates[].rate ist double
            async with aiohttp.ClientSession() as session:
                url = f"{self._get_base_url()}/api/v1/funding-rates"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        funding_rates = data.get('funding_rates', [])
                        
                        for fr in funding_rates:
                            # Nur Lighter rates nehmen
                            if fr.get('exchange') == 'lighter':
                                symbol = fr.get('symbol', '')
                                rate = fr.get('rate')  # double laut API
                                
                                if symbol and rate is not None:
                                    # Normalize symbol
                                    if not symbol.endswith('-USD'):
                                        symbol = f"{symbol}-USD"
                                    self.funding_cache[symbol] = float(rate)
                        
                        logger.debug(f"Lighter: Loaded {len(self.funding_cache)} funding rates")
                        return True
        except Exception as e:
            logger.error(f"Failed to load Lighter funding rates: {e}")
        return False

    async def load_market_cache(self, force: bool = False, max_retries: int = 1):
        """Load market data from Lighter API"""
        print(
            f"DEBUG: load_market_cache called, has market_info: {hasattr(self, 'market_info')}"
        )

        if not getattr(config, "LIVE_TRADING", False):
            logger.warning("LIVE_TRADING=False, skipping market load")
            return

        if not HAVE_LIGHTER_SDK:
            logger.error("❌ Lighter SDK not installed - cannot load markets")
            return

        if (
            not force
            and self._last_market_cache_at
            and (time.time() - self._last_market_cache_at < 300)
        ):
            return

        logger.info("⚙️ Lighter: Loading markets...")

        try:
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)

            print("DEBUG: Calling order_books()...")

            market_list = None
            api_client = getattr(signer, "api_client", None)

            try:
                if hasattr(order_api, "order_books"):
                    market_list = await order_api. order_books()
            except Exception as e:
                logger.debug(f"order_books() failed: {e}")
                market_list = None

            if not market_list:
                try:
                    resp = None
                    if api_client is not None:
                        if hasattr(api_client, "get"):
                            resp = await api_client. get("/order-books")
                        elif hasattr(api_client, "request"):
                            resp = await api_client.request("GET", "/order-books")

                    if isinstance(resp, dict):
                        markets_data = (
                            resp. get("data")
                            or resp.get("order_books")
                            or resp.get("result")
                            or []
                        )
                    elif isinstance(resp, list):
                        markets_data = resp
                    else:
                        markets_data = []

                    class _ML:
                        pass

                    ml = _ML()
                    objs = []
                    for m in markets_data:
                        if hasattr(m, "market_id") or isinstance(m, type):
                            objs.append(m)
                        elif isinstance(m, dict):

                            class M:
                                pass

                            o = M()
                            for k, v in m.items():
                                setattr(o, k, v)
                            objs.append(o)
                    ml.order_books = objs
                    market_list = ml
                except Exception as e:
                    logger.debug(f"Fallback market load failed: {e}")
                    market_list = None

            print(f"DEBUG: market_list type: {type(market_list)}")

            if (
                not market_list
                or not hasattr(market_list, "order_books")
                or not market_list.order_books
            ):
                logger.warning("⚠️ Lighter: No markets from API")
                return

            all_ids = [
                m.market_id
                for m in market_list.order_books
                if hasattr(m, "market_id")
            ]
            total_markets = len(all_ids)

            BATCH_SIZE = 10
            SLEEP_BETWEEN_BATCHES = 1.0
            MAX_RETRIES_LOCAL = max_retries

            successful_loads = 0
            processed_ids = set()

            for batch_num, i in enumerate(range(0, len(all_ids), BATCH_SIZE), start=1):
                batch_ids = all_ids[i : i + BATCH_SIZE]

                if batch_num > 1:
                    await asyncio.sleep(SLEEP_BETWEEN_BATCHES)

                for retry in range(MAX_RETRIES_LOCAL + 1):
                    tasks = [
                        self._fetch_single_market(order_api, mid) for mid in batch_ids
                    ]
                    results = await asyncio. gather(*tasks, return_exceptions=True)

                    batch_loaded_ids = []
                    for mid in batch_ids:
                        if mid in processed_ids:
                            continue
                        if any(m["i"] == mid for m in self.market_info. values()):
                            batch_loaded_ids.append(mid)
                            processed_ids.add(mid)

                    batch_success_count = len(batch_loaded_ids)
                    successful_loads += batch_success_count

                    if batch_success_count == len(batch_ids):
                        break

                    has_429 = any(
                        isinstance(r, Exception) and "429" in str(r) for r in results
                    )

                    if has_429 and retry < MAX_RETRIES_LOCAL:
                        wait_time = (retry + 1) * 2
                        logger.warning(
                            f"⚠️ Rate Limited.  Retry {retry+1}/{MAX_RETRIES_LOCAL} in {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    break

            self._last_market_cache_at = time.time()
            success_rate = (
                (successful_loads / total_markets * 100) if total_markets > 0 else 0
            )

            logger.info(
                f"✅ Lighter: {successful_loads}/{total_markets} markets loaded ({success_rate:.1f}%)"
            )

        except Exception as e:
            logger.error(f"❌ Lighter Market Cache Error: {e}")
            import traceback

            traceback.print_exc()

    async def initialize(self):
        """Initialize the adapter"""
        try:
            logger.info("🔄 Lighter: Initializing...")

            if not HAVE_LIGHTER_SDK:
                logger. error("❌ Lighter SDK not installed")
                return

            from lighter.api_client import ApiClient

            self. api_client = ApiClient()
            self.api_client. configuration. host = self._get_base_url()

            await self.load_market_cache()

            if not self. market_info:
                logger.error("❌ Lighter: Market cache empty after load")
                return

            logger.info(f"✅ Lighter: Loaded {len(self. market_info)} markets")
        except ImportError as e:
            logger.error(f"❌ Lighter SDK import error: {e}")
        except Exception as e:
            logger.error(f"❌ Lighter init error: {e}")

    async def load_funding_rates_and_prices(self):
        """Lädt Funding Rates und Preise von der Lighter REST API"""
        if not getattr(config, "LIVE_TRADING", False):
            return

        if not HAVE_LIGHTER_SDK:
            return

        try:
            signer = await self._get_signer()
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

                    rate_float = float(rate) if rate else 0.0

                    for symbol, data in self.market_info. items():
                        if data.get("i") == market_id:
                            self.funding_cache[symbol] = rate_float
                            updated += 1
                            break

                self.rate_limiter.on_success()
                logger.debug(f"Lighter: Loaded {updated} funding rates")

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            else:
                logger. error(f"Lighter Funding Fetch Error: {e}")

    async def fetch_funding_rates(self):
        """Holt Funding Rates - mit dynamischem Rate Limiting"""
        return await with_rate_limit(
            Exchange. LIGHTER, "default", lambda: self.load_funding_rates_and_prices()
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

            response = await order_api.order_book_orders(
                market_id=market_id, limit=limit
            )

            if response and hasattr(response, "asks") and hasattr(response, "bids"):
                bids = []
                for b in response.bids or []:
                    price = float(getattr(b, "price", 0))
                    size = float(getattr(b, "remaining_base_amount", 0))
                    if price > 0 and size > 0:
                        bids.append([price, size])

                asks = []
                for a in response. asks or []:
                    price = float(getattr(a, "price", 0))
                    size = float(getattr(a, "remaining_base_amount", 0))
                    if price > 0 and size > 0:
                        asks.append([price, size])

                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])

                result = {
                    "bids": bids[:limit],
                    "asks": asks[:limit],
                    "timestamp": int(time.time() * 1000),
                    "symbol": symbol,
                }

                self.orderbook_cache[symbol] = result
                self.rate_limiter.on_success()

                return result

        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "rate limit" in err_str:
                self.rate_limiter.penalize_429()
            logger.debug(f"Lighter orderbook {symbol}: {e}")

        return self.orderbook_cache.get(
            symbol, {"bids": [], "asks": [], "timestamp": 0}
        )

    async def fetch_open_interest(self, symbol: str) -> float:
        if not hasattr(self, "_oi_cache"):
            self._oi_cache = {}
            self._oi_cache_time = {}

        now = time.time()
        if symbol in self._oi_cache:
            if now - self._oi_cache_time. get(symbol, 0) < 60.0:
                return self._oi_cache[symbol]

        try:
            if not HAVE_LIGHTER_SDK:
                return 0.0

            market_data = self. market_info.get(symbol)
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
                    oi = float(details.open_interest)
                    self._oi_cache[symbol] = oi
                    self._oi_cache_time[symbol] = now
                    self.rate_limiter.on_success()
                    return oi

                if hasattr(details, "volume_24h"):
                    vol = float(details. volume_24h)
                    self._oi_cache[symbol] = vol
                    self._oi_cache_time[symbol] = now
                    self. rate_limiter. on_success()
                    return vol
            return 0.0
        except Exception as e:
            if "429" in str(e). lower():
                self.rate_limiter.penalize_429()
            return 0.0

    def min_notional_usd(self, symbol: str) -> float:
        """Berechne Minimum Notional"""
        HARD_MIN_USD = 5.0
        SAFETY_BUFFER = 1.10

        data = self.market_info.get(symbol)
        if not data:
            return HARD_MIN_USD

        try:
            raw_price = self.fetch_mark_price(symbol)
            try:
                price = float(raw_price) if raw_price is not None else 0.0
            except (ValueError, TypeError):
                price = 0.0
            
            if price <= 0:
                return HARD_MIN_USD

            try:
                min_notional_api = float(data.get("min_notional", 0) or 0)
            except (ValueError, TypeError):
                min_notional_api = 0.0
            
            try:
                min_qty = float(data.get("min_quantity", 0) or 0)
            except (ValueError, TypeError):
                min_qty = 0.0
            
            min_qty_usd = min_qty * price if min_qty > 0 else 0
            api_min = max(min_notional_api, min_qty_usd)
            safe_min = api_min * SAFETY_BUFFER

            return max(HARD_MIN_USD, safe_min)
        except Exception:
            return HARD_MIN_USD

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Funding Rate aus Cache"""
        rate = self.funding_cache.get(symbol)
        if rate is None:
            return None

        rate_float = float(rate)

        if abs(rate_float) > 0.1:
            rate_float = rate_float / 8.0

        return rate_float

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Mark Price aus Cache"""
        if symbol not in self.market_info:
            return None

        if symbol in self.price_cache:
            price = self.price_cache[symbol]
            if price and price > 0:
                return float(price)

        return None

    def get_price(self, symbol: str) -> Optional[float]:
        """Alias for fetch_mark_price - used by order execution code."""
        return self.fetch_mark_price(symbol)

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
                    if (
                        response
                        and getattr(response, "accounts", None)
                        and response.accounts[0]
                    ):
                        acc = response.accounts[0]
                        buying = getattr(acc, "buying_power", None) or getattr(
                            acc, "total_asset_value", "0"
                        )
                        try:
                            val = float(buying or 0)
                        except Exception:
                            val = 0.0

                    safe_balance = val * 0.95

                    self._balance_cache = safe_balance
                    self._last_balance_update = time.time()

                    logger.debug(
                        f"Lighter Balance: Raw=${val:.2f}, Safe=${safe_balance:.2f}"
                    )
                    break
                except Exception as e:
                    if "429" in str(e):
                        await asyncio.sleep(2)
                        continue
                    if isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError)):
                        await asyncio. sleep(1)
                        continue
                    raise
        except Exception as e:
            if "429" not in str(e):
                logger.error(f"❌ Lighter Balance Error: {e}")

        return self._balance_cache

    async def fetch_open_positions(self) -> List[dict]:
        if not getattr(config, "LIVE_TRADING", False):
            return []

        if not HAVE_LIGHTER_SDK:
            return []

        try:
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)

            await self.rate_limiter.acquire()
            await asyncio.sleep(0.2)

            response = await account_api.account(
                by="index", value=str(self._resolved_account_index)
            )

            if not response or not response.accounts or not response.accounts[0]:
                return []

            account = response.accounts[0]

            if not hasattr(account, "positions") or not account. positions:
                return []

            positions = []
            for p in account.positions:
                symbol_raw = getattr(p, "symbol", None)
                position_qty = getattr(p, "position", None)
                sign = getattr(p, "sign", None)

                if not symbol_raw or position_qty is None or sign is None:
                    continue

                multiplier = 1 if int(sign) == 0 else -1
                size = float(position_qty) * multiplier

                if abs(size) > 1e-8:
                    symbol = (
                        f"{symbol_raw}-USD"
                        if not symbol_raw.endswith("-USD")
                        else symbol_raw
                    )
                    positions. append({"symbol": symbol, "size": size})

            logger.info(f"Lighter: Found {len(positions)} open positions")
            return positions

        except Exception as e:
            logger. error(f"Lighter Positions Error: {e}")
            return []

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        """Scale amounts - BULLETPROOF VERSION."""
        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f"Metadata missing for {symbol}")

        # ═══════════════════════════════════════════════════════════════
        # EXAKT wie im TypeScript SDK:
        # baseScale = Math.pow(10, details.size_decimals)
        # quoteScale = Math.pow(10, details.price_decimals)
        # ═══════════════════════════════════════════════════════════════
        
        def to_int(val, default=8) -> int:
            if val is None:
                return default
            try:
                return int(float(str(val)))
            except:
                return default

        def to_decimal(val, default="0") -> Decimal:
            if val is None:
                return Decimal(default)
            try:
                return Decimal(str(val))
            except:
                return Decimal(default)

        # Get decimals from API response
        size_decimals = to_int(data.get('sd'), 8)
        price_decimals = to_int(data.get('pd'), 6)
        
        # Calculate scales (EXACT from SDK)
        base_scale = Decimal(10) ** size_decimals
        quote_scale = Decimal(10) ** price_decimals
        
        # Get min values
        min_base_amount = to_decimal(data.get('ss'), "0.00000001")
        
        ZERO = Decimal("0")

        # Ensure minimum notional ($10 safety)
        notional = qty * price
        if notional < Decimal("10.5"):
            if price > ZERO:
                qty = Decimal("10.5") / price

        # Ensure minimum quantity
        if min_base_amount > ZERO and qty < min_base_amount:
            qty = min_base_amount * Decimal("1.05")

        # Scale to integers (EXACT from SDK: Math.round(amount * baseScale))
        scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
        scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_DOWN if side == 'SELL' else ROUND_UP))

        # CRITICAL: Validate price_int is not 0
        if scaled_price == 0:
            raise ValueError(f"{symbol}: scaled_price is 0! price={price}, pd={price_decimals}")

        if scaled_base == 0:
            raise ValueError(f"{symbol}: scaled_base is 0!")

        return scaled_base, scaled_price

    @rate_limited(Exchange. LIGHTER)
    async def open_live_position(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        **kwargs
    ) -> Tuple[bool, Optional[str]]:
        """Open a position on Lighter exchange."""
        # ═══════════════════════════════════════════════════════════════
        # PRE-VALIDATION
        # ═══════════════════════════════════════════════════════════════
        # Refresh market limits before order (optional - kann API-Calls sparen wenn auskommentiert)
        # await self.refresh_market_limits(symbol)
        # Validate order params
        is_valid, error_msg = await self.validate_order_params(symbol, notional_usd)
        if not is_valid:
            logger.error(f"❌ Order validation failed for {symbol}: {error_msg}")
            raise ValueError(f"Order validation failed: {error_msg}")
        # Get price if not provided
        if price is None:
            price = self.get_price(symbol)
            if not price:
                raise ValueError(f"No price available for {symbol}")
        logger.info(f"🚀 LIGHTER OPEN {symbol}: side={side}, size_usd=${notional_usd:.2f}, price=${price:.6f}")
        # ...existing code...

        try:
            market_id = self.market_info[symbol]["i"]
            price_decimal = Decimal(str(price))
            notional_decimal = Decimal(str(notional_usd))
            slippage = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
            slippage_multiplier = (
                Decimal(1) + (slippage / Decimal(100))
                if side == "BUY"
                else Decimal(1) - (slippage / Decimal(100))
            )
            limit_price = price_decimal * slippage_multiplier
            qty = notional_decimal / limit_price

            base, price_int = self._scale_amounts(symbol, qty, limit_price, side)
            
            if base == 0:
                return False, None

            signer = await self._get_signer()
            max_retries = 2

            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        await asyncio.sleep(0.5 * attempt)

                    client_oid = int(time.time() * 1000) + random.randint(0, 99999)

                    await self.rate_limiter.acquire()
                    tx, resp, err = await signer.create_order(
                        market_index=market_id,
                        client_order_index=client_oid,
                        base_amount=base,
                        price=price_int,
                        is_ask=(side == "SELL"),
                        order_type=SignerClient.ORDER_TYPE_LIMIT,
                        time_in_force=SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                        reduce_only=reduce_only,
                        trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                        order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,
                    )

                    if err:
                        err_str = str(err).lower()
                        if (
                            "nonce" in err_str
                            or "429" in err_str
                            or "too many requests" in err_str
                        ):
                            if attempt < max_retries:
                                continue
                        logger.error(f"Lighter Order Error: {err}")
                        return False, None

                    tx_hash = getattr(resp, "tx_hash", "OK")
                    logger.info(f"Lighter Order: {tx_hash}")
                    return True, str(tx_hash)

                except Exception as inner_e:
                    logger.error(f"Lighter Inner Error: {inner_e}")
                    return False, None

            return False, None

        except Exception as e:
            logger. error(f"Lighter Execution Error: {e}")
            return False, None

    @rate_limited(Exchange. LIGHTER, "sendTx")
    async def close_live_position(
        self, symbol: str, original_side: str, notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        """Close position using ACTUAL position size from exchange"""
        if not HAVE_LIGHTER_SDK:
            return False, None

        max_retries = 3

        for attempt in range(max_retries):
            try:
                positions = await self.fetch_open_positions()
                actual_pos = next(
                    (p for p in (positions or []) if p. get("symbol") == symbol),
                    None,
                )

                if not actual_pos or abs(actual_pos. get("size", 0)) < 1e-8:
                    logger.info(f"✅ Lighter {symbol} already closed")
                    return True, None

                actual_size = actual_pos.get("size", 0)
                actual_size_abs = abs(actual_size)

                if actual_size > 0:
                    close_side = "SELL"
                else:
                    close_side = "BUY"

                price = self.fetch_mark_price(symbol)
                if not price or price <= 0:
                    return False, None

                actual_notional = actual_size_abs * price
                
                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: Slippage auf 3% begrenzen (API Error 21733 vermeiden)
                # Laut API-Doku: "order price flagged as an accidental price"
                # ═══════════════════════════════════════════════════════════════
                slippage = Decimal('0.03')  # 3% - sicher innerhalb der API-Limits
                price_decimal = Decimal(str(price))

                if close_side == "BUY":
                    limit_price = price_decimal * (Decimal(1) + slippage)
                else:
                    limit_price = price_decimal * (Decimal(1) - slippage)

                logger.info(
                    f"🔻 Lighter CLOSE {symbol}: size={actual_size_abs:.6f}, side={close_side}"
                )

                qty = Decimal(str(actual_notional)) / limit_price
                base, price_int = self._scale_amounts(symbol, qty, limit_price, close_side)

                if base == 0:
                    return False, None

                signer = await self._get_signer()
                client_oid = int(time.time() * 1000) + random.randint(0, 99999)
                market_id = self. market_info[symbol]["i"]

                await self.rate_limiter.acquire()
                tx, resp, err = await signer.create_order(
                    market_index=market_id,
                    client_order_index=client_oid,
                    base_amount=base,
                    price=price_int,
                    is_ask=(close_side == "SELL"),
                    order_type=SignerClient.ORDER_TYPE_LIMIT,
                    time_in_force=SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=True,
                    trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                    order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,
                )

                if err:
                    logger.error(f"Order error: {err}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    return False, None

                self.rate_limiter.on_success()

                tx_hash = getattr(resp, "tx_hash", "OK")
                logger. info(f"Order sent: {tx_hash}")

                await asyncio.sleep(2 + attempt)
                updated_positions = await self.fetch_open_positions()
                still_open = any(
                    p["symbol"] == symbol and abs(p. get("size", 0)) > 1e-8
                    for p in (updated_positions or [])
                )

                if not still_open:
                    logger.info(f"✅ Lighter {symbol} CLOSED")
                    return True, str(tx_hash)
                else:
                    if attempt < max_retries - 1:
                        continue
                    return False, str(tx_hash)

            except Exception as e:
                logger.error(f"Close exception: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False, None

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
                            if hasattr(api_client, "session") and hasattr(
                                api_client.session, "close"
                            ):
                                maybe2 = api_client.session.close()
                                if asyncio.iscoroutine(maybe2):
                                    await maybe2
                        except Exception:
                            pass

                if hasattr(self._signer, "close"):
                    maybe_sig = self._signer.close()
                    if asyncio.iscoroutine(maybe_sig):
                        await maybe_sig

            except Exception as e:
                logger.debug(f"Lighter cleanup warning: {e}")
            finally:
                self._signer = None

        logger.info("✅ Lighter Adapter closed")

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol"""
        if not HAVE_LIGHTER_SDK:
            return False

        try:
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)

            market_data = self.market_info.get(symbol)
            if not market_data:
                return False

            market_id = market_data.get("i")

            orders_resp = None
            await self.rate_limiter.acquire()
            candidate_methods = [
                "list_orders",
                "get_open_orders",
                "get_orders",
                "orders",
                "list_open_orders",
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
                orders_list = orders_resp. orders
            elif isinstance(orders_resp, dict):
                orders_list = (
                    orders_resp.get("orders")
                    or orders_resp.get("data")
                    or orders_resp.get("result")
                    or None
                )
                if isinstance(orders_list, dict) and orders_list.get("orders"):
                    orders_list = orders_list. get("orders")
            elif (
                hasattr(orders_resp, "data")
                and getattr(orders_resp, "data") is not None
            ):
                data = getattr(orders_resp, "data")
                if isinstance(data, list):
                    orders_list = data
                elif isinstance(data, dict) and data.get("orders"):
                    orders_list = data. get("orders")
            elif isinstance(orders_resp, list):
                orders_list = orders_resp

            if not orders_list:
                return True

            cancel_candidates = [
                "cancel_order",
                "cancel",
                "cancel_order_by_id",
                "delete_order",
            ]
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

                if status and status not in ["PENDING", "OPEN"]:
                    continue

                cancelled = False
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
                            await asyncio.sleep(0.1)
                            break
                        except Exception as e:
                            logger.debug(
                                f"Cancel order {oid} via {cancel_name} failed: {e}"
                            )
                            continue

                if not cancelled:
                    try:
                        if hasattr(signer, "cancel_order"):
                            try:
                                await self.rate_limiter.acquire()
                                await signer. cancel_order(oid)
                                await asyncio.sleep(0.1)
                            except TypeError:
                                await self.rate_limiter.acquire()
                                await signer. cancel_order(order_id=oid)
                    except Exception as e:
                        logger. debug(f"Signer cancel order {oid} failed: {e}")

            return True
        except Exception as e:
            logger. debug(f"Lighter cancel_all_orders error: {e}")
            return False