# src/adapters/base_adapter.py
import logging
import random
from decimal import Decimal
from typing import Any

import aiohttp

import config
from src.core.interfaces import ExchangeAdapter, OrderResult, Position
from src.utils import safe_decimal

logger = logging.getLogger(__name__)


class BaseAdapter(ExchangeAdapter):
    @property
    def name(self) -> str:
        return self._name

    def __init__(self, name: str):
        self._name = name
        logger.info(f"Initialisiere {self._name} Adapter...")
        # Wird von den konkreten Adaptern überschrieben
        self.rate_limiter = None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get existing session or create a new one if missing/closed."""
        if self._session is None or self._session.closed:
            # Use a slightly larger pool limit if needed, or default
            # TCP Keep-Alive prevents connection drops and improves performance
            connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=60)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _request_with_ratelimit(self, method: str, url: str, **kwargs):
        """Zentrale Methode – alle HTTP-Requests gehen hier durch"""
        if self.rate_limiter is None:
            raise RuntimeError(f"Rate limiter nicht initialisiert für {self.name}")

        await self.rate_limiter.acquire()

        session = await self._get_session()
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 429:
                    self.rate_limiter.penalize_429()
                    resp.raise_for_status()  # wirft weiter
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error(f"{self.name} HTTP {resp.status} {url} → {text}")
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                self.rate_limiter.penalize_429()
            raise

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

    async def fetch_funding_rate(self, symbol: str) -> Decimal:
        if not getattr(config, "LIVE_TRADING", False):
            return safe_decimal(random.uniform(0.0005, 0.002))
        raise NotImplementedError(f"{self.name}.fetch_funding_rate() muss implementiert werden.")

    async def fetch_mark_price(self, symbol: str) -> Decimal:
        if not getattr(config, "LIVE_TRADING", False):
            return safe_decimal(random.uniform(50, 100000))
        raise NotImplementedError(f"{self.name}.fetch_mark_price() muss implementiert werden.")

    def min_notional_usd(self, symbol: str) -> float:
        if not getattr(config, "LIVE_TRADING", False):
            return 10.0
        logger.warning(f"{self.name}: min_notional_usd Fallback → return 20.0")
        return 20.0

    async def fetch_open_positions(self) -> list[Position]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Keine Positionen.")
            return []
        logger.warning(f"{self.name}: fetch_open_positions nicht implementiert.")
        return []

    async def get_open_orders(self, symbol: str) -> list[dict]:
        """
        Fetch open orders for a symbol.
        Returns: [{'id': str, 'price': float, 'side': 'BUY'|'SELL', 'size': float}]
        """
        if not getattr(config, "LIVE_TRADING", False):
            return []
        # Default empty, but should be implemented
        logger.warning(f"{self.name}: get_open_orders nicht implementiert.")
        return []

    async def open_live_position(
        self, symbol: str, side: str, notional_usd: float, reduce_only: bool = False, post_only: bool = False
    ) -> tuple[bool, str | None]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Order {side} {symbol} (${notional_usd}) simuliert.")
            return True, "DRY_RUN_ORDER_123"
        raise NotImplementedError(f"{self.name}.open_live_position() muss implementiert werden.")

    async def close_live_position(
        self, symbol: str, original_side: str, notional_usd: float
    ) -> tuple[bool, str | None]:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"{self.name}: Dry-Run → Close {symbol} simuliert.")
            return True, "DRY_RUN_CLOSE_456"
        logger.warning(f"{self.name}: close_live_position nicht implementiert.")
        return False, None

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: Decimal,
        price: Decimal | None = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError

    async def get_order_status(self, symbol: str, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def get_available_balance(self) -> Decimal:
        return safe_decimal(await self.get_real_available_balance())

    async def fetch_fee_schedule(self) -> tuple[Decimal, Decimal]:
        raise NotImplementedError

    async def aclose(self):
        """Cleanup all resources"""
        if hasattr(self, "_session") and self._session:
            try:
                await self._session.close()
            except Exception:
                logger.exception(f"{self.name}: Fehler beim Schließen der Session")
        logger.info(f"✅ {self.name} Adapter geschlossen.")
