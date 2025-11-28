# src/adapters/base_adapter.py
import logging
import config
import random
import asyncio
from typing import Optional, List, Tuple
import aiohttp

logger = logging.getLogger(__name__)

class BaseAdapter:
    def __init__(self, name: str):
        # Do not overwrite if a subclass already provided a `name` attribute
        if not hasattr(self, "name") or self.name is None:
            self.name = name
        logger.info(f"Initialisiere {self.name} Adapter...")
        # Wird von den konkreten Adaptern überschrieben
        self.rate_limiter = None  # Global session for ALL HTTP
        # Shared session for connection pooling (Fix für "Unclosed client session")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazy initialization of shared session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300),
            )
        return self._session
    async def _request_with_ratelimit(
        self, method: str, url: str, retries: int = 3, **kwargs
    ):
        """Zentrale Methode – alle HTTP-Requests gehen hier durch"""
        if self.rate_limiter is None:
            raise RuntimeError(f"Rate limiter nicht initialisiert für {self.name}")

        await self.rate_limiter.acquire()

        for attempt in range(retries):
            try:
                session = await self._get_session()
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status == 429:
                        self.rate_limiter.penalize_429()
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.error(f"{self.name} HTTP {resp.status} {url} → {text}")
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status, message=text
                        )
                    resp.raise_for_status()
                    try:
                        return await resp.json()
                    except aiohttp.ContentTypeError:
                        return await resp.text()
            except aiohttp.ClientError as e:
                logger.warning(f"{self.name} Request failed (attempt {attempt+1}): {e}")
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))

    async def load_market_cache(self, force: bool = False):
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Market-Cache simuliert.")
            return
        raise NotImplementedError(f"{self.name}.load_market_cache() muss implementiert werden.")

    async def load_funding_rates_and_prices(self):
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Funding-Rates simuliert.")
            return
        raise NotImplementedError(f"{self.name}.load_funding_rates_and_prices() muss implementiert werden.")

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        if not getattr(config, "LIVE_TRADING", False):
            return random.uniform(0.0005, 0.002)
        raise NotImplementedError(f"{self.name}.fetch_funding_rate() muss implementiert werden.")

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        if not getattr(config, "LIVE_TRADING", False):
            return random.uniform(50, 100000)
        raise NotImplementedError(f"{self.name}.fetch_mark_price() muss implementiert werden.")
    
    def min_notional_usd(self, symbol: str) -> float:
        if not getattr(config, "LIVE_TRADING", False):
            return 10.0
        logger.warning(f"{self.name}: min_notional_usd Fallback → return 20.0")
        return 20.0

    async def fetch_open_positions(self) -> Optional[List[dict]]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Keine Positionen.")
            return []
        logger.warning(f"{self.name}: fetch_open_positions nicht implementiert.")
        return []

    async def open_live_position(
        self, 
        symbol: str, 
        side: str, 
        notional_usd: float,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> Tuple[bool, Optional[str]]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Order {side} {symbol} (${notional_usd}) simuliert.")
            return True, "DRY_RUN_ORDER_123"
        raise NotImplementedError(f"{self.name}.open_live_position() muss implementiert werden.")

    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Close {symbol} simuliert.")
            return True, "DRY_RUN_CLOSE_456"
        logger.warning(f"{self.name}: close_live_position nicht implementiert.")
        return False, None

    async def get_order_fee(self, order_id: str) -> float:
        if not getattr(config, "LIVE_TRADING", False):
            return 0.0005
        raise NotImplementedError(f"{self.name}.get_order_fee() muss implementiert werden.")

    async def get_real_available_balance(self) -> float:
        if not getattr(config, "LIVE_TRADING", False):
            return 10000.0
        raise NotImplementedError(f"{self.name}.get_real_available_balance() muss implementiert werden.")

    async def aclose(self):
        """Cleanup all resources"""
        if hasattr(self, "_session") and self._session and not self._session.closed:
            try:
                await asyncio.wait_for(self._session.close(), timeout=5.0)
            except asyncio.TimeoutError:
                # Force close if timeout
                try:
                    if getattr(self._session, "_connector", None):
                        self._session._connector.close()
                except Exception:
                    logger.exception(f"{self.name}: Fehler beim forcierten Schließen der Session (Timeout)")
            except Exception as e:
                logger.exception(f"{self.name}: Fehler beim Schließen der Session: {e}")
        logger.info(f"✅ {self.name} Adapter geschlossen.")