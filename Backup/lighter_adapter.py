# src/adapters/lighter_adapter.py – RATE LIMIT & NONCE FIX
import asyncio
import aiohttp  # <--- NEU
import json     # <--- NEU
import logging
import time
import random  # WICHTIG für Nonce
from typing import Dict, Tuple, Optional, List
from decimal import Decimal, ROUND_UP

import config
from lighter.api.order_api import OrderApi
from lighter.api.funding_api import FundingApi
from lighter.signer_client import SignerClient
from lighter.api.account_api import AccountApi
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

MARKET_OVERRIDES = {
    "ASTER-USD": {'ss': Decimal('1'), 'sd': 8},
    "HYPE-USD":  {'ss': Decimal('0.01'), 'sd': 6},
    "MEGA-USD":  {'ss': Decimal('99999999')}
}

class LighterAdapter(BaseAdapter):
    async def get_order_fee(self, order_id: str) -> float:
        """
        Gibt die Fee zurück.
        Quelle: Lighter Docs (Standard Account = 0% Maker/Taker).
        Wir nehmen an, wir sind im Standard-Tier (Default).
        """
        return 0.0000  # 0% Gebühren!
    def __init__(self):
        super().__init__("Lighter")
        self.market_info: Dict[str, dict] = {}
        self.funding_cache: Dict[str, float] = {}
        self.price_cache: Dict[str, float] = {}
        self._signer: Optional[SignerClient] = None
        self._resolved_account_index: Optional[int] = None
        self._resolved_api_key_index: Optional[int] = None
        # Semaphore erhöht auf 10, aber mit Batch-Logik
        self.semaphore = asyncio.Semaphore(10) 
        self._last_market_cache_at: Optional[float] = None
        # NEU: Balance Cache Variablen
        self._balance_cache = 0.0
        self._last_balance_update = 0.0

    # --- WEBSOCKET IMPLEMENTATION (NEU) ---
    async def start_websocket(self):
        """Hört auf Lighter Trades via Stream"""
        # 🟢 NEU: /stream statt /ws
        ws_url = "wss://mainnet.zklighter.elliot.ai/stream" 
        
        while True:
            try:
                if not self.market_info:
                     await self.load_market_cache(force=True)

                session = aiohttp.ClientSession()
                async with session.ws_connect(ws_url) as ws:
                    logger.info("🔌 Lighter WebSocket verbunden.")
                    
                    # Lighter nutzt oft IDs, aber manche Streams nutzen Strings.
                    # Wir bleiben bei IDs, da das SDK das so macht.
                    market_ids = [m['i'] for m in self.market_info.values()]
                    
                    # Subscribe Command
                    sub_msg = {
                        "method": "subscribe",
                        "params": ["trade", market_ids],
                        "id": 1
                    }
                    await ws.send_json(sub_msg)
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            
                            # Lighter Stream Antwort
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
                                    # WENN CACHE UPDATE -> EVENT FEUERN
                                    if updated and hasattr(self, 'price_update_event'):
                                        self.price_update_event.set()
                                            
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
            except Exception as e:
                logger.error(f"Lighter WS Error: {e} - Reconnect in 5s")
                await asyncio.sleep(5)
            finally:
                if 'session' in locals(): await session.close()

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
            try:
                details = await order_api.order_book_details(market_id=market_id)
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
            except Exception as e:
                # Stummes Handling für Rate Limits
                if "429" in str(e):
                    # Kein Log-Spam hier, nur kurzer Sleep
                    await asyncio.sleep(1.0)
                else:
                    logger.debug(f"Lighter Fetch Error ID {market_id}: {e}")

    async def load_market_cache(self, force: bool = False):
        if not getattr(config, "LIVE_TRADING", False): return

        if not force and self._last_market_cache_at and (time.time() - self._last_market_cache_at < 60):
            return

        logger.info("Lighter: Aktualisiere Märkte...")
        signer = await self._get_signer()
        order_api = OrderApi(signer.api_client)
        try:
            market_list = await order_api.order_books()
            if not market_list or not market_list.order_books: return

            all_ids = [m.market_id for m in market_list.order_books if hasattr(m, 'market_id')]
            
            # OPTIMIERTE BATCH LOGIK MIT PAUSEN
            BATCH_SIZE = 10
            SLEEP_BETWEEN_BATCHES = 0.25
            
            for i in range(0, len(all_ids), BATCH_SIZE):
                batch_ids = all_ids[i:i + BATCH_SIZE]
                tasks = [self._fetch_single_market(order_api, mid) for mid in batch_ids]
                await asyncio.gather(*tasks)
                await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
                
            self._last_market_cache_at = time.time()
            logger.info(f"Lighter: {len(self.market_info)} Märkte geladen.")
        except Exception as e:
            logger.error(f"Lighter Cache Error: {e}")

    async def load_funding_rates_and_prices(self):
        if not getattr(config, "LIVE_TRADING", False): return
        signer = await self._get_signer()
        funding_api = FundingApi(signer.api_client)
        try:
            fd_response = await funding_api.funding_rates()
            if fd_response and fd_response.funding_rates:
                rates_by_id = {fr.market_id: fr.rate for fr in fd_response.funding_rates}
                for symbol, data in self.market_info.items():
                    if rate := rates_by_id.get(data['i']):
                        self.funding_cache[symbol] = float(rate)
        except Exception as e:
            logger.error(f"Lighter Funding Fetch Error: {e}")

    # Standard Methoden
    def fetch_24h_vol(self, symbol: str) -> float: return 0.0
    def min_notional_usd(self, symbol: str) -> float: return self.market_info.get(symbol, {}).get('min_notional', 1.0)
    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Lighter liefert 8-Stunden-Rates. Wir normalisieren auf Stunden-Rate.
        Quelle: https://apidocs.lighter.xyz/reference/funding-rates
        """
        rate_8h = self.funding_cache.get(symbol)
        if rate_8h is None:
            return None
        # Normalisiere auf stündliche Rate
        return float(rate_8h) / 8.0
    def fetch_mark_price(self, symbol: str) -> Optional[float]: return self.price_cache.get(symbol)
    
    async def get_real_available_balance(self) -> float:
        # FIX: Cache Duration auf 60 Sekunden erhöhen!
        # Lighter mag keine Frequent-Poller auf der Balance-Route.
        if time.time() - self._last_balance_update < 60.0:
            # Wenn Cache 0 ist, aber wir wissen, dass wir leben, versuche einmalig Retry nach 5s
            if self._balance_cache == 0 and getattr(config, "LIVE_TRADING", False):
                pass # Logic Flow-through erlauben falls kritisch 0
            else:
                return self._balance_cache

        try:
            signer = await self._get_signer()
            await asyncio.sleep(0.5) # Längerer Sleep vor Request
            
            # Retry Logik für Balance
            for _ in range(2):
                try:
                    response = await AccountApi(signer.api_client).account(by="index", value=str(self._resolved_account_index))
                    val = 0.0
                    if response and response.accounts and response.accounts[0]:
                        acc = response.accounts[0]
                        buying = getattr(acc, 'buying_power', None) or getattr(acc, 'total_asset_value', '0')
                        val = float(buying or 0)
                    
                    self._balance_cache = val
                    self._last_balance_update = time.time()
                    return val
                except Exception as e:
                    if "429" in str(e):
                        await asyncio.sleep(2)
                        continue
                    raise e
            
            return self._balance_cache

        except Exception as e:
            if "429" in str(e):
                logger.warning(f"⚠️ Lighter Balance 429 (Rate Limit). Cache: ${self._balance_cache:.2f}")
            else:
                logger.error(f"❌ Lighter Balance Error: {e}")
            return self._balance_cache

    async def fetch_open_positions(self) -> List[dict]:
        if not getattr(config, "LIVE_TRADING", False): return []
        try:
            signer = await self._get_signer()
            await asyncio.sleep(0.2) # Safety Pause
            response = await AccountApi(signer.api_client).account(by="index", value=str(self._resolved_account_index))
            positions = []
            if response and response.accounts and response.accounts[0].positions:
                for p in response.accounts[0].positions:
                    multiplier = 1 if int(p.sign) == 0 else -1
                    size = float(p.position or 0.0) * multiplier
                    if abs(size) > 1e-8:
                        symbol = f"{p.symbol}-USD" if not p.symbol.endswith("-USD") else p.symbol
                        positions.append({'symbol': symbol, 'size': size})
            return positions
        except: return []

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        data = self.market_info.get(symbol)
        if not data: raise ValueError(f"Metadata missing for {symbol}")
        
        sd, pd = data.get('sd', 8), data.get('pd', 6)
        step_size = Decimal(str(data.get('ss', '0.00000001')))
        price_step = Decimal(str(data.get('mps', '0.00001')))
        
        if step_size > 0: qty = ((qty + step_size - Decimal('1e-12')) // step_size) * step_size
        if price_step > 0:
            if side.upper() == 'BUY': price = ((price + price_step - Decimal('1e-12')) // price_step) * price_step
            else: price = (price // price_step) * price_step
            
        return int(qty * (Decimal(10) ** sd)), int(price * (Decimal(10) ** pd))

    async def open_live_position(self, symbol: str, side: str, notional_usd: float, reduce_only: bool = False, post_only: bool = False) -> tuple[bool, str | None]:
        if not getattr(config, "LIVE_TRADING", False): return True, None
        price = self.fetch_mark_price(symbol)
        if not price or price <= 0: return False, None

        try:
            market_id = self.market_info[symbol]['i']
            slippage = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
            limit_price = Decimal(str(price)) * (Decimal(1) + slippage/100 if side == 'BUY' else Decimal(1) - slippage/100)
            qty = Decimal(str(notional_usd)) / limit_price

            base, price_int = self._scale_amounts(symbol, qty, limit_price, side)
            if base == 0: return False

            signer = await self._get_signer()

            # === RETRY LOGIK (Best Practice aus beiden Welten) ===
            # Versucht es bis zu 3 Mal, falls Nonce oder Rate Limit Fehler kommen
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    # Backoff: Kurz warten bei Retry (0s, 0.5s, 1.0s)
                    if attempt > 0: await asyncio.sleep(0.5 * attempt)

                    # Nonce mit Jitter
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
                        # Bei diesen Fehlern lohnt sich ein Retry
                        if "nonce" in err_str or "429" in err_str or "too many requests" in err_str:
                            logger.warning(f"⚠️ Lighter Retry ({attempt+1}/{max_retries+1}): {err}")
                            if attempt < max_retries: continue # Nächster Versuch

                        # Bei anderen Fehlern (z.B. Insufficient Funds) brechen wir ab
                        logger.error(f"Lighter Order Error: {err}")
                        return False, None

                    tx_hash = getattr(resp, 'tx_hash', 'OK')
                    logger.info(f"Lighter Order Sent: {tx_hash}")
                    return True, str(tx_hash)

                except Exception as inner_e:
                    logger.error(f"Lighter Inner Error: {inner_e}")
                    return False, None

            return False, None # Wenn alle Versuche fehlschlagen

        except Exception as e:
            logger.error(f"Lighter Execution Error: {e}")
            return False, None

    async def close_live_position(self, symbol: str, original_side: str, notional_usd: float) -> bool:
        close_side = 'SELL' if original_side == 'BUY' else 'BUY'
        return await self.open_live_position(symbol, close_side, notional_usd, reduce_only=True)

    async def aclose(self):
        if self._signer:
            try:
                if hasattr(self._signer, 'api_client') and hasattr(self._signer.api_client, 'close'):
                    await self._signer.api_client.close()
                elif hasattr(self._signer, 'close'):
                    await self._signer.close()
            except: pass