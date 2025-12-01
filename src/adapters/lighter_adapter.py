# src/adapters/lighter_adapter.py
import asyncio
import aiohttp
import json
import logging
import time
import random
import websockets
from typing import Dict, Tuple, Optional, List, Any
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



# ═══════════════════════════════════════════════════════════════════════════════
# DEBUG: Type-Safety Wrapper mit Logging
# ═══════════════════════════════════════════════════════════════════════════════

def debug_safe_float(val, default=0.0, context="unknown"):
    """
    Convert any value to float safely WITH DEBUG LOGGING. 
    Logs warnings when type conversion is needed.
    """
    if val is None:
        return default
    
    original_type = type(val).__name__
    
    if isinstance(val, float):
        return val
    
    if isinstance(val, int):
        return float(val)
    
    if isinstance(val, str):
        try:
            result = float(val)
            # Nur loggen wenn es wirklich ein String war (das ist unser Bug!)
            if val != str(result):  # Vermeidet Spam bei "0.0" -> 0.0
                logger.debug(f"🔍 TYPE CONVERT [{context}]: '{val}' (str) -> {result} (float)")
            return result
        except ValueError:
            logger.warning(f"⚠️ TYPE ERROR [{context}]: Cannot convert '{val}' to float, using default {default}")
            return default
    
    # Decimal oder andere Typen
    try:
        result = float(val)
        logger.debug(f"🔍 TYPE CONVERT [{context}]: {val} ({original_type}) -> {result} (float)")
        return result
    except (ValueError, TypeError) as e:
        logger.error(f"❌ TYPE ERROR [{context}]: {val} ({original_type}) -> {e}")
        return default


def debug_safe_int(val, default=0, context="unknown"):
    """Convert any value to int safely WITH DEBUG LOGGING."""
    if val is None:
        return default
    
    original_type = type(val).__name__
    
    if isinstance(val, int):
        return val
    
    try:
        result = int(float(str(val)))
        if not isinstance(val, int):
            logger.debug(f"🔍 TYPE CONVERT [{context}]: {val} ({original_type}) -> {result} (int)")
        return result
    except (ValueError, TypeError) as e:
        logger.error(f"❌ TYPE ERROR [{context}]: {val} ({original_type}) -> {e}")
        return default


def debug_compare(val1, op, val2, context="unknown"):
    """
    Safe comparison with debug logging.
    Usage: debug_compare(size, '>', 0, "position_size_check")
    """
    try:
        v1 = debug_safe_float(val1, 0.0, f"{context}_left")
        v2 = debug_safe_float(val2, 0.0, f"{context}_right")
        
        if op == '>':
            return v1 > v2
        elif op == '<':
            return v1 < v2
        elif op == '>=':
            return v1 >= v2
        elif op == '<=':
            return v1 <= v2
        elif op == '==':
            return v1 == v2
        else:
            raise ValueError(f"Unknown operator: {op}")
    except Exception as e:
        logger.error(
            f"❌ COMPARE ERROR [{context}]: {val1} ({type(val1).__name__}) {op} {val2} ({type(val2).__name__}) -> {e}"
        )
        return False


class LighterAdapter(BaseAdapter):
    def __init__(self):
        print(f"DEBUG: LighterAdapter.__init__ called at {time.time()}")
        super().__init__("Lighter")
        self.market_info = {}
        print(f"DEBUG: market_info initialized: {hasattr(self, 'market_info')}")
        self.funding_cache = {}
        self.price_cache = {}
        self.orderbook_cache = {}
        # Added missing internal caches (underscore variants) for compatibility with components
        # expecting these attributes (e.g. WebSocket managers or unified cache interfaces).
        self._price_cache = {}
        self._price_cache_time = {}
        self._orderbook_cache = {}
        self._orderbook_cache_time = {}
        self._trade_cache = {}
        self._funding_cache = {}
        self._funding_cache_time = {}
        
        # NEW: Public Aliases for Latency/Prediction modules
        self.price_cache_time = self._price_cache_time
        self.funding_cache_time = self._funding_cache_time
        
        self.price_update_event = None
        self._ws_message_queue = asyncio.Queue()
        self._signer = None
        self._resolved_account_index = None
        self._resolved_api_key_index = None
        self.semaphore = asyncio.Semaphore(5) # Reduced concurrency
        self.rate_limiter = LIGHTER_RATE_LIMITER
        self._last_market_cache_at = None
        self._balance_cache = 0.0
        self._last_balance_update = 0.0
        # Base URL cached for REST prefetch operations
        self.base_url = self._get_base_url()

    async def _get_session(self):
        """Get or create aiohttp session"""
        if not hasattr(self, '_session') or self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

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
                    logger.debug(f"{self.name} REST GET {path} returned {resp.status}")
                    return None
                data = await resp.json()
                self.rate_limiter.on_success()
                return data
        except Exception as e:
            logger.debug(f"{self.name} REST GET {path} error: {e}")
            return None

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
                            
                            # FIX: Konvertiere kritische Werte sofort zu float
                            if 'min_notional' in data:
                                self.market_info[symbol]['min_notional'] = float(data['min_notional'])
                            if 'min_base_amount' in data:
                                self.market_info[symbol]['min_quantity'] = float(data['min_base_amount'])
                            
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

    async def validate_order_params(self, symbol: str, notional_usd: float) -> Tuple[bool, str]:
        try:
            if symbol not in self.market_info:
                return True, ""

            market_data = self.market_info[symbol]
            
            # PARANOID CASTING: Sicherstellen, dass wir floats haben
            def to_float(val):
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return 0.0

            min_notional = to_float(market_data.get("min_notional", 0))
            max_notional = to_float(market_data.get("max_notional", 0))
            check_val = float(notional_usd)

            # Logik
            if min_notional > 0 and check_val < min_notional:
                # Toleranz für Closes (90%)
                if check_val > (min_notional * 0.9):
                    return True, ""
                return False, f"Notional ${check_val:.2f} < Min ${min_notional:.2f}"

            if max_notional > 0 and check_val > max_notional:
                return False, f"Notional ${check_val:.2f} > Max ${max_notional:.2f}"

            return True, ""

        except Exception as e:
            logger.error(f"Validation error {symbol}: {e}")
            return True, "" # Fail open

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

                    # CRITICAL: Extract real market_id from API response
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

                    if real_market_id > 254:
                        logger.warning(f"⚠️ Skipping {normalized_symbol}: market_id={real_market_id} > 254 (API limit)")
                        return False

                    def safe_int(val, default=8):
                        if val is None:
                            return default
                        try:
                            return int(val)
                        except (ValueError, TypeError):
                            return default

                    def safe_decimal(val, default="0"):
                        if val is None or val == "":
                            return Decimal(default)
                        try:
                            return Decimal(str(val))
                        except Exception:
                            return Decimal(default)

                    def safe_float(val, default=0.0):
                        if val is None:
                            return default
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return default

                    size_decimals = safe_int(getattr(m, 'size_decimals', None), 8)
                    price_decimals = safe_int(getattr(m, 'price_decimals', None), 6)
                    min_base = safe_decimal(getattr(m, 'min_base_amount', None), "0.00000001")
                    min_quote = safe_decimal(getattr(m, 'min_quote_amount', None), "0.00001")

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

    async def load_market_cache(self, force: bool = False):
        """Load all market metadata from Lighter API - FIXED VERSION"""
        if self.market_info and not force:
            return

        # ═══════════════════════════════════════════════════════════════
        # PARANOID CASTING HELPERS - API gibt STRINGS zurück!  
        # ═══════════════════════════════════════════════════════════════
        def safe_float(val, default=0.0):
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def safe_int(val, default=0):
            if val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        try:
            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 1: Lade Basis-Märkte von /orderBooks (für market_id Mapping)
            # ═══════════════════════════════════════════════════════════════
            data = await self._rest_get("/api/v1/orderBooks")
            if not data:
                data = await self._rest_get("/info")
                if not data:
                    logger.error(f"{self.name}: Failed to load markets")
                    return

            markets = data.get("order_books", data.get("markets", data.get("data", [])))
            if isinstance(markets, dict):
                markets = list(markets.values())

            # Temporäres Dict für market_id -> symbol Mapping
            market_id_to_symbol = {}

            for m in markets:
                try:
                    symbol = m.get("ticker", m.get("symbol", ""))
                    if not symbol:
                        symbol = f"MARKET-{m.get('market_index', m.get('i', 'X'))}"

                    symbol = symbol.replace("_", "-").replace("/", "-").upper()
                    if not symbol.endswith("-USD"):
                        symbol = f"{symbol}-USD"

                    if symbol in getattr(config, "BLACKLIST_SYMBOLS", set()):
                        continue

                    # Extract market_id
                    raw_id = (
                        m.get("market_id") or m.get("market_index") or 
                        m.get("marketId") or m.get("i") or m.get("index") or m.get("id")
                    )

                    try:
                        real_id = int(float(str(raw_id))) if raw_id is not None else -1
                    except (ValueError, TypeError):
                        real_id = -1
                    
                    if real_id < 0 or real_id > 254:
                        logger.warning(f"⚠️ Skipping {symbol}: market_id={raw_id} (parsed={real_id}) invalid or >254")
                        continue

                    # Speichere vorläufig mit Defaults (wird unten überschrieben)
                    self.market_info[symbol] = {
                        "i": real_id,
                        "sd": 8,  # Default, wird unten überschrieben
                        "pd": 6,  # Default, wird unten überschrieben
                        "min_notional": safe_float(m.get("min_order_size_usd", m.get("min_notional", 10)), 10.0),
                        "max_notional": safe_float(m.get("max_order_size_usd", m.get("max_notional", 0)), 0.0),
                        "min_base_amount": safe_float(m.get("min_base_amount", 0.01), 0.01),
                        "tick_size": safe_float(m.get("tick_size", 0.01), 0.01),
                        "lot_size": safe_float(m.get("lot_size", 0.0001), 0.0001),
                    }
                    
                    market_id_to_symbol[real_id] = symbol

                except Exception as e:
                    logger.debug(f"{self.name} market parse error: {e}")

            logger.info(f"✅ {self.name}: Loaded {len(self.market_info)} markets (base data)")

            # ═══════════════════════════════════════════════════════════════
            # SCHRITT 2: Lade DETAILS mit size_decimals vom SDK/API
            # ═══════════════════════════════════════════════════════════════
            if HAVE_LIGHTER_SDK:
                try:
                    signer = await self._get_signer()
                    order_api = OrderApi(signer.api_client)
                    
                    await self.rate_limiter.acquire()
                    details_response = await order_api.order_book_details()
                    
                    if details_response and details_response.order_book_details:
                        updated_count = 0
                        
                        for detail in details_response.order_book_details:
                            # Symbol aus Detail extrahieren
                            symbol_raw = getattr(detail, 'symbol', None)
                            if not symbol_raw:
                                continue
                            
                            symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                            
                            if symbol not in self.market_info:
                                continue
                            
                            # ══════════════════════════════════════════════════
                            # KRITISCH: Die echten Dezimalstellen extrahieren! 
                            # ══════════════════════════════════════════════════
                            size_decimals = safe_int(getattr(detail, 'size_decimals', None), 8)
                            price_decimals = safe_int(getattr(detail, 'price_decimals', None), 6)
                            
                            # min_base_amount aus Details (oft genauer als /orderBooks)
                            min_base = getattr(detail, 'min_base_amount', None)
                            if min_base is not None:
                                min_base_float = safe_float(min_base, 0.01)
                            else:
                                min_base_float = self.market_info[symbol].get('min_base_amount', 0.01)
                            
                            # Update market_info mit echten Werten
                            self.market_info[symbol].update({
                                'sd': size_decimals,
                                'pd': price_decimals,
                                'size_decimals': size_decimals,  # Alias für Debug
                                'price_decimals': price_decimals,  # Alias für Debug
                                'min_base_amount': min_base_float,
                                'supported_size_decimals': safe_int(getattr(detail, 'supported_size_decimals', None), 2),
                                'supported_price_decimals': safe_int(getattr(detail, 'supported_price_decimals', None), 2),
                            })
                            
                            updated_count += 1
                            
                            # Preis cachen falls vorhanden
                            mark_price = getattr(detail, 'mark_price', None) or getattr(detail, 'last_trade_price', None)
                            if mark_price:
                                price_float = safe_float(mark_price)
                                if price_float > 0:
                                    self.price_cache[symbol] = price_float
                                    self._price_cache[symbol] = price_float
                                    self._price_cache_time[symbol] = time.time()
                        
                        self.rate_limiter.on_success()
                        logger.info(f"✅ {self.name}: Updated {updated_count} markets with size_decimals from API")
                        
                except Exception as e:
                    logger.warning(f"⚠️ Could not load order_book_details: {e}")
                    logger.warning("   Using default size_decimals=8 (may cause order errors!)")

            # ═══════════════════════════════════════════════════════════════
            # DEBUG: Zeige die geladenen Werte für Test-Symbole
            # ═══════════════════════════════════════════════════════════════
            for symbol in ['ADA-USD', 'SEI-USD', 'RESOLV-USD', 'TIA-USD']:
                if symbol in self.market_info:
                    info = self.market_info[symbol]
                    logger.info(
                        f"📊 MARKET {symbol}: "
                        f"size_decimals={info.get('size_decimals', info.get('sd'))}, "
                        f"price_decimals={info.get('price_decimals', info.get('pd'))}, "
                        f"min_base_amount={info.get('min_base_amount')}"
                    )

            await self._resolve_account_index()

        except Exception as e:
            logger.error(f"{self.name} load_market_cache error: {e}")

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
        """Lädt Funding Rates UND Preise von der Lighter REST API"""
        if not getattr(config, "LIVE_TRADING", False):
            return

        if not HAVE_LIGHTER_SDK:
            return

        try:
            signer = await self._get_signer()
            
            # ═══════════════════════════════════════════════════════════════
            # 1. FUNDING RATES laden (existierender Code)
            # ═══════════════════════════════════════════════════════════════
            funding_api = FundingApi(signer.api_client)
            await self.rate_limiter.acquire()
            fd_response = await funding_api.funding_rates()

            if fd_response and fd_response.funding_rates:
                updated = 0
                for fr in fd_response.funding_rates:
                    market_id = getattr(fr, "market_id", None)
                    rate = getattr(fr, "rate", None)

                    if market_id is None or rate is None:
                        continue

                    rate_float = float(rate) if rate else 0.0

                    for symbol, data in self.market_info.items():
                        if data.get("i") == market_id:
                            self.funding_cache[symbol] = rate_float
                            self._funding_cache[symbol] = rate_float
                            updated += 1
                            break

                self.rate_limiter.on_success()
                logger.debug(f"Lighter: Loaded {updated} funding rates")

            # ═══════════════════════════════════════════════════════════════
            # 2. PREISE laden via order_book_details (NEU!)
            # ═══════════════════════════════════════════════════════════════
            await asyncio.sleep(0.5)  # Rate limit buffer
            
            order_api = OrderApi(signer.api_client)
            await self.rate_limiter.acquire()
            
            try:
                market_list = await order_api.order_book_details()
                if market_list and market_list.order_book_details:
                    price_count = 0
                    now = time.time()
                    
                    for m in market_list.order_book_details:
                        symbol_raw = getattr(m, "symbol", None)
                        if not symbol_raw:
                            continue
                        
                        symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                        
                        # Preis extrahieren
                        mark_price = getattr(m, "mark_price", None) or getattr(m, "last_trade_price", None)
                        if mark_price:
                            price_float = float(mark_price)
                            if price_float > 0:
                                # BEIDE Caches updaten! 
                                self.price_cache[symbol] = price_float
                                self._price_cache[symbol] = price_float
                                self._price_cache_time[symbol] = now
                                price_count += 1
                    
                    self.rate_limiter.on_success()
                    logger.debug(f"Lighter: Loaded {price_count} prices via REST")
                    
            except Exception as e:
                if "429" in str(e):
                    self.rate_limiter.penalize_429()
                else:
                    logger.debug(f"Lighter price fetch warning: {e}")

        except Exception as e:
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            else:
                logger.error(f"Lighter Funding/Price Fetch Error: {e}")

    async def fetch_initial_funding_rates(self):
        """
        Lädt Funding Rates einmalig per REST API, um den Cache sofort zu füllen.
        Verhindert 'Missing rates' Warnungen beim Start.
        """
        try:
            url = f"{self.base_url}/api/v1/info/markets"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        markets = []
                        # Support multiple possible response shapes
                        if isinstance(data, dict):
                            if 'result' in data and isinstance(data['result'], list):
                                markets = data['result']
                            elif 'data' in data and isinstance(data['data'], list):
                                markets = data['data']
                            elif 'markets' in data and isinstance(data['markets'], list):
                                markets = data['markets']
                            else:
                                # Try raw list if dict has numeric keys
                                markets = [v for v in data.values() if isinstance(v, dict) and 'symbol' in v]
                        elif isinstance(data, list):
                            markets = data

                        loaded = 0
                        now_ts = time.time()
                        for m in markets:
                            try:
                                symbol_raw = m.get('symbol') or m.get('market') or m.get('ticker')
                                if not symbol_raw:
                                    continue
                                symbol = symbol_raw if symbol_raw.endswith('-USD') else f"{symbol_raw}-USD"
                                # Try multiple possible rate keys
                                rate_val = (
                                    m.get('hourlyFundingRate') or
                                    m.get('fundingRateHourly') or
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
                                # Store raw hourly rate (downstream logic converts units)
                                if rate_float != 0:
                                    self._funding_cache[symbol] = rate_float
                                    self.funding_cache[symbol] = rate_float  # keep both for consistency
                                    loaded += 1
                            except Exception:
                                continue
                        if loaded > 0:
                            logger.info(f"✅ Lighter: Pre-fetched {loaded} funding rates via REST.")
                        else:
                            logger.warning("Lighter initial funding fetch: no rates parsed.")
                    else:
                        logger.warning(f"Lighter initial funding fetch HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Konnte initiale Funding Rates nicht laden: {e}")

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
        """Berechne Minimum Notional - FIXED VERSION"""
        HARD_MIN_USD = 5.0
        SAFETY_BUFFER = 1.10

        data = self.market_info.get(symbol)
        if not data:
            return HARD_MIN_USD

        try:
            raw_price = self.fetch_mark_price(symbol)
            price = float(raw_price) if raw_price is not None else 0.0
            
            if price <= 0:
                return HARD_MIN_USD

            # ═══════════════════════════════════════════════════════════════
            # FIX: Nutze min_base_amount (echte API-Daten), NICHT min_quantity
            # ═══════════════════════════════════════════════════════════════
            min_base = data.get("min_base_amount", 0)
            try:
                min_base_float = float(min_base)
            except (ValueError, TypeError):
                min_base_float = 0.0
            
            # Berechne USD-Wert der Mindestmenge
            min_qty_usd = min_base_float * price if min_base_float > 0 else 0
            
            # API min_notional (falls vorhanden)
            raw_min_notional = data.get("min_notional", 0)
            try:
                min_notional_api = float(raw_min_notional)
            except (ValueError, TypeError):
                min_notional_api = 0.0
            
            # Nimm den größeren Wert
            api_min = max(min_notional_api, min_qty_usd)
            safe_min = api_min * SAFETY_BUFFER

            result = max(HARD_MIN_USD, safe_min)
            
            # DEBUG: Logge die Berechnung
            logger.debug(
                f"MIN_NOTIONAL {symbol}: min_base={min_base_float} coins, "
                f"price=${price:.4f}, min_qty_usd=${min_qty_usd:.2f}, "
                f"final=${result:.2f}"
            )
            
            return result
            
        except Exception as e:
            logger.debug(f"min_notional_usd error {symbol}: {e}")
            return HARD_MIN_USD

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Funding Rate aus Cache (WS > REST) - mit robuster Typ-Konvertierung"""
        # 1. WebSocket Cache
        if symbol in self._funding_cache:
            rate = self._funding_cache[symbol]
        else:
            # 2. REST Cache
            rate = self.funding_cache.get(symbol)

        if rate is None:
            return None

        # Defensive: Ensure rate is float
        try:
            rate_float = float(rate)
        except (ValueError, TypeError):
            logger.warning(f"Invalid funding rate type for {symbol}: {type(rate)} = {rate}")
            return None

        # EXTREME VALUE CHECK (Annualized APY)
        # Wenn der Wert > 20.0 ist (z.B. 50.0%), ist es annualisiert.
        if abs(rate_float) > 20.0:
            return rate_float / 100.0 / (24 * 365)

        # KORREKTUR: Lighter API liefert 8-Stunden-Rate (Decimal).
        # Die Zahlungen erfolgen aber stündlich (1/8 der Rate).
        # Um auf die korrekte "Hourly Rate" für den Bot zu kommen, müssen wir durch 8 teilen.
        return rate_float / 8.0

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Sichere Version: Gibt immer float oder None zurück"""
        try:
            # 1. WebSocket Cache
            if symbol in self._price_cache:
                price = self._price_cache[symbol]
                if price is not None:
                     return float(price)

            # 2. REST Cache
            if symbol in self.price_cache:
                price = self.price_cache[symbol]
                if price is not None:
                    return float(price)
            
        except (TypeError, ValueError) as e:
            logger.debug(f"fetch_mark_price({symbol}): Konvertierungsfehler: {e}")
        
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

    async def quantize_base_amount(self, symbol: str, size_usd: float) -> int:
        """
        Quantizes USD size to valid base_amount for Lighter API.

        CRITICAL: base_amount must be a multiple of (min_base_amount * 10^size_decimals)
        """
        market_info = self.market_info.get(symbol)
        if not market_info:
            raise ValueError(f"No market info for {symbol}")

        # Get market parameters from cache (keys align with loader: 'sd' and 'min_base_amount'/'ss')
        size_decimals = int(float(str(market_info.get('sd', 4))))
        # Prefer 'min_base_amount' (float), fallback to 'ss' (Decimal)
        mba = market_info.get('min_base_amount', None)
        if mba is None:
            ss = market_info.get('ss', None)
            try:
                mba = float(ss) if ss is not None else 0.01
            except Exception:
                mba = 0.01

        try:
            min_base_amount = float(mba)
        except (ValueError, TypeError):
            min_base_amount = 0.01

        price = self.fetch_mark_price(symbol)

        if not price or price <= 0:
            raise ValueError(f"Invalid price for {symbol}")

        # Calculate base scale (e.g., 10^4 = 10000 for 4 decimals)
        base_scale = 10 ** size_decimals

        # Calculate size in coins
        size_coins = size_usd / price

        # Convert to base units (raw integer)
        raw_base_units = size_coins * base_scale

        # Calculate the lot size (minimum tradeable unit)
        lot_size = int(min_base_amount * base_scale)

        # CRITICAL: Round DOWN to nearest valid lot size!
        quantized_base = int(raw_base_units // lot_size) * lot_size

        # Ensure minimum size is met
        if quantized_base < lot_size:
            quantized_base = lot_size

        logger.debug(
            f"QUANTIZE {symbol}: USD={size_usd:.2f}, coins={size_coins:.6f}, "
            f"raw={raw_base_units:.0f}, lot_size={lot_size}, final={quantized_base}"
        )

        return quantized_base

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
            logger.error(f"🔍 DEBUG: {symbol} market_id = {market_id}, type = {type(market_id)}")
            try:
                mid_int = int(float(str(market_id)))
            except Exception:
                mid_int = -1
            if mid_int > 254:
                logger.error(f"❌ INVALID market_id for {symbol}: {mid_int} > 254!")
                return False, None
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

            # Strict quantization for base amount using USD notional
            base = await self.quantize_base_amount(symbol, float(notional_usd))
            # Preserve price integer scaling from SDK
            _, price_int = self._scale_amounts(symbol, qty, limit_price, side)
            
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

    @rate_limited(Exchange.LIGHTER, "sendTx")
    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        """Close a position on Lighter - BULLETPROOF VERSION with local type safety."""
        # ═══════════════════════════════════════════════════════════════
        # PARANOID CASTING - Alle Inputs sofort konvertieren
        # ═══════════════════════════════════════════════════════════════
        notional_usd = float(notional_usd) if notional_usd is not None else 0.0
        original_side = str(original_side).upper()
        # ═══════════════════════════════════════════════════════════════
        # LOKALE HELPER (garantiert verfügbar in dieser Methode)
        # ═══════════════════════════════════════════════════════════════
        def _safe_float(val, default=0.0):
            if val is None:
                return default
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def _safe_int(val, default=0):
            if val is None:
                return default
            try:
                return int(float(str(val)))
            except (ValueError, TypeError):
                return default
        
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Close {symbol} simuliert.")
            return True, "DRY_RUN_CLOSE_456"

        try:
            positions = await self.fetch_open_positions()
            
            # DEBUG: Log raw position data
            for p in (positions or []):
                if p.get('symbol') == symbol:
                    logger.info(f"🔍 RAW POSITION DATA for {symbol}:")
                    for key, val in p.items():
                        logger.info(f"    {key}: {val} (type: {type(val).__name__})")

            # SAFE FILTER: Konvertiere size BEVOR wir vergleichen
            pos = None
            for p in (positions or []):
                if p.get('symbol') != symbol:
                    continue
                # FORCE CAST everything from API response
                size_raw = p.get('size', 0)
                sign_raw = p.get('sign', 0)
                mark_price_raw = p.get('mark_price', 0)
                # Convert to proper types
                size = float(size_raw) if size_raw is not None else 0.0
                sign = int(float(str(sign_raw))) if sign_raw is not None else 0
                mark_price = float(mark_price_raw) if mark_price_raw is not None else 0.0
                size_val = _safe_float(size, 0.0)
                if abs(size_val) > 1e-8:
                    pos = p
                    break

            if not pos:
                logger.info(f"Lighter Rollback {symbol}: No position found (already closed?)")
                return True, None

            # ═══════════════════════════════════════════════════════════════
            # ALLE WERTE SICHER KONVERTIEREN
            # ═══════════════════════════════════════════════════════════════
            
            # Position Size - API gibt oft strings zurück! 
            position_size_raw = pos.get('position') or pos.get('size') or 0
            position_size = _safe_float(position_size_raw, 0.0)
            
            # Sign (0 = long, 1 = short) - API gibt manchmal string "0" oder "1"
            sign_raw = pos.get('sign', 0)
            sign = _safe_int(sign_raw, 0)
            
            # Actual size mit Vorzeichen
            if sign == 0:
                actual_size = position_size
            else:
                actual_size = -position_size
            
            close_size_coins = abs(actual_size)
            
            # SICHERE VERGLEICHE (beide Seiten sind jetzt garantiert float/int)
            if close_size_coins <= 1e-8 or position_size <= 0:
                logger.info(f"Lighter {symbol}: Position size too small ({position_size}), treating as closed")
                return True, None

            # ──────────────────────────────────────────────────────────────
            # Preise sicher konvertieren
            # ──────────────────────────────────────────────────────────────
            
            # Mark Price aus Position oder Cache
            mark_price_raw = pos.get('mark_price')
            mark_price = _safe_float(mark_price_raw, 0.0)
            
            # Entry Price als Fallback
            entry_price_raw = pos.get('avg_entry_price') or pos.get('entry_price')
            entry_price = _safe_float(entry_price_raw, 0.0)

            # Preis-Validierung
            if mark_price <= 0:
                logger.warning(f"Lighter {symbol}: Mark price ungültig ({mark_price_raw}), verwende Entry Price")
                mark_price = entry_price
                
            if mark_price <= 0:
                # Fallback auf Cache
                cache_price = self.price_cache.get(symbol) or self._price_cache.get(symbol)
                mark_price = _safe_float(cache_price, 0.0)
                    
            if mark_price <= 0:
                # Letzter Fallback: fetch_mark_price
                fetched_price = self.fetch_mark_price(symbol)
                mark_price = _safe_float(fetched_price, 0.0)
                
            if mark_price <= 0:
                logger.error(f"Lighter close {symbol}: Kein Preis verfügbar!")
                return False, None
                
            logger.info(f"Lighter close {symbol}: Using price ${mark_price:.6f}")

            # Notional berechnen
            close_notional_usd = close_size_coins * mark_price

            logger.info(
                f"Lighter Close {symbol}: size={actual_size:+.6f} coins @ ${mark_price:.6f} = ${close_notional_usd:.2f} (sign={sign})"
            )

            # Close Order - gegenseitige Seite
            close_side = "BUY" if actual_size < 0 else "SELL"
            
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
            logger.error(f"❌ TYPE ERROR in close_live_position for {symbol}: {e}")
            logger.error(f"   This usually means a string vs float comparison failed!")
            import traceback
            logger.error(traceback.format_exc())
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