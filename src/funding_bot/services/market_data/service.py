"""
Market Data Service.

Unified source of truth for:
- Mark prices
- Funding rates
- Orderbook L1
- Market metadata

Handles staleness detection and REST fallback for WebSocket data.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from decimal import Decimal

from funding_bot.config.settings import Settings
from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.services.market_data.accessors import (
    get_common_symbols,
    get_fee_schedule,
    get_funding,
    get_lighter_symbol,
    get_market_info,
    get_orderbook,
    get_price,
    get_x10_symbol,
    is_healthy,
)
from funding_bot.services.market_data.fresh import (
    get_fresh_funding,
    get_fresh_orderbook,
    get_fresh_orderbook_depth,
    get_fresh_price,
)
from funding_bot.services.market_data.hydrate import _hydrate_symbol_from_adapter_cache
from funding_bot.services.market_data.models import (
    FundingSnapshot,
    OrderbookDepthSnapshot,
    OrderbookSnapshot,
    PriceSnapshot,
)
from funding_bot.services.market_data.refresh import (
    _batch_refresh_adapters,
    _discover_common_symbols,
    _refresh_loop,
    _refresh_symbol,
    _refresh_symbols_parallel,
    refresh_all,
)
from funding_bot.services.market_data.streams import (
    _run_lighter_mark_price_trickle,
    _schedule_lighter_mark_price_trickle,
    _start_market_data_streams,
)

logger = get_logger(__name__)


class MarketDataService:
    """
    Unified market data service.

    Features:
    - Parallel fetching from both exchanges
    - Staleness detection
    - Caching with TTL
    - Common symbol resolution
    """

    def __init__(
        self,
        settings: Settings,
        lighter: ExchangePort,
        x10: ExchangePort,
    ):
        self.settings = settings
        self.lighter = lighter
        self.x10 = x10

        # Caches
        self._prices: dict[str, PriceSnapshot] = {}
        self._funding: dict[str, FundingSnapshot] = {}
        self._orderbooks: dict[str, OrderbookSnapshot] = {}
        self._orderbook_depth: dict[str, OrderbookDepthSnapshot] = {}
        # Fee cache (loaded once at startup - fees don't change during runtime)
        self._fee_cache: dict[str, dict[str, Decimal]] = {}

        # Common symbols (intersection) - uses Lighter format (e.g., 'ETH')
        self._common_symbols: set[str] = set()
        # Symbol mapping: lighter_symbol -> x10_symbol (e.g., 'ETH' -> 'ETH-USD')
        self._symbol_map_to_x10: dict[str, str] = {}

        # Health tracking
        self._lighter_healthy = True
        self._x10_healthy = True
        self._last_refresh: datetime | None = None  # legacy alias for last successful refresh
        self._last_successful_refresh: datetime | None = None
        self._last_refresh_attempt: datetime | None = None
        self._last_refresh_started: datetime | None = None
        self._refresh_in_progress: bool = False
        self._last_refresh_duration_seconds: float | None = None
        self._last_refresh_error: str | None = None
        self._last_lighter_success: datetime | None = None
        self._last_x10_success: datetime | None = None
        self._last_reported_health: bool | None = None

        # Background refresh task
        self._refresh_task: asyncio.Task | None = None
        self._running = False

        # Round-robin index for slowly refreshing real Lighter prices/orderbook (Premium-friendly)
        self._lighter_price_rr_offset: int = 0
        self._lighter_trickle_task: asyncio.Task | None = None
        self._last_batch_refresh_at: datetime | None = None

    async def start(self) -> None:
        """Start the market data service."""
        if self._running:
            return

        logger.info("Starting MarketDataService...")
        self._running = True

        # Discover common symbols
        await self._discover_common_symbols()

        # Load fee schedules ONCE at startup (fees don't change during runtime)
        await self._load_fee_schedules()

        # Start public market-data streams (best-effort)
        await self._start_market_data_streams()

        # Start background refresh
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="market_data_refresh")

        logger.info(f"MarketDataService started with {len(self._common_symbols)} symbols")

    async def stop(self) -> None:
        """Stop the market data service."""
        self._running = False

        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None

        if self._lighter_trickle_task and not self._lighter_trickle_task.done():
            self._lighter_trickle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._lighter_trickle_task
        self._lighter_trickle_task = None

        logger.info("MarketDataService stopped")

    async def _load_fee_schedules(self) -> None:
        """
        Load fee schedules ONCE at startup.

        Fees don't change during runtime, so we fetch them once and cache.
        This eliminates 2 REST API calls per symbol per scan.
        """
        if not self._common_symbols:
            return

        logger.info(f"Loading fee schedules for {len(self._common_symbols)} symbols...")

        # Use a single symbol to get fees (fees are account-wide, not per-symbol)
        # Just pick the first symbol to query
        sample_symbol = next(iter(self._common_symbols))

        try:
            fees = await self.lighter.get_fee_schedule(sample_symbol)
            lighter_maker = fees.get("maker_fee", Decimal("0"))
            lighter_taker = fees.get("taker_fee", Decimal("0"))

            fees = await self.x10.get_fee_schedule(self.get_x10_symbol(sample_symbol))
            x10_maker = fees.get("maker_fee", Decimal("0"))
            x10_taker = fees.get("taker_fee", Decimal("0"))

            # Cache the fee schedule (same for all symbols)
            fee_schedule = {
                "lighter_maker": lighter_maker,
                "lighter_taker": lighter_taker,
                "x10_maker": x10_maker,
                "x10_taker": x10_taker,
            }

            # Store for all symbols (fees are account-wide)
            for symbol in self._common_symbols:
                self._fee_cache[symbol] = fee_schedule

            logger.info(
                f"Fee schedules cached: Lighter Maker={lighter_maker:.4%}, Taker={lighter_taker:.4%} | "
                f"X10 Maker={x10_maker:.4%}, Taker={x10_taker:.4%}"
            )
        except Exception as e:
            logger.warning(f"Failed to load fee schedules, will use configured defaults: {e}")
            # Fallback to configured defaults
            fee_schedule = {
                "lighter_maker": self.settings.trading.maker_fee_lighter,
                "lighter_taker": self.settings.trading.taker_fee_lighter,
                "x10_maker": self.settings.trading.maker_fee_x10,
                "x10_taker": self.settings.trading.taker_fee_x10,
            }

            for symbol in self._common_symbols:
                self._fee_cache[symbol] = fee_schedule

    _discover_common_symbols = _discover_common_symbols
    _start_market_data_streams = _start_market_data_streams
    _refresh_loop = _refresh_loop
    refresh_all = refresh_all
    _batch_refresh_adapters = _batch_refresh_adapters
    _refresh_symbol = _refresh_symbol
    _refresh_symbols_parallel = _refresh_symbols_parallel
    _schedule_lighter_mark_price_trickle = _schedule_lighter_mark_price_trickle
    _run_lighter_mark_price_trickle = _run_lighter_mark_price_trickle
    _hydrate_symbol_from_adapter_cache = _hydrate_symbol_from_adapter_cache

    get_common_symbols = get_common_symbols
    get_price = get_price
    get_funding = get_funding
    get_orderbook = get_orderbook
    get_fresh_price = get_fresh_price
    get_fresh_funding = get_fresh_funding
    get_fresh_orderbook = get_fresh_orderbook
    get_fresh_orderbook_depth = get_fresh_orderbook_depth
    is_healthy = is_healthy
    get_market_info = get_market_info
    get_x10_symbol = get_x10_symbol
    get_lighter_symbol = get_lighter_symbol
    get_fee_schedule = get_fee_schedule
