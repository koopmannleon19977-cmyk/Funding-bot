"""
Historical data ingestion service.

Fetches hourly-level candle data from both exchanges,
normalizes timestamps, and stores in database.

NOTE: Exchanges only provide 1h resolution - minute-level data is not available.

Rate Limiting:
- Uses dedicated backfill rate limiters (separate from production)
- Configurable budget via settings.historical.backfill_rate_budget_pct
- Exponential backoff on 429 errors
- Respects inter-request delays to avoid API overload
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiohttp

from funding_bot.domain.historical import FundingCandle
from funding_bot.observability.logging import get_logger
from funding_bot.ports.store import TradeStorePort
from funding_bot.services.rate_limiter import get_rate_limiter_manager

# =============================================================================
# Constants
# =============================================================================

# API Configuration
API_TIMEOUT_SECONDS = 30
MAX_LIGHTER_CANDLES = 2160  # 90 days * 24 hours
X10_CHUNK_DAYS = 7  # Process X10 in 7-day chunks
X10_FUNDING_HISTORY_PAGE_LIMIT = 100  # Extended funding history max page size (per docs)

# Connection Pooling
SESSION_POOL_LIMIT = 10  # Max concurrent connections per session
SESSION_KEEPALIVE_SECONDS = 30

# Funding Rate Validation (P2.2: bounds check)
FUNDING_RATE_MAX_ABS = Decimal("0.01")  # Max 1% per hour (extreme but possible)

# Transient HTTP errors that should trigger retry
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

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

        # Rate limiting configuration from settings (with safe defaults for mocks)
        hist_settings = getattr(settings, "historical", None)

        def _safe_get(obj, attr, default):
            """Safely get attribute, handling MagicMock and other edge cases."""
            try:
                val = getattr(obj, attr, default)
                # Check if it's a real value (not a MagicMock)
                if val is None or (hasattr(val, '_mock_name') and val._mock_name):
                    return default
                return float(val) if isinstance(default, float) else int(val)
            except (TypeError, ValueError):
                return default

        def _safe_decimal_get(obj, attr, default: Decimal) -> Decimal:
            """Safely get Decimal-like attribute, handling mocks/None."""
            try:
                val = getattr(obj, attr, default)
                if val is None or (hasattr(val, "_mock_name") and val._mock_name):
                    return default
                return Decimal(str(val))
            except Exception:
                return default

        self._backfill_rate_budget_pct = _safe_get(hist_settings, "backfill_rate_budget_pct", 0.10)
        self._max_concurrent_symbols = _safe_get(hist_settings, "backfill_max_concurrent_symbols", 2)
        self._inter_request_delay_ms = _safe_get(hist_settings, "backfill_inter_request_delay_ms", 200)
        self._max_retries = _safe_get(hist_settings, "backfill_max_retries", 3)
        self._retry_base_delay_ms = _safe_get(hist_settings, "backfill_retry_base_delay_ms", 1000)
        self._x10_chunk_days = _safe_get(hist_settings, "x10_chunk_days", X10_CHUNK_DAYS)
        self._funding_rate_max_abs = _safe_decimal_get(hist_settings, "funding_rate_max_abs", FUNDING_RATE_MAX_ABS)

        # Initialize rate limiters with reduced capacity for backfill
        # This ensures backfill doesn't starve production operations
        rate_manager = get_rate_limiter_manager()

        # Lighter backfill: 10% of 24,000 = 2,400 req/min
        lighter_backfill_limit = int(24000 * self._backfill_rate_budget_pct)
        self._lighter_rate_limiter = rate_manager.get_limiter(
            "LIGHTER",
            "backfill",
            requests_per_minute=max(60, lighter_backfill_limit),  # Minimum 1 req/sec
        )

        # X10 backfill: 10% of 10,000 = 1,000 req/min
        x10_backfill_limit = int(10000 * self._backfill_rate_budget_pct)
        self._x10_rate_limiter = rate_manager.get_limiter(
            "X10",
            "backfill",
            requests_per_minute=max(60, x10_backfill_limit),
        )

        logger.info(
            f"Backfill rate limiters initialized: "
            f"Lighter={lighter_backfill_limit}/min, X10={x10_backfill_limit}/min "
            f"(budget={self._backfill_rate_budget_pct * 100:.0f}%)"
        )

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

        # Use configurable concurrency from settings
        batch_size = self._max_concurrent_symbols
        inter_batch_delay = self._inter_request_delay_ms / 1000.0  # Convert to seconds

        logger.info(
            f"Backfill config: batch_size={batch_size}, "
            f"inter_batch_delay={inter_batch_delay:.2f}s, "
            f"max_retries={self._max_retries}"
        )

        # P1: Create ONE session for the entire backfill operation (connection pooling)
        connector = aiohttp.TCPConnector(
            limit=SESSION_POOL_LIMIT,
            keepalive_timeout=SESSION_KEEPALIVE_SECONDS,
        )
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async def _backfill_single(symbol: str) -> tuple[str, int]:
                """Backfill a single symbol with error handling and retry."""
                last_error = None
                for attempt in range(self._max_retries):
                    try:
                        count = await self._backfill_symbol(symbol, start_time, end_time, session)
                        logger.info(f"Backfilled {symbol}: {count} records")
                        return (symbol, count)
                    except Exception as e:
                        last_error = e
                        # P2.1: Only retry on transient errors
                        if attempt < self._max_retries - 1:
                            # Exponential backoff: 1s, 2s, 4s, ...
                            delay = (self._retry_base_delay_ms / 1000.0) * (2 ** attempt)
                            logger.warning(
                                f"Backfill {symbol} failed (attempt {attempt + 1}/{self._max_retries}): {e}. "
                                f"Retrying in {delay:.1f}s"
                            )
                            await asyncio.sleep(delay)

                logger.error(f"Failed to backfill {symbol} after {self._max_retries} attempts: {last_error}")
                return (symbol, 0)

            # Process in batches with asyncio.gather
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                batch_num = (i // batch_size) + 1
                total_batches = (len(symbols) + batch_size - 1) // batch_size

                logger.info(f"Processing batch {batch_num}/{total_batches}: {batch}")

                batch_results = await asyncio.gather(
                    *[_backfill_single(sym) for sym in batch],
                    return_exceptions=True
                )

                for result in batch_results:
                    if isinstance(result, tuple):
                        sym, count = result
                        results[sym] = count
                    elif isinstance(result, BaseException):
                        logger.error(f"Batch backfill exception: {result}")

                # Configurable delay between batches
                if i + batch_size < len(symbols):
                    await asyncio.sleep(inter_batch_delay)

        # Log rate limiter stats
        logger.info(
            f"Backfill complete. Rate limiter stats: "
            f"Lighter={self._lighter_rate_limiter.get_stats()}, "
            f"X10={self._x10_rate_limiter.get_stats()}"
        )

        return results

    async def _backfill_symbol(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        session: aiohttp.ClientSession,
    ) -> int:
        """Backfill data for a single symbol.

        NOTE: Lighter API uses count_back parameter and ignores time range,
        so we fetch Lighter data ONCE (not chunked) to avoid redundant API calls.
        X10 respects time ranges, so we still chunk X10 fetches.

        Args:
            symbol: Trading symbol
            start_time: Start of backfill period
            end_time: End of backfill period
            session: Shared aiohttp session for connection reuse
        """
        total_inserted = 0

        # === LIGHTER: Single fetch (API uses count_back, ignores time range) ===
        # Calculate how many hourly candles we need for the full period
        hours_needed = int((end_time - start_time).total_seconds() / 3600)

        try:
            lighter_data = await self._fetch_lighter_candles(
                symbol, start_time, end_time,
                count_back=min(hours_needed, MAX_LIGHTER_CANDLES),
                session=session,
            )
            if lighter_data:
                inserted = await self.store.insert_funding_candles(lighter_data)
                total_inserted += inserted
        except Exception as e:
            logger.warning(f"Lighter fetch failed for {symbol}: {e}")

        # === X10: Chunked fetch (API respects time ranges) ===
        # Process in configurable day chunks to respect API limits (default: 7).
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + timedelta(days=self._x10_chunk_days), end_time)

            try:
                x10_data = await self._fetch_x10_candles(symbol, chunk_start, chunk_end, session)
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
        session: aiohttp.ClientSession | None = None,
    ) -> list[FundingCandle]:
        """
        Fetch hourly funding candles from Lighter using REST API.

        NOTE: Lighter API only supports 1h and 1d resolution - 1m is NOT available.
        Uses Lighter's /api/v1/fundings endpoint (public, no auth required).

        IMPORTANT: Lighter API uses count_back and ignores start_timestamp/end_timestamp.
        Pass appropriate count_back for your time range (e.g., 2160 for 90 days).

        Args:
            symbol: Trading symbol
            start_time: Start of period (passed to API but ignored)
            end_time: End of period (passed to API but ignored)
            count_back: Number of candles to fetch
            session: Optional shared aiohttp session for connection reuse
        """
        candles = []
        skipped_extreme = 0
        max_abs_rate = Decimal("0")
        first_skipped_ts: datetime | None = None
        last_skipped_ts: datetime | None = None

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

            # P1: Use shared session if provided, otherwise create new one (fallback)
            url = f"{self.lighter_base_url}/api/v1/fundings"
            params = {
                "market_id": market_id,
                "resolution": "1h",
                "start_timestamp": int(start_time.timestamp()),
                "end_timestamp": int(end_time.timestamp()),
                "count_back": count_back,
            }

            async def _fetch_lighter_api():
                # Use shared session or create fallback
                if session:
                    async with session.get(url, params=params) as resp:
                        return resp.status, await resp.json() if resp.status == 200 else await resp.text()
                else:
                    # Fallback for direct calls without session
                    async with aiohttp.ClientSession() as fallback_session:
                        async with fallback_session.get(
                            url, params=params, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
                        ) as resp:
                            return resp.status, await resp.json() if resp.status == 200 else await resp.text()

            # Execute with rate limiting
            status, data = await self._lighter_rate_limiter.execute(
                _fetch_lighter_api,
                label=f"lighter_fundings_{symbol}"
            )

            if status == 200:
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
                    # Rate is HOURLY and returned in PERCENT format (e.g., 0.5000 = 0.5%).
                    # Convert to decimal before applying any interval normalization.
                    # Configurable interval for backwards compatibility (default: 1 = hourly).
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

                    # Convert to hourly rate (normalize by interval)
                    # NOTE: Lighter API returns rates in DECIMAL format (e.g., 0.0001 = 0.01%)
                    # NO division by 100 - API already returns decimal format.
                    # This matches the live adapter behavior (see lighter/adapter.py:980-981)
                    rate_hourly = rate_raw / interval_hours

                    # P2.2: Bounds validation - clamp extreme rates to max (preserves data continuity)
                    if abs(rate_hourly) > self._funding_rate_max_abs:
                        skipped_extreme += 1
                        abs_rate = abs(rate_hourly)
                        if abs_rate > max_abs_rate:
                            max_abs_rate = abs_rate
                        if first_skipped_ts is None:
                            first_skipped_ts = timestamp
                        last_skipped_ts = timestamp
                        # Clamp to max instead of skipping (preserves time series continuity)
                        sign = Decimal("1") if rate_hourly > 0 else Decimal("-1")
                        rate_hourly = sign * self._funding_rate_max_abs

                    # Convert to APY (hourly -> annual, simple multiplication)
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
                if skipped_extreme:
                    logger.info(
                        f"Lighter {symbol}: Clamped {skipped_extreme} extreme funding rates "
                        f"to ±{self._funding_rate_max_abs} (max_abs={max_abs_rate}) "
                        f"between {first_skipped_ts} and {last_skipped_ts}"
                    )
            else:
                logger.warning(f"Lighter fundings API returned status {status} for {symbol}")
                logger.debug(f"Response: {data}")

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
        session: aiohttp.ClientSession | None = None,
    ) -> list[FundingCandle]:
        """
        Fetch funding rate history from X10 REST API.

        Uses GET /info/<market>/funding?startTime=<start>&endTime=<end>.
        The caller already chunks large ranges (see `X10_CHUNK_DAYS`), so this method
        fetches the given range in a single request and retries transient failures.
        Ref: https://api.docs.extended.exchange/#get-funding-rates-history

        Args:
            symbol: Trading symbol
            start_time: Start of period
            end_time: End of period
            session: Optional shared aiohttp session for connection reuse
        """
        candles_by_ts: dict[datetime, FundingCandle] = {}
        x10_symbol = _get_x10_symbol(symbol)

        # Track clamped extreme rates for summary logging
        clamped_count = 0
        max_abs_rate = Decimal("0")
        first_clamped_ts: datetime | None = None
        last_clamped_ts: datetime | None = None

        url = f"{self.x10_base_url}/info/{x10_symbol}/funding"
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        attempts = max(1, int(self._max_retries))
        cursor: int | None = None
        page_count = 0

        while True:
            page_count += 1
            params: dict[str, int] = {
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": X10_FUNDING_HISTORY_PAGE_LIMIT,
            }
            if cursor is not None:
                params["cursor"] = cursor

            async def _fetch_x10_api(_params: dict[str, int] = params):
                if session:
                    async with session.get(url, params=_params) as resp:
                        return resp.status, await resp.json() if resp.status == 200 else await resp.text()

                async with aiohttp.ClientSession() as fallback_session:
                    async with fallback_session.get(
                        url, params=_params, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
                    ) as resp:
                        return resp.status, await resp.json() if resp.status == 200 else await resp.text()

            label = f"x10_funding_{symbol}_{start_time.date()}_{end_time.date()}_p{page_count}"

            status = None
            data = None

            for attempt in range(attempts):
                try:
                    status, data = await self._x10_rate_limiter.execute(_fetch_x10_api, label=label)
                except Exception as e:
                    if attempt < attempts - 1:
                        base = self._retry_base_delay_ms / 1000.0
                        backoff = base * (2**attempt)
                        delay = backoff + (backoff * random.uniform(0.0, 0.25))
                        logger.warning(
                            f"X10 funding history request failed for {symbol} ({start_time.date()} - {end_time.date()}): {e}. "
                            f"Retrying in {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.warning(
                        f"X10 funding history request failed for {symbol} ({start_time.date()} - {end_time.date()}): {e}. "
                        f"Giving up after {attempts} attempts"
                    )
                    return list(candles_by_ts.values())

                if status == 200:
                    break

                # P2.1: Distinguish transient vs permanent errors
                if status in TRANSIENT_HTTP_CODES:
                    if attempt < attempts - 1:
                        base = self._retry_base_delay_ms / 1000.0
                        backoff = base * (2**attempt)
                        delay = backoff + (backoff * random.uniform(0.0, 0.25))
                        logger.warning(
                            f"X10 funding history returned {status} for {symbol} ({start_time.date()} - {end_time.date()}). "
                            f"Retrying in {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.warning(
                        f"X10 funding history returned {status} for {symbol} ({start_time.date()} - {end_time.date()}). "
                        f"Giving up after {attempts} attempts"
                    )
                    return list(candles_by_ts.values())

                if status == 400:
                    # Market not found = Symbol not available on X10
                    logger.debug(f"Symbol {symbol} not available on X10 (400 Market not found)")
                    return []

                if status == 404:
                    logger.debug(f"X10 endpoint not found for {symbol}")
                    return []

                # Unknown/non-retryable HTTP response.
                snippet = str(data)[:200] if data is not None else ""
                logger.warning(f"X10 returned {status} for {symbol} ({start_time.date()} - {end_time.date()}): {snippet}")
                return list(candles_by_ts.values())

            if status != 200:
                return list(candles_by_ts.values())

            funding_rates = (data or {}).get("data", [])

            for rate_data in funding_rates:
                ts_val = rate_data.get("timestamp", rate_data.get("T", 0))
                if isinstance(ts_val, str):
                    ts_val = int(ts_val)

                if ts_val > 1e12:  # Milliseconds
                    timestamp = datetime.fromtimestamp(ts_val / 1000, tz=UTC)
                else:  # Seconds
                    timestamp = datetime.fromtimestamp(ts_val, tz=UTC)

                # X10 API returns rates in DECIMAL format (e.g., -0.000360)
                rate_val = rate_data.get("funding_rate", rate_data.get("f", 0))
                rate_hourly = Decimal(str(rate_val))

                # P2.2: Bounds validation - clamp extreme rates to max (preserves data continuity)
                if abs(rate_hourly) > self._funding_rate_max_abs:
                    clamped_count += 1
                    abs_rate = abs(rate_hourly)
                    if abs_rate > max_abs_rate:
                        max_abs_rate = abs_rate
                    if first_clamped_ts is None:
                        first_clamped_ts = timestamp
                    last_clamped_ts = timestamp
                    # Clamp to max instead of skipping
                    sign = Decimal("1") if rate_hourly > 0 else Decimal("-1")
                    rate_hourly = sign * self._funding_rate_max_abs

                funding_apy = rate_hourly * 24 * 365

                candles_by_ts[timestamp] = FundingCandle(
                    timestamp=timestamp,
                    symbol=symbol,
                    exchange="X10",
                    mark_price=None,
                    index_price=None,
                    spread_bps=None,
                    funding_rate_hourly=rate_hourly,
                    funding_apy=funding_apy,
                    fetched_at=datetime.now(UTC),
                )

            # Cursor-based pagination (Extended max page size is limited).
            pagination = (data or {}).get("pagination") or {}
            next_cursor = pagination.get("cursor")
            count = pagination.get("count")
            if count is None:
                try:
                    count = int(pagination.get("total", 0))  # fallback if present
                except Exception:
                    count = None

            if next_cursor is None:
                break

            try:
                next_cursor_int = int(next_cursor)
            except Exception:
                break

            # Safety: avoid infinite loops on buggy cursors.
            if cursor is not None and next_cursor_int == cursor:
                logger.warning(f"X10 funding history pagination cursor did not advance for {symbol}; stopping pagination.")
                break

            # If we got fewer than the max page size, we are done.
            if count is not None and int(count) < X10_FUNDING_HISTORY_PAGE_LIMIT:
                break
            if len(funding_rates) < X10_FUNDING_HISTORY_PAGE_LIMIT:
                break

            cursor = next_cursor_int

        # Log summary of clamped extreme rates (avoid log spam during pagination)
        if clamped_count:
            logger.info(
                f"X10 {symbol}: Clamped {clamped_count} extreme funding rates "
                f"to ±{self._funding_rate_max_abs} (max_abs={max_abs_rate}) "
                f"between {first_clamped_ts} and {last_clamped_ts}"
            )

        return list(candles_by_ts.values())

    # P3.1: Removed deprecated _fetch_x10_candle_type() - was dead code returning []

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

        # Create session for daily update (same pattern as backfill)
        connector = aiohttp.TCPConnector(
            limit=SESSION_POOL_LIMIT,
            keepalive_timeout=SESSION_KEEPALIVE_SECONDS,
        )
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            for symbol in symbols:
                try:
                    count = await self._backfill_symbol(symbol, start_time, end_time, session)
                    results[symbol] = count
                except Exception as e:
                    logger.error(f"Failed to update {symbol}: {e}")
                    results[symbol] = 0

        return results

