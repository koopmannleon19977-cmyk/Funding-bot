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
from functools import wraps

def rate_limit_retry(max_retries=3, base_delay=5.0):
    """Decorator für automatisches Retry bei 429 Errors"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if "429" in str(e) or "Too Many Requests" in str(e):
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Rate limit hit, waiting {delay}s (attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(delay)
                    else:
                        raise
            raise Exception(f"Max retries ({max_retries}) exceeded for rate limit")
        return wrapper
    return decorator
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
        
        # KRITISCH: Cache-Attribute MÜSSEN existieren
        self.funding_cache: Dict[str, float] = {}      # Öffentlich - für externe Zugriffe
        self.price_cache: Dict[str, float] = {}        # Öffentlich - für Price-Abrufe
        self.market_info: Dict[str, Any] = {}          # Market-Metadaten
        
        print(f"DEBUG: market_info initialized: {hasattr(self, 'market_info')}")
        
        # Weitere Caches
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def validate_order_params(self, symbol: str, size_usd: float) -> Tuple[bool, str]:
        """
        Validate order parameters before sending to API.
        Returns (is_valid, error_message). 
        """
        market_info = self.market_info.get(symbol, {})
        
        if not market_info:
            return False, f"Market {symbol} not found"
        
        price = self.get_price(symbol)
        # Ensure price is float for comparisons
        try:
            price = float(price) if price is not None else 0.0
        except (ValueError, TypeError):
            price = 0.0
        
        if not price or price <= 0:
            return False, f"Invalid price for {symbol}: {price}"
        
        # Calculate quantity
        quantity = size_usd / price
        
        # Check minimums - ensure values are float
        try:
            min_base = float(market_info.get('min_base_amount', 0.0001))
        except (ValueError, TypeError):
            min_base = 0.0001
        
        try:
            min_notional = float(market_info.get('min_notional', 5.0))
        except (ValueError, TypeError):
            min_notional = 5.0
        
        if quantity < min_base:
            return False, f"Quantity {quantity:.6f} < min_base {min_base}"
        
        notional = quantity * price
        if notional < min_notional:
            return False, f"Notional ${notional:.2f} < min_notional ${min_notional}"
        
        return True, "OK"

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def load_markets(self):
        """Alias for load_market_cache - called by main()"""
        await self.load_market_cache(force=True)

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def start_websocket(self):
        """WebSocket entry point für WebSocketManager"""
        logger.info(f"🌐 {self.name}: WebSocket Manager starting streams...")
        await asyncio.gather(
            self._poll_prices(),
            self._poll_funding_rates(),
            return_exceptions=True,
        )

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def _poll_prices(self):
        """Wrapper kompatibel mit WebSocketManager"""
        await self._rest_price_poller(interval=2.0)

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def _poll_funding_rates(self):
        """Wrapper kompatibel mit WebSocketManager"""
        await self._rest_funding_poller(interval=30.0)

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def ws_message_stream(self):
        """Async iterator für WebSocketManager"""
        while True:
            msg = await self._ws_message_queue. get()
            yield msg

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def _rest_price_poller(self, interval: float = 5.0):
        """REST-basiertes Preis-Polling - uses /orderBookDetails endpoint (HAS last_trade_price)"""
        while True:
            try:
                await self.rate_limiter.acquire()
                
                # ✅ CRITICAL FIX: Use orderBookDetails endpoint (has last_trade_price field)
                # orderBooks endpoint does NOT have last_trade_price!
                async with aiohttp.ClientSession() as session:
                    url = f"{self._get_base_url()}/api/v1/orderBookDetails"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 429:
                            self.rate_limiter.penalize_429()
                            await asyncio.sleep(30)
                            continue
                        
                        if resp.status != 200:
                            logger.debug(f"Lighter price fetch HTTP {resp.status}")
                            await asyncio.sleep(interval)
                            continue
                        
                        data = await resp.json()
                        self.rate_limiter.on_success()
                
                # ✅ Parse response - orderBookDetails returns {"order_book_details": [...]}
                markets = data.get("order_book_details") or data.get("orderBookDetails") or []
                updated = 0
                
                for m in markets:
                    symbol_raw = m.get("symbol", "")
                    if not symbol_raw:
                        continue
                    
                    symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                    
                    # ✅ Extract price from last_trade_price field (confirmed in SDK OrderBookDetail model)
                    price = m.get("last_trade_price")
                    if price is not None:
                        try:
                            price_float = float(price)
                            if price_float > 0:
                                self.price_cache[symbol] = (price_float, time.time())
                                updated += 1
                                logger.debug(f"{symbol}: Cached price ${price_float:.4f}")
                        except (ValueError, TypeError) as e:
                            logger.debug(f"{symbol}: Failed to parse price {price}: {e}")
                            continue
                
                if updated > 0:
                    logger.info(f"✅ Lighter REST: Loaded {updated} prices")
                    if self.price_update_event:
                        self.price_update_event.set()
                else:
                    logger.warning(f"⚠️ Lighter REST: Loaded 0 prices from {len(markets)} markets")
                
                await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Lighter REST price poller error: {e}")
                await asyncio.sleep(interval * 2)

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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
                    # CRITICAL FIX: Use supported_* fields for ACTUAL scaling
                    # NOT size_decimals/price_decimals (those are display only)
                    # 
                    # X10 Comparison:
                    # - X10 SDK handles scaling internally
                    # - Lighter requires manual scaling with EXACT decimals
                    # 
                    # API Fields:
                    # - size_decimals: Display decimals (e.g., 8 for BTC, APT)
                    # - supported_size_decimals: ACTUAL scaling decimals (4 for APT!)
                    # - price_decimals: Display decimals (e.g., 6)
                    # - supported_price_decimals: ACTUAL scaling decimals (2 for APT!)
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

                    # ✅ CRITICAL FIX: Use supported_* fields (actual scaling decimals)
                    # These tell us how the API actually scales integers
                    size_decimals_actual = safe_int(getattr(m, 'supported_size_decimals', None), 4)
                    price_decimals_actual = safe_int(getattr(m, 'supported_price_decimals', None), 2)
                    
                    # Display decimals (for logging/UI only, not for scaling)
                    size_decimals_display = safe_int(getattr(m, 'size_decimals', None), 8)
                    price_decimals_display = safe_int(getattr(m, 'price_decimals', None), 6)
                    
                    # Min amounts (already in human-readable format, NOT scaled)
                    min_base = safe_decimal(getattr(m, 'min_base_amount', None), "0.00000001")
                    min_quote = safe_decimal(getattr(m, 'min_quote_amount', None), "0.00001")
                    
                    market_data = {
                        'i': getattr(m, 'market_id', market_id),
                        'symbol': normalized_symbol,
                        
                        # ✅ ACTUAL decimals for scaling (used in _scale_amounts)
                        'sd': size_decimals_actual,    # e.g., 4 for APT (not 8!)
                        'pd': price_decimals_actual,   # e.g., 2 for APT (not 6!)
                        
                        # Display decimals (for reference only)
                        'display_sd': size_decimals_display,
                        'display_pd': price_decimals_display,
                        
                        # Min amounts (human-readable, not scaled)
                        'ss': min_base,                # e.g., Decimal("0.001")
                        'mps': min_quote,              # e.g., Decimal("10")
                        'min_notional': float(min_quote) if min_quote else 10.0,
                        'min_quantity': float(min_base) if min_base else 0.0,
                    }

                    if normalized_symbol in MARKET_OVERRIDES:
                        market_data.update(MARKET_OVERRIDES[normalized_symbol])

                    self.market_info[normalized_symbol] = market_data

                    # last_trade_price ist double laut API
                    price = getattr(m, 'last_trade_price', None)
                    if price is not None:
                        self.price_cache[normalized_symbol] = (safe_float(price), time.time())
                    
                    # ✅ ZUSÄTZLICH: Cache funding rate aus market details (Fallback)
                    funding_rate = getattr(m, 'current_funding_rate', None)
                    if funding_rate is not None:
                        try:
                            rate_float = float(funding_rate)
                            self.funding_cache[normalized_symbol] = rate_float
                            logger.debug(f"{normalized_symbol}: Cached funding rate {rate_float:.6f}")
                        except (ValueError, TypeError):
                            pass
                    
                    # Debug log to verify correct decimals
                    logger.debug(
                        f"✅ {normalized_symbol}: "
                        f"size_decimals={size_decimals_actual} (display: {size_decimals_display}), "
                        f"price_decimals={price_decimals_actual} (display: {price_decimals_display}), "
                        f"min_base={min_base}"
                    )
                        
                    self.rate_limiter.on_success()
                    return True
                    
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate limit" in error_str:
                    self.rate_limiter.penalize_429()
                return False
            return False

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def _load_funding_rates(self) -> bool:
        """Load funding rates from Lighter REST API"""
        try:
            await self.rate_limiter.acquire()
            
            async with aiohttp.ClientSession() as session:
                # ✅ KRITISCH: Bindestrich, nicht camelCase! (funding-rates)
                url = f"{self._get_base_url()}/api/v1/funding-rates"
                async with session.get(
                    url, 
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 429:
                        self.rate_limiter.penalize_429()
                        logger.warning("⚠️ Lighter funding rates rate limited")
                        return False
                    
                    if resp.status != 200:
                        logger.debug(f"Lighter funding rates HTTP {resp.status}")
                        return False
                    
                    data = await resp.json()
                    self.rate_limiter.on_success()

            funding_rates = (
                data.get('funding_rates') or 
                data.get('fundingRates') or
                data.get('data') or 
                []
            )
            
            loaded = 0
            
            for fr in funding_rates:
                exchange = fr.get('exchange', 'lighter')
                if exchange and exchange.lower() != 'lighter':
                    continue
                    
                symbol = fr.get('symbol', '')
                rate = fr.get('rate')
                
                if symbol and rate is not None:
                    if not symbol.endswith('-USD'):
                        symbol = f"{symbol}-USD"
                    self.funding_cache[symbol] = float(rate)
                    loaded += 1
            
            logger.debug(f"Lighter: Loaded {loaded} funding rates")
            return True
            
        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.penalize_429()
            logger.debug(f"Lighter funding rates error: {e}")
            return False

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def load_market_cache(self, force: bool = False, max_retries: int = 1):
        """Load market data from Lighter API - RATE LIMIT SAFE VERSION"""
        print(f"DEBUG: load_market_cache called, has market_info: {hasattr(self, 'market_info')}")

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
            # ═══════════════════════════════════════════════════════════════
            # REST API direkt mit camelCase endpoint
            # ═══════════════════════════════════════════════════════════════
            base_url = self._get_base_url()
            
            async with aiohttp.ClientSession() as session:
                await self.rate_limiter.acquire()
                
                # ✅ CRITICAL FIX: Use /orderBookDetails (has last_trade_price)
                # NOT /orderBooks (missing last_trade_price field!)
                url = f"{base_url}/api/v1/orderBookDetails"
                logger.debug(f"Fetching markets from: {url}")
                
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 429:
                        self.rate_limiter.penalize_429()
                        logger.warning("⚠️ Lighter markets API rate limited, waiting...")
                        await asyncio.sleep(30)
                        return
                    
                    if resp.status != 200:
                        logger.error(f"Lighter markets API error: {resp.status}")
                        text = await resp.text()
                        logger.debug(f"Response: {text[:200]}")
                        return
                    
                    data = await resp.json()
                    self.rate_limiter.on_success()

            # ✅ Parse response - orderBookDetails returns {"order_book_details": [...]}
            markets_data = (
                data.get("order_book_details") or 
                data.get("orderBookDetails") or
                []
            )
            
            if not markets_data:
                logger.warning("⚠️ Lighter: No markets in API response")
                return

            loaded_count = 0
            
            for m in markets_data:
                try:
                    # Handle both dict and object responses
                    if isinstance(m, dict):
                        symbol_raw = m.get("symbol", "")
                        market_id = m.get("market_id", m.get("marketId", m.get("i", 0)))
                        
                        # ✅ CRITICAL FIX: Use supported_* fields (actual scaling decimals)
                        size_decimals_actual = int(m.get("supported_size_decimals", m.get("supportedSizeDecimals", 4)))
                        price_decimals_actual = int(m.get("supported_price_decimals", m.get("supportedPriceDecimals", 2)))
                        
                        # Display decimals (for reference only)
                        size_decimals_display = int(m.get("size_decimals", m.get("sizeDecimals", 8)))
                        price_decimals_display = int(m.get("price_decimals", m.get("priceDecimals", 6)))
                        
                        min_base = m.get("min_base_amount", m.get("minBaseAmount", "0.00000001"))
                        min_quote = m.get("min_quote_amount", m.get("minQuoteAmount", "0.00001"))
                        last_price = m.get("last_trade_price", m.get("lastTradePrice", 0))
                    else:
                        symbol_raw = getattr(m, "symbol", "")
                        market_id = getattr(m, "market_id", getattr(m, "marketId", 0))
                        
                        # ✅ CRITICAL FIX: Use supported_* fields (actual scaling decimals)
                        size_decimals_actual = int(getattr(m, "supported_size_decimals", getattr(m, "supportedSizeDecimals", 4)))
                        price_decimals_actual = int(getattr(m, "supported_price_decimals", getattr(m, "supportedPriceDecimals", 2)))
                        
                        # Display decimals (for reference only)
                        size_decimals_display = int(getattr(m, "size_decimals", getattr(m, "sizeDecimals", 8)))
                        price_decimals_display = int(getattr(m, "price_decimals", getattr(m, "priceDecimals", 6)))
                        
                        min_base = getattr(m, "min_base_amount", getattr(m, "minBaseAmount", "0.00000001"))
                        min_quote = getattr(m, "min_quote_amount", getattr(m, "minQuoteAmount", "0.00001"))
                        last_price = getattr(m, "last_trade_price", getattr(m, "lastTradePrice", 0))
                    
                    if not symbol_raw:
                        continue
                    
                    # Normalize symbol
                    symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                    
                    # Build market info with CORRECT decimals
                    market_data = {
                        'i': market_id,
                        'symbol': symbol,
                        
                        # ✅ ACTUAL decimals for scaling (used in _scale_amounts)
                        'sd': size_decimals_actual,
                        'pd': price_decimals_actual,
                        
                        # Display decimals (for reference only)
                        'display_sd': size_decimals_display,
                        'display_pd': price_decimals_display,
                        
                        # Min amounts (human-readable)
                        'ss': Decimal(str(min_base)) if min_base else Decimal("0.00000001"),
                        'mps': Decimal(str(min_quote)) if min_quote else Decimal("0.00001"),
                        'min_notional': float(min_quote) if min_quote else 10.0,
                        'min_quantity': float(min_base) if min_base else 0.0,
                    }
                    
                    # Apply overrides
                    if symbol in MARKET_OVERRIDES:
                        market_data.update(MARKET_OVERRIDES[symbol])
                    
                    self.market_info[symbol] = market_data
                    
                    # Cache price from last_trade_price field (SDK confirmed)
                    if last_price is not None:
                        try:
                            price_float = float(last_price)
                            if price_float > 0:
                                self.price_cache[symbol] = (price_float, time.time())
                                logger.debug(f"{symbol}: Cached price ${price_float:.2f}")
                        except (ValueError, TypeError):
                            pass
                    
                    # ✅ ZUSÄTZLICH: Cache funding rate wenn verfügbar
                    try:
                        # API kann funding_rate oder current_funding_rate enthalten
                        funding = None
                        if isinstance(m, dict):
                            funding = m.get('current_funding_rate') or m.get('funding_rate')
                        else:
                            funding = getattr(m, 'current_funding_rate', None) or getattr(m, 'funding_rate', None)
                        
                        if funding is not None:
                            rate_float = float(funding)
                            self.funding_cache[symbol] = rate_float
                            logger.debug(f"{symbol}: Cached funding rate {rate_float:.6f}")
                    except (ValueError, TypeError, AttributeError):
                        pass
                    
                    loaded_count += 1
                    
                except Exception as e:
                    logger.debug(f"Error parsing market {m}: {e}")
                    continue

            self._last_market_cache_at = time.time()
            logger.info(f"✅ Lighter: {loaded_count} markets loaded (REST API)")

            # Load Funding Rates (separate, rate-limited)
            await asyncio.sleep(1.0)
            await self._load_funding_rates()

        except aiohttp.ClientError as e:
            logger.error(f"❌ Lighter Market Cache Network Error: {e}")
        except Exception as e:
            logger.error(f"❌ Lighter Market Cache Error: {e}")
            import traceback
            traceback.print_exc()
    
    async def load_prices_from_rest(self) -> int:
        """Load prices from REST API into price_cache"""
        try:
            # Orderbook endpoint for prices
            url = f"{self._get_base_url()}/api/v1/orderBooks"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning(f"Lighter REST: HTTP {resp.status}")
                        return 0
                    
                    data = await resp.json()
            
            count = 0
            
            # Response is a dict with market_id as key
            if isinstance(data, dict):
                for market_id_str, market_data in data.items():
                    try:
                        market_id = int(market_id_str)
                        
                        # Symbol from mapping
                        symbol = self._market_id_to_symbol.get(market_id)
                        if not symbol:
                            continue
                        
                        bids = market_data.get("bids", [])
                        asks = market_data.get("asks", [])
                        
                        if bids and asks:
                            try:
                                best_bid = float(bids[0]["price"]) if bids else 0
                                best_ask = float(asks[0]["price"]) if asks else 0
                                mid_price = (best_bid + best_ask) / 2
                                
                                if mid_price > 0:
                                    self.price_cache[symbol] = (mid_price, time.time())
                                    count += 1
                            except (ValueError, TypeError, KeyError, IndexError):
                                continue
                        
                    except (ValueError, KeyError, IndexError):
                        continue
            
            logger.info(f"✅ Lighter REST: Loaded {count} prices")
            return count
            
        except Exception as e:
            logger.error(f"Lighter REST prices error: {e}")
            return 0

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def load_funding_rates_and_prices(self):
        """Load Funding Rates from Lighter REST API (prices handled by _rest_price_poller)"""
        if not getattr(config, "LIVE_TRADING", False):
            return

        # Only load funding rates - prices are loaded by _rest_price_poller
        await self._load_funding_rates()

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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
                    try:
                        price = float(getattr(b, "price", 0))
                        size = float(getattr(b, "remaining_base_amount", 0))
                        if price > 0 and size > 0:
                            bids.append([price, size])
                    except (ValueError, TypeError):
                        continue

                asks = []
                for a in response.asks or []:
                    try:
                        price = float(getattr(a, "price", 0))
                        size = float(getattr(a, "remaining_base_amount", 0))
                        if price > 0 and size > 0:
                            asks.append([price, size])
                    except (ValueError, TypeError):
                        continue

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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

            if response and hasattr(response, 'order_book_details') and response.order_book_details:
                # API returns array, access first element
                details = response.order_book_details[0]

                # Get mark price for conversion (open_interest is in base tokens, not USD)
                mark_price = 0.0
                for price_field in ['mark_price', 'last_trade_price', 'last_price']:
                    if hasattr(details, price_field):
                        price_raw = getattr(details, price_field, 0)
                        try:
                            mark_price = float(price_raw) if price_raw else 0.0
                        except (ValueError, TypeError):
                            mark_price = 0.0
                        if mark_price > 0:
                            break
                
                # Fallback to cached price if not in response
                if mark_price <= 0:
                    cached_price = self.fetch_mark_price(symbol)
                    try:
                        mark_price = float(cached_price) if cached_price is not None else 0.0
                    except (ValueError, TypeError):
                        mark_price = 0.0

                # Try open_interest first (in base tokens - must convert to USD)
                if hasattr(details, "open_interest") and mark_price > 0:
                    oi_base = getattr(details, "open_interest", 0)
                    try:
                        oi_base_float = float(oi_base) if oi_base else 0.0
                    except (ValueError, TypeError):
                        oi_base_float = 0.0
                    
                    if oi_base_float > 0:
                        # Convert base tokens to USD
                        oi_usd = oi_base_float * mark_price
                        self._oi_cache[symbol] = oi_usd
                        self._oi_cache_time[symbol] = now
                        self.rate_limiter.on_success()
                        logger.debug(f"Lighter OI {symbol}: {oi_base_float:.4f} base * ${mark_price:.2f} = ${oi_usd:,.0f}")
                        return oi_usd

                # Fallback to daily_quote_token_volume (already in USD)
                if hasattr(details, "daily_quote_token_volume"):
                    vol_raw = getattr(details, "daily_quote_token_volume", 0)
                    try:
                        vol = float(vol_raw) if vol_raw else 0.0
                    except (ValueError, TypeError):
                        vol = 0.0
                    
                    if vol > 0:
                        self._oi_cache[symbol] = vol
                        self._oi_cache_time[symbol] = now
                        self.rate_limiter.on_success()
                        logger.debug(f"Lighter OI {symbol}: ${vol:,.0f} (from volume)")
                        return vol

                # Last fallback to volume_24h (already in USD)
                if hasattr(details, "volume_24h"):
                    vol_raw = getattr(details, "volume_24h", 0)
                    try:
                        vol = float(vol_raw) if vol_raw else 0.0
                    except (ValueError, TypeError):
                        vol = 0.0
                    
                    if vol > 0:
                        self._oi_cache[symbol] = vol
                        self._oi_cache_time[symbol] = now
                        self.rate_limiter.on_success()
                        return vol
            return 0.0
        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.penalize_429()
                logger.debug(f"Lighter OI {symbol}: Rate limited")
            else:
                logger.debug(f"Lighter OI {symbol}: {e}")
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

        try:
            rate_float = float(rate)
        except (ValueError, TypeError):
            return None

        if abs(rate_float) > 0.1:
            rate_float = rate_float / 8.0

        return rate_float

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        """Mark Price aus Cache (with timestamp)"""
        if symbol not in self.market_info:
            return None

        if symbol in self.price_cache:
            cache_entry = self.price_cache[symbol]
            if cache_entry is not None:
                try:
                    # Handle both old format (float) and new format (tuple)
                    if isinstance(cache_entry, tuple):
                        price_float, timestamp = cache_entry
                    else:
                        price_float = float(cache_entry)
                    
                    if price_float > 0:
                        return price_float
                except (ValueError, TypeError):
                    pass

        return None

    async def get_price(self, symbol: str) -> Optional[float]:
        """Get price with staleness check and REST fallback - NEVER returns None during normal operation."""
        if symbol not in self.market_info:
            logger.warning(f"⚠️ {symbol} not found in market_info")
            return None
        
        # Check cache first
        if symbol in self.price_cache:
            cache_entry = self.price_cache[symbol]
            try:
                # Handle both old format (float) and new format (tuple)
                if isinstance(cache_entry, tuple):
                    price, timestamp = cache_entry
                    # Check if cache is fresh (< 5 seconds old)
                    if time.time() - timestamp < 5.0:
                        if price > 0:
                            return price
                else:
                    # Old format - treat as stale
                    price = float(cache_entry)
                    if price > 0:
                        logger.debug(f"{symbol}: Using old cache format price")
                        return price
            except (ValueError, TypeError, IndexError) as e:
                logger.warning(f"⚠️ {symbol}: Cache parse error: {e}")
        
        # Cache is stale or empty - fetch from REST API immediately
        logger.info(f"🔄 {symbol}: Cache stale/empty, fetching from REST API")
        
        if not HAVE_LIGHTER_SDK:
            logger.error(f"⚠️ Lighter SDK not available for {symbol} REST fallback")
            return None
        
        try:
            await self.rate_limiter.acquire()
            
            # Get market_id for this symbol
            market_id = self.market_info[symbol].get("i")
            if market_id is None:
                logger.error(f"⚠️ {symbol}: No market_id found")
                return None
            
            # Use SDK's order_book_orders method to fetch live orderbook
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)
            
            response = await order_api.order_book_orders(market_id=market_id, limit=1)
            
            if response and response.bids and response.asks:
                # Calculate mid price from best bid/ask
                best_bid = float(response.bids[0].price)
                best_ask = float(response.asks[0].price)
                mid_price = (best_bid + best_ask) / 2.0
                
                if mid_price > 0:
                    # Update cache with fresh price
                    self.price_cache[symbol] = (mid_price, time.time())
                    logger.info(f"✅ {symbol}: Fetched fresh price from REST: ${mid_price:.4f}")
                    self.rate_limiter.on_success()
                    return mid_price
            
            logger.error(f"⚠️ {symbol}: No bids/asks in REST response")
            return None
            
        except Exception as e:
            logger.error(f"❌ {symbol}: REST fallback failed: {e}")
            if "429" in str(e):
                self.rate_limiter.penalize_429()
            return None

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
    async def fetch_open_positions(self) -> List[dict]:
        """
        Fetch open positions from Lighter.
        
        Returns:
            List of dicts: [{"symbol": "BTC-USD", "size": 0.5}, ...]
            Empty list if no positions or error.
        """
        if not getattr(config, "LIVE_TRADING", False):
            logger.debug("Lighter: LIVE_TRADING=False, skipping positions")
            return []

        if not HAVE_LIGHTER_SDK:
            logger.warning("Lighter: SDK not available")
            return []

        try:
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)

            await self.rate_limiter.acquire()
            await asyncio.sleep(0.2)

            response = await account_api.account(
                by="index", value=str(self._resolved_account_index)
            )

            # Handle empty response (normal when no positions)
            if not response or not response.accounts or not response.accounts[0]:
                logger.debug("Lighter: No positions (empty account response)")
                return []

            account = response.accounts[0]

            if not hasattr(account, "positions") or not account.positions:
                logger.debug("Lighter: No positions (empty positions array)")
                return []

            positions = []
            for p in account.positions:
                try:
                    symbol_raw = getattr(p, "symbol", None)
                    position_qty = getattr(p, "position", None)
                    sign = getattr(p, "sign", None)

                    if not symbol_raw or position_qty is None or sign is None:
                        continue

                    # Calculate signed size
                    try:
                        multiplier = 1 if int(sign) == 0 else -1
                        size = float(position_qty) * multiplier
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Lighter: Invalid position data for {symbol_raw}: "
                            f"qty={position_qty}, sign={sign}"
                        )
                        continue

                    # Only include non-zero positions
                    if abs(size) > 1e-8:
                        symbol = (
                            f"{symbol_raw}-USD"
                            if not symbol_raw.endswith("-USD")
                            else symbol_raw
                        )
                        positions.append({"symbol": symbol, "size": size})
                        logger.debug(f"Lighter: Position {symbol} size={size:.6f}")
                except Exception as e:
                    logger.debug(f"Lighter: Error parsing position: {e}")
                    continue

            if positions:
                logger.info(f"Lighter: Found {len(positions)} open positions")
            else:
                logger.debug("Lighter: No positions (filtered result)")
            
            return positions

        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.penalize_429()
                logger.warning("Lighter Positions: Rate limited")
            else:
                logger.error(f"Lighter Positions Error: {e}")
            return []

    def usd_to_base_amount(
        self, 
        symbol: str, 
        usd_amount: float, 
        current_price: Optional[float] = None
    ) -> int:
        """
        Convert USD order size to correctly scaled baseAmount for Lighter API.
        
        This function:
        1. Fetches market precision requirements (size_decimals, price_decimals)
        2. Calculates quantity in base tokens (e.g., ETH, APT)
        3. Scales to integer units using the market's baseScale
        4. Validates against minimum order size
        
        Example:
            # Order $14.36 worth of APT at $10.20 per APT
            base_amount = adapter.usd_to_base_amount("APT-USD", 14.36, 10.20)
            # Returns: 14080 (scaled units representing 1.408 APT)
            
        Args:
            symbol: Market symbol (e.g., "APT-USD", "ETH-USD")
            usd_amount: USD value to spend (e.g., 14.36)
            current_price: Current price per token in USD (optional, fetches if None)
            
        Returns:
            baseAmount as integer (scaled for Lighter API)
            
        Raises:
            ValueError: If market not found, price unavailable, or size below minimum
        """
        # 1. Get market configuration
        market_data = self.market_info.get(symbol)
        if not market_data:
            raise ValueError(f"❌ Market {symbol} not found. Call load_market_cache() first.")
        
        # 2. Get current price (fetch if not provided)
        if current_price is None:
            current_price = self.get_price(symbol)
        
        # Ensure price is float for comparisons
        try:
            current_price = float(current_price) if current_price is not None else 0.0
        except (ValueError, TypeError):
            current_price = 0.0
        
        if not current_price or current_price <= 0:
            raise ValueError(f"❌ No valid price for {symbol}")
        
        # 3. Calculate quantity in base tokens
        # Formula: quantity = usd_amount / price
        # Example: $14.36 / $10.20 = 1.408 APT
        quantity = Decimal(str(usd_amount)) / Decimal(str(current_price))
        
        # 4. Get scaling parameters from API
        def safe_int(val, default=8) -> int:
            if val is None:
                return default
            try:
                return int(float(str(val)))
            except (ValueError, TypeError):
                return default
        
        def safe_decimal(val, default="0") -> Decimal:
            if val is None:
                return Decimal(default)
            try:
                return Decimal(str(val))
            except (ValueError, TypeError):
                return Decimal(default)
        
        # ✅ CRITICAL: Use supported_size_decimals (actual scaling)
        # NOT size_decimals (display only)
        size_decimals = safe_int(market_data.get('sd'), 8)
        min_base_amount = safe_decimal(market_data.get('ss'), "0.00000001")
        
        # 5. Calculate baseScale (SDK: Math.pow(10, size_decimals))
        base_scale = Decimal(10) ** size_decimals
        
        # 6. Ensure minimum order size
        if min_base_amount > Decimal("0") and quantity < min_base_amount:
            # Increase to minimum with 5% buffer
            quantity = min_base_amount * Decimal("1.05")
            logger.warning(
                f"⚠️ {symbol}: USD amount ${usd_amount:.2f} too small. "
                f"Increased to min {min_base_amount:.8f} tokens (${quantity * Decimal(str(current_price)):.2f})"
            )
        
        # 7. Scale to integer units (SDK: Math.round(amount * baseScale))
        # Use ROUND_UP to ensure we meet minimum requirements
        scaled_base = int((quantity * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
        
        # 8. Validation
        if scaled_base == 0:
            raise ValueError(
                f"❌ {symbol}: Scaled baseAmount is 0!\n"
                f"   USD amount: ${usd_amount:.2f}\n"
                f"   Price: ${current_price:.6f}\n"
                f"   Quantity: {quantity:.8f}\n"
                f"   size_decimals: {size_decimals}\n"
                f"   base_scale: {base_scale}\n"
                f"   min_base_amount: {min_base_amount}"
            )
        
        # 9. Calculate actual USD value (for logging)
        actual_quantity = Decimal(scaled_base) / base_scale
        actual_usd = float(actual_quantity * Decimal(str(current_price)))
        
        logger.info(
            f"💱 {symbol} USD → baseAmount:\n"
            f"   Input: ${usd_amount:.2f} @ ${current_price:.6f}\n"
            f"   Calculated: {float(quantity):.8f} tokens\n"
            f"   Scaled: {scaled_base} units (size_decimals={size_decimals})\n"
            f"   Actual: {float(actual_quantity):.8f} tokens = ${actual_usd:.2f}"
        )
        
        return scaled_base

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        """
        Scale amounts to Lighter API units - EXACT SDK implementation.
        
        From lighter-ts SDK:
        - baseScale = Math.pow(10, size_decimals)
        - quoteScale = Math.pow(10, price_decimals)
        - baseAmount = Math.round(amount * baseScale)
        - priceAmount = Math.round(price * quoteScale)
        
        Args:
            symbol: Market symbol (e.g., "APT-USD")
            qty: Quantity in human-readable format (e.g., 0.01 ETH)
            price: Price in human-readable format (e.g., 3000.50 USD)
            side: "BUY" or "SELL"
            
        Returns:
            (base_amount_scaled, price_scaled) as integers
        """
        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f"❌ Market data missing for {symbol}")
        
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Get size_decimals and price_decimals from API response
        # ═══════════════════════════════════════════════════════════════
        
        def safe_int(val, default=8) -> int:
            """Convert to int safely"""
            if val is None:
                return default
            try:
                return int(float(str(val)))
            except (ValueError, TypeError):
                return default

        def safe_decimal(val, default="0") -> Decimal:
            """Convert to Decimal safely"""
            if val is None:
                return Decimal(default)
            try:
                return Decimal(str(val))
            except (ValueError, TypeError):
                return Decimal(default)

        # Get decimals from market data (API response fields)
        size_decimals = safe_int(data.get('sd'), 8)      # size_decimals from API
        price_decimals = safe_int(data.get('pd'), 6)     # price_decimals from API
        
        # Calculate scales EXACTLY as SDK does
        base_scale = Decimal(10) ** size_decimals        # e.g., 10^8 = 100,000,000 for APT
        quote_scale = Decimal(10) ** price_decimals      # e.g., 10^6 = 1,000,000 for APT
        
        # Get minimum base amount (step size)
        min_base_amount = safe_decimal(data.get('ss'), "0.00000001")
        min_quote_amount = safe_decimal(data.get('mps'), "0.01")
        
        ZERO = Decimal("0")
        
        # ═══════════════════════════════════════════════════════════════
        # VALIDATION: Ensure minimum notional (~$10 USD)
        # ═══════════════════════════════════════════════════════════════
        notional = qty * price
        MIN_NOTIONAL = Decimal("10.5")  # $10 with buffer
        
        if notional < MIN_NOTIONAL:
            if price > ZERO:
                qty = MIN_NOTIONAL / price
                logger.debug(f"{symbol}: Adjusted qty to ${MIN_NOTIONAL:.2f} notional")

        # ═══════════════════════════════════════════════════════════════
        # VALIDATION: Ensure minimum quantity
        # ═══════════════════════════════════════════════════════════════
        if min_base_amount > ZERO and qty < min_base_amount:
            qty = min_base_amount * Decimal("1.05")  # 5% buffer
            logger.debug(f"{symbol}: Adjusted qty to min {min_base_amount}")

        # ═══════════════════════════════════════════════════════════════
        # SCALE TO INTEGERS (EXACT SDK LOGIC)
        # TypeScript: Math.round(amount * baseScale)
        # ═══════════════════════════════════════════════════════════════
        
        # Round UP for base amount (ensure we meet minimum)
        scaled_base = int((qty * base_scale).quantize(Decimal('1'), rounding=ROUND_UP))
        
        # Round price based on side (DOWN for SELL, UP for BUY to be conservative)
        if side == 'SELL':
            scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_DOWN))
        else:
            scaled_price = int((price * quote_scale).quantize(Decimal('1'), rounding=ROUND_UP))

        # ═══════════════════════════════════════════════════════════════
        # CRITICAL VALIDATION: Ensure non-zero values
        # ═══════════════════════════════════════════════════════════════
        if scaled_price == 0:
            raise ValueError(
                f"❌ {symbol}: Scaled price is 0!\n"
                f"   Input price: {price}\n"
                f"   price_decimals: {price_decimals}\n"
                f"   quote_scale: {quote_scale}\n"
                f"   Calculation: {price} * {quote_scale} = {price * quote_scale}"
            )

        if scaled_base == 0:
            raise ValueError(
                f"❌ {symbol}: Scaled base amount is 0!\n"
                f"   Input qty: {qty}\n"
                f"   size_decimals: {size_decimals}\n"
                f"   base_scale: {base_scale}\n"
                f"   min_base_amount: {min_base_amount}"
            )

        # ═══════════════════════════════════════════════════════════════
        # LOGGING for debugging
        # ═══════════════════════════════════════════════════════════════
        logger.debug(
            f"📐 {symbol} Scaling:\n"
            f"   Input: {qty:.8f} @ ${price:.6f}\n"
            f"   Decimals: size={size_decimals}, price={price_decimals}\n"
            f"   Scales: base={base_scale}, quote={quote_scale}\n"
            f"   Output: base={scaled_base}, price={scaled_price}\n"
            f"   Notional: ${notional:.2f}"
        )

        return scaled_base, scaled_price

    @rate_limited(Exchange. LIGHTER)
    @rate_limit_retry(max_retries=3, base_delay=5.0)
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
        
        # Ensure price is float for comparisons and calculations
        try:
            price = float(price) if price is not None else 0.0
        except (ValueError, TypeError):
            price = 0.0
        
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
    @rate_limit_retry(max_retries=3, base_delay=5.0)
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
                # Ensure size is float for comparisons
                try:
                    actual_size = float(actual_size) if actual_size is not None else 0.0
                except (ValueError, TypeError):
                    actual_size = 0.0
                
                actual_size_abs = abs(actual_size)

                if actual_size > 0:
                    close_side = "SELL"
                else:
                    close_side = "BUY"

                price = self.fetch_mark_price(symbol)
                # Ensure price is float for comparisons
                try:
                    price = float(price) if price is not None else 0.0
                except (ValueError, TypeError):
                    price = 0.0
                
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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

    @rate_limit_retry(max_retries=3, base_delay=5.0)
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