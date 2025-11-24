# src/adapters/lighter_adapter.py
import asyncio
import aiohttp
import json
import logging
import time
import random
from typing import Dict, Tuple, Optional, List
from decimal import Decimal, ROUND_UP, ROUND_DOWN

import config

try:
    from lighter.lighter_api.order_api import OrderApi
    from lighter.lighter_api.funding_api import FundingApi
    from lighter.lighter_api.account_api import AccountApi
except Exception:
    from lighter.api.order_api import OrderApi
    from lighter.api.funding_api import FundingApi
    from lighter.api.account_api import AccountApi

from lighter.signer_client import SignerClient
from .base_adapter import BaseAdapter
from src.rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger(__name__)

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
        self.orderbook_cache: Dict[str, dict] = {}
        self._signer: Optional[SignerClient] = None
        self._resolved_account_index: Optional[int] = None
        self._resolved_api_key_index: Optional[int] = None
        self.semaphore = asyncio.Semaphore(3)
        self.rate_limiter = AdaptiveRateLimiter(
            initial_rate=3.0,
            min_rate=1.0,
            max_rate=15.0,
            name="Lighter"
        )
        self._last_market_cache_at: Optional[float] = None
        self._balance_cache = 0.0
        self._last_balance_update = 0.0

    async def get_order_fee(self, order_id: str) -> float:
        """
        Fetch real fee from Lighter order

        Returns:
            Fee rate (e.g. 0.0 for maker rebate)
        """
        if not order_id or order_id == "DRY_RUN_ORDER_123":
            return 0.0

        try:
            # Try to fetch via Lighter Order API if available. Some SDKs expose
            # an order/details endpoint similar to X10. We'll attempt several
            # candidate method names on the OrderApi and parse a returned
            # object/dict for fee, filled quantity and price. If no suitable
            # endpoint is available, fall back to 0.0.

            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)

            # Candidate method names that an SDK might implement
            candidate_methods = [
                'order_details', 'order_detail', 'get_order', 'get_order_by_id',
                'order', 'order_status', 'order_info', 'get_order_info', 'retrieve_order'
            ]

            method = None
            for name in candidate_methods:
                if hasattr(order_api, name):
                    method = getattr(order_api, name)
                    break

            if method is None:
                # API endpoint not available
                # Return 0.0 as before and keep explicit comment to aid future work
                # API endpoint not available
                return 0.0

            # Try a few common parameter names when calling the SDK method
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
                    # signature mismatch, try next
                    continue
                except Exception:
                    # Some SDK calls may raise; ignore and try next
                    continue

            if resp is None:
                return 0.0

            # Normalize response into a dict-like accessor
            def _get(obj, keys):
                if obj is None:
                    return None
                # dict-like
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
                # fallback, try attrib 'data' or 'order'
                try:
                    if hasattr(obj, 'data'):
                        d = getattr(obj, 'data')
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

            # Candidate field names
            fee_keys = ['fee', 'fee_amount', 'fee_usd', 'fees', 'fee_value']
            filled_keys = ['filled', 'filled_amount', 'filled_amount_of_synthetic', 'filled_quantity', 'filled_size', 'filled_amount_of_quote']
            price_keys = ['price', 'avg_price', 'filled_price']

            fee_abs = _get(resp, fee_keys)
            filled = _get(resp, filled_keys)
            price = _get(resp, price_keys)

            # If response nested under 'order' or similar, attempt that too
            if fee_abs is None or filled is None or price is None:
                nested = None
                if isinstance(resp, dict):
                    nested = resp.get('order') or resp.get('data') or resp.get('result')
                elif hasattr(resp, 'order'):
                    nested = getattr(resp, 'order')
                elif hasattr(resp, 'data'):
                    nested = getattr(resp, 'data')

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
                        # sanity bound
                        if 0 <= fee_rate <= 0.1:
                            return fee_rate
            except Exception:
                pass

            # If we reach here, we couldn't compute a concrete fee
            return 0.0
        except Exception as e:
            logger.error(f"Lighter Fee Fetch Error for {order_id}: {e}")
            return 0.0

    async def start_websocket(self):
        ws_url = "wss://mainnet.zklighter.elliot.ai/stream"
        retry_delay = 2
        max_delay = 60
        
        while True:
            session = None
            try:
                # CRITICAL: Load markets BEFORE connecting if not loaded
                if not self.market_info:
                    await self.load_market_cache(force=True)

                logger.debug(f"Lighter: Connecting to {ws_url}")
                session = aiohttp.ClientSession()
                async with session.ws_connect(
                    ws_url,
                    heartbeat=15,
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=30)
                ) as ws:
                    logger.info("🟢 Lighter WebSocket connected")
                    logger.debug(f"Lighter: Connection state: {ws.closed}")
                    retry_delay = 5
                    
                    # CRITICAL: Verify market_info loaded before subscribing
                    if not self.market_info:
                        logger.error("❌ Lighter: No markets available - cannot subscribe")
                        await asyncio.sleep(5)
                        break
                    
                    market_ids = [m['i'] for m in self.market_info.values()]
                    logger.info(f"📊 Lighter: Subscribing to {len(market_ids)} markets")
                    priority_markets = market_ids[:20]
                    
                    sub_msg = {
                        "method": "subscribe",
                        "params": ["trade", "order_book", priority_markets],
                        "id": 1
                    }
                    
                    logger.debug(f"Lighter: Sending subscription for {len(priority_markets)} markets")
                    await ws.send_json(sub_msg)
                    
                    logger.debug("Lighter: Waiting for subscription confirmation...")
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                
                                # Debug first message
                                if not hasattr(self, '_ws_debug_logged'):
                                    try:
                                        logger.debug(f"Lighter first message: {data}")
                                    except Exception:
                                        logger.debug("Lighter first message received (unable to stringify)")
                                    self._ws_debug_logged = True
                            except Exception:
                                continue
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
                                            try:
                                                self.price_cache[sym] = float(price_str)
                                            except Exception:
                                                pass
                                            updated = True
                                            break

                                    if updated and hasattr(self, 'price_update_event'):
                                        try:
                                            self.price_update_event.set()
                                        except Exception:
                                            pass

                                market_id = p.get("market_id")
                                if market_id and "order_book" in p:
                                    for sym, info in self.market_info.items():
                                        if info.get('i') == market_id:
                                            ob_data = p["order_book"]
                                            bids = []
                                            asks = []

                                            for bid in ob_data.get("bids", []):
                                                try:
                                                    bids.append([
                                                        float(bid.get("price", 0)),
                                                        float(bid.get("size", 0))
                                                    ])
                                                except Exception:
                                                    pass

                                            for ask in ob_data.get("asks", []):
                                                try:
                                                    asks.append([
                                                        float(ask.get("price", 0)),
                                                        float(ask.get("size", 0))
                                                    ])
                                                except Exception:
                                                    pass

                                            self.orderbook_cache[sym] = {
                                                'bids': bids,
                                                'asks': asks,
                                                'timestamp': time.time()
                                            }
                                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f"⚠️ Lighter WS Error: {msg}")
                            break
            except asyncio.CancelledError:
                logger.info("🛑 Lighter WS stopped (CancelledError)")
                break
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate limit" in error_str:
                    retry_delay = min(retry_delay * 2, max_delay)
                    logger.warning(f"⚠️ Lighter WS Rate Limited! Backoff: {retry_delay}s")
                else:
                    retry_delay = min(retry_delay * 1.5, max_delay)
                    logger.error(f"❌ Lighter WS Error: {e}. Reconnect in {retry_delay:.0f}s...")
                await asyncio.sleep(retry_delay)
            finally:
                await asyncio.sleep(0)
                if session and not session.closed:
                    try:
                        await session.close()
                    except Exception:
                        pass

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
        async with self.semaphore:
            await self.rate_limiter.acquire()
            try:
                details = await order_api.order_book_details(market_id=market_id)
                if not details or not details.order_book_details:
                    return False

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
                    return True
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
                    self.rate_limiter.on_429()
                    return False
            return False

    async def load_market_cache(self, force: bool = False, max_retries: int = 1):
        if not getattr(config, "LIVE_TRADING", False):
            return

        if not force and self._last_market_cache_at and (time.time() - self._last_market_cache_at < 300):
            return

        logger.info("⚙️ Lighter: Aktualisiere Märkte...")
        signer = None
        order_api = None
        try:
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)

            market_list = await order_api.order_books()
            if not market_list or not getattr(market_list, 'order_books', None):
                logger.warning("⚠️ Lighter: Keine Markets von API erhalten")
                return

            all_ids = [getattr(m, 'market_id', None) for m in market_list.order_books]
            all_ids = [mid for mid in all_ids if mid]
            total_markets = len(all_ids)

            BATCH_SIZE = 5
            SLEEP_BETWEEN_BATCHES = 3.0
            MAX_RETRIES = max_retries

            successful_loads = 0
            failed_markets = []
            processed_ids = set()

            for batch_num, i in enumerate(range(0, len(all_ids), BATCH_SIZE), start=1):
                batch_ids = all_ids[i:i + BATCH_SIZE]

                if batch_num > 1:
                    await asyncio.sleep(SLEEP_BETWEEN_BATCHES)

                for retry in range(MAX_RETRIES + 1):
                    tasks = [self._fetch_single_market(order_api, mid) for mid in batch_ids]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Re-evaluate which ids were actually loaded into market_info
                    batch_loaded_ids = []
                    for mid in batch_ids:
                        if mid in processed_ids:
                            continue
                        if any(m['i'] == mid for m in self.market_info.values()):
                            batch_loaded_ids.append(mid)
                            processed_ids.add(mid)

                    batch_success_count = len(batch_loaded_ids)
                    batch_expected_count = len(batch_ids)

                    successful_loads += batch_success_count
                    batch_failed_ids = set(batch_ids) - processed_ids

                    if batch_success_count == batch_expected_count:
                        break

                    has_429 = any(
                        isinstance(r, Exception) and "429" in str(r)
                        for r in results
                    )

                    if has_429 and retry < MAX_RETRIES:
                        wait_time = (retry + 1) * 2
                        logger.warning(
                            f"⚠️ Lighter Batch {batch_num}: Rate Limited. Retry {retry+1}/{MAX_RETRIES} in {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    if not has_429 and batch_failed_ids and retry < MAX_RETRIES:
                        await asyncio.sleep(1)
                        retry_tasks = [self._fetch_single_market(order_api, mid) for mid in batch_failed_ids]
                        await asyncio.gather(*retry_tasks, return_exceptions=True)

                        for mid in list(batch_failed_ids):
                            if any(m['i'] == mid for m in self.market_info.values()):
                                successful_loads += 1
                                batch_failed_ids.remove(mid)

                    if retry == MAX_RETRIES and batch_failed_ids:
                        failed_markets.extend(batch_failed_ids)

                    break

            if successful_loads == 0 and max_retries > 0:
                logger.warning("⚠️ 0 markets loaded, retry without batch retries...")
                await self.load_market_cache(force=True, max_retries=0)

            self._last_market_cache_at = time.time()
            success_rate = (successful_loads / total_markets * 100) if total_markets > 0 else 0

            if failed_markets:
                logger.warning(
                    f"⚙️ Lighter: {successful_loads}/{total_markets} Märkte geladen ({success_rate:.1f}%). {len(failed_markets)} failed."
                )
            else:
                logger.info(f"✅ Lighter: {successful_loads} Märkte geladen ({success_rate:.1f}%).")

        except Exception as e:
            logger.error(f"❌ Lighter Market Cache Error: {e}")
        finally:
            # CRITICAL: Final cleanup attempt - ensure underlying HTTP/session objects closed
            try:
                if signer and hasattr(signer, 'api_client'):
                    api_client = signer.api_client
                    # aiohttp-based clients may expose 'close' as coroutine or regular
                    if hasattr(api_client, 'close'):
                        maybe = api_client.close()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    # Some SDKs wrap an underlying session
                    elif hasattr(api_client, 'session') and hasattr(api_client.session, 'close'):
                        maybe2 = api_client.session.close()
                        if asyncio.iscoroutine(maybe2):
                            await maybe2
            except Exception as e:
                logger.debug(f"Session close attempt failed: {e}")

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
            logger.error(f" Lighter Funding Fetch Error: {e}")

    def fetch_24h_vol(self, symbol: str) -> float:
        return 0.0
    
    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> dict:
        try:
            if symbol in self.orderbook_cache:
                ob = self.orderbook_cache[symbol]
                if time.time() - ob.get('timestamp', 0) < 2.0:
                    return {
                        'bids': ob.get('bids', [])[:limit],
                        'asks': ob.get('asks', [])[:limit]
                    }
            return {'bids': [], 'asks': []}
        except Exception:
            return {'bids': [], 'asks': []}

    async def fetch_open_interest(self, symbol: str) -> float:
        if not hasattr(self, '_oi_cache'):
            self._oi_cache = {}
            self._oi_cache_time = {}
        
        now = time.time()
        if symbol in self._oi_cache:
            if now - self._oi_cache_time.get(symbol, 0) < 60.0:
                return self._oi_cache[symbol]
        
        try:
            market_data = self.market_info.get(symbol)
            if not market_data:
                return 0.0
            
            market_id = market_data.get('i')
            if not market_id:
                return 0.0
            
            await self.rate_limiter.acquire()
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)
            response = await order_api.order_book_details(market_id=market_id)
            
            if response and response.order_book_details:
                details = response.order_book_details[0]
                
                if hasattr(details, 'open_interest'):
                    oi = float(details.open_interest)
                    self._oi_cache[symbol] = oi
                    self._oi_cache_time[symbol] = now
                    self.rate_limiter.on_success()
                    return oi
                
                if hasattr(details, 'volume_24h'):
                    vol = float(details.volume_24h)
                    self._oi_cache[symbol] = vol
                    self._oi_cache_time[symbol] = now
                    self.rate_limiter.on_success()
                    return vol
            return 0.0
        except Exception as e:
            if "429" in str(e).lower():
                self.rate_limiter.on_429()
            return 0.0

    def min_notional_usd(self, symbol: str) -> float:
        HARD_MIN_USD = 15.0
        SAFETY_BUFFER = 1.10
        
        data = self.market_info.get(symbol)
        if not data:
            return HARD_MIN_USD
        
        try:
            price = self.fetch_mark_price(symbol)
            if not price or price <= 0:
                return HARD_MIN_USD
            
            min_notional_api = float(data.get('min_notional', 0))
            min_qty = float(data.get('min_quantity', 0))
            min_qty_usd = min_qty * price if min_qty > 0 else 0
            api_min = max(min_notional_api, min_qty_usd)
            safe_min = api_min * SAFETY_BUFFER
            result = max(safe_min, HARD_MIN_USD)
            return result
        except Exception:
            return HARD_MIN_USD
    
    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        rate_8h = self.funding_cache.get(symbol)
        if rate_8h is None:
            return None
        return float(rate_8h) / 8.0
    
    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        return self.price_cache.get(symbol)
    
    async def get_real_available_balance(self) -> float:
        if time.time() - self._last_balance_update < 2.0:
            return self._balance_cache
        
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
            if "429" not in str(e):
                logger.error(f" Lighter Balance Error: {e}")
        return self._balance_cache

    async def fetch_open_positions(self) -> List[dict]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.debug("Lighter: LIVE_TRADING=False, returning []")
            return []
        
        try:
            signer = await self._get_signer()
            account_api = AccountApi(signer.api_client)
            
            logger.debug(f"Lighter: Fetching positions for account_index={self._resolved_account_index}")
            
            await asyncio.sleep(0.2)  # Rate limit protection
            
            response = await account_api.account(
                by="index", 
                value=str(self._resolved_account_index)
            )
            
            if not response:
                logger.warning("Lighter: API returned None")
                return []
            
            if not response.accounts:
                logger.warning("Lighter: API returned no accounts")
                return []
            
            if not response.accounts[0]:
                logger.warning("Lighter: API returned empty account")
                return []
            
            account = response.accounts[0]
            
            if not hasattr(account, 'positions') or not account.positions:
                logger.debug("Lighter: Account has no positions")
                return []
            
            positions = []
            for p in account.positions:
                symbol_raw = getattr(p, 'symbol', None)
                position_qty = getattr(p, 'position', None)
                sign = getattr(p, 'sign', None)
                
                if not symbol_raw or position_qty is None or sign is None:
                    continue
                
                multiplier = 1 if int(sign) == 0 else -1
                size = float(position_qty) * multiplier
                
                logger.debug(f"Lighter: Position {symbol_raw} sign={sign} qty={position_qty} size={size}")
                
                if abs(size) > 1e-8:
                    symbol = f"{symbol_raw}-USD" if not symbol_raw.endswith("-USD") else symbol_raw
                    positions.append({
                        'symbol': symbol, 
                        'size': size
                    })
            
            logger.info(f"Lighter: Found {len(positions)} open positions")
            if positions:
                logger.info(f"Lighter: {[(p['symbol'], p['size']) for p in positions]}")
            
            return positions
            
        except Exception as e:
            logger.error(f"Lighter Positions Error: {e}")
            import traceback
            traceback.print_exc()
            return []

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f" Metadata missing for {symbol}")

        sd = data.get('sd', 8)
        pd = data.get('pd', 6)
        step_size = Decimal(str(data.get('ss', '0.00000001')))
        price_step = Decimal(str(data.get('mps', '0.00001')))
        min_notional = Decimal(str(data.get('min_notional', 10.0)))
        min_quantity = Decimal(str(data.get('min_quantity', 0.0)))

        SAFETY_BUFFER = Decimal('1.05')

        if price_step > 0:
            if side.upper() == 'BUY':
                price = ((price + price_step - Decimal('1e-12')) // price_step) * price_step
            else:
                price = (price // price_step) * price_step

        target_notional = qty * price
        min_required_notional = min_notional * SAFETY_BUFFER

        if target_notional < min_required_notional:
            min_qty_needed = min_required_notional / price
            qty = min_qty_needed * SAFETY_BUFFER

        if step_size > 0:
            qty_in_steps = qty / step_size
            rounded_steps = qty_in_steps.quantize(Decimal('1'), rounding=ROUND_UP)
            qty = rounded_steps * step_size

            if min_quantity > 0 and qty < min_quantity:
                steps_needed = (min_quantity / step_size).quantize(Decimal('1'), rounding=ROUND_UP)
                qty = steps_needed * step_size

        scaled_base = int((qty * (Decimal(10) ** sd)).quantize(Decimal('1'), rounding=ROUND_UP))
        scaled_price = int(price * (Decimal(10) ** pd))

        actual_qty = Decimal(scaled_base) / (Decimal(10) ** sd)
        actual_price = Decimal(scaled_price) / (Decimal(10) ** pd)
        actual_notional = actual_qty * actual_price

        if actual_notional < min_notional * Decimal('0.99'):
            if step_size > 0:
                qty_bumped = actual_qty + step_size
                scaled_base = int((qty_bumped * (Decimal(10) ** sd)).quantize(Decimal('1'), rounding=ROUND_UP))

        if scaled_base == 0:
            raise ValueError(f" {symbol} Fatal: Scaled base is 0!")

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
            price_decimal = Decimal(str(price))
            notional_decimal = Decimal(str(notional_usd))
            slippage = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
            slippage_multiplier = Decimal(1) + (slippage / Decimal(100)) if side == 'BUY' else Decimal(1) - (slippage / Decimal(100))
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
                        if "nonce" in err_str or "429" in err_str or "too many requests" in err_str:
                            if attempt < max_retries:
                                continue
                        logger.error(f" Lighter Order Error: {err}")
                        return False, None

                    tx_hash = getattr(resp, 'tx_hash', 'OK')
                    logger.info(f" Lighter Order: {tx_hash}")
                    return True, str(tx_hash)

                except Exception as inner_e:
                    logger.error(f" Lighter Inner Error: {inner_e}")
                    return False, None

            return False, None

        except Exception as e:
            logger.error(f" Lighter Execution Error: {e}")
            return False, None

    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        close_side = "SELL" if original_side == "BUY" else "BUY"
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                price = self.fetch_mark_price(symbol)
                if not price or price <= 0:
                    return False, None
                
                slippage_pct = [2.0, 5.0, 10.0][min(attempt, 2)]
                slippage = Decimal(str(slippage_pct)) / Decimal(100)
                price_decimal = Decimal(str(price))
                
                if close_side == "BUY":
                    limit_price = price_decimal * (Decimal(1) + slippage)
                else:
                    limit_price = price_decimal * (Decimal(1) - slippage)
                
                logger.info(f" AGGRESSIVE CLOSE {symbol}: Attempt {attempt+1}, Slippage {slippage_pct}%")
                
                qty = Decimal(str(notional_usd)) / limit_price
                base, price_int = self._scale_amounts(symbol, qty, limit_price, close_side)
                
                if base == 0:
                    return False, None
                
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
                    order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
                )
                
                if err:
                    logger.error(f" Order error: {err}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    return False, None
                
                tx_hash = getattr(resp, 'tx_hash', 'OK')
                logger.info(f" Order sent: {tx_hash}")
                
                await asyncio.sleep(2 + attempt)
                updated_positions = await self.fetch_open_positions()
                still_open = any(
                    p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8
                    for p in (updated_positions or [])
                )
                
                if not still_open:
                    logger.info(f" {symbol} VERIFIED CLOSED")
                    return True, str(tx_hash)
                else:
                    if attempt < max_retries - 1:
                        continue
                    else:
                        logger.error(f" {symbol} FAILED after {max_retries} attempts!")
                        return False, str(tx_hash)
            
            except Exception as e:
                logger.error(f" Close exception: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False, None
        
        return False, None

    async def aclose(self):
        """Cleanup all sessions and connections"""
        if self._signer:
            try:
                # Close API client session
                if hasattr(self._signer, 'api_client'):
                    api_client = self._signer.api_client
                    try:
                        if hasattr(api_client, 'close'):
                            maybe = api_client.close()
                            if asyncio.iscoroutine(maybe):
                                await maybe
                            logger.debug("Lighter: API client closed")
                    except Exception:
                        # try closing underlying session if present
                        try:
                            if hasattr(api_client, 'session') and hasattr(api_client.session, 'close'):
                                maybe2 = api_client.session.close()
                                if asyncio.iscoroutine(maybe2):
                                    await maybe2
                                logger.debug("Lighter: Session closed")
                        except Exception:
                            pass

                # Close signer itself if it has close method
                if hasattr(self._signer, 'close'):
                    maybe_sig = self._signer.close()
                    if asyncio.iscoroutine(maybe_sig):
                        await maybe_sig
                    logger.debug("Lighter: Signer closed")

            except Exception as e:
                logger.debug(f"Lighter cleanup warning: {e}")
            finally:
                self._signer = None

        logger.info("✅ Lighter Adapter geschlossen.")

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol"""
        try:
            signer = await self._get_signer()
            order_api = OrderApi(signer.api_client)
            
            market_data = self.market_info.get(symbol)
            if not market_data:
                return False
            
            market_id = market_data.get('i')
            
            # Get open orders
            orders_resp = await order_api.get_orders(market_id=market_id)
            if not orders_resp or not orders_resp.orders:
                return True
            
            # Cancel each order
            for order in orders_resp.orders:
                if getattr(order, 'status', None) in ["PENDING", "OPEN"]:
                    try:
                        await order_api.cancel_order(order.id)
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.debug(f"Cancel order {getattr(order, 'id', 'unknown')}: {e}")
            
            return True
        except Exception as e:
            logger.debug(f"Lighter cancel_all_orders error: {e}")
            return False