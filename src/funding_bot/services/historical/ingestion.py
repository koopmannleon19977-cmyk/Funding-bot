"""
Historical data ingestion service.

Fetches hourly-level candle data from both exchanges,
normalizes timestamps, and stores in database.

NOTE: Exchanges only provide 1h resolution - minute-level data is not available.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiohttp

from funding_bot.domain.historical import FundingCandle
from funding_bot.observability.logging import get_logger
from funding_bot.ports.store import TradeStorePort

logger = get_logger(__name__)


def _get_x10_symbol(symbol: str) -> str:
    """Convert Lighter symbol format to X10 format."""
    return f"{symbol}-USD"


def _normalize_x10_api_url(base_url: str) -> str:
    """Normalize X10 API base URL to include /api/v1 path."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://api.starknet.extended.exchange/api/v1"
    if "api.starknet.extended.exchange" not in base:
        return "https://api.starknet.extended.exchange/api/v1"
    # Ensure /api/v1 suffix
    if not base.endswith("/api/v1") and (base.endswith("api.starknet.extended.exchange") or "/api/" not in base):
        base = base + "/api/v1"
    return base


def _normalize_lighter_api_url(base_url: str) -> str:
    """Normalize Lighter API base URL."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://mainnet.zklighter.elliot.ai"
    if "zklighter" not in base and "elliot.ai" not in base:
        return "https://mainnet.zklighter.elliot.ai"
    return base


class HistoricalIngestionService:
    """
    Service for ingesting historical minute-level funding data.

    Features:
    - Incremental daily updates
    - Rate limit management
    - Pagination handling
    - Timestamp normalization
    - Common symbol filtering (only fetch symbols available on BOTH exchanges)
    """

    def __init__(
        self,
        store: TradeStorePort,
        settings: object,  # Settings object
        lighter_adapter=None,  # Optional: Lighter adapter for dynamic market ID mapping
    ):
        self.store = store
        self.settings = settings
        self.lighter_adapter = lighter_adapter

        # Rate limiting: X10 allows 1000 req/min
        self.x10_rate_limit = 1000 / 60  # 16.67 req/sec
        self.lighter_rate_limit = 10 / 60  # Conservative for Lighter

        # Extract X10 base URL from settings
        x10_settings = getattr(settings, "x10", None) or {}
        if hasattr(x10_settings, "base_url"):
            x10_url = x10_settings.base_url
        elif isinstance(x10_settings, dict):
            x10_url = x10_settings.get("base_url", "")
        else:
            x10_url = ""
        self.x10_base_url = _normalize_x10_api_url(x10_url)

        # Extract Lighter base URL
        lighter_settings = getattr(settings, "lighter", None) or {}
        if hasattr(lighter_settings, "base_url"):
            lighter_url = lighter_settings.base_url
        elif isinstance(lighter_settings, dict):
            lighter_url = lighter_settings.get("base_url", "https://mainnet.zklighter.elliot.ai")
        else:
            lighter_url = "https://mainnet.zklighter.elliot.ai"

        # Normalize Lighter base URL
        self.lighter_base_url = _normalize_lighter_api_url(lighter_url)

        # Cache for market ID mappings (symbol -> market_id)
        self._market_id_cache: dict[str, int] = {}
        self._markets_fetched = False

        # Load common symbols whitelist (symbols available on BOTH exchanges)
        self._common_symbols_whitelist = self._load_common_symbols_whitelist()

    def _load_common_symbols_whitelist(self) -> set[str] | None:
        """
        Load common symbols whitelist from config file.

        Returns:
            Set of symbols available on BOTH exchanges, or None if config not found
        """
        try:
            config_path = Path("config/common_symbols.json")

            if not config_path.exists():
                logger.debug("Common symbols config not found, will fetch all symbols")
                return None

            import json
            with open(config_path) as f:
                config = json.load(f)

            common_symbols = set(config.get("common_symbols", []))

            if common_symbols:
                logger.info(f"Loaded {len(common_symbols)} common symbols from whitelist")
                return common_symbols
            else:
                logger.warning("Common symbols config is empty")
                return None

        except Exception as e:
            logger.warning(f"Failed to load common symbols whitelist: {e}")
            return None

    def _is_symbol_available_on_both_exchanges(self, symbol: str) -> bool:
        """
        Check if symbol is available on BOTH exchanges (common symbols).

        Args:
            symbol: Symbol to check

        Returns:
            True if symbol is in common symbols whitelist, True if whitelist not loaded
        """
        # If whitelist not loaded, allow all symbols (backward compatibility)
        if self._common_symbols_whitelist is None:
            return True

        return symbol in self._common_symbols_whitelist

    async def backfill(
        self,
        symbols: list[str],
        days_back: int = 90,
    ) -> dict[str, int]:
        """
        Initial backfill of historical data.

        Args:
            symbols: List of symbols to backfill
            days_back: Number of days to fetch

        Returns:
            Dict with counts: {symbol: records_inserted}
        """
        # Filter symbols to only those available on BOTH exchanges
        if self._common_symbols_whitelist is not None:
            original_count = len(symbols)
            symbols = [s for s in symbols if self._is_symbol_available_on_both_exchanges(s)]
            filtered_count = original_count - len(symbols)
            logger.info(
                f"Symbol filtering: {original_count} -> {len(symbols)} "
                f"({filtered_count} symbols not available on both exchanges, skipped)"
            )

        logger.info(f"Starting backfill for {len(symbols)} symbols, {days_back} days")

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=days_back)

        results = {}
        for symbol in symbols:
            try:
                count = await self._backfill_symbol(symbol, start_time, end_time)
                results[symbol] = count
                logger.info(f"Backfilled {symbol}: {count} records")

                # Rate limit delay between symbols
                await asyncio.sleep(1.0)

            except Exception as e:
                logger.error(f"Failed to backfill {symbol}: {e}")
                results[symbol] = 0

        return results

    async def _backfill_symbol(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> int:
        """Backfill data for a single symbol.

        NOTE: Lighter API uses count_back parameter and ignores time range,
        so we fetch Lighter data ONCE (not chunked) to avoid redundant API calls.
        X10 respects time ranges, so we still chunk X10 fetches.
        """
        total_inserted = 0

        # === LIGHTER: Single fetch (API uses count_back, ignores time range) ===
        # Calculate how many hourly candles we need for the full period
        hours_needed = int((end_time - start_time).total_seconds() / 3600)

        try:
            lighter_data = await self._fetch_lighter_candles(
                symbol, start_time, end_time, count_back=min(hours_needed, 2160)  # Max 90 days
            )
            if lighter_data:
                inserted = await self.store.insert_funding_candles(lighter_data)
                total_inserted += inserted
        except Exception as e:
            logger.warning(f"Lighter fetch failed for {symbol}: {e}")

        # === X10: Chunked fetch (API respects time ranges) ===
        # Process in 7-day chunks to avoid memory issues and respect API limits
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + timedelta(days=7), end_time)

            try:
                x10_data = await self._fetch_x10_candles(symbol, chunk_start, chunk_end)
                if x10_data:
                    inserted = await self.store.insert_funding_candles(x10_data)
                    total_inserted += inserted
            except Exception as e:
                logger.warning(f"X10 fetch failed for {symbol} ({chunk_start.date()} - {chunk_end.date()}): {e}")

            chunk_start = chunk_end

        return total_inserted

    async def _fetch_lighter_candles(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        count_back: int = 1000,
    ) -> list[FundingCandle]:
        """
        Fetch hourly funding candles from Lighter using REST API.

        NOTE: Lighter API only supports 1h and 1d resolution - 1m is NOT available.
        Uses Lighter's /api/v1/fundings endpoint (public, no auth required).

        IMPORTANT: Lighter API uses count_back and ignores start_timestamp/end_timestamp.
        Pass appropriate count_back for your time range (e.g., 2160 for 90 days).

        Query params:
        - market_id: int (0-based, not 1-based)
        - resolution: "1h" or "1d"
        - start_timestamp: int (seconds, not milliseconds) - IGNORED by API
        - end_timestamp: int (seconds, not milliseconds) - IGNORED by API
        - count_back: int (number of candles to fetch, API returns this many)
        """
        candles = []

        try:
            # Try to get market_id from Lighter adapter (dynamic mapping for all 121+ markets)
            market_id = None

            if self.lighter_adapter:
                try:
                    # Use adapter's market indices which has all loaded markets
                    market_indices = getattr(self.lighter_adapter, '_market_indices', {})
                    market_id = market_indices.get(symbol)

                    if market_id is None:
                        # Try loading markets if not loaded yet (side effect: populates _market_indices)
                        _ = await self.lighter_adapter.load_markets()
                        market_indices = getattr(self.lighter_adapter, '_market_indices', {})
                        market_id = market_indices.get(symbol)

                except Exception as e:
                    logger.debug(f"Failed to get market ID from adapter for {symbol}: {e}")

            # Fallback to hardcoded mapping for common markets if adapter not available
            if market_id is None:
                market_id_map = {
                    "ETH": 0,
                    "BTC": 1,
                    "SOL": 2,
                    "DOGE": 3,
                    "PEPE": 4,
                }
                market_id = market_id_map.get(symbol)

            if market_id is None:
                logger.debug(f"No market ID mapping for {symbol}, skipping Lighter data")
                return []

            # Use REST API directly for funding history
            # Note: Lighter API uses count_back and ignores time range parameters
            async with aiohttp.ClientSession() as session:
                url = f"{self.lighter_base_url}/api/v1/fundings"
                params = {
                    "market_id": market_id,
                    "resolution": "1h",  # Note: 1m resolution not available
                    "start_timestamp": int(start_time.timestamp()),  # Passed but ignored by API
                    "end_timestamp": int(end_time.timestamp()),      # Passed but ignored by API
                    "count_back": count_back,  # This is what actually controls how many candles are returned
                }

                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Parse fundings array
                        fundings = data.get("fundings", [])

                        for funding_item in fundings:
                            # Parse timestamp (seconds from API)
                            ts_val = funding_item.get("timestamp", 0)
                            if isinstance(ts_val, str):
                                ts_val = int(ts_val)

                            # Convert to datetime (timestamp is in seconds)
                            timestamp = datetime.fromtimestamp(ts_val, tz=UTC)

                            # Parse funding rate (comes as string like "0.0463")
                            # Rate is HOURLY (API returns DECIMAL format, e.g., -0.000386 = -0.0386%)
                            # NO division by 100 needed - API already returns decimal format
                            # Configurable interval for backwards compatibility (default: 1 = hourly, no division)
                            rate_val = funding_item.get("rate", "0")
                            rate_raw = Decimal(str(rate_val))

                            # Normalize to hourly (divide by interval if > 1)
                            # Uses settings.lighter.funding_rate_interval_hours (default: 1)
                            interval_hours = Decimal("1")  # Default: hourly
                            lighter_settings = getattr(self.settings, "lighter", None)
                            if lighter_settings and hasattr(lighter_settings, "funding_rate_interval_hours"):
                                interval_val = lighter_settings.funding_rate_interval_hours
                                # Ensure it's a Decimal, not a mock or None
                                if interval_val is not None and not callable(interval_val):
                                    try:
                                        interval_hours = Decimal(str(interval_val))
                                    except (ValueError, TypeError):
                                        interval_hours = Decimal("1")

                            # Lighter returns an unsigned rate plus a direction field indicating who pays.
                            # Normalize to the "long funding rate" convention used throughout the bot:
                            # - direction="short" => shorts pay longs => long funding is NEGATIVE
                            # - otherwise => longs pay shorts => long funding is POSITIVE
                            direction = str(funding_item.get("direction", "") or "").lower()
                            if direction == "short":
                                rate_raw = -rate_raw

                            # Convert to hourly rate (API already returns DECIMAL format, not percent)
                            rate_hourly = rate_raw / interval_hours

                            # Convert to APY (hourly -> annual)
                            funding_apy = rate_hourly * 24 * 365

                            candles.append(FundingCandle(
                                timestamp=timestamp,
                                symbol=symbol,
                                exchange="LIGHTER",
                                mark_price=None,
                                index_price=None,
                                spread_bps=None,
                                funding_rate_hourly=rate_hourly,
                                funding_apy=funding_apy,
                                fetched_at=datetime.now(UTC),
                            ))

                        logger.info(f"Fetched {len(fundings)} hourly funding records from Lighter for {symbol}")
                    else:
                        logger.warning(f"Lighter fundings API returned status {resp.status} for {symbol}")
                        text = await resp.text()
                        logger.debug(f"Response: {text}")

        except Exception as e:
            logger.error(f"Error fetching Lighter candles for {symbol}: {e}")

        return candles

    async def _get_lighter_market_id(self, api, symbol: str) -> int | None:
        """
        Get market_id for a symbol from Lighter.

        Fetches the markets list from Lighter API and caches the mapping.
        NOTE: api parameter is unused (kept for compatibility), uses REST API directly.
        """
        try:
            # Check cache first
            if symbol in self._market_id_cache:
                return self._market_id_cache[symbol]

            # Fetch markets if not already fetched
            if not self._markets_fetched:
                await self._fetch_lighter_markets(api)

            # Try cache again after fetching
            return self._market_id_cache.get(symbol)

        except Exception as e:
            logger.error(f"Error getting Lighter market ID for {symbol}: {e}")
            return None

    async def _fetch_lighter_markets(self, api) -> None:
        """
        Fetch all markets from Lighter and build symbol -> market_id mapping.

        Uses REST API directly since SDK may not have a markets endpoint.
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Lighter REST API for markets
                url = f"{self.lighter_base_url}/api/v1/perpetual/markets"

                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Parse markets list - adjust based on actual API response structure
                        markets = data.get("data", data.get("markets", []))

                        for market in markets:
                            # Handle different possible response structures
                            market_id = market.get("id") or market.get("marketId")
                            symbol_name = (
                                market.get("symbol") or
                                market.get("name") or
                                market.get("pair", "")
                            )

                            # Normalize symbol (remove -USD, /USD, etc.)
                            symbol_name = symbol_name.replace("-USD", "").replace("/USD", "").upper()

                            if market_id and symbol_name:
                                self._market_id_cache[symbol_name] = int(market_id)

                        self._markets_fetched = True
                        logger.info(f"Fetched {len(self._market_id_cache)} Lighter markets")

                        # Log some examples for debugging
                        examples = list(self._market_id_cache.items())[:5]
                        logger.debug(f"Market ID mapping examples: {examples}")

                    else:
                        logger.warning(f"Lighter markets API returned status {resp.status}")

        except Exception as e:
            logger.error(f"Error fetching Lighter markets: {e}")

            # Fallback: try to use common market IDs based on known Lighter markets
            # This is a temporary fallback if the API call fails
            self._market_id_cache.update({
                "ETH": 1,
                "BTC": 2,
                "SOL": 3,
                "DOGE": 4,
                "PEPE": 5,
            })
            logger.warning("Using fallback market ID mapping")

    async def _fetch_x10_candles(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[FundingCandle]:
        """
        Fetch funding rate history from X10 REST API.

        Uses GET /info/<market>/funding?startTime=<start>&endTime=<end>
        Ref: https://api.docs.extended.exchange/#get-funding-rates-history
        """
        candles = []
        x10_symbol = _get_x10_symbol(symbol)

        # Process in 1-day chunks
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + timedelta(days=1), end_time)

            try:
                async with aiohttp.ClientSession() as session:
                    # X10 funding history endpoint
                    url = f"{self.x10_base_url}/info/{x10_symbol}/funding"
                    params = {
                        "startTime": int(chunk_start.timestamp() * 1000),
                        "endTime": int(chunk_end.timestamp() * 1000),
                    }

                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()

                            # Parse funding rates
                            funding_rates = data.get("data", [])

                            for rate_data in funding_rates:
                                # Parse timestamp (milliseconds)
                                ts_val = rate_data.get("timestamp", rate_data.get("T", 0))
                                if isinstance(ts_val, str):
                                    ts_val = int(ts_val)

                                if ts_val > 1e12:  # Milliseconds
                                    timestamp = datetime.fromtimestamp(ts_val / 1000, tz=UTC)
                                else:  # Seconds
                                    timestamp = datetime.fromtimestamp(ts_val, tz=UTC)

                                # Parse funding rate (hourly)
                                # X10 API returns rates in DECIMAL format (e.g., -0.000360)
                                # NO division by 100 - API already returns decimal format
                                rate_val = rate_data.get("funding_rate", rate_data.get("f", 0))
                                rate_hourly = Decimal(str(rate_val))  # Already in decimal format

                                # Convert to APY (hourly -> annual)
                                funding_apy = rate_hourly * 24 * 365

                                candles.append(FundingCandle(
                                    timestamp=timestamp,
                                    symbol=symbol,
                                    exchange="X10",
                                    mark_price=None,
                                    index_price=None,
                                    spread_bps=None,
                                    funding_rate_hourly=rate_hourly,
                                    funding_apy=funding_apy,
                                    fetched_at=datetime.now(UTC),
                                ))
                        else:
                            # Handle 400 errors gracefully (symbol not available on X10)
                            if resp.status == 400:
                                # Market not found = Symbol not available on X10
                                logger.debug(f"Symbol {symbol} not available on X10 (400 Market not found)")
                                break  # Stop trying this symbol
                            else:
                                logger.warning(f"X10 returned {resp.status} for {symbol}")

            except Exception as e:
                logger.error(f"Error fetching X10 candles for {symbol}: {e}")
                break

            chunk_start = chunk_end

            # Rate limit: 60ms between requests
            await asyncio.sleep(0.06)

        return candles

    async def _fetch_x10_candle_type(
        self,
        session: aiohttp.ClientSession,
        x10_symbol: str,
        candle_type: str,  # "mark-prices" or "index-prices"
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """Fetch candles of a specific type from X10.

        NOTE: This method is DEPRECATED - X10 uses /info/<market>/funding endpoint instead.
        Kept for potential future use if candle data becomes available.
        """
        return []

    async def daily_update(self, symbols: list[str]) -> dict[str, int]:
        """
        Incremental daily update (fetches last 2 days to fill gaps).

        Called once per day via scheduler.
        """
        # Filter symbols to only those available on BOTH exchanges
        if self._common_symbols_whitelist is not None:
            original_count = len(symbols)
            symbols = [s for s in symbols if self._is_symbol_available_on_both_exchanges(s)]
            if len(symbols) < original_count:
                logger.info(f"Daily update: Filtered {original_count - len(symbols)} symbols not on both exchanges")

        logger.info(f"Running daily update for {len(symbols)} symbols")

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=2)

        results = {}
        for symbol in symbols:
            try:
                count = await self._backfill_symbol(symbol, start_time, end_time)
                results[symbol] = count
            except Exception as e:
                logger.error(f"Failed to update {symbol}: {e}")
                results[symbol] = 0

        return results

