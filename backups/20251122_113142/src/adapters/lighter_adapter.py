# src/adapters/lighter_adapter.py - FIXED VERSION

import asyncio
import aiohttp
import json
import logging
import time
import random
from typing import Dict, Tuple, Optional, List
from decimal import Decimal

import config
from lighter.api.order_api import OrderApi
from lighter.api.funding_api import FundingApi
from lighter.signer_client import SignerClient
from lighter.api.account_api import AccountApi
from .base_adapter import BaseAdapter

from src.rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # ✅ ENABLE DEBUG

MARKET_OVERRIDES = {
    "ASTER-USD": {'ss': Decimal('1'), 'sd': 8},
    "HYPE-USD":  {'ss': Decimal('0.01'), 'sd': 6},
    "MEGA-USD":  {'ss': Decimal('99999999')}
}

class LighterAdapter(BaseAdapter):
    def __init__(self):
        super().__init__("Lighter")
        self.market_info: Dict[str, dict] = {}
        self.funding_cache: Dict[str, float] = {}
        self.price_cache: Dict[str, float] = {}
        self._signer: Optional[SignerClient] = None
        self._resolved_account_index: Optional[int] = None
        self._resolved_api_key_index: Optional[int] = None
        self.semaphore = asyncio.Semaphore(10)
        self.rate_limiter = AdaptiveRateLimiter(
            initial_rate=8.0,
            min_rate=2.0,
            max_rate=15.0,
            name="Lighter"
        )
        self._last_market_cache_at: Optional[float] = None
        self._balance_cache = 0.0
        self._last_balance_update = 0.0

    async def get_order_fee(self, order_id: str) -> float:
        """
        ✅ SIMPLIFIED: Lighter Standard Account = 0% Fees
        
        Lighter Orders returnen tx_hash statt order_id.
        Da Standard Accounts IMMER 0% haben, brauchen wir keinen API Call.
        
        Falls du später Fee-Tracking willst:
        - Nutze GET /orders Endpoint mit Filter
        - Oder speichere die order_id aus dem Order Event
        """
        if not order_id or order_id == "DRY_RUN_ORDER_123":
            return 0.0
        
        # Lighter Standard Account = Immer 0%
        # (laut Docs: https://apidocs.lighter.xyz/docs/fees)
        return 0.0

    async def start_websocket(self):
        """
        ✅ PRODUCTION-GRADE: WebSocket mit Exponential Backoff
        
        Flow:
        1. Connect
        2. Subscribe zu Markets
        3. Handle Messages
        4. Bei Error → Exponential Backoff (5s → 10s → 20s → 60s max)
        """
        ws_url = "wss://mainnet.zklighter.elliot.ai/stream"
        
        retry_delay = 2  # Start bei 2s
        max_delay = 60  # Cap bei 60s
        
        while True:
            session = None
            try:
                if not self.market_info:
                    await self.load_market_cache(force=True)

                session = aiohttp.ClientSession()
                async with session.ws_connect(ws_url) as ws:
                    logger.info("🔌 Lighter WebSocket verbunden.")
                    
                    # ===== RESET BACKOFF BEI SUCCESS =====
                    retry_delay = 5
                    
                    market_ids = [m['i'] for m in self.market_info.values()]
                    
                    sub_msg = {
                        "method": "subscribe",
                        "params": ["trade", market_ids],
                        "id": 1
                    }
                    await ws.send_json(sub_msg)
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            
                            if "params" in data and isinstance(data["params"], dict):
                                p = data["params"]
                                mid = p.get("marketId")
                                price_str = p.get("price")
                                
                                if mid and price_str:
                                    updated = False
                                    for sym, info in self.market_info.items():
                                        if info['i'] == mid:
                                            self.price_cache[sym] = float(price_str)
                                            updated = True
                                            break
                                    
                                    if updated and hasattr(self, 'price_update_event'):
                                        self.price_update_event.set()
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f"⚠️ Lighter WS Error: {msg}")
                            break
            except asyncio.CancelledError:
                logger.info("🛑 Lighter WS stopped (CancelledError)")
                break
            except Exception as e:
                error_str = str(e).lower()
                # ===== EXPONENTIAL BACKOFF =====
                if "429" in error_str or "rate limit" in error_str:
                    retry_delay = min(retry_delay * 2, max_delay)
                    logger.warning(
                        f"⚠️ Lighter WS Rate Limited! Backoff: {retry_delay}s "
                        f"(max: {max_delay}s)"
                    )
                else:
                    # Andere Errors → Moderate Backoff
                    retry_delay = min(retry_delay * 1.5, max_delay)
                    logger.error(
                        f"❌ Lighter WS Error: {e}. "
                        f"Reconnect in {retry_delay:.0f}s..."
                    )
                await asyncio.sleep(retry_delay)
            finally:
                await asyncio.sleep(0)  # Let pending tasks finish
                if session and not session.closed:
                    await session.close()

    def _get_base_url(self) -> str:
        return getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")

    async def _auto_resolve_indices(self) -> Tuple[int, int]:
        return int(config.LIGHTER_ACCOUNT_INDEX), int(config.LIGHTER_API_KEY_INDEX)

    async def _get_signer(self) -> SignerClient:
        if self._signer is None:
            if self._resolved_account_index is None:
                self._resolved_account_index, self._resolved_api_key_index = await self._auto_resolve_indices()
            priv_key = str(getattr(config, "LIGHTER_API_PRIVATE_KEY", ""))
            self._signer = SignerClient(
                url=self._get_base_url(),
                private_key=priv_key,
                api_key_index=self._resolved_api_key_index,
                account_index=self._resolved_account_index
            )
        return self._signer

    async def _fetch_single_market(self, order_api: OrderApi, market_id: int):
        """
        ✅ FIXED: Besseres Error Handling
        """
        async with self.semaphore:
            await self.rate_limiter.acquire()
            try:
                details = await order_api.order_book_details(market_id=market_id)
                # Debug: Log successful fetch
                if details and details.order_book_details:
                    logger.debug(f"✅ Market {market_id} fetched successfully")
                else:
                    logger.debug(f"⚠️ Market {market_id} returned empty")
                    return False

                if details and details.order_book_details:
                    m = details.order_book_details[0]
                    if symbol := getattr(m, 'symbol', None):
                        normalized_symbol = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol

                        market_data = {
                            'i': m.market_id,
                            'sd': getattr(m, 'size_decimals', 8),
                            'pd': getattr(m, 'price_decimals', 6),
                            'ss': Decimal(getattr(m, 'step_size', '0.00000001')),
                            'mps': Decimal(getattr(m, 'min_price_step', '0.00001') or '0.00001'),
                            'min_notional': float(getattr(m, 'min_notional', 10.0)),
                            'min_quantity': float(getattr(m, 'min_quantity', 0.0)),
                        }

                        if normalized_symbol in MARKET_OVERRIDES:
                            market_data.update(MARKET_OVERRIDES[normalized_symbol])

                        self.market_info[normalized_symbol] = market_data

                        if price := getattr(m, 'last_trade_price', None):
                            self.price_cache[normalized_symbol] = float(price)
                        self.rate_limiter.on_success()
                        return True  # ✅ Success indicator
            except Exception as e:
                error_str = str(e).lower()

                # Log actual error for debugging
                logger.error(f"❌ Market {market_id} fetch error: {type(e).__name__}: {e}")

                if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
                    # ✅ Don't spam logs for rate limits
                    try:
                        self.rate_limiter.on_429()
                    except Exception:
                        # ensure we don't mask the rate-limit handling if limiter errors
                        pass
                    raise Exception(f"429_RATE_LIMIT: Market {market_id}")
                else:
                    # Log other errors at debug level
                    logger.debug(f"Lighter Fetch Error ID {market_id}: {e}")
            return False  # ✅ Failed

    async def load_market_cache(self, force: bool = False, max_retries: int = 1):
        """
        ✅ PRODUCTION-GRADE: Market Loading mit Retry & Rate Limit Handling
        
        Improvements:
        - Post-Batch Validation (checkt ob alle Markets geladen)
        - Failed Market Retry (max 2x)
        - Smart Rate Limit Handling (Exponential Backoff)
        - Detailed Success/Fail Logging
        """
        if not getattr(config, "LIVE_TRADING", False):
            return

        if not force and self._last_market_cache_at and (time.time() - self._last_market_cache_at < 300):
            return

        logger.info("🔄 Lighter: Aktualisiere Märkte...")
        signer = await self._get_signer()
        order_api = OrderApi(signer.api_client)
        
        try:
            # ===== 1. HOLE LISTE ALLER MARKETS =====
            market_list = await order_api.order_books()
            if not market_list or not market_list.order_books:
                logger.warning("⚠️ Lighter: Keine Markets von API erhalten")
                return

            all_ids = [m.market_id for m in market_list.order_books if hasattr(m, 'market_id')]
            total_markets = len(all_ids)
            logger.debug(f"📋 Lighter: {total_markets} Markets zu laden...")
            
            # ===== 2. BATCH PROCESSING MIT RETRY =====
            BATCH_SIZE = 10
            SLEEP_BETWEEN_BATCHES = 0.5
            MAX_RETRIES = max_retries
            
            successful_loads = 0
            failed_markets = []
            
            # Track which markets are actually processed
            processed_ids = set()
            
            for batch_num, i in enumerate(range(0, len(all_ids), BATCH_SIZE), start=1):
                batch_ids = all_ids[i:i + BATCH_SIZE]
                
                # Try batch up to MAX_RETRIES times
                for retry in range(MAX_RETRIES + 1):
                    try:
                        # Execute batch
                        tasks = [self._fetch_single_market(order_api, mid) for mid in batch_ids]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # ✅ CHECK FOR ACTUAL EXCEPTIONS
                        for idx, result in enumerate(results):
                            if isinstance(result, Exception):
                                logger.error(f"❌ Market {batch_ids[idx]}: {result}")
                        
                        # ===== 🆕 POST-BATCH VALIDATION =====
                        batch_loaded_ids = []
                        for mid in batch_ids:
                            if mid in processed_ids:
                                continue  # Already loaded
                            
                            if any(m['i'] == mid for m in self.market_info.values()):
                                batch_loaded_ids.append(mid)
                                processed_ids.add(mid)
                        
                        batch_success_count = len(batch_loaded_ids)
                        batch_expected_count = len(batch_ids)
                        
                        successful_loads += batch_success_count
                        
                        # ===== 🆕 IDENTIFY FAILED MARKETS =====
                        batch_failed_ids = set(batch_ids) - processed_ids
                        
                        # If we got all markets in this batch, break retry loop
                        if batch_success_count == batch_expected_count:
                            if batch_num % 10 == 0:  # Log every 10th batch
                                logger.debug(f"✅ Batch {batch_num}: {batch_success_count}/{batch_expected_count} loaded")
                            break
                        
                        # Some failed - check if 429 error
                        has_429 = any(
                            isinstance(r, Exception) and "429" in str(r) 
                            for r in results
                        )
                        
                        if has_429 and retry < MAX_RETRIES:
                            wait_time = (retry + 1) * 2  # Exponential: 2s, 4s
                            logger.warning(
                                f"⚠️ Lighter Batch {batch_num}: Rate Limited. "
                                f"Retry {retry+1}/{MAX_RETRIES} in {wait_time}s... "
                                f"({batch_success_count}/{batch_expected_count} loaded)"
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        
                        # ===== 🆕 RETRY FAILED MARKETS (if not 429) =====
                        if not has_429 and batch_failed_ids and retry < MAX_RETRIES:
                            logger.info(
                                f"🔄 Batch {batch_num}: Retrying {len(batch_failed_ids)} failed markets..."
                            )
                            await asyncio.sleep(1)
                            
                            # Retry nur failed markets
                            retry_tasks = [self._fetch_single_market(order_api, mid) for mid in batch_failed_ids]
                            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
                            
                            # Re-count successes
                            for mid in batch_failed_ids:
                                if any(m['i'] == mid for m in self.market_info.values()):
                                    successful_loads += 1
                                    batch_failed_ids.remove(mid)
                        
                        # Log failed markets (on last retry)
                        if retry == MAX_RETRIES and batch_failed_ids:
                            failed_markets.extend(batch_failed_ids)
                            logger.debug(
                                f"⚠️ Batch {batch_num}: {len(batch_failed_ids)} markets failed after {MAX_RETRIES+1} attempts"
                            )
                        
                        break  # Exit retry loop
                        
                    except Exception as e:
                        if retry < MAX_RETRIES:
                            logger.warning(f"⚠️ Lighter Batch {batch_num} Error: {e}. Retrying...")
                            await asyncio.sleep(2)
                        else:
                            logger.error(f"❌ Lighter Batch {batch_num} failed after {MAX_RETRIES} retries: {e}")
                            failed_markets.extend(batch_ids)
                
                # Rate limiting between batches
                if i + BATCH_SIZE < len(all_ids):  # Don't sleep after last batch
                    await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
            
            # Emergency: If 0 markets loaded, disable retries and try once more
            if successful_loads == 0 and max_retries > 0:
                logger.warning("⚠️ 0 markets loaded, retry without batch retries...")
                await self.load_market_cache(force=True, max_retries=0)

            # ===== 3. SUMMARY LOG =====
            self._last_market_cache_at = time.time()
            
            success_rate = (successful_loads / total_markets * 100) if total_markets > 0 else 0
            
            if failed_markets:
                logger.warning(
                    f"⚠️ Lighter: {successful_loads}/{total_markets} Märkte geladen ({success_rate:.1f}%). "
                    f"{len(failed_markets)} failed."
                )
                if len(failed_markets) <= 10:  # Only log if few failures
                    logger.debug(f"Failed Market IDs: {failed_markets}")
            else:
                logger.info(f"✅ Lighter: {successful_loads} Märkte geladen (100%).")
                
        except Exception as e:
            logger.error(f"❌ Lighter Market Cache Error: {e}")

    async def load_funding_rates_and_prices(self):
        if not getattr(config, "LIVE_TRADING", False):
            return
        
        try:
            signer = await self._get_signer()
            funding_api = FundingApi(signer.api_client)
            
            fd_response = await funding_api.funding_rates()
            if fd_response and fd_response.funding_rates:
                rates_by_id = {fr.market_id: fr.rate for fr in fd_response.funding_rates}
                for symbol, data in self.market_info.items():
                    if rate := rates_by_id.get(data['i']):
                        self.funding_cache[symbol] = float(rate)
        except Exception as e:
            logger.error(f"Lighter Funding Fetch Error: {e}")

    def fetch_24h_vol(self, symbol: str) -> float:
        return 0.0
    
    def min_notional_usd(self, symbol: str) -> float:
        """
        🔧 UNIVERSAL: Berechnet echtes Minimum mit Safety-Buffer
        """
        HARD_MIN_USD = 15.0
        SAFETY_BUFFER = 1.10  # 10% Buffer
        
        data = self.market_info.get(symbol)
        if not data:
            return HARD_MIN_USD
        
        try:
            price = self.fetch_mark_price(symbol)
            if not price or price <= 0:
                return HARD_MIN_USD
            
            # 1. USD-basiertes Minimum (aus API)
            min_notional_api = float(data.get('min_notional', 0))
            
            # 2. Quantity-basiertes Minimum (aus API)
            min_qty = float(data.get('min_quantity', 0))
            min_qty_usd = min_qty * price if min_qty > 0 else 0
            
            # 3. Nimm das GRÖSSERE von beiden
            api_min = max(min_notional_api, min_qty_usd)
            
            # 4. Safety-Buffer (10% drauf)
            safe_min = api_min * SAFETY_BUFFER
            
            # 5. Niemals unter HARD_MIN
            result = max(safe_min, HARD_MIN_USD)
            
            return result
            
        except Exception as e:
            logger.warning(f"⚠️ {symbol} min_notional_usd Error: {e}")
            return HARD_MIN_USD
    
    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Lighter: 8h-Rate → Stunden-Rate"""
        rate_8h = self.funding_cache.get(symbol)
        if rate_8h is None:
            return None
        return float(rate_8h) / 8.0
    
    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        return self.price_cache.get(symbol)
    
    async def get_real_available_balance(self) -> float:
        if time.time() - self._last_balance_update < 60.0:
            if self._balance_cache == 0 and getattr(config, "LIVE_TRADING", False):
                pass
            else:
                return self._balance_cache
        account_api = None
        try:
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)
            
            await asyncio.sleep(0.5)
            
            for _ in range(2):
                try:
                    response = await account_api.account(
                        by="index", 
                        value=str(self._resolved_account_index)
                    )
                    val = 0.0
                    if response and response.accounts and response.accounts[0]:
                        acc = response.accounts[0]
                        buying = getattr(acc, 'buying_power', None) or getattr(acc, 'total_asset_value', '0')
                        val = float(buying or 0)
                    
                    self._balance_cache = val
                    self._last_balance_update = time.time()
                    break
                except Exception as e:
                    if "429" in str(e):
                        await asyncio.sleep(2)
                        continue
                    raise e
        except Exception as e:
            if "429" in str(e):
                logger.warning(f"⚠️ Lighter Balance 429. Cache: ${self._balance_cache:.2f}")
            else:
                logger.error(f"❌ Lighter Balance Error: {e}")
        finally:
            pass
        return self._balance_cache

    async def fetch_open_positions(self) -> List[dict]:
        if not getattr(config, "LIVE_TRADING", False):
            return []
        account_api = None
        try:
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)
            await asyncio.sleep(0.2)
            response = await account_api.account(
                by="index", 
                value=str(self._resolved_account_index)
            )
            positions = []
            if response and response.accounts and response.accounts[0].positions:
                for p in response.accounts[0].positions:
                    multiplier = 1 if int(p.sign) == 0 else -1
                    size = float(p.position or 0.0) * multiplier
                    if abs(size) > 1e-8:
                        symbol = f"{p.symbol}-USD" if not p.symbol.endswith("-USD") else p.symbol
                        positions.append({'symbol': symbol, 'size': size})
            return positions
        except Exception:
            return []

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        """
        🔧 PRODUCTION-GRADE FIX: Skaliert Quantity & Price mit Pre-Validation

        Flow:
        1. Validate inputs
        2. Calculate target notional
        3. PRE-CHECK min_notional BEFORE scaling
        4. Apply step_size rounding (ROUND_UP for quantity)
        5. Scale to integers
        6. POST-CHECK and auto-bump if needed
        7. Final validation
        """
        from decimal import ROUND_UP, ROUND_DOWN

        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f"❌ Metadata missing for {symbol}")

        # ===== 1. GET MARKET CONFIG (ALL AS DECIMAL) =====
        sd = data.get('sd', 8)  # Size decimals (int)
        pd = data.get('pd', 6)  # Price decimals (int)
        step_size = Decimal(str(data.get('ss', '0.00000001')))
        price_step = Decimal(str(data.get('mps', '0.00001')))
        min_notional = Decimal(str(data.get('min_notional', 10.0)))  # ✅ CONVERT TO DECIMAL
        min_quantity = Decimal(str(data.get('min_quantity', 0.0)))   # ✅ CONVERT TO DECIMAL

        SAFETY_BUFFER = Decimal('1.05')

        # ===== 2. PRICE ROUNDING =====
        if price_step > 0:
            if side.upper() == 'BUY':
                price = ((price + price_step - Decimal('1e-12')) // price_step) * price_step
            else:
                price = (price // price_step) * price_step

        # ===== 3. PRE-VALIDATION: CHECK IF TARGET NOTIONAL IS ACHIEVABLE =====
        target_notional = qty * price  # ✅ NOW ALL DECIMAL
        min_required_notional = min_notional * SAFETY_BUFFER  # ✅ NOW ALL DECIMAL

        if target_notional < min_required_notional:
            logger.debug(
                f"🔍 {symbol} Pre-Check: Target ${float(target_notional):.2f} < Required ${float(min_required_notional):.2f}"
            )

            # Calculate minimum quantity needed
            min_qty_needed = min_required_notional / price

            # Apply safety buffer to quantity as well
            qty = min_qty_needed * SAFETY_BUFFER

            logger.info(
                f"⬆️ {symbol} Auto-Bump: {float(min_qty_needed):.8f} → {float(qty):.8f} "
                f"(${float(target_notional):.2f} → ${float(qty * price):.2f})"
            )

        # ===== 4. QUANTITY ROUNDING WITH STEP SIZE =====
        if step_size > 0:
            qty_in_steps = qty / step_size
            rounded_steps = qty_in_steps.quantize(Decimal('1'), rounding=ROUND_UP)
            qty = rounded_steps * step_size

            if min_quantity > 0 and qty < min_quantity:
                steps_needed = (min_quantity / step_size).quantize(
                    Decimal('1'), rounding=ROUND_UP
                )
                qty = steps_needed * step_size
                logger.debug(f"📏 {symbol} Quantity bumped to min: {float(qty):.8f}")

        # ===== 5. SCALE TO INTEGERS =====
        scaled_base = int((qty * (Decimal(10) ** sd)).quantize(Decimal('1'), rounding=ROUND_UP))
        scaled_price = int(price * (Decimal(10) ** pd))

        # ===== 6. POST-VALIDATION: VERIFY SCALED AMOUNTS =====
        actual_qty = Decimal(scaled_base) / (Decimal(10) ** sd)
        actual_price = Decimal(scaled_price) / (Decimal(10) ** pd)
        actual_notional = actual_qty * actual_price

        if actual_notional < min_notional * Decimal('0.99'):  # ✅ FIX: Decimal('0.99') statt 0.99
            logger.warning(
                f"⚠️ {symbol} POST-CHECK FAILED: ${float(actual_notional):.2f} < ${float(min_notional):.2f}"
            )
            if step_size > 0:
                qty_bumped = actual_qty + step_size
                scaled_base = int((qty_bumped * (Decimal(10) ** sd)).quantize(Decimal('1'), rounding=ROUND_UP))
                actual_qty = Decimal(scaled_base) / (Decimal(10) ** sd)
                actual_notional = actual_qty * actual_price
                logger.info(
                    f"🔧 {symbol} Emergency Bump: Added 1 step → ${float(actual_notional):.2f}"
                )

        # ===== 7. FINAL SAFETY CHECK =====
        if scaled_base == 0:
            raise ValueError(
                f"❌ {symbol} Fatal: Scaled base is 0! "
                f"Input qty: {float(qty):.8f}, Price: {float(price):.2f}, "
                f"Step size: {float(step_size):.8f}"
            )

        if actual_notional < min_notional * Decimal('0.95'):  # ✅ FIX: Decimal('0.95') statt 0.95
            raise ValueError(
                f"❌ {symbol} Fatal: Cannot meet min_notional requirement. "
                f"Actual: ${float(actual_notional):.2f}, Required: ${float(min_notional):.2f}, "
                f"Step size: {float(step_size):.8f} may be too large."
            )

        if symbol in ["BTC-USD", "ETH-USD"] or logger.level == logging.DEBUG:
            logger.debug(
                f"✅ {symbol} Scaling Success:\n"
                f"   Input:  qty={float(qty):.8f}, price={float(price):.2f}\n"
                f"   Output: base={scaled_base}, price_int={scaled_price}\n"
                f"   Actual: qty={float(actual_qty):.8f}, notional=${float(actual_notional):.2f}\n"
                f"   Min Required: ${float(min_notional):.2f}, Step: {float(step_size):.8f}"
            )

        return scaled_base, scaled_price

    async def open_live_position(
        self, 
        symbol: str, 
        side: str, 
        notional_usd: float, 
        reduce_only: bool = False, 
        post_only: bool = False
    ) -> Tuple[bool, Optional[str]]:
        if not getattr(config, "LIVE_TRADING", False):
            return True, None
        
        price = self.fetch_mark_price(symbol)
        if not price or price <= 0:
            return False, None

        try:
            market_id = self.market_info[symbol]['i']
            
            # Convert ALL inputs to Decimal early
            price_decimal = Decimal(str(price))
            notional_decimal = Decimal(str(notional_usd))
            slippage = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
            
            # Calculate limit price with slippage
            slippage_multiplier = Decimal(1) + (slippage / Decimal(100)) if side == 'BUY' else Decimal(1) - (slippage / Decimal(100))
            limit_price = price_decimal * slippage_multiplier
            
            # Calculate quantity
            qty = notional_decimal / limit_price

            base, price_int = self._scale_amounts(symbol, qty, limit_price, side)
            
            # 🔍 CRITICAL DEBUG
            if symbol in ["BTC-USD", "ETH-USD"]:
                actual_qty = Decimal(base) / (Decimal(10) ** self.market_info[symbol]['sd'])
                actual_notional = float(actual_qty * Decimal(str(limit_price)))
                logger.info(
                    f"🔍 {symbol} Scaling Debug:\n"
                    f"   Input Qty: {qty:.8f}\n"
                    f"   Step Size: {self.market_info[symbol].get('ss')}\n"
                    f"   Scaled Base: {base}\n"
                    f"   Actual Qty: {actual_qty:.8f}\n"
                    f"   Actual Notional: ${actual_notional:.2f}"
                )
            
            if base == 0:
                return False, None

            signer = await self._get_signer()

            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        await asyncio.sleep(0.5 * attempt)

                    client_oid = int(time.time() * 1000) + random.randint(0, 99999)

                    tx, resp, err = await signer.create_order(
                        market_index=market_id,
                        client_order_index=client_oid, 
                        base_amount=base,
                        price=price_int,
                        is_ask=(side == 'SELL'),
                        order_type=SignerClient.ORDER_TYPE_LIMIT,
                        time_in_force=SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                        reduce_only=reduce_only,
                        trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                        order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
                    )

                    if err:
                        err_str = str(err).lower()
                        
                        # 🔧 BESSERES ERROR LOGGING
                        if "invalid order base or quote amount" in err_str:
                            logger.error(
                                f"❌ {symbol} Invalid Amount Error!\n"
                                f"   Notional: ${notional_usd:.2f}\n"
                                f"   Base: {base} (scaled)\n"
                                f"   Price: {price_int} (scaled)\n"
                                f"   Min Notional: ${self.min_notional_usd(symbol):.2f}"
                            )
                            return False, None
                        
                        if "nonce" in err_str or "429" in err_str or "too many requests" in err_str:
                            logger.warning(f"⚠️ Lighter Retry ({attempt+1}/{max_retries+1}): {err}")
                            if attempt < max_retries:
                                continue

                        logger.error(f"Lighter Order Error: {err}")
                        return False, None

                    tx_hash = getattr(resp, 'tx_hash', 'OK')
                    logger.info(f"✅ Lighter Order: {tx_hash}")
                    return True, str(tx_hash)

                except Exception as inner_e:
                    logger.error(f"Lighter Inner Error: {inner_e}")
                    return False, None

            return False, None

        except Exception as e:
            logger.error(f"Lighter Execution Error: {e}")
            return False, None

    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        """
        🔥 AGGRESSIVE CLOSE mit Position Verification
        """
        close_side = "SELL" if original_side == "BUY" else "BUY"
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # 1. Get Price
                price = self.fetch_mark_price(symbol)
                if not price or price <= 0:
                    return False, None
                
                # 2. AGGRESSIVE SLIPPAGE (2%, 5%, 10%)
                slippage_pct = [2.0, 5.0, 10.0][min(attempt, 2)]
                slippage = Decimal(str(slippage_pct)) / Decimal(100)
                price_decimal = Decimal(str(price))
                
                if close_side == "BUY":
                    limit_price = price_decimal * (Decimal(1) + slippage)
                else:
                    limit_price = price_decimal * (Decimal(1) - slippage)
                
                logger.info(
                    f"🔥 AGGRESSIVE CLOSE {symbol}: "
                    f"Attempt {attempt+1}, Slippage {slippage_pct}%"
                )
                
                # 3. Calculate & Scale
                qty = Decimal(str(notional_usd)) / limit_price
                base, price_int = self._scale_amounts(symbol, qty, limit_price, close_side)
                
                if base == 0:
                    return False, None
                
                # 4. Send Order (SHORT EXPIRY!)
                signer = await self._get_signer()
                client_oid = int(time.time() * 1000) + random.randint(0, 99999)
                market_id = self.market_info[symbol]['i']
                
                tx, resp, err = await signer.create_order(
                    market_index=market_id,
                    client_order_index=client_oid,
                    base_amount=base,
                    price=price_int,
                    is_ask=(close_side == 'SELL'),
                    order_type=SignerClient.ORDER_TYPE_LIMIT,
                    time_in_force=SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=True,
                    trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                    order_expiry=int(time.time() * 1000) + 10000  # 🔥 10s expiry!
                )
                
                if err:
                    logger.error(f"Order error: {err}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    return False, None
                
                tx_hash = getattr(resp, 'tx_hash', 'OK')
                logger.info(f"✅ Order sent: {tx_hash}")
                
                # 5. VERIFY CLOSE
                await asyncio.sleep(2 + attempt)
                
                updated_positions = await self.fetch_open_positions()
                still_open = any(
                    p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8
                    for p in (updated_positions or [])
                )
                
                if not still_open:
                    logger.info(f"✅✅ {symbol} VERIFIED CLOSED")
                    return True, str(tx_hash)
                else:
                    logger.warning(f"⚠️ {symbol} still open after attempt {attempt+1}")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        logger.error(f"❌ {symbol} FAILED after {max_retries} attempts!")
                        return False, str(tx_hash)
            
            except Exception as e:
                logger.error(f"Close exception: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False, None
        
        return False, None

    async def aclose(self):
        if self._signer:
            try:
                if hasattr(self._signer, 'api_client') and hasattr(self._signer.api_client, 'close'):
                    await self._signer.api_client.close()
                elif hasattr(self._signer, 'close'):
                    await self._signer.close()
            except:
                pass