"""
Lighter Protocol Exchange Adapter.

Implements ExchangePort using the official Lighter Python SDK.
Handles all SDK type conversions to domain types.

SDK: https://github.com/elliottech/lighter-python
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import aiohttp

from funding_bot.adapters.exchanges.lighter.orderbook import LocalOrderbook
from funding_bot.config.settings import Settings
from funding_bot.domain.errors import (
    ExchangeError,
    InsufficientBalanceError,
    OrderRejectedError,
)
from funding_bot.domain.models import (
    Balance,
    Exchange,
    FundingRate,
    MarketInfo,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
)
from funding_bot.observability.logging import get_logger
from funding_bot.ports.exchange import ExchangePort
from funding_bot.utils.decimals import (
    LIGHTER_FUNDING_RATE_CAP,
    clamp_funding_rate,
)
from funding_bot.utils.decimals import (
    safe_decimal as _safe_decimal,
)

logger = get_logger(__name__)


def _clamp_avg_fill_price(avg_fill_price: Decimal, ref_price: Decimal) -> Decimal:
    """
    Lighter WS/SDK fields for "filled_quote_amount" can be non-notional (or differently scaled).
    Avoid poisoning PnL/slippage logic with an implausible avg fill price by clamping to ref_price.
    """
    if avg_fill_price <= 0 or ref_price <= 0:
        return avg_fill_price
    # Very wide sanity bounds; real fills should never be off by 10x vs the order price.
    if avg_fill_price < (ref_price / Decimal("10")) or avg_fill_price > (ref_price * Decimal("10")):
        return ref_price
    return avg_fill_price


class _WebSocketCircuitBreaker:
    """
    Circuit breaker pattern for WebSocket connections.

    Prevents connection storms by stopping reconnection attempts after repeated failures.
    Automatically recovers after a cooldown period.
    """

    def __init__(self, threshold: int = 10, cooldown_seconds: float = 60.0):
        self.failure_count = 0
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self.last_failure_time = 0.0
        self.circuit_open = False

    def record_failure(self) -> bool:
        """
        Record a connection failure.

        Returns True if the circuit should open (too many failures).
        """
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.threshold:
            self.circuit_open = True
            logger.warning(
                f"WebSocket circuit breaker OPEN after {self.failure_count} failures. Cooldown: {self.cooldown}s"
            )
            return True
        return False

    def record_success(self) -> None:
        """Reset circuit on successful connection."""
        if self.circuit_open:
            logger.info("WebSocket circuit breaker RESET - connection recovered")
        self.failure_count = 0
        self.circuit_open = False

    def should_attempt_connection(self) -> bool:
        """
        Check if a connection attempt should be made.

        Returns False if circuit is open and cooldown hasn't elapsed.
        """
        if not self.circuit_open:
            return True
        # Check if cooldown period has passed
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed >= self.cooldown:
            logger.info("WebSocket circuit breaker COOLDOWN elapsed - allowing reconnection")
            self.circuit_open = False
            self.failure_count = 0
            return True
        return False


class _WebSocketHealthMonitor:
    """
    Monitor WebSocket connection health.

    Tracks message rate and errors to detect unhealthy connections.
    """

    def __init__(self, message_threshold: int = 5, error_threshold: int = 5):
        self.connection_start = time.monotonic()
        self.last_message_time = time.monotonic()
        self.message_count = 0
        self.error_count = 0
        self.message_threshold = message_threshold
        self.error_threshold = error_threshold

    def record_message(self) -> None:
        """Record that a message was received."""
        self.last_message_time = time.monotonic()
        self.message_count += 1

    def record_error(self) -> None:
        """Record a connection error."""
        self.error_count += 1

    def is_healthy(self) -> bool:
        """
        Check if WebSocket connection is healthy.

        Returns False if:
        - No recent messages (last 30s)
        - Too many errors
        - Connection too new (grace period of 5s)
        """
        age = time.monotonic() - self.connection_start
        if age < 5:  # Grace period
            return True
        time_since_last = time.monotonic() - self.last_message_time
        has_recent_messages = time_since_last < 30
        low_error_rate = self.error_count < self.error_threshold
        healthy = has_recent_messages and low_error_rate
        if not healthy:
            logger.debug(
                f"WebSocket health check: age={age:.1f}s, "
                f"time_since_last={time_since_last:.1f}s, "
                f"errors={self.error_count}, healthy={healthy}"
            )
        return healthy


def _get_ws_reconnect_delay(
    attempt: int,
    base_delay: float = 2.0,
    max_delay: float = 120.0,
    jitter_factor: float = 0.15,
) -> float:
    """
    Calculate WebSocket reconnect delay with exponential backoff and jitter.

    Jitter prevents connection storms when multiple connections fail simultaneously.

    Args:
        attempt: Current reconnection attempt number (0-indexed)
        base_delay: Base delay in seconds
        max_delay: Maximum delay cap in seconds
        jitter_factor: Random jitter as fraction of delay (default 15%)

    Returns:
        Delay in seconds before next reconnection attempt
    """
    delay = min(max_delay, base_delay * (2**attempt))
    # Add ±jitter_factor random jitter to prevent thundering herd
    jitter = delay * jitter_factor * (random.random() * 2 - 1)
    return max(0, delay + jitter)


class LighterAdapter(ExchangePort):
    """
    Lighter Protocol exchange adapter.

    Uses the official SDK for signing and order submission.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._connected = False

        # API Configuration
        self._base_url = settings.lighter.base_url or "https://mainnet.zklighter.elliot.ai"
        self._private_key = (
            settings.lighter.private_key
            or os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", "")
            or os.getenv("LIGHTER_PRIVATE_KEY", "")
        )
        self._account_index = settings.lighter.account_index  # Can be 0 (valid) or None (disabled)
        self._api_key_index = settings.lighter.api_key_index
        self._l1_address = settings.lighter.l1_address or os.getenv("LIGHTER_L1_ADDRESS", "")

        # SDK clients (lazy init)
        self._client: Any = None
        self._signer: Any = None  # SignerClient for authenticated requests
        self._auth_token: str | None = None  # Auth token for premium rate limits
        self._auth_token_created_at: float = 0.0  # Timestamp when token was created (for proactive refresh)
        self._api_client: Any = None  # Lighter SDK API client
        self._order_api: Any = None  # Lighter SDK Order API
        self._funding_api: Any = None  # Lighter SDK Funding API
        self._account_api: Any = None  # Lighter SDK Account API

        # Fees cache
        self._maker_fee: Decimal | None = None
        self._taker_fee: Decimal | None = None
        self._user_tier: str = "standard"

        # WebSocket
        self._ws_task: asyncio.Task | None = None
        self._order_callbacks: list[Callable[[Order], Awaitable[None]]] = []
        self._position_callbacks: list[Callable[[Position], Awaitable[None]]] = []
        self._ws_client_cls: Any = None
        # WS readiness: set once we have successfully received at least one account stream message.
        # Used as a safety gate for WS-driven fill waiting (mirrors `.tmp`'s WS-first execution model).
        self._ws_account_ready = asyncio.Event()
        self._ws_account_ready_logged = False
        # Dedicated account_orders WS (auth + per-market subscriptions) for fast fill detection.
        # `.tmp` tools rely on `update/account_orders` callbacks; `account_all` alone is often too slow.
        self._orders_ws_task: asyncio.Task | None = None
        self._orders_ws_ready = asyncio.Event()
        self._orders_ws_market_ready: dict[int, asyncio.Event] = {}
        self._orders_ws_subscribe_queue: asyncio.Queue[int] = asyncio.Queue()
        self._orders_ws_markets: set[int] = set()

        # Trading WebSocket for order submission (faster than HTTP)
        # Per SDK docs: jsonapi/sendtx over WS is ~50-100ms faster than HTTP /api/v1/sendTx
        self._trading_ws: Any = None  # websockets.WebSocketClientProtocol
        self._trading_ws_lock = asyncio.Lock()
        self._trading_ws_connected = asyncio.Event()
        self._trading_ws_task: asyncio.Task | None = None
        self._trading_ws_response_futures: dict[str, asyncio.Future] = {}
        self._use_ws_orders: bool = bool(
            getattr(getattr(settings, "websocket", None), "lighter_ws_order_submission_enabled", True)
        )
        self._trading_ws_connect_timeout_seconds: float = float(
            getattr(getattr(settings, "websocket", None), "lighter_ws_order_submission_connect_timeout_seconds", 2.0)
            or 2.0
        )
        self._trading_ws_response_timeout_seconds: float = float(
            getattr(getattr(settings, "websocket", None), "lighter_ws_order_submission_response_timeout_seconds", 2.0)
            or 2.0
        )

        # Caches
        self._markets: dict[str, MarketInfo] = {}
        self._market_indices: dict[str, int] = {}  # symbol -> market_id
        self._market_id_to_symbol: dict[int, str] = {}
        self._markets_lock = asyncio.Lock()
        self._markets_loaded_at: datetime | None = None  # Track when markets were last loaded
        self._price_cache: dict[str, Decimal] = {}
        self._funding_cache: dict[str, FundingRate] = {}
        self._orderbook_cache: dict[str, dict[str, Decimal]] = {}
        # order_id -> (fee_usd, filled_qty_at_calc)
        self._order_fee_cache: dict[str, tuple[Decimal, Decimal]] = {}
        self._price_updated_at: dict[str, datetime] = {}
        self._orderbook_updated_at: dict[str, datetime] = {}
        # client_order_id (client_order_index) -> system order_id/order_index
        # This is critical for resolving `pending_{client_index}_{market}` placeholders without extra REST calls
        # and for fast WS-driven fill detection (mirrors the client-id correlation approach in `.tmp` tools).
        self._client_order_id_map: dict[str, str] = {}

        # WS public orderbook subscriptions (market_ids)
        self._ws_order_book_ids: list[int] = []
        # WS orderbook continuity tracking (see `.tmp/lighter_ws.html`):
        # - Validate begin_nonce matches previous nonce (per market).
        # - If we detect a gap, mark cached quotes stale and reconnect to resubscribe for a fresh snapshot.
        self._ws_orderbook_last_nonce: dict[int, int] = {}
        self._ws_orderbook_last_offset: dict[int, int] = {}
        # Per-symbol public orderbook WS (like `.tmp`): subscribe only when needed.
        self._ob_ws_lock = asyncio.Lock()
        self._ob_ws_tasks: dict[int, asyncio.Task] = {}
        self._ob_ws_ready: dict[int, asyncio.Event] = {}
        self._ob_ws_books: dict[int, LocalOrderbook] = {}
        self._ob_ws_last_nonce: dict[int, int] = {}
        self._ob_ws_last_offset: dict[int, int] = {}
        self._ob_ws_snapshot_loaded: set[int] = set()
        self._ob_ws_last_used: dict[int, float] = {}
        self._ob_ws_max_connections: int = int(
            getattr(getattr(settings, "websocket", None), "lighter_orderbook_ws_max_connections", 20) or 20
        )
        self._ob_ws_ttl_seconds: float = float(
            getattr(getattr(settings, "websocket", None), "lighter_orderbook_ws_ttl_seconds", 120.0) or 120.0
        )

        # Rate limiting with exponential backoff
        self._request_lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._rate_limit_backoff = 1.0  # 🚀 PERFORMANCE: Start with 1s backoff for faster rate-limit recovery (was 0.0)
        self._rate_limit_until = 0.0  # Timestamp until which we're rate-limited

        # 🚀 PERFORMANCE: WebSocket Fill Cache (instant fill detection)
        # Maps client_order_id -> (Order, timestamp) for immediate get_order() lookups.
        # Populated by WS updates, avoiding slow REST API searches (2-5s timeout).
        # Cleaned up when order is terminal (filled/cancelled) or TTL expires.
        self._ws_fill_cache: dict[str, tuple[Order, float]] = {}
        self._ws_fill_cache_lock = asyncio.Lock()  # Thread-safe cache ops
        self._ws_fill_cache_ttl_seconds: float = 300.0  # 5 minutes TTL for stale entries

        # 🔧 WebSocket Stability: Circuit breaker and health monitoring
        cb_threshold = int(getattr(getattr(settings, "websocket", None), "circuit_breaker_threshold", 10) or 10)
        cb_cooldown = float(
            getattr(getattr(settings, "websocket", None), "circuit_breaker_cooldown_seconds", 60.0) or 60.0
        )
        health_msg_threshold = int(
            getattr(getattr(settings, "websocket", None), "health_check_message_threshold", 5) or 5
        )
        health_error_threshold = int(
            getattr(getattr(settings, "websocket", None), "health_check_error_threshold", 5) or 5
        )

        self._ob_ws_circuit_breaker: dict[int, _WebSocketCircuitBreaker] = {}
        self._ob_ws_health_monitor: dict[int, _WebSocketHealthMonitor] = {}

        # Factory functions to create per-market circuit breaker and health monitor
        def _create_circuit_breaker() -> _WebSocketCircuitBreaker:
            return _WebSocketCircuitBreaker(threshold=cb_threshold, cooldown_seconds=cb_cooldown)

        def _create_health_monitor() -> _WebSocketHealthMonitor:
            return _WebSocketHealthMonitor(
                message_threshold=health_msg_threshold, error_threshold=health_error_threshold
            )

        self._ob_ws_create_circuit_breaker = _create_circuit_breaker
        self._ob_ws_create_health_monitor = _create_health_monitor

    @property
    def exchange(self) -> Exchange:
        return Exchange.LIGHTER

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def initialize(self) -> None:
        """Initialize the adapter."""
        if self._connected:
            return

        logger.info("Initializing Lighter adapter...")

        # Create HTTP session
        # 🚀 PERFORMANCE PREMIUM-OPTIMIZED: TCPConnector with aggressive connection pooling
        # Premium (24,000 req/min) allows 400x more concurrency than Standard (60 req/min)
        connector = aiohttp.TCPConnector(
            limit=200,  # Total connections (increased from 100 for Premium)
            limit_per_host=100,  # Max 100 concurrent connections to Lighter API (was 30)
            ttl_dns_cache=1800,  # Cache DNS results for 30 minutes (was 5 minutes)
            use_dns_cache=True,  # Enable DNS caching to avoid repeated lookups
            keepalive_timeout=300,  # Keep connections alive for 5 minutes (was 30s)
            enable_cleanup_closed=True,  # Auto-cleanup closed connections
            force_close=False,  # Enable keep-alive for connection reuse
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(
                total=30,  # Overall request timeout
                connect=10,  # Connection establishment timeout (TLS handshake)
                sock_read=5,  # Socket read timeout
            ),
            headers={"Content-Type": "application/json"},
        )

        # 🚀 PERFORMANCE: Warm up HTTP session to avoid first-request TLS handshake latency
        # This eliminates 100-200ms delay on the first actual API call
        await self._warmup_http_session()

        try:
            # Initialize SDK components
            await self._init_sdk()

            # Detect Account Tier & Fees
            await self._detect_tier_and_fees()

            # Load markets
            await self.load_markets()

            # Pre-warm trading WS to avoid first-order handshake latency.
            if self._use_ws_orders:
                with contextlib.suppress(Exception):
                    ready = await self.ensure_trading_ws(timeout=self._trading_ws_connect_timeout_seconds)
                    if not ready:
                        logger.debug("Lighter trading WS pre-warm timed out")

            self._connected = True
            logger.info(f"Lighter adapter initialized with {len(self._markets)} markets")

        except Exception as e:
            logger.exception(f"Failed to initialize Lighter adapter: {e}")
            await self.close()
            raise

    async def _init_sdk(self) -> None:
        """Initialize Lighter SDK components."""
        try:
            # Import SDK components
            from lighter import AccountApi, ApiClient, Configuration, FundingApi, OrderApi

            # Create API client with optional auth token
            config = Configuration(host=self._base_url)

            # If we have credentials, create SignerClient for auth token
            # NOTE: account_index=0 is valid (explicit first account), only None means disabled
            if self._private_key and self._account_index is not None:
                try:
                    from lighter import SignerClient

                    self._signer = SignerClient(
                        url=self._base_url,
                        api_private_keys={self._api_key_index: self._private_key},
                        account_index=self._account_index,
                    )
                    # Create auth token (8 hour expiry max per docs)
                    # Use -60s timestamp to prevent "future timestamp" errors if local clock is fast
                    now = int(time.time())
                    timestamp = now - 60
                    auth_token, error = self._signer.create_auth_token_with_expiry(28800, timestamp=timestamp)
                    if error:
                        logger.warning(f"Failed to create auth token: {error}")
                    else:
                        self._auth_token = auth_token
                        self._auth_token_created_at = time.time()  # Track creation time for proactive refresh
                        # Add auth token to API client headers
                        config.api_key = {"apiKey": auth_token}
                        logger.info(
                            f"Lighter authenticated as account {self._account_index} "
                            "(tier/rate limits determined via account_limits)"
                        )
                except Exception as e:
                    logger.warning(f"Failed to init SignerClient: {e}. Using unauthenticated mode.")
            else:
                logger.info("Lighter running in unauthenticated mode (Standard rate limits: 60 req/min)")

            self._api_client = ApiClient(config)
            self._order_api = OrderApi(self._api_client)
            self._funding_api = FundingApi(self._api_client)
            self._account_api = AccountApi(self._api_client)

            try:
                # Use our local WS client wrapper so we can keep order_book metadata (nonce/begin_nonce/offset)
                # hot for safe continuity validation (see `.tmp/lighter_ws.html`).
                from .ws_client import WsClient

                self._ws_client_cls = WsClient
            except ImportError:
                logger.warning("Lighter WS Client not available")

            logger.debug("Lighter SDK initialized")

        except ImportError:
            logger.warning("Lighter SDK not installed. Using REST-only mode. Install with: pip install lighter-python")
            self._api_client = None
            self._order_api = None
            self._funding_api = None
            self._account_api = None
        except Exception as e:
            logger.warning(f"Lighter SDK init failed, using REST: {e}")
            self._api_client = None
            self._order_api = None
            self._funding_api = None
            self._account_api = None

    async def _refresh_auth_token(self) -> bool:
        """Refresh Lighter auth token."""
        if not self._signer or self._account_index is None:
            return False

        try:
            logger.info("Refreshing Lighter auth token...")
            # 8h expiry max (per Lighter docs), backdated 60s for clock skew
            now = int(time.time())
            timestamp = now - 60
            auth_token, error = self._signer.create_auth_token_with_expiry(28800, timestamp=timestamp)

            if error:
                logger.warning(f"Failed to refresh auth token: {error}")
                return False

            self._auth_token = auth_token
            self._auth_token_created_at = time.time()  # Track refresh time for proactive refresh

            # Update API client config
            if self._api_client:
                self._api_client.configuration.api_key = {"apiKey": auth_token}

            logger.info("Lighter auth token refreshed successfully")
            return True

        except Exception as e:
            logger.warning(f"Error refreshing auth token: {e}")
            return False

    async def _maybe_refresh_token_proactively(self) -> bool:
        """
        Proactively refresh auth token if approaching expiry.

        Refreshes token after 7 hours (25200s) to avoid 401 errors at 8h expiry.
        This is called periodically from the market data refresh loop.

        Returns:
            True if token was refreshed, False if not needed or failed.
        """
        if not self._auth_token or not self._auth_token_created_at:
            return False

        token_age = time.time() - self._auth_token_created_at
        # Refresh if token is older than 7 hours (25200 seconds)
        # Token expires at 8 hours (28800 seconds), so we have 1 hour buffer
        if token_age > 25200:
            logger.info(f"Token age {token_age/3600:.1f}h > 7h, proactively refreshing...")
            return await self._refresh_auth_token()

        return False

    async def close(self) -> None:
        """Close connections."""
        self._connected = False

        # Stop per-symbol orderbook streams
        for task in list(self._ob_ws_tasks.values()):
            with contextlib.suppress(Exception):
                task.cancel()
        for task in list(self._ob_ws_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._ob_ws_tasks.clear()
        self._ob_ws_ready.clear()
        self._ob_ws_books.clear()
        self._ob_ws_last_nonce.clear()
        self._ob_ws_last_offset.clear()
        self._ob_ws_snapshot_loaded.clear()
        self._ob_ws_last_used.clear()

        if self._session:
            await self._session.close()
            self._session = None

        if self._signer:
            with contextlib.suppress(Exception):
                await self._signer.close()
            self._signer = None

        if self._api_client:
            with contextlib.suppress(Exception):
                await self._api_client.close()
            self._api_client = None

        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None

        if self._orders_ws_task:
            self._orders_ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._orders_ws_task
            self._orders_ws_task = None
            self._orders_ws_ready.clear()
            self._clear_orders_ws_market_ready()

        if self._trading_ws_task:
            self._trading_ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._trading_ws_task
            self._trading_ws_task = None

        self._trading_ws_connected.clear()
        with contextlib.suppress(Exception):
            if self._trading_ws is not None:
                await self._trading_ws.close()
        self._trading_ws = None
        # Fail and clear any pending WS request futures (best-effort).
        for fut in list(self._trading_ws_response_futures.values()):
            if not fut.done():
                with contextlib.suppress(Exception):
                    fut.set_exception(asyncio.CancelledError())
        self._trading_ws_response_futures.clear()

        logger.info("Lighter adapter closed")

    def _get_request_delay(self) -> float:
        """Get appropriate request delay based on user tier.

        CALCULATED FROM OFFICIAL DOCS (https://apidocs.lighter.xyz/docs/rate-limits):

        Premium:
          - Budget: 24,000 weighted pts per minute
          - "Other endpoints" (most market data): 300 pts per request
          - Max requests: 24,000 / 300 = 80 req/min
          - With 15% safety buffer: 80 * 0.85 = 68 req/min
          - Delay: 60s / 68 = 0.88s

        Standard:
          - Limit: 60 req/min
          - With 15% safety buffer: 60 * 0.85 = 51 req/min
          - Delay: 60s / 51 = 1.18s
        """
        if (self._user_tier or "").lower() == "premium":
            return 0.88  # 68 req/min (85% of 80 req/min @ 300pts each from 24k budget)
        return 1.18  # 51 req/min (85% of 60/min Standard limit)

    def _get_initial_backoff(self) -> float:
        """Get initial backoff time based on user tier.

        Premium: 3s (fast recovery)
        Standard: 10s (more conservative)
        """
        if (self._user_tier or "").lower() == "premium":
            return 3.0
        return 10.0

    async def _request(
        self,
        method: str,
        path: str,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an HTTP request with rate limiting and exponential backoff."""
        if not self._session:
            raise ExchangeError("Session not initialized", exchange="LIGHTER")

        url = f"{self._base_url}{path}"

        # Add auth token to headers if available
        if self._auth_token:
            headers = kwargs.get("headers", {})
            headers["Authorization"] = self._auth_token
            kwargs["headers"] = headers

        # Get dynamic delays based on auth status
        request_delay = self._get_request_delay()
        initial_backoff = self._get_initial_backoff()

        for attempt in range(max_retries):
            async with self._request_lock:
                # Check if we're currently rate-limited
                import time

                now = time.time()
                if now < self._rate_limit_until:
                    wait_time = self._rate_limit_until - now
                    logger.debug(f"Lighter rate-limited, waiting {wait_time:.1f}s before retry")
                    await asyncio.sleep(wait_time)

                # Dynamic rate limiting between requests
                loop_now = asyncio.get_event_loop().time()
                if loop_now - self._last_request_time < request_delay:
                    await asyncio.sleep(request_delay)
                self._last_request_time = asyncio.get_event_loop().time()

                try:
                    async with self._session.request(method, url, **kwargs) as resp:
                        if resp.status == 429:
                            # Exponential backoff: 3s, 6s, 12s (auth) or 10s, 20s, 40s (unauth)
                            backoff = initial_backoff * (2**attempt)
                            self._rate_limit_until = time.time() + backoff
                            logger.warning(
                                f"Lighter rate-limited, backing off for {backoff:.1f}s "
                                f"(attempt {attempt + 1}/{max_retries})"
                            )
                            if attempt < max_retries - 1:
                                await asyncio.sleep(backoff)
                                continue
                            raise ExchangeError(
                                "Rate limit exceeded after retries",
                                exchange="LIGHTER",
                                details={"status": 429},
                            )

                        data = await resp.json()

                        if resp.status >= 400:
                            raise ExchangeError(
                                f"API error: {data}",
                                exchange="LIGHTER",
                                details={"status": resp.status, "response": data},
                            )

                        # Success - reset backoff
                        self._rate_limit_backoff = 0.0
                        return data

                except aiohttp.ClientError as e:
                    raise ExchangeError(
                        f"Connection error: {e}",
                        exchange="LIGHTER",
                    ) from e

        raise ExchangeError("Max retries exceeded", exchange="LIGHTER")

    # =========================================================================
    # Market Data
    # =========================================================================

    async def refresh_all_market_data(self) -> None:
        """
        Batch refresh all market data.
        Fetches funding rates for all markets.
        NOTE: Lighter API does NOT support batch fetching of prices/orderbooks.
        Prices must be fetched individually or lazily.
        """
        if not self._funding_api:
            return

        try:
            # Batch fetch all funding rates in a single call
            result = await self._sdk_call_with_retry(
                lambda: self._funding_api.funding_rates(
                    _headers={"Authorization": self._auth_token} if self._auth_token else None
                )
            )
            rates = getattr(result, "funding_rates", []) or []

            for rate_data in rates:
                # Filter for Lighter exchange only (but be lenient if exchange field is missing)
                rate_exchange = getattr(rate_data, "exchange", "").lower()
                if rate_exchange and rate_exchange != "lighter":
                    # Skip only if exchange field exists and is NOT "lighter"
                    # If exchange field is missing/empty, accept the rate anyway
                    logger.debug(f"Skipping non-Lighter rate: exchange={rate_exchange}")
                    continue

                market_id = getattr(rate_data, "market_id", None)
                rate_symbol = getattr(rate_data, "symbol", None)

                # Find symbol from market_id or use symbol directly
                symbol = rate_symbol
                if not symbol and market_id is not None:
                    symbol = self._market_id_to_symbol.get(int(market_id))

                if not symbol:
                    continue

                # Normalize: FundingApi.funding_rates() returns an 8-hour funding rate in DECIMAL
                # format (signed from the perspective of LONGs). Convert to HOURLY by dividing by 8.
                raw_rate = _safe_decimal(getattr(rate_data, "rate", 0))
                funding_rate = raw_rate / Decimal("8")

                # Validate against documented cap (±0.5%/hour)
                funding_rate = clamp_funding_rate(funding_rate, LIGHTER_FUNDING_RATE_CAP, symbol, "Lighter")

                next_time = getattr(rate_data, "next_funding_time", None)

                self._funding_cache[symbol] = FundingRate(
                    symbol=symbol,
                    exchange=Exchange.LIGHTER,
                    rate=funding_rate,
                    next_funding_time=(datetime.fromtimestamp(int(next_time) / 1000, tz=UTC) if next_time else None),
                )

            logger.debug(f"Lighter batch refresh: {len(self._funding_cache)} funding rates cached")
            if len(self._funding_cache) == 0:
                logger.warning("Lighter funding cache is empty after refresh - API may not be returning exchange field")

        except Exception as e:
            logger.warning(f"Lighter batch refresh failed: {e}")

    async def _sdk_call_with_retry(self, coro_func: Callable[[], Awaitable[Any]], max_retries: int = 3) -> Any:
        """Wrap SDK calls with retry logic for rate limits."""
        import time

        # Optional SDK import for offline tests
        try:
            from lighter.exceptions import ApiException  # type: ignore
        except ImportError:
            class ApiException(Exception):  # type: ignore
                status: int | None = None

        # Dynamic backoff based on user tier
        initial_backoff = self._get_initial_backoff()
        request_delay = self._get_request_delay()

        for attempt in range(max_retries):
            try:
                # Serialize SDK calls to avoid bursts from concurrent tasks.
                async with self._request_lock:
                    # Check if we're currently rate-limited
                    now = time.time()
                    if now < self._rate_limit_until:
                        wait_time = self._rate_limit_until - now
                        logger.debug(f"Lighter rate-limited, waiting {wait_time:.1f}s before SDK call")
                        await asyncio.sleep(wait_time)

                    # Rate limiting between SDK calls
                    loop_now = asyncio.get_event_loop().time()
                    if loop_now - self._last_request_time < request_delay:
                        await asyncio.sleep(request_delay)
                    self._last_request_time = asyncio.get_event_loop().time()

                    return await coro_func()

            except ApiException as e:
                # Handle Auth Expiry (20013) or Unauthorized (401)
                is_auth_error = e.status == 401
                if hasattr(e, "body") and '"code":20013' in str(e.body):
                    is_auth_error = True

                if is_auth_error:
                    logger.warning(f"Lighter Auth Error (401/20013) on attempt {attempt + 1}. Refreshing token...")
                    refreshed = await self._refresh_auth_token()
                    if refreshed:
                        # Retry immediately with new token
                        # Note: The lambda likely captures 'self' which has updated auth now
                        # but some wrapper calls might need to re-read self._auth_token
                        continue
                    else:
                        logger.error("Failed to refresh token on auth error")
                        # Don't re-raise immediately, might have other retries? No, auth fail is hard.
                        # But loop continues to raise if max retries hit.

                if e.status == 429:
                    # Dynamic backoff: 3s, 6s, 12s (auth) or 10s, 20s, 40s (unauth)
                    backoff = initial_backoff * (2**attempt)
                    self._rate_limit_until = time.time() + backoff
                    logger.warning(
                        f"Lighter SDK rate-limited, backing off for {backoff:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        continue

                # Some Lighter endpoints intermittently respond with HTTP 400 but an "internal server error"
                # payload (e.g. code=29500). Treat these as transient and retry with backoff.
                body = str(getattr(e, "body", "") or "")
                body_l = body.lower()
                is_transient_400 = e.status == 400 and ("code=29500" in body or "internal server error" in body_l)
                is_transient_5xx = e.status in (500, 502, 503, 504)
                if is_transient_400 or is_transient_5xx:
                    backoff = initial_backoff * (2**attempt)
                    logger.warning(
                        f"Lighter SDK transient error (HTTP {e.status}), retrying in {backoff:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        continue
                raise

        raise ExchangeError("Max retries exceeded for SDK call", exchange="LIGHTER")

    async def load_markets(self) -> dict[str, MarketInfo]:
        """Load all available markets using SDK.

        🚀 PERFORMANCE PREMIUM-OPTIMIZED: Markets are cached and only refreshed
        after market_cache_ttl_seconds (default: 1 hour). Exchange metadata
        (tick sizes, min qty, etc.) rarely changes, so aggressive caching is safe.
        """
        # Check if cache is still valid
        if self._markets and not self._is_market_cache_stale():
            return self._markets

        async with self._markets_lock:
            # Double-check after acquiring lock
            if self._markets and not self._is_market_cache_stale():
                return self._markets

            try:
                # Reset in case we are reloading after a partial failure.
                self._markets.clear()
                self._market_indices.clear()
                self._market_id_to_symbol.clear()

                # Use SDK if available
                if self._order_api:
                    # Optimization: Use order_book_details() to fetch ALL market metadata in one call.
                    # This includes: Symbol, Precision, Fees, Funding Rates, AND Leverage (IMR).
                    result = await self._sdk_call_with_retry(lambda: self._order_api.order_book_details())

                    # result is OrderBookDetails
                    perp_details = getattr(result, "order_book_details", []) or []

                    for d in perp_details:
                        symbol = d.symbol if hasattr(d, "symbol") else ""
                        if not symbol or (hasattr(d, "status") and d.status != "active"):
                            continue

                        # Parse IDs
                        market_id = int(d.market_id) if hasattr(d, "market_id") else -1
                        if market_id < 0:
                            continue
                        self._market_indices[symbol] = market_id
                        self._market_id_to_symbol[market_id] = symbol

                        # Parse Precisions
                        price_decimals = (
                            int(d.supported_price_decimals) if hasattr(d, "supported_price_decimals") else 2
                        )
                        size_decimals = int(d.supported_size_decimals) if hasattr(d, "supported_size_decimals") else 4

                        tick_size = Decimal(f"1e-{price_decimals}")
                        step_size = Decimal(f"1e-{size_decimals}")

                        # Parse Limits
                        min_qty = (
                            _safe_decimal(d.min_base_amount, Decimal("0"))
                            if hasattr(d, "min_base_amount")
                            else Decimal("0")
                        )
                        min_notional = (
                            _safe_decimal(d.min_quote_amount, Decimal("10"))
                            if hasattr(d, "min_quote_amount")
                            else Decimal("10")
                        )

                        # Parse Fees
                        maker_fee = (
                            _safe_decimal(d.maker_fee, Decimal("0")) if hasattr(d, "maker_fee") else Decimal("0")
                        )
                        taker_fee = (
                            _safe_decimal(d.taker_fee, Decimal("0")) if hasattr(d, "taker_fee") else Decimal("0")
                        )

                        # Parse Dynamic Leverage (IMR)
                        # min_initial_margin_fraction is StrictInt (likely scaled by 1e18)
                        max_leverage = Decimal("3")  # Fallback
                        imr_raw = getattr(d, "min_initial_margin_fraction", None)
                        if imr_raw is not None:
                            try:
                                imr_int = int(str(imr_raw))
                                if imr_int > 0:
                                    leverage = Decimal("10000") / Decimal(imr_int)
                                    max_leverage = leverage.quantize(Decimal("0.1"), rounding="ROUND_FLOOR")
                            except Exception:
                                logger.warning(f"Failed to parse IMR for {symbol}: {imr_raw}")

                        info = MarketInfo(
                            symbol=symbol,
                            exchange=Exchange.LIGHTER,
                            base_asset=symbol.split("-")[0] if "-" in symbol else symbol,
                            quote_asset="USD",
                            price_precision=price_decimals,
                            qty_precision=size_decimals,
                            tick_size=tick_size,
                            step_size=step_size,
                            min_qty=min_qty,
                            max_qty=Decimal("1000000"),
                            min_notional=min_notional,
                            maker_fee=maker_fee,
                            taker_fee=taker_fee,
                            lighter_max_leverage=max_leverage,
                            lighter_min_initial_margin_fraction=int(str(imr_raw)) if imr_raw else None,
                        )
                        self._markets[symbol] = info

                        # Cache funding immediately (API returns rate per configured interval; normalize to hourly).
                        # API returns rates in DECIMAL format (e.g., -0.000386 = -0.0386%).
                        # NO division by 100 - API already returns decimal format.
                        try:
                            raw_rate = _safe_decimal(getattr(d, "funding_rate", 0))
                            interval_hours = self.settings.lighter.funding_rate_interval_hours
                            funding_rate = raw_rate / interval_hours

                            next_time_ts = getattr(d, "funding_timestamp", None)

                            self._funding_cache[symbol] = FundingRate(
                                symbol=symbol,
                                exchange=Exchange.LIGHTER,
                                rate=funding_rate,
                                next_funding_time=(
                                    datetime.fromtimestamp(int(next_time_ts) / 1000, tz=UTC) if next_time_ts else None
                                ),
                            )
                        except Exception:
                            pass

                    logger.info(f"Loaded {len(self._markets)} Lighter markets via SDK (Batch Details)")
                    self._markets_loaded_at = datetime.now(UTC)  # Update cache timestamp
                    return self._markets

                # Fallback to REST
                data = await self._request("GET", "/api/v1/info/markets")
                markets = data.get("markets", data) if isinstance(data, dict) else data

                for m in markets:
                    symbol = m.get("symbol", "")
                    if not symbol:
                        continue

                    info = MarketInfo(
                        symbol=symbol,
                        exchange=Exchange.LIGHTER,
                        base_asset=m.get("base_asset", symbol.split("-")[0] if "-" in symbol else symbol),
                        quote_asset=m.get("quote_asset", "USD"),
                        price_precision=int(m.get("price_precision", 2)),
                        qty_precision=int(m.get("qty_precision", 4)),
                        tick_size=_safe_decimal(m.get("tick_size"), Decimal("0.01")),
                        step_size=_safe_decimal(m.get("step_size"), Decimal("0.0001")),
                        min_qty=_safe_decimal(m.get("min_qty"), Decimal("0")),
                        max_qty=_safe_decimal(m.get("max_qty"), Decimal("1000000")),
                        min_notional=_safe_decimal(m.get("min_notional"), Decimal("1")),
                        maker_fee=Decimal("0"),
                        taker_fee=Decimal("0"),
                    )
                    self._markets[symbol] = info

                logger.debug(f"Loaded {len(self._markets)} Lighter markets via REST")
                self._markets_loaded_at = datetime.now(UTC)  # Update cache timestamp
                return self._markets

            except Exception as e:
                logger.exception(f"Failed to load Lighter markets: {e}")
                return {}

    def _is_market_cache_stale(self) -> bool:
        """Check if the market cache is stale based on TTL setting."""
        if self._markets_loaded_at is None:
            return True  # No cache yet

        # Get cache TTL from settings (default: 1 hour)
        cache_ttl = float(
            getattr(
                getattr(getattr(self.settings, "market_data", None), "market_cache_ttl_seconds", None),
                "market_cache_ttl_seconds",
                3600.0
            ) or 3600.0
        )

        # Check if cache has expired
        age = (datetime.now(UTC) - self._markets_loaded_at).total_seconds()
        return age > cache_ttl

    async def get_market_info(self, symbol: str) -> MarketInfo | None:
        """Get market metadata for a symbol."""
        if not self._markets or self._is_market_cache_stale():
            await self.load_markets()
        return self._markets.get(symbol)

    def _get_market_index(self, symbol: str) -> int:
        """Get market index from symbol. Lighter uses numeric market IDs.

        Returns -1 if unknown (0 is valid for ETH).
        """
        if hasattr(self, "_market_indices") and symbol in self._market_indices:
            return self._market_indices[symbol]
        return -1

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Get current mark price for a symbol."""
        try:
            # Fast path: use fresh WS/L1 cache if available.
            ws_cfg = getattr(self.settings, "websocket", None)
            max_age = float(getattr(ws_cfg, "orderbook_l1_fallback_max_age_seconds", 10.0) or 10.0)
            now = datetime.now(UTC)

            cached_price = self._price_cache.get(symbol)
            cached_price_ts = self._price_updated_at.get(symbol)
            if cached_price is not None and cached_price_ts:
                age = (now - cached_price_ts).total_seconds()
                if age <= max_age and cached_price > 0:
                    return cached_price

            cached_l1 = self._orderbook_cache.get(symbol)
            cached_l1_ts = self._orderbook_updated_at.get(symbol)
            if cached_l1 and cached_l1_ts:
                age = (now - cached_l1_ts).total_seconds()
                if age <= max_age:
                    best_bid = _safe_decimal(cached_l1.get("best_bid"))
                    best_ask = _safe_decimal(cached_l1.get("best_ask"))
                    if best_bid > 0 and best_ask > 0:
                        price = (best_bid + best_ask) / 2
                        self._price_cache[symbol] = price
                        self._price_updated_at[symbol] = now
                        return price

            # Use SDK if available
            if self._order_api:
                # Get market_id from markets cache
                if not self._markets:
                    await self.load_markets()

                market_id = self._get_market_index(symbol)
                if market_id < 0:
                    raise ExchangeError(
                        f"Unknown market_id for {symbol}",
                        exchange="LIGHTER",
                        symbol=symbol,
                    )

                # IMPORTANT: order_book_details returns a wrapper with a LIST of details (no mark_price at top-level).
                # For an efficient single-call "real price", use order_book_orders(limit=1) and compute mid.
                orders = await self._sdk_call_with_retry(
                    lambda: self._order_api.order_book_orders(
                        market_id=market_id,
                        limit=1,
                        _headers={"Authorization": self._auth_token} if self._auth_token else None,
                    )
                )
                bids = getattr(orders, "bids", []) or []
                asks = getattr(orders, "asks", []) or []

                best_bid = _safe_decimal(getattr(bids[0], "price", 0)) if bids else Decimal("0")
                best_ask = _safe_decimal(getattr(asks[0], "price", 0)) if asks else Decimal("0")

                if best_bid > 0 and best_ask > 0:
                    price = (best_bid + best_ask) / 2
                else:
                    # Fallback: last trade price from order_book_details
                    details = await self._sdk_call_with_retry(
                        lambda: self._order_api.order_book_details(market_id=market_id)
                    )
                    details_list = getattr(details, "order_book_details", []) or []
                    detail = details_list[0] if details_list else None
                    price = _safe_decimal(getattr(detail, "last_trade_price", 0))

                # Cache price + a minimal L1 view if available
                self._price_cache[symbol] = price
                self._price_updated_at[symbol] = datetime.now(UTC)
                if best_bid > 0 or best_ask > 0:
                    bid_qty = _safe_decimal(getattr(bids[0], "remaining_base_amount", 0)) if bids else Decimal("0")
                    ask_qty = _safe_decimal(getattr(asks[0], "remaining_base_amount", 0)) if asks else Decimal("0")
                    self._orderbook_cache[symbol] = {
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "bid_qty": bid_qty,
                        "ask_qty": ask_qty,
                    }

                return price

            # REST fallback
            data = await self._request("GET", f"/api/v1/orderBookDetails?market_index={self._get_market_index(symbol)}")
            price = _safe_decimal(data.get("mark_price") or data.get("last_price") or data.get("mid_price"))
            self._price_cache[symbol] = price
            self._price_updated_at[symbol] = datetime.now(UTC)
            return price

        except Exception as e:
            if symbol in self._price_cache:
                return self._price_cache[symbol]
            raise ExchangeError(
                f"Failed to get mark price for {symbol}: {e}",
                exchange="LIGHTER",
                symbol=symbol,
            ) from e

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate for a symbol (from cache)."""
        # Return cached rate - batch refresh updates the cache
        if symbol in self._funding_cache:
            return self._funding_cache[symbol]

        # If not cached, try a batch refresh
        await self.refresh_all_market_data()
        if symbol in self._funding_cache:
            return self._funding_cache[symbol]

        # Return zero rate if still not found
        return FundingRate(symbol=symbol, exchange=Exchange.LIGHTER, rate=Decimal("0"))

    async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]:
        """
        Get best bid/ask.

        Phase 3.2 Optimization: Cache-first with WebSocket auto-start (50ms updates).
        """
        try:
            market_id = self._get_market_index(symbol)
            now = datetime.now(UTC)

            # Phase 3.2: Auto-start per-market WebSocket for this symbol (non-blocking).
            # This ensures subsequent calls benefit from 50ms WS updates instead of REST.
            if market_id >= 0:
                ws_task = self._ob_ws_tasks.get(market_id)
                if ws_task is None or ws_task.done():
                    # Start WebSocket in background (don't wait - first call uses REST/SDK)
                    asyncio.create_task(self.ensure_orderbook_ws(symbol, timeout=0.0))

            # Phase 3.2: Check cache freshness first (Turbo Mode like X10).
            # Use WS cache if fresh (< 2s) and has valid depth.
            if symbol in self._orderbook_cache:
                cached = self._orderbook_cache[symbol]
                last_update = self._orderbook_updated_at.get(symbol)
                if last_update:
                    age = (now - last_update).total_seconds()
                    cache_has_depth = (
                        cached.get("bid_qty", Decimal("0")) > 0
                        and cached.get("ask_qty", Decimal("0")) > 0
                    )
                    if age < 2.0 and cache_has_depth:
                        return cached

            # Use SDK if available
            if self._order_api:
                if market_id < 0:
                    raise ExchangeError(
                        f"Unknown market_id for {symbol}",
                        exchange="LIGHTER",
                        symbol=symbol,
                    )

                # order_book_orders returns bids/asks lists; use limit=1 for L1
                book = await self._sdk_call_with_retry(
                    lambda: self._order_api.order_book_orders(
                        market_id=market_id,
                        limit=1,
                        _headers={"Authorization": self._auth_token} if self._auth_token else None,
                    )
                )

                bids = getattr(book, "bids", []) or []
                asks = getattr(book, "asks", []) or []

                best_bid = _safe_decimal(getattr(bids[0], "price", 0)) if bids else Decimal("0")
                best_ask = _safe_decimal(getattr(asks[0], "price", 0)) if asks else Decimal("0")
                bid_qty = _safe_decimal(getattr(bids[0], "remaining_base_amount", 0)) if bids else Decimal("0")
                ask_qty = _safe_decimal(getattr(asks[0], "remaining_base_amount", 0)) if asks else Decimal("0")

                result = {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "bid_qty": bid_qty,
                    "ask_qty": ask_qty,
                }
                self._orderbook_cache[symbol] = result
                self._orderbook_updated_at[symbol] = now
                if best_bid > 0 and best_ask > 0 and best_bid < best_ask:
                    self._price_cache[symbol] = (best_bid + best_ask) / 2
                    self._price_updated_at[symbol] = now
                return result

            # REST fallback
            data = await self._request("GET", f"/api/v1/orderBookDetails?market_index={market_id}")

            result = {
                "best_bid": _safe_decimal(data.get("best_bid_price") or data.get("bid_price")),
                "best_ask": _safe_decimal(data.get("best_ask_price") or data.get("ask_price")),
                "bid_qty": _safe_decimal(data.get("best_bid_qty", 0)),
                "ask_qty": _safe_decimal(data.get("best_ask_qty", 0)),
            }

            self._orderbook_cache[symbol] = result
            self._orderbook_updated_at[symbol] = now
            best_bid = result.get("best_bid", Decimal("0"))
            best_ask = result.get("best_ask", Decimal("0"))
            if (
                isinstance(best_bid, Decimal)
                and isinstance(best_ask, Decimal)
                and best_bid > 0
                and best_ask > 0
                and best_bid < best_ask
            ):
                self._price_cache[symbol] = (best_bid + best_ask) / 2
                self._price_updated_at[symbol] = now
            return result

        except Exception as e:
            if symbol in self._orderbook_cache:
                return self._orderbook_cache[symbol]
            raise ExchangeError(
                f"Failed to get orderbook for {symbol}: {e}",
                exchange="LIGHTER",
                symbol=symbol,
            ) from e

    async def get_orderbook_depth(self, symbol: str, depth: int = 20) -> dict[str, list[tuple[Decimal, Decimal]]]:
        """
        Get top-N orderbook levels (bids/asks) as (price, qty) tuples.

        Notes:
        - Uses the Lighter SDK `order_book_orders(market_id, limit)` when available.
        - Returns empty lists on markets that are unavailable.
        """
        try:
            market_id = self._get_market_index(symbol)
            limit = int(depth or 1)
            limit = max(1, min(limit, 250))

            # Prefer WS-backed LocalOrderbook when available and fresh.
            book = self._ob_ws_books.get(market_id)
            if book and book.snapshot_loaded and book.bids and book.asks:
                ws_ts = self._orderbook_updated_at.get(symbol)
                max_age = float(
                    getattr(getattr(self.settings, "websocket", None), "orderbook_l1_fallback_max_age_seconds", 10.0)
                    or 10.0
                )
                if not ws_ts or (datetime.now(UTC) - ws_ts).total_seconds() <= max_age:
                    depth_book = book.get_depth(limit)
                    if depth_book.get("bids") and depth_book.get("asks"):
                        return {
                            "bids": depth_book["bids"],
                            "asks": depth_book["asks"],
                            "updated_at": ws_ts or datetime.now(UTC),
                        }

            # Prefer SDK (returns SimpleOrder objects with string fields).
            if self._order_api:
                if market_id < 0:
                    raise ExchangeError(
                        f"Unknown market_id for {symbol}",
                        exchange="LIGHTER",
                        symbol=symbol,
                    )

                book = await self._sdk_call_with_retry(
                    lambda: self._order_api.order_book_orders(
                        market_id=market_id,
                        limit=limit,
                        _headers={"Authorization": self._auth_token} if self._auth_token else None,
                    )
                )

                bids_raw = getattr(book, "bids", []) or []
                asks_raw = getattr(book, "asks", []) or []

                bids: list[tuple[Decimal, Decimal]] = []
                asks: list[tuple[Decimal, Decimal]] = []

                for row in bids_raw:
                    price = _safe_decimal(getattr(row, "price", 0))
                    qty = _safe_decimal(getattr(row, "remaining_base_amount", 0))
                    if price > 0 and qty > 0:
                        bids.append((price, qty))

                for row in asks_raw:
                    price = _safe_decimal(getattr(row, "price", 0))
                    qty = _safe_decimal(getattr(row, "remaining_base_amount", 0))
                    if price > 0 and qty > 0:
                        asks.append((price, qty))

                # Defensive sort (SDK should already return correctly ordered lists).
                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])

                return {"bids": bids, "asks": asks, "updated_at": datetime.now(UTC)}

            # Fallback: synthesize depth from L1 only.
            l1 = await self.get_orderbook_l1(symbol)
            best_bid = _safe_decimal(l1.get("best_bid", 0))
            best_ask = _safe_decimal(l1.get("best_ask", 0))
            bid_qty = _safe_decimal(l1.get("bid_qty", 0))
            ask_qty = _safe_decimal(l1.get("ask_qty", 0))
            bids = [(best_bid, bid_qty)] if best_bid > 0 and bid_qty > 0 else []
            asks = [(best_ask, ask_qty)] if best_ask > 0 and ask_qty > 0 else []
            return {"bids": bids, "asks": asks, "updated_at": datetime.now(UTC)}

        except Exception as e:
            raise ExchangeError(
                f"Failed to get orderbook depth for {symbol}: {e}",
                exchange="LIGHTER",
                symbol=symbol,
            ) from e

    # =========================================================================
    # Account
    # =========================================================================

    async def get_available_balance(self) -> Balance:
        """Get available margin/balance."""
        try:
            # Prefer SDK (REST endpoints have changed over time; SDK stays current).
            if self._account_api and self._account_index is not None:
                accounts_resp = await self._sdk_call_with_retry(
                    lambda: self._account_api.account(
                        by="index",
                        value=str(self._account_index),
                        _headers={"Authorization": self._auth_token} if self._auth_token else None,
                    )
                )
                accounts = getattr(accounts_resp, "accounts", []) or []
                acct = accounts[0] if accounts else None

                available = _safe_decimal(getattr(acct, "available_balance", None) or 0)
                total = _safe_decimal(
                    getattr(acct, "total_asset_value", None) or getattr(acct, "collateral", None) or 0
                )

                return Balance(
                    exchange=Exchange.LIGHTER,
                    available=available,
                    total=total,
                    currency="USD",
                )

            # Fallback (legacy REST)
            data = await self._request("GET", "/api/v1/account")
            return Balance(
                exchange=Exchange.LIGHTER,
                available=_safe_decimal(data.get("available_margin")),
                total=_safe_decimal(data.get("total_equity")),
                currency="USD",
            )

        except Exception as e:
            raise ExchangeError(
                f"Failed to get balance: {e}",
                exchange="LIGHTER",
            ) from e

    async def _warmup_http_session(self) -> None:
        """
        Warm up HTTP session with a lightweight request.

        This prevents the first real API call from incurring 100-200ms TLS handshake latency.
        The session is now "primed" and ready for immediate use.
        """
        if not self._session:
            return

        logger.info("Warming up Lighter HTTP session (TLS handshake)...")
        try:
            # Send a lightweight GET request to establish the connection
            # Using /api/v1/account_limits as a simple authenticated endpoint
            async with self._session.get(
                f"{self._base_url}/api/v1/account_limits",
                params={"account_index": self._account_index} if self._account_index is not None else None,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                # We don't care about the response, just the connection
                await resp.read()
            logger.debug("Lighter HTTP session warmup complete")
        except Exception:
            # Connection is established even if request fails (auth, etc.)
            logger.debug("Lighter HTTP session warmup completed (connection established)")

    async def _detect_tier_and_fees(self) -> None:
        """Detect account tier and set appropriate fees."""
        if not self._account_api or self._account_index is None or not self._auth_token:
            # Fallback to configured fees
            self._taker_fee = self.settings.trading.taker_fee_lighter
            self._maker_fee = self.settings.trading.maker_fee_lighter
            return

        try:
            # Call account_limits to get user_tier
            limits = await self._sdk_call_with_retry(
                lambda: self._account_api.account_limits(
                    account_index=self._account_index, _headers={"Authorization": self._auth_token}
                )
            )

            self._user_tier = str(getattr(limits, "user_tier", "standard")).lower()
            logger.info(f"Lighter Account Tier detected: {self._user_tier}")

            if self._user_tier == "premium":
                # Premium fees: 0.02% Taker, 0.002% Maker
                self._taker_fee = Decimal("0.0002")
                self._maker_fee = Decimal("0.00002")
                logger.info(
                    f"Applying Lighter Premium fees: Taker={self._taker_fee * 100}%, Maker={self._maker_fee * 100}%"
                )
            else:
                # Fallback to configured fees
                self._taker_fee = self.settings.trading.taker_fee_lighter
                self._maker_fee = self.settings.trading.maker_fee_lighter
                logger.info(
                    f"Using configured Lighter fees: Taker={self._taker_fee * 100}%, Maker={self._maker_fee * 100}%"
                )

        except Exception as e:
            # Fallback to configured fees
            self._taker_fee = self.settings.trading.taker_fee_lighter
            self._maker_fee = self.settings.trading.maker_fee_lighter
            logger.debug(f"Failed to detect Lighter tier: {e}. Using configured fees.")

    async def get_fee_schedule(self, symbol: str | None = None) -> dict[str, Decimal]:
        """Get fee schedule."""
        # If we have cached tier fees, use them
        if self._taker_fee is not None:
            return {
                "maker_fee": self._maker_fee or Decimal("0"),
                "taker_fee": self._taker_fee,
            }

        # Fallback to market info if symbol provided
        if symbol:
            info = await self.get_market_info(symbol)
            if info:
                return {
                    "maker_fee": info.maker_fee,
                    "taker_fee": info.taker_fee,
                }

        # Ultimate fallback to defaults
        return {
            "maker_fee": self.settings.trading.maker_fee_lighter,
            "taker_fee": self.settings.trading.taker_fee_lighter,
        }

    # =========================================================================
    # Positions
    # =========================================================================

    async def list_positions(self) -> list[Position]:
        """Get all open positions."""
        try:
            # Prefer SDK: account(by=index) includes position details.
            if self._account_api and self._account_index is not None:
                accounts_resp = await self._sdk_call_with_retry(
                    lambda: self._account_api.account(
                        by="index",
                        value=str(self._account_index),
                        _headers={"Authorization": self._auth_token} if self._auth_token else None,
                    )
                )
                accounts = getattr(accounts_resp, "accounts", []) or []
                acct = accounts[0] if accounts else None
                acct_positions = getattr(acct, "positions", []) or []

                positions: list[Position] = []
                for p in acct_positions:
                    qty = _safe_decimal(getattr(p, "position", 0))
                    sign = int(getattr(p, "sign", 1) or 1)
                    if qty == 0:
                        continue

                    side = Side.BUY if sign >= 0 else Side.SELL
                    symbol = str(getattr(p, "symbol", ""))

                    positions.append(
                        Position(
                            symbol=symbol,
                            exchange=Exchange.LIGHTER,
                            side=side,
                            qty=abs(qty),
                            entry_price=_safe_decimal(getattr(p, "avg_entry_price", 0)),
                            mark_price=Decimal("0"),  # not included in this endpoint; refreshed via market data
                            liquidation_price=_safe_decimal(getattr(p, "liquidation_price", 0)) or None,
                            unrealized_pnl=_safe_decimal(getattr(p, "unrealized_pnl", 0)),
                            leverage=Decimal("1"),
                        )
                    )

                return positions

            # Fallback (legacy REST)
            data = await self._request("GET", "/api/v1/positions")
            positions_data = data.get("positions", [])
            positions = []
            for p in positions_data:
                qty = _safe_decimal(p.get("size") or p.get("qty"))
                if qty == 0:
                    continue

                side = Side.BUY if qty > 0 else Side.SELL

                positions.append(
                    Position(
                        symbol=p.get("symbol", ""),
                        exchange=Exchange.LIGHTER,
                        side=side,
                        qty=abs(qty),
                        entry_price=_safe_decimal(p.get("entry_price")),
                        mark_price=_safe_decimal(p.get("mark_price")),
                        liquidation_price=_safe_decimal(p.get("liquidation_price")) or None,
                        unrealized_pnl=_safe_decimal(p.get("unrealized_pnl")),
                        leverage=_safe_decimal(p.get("leverage"), Decimal("1")),
                    )
                )

            return positions

        except Exception as e:
            raise ExchangeError(
                f"Failed to list positions: {e}",
                exchange="LIGHTER",
            ) from e

    async def get_position(self, symbol: str) -> Position | None:
        """Get position for a specific symbol."""

        def _norm(s: str) -> str:
            return str(s or "").strip().upper().replace("-USD", "")

        target = _norm(symbol)
        positions = await self.list_positions()
        for p in positions:
            if _norm(p.symbol) == target:
                return p
        return None

    async def get_realized_funding(self, symbol: str, start_time: datetime | None = None) -> Decimal:
        """Get cumulative realized funding payment for a symbol since start_time."""
        total_funding = Decimal("0")

        if not self._account_api or self._account_index is None:
            logger.debug(
                "Funding collection skipped: account API not initialized or account_index is None "
                f"(account_index={self._account_index}, symbol={symbol})"
            )
            return total_funding

        try:
            market_id = self._get_market_index(symbol)
            if market_id < 0:
                logger.warning(f"Unknown market_id for {symbol}, cannot fetch funding")
                return total_funding

            cursor = None
            start_ts = int(start_time.timestamp()) if start_time else 0

            while True:
                resp = await self._sdk_call_with_retry(
                    lambda c=cursor: self._account_api.position_funding(
                        account_index=self._account_index,
                        market_id=market_id,
                        limit=100,
                        cursor=c,
                        side="all",
                        _headers={"Authorization": self._auth_token} if self._auth_token else None,
                    ),
                    max_retries=5,
                )

                items = getattr(resp, "position_fundings", []) or []

                # Assume items are ordered newest to oldest
                for item in items:
                    ts = getattr(item, "timestamp", 0)

                    # Lighter timestamps are usually seconds, but sometimes ms. Check magnitude.
                    # Based on other endpoints, likely seconds. If > 30000000000, it's ms.
                    # Standard check:
                    if ts > 1e11:
                        ts = ts / 1000

                    if ts < start_ts:
                        # Reached items before start_time
                        return total_funding

                    # Accumulate change
                    # change is a string representing the funding payment amount
                    change = _safe_decimal(getattr(item, "change", "0"))
                    total_funding += change

                cursor = getattr(resp, "next_cursor", None)
                if not cursor or not items:
                    break

        except Exception as e:
            logger.warning(f"Failed to fetch funding history for {symbol}: {e}")
            raise ExchangeError(f"Failed to fetch funding history: {e}", exchange="LIGHTER") from e

        # Log when no funding found (not spammy: only once per collection cycle)
        if total_funding == 0:
            logger.debug(f"No funding payments found for {symbol} since {start_time or 'account open'}")

        return total_funding

    # =========================================================================
    # Orders
    # =========================================================================

    # =========================================================================
    # Orders
    # =========================================================================

    async def _validate_order_price(self, request: OrderRequest) -> None:
        """
        Validate order price against current mark price to prevent fat-finger errors
        or stale data execution. Enforces Rule 4 (Safety).
        """
        # Market orders are handled by the matching engine
        if request.order_type == OrderType.MARKET or not request.price:
            return

        try:
            # Use internal cache if available and fresh to avoid latency
            mark_price: Decimal | None = None
            cached_price = self._price_cache.get(request.symbol)

            # Simple freshness check if we had timestamps (TODO), for now trust cache or fetch
            # If we have no cache, we MUST fetch.
            if cached_price and cached_price > 0:
                mark_price = cached_price
            else:
                mark_price = await self.get_mark_price(request.symbol)
        except Exception:
            mark_price = Decimal("0")

        if not mark_price or mark_price <= 0:
            # Safety First: If we don't know the price, we don't trade.
            raise OrderRejectedError(
                "Mark price unavailable for validation",
                symbol=request.symbol,
            )

        # 10% Safety Band
        # This prevents "fat finger" orders or algorithm hallucinations (e.g. price=1.0 for BTC)
        deviation = Decimal("0.10")
        lower_bound = mark_price * (Decimal("1") - deviation)
        upper_bound = mark_price * (Decimal("1") + deviation)

        if request.price < lower_bound or request.price > upper_bound:
            msg = (
                f"Price deviation blocked: {request.symbol} Order={request.price:.4f} "
                f"Mark={mark_price:.4f} (+/- {deviation * 100}%)"
            )
            logger.error(f"SAFETY INTERVENTION: {msg}")
            raise OrderRejectedError(msg, symbol=request.symbol)

    async def place_order(self, request: OrderRequest) -> Order:
        """Place an order using SignerClient (preferred) or REST fallback."""
        # [Rule 4] Price-Banding Check
        await self._validate_order_price(request)

        if self._signer:
            return await self._place_order_signed(request)
        return await self._place_order_rest(request)

    async def _place_order_signed(self, request: OrderRequest) -> Order:
        """Place an order using Lighter SDK SignerClient."""
        logger.info(
            f"Placing Lighter signed order: {request.side.value} {request.qty} {request.symbol} "
            f"@ {request.price or 'MARKET'}"
        )

        try:
            # 1. Get market details
            market_info = await self.get_market_info(request.symbol)
            if not market_info:
                raise OrderRejectedError(f"Unknown market {request.symbol}", exchange="LIGHTER", symbol=request.symbol)

            market_index = self._get_market_index(request.symbol)

            # 2. Generate client_order_index (int32)
            # Use timestamp + random to ensure uniqueness and fit in int32 (if needed) or int64
            # Lighter uses int64 for client_order_index
            client_order_index = int(time.time() * 1000) + random.randint(0, 1000)

            # 3. Scale values & Emulate Market Order
            price_val = request.price
            order_type = request.order_type
            tif = request.time_in_force
            is_market_style = order_type == OrderType.MARKET or not price_val or price_val <= 0

            # Default mappings (SignerClient: 0=LIMIT, 1=MARKET; TIF: 0=IOC, 1=GTT, 2=POST_ONLY)
            order_type_int = 1 if order_type == OrderType.MARKET else 0
            tif_int = 1  # Default GTC/GTT

            if is_market_style:
                # Market orders require an avg_execution_price; derive it from L1 to ensure
                # the IOC is actually marketable (mark price can drift away from bid/ask).
                best_bid = Decimal("0")
                best_ask = Decimal("0")
                try:
                    ob = await self.get_orderbook_l1(request.symbol)
                    best_bid = _safe_decimal(ob.get("best_bid", "0"))
                    best_ask = _safe_decimal(ob.get("best_ask", "0"))
                except Exception:
                    pass

                # Use configured slippage for taker orders, but enforce a higher minimum for
                # reduce-only closes so we actually flatten positions during exit/reconcile.
                slippage = _safe_decimal(
                    getattr(self.settings.execution, "taker_order_slippage", None),
                    default=Decimal("0.02"),
                )
                close_min = _safe_decimal(
                    getattr(self.settings.execution, "taker_close_slippage", None),
                    default=Decimal("0.01"),
                )
                if request.reduce_only:
                    slippage = max(slippage, close_min)

                # Safety clamp: avoid accidental extremes on misconfig.
                slippage = max(Decimal("0.0005"), min(slippage, Decimal("0.05")))

                ref_price = Decimal("0")
                if request.side == Side.BUY and best_ask > 0:
                    ref_price = best_ask
                elif request.side == Side.SELL and best_bid > 0:
                    ref_price = best_bid

                if ref_price <= 0:
                    try:
                        ref_price = await self.get_mark_price(request.symbol)
                    except Exception as e:
                        raise OrderRejectedError(
                            f"Cannot emulate market order: price unavailable for {request.symbol}",
                            exchange="LIGHTER",
                            symbol=request.symbol,
                        ) from e

                if request.side == Side.BUY:
                    price_val = ref_price * (Decimal("1") + slippage)
                    if best_ask > 0:
                        price_val = max(price_val, best_ask)
                else:
                    price_val = ref_price * (Decimal("1") - slippage)
                    if best_bid > 0:
                        price_val = min(price_val, best_bid)

                logger.info(
                    f"Lighter MARKET px derived for {request.symbol}: "
                    f"bid={best_bid} ask={best_ask} ref={ref_price} slip={slippage} -> px={price_val}"
                )

                # Force MARKET + IOC for taker behavior
                order_type_int = 1  # MARKET
                tif_int = 0  # IOC
            else:
                # Standard TIF mapping if not Market
                if tif == TimeInForce.IOC:
                    tif_int = 0
                elif tif == TimeInForce.POST_ONLY:
                    tif_int = 2

            # Now calculate ints.
            price_scale = Decimal("10") ** market_info.price_precision
            if is_market_style:
                # For BUY taker orders, round UP so we don't accidentally become non-marketable.
                scaled_price = price_val * price_scale
                rounding = ROUND_CEILING if request.side == Side.BUY else ROUND_FLOOR
                price_int = int(scaled_price.to_integral_value(rounding=rounding))
            else:
                price_int = int(price_val * price_scale)
            qty_int = int(request.qty * (Decimal("10") ** market_info.qty_precision))

            # 4. Map Enums (Done above)
            is_ask = request.side == Side.SELL

            # Determine Expiry
            # For IOC (0), Lighter requires explicit 0 expiry
            expiry = 0 if tif_int == 0 else -1

            # 5. Send Tx (prefer WS `jsonapi/sendtx`, fallback to HTTP /api/v1/sendTx via SDK)
            tx_hash: str | None = None
            ws_used = False

            if self._use_ws_orders and await self.ensure_trading_ws(timeout=self._trading_ws_connect_timeout_seconds):
                try:
                    tx_hash, _ws_resp = await self._ws_submit_signed_tx(
                        purpose="create_order",
                        signer_fn=lambda api_key_index, nonce: self._signer.sign_create_order(
                            market_index=market_index,
                            client_order_index=client_order_index,
                            base_amount=qty_int,
                            price=price_int,
                            is_ask=is_ask,
                            order_type=order_type_int,
                            time_in_force=tif_int,
                            reduce_only=request.reduce_only,
                            trigger_price=0,
                            order_expiry=expiry,
                            nonce=nonce,
                            api_key_index=api_key_index,
                        ),
                        timeout=self._trading_ws_response_timeout_seconds,
                    )
                    ws_used = True
                except Exception as e:
                    logger.warning(f"Lighter WS order submission failed; falling back to HTTP sendTx: {e}")

            if not ws_used:
                # create_order returns (CreateOrder, RespSendTx, Error)
                _create_order_obj, resp_tx, err = await self._sdk_call_with_retry(
                    lambda: self._signer.create_order(
                        market_index=market_index,
                        client_order_index=client_order_index,
                        base_amount=qty_int,
                        price=price_int,
                        is_ask=is_ask,
                        order_type=order_type_int,
                        time_in_force=tif_int,
                        reduce_only=request.reduce_only,
                        order_expiry=expiry,  # Explicit expiry
                        nonce=-1,  # Auto-nonce (SDK-managed)
                        api_key_index=self._api_key_index,
                    )
                )

                if err:
                    raise OrderRejectedError(f"Signer error: {err}", exchange="LIGHTER", symbol=request.symbol)

                tx_hash = str(getattr(resp_tx, "tx_hash", "") or "")

            logger.info(f"Signed order sent. TxHash: {tx_hash}, ClientIndex: {client_order_index}, ws_sendtx={ws_used}")

            # 6. Return immediately with a deterministic placeholder ID.
            # The system order_id may not be discoverable yet via REST, and resolving it synchronously
            # adds latency (especially for instant fills). We correlate fills/cancels via WS using
            # `client_order_index`, mirroring the approach used in `.tmp` tools.
            status = OrderStatus.PENDING
            final_order_id = f"pending_{client_order_index}_{market_index}"

            return Order(
                order_id=final_order_id,
                symbol=request.symbol,
                exchange=Exchange.LIGHTER,
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                reduce_only=request.reduce_only,
                status=status,
                filled_qty=Decimal("0"),
                avg_fill_price=Decimal("0"),
                fee=Decimal("0"),
                client_order_id=str(client_order_index),
            )

        except Exception as e:
            error_msg = str(e)
            logger.exception(f"Failed to place signed order: {e}")

            if "not enough margin" in error_msg.lower() or "code=21739" in error_msg.lower():
                raise InsufficientBalanceError(
                    f"Insufficient Lighter margin: {e}",
                    required=request.qty * (request.price or Decimal("0")),
                    available=Decimal("0"),
                    exchange="LIGHTER",
                    symbol=request.symbol,
                ) from e

            raise OrderRejectedError(f"Order failed: {e}", exchange="LIGHTER", symbol=request.symbol) from e

    async def _resolve_order_id(self, client_order_index: int, market_index: int, retries: int = 3) -> int | None:
        """Try to find the system order ID for a given client index."""
        if not self._order_api:
            return None

        # Try to find the order in active (open) OR inactive (filled/cancelled) orders
        # Because sometimes an order fills INSTANTLY before we can even resolve the ID

        for i in range(retries):
            try:
                # 1. Search Active Orders first
                orders_resp = await self._sdk_call_with_retry(
                    lambda: self._order_api.account_active_orders(
                        account_index=self._account_index, market_id=market_index, auth=self._auth_token
                    )
                )
                orders = getattr(orders_resp, "orders", []) or []

                for o in orders:
                    idx = getattr(o, "client_order_index", None)
                    if idx == client_order_index:
                        resolved = getattr(o, "order_id", None) or getattr(o, "order_index", None)
                        if resolved:
                            self._client_order_id_map[str(client_order_index)] = str(resolved)
                        return resolved

                # 2. Search Inactive Orders (filled/cancelled) if not active
                # In active trading there can be many inactive orders; `limit=5` can miss our
                # client index and cause repeated pending_* cancel failures.
                inactive_resp = await self._sdk_call_with_retry(
                    lambda: self._order_api.account_inactive_orders(
                        account_index=self._account_index, market_id=market_index, limit=20, auth=self._auth_token
                    )
                )
                inactive_orders = getattr(inactive_resp, "orders", []) or []

                for o in inactive_orders:
                    idx = getattr(o, "client_order_index", None)
                    if idx == client_order_index:
                        logger.info(f"Resolved order {client_order_index} in inactive orders (likely instant fill)")
                        resolved = getattr(o, "order_id", None) or getattr(o, "order_index", None)
                        if resolved:
                            self._client_order_id_map[str(client_order_index)] = str(resolved)
                        return resolved

                # Wait a bit before retry if not found
                if i < retries - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.warning(f"Error resolving order ID: {e}")
                if i < retries - 1:
                    await asyncio.sleep(0.5)

        return None

    async def _place_order_rest(self, request: OrderRequest) -> Order:
        """Place an order using legacy REST API."""
        logger.info(
            f"Placing Lighter REST order: {request.side.value} {request.qty} {request.symbol} "
            f"@ {request.price or 'MARKET'}"
        )

        try:
            # Build order payload
            payload = {
                "symbol": request.symbol,
                "side": request.side.value.lower(),
                "size": str(request.qty),
                "order_type": request.order_type.value.lower(),
                "reduce_only": request.reduce_only,
            }

            if request.price and request.order_type == OrderType.LIMIT:
                payload["price"] = str(request.price)

            if request.time_in_force:
                tif_map = {
                    TimeInForce.GTC: "gtc",
                    TimeInForce.IOC: "ioc",
                    TimeInForce.POST_ONLY: "post_only",
                    TimeInForce.FOK: "fok",
                }
                payload["time_in_force"] = tif_map.get(request.time_in_force, "gtc")

            if request.client_order_id:
                payload["client_order_id"] = request.client_order_id

            # Submit order
            data = await self._request("POST", "/api/v1/orders", json=payload)

            # Parse response
            order_id = str(data.get("order_id", ""))
            status_str = data.get("status", "pending").upper()

            try:
                status = OrderStatus(status_str)
            except ValueError:
                status = OrderStatus.PENDING

            order = Order(
                order_id=order_id,
                symbol=request.symbol,
                exchange=Exchange.LIGHTER,
                side=request.side,
                order_type=request.order_type,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                reduce_only=request.reduce_only,
                status=status,
                filled_qty=_safe_decimal(data.get("filled_qty")),
                avg_fill_price=_safe_decimal(data.get("avg_fill_price")),
                fee=_safe_decimal(data.get("fee")),
                client_order_id=request.client_order_id,
            )

            logger.info(f"Lighter order placed: {order_id} status={status}")
            return order

        except ExchangeError:
            raise
        except Exception as e:
            logger.exception(f"Failed to place Lighter order: {e}")
            raise OrderRejectedError(
                f"Order rejected: {e}",
                exchange="LIGHTER",
                symbol=request.symbol,
            ) from e

    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """
        Get order status by ID.

        Since Lighter does not have a direct "get_order_by_id" endpoint in the SDK/API,
        we must search for the order in:
        1. Active orders (open/partial)
        2. Inactive orders (filled/cancelled)
        """
        # Guard against None/empty order_id
        if not order_id:
            logger.warning(f"get_order called with empty order_id for {symbol}")
            return None

        # 🚀 PERFORMANCE: Check WebSocket fill cache FIRST (instant lookup)
        # Avoids 2-5s REST API timeout for orders that were already filled via WS.
        # Cache is populated by _on_ws_account_update when order reaches terminal state.
        cached = await self._get_cached_fill(order_id, symbol)
        if cached:
            return cached  # ⚡ Instant return (~5ms vs 2-5s REST search)
        # Handle pending IDs (from signed placement)
        if order_id.startswith("pending_"):
            parts = order_id.split("_")
            if len(parts) >= 3:
                try:
                    client_index = int(parts[1])
                    market_index = int(parts[2])
                    cached = self._client_order_id_map.get(str(client_index))
                    if cached:
                        order_id = cached
                    else:
                        resolved_id = await self._resolve_order_id(client_index, market_index, retries=1)
                        if resolved_id:
                            order_id = str(resolved_id)
                        else:
                            # Still pending/unknown
                            return Order(
                                order_id=order_id,
                                symbol=symbol,
                                exchange=Exchange.LIGHTER,
                                side=Side.BUY,  # Unknown
                                order_type=OrderType.LIMIT,  # Unknown
                                qty=Decimal("0"),
                                price=Decimal("0"),
                                status=OrderStatus.PENDING,
                            )
                except ValueError:
                    pass

        try:
            # We need market_id for the SDK calls
            market_index = self._get_market_index(symbol)
            if not market_index:
                # If we don't know the market, we can't search efficienctly
                logger.warning(f"Cannot get_order {order_id}: unknown market for symbol {symbol}")
                return None

            # 1. Search Active Orders
            if self._order_api:
                try:
                    active_resp = await self._sdk_call_with_retry(
                        lambda: self._order_api.account_active_orders(
                            account_index=self._account_index, market_id=market_index, auth=self._auth_token
                        )
                    )
                    active_orders = getattr(active_resp, "orders", []) or []
                    for o in active_orders:
                        # Check both order_id and order_index
                        oid = str(getattr(o, "order_id", "") or getattr(o, "order_index", ""))
                        if oid == order_id:
                            mapped = self._map_sdk_order_to_model(o, symbol, OrderStatus.OPEN)
                            return await self._enrich_order_fee(symbol, mapped)
                except Exception as e:
                    logger.warning(f"Failed to fetch active orders during get_order: {e}")

                # 2. Search Inactive Orders (Recent history)
                try:
                    # Fetch recent inactive orders (limit=20 should be enough for immediate verification)
                    inactive_resp = await self._sdk_call_with_retry(
                        lambda: self._order_api.account_inactive_orders(
                            account_index=self._account_index, market_id=market_index, limit=20, auth=self._auth_token
                        )
                    )
                    inactive_orders = getattr(inactive_resp, "orders", []) or []
                    for o in inactive_orders:
                        oid = str(getattr(o, "order_id", "") or getattr(o, "order_index", ""))
                        if oid == order_id:
                            # Inactive means executed or cancelled.
                            # We can try to infer status or default to FILLED/CANCELLED based on qty
                            # But usually inactive orders in Lighter are filled or cancelled.
                            # The SDK object might have a status string.
                            mapped = self._map_sdk_order_to_model(o, symbol, None)
                            return await self._enrich_order_fee(symbol, mapped)
                except Exception as e:
                    logger.warning(f"Failed to fetch inactive orders during get_order: {e}")

            # Fallback: If we can't find it, we can't verify it.
            # We DONT use the old REST endpoint because it 404s.
            logger.warning(f"Order {order_id} not found in active or recent inactive orders")
            return None

        except Exception as e:
            logger.warning(f"Failed to get order {order_id}: {e}")
            return None

    async def _cache_filled_order(self, order: Order) -> None:
        """
        Cache filled/cancelled order for instant get_order() lookups.

        Avoids slow REST API searches (2-5s timeout). Cache entries include
        timestamp for TTL-based cleanup.
        """
        now = time.time()
        # Cache by client ID (if available) - usually the preferred key for pending lookups
        if order.client_order_id:
            async with self._ws_fill_cache_lock:
                self._ws_fill_cache[str(order.client_order_id)] = (order, now)

        # Cache by system ID (if available) - used when polling via resolved order ID
        if order.order_id and str(order.order_id) != str(order.client_order_id):
            async with self._ws_fill_cache_lock:
                self._ws_fill_cache[str(order.order_id)] = (order, now)

    async def _cleanup_stale_fill_cache(self) -> int:
        """
        Remove stale entries from fill cache based on TTL.

        Called periodically from market data refresh loop.

        Returns:
            Number of entries removed.
        """
        now = time.time()
        async with self._ws_fill_cache_lock:
            stale_keys = [
                k for k, (_, ts) in self._ws_fill_cache.items()
                if now - ts > self._ws_fill_cache_ttl_seconds
            ]
            for k in stale_keys:
                del self._ws_fill_cache[k]
            if stale_keys:
                logger.debug(f"Cleaned up {len(stale_keys)} stale fill cache entries")
            return len(stale_keys)

    async def _get_cached_fill(self, order_id: str, symbol: str) -> Order | None:
        """
        Check WebSocket fill cache for instant order lookup.

        Returns cached order if found and not expired, None otherwise.
        """
        lookup_ids = [str(order_id)]

        # Extract client_order_id from pending_* format
        if order_id.startswith("pending_"):
            parts = order_id.split("_")
            if len(parts) >= 3:
                lookup_ids.append(parts[1])  # Extract client_index

        now = time.time()
        async with self._ws_fill_cache_lock:
            for lid in lookup_ids:
                entry = self._ws_fill_cache.get(lid)
                if entry:
                    cached, ts = entry
                    # Check TTL
                    if now - ts > self._ws_fill_cache_ttl_seconds:
                        continue  # Expired, skip
                    if cached.symbol == symbol:
                        logger.debug(
                            f"⚡ WS Fill Cache HIT for {symbol} order {order_id} (key={lid}) "
                            f"(status={cached.status}, filled={cached.filled_qty}/{cached.qty})"
                        )
                        return cached

        return None

    def _map_sdk_order_to_model(self, sdk_order: Any, symbol: str, default_status: OrderStatus | None) -> Order:
        """Map SDK order object to domain Order model."""

        # ID
        order_id = str(getattr(sdk_order, "order_id", "") or getattr(sdk_order, "order_index", ""))

        # Status
        # Some SDK objects have 'status' field, others don't.
        status = default_status
        if hasattr(sdk_order, "status"):
            s = str(sdk_order.status).lower()
            if s == "open":
                status = OrderStatus.OPEN
            elif s == "filled":
                status = OrderStatus.FILLED
            elif s == "canceled" or s == "cancelled":
                status = OrderStatus.CANCELLED
            elif s == "partially_filled":
                status = OrderStatus.PARTIALLY_FILLED
            elif s == "pending":
                status = OrderStatus.PENDING
            elif "canceled" in s:
                status = OrderStatus.CANCELLED

        # Infer status from remaining size if not set or ambiguous
        # Note: SDK uses strings for amounts (StrictStr)
        rem_str = getattr(sdk_order, "remaining_base_amount", None)
        rem = _safe_decimal(rem_str) if rem_str is not None else None

        if not status:
            if rem is not None and rem == 0 and hasattr(sdk_order, "filled_base_amount"):
                status = OrderStatus.FILLED
            else:
                status = OrderStatus.OPEN

        # Side
        side = Side.BUY
        if hasattr(sdk_order, "is_ask"):
            if sdk_order.is_ask:
                side = Side.SELL
        elif hasattr(sdk_order, "side") and str(sdk_order.side).lower() == "sell":
            side = Side.SELL

        # Qty & Price
        qty = _safe_decimal(getattr(sdk_order, "initial_base_amount", 0))
        price = _safe_decimal(getattr(sdk_order, "price", 0))

        filled = _safe_decimal(getattr(sdk_order, "filled_base_amount", 0))

        # Avg Fill Price
        avg_price = _safe_decimal(
            getattr(sdk_order, "avg_filled_price", None) or getattr(sdk_order, "avg_fill_price", None)
        )
        if avg_price <= 0 and filled > 0:
            quote_filled = _safe_decimal(getattr(sdk_order, "filled_quote_amount", 0))
            if quote_filled > 0:
                avg_price = quote_filled / filled
        if avg_price <= 0 and price > 0:
            avg_price = price
        avg_price = _clamp_avg_fill_price(avg_price, price)

        return Order(
            order_id=order_id,
            symbol=symbol,
            exchange=Exchange.LIGHTER,
            side=side,
            order_type=OrderType.LIMIT,
            qty=qty,
            price=price,
            status=status or OrderStatus.OPEN,
            filled_qty=filled,
            avg_fill_price=avg_price,
            fee=Decimal("0"),  # Enriched via trades endpoint when possible
            client_order_id=str(getattr(sdk_order, "client_order_index", "")),
        )

    async def _enrich_order_fee(self, symbol: str, order: Order) -> Order:
        """
        Populate `Order.fee` when the SDK order object doesn't include it.

        Lighter order listing endpoints don't include per-order fee. We use
        `/api/v1/trades` (SDK: `OrderApi.trades`) filtered by `order_index` to
        infer the fee paid by this account as maker/taker.
        """
        try:
            if order.exchange != Exchange.LIGHTER:
                return order
            if order.filled_qty <= 0:
                return order

            cached = self._order_fee_cache.get(order.order_id)
            if cached is not None:
                cached_fee, cached_filled_qty = cached
                # If the filled_qty hasn't increased since the last calc, reuse the cached fee.
                # For partially filled orders, allow refresh if filled_qty grows.
                if order.filled_qty <= cached_filled_qty:
                    order.fee = cached_fee
                    return order

            if not self._order_api:
                return order

            try:
                order_index = int(order.order_id)
            except Exception:
                return order

            market_id = self._get_market_index(symbol)
            if market_id < 0:
                return order

            trades_resp = await self._sdk_call_with_retry(
                lambda: self._order_api.trades(
                    sort_by="timestamp",
                    limit=100,
                    market_id=market_id,
                    account_index=self._account_index,
                    order_index=order_index,
                    sort_dir="desc",
                    auth=self._auth_token,
                )
            )

            trades = getattr(trades_resp, "trades", []) or []
            fees = await self.get_fee_schedule(symbol)
            maker_rate = _safe_decimal(fees.get("maker_fee"), Decimal("0"))
            taker_rate = _safe_decimal(fees.get("taker_fee"), Decimal("0"))

            if not trades:
                notional = abs(order.filled_qty * (order.avg_fill_price or Decimal("0")))
                est = notional * maker_rate if notional > 0 else Decimal("0")
                self._order_fee_cache[order.order_id] = (est, order.filled_qty)
                order.fee = est
                return order

            fee_int_sum = 0
            est_fee_sum = Decimal("0")
            notional_sum = Decimal("0")

            for t in trades:
                t_type = str(getattr(t, "type", "") or "")
                if t_type and t_type != "trade":
                    continue

                usd_amount = _safe_decimal(getattr(t, "usd_amount", 0))
                notional_sum += abs(usd_amount)

                ask_acct = getattr(t, "ask_account_id", None)
                bid_acct = getattr(t, "bid_account_id", None)
                is_maker_ask = bool(getattr(t, "is_maker_ask", False))

                fee_int: int | None = None
                role_rate = maker_rate

                if ask_acct == self._account_index:
                    if is_maker_ask:
                        fee_int = getattr(t, "maker_fee", None)
                        role_rate = maker_rate
                    else:
                        fee_int = getattr(t, "taker_fee", None)
                        role_rate = taker_rate
                elif bid_acct == self._account_index:
                    if is_maker_ask:
                        fee_int = getattr(t, "taker_fee", None)
                        role_rate = taker_rate
                    else:
                        fee_int = getattr(t, "maker_fee", None)
                        role_rate = maker_rate
                else:
                    continue

                if fee_int is not None:
                    with contextlib.suppress(Exception):
                        fee_int_sum += int(fee_int)

                est_fee_sum += abs(usd_amount) * role_rate

            if fee_int_sum <= 0:
                self._order_fee_cache[order.order_id] = (est_fee_sum, order.filled_qty)
                order.fee = est_fee_sum
                return order

            # ✅ FIX: Lighter SDK returns maker_fee/taker_fee as INT scaled by 1e6 (USDC micro-units).
            # This is the standard USDC decimals (6) on Starknet/Layer-2 chains.
            # Empirical verification against Lighter CSV export confirmed scale = 1,000,000:
            #   - CSV fee: $0.0023
            #   - SDK fee_int: 2300
            #   - 2300 / 1000000 = 0.0023 ✅
            # Previous "guessing" logic incorrectly chose 1e8, yielding $0.000023 (100x too small).
            LIGHTER_FEE_SCALE = Decimal("1000000")  # 1e6 = USDC micro-units (6 decimals)
            fee_usd = Decimal(fee_int_sum) / LIGHTER_FEE_SCALE

            # Sanity check: Fee should not exceed 5% of notional (protects against corruption)
            if notional_sum > 0 and abs(fee_usd) > notional_sum * Decimal("0.05"):
                logger.warning(
                    f"Lighter fee sanity check failed: fee_usd={fee_usd}, notional={notional_sum}, "
                    f"fee_int_sum={fee_int_sum}. Falling back to estimated fee."
                )
                fee_final = est_fee_sum
            else:
                fee_final = fee_usd

            self._order_fee_cache[order.order_id] = (fee_final, order.filled_qty)
            order.fee = fee_final
            return order

        except Exception:
            return order

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order using SignerClient (preferred) or REST fallback."""
        if self._signer:
            return await self._cancel_order_signed(symbol, order_id)
        return await self._cancel_order_rest(symbol, order_id)

    async def _cancel_order_signed(self, symbol: str, order_id: str) -> bool:
        """Cancel order using SignerClient."""
        try:
            # Guard against None/empty order_id
            if not order_id:
                logger.warning(f"cancel_order called with empty order_id for {symbol}")
                return False
            # Handle pending order ID
            if order_id.startswith("pending_"):
                logger.warning(f"Cannot cancel pending order {order_id} via ID - attempting resolve")
                parts = order_id.split("_")
                if len(parts) >= 3:
                    try:
                        client_index = int(parts[1])
                        market_index = int(parts[2])
                        cached = self._client_order_id_map.get(str(client_index))
                        if cached:
                            order_id = cached
                        else:
                            resolved = await self._resolve_order_id(client_index, market_index)
                            if resolved:
                                order_id = str(resolved)
                            else:
                                # Fallback: allow cancelling by client index (mirrors `.tmp` tools).
                                logger.warning(
                                    "Could not resolve pending order ID for cancellation; "
                                    "attempting cancel via client index"
                                )
                                order_id = str(client_index)
                    except ValueError:
                        return False

            market_index = self._get_market_index(symbol)
            try:
                order_index = int(order_id)
            except ValueError:
                logger.warning(f"Invalid order_id format for cancellation: {order_id}")
                return False

            tx_hash: str | None = None
            ws_used = False

            if self._use_ws_orders and await self.ensure_trading_ws(timeout=self._trading_ws_connect_timeout_seconds):
                try:
                    tx_hash, _ws_resp = await self._ws_submit_signed_tx(
                        purpose="cancel_order",
                        signer_fn=lambda api_key_index, nonce: self._signer.sign_cancel_order(
                            market_index=market_index,
                            order_index=order_index,
                            nonce=nonce,
                            api_key_index=api_key_index,
                        ),
                        timeout=self._trading_ws_response_timeout_seconds,
                    )
                    ws_used = True
                except Exception as e:
                    logger.warning(f"Lighter WS cancel submission failed; falling back to HTTP sendTx: {e}")

            if not ws_used:
                _, resp_tx, err = await self._sdk_call_with_retry(
                    lambda: self._signer.cancel_order(
                        market_index=market_index,
                        order_index=order_index,
                        nonce=-1,
                        api_key_index=self._api_key_index,
                    )
                )

                if err:
                    logger.warning(f"Failed to cancel order {order_id}: {err}")
                    return False

                tx_hash = str(getattr(resp_tx, "tx_hash", "") or "")

            logger.info(f"Cancelled Lighter order {order_id}. Tx: {tx_hash} ws_sendtx={ws_used}")
            return True

        except Exception as e:
            logger.warning(f"Error cancelling order {order_id}: {e}")
            return False

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        price: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> bool:
        """
        Modify usage SignerClient.modify_order (Atomic Cancel+Replace).

        Supports modifying price or qty (or both). Lighter SDK supports base_amount and price updates.
        """
        if not self._signer:
            return False

        # Only Price modification is strictly supported by our logic right now,
        # but Lighter SDK allows qty too. We default to current values if None?
        # Requires knowing current values. Domain interface says optional.
        # IF price is None and qty is None -> No op.

        if price is None and qty is None:
            return True

        try:
            # 1. Resolve Order ID (System ID preferred, client ID if needed)
            # Guard against None/empty order_id
            if not order_id:
                logger.warning(f"modify_order called with empty order_id for {symbol}")
                return False
            client_index_int = None
            order_index_int = None

            if order_id.startswith("pending_"):
                parts = order_id.split("_")
                if len(parts) >= 2:
                    with contextlib.suppress(ValueError):
                        client_index_int = int(parts[1])
            else:
                with contextlib.suppress(ValueError):
                    order_index_int = int(order_id)

            # If we only have client_index, try to resolve to system index,
            # BUT Lighter SDK might support client_index passing?
            # Looking at .tmp, they pass client_order_index to order_index param.
            # Let's try to resolve to system ID first to be safe, as 'modify_order' usually implies system ID.
            # If resolution fails, we can try passing the client index if it looks like one.

            market_index = self._get_market_index(symbol)

            final_order_index = order_index_int
            if final_order_index is None and client_index_int is not None:
                # Try to resolve or fallback
                # Check cache first
                cached = self._client_order_id_map.get(str(client_index_int))
                if cached:
                    final_order_index = int(cached)
                else:
                    # Active resolution
                    resolved = await self._resolve_order_id(client_index_int, market_index, retries=1)
                    # Fallback to client_index_int if resolution fails
                    final_order_index = resolved or client_index_int

            if final_order_index is None:
                logger.warning(f"Cannot modify order {order_id}: could not resolve to integer index")
                return False

            # 2. Need Quantity if not provided?
            # Lighter SDK modify_order takes (base_amount, price). BOTH are required arguments usually?
            # Let's check signature in .tmp usage:
            # await self.lighter_client.modify_order(market_index, order_index, base_amount, price, trigger_price)
            # So we NEED base_amount.
            final_qty_int = 0
            final_price_int = 0

            # Get market info for precision
            market_info = await self.get_market_info(symbol)
            if not market_info:
                return False

            if qty is None or price is None:
                # We need to fetch the current order to fill in missing values
                current_order = await self.get_order(symbol, order_id)
                if not current_order:
                    logger.warning(f"Cannot modify order {order_id}: order not found to fill missing params")
                    return False

                target_qty = qty if qty is not None else current_order.qty
                target_price = price if price is not None else current_order.price
            else:
                target_qty = qty
                target_price = price

            # Convert to ints
            final_price_int = int(target_price * (Decimal("10") ** market_info.price_precision))
            final_qty_int = int(target_qty * (Decimal("10") ** market_info.qty_precision))

            logger.info(
                f"Modifying Lighter order {final_order_index} ({symbol}): "
                f"New Price={target_price}, New Qty={target_qty}"
            )

            tx_hash: str | None = None
            ws_used = False

            if self._use_ws_orders and await self.ensure_trading_ws(timeout=self._trading_ws_connect_timeout_seconds):
                try:
                    tx_hash, _ws_resp = await self._ws_submit_signed_tx(
                        purpose="modify_order",
                        signer_fn=lambda api_key_index, nonce: self._signer.sign_modify_order(
                            market_index=market_index,
                            order_index=final_order_index,
                            base_amount=final_qty_int,
                            price=final_price_int,
                            trigger_price=0,
                            nonce=nonce,
                            api_key_index=api_key_index,
                        ),
                        timeout=self._trading_ws_response_timeout_seconds,
                    )
                    ws_used = True
                except Exception as e:
                    logger.warning(f"Lighter WS modify submission failed; falling back to HTTP sendTx: {e}")

            if not ws_used:
                # 3. Request (HTTP /api/v1/sendTx via SDK)
                _, resp_tx, err = await self._sdk_call_with_retry(
                    lambda: self._signer.modify_order(
                        market_index=market_index,
                        order_index=final_order_index,
                        base_amount=final_qty_int,
                        price=final_price_int,
                        trigger_price=0,
                        nonce=-1,
                        api_key_index=self._api_key_index,
                    )
                )

                if err:
                    logger.warning(f"Failed to modify order {order_id}: {err}")
                    return False

                tx_hash = str(getattr(resp_tx, "tx_hash", "") or "")

            logger.info(f"Modified Lighter order {final_order_index}. Tx: {tx_hash} ws_sendtx={ws_used}")
            return True

        except Exception as e:
            logger.warning(f"Error modifying order {order_id}: {e}")
            return False

    async def _cancel_order_rest(self, symbol: str, order_id: str) -> bool:
        """Cancel order using legacy REST API."""
        try:
            await self._request("DELETE", f"/api/v1/orders/{order_id}")
            logger.info(f"Cancelled Lighter order {order_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """Cancel all open orders."""
        if self._signer:
            # SDK supports cancel_all but requires timestamp
            # Currently implementing via iterative cancel for safety/simplicity with symbol filter
            # Or use SignerClient.cancel_all_orders which cancels EVERYTHING for account

            # If symbol provided, we must fetch and cancel individually
            if symbol:
                orders = await self.get_open_orders(symbol)
                count = 0
                for o in orders:
                    if await self.cancel_order(symbol, o.order_id):
                        count += 1
                return count
            else:
                # Cancel everything (account-wide).
                # Per SDK example: for immediate cancel-all use timestamp_ms=0 (not "now").
                _, resp_tx, err = await self._sdk_call_with_retry(
                    lambda: self._signer.cancel_all_orders(
                        time_in_force=self._signer.CANCEL_ALL_TIF_IMMEDIATE,
                        timestamp_ms=0,
                        nonce=-1,
                        api_key_index=self._api_key_index,
                    )
                )
                if err:
                    logger.warning(f"Failed to cancel all orders via signer: {err}")
                else:
                    logger.info(f"Cancelled all orders via signer. Tx: {resp_tx.tx_hash}")
                    return 1  # batch

        # Fallback REST
        try:
            params = {}
            if symbol:
                params["symbol"] = symbol

            data = await self._request(
                "DELETE",
                "/api/v1/orders",
                params=params if params else None,
            )

            cancelled = data.get("cancelled", 0)
            logger.info(f"Cancelled {cancelled} Lighter orders")
            return cancelled

        except Exception as e:
            logger.warning(f"Failed to cancel all orders: {e}")
            return 0

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Fetch open orders helper."""
        # This is needed for cancel_all(symbol) with signer
        if not self._order_api:
            return []

        try:
            market_id = self._get_market_index(symbol) if symbol else None
            # If no symbol, we iterate all markets? Too expensive.
            # Lighter API account_active_orders requires market_id.

            orders = []
            markets_to_check = [market_id] if market_id is not None else self._market_indices.values()

            for mid in markets_to_check:
                resp = await self._sdk_call_with_retry(
                    lambda mid=mid: self._order_api.account_active_orders(
                        account_index=self._account_index, market_id=mid, auth=self._auth_token
                    )
                )
                raw_orders = getattr(resp, "orders", []) or []
                for o in raw_orders:
                    # Convert to Order domain model
                    # ... simplified conversion ...
                    oid = str(getattr(o, "order_id", getattr(o, "order_index", "")))
                    orders.append(
                        Order(
                            order_id=oid,
                            symbol=symbol or "UNKNOWN",  # We need reverse map for symbol if iterating
                            exchange=Exchange.LIGHTER,
                            side=Side.BUY,  # Mock, we just need ID for cancel
                            order_type=OrderType.LIMIT,
                            qty=Decimal("0"),
                            price=Decimal("0"),
                            status=OrderStatus.OPEN,
                        )
                    )
            return orders
        except Exception:
            return []

    # =========================================================================
    # WebSocket
    # =========================================================================

    async def subscribe_positions(self, callback: Callable[[Position], Awaitable[None]]) -> None:
        """Subscribe to position updates."""
        self._position_callbacks.append(callback)
        if not self._ws_task and self._ws_client_cls:
            await self._start_websocket()

    async def subscribe_orders(self, callback: Callable[[Order], Awaitable[None]]) -> None:
        """Subscribe to order updates."""
        self._order_callbacks.append(callback)
        if not self._ws_task and self._ws_client_cls:
            await self._start_websocket()

    async def subscribe_orderbook_l1(
        self,
        symbols: list[str] | None = None,
        *,
        depth: int | None = None,
    ) -> None:
        """
        Subscribe to public orderbook updates and keep L1 cache hot.

        IMPORTANT: We do NOT subscribe to dozens of markets in a single connection.
        Lighter will disconnect with `Too Many Websocket Messages` (30009) when subscribing to many orderbooks.
        Instead we follow `.tmp`: per-symbol `order_book/{market}` streams started on-demand.
        """
        _ = depth  # depth is not supported by Lighter order_book WS; best-effort only
        if not symbols:
            return
        for sym in list(symbols):
            with contextlib.suppress(Exception):
                await self.ensure_orderbook_ws(sym, timeout=0.0)

    def _get_ob_ws_ready_event(self, market_id: int) -> asyncio.Event:
        ev = self._ob_ws_ready.get(market_id)
        if ev is None:
            ev = asyncio.Event()
            self._ob_ws_ready[market_id] = ev
        return ev

    def _ob_ws_reset_market(self, market_id: int) -> None:
        """Reset local orderbook state for a market."""
        # Remove from books to force re-creation as LocalOrderbook in the loop
        self._ob_ws_books.pop(market_id, None)

        # Legacy tracking cleanup
        self._ob_ws_last_nonce.pop(market_id, None)
        self._ob_ws_last_offset.pop(market_id, None)

        self._ob_ws_snapshot_loaded.discard(market_id)
        self._get_ob_ws_ready_event(market_id).clear()

    def _prune_orderbook_ws_tasks_locked(self, *, keep_market_id: int | None = None) -> None:
        """Prune stale or excess per-market orderbook WS tasks (lock must be held)."""
        now = time.monotonic()
        ttl = float(self._ob_ws_ttl_seconds or 0)

        stale: list[int] = []
        for mid, task in list(self._ob_ws_tasks.items()):
            if task.done():
                stale.append(mid)
                continue
            if ttl > 0:
                last_used = self._ob_ws_last_used.get(mid)
                if last_used is not None and (now - last_used) > ttl:
                    stale.append(mid)

        for mid in stale:
            task = self._ob_ws_tasks.pop(mid, None)
            if task:
                with contextlib.suppress(Exception):
                    task.cancel()
            self._ob_ws_last_used.pop(mid, None)
            self._ob_ws_reset_market(mid)

        max_conn = int(self._ob_ws_max_connections or 0)
        if max_conn > 0 and len(self._ob_ws_tasks) >= max_conn:
            # Evict least-recently-used tasks (excluding the requested market).
            candidates: list[tuple[float, int]] = []
            for mid in self._ob_ws_tasks:
                if keep_market_id is not None and mid == keep_market_id:
                    continue
                last_used = self._ob_ws_last_used.get(mid, 0.0)
                candidates.append((last_used, mid))

            for _last_used, mid in sorted(candidates):
                if len(self._ob_ws_tasks) < max_conn:
                    break
                task = self._ob_ws_tasks.pop(mid, None)
                if task:
                    with contextlib.suppress(Exception):
                        task.cancel()
                self._ob_ws_last_used.pop(mid, None)
                self._ob_ws_reset_market(mid)

    async def ensure_orderbook_ws(self, symbol: str, *, timeout: float) -> bool:
        """
        Ensure a per-symbol `order_book/{market}` WS is running and snapshot is loaded.

        This mirrors `.tmp` behavior: subscribe only to the market we need right now.
        """
        if not symbol:
            return False
        if not self._markets:
            with contextlib.suppress(Exception):
                await self.load_markets()
        market_id = self._get_market_index(symbol)
        if market_id < 0:
            return False
        self._ob_ws_last_used[market_id] = time.monotonic()

        async with self._ob_ws_lock:
            self._prune_orderbook_ws_tasks_locked(keep_market_id=market_id)
            task = self._ob_ws_tasks.get(market_id)
            if task is None or task.done():
                self._ob_ws_reset_market(market_id)
                self._ob_ws_tasks[market_id] = asyncio.create_task(
                    self._orderbook_ws_loop(market_id),
                    name=f"lighter_ob_ws_{market_id}",
                )
                logger.info(f"Lighter orderbook WS started (on-demand): symbol={symbol} market_id={market_id}")

        if timeout <= 0:
            return True

        ev = self._get_ob_ws_ready_event(market_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.0, float(timeout)))
        except TimeoutError:
            return False
        return ev.is_set()

    async def _orderbook_ws_loop(self, market_id: int) -> None:
        """
        Per-market orderbook WS loop.

        - Subscribes to `order_book/{market_id}`
        - Waits for snapshot (`subscribed/order_book`) before applying updates
        - On continuity issues: unsubscribe/subscribe to request fresh snapshot (like `.tmp`)
        - Uses circuit breaker to prevent connection storms
        - Uses health monitor to detect unhealthy connections
        - Uses exponential backoff with jitter for reconnects
        """
        try:
            import websockets
        except Exception:
            return

        # Get jitter_factor from settings (default 15%)
        jitter_factor = float(
            getattr(getattr(self.settings, "websocket", None), "reconnect_jitter_factor", 0.15) or 0.15
        )
        base_delay = 1.0
        max_delay = 30.0

        ws_url = self._get_ws_stream_url()
        sub_channel = f"order_book/{int(market_id)}"
        symbol = self._market_id_to_symbol.get(int(market_id), "")

        # Initialize circuit breaker and health monitor for this market
        if market_id not in self._ob_ws_circuit_breaker:
            self._ob_ws_circuit_breaker[market_id] = self._ob_ws_create_circuit_breaker()
        if market_id not in self._ob_ws_health_monitor:
            self._ob_ws_health_monitor[market_id] = self._ob_ws_create_health_monitor()

        circuit_breaker = self._ob_ws_circuit_breaker[market_id]
        health_monitor = self._ob_ws_health_monitor[market_id]

        attempt = 0
        while True:
            # Check circuit breaker before attempting connection
            if not circuit_breaker.should_attempt_connection():
                cooldown_remaining = circuit_breaker.cooldown - (time.monotonic() - circuit_breaker.last_failure_time)
                logger.debug(
                    f"Lighter orderbook WS circuit breaker ACTIVE for market_id={market_id}. "
                    f"Cooldown remaining: {cooldown_remaining:.1f}s"
                )
                await asyncio.sleep(5)  # Wait before checking again
                continue

            try:
                self._ob_ws_reset_market(market_id)

                # 🚀 PERFORMANCE: Optimized ping settings (consistent with trading WS)
                async with websockets.connect(ws_url, ping_interval=15, ping_timeout=10) as ws:  # type: ignore[attr-defined]
                    # Connection successful - record in circuit breaker
                    circuit_breaker.record_success()
                    attempt = 0  # Reset attempt counter on successful connection

                    await ws.send(json.dumps({"type": "subscribe", "channel": sub_channel}))
                    self._ob_ws_last_used[market_id] = time.monotonic()

                    while True:
                        raw = await ws.recv()
                        if not raw:
                            continue
                        self._ob_ws_last_used[market_id] = time.monotonic()
                        health_monitor.record_message()
                        data = json.loads(raw) if isinstance(raw, str) else raw

                        if isinstance(data, dict) and data.get("type") == "ping":
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"type": "pong"}))
                            continue

                        # Handle server-side error payloads (avoid crashing the whole adapter).
                        if isinstance(data, dict) and data.get("error"):
                            err = data.get("error") or {}
                            logger.warning(f"Lighter orderbook WS error payload: market_id={market_id} err={err}")
                            health_monitor.record_error()
                            break

                        msg_type = data.get("type") if isinstance(data, dict) else None

                        # Get or Create LocalOrderbook
                        if market_id not in self._ob_ws_books:
                            self._ob_ws_books[market_id] = LocalOrderbook(symbol, market_id)
                        book = self._ob_ws_books[market_id]

                        if msg_type == "subscribed/order_book":
                            ob = data.get("order_book") if isinstance(data, dict) else None
                            if not isinstance(ob, dict):
                                continue

                            book.apply_snapshot(ob)

                            self._ob_ws_snapshot_loaded.add(market_id)
                            self._get_ob_ws_ready_event(market_id).set()
                            self._ob_ws_update_cache_from_book(market_id)
                            continue

                        if msg_type == "update/order_book":
                            if market_id not in self._ob_ws_snapshot_loaded:
                                continue
                            ob = data.get("order_book") if isinstance(data, dict) else None
                            if not isinstance(ob, dict):
                                continue

                            # Delegate to `LocalOrderbook` for validation and update
                            try:
                                book.apply_update(ob)
                                self._ob_ws_update_cache_from_book(market_id)
                            except ExchangeError as e:
                                # Gap/Integrity Error -> Resync
                                logger.warning(f"Lighter WS Integrity Error: {e}. Resynchronizing...")
                                health_monitor.record_error()
                                self._mark_ws_orderbook_stale(symbol)
                                self._ob_ws_snapshot_loaded.discard(market_id)
                                self._get_ob_ws_ready_event(market_id).clear()
                                with contextlib.suppress(Exception):
                                    await ws.send(json.dumps({"type": "unsubscribe", "channel": sub_channel}))
                                    await asyncio.sleep(0.5)
                                    await ws.send(json.dumps({"type": "subscribe", "channel": sub_channel}))
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Lighter orderbook WS error: market_id={market_id} err={e}")
                health_monitor.record_error()
                circuit_breaker.record_failure()

            # Enhanced exponential backoff with jitter
            delay = _get_ws_reconnect_delay(attempt, base_delay, max_delay, jitter_factor)
            logger.debug(
                f"Lighter orderbook WS reconnecting market_id={market_id} after {delay:.1f}s (attempt {attempt + 1})"
            )
            await asyncio.sleep(delay)
            attempt += 1

    async def _start_websocket(self) -> None:
        """Start WebSocket client task."""
        if self._ws_task:
            return

        self._ws_account_ready.clear()
        self._ws_account_ready_logged = False
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("Lighter WebSocket task started")

    def _get_ws_stream_url(self) -> str:
        base = str(self._base_url or "").strip()
        if not base:
            base = "https://mainnet.zklighter.elliot.ai"
        base = base.replace("https://", "").replace("http://", "")
        base = base.strip().strip("/")
        return f"wss://{base}/stream"

    def _get_orders_ws_market_event(self, market_index: int) -> asyncio.Event:
        ev = self._orders_ws_market_ready.get(market_index)
        if ev is None:
            ev = asyncio.Event()
            self._orders_ws_market_ready[market_index] = ev
        return ev

    def _clear_orders_ws_market_ready(self) -> None:
        # Reset per-market readiness so callers don't treat stale subscriptions as "ready"
        # across disconnects/reconnects.
        for ev in list(self._orders_ws_market_ready.values()):
            with contextlib.suppress(Exception):
                ev.clear()

    async def _start_orders_websocket(self) -> None:
        """
        Start dedicated `account_orders` WS loop (auth + per-market subscriptions).

        This is separate from the SDK WsClient `account_all` stream because `update/account_orders`
        is the fastest way to get per-order fills (as used by `.tmp` tools).
        """
        if self._orders_ws_task:
            return

        self._orders_ws_ready.clear()
        self._clear_orders_ws_market_ready()
        self._orders_ws_task = asyncio.create_task(self._orders_ws_loop())
        logger.info("Lighter account_orders WebSocket task started")

    def _create_ws_auth_token(self, *, deadline_seconds: int = 600) -> str | None:
        if not self._signer:
            return self._auth_token
        try:
            ts = int(time.time()) - 60
            auth, err = self._signer.create_auth_token_with_expiry(
                int(deadline_seconds),
                timestamp=ts,
                api_key_index=self._api_key_index,
            )
            if err:
                logger.warning(f"Failed to create WS auth token: {err}")
                return self._auth_token
            return auth or self._auth_token
        except Exception as e:
            logger.warning(f"Failed to create WS auth token: {e}")
            return self._auth_token

    def _fail_trading_ws_futures(self, exc: Exception) -> None:
        for fut in list(self._trading_ws_response_futures.values()):
            if not fut.done():
                with contextlib.suppress(Exception):
                    fut.set_exception(exc)
        self._trading_ws_response_futures.clear()

    async def _start_trading_websocket(self) -> None:
        if self._trading_ws_task:
            return
        self._trading_ws_connected.clear()
        self._trading_ws_task = asyncio.create_task(self._trading_ws_loop())
        logger.info("Lighter trading WebSocket task started")

    async def ensure_trading_ws(self, *, timeout: float) -> bool:
        if not self._use_ws_orders:
            return False
        if not self._trading_ws_task:
            await self._start_trading_websocket()
        if timeout <= 0:
            return True
        try:
            await asyncio.wait_for(self._trading_ws_connected.wait(), timeout=max(0.0, float(timeout)))
        except TimeoutError:
            return False
        return bool(self._trading_ws_connected.is_set())

    async def _trading_ws_loop(self) -> None:
        """
        Persistent WS connection for `jsonapi/sendtx` to avoid HTTP handshake per transaction.

        Messages are dispatched by `data.id` into `_trading_ws_response_futures`.
        """
        try:
            import websockets
        except Exception:
            logger.warning("websockets package not available; trading WS disabled")
            return

        ws_cfg = getattr(self.settings, "websocket", None)
        ping_interval = float(getattr(ws_cfg, "ping_interval", 15.0) or 15.0)
        ping_timeout = float(getattr(ws_cfg, "ping_timeout", 10.0) or 10.0)
        base_delay = float(getattr(ws_cfg, "reconnect_delay_initial", 2.0) or 2.0)
        max_delay = float(getattr(ws_cfg, "reconnect_delay_max", 120.0) or 120.0)
        delay = max(0.1, base_delay)

        url = self._get_ws_stream_url()

        while True:
            try:
                async with websockets.connect(url, ping_interval=ping_interval, ping_timeout=ping_timeout) as ws:  # type: ignore[attr-defined]
                    self._trading_ws = ws
                    self._trading_ws_connected.set()
                    delay = max(0.1, base_delay)

                    while True:
                        raw = await ws.recv()
                        if not raw:
                            continue
                        try:
                            msg = json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            continue
                        if not isinstance(msg, dict):
                            continue

                        if msg.get("type") == "ping":
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"type": "pong"}))
                            continue

                        data = msg.get("data") if isinstance(msg.get("data"), dict) else None
                        req_id: str | None = None
                        if data and isinstance(data.get("id"), str):
                            req_id = data.get("id")
                        elif isinstance(msg.get("id"), str):
                            req_id = msg.get("id")

                        if req_id and req_id in self._trading_ws_response_futures:
                            fut = self._trading_ws_response_futures.pop(req_id)
                            if not fut.done():
                                fut.set_result(msg)
                        else:
                            # Common initial message is "connected"; keep noise low.
                            if msg.get("type") != "connected":
                                logger.debug(f"Lighter trading WS unhandled message: {msg.get('type')}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._trading_ws_connected.clear()
                self._trading_ws = None
                self._fail_trading_ws_futures(RuntimeError(f"trading ws disconnected: {e}"))

            jitter = random.random() * 0.25 * delay
            await asyncio.sleep(min(max_delay, delay + jitter))
            delay = min(max_delay, max(base_delay, delay * 2))

    async def _trading_ws_send_tx(
        self,
        *,
        tx_type: int,
        tx_info_json: str,
        request_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        ws = self._trading_ws
        if not ws or not self._trading_ws_connected.is_set():
            raise RuntimeError("trading ws not connected")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._trading_ws_response_futures[request_id] = fut

        payload = {
            "type": "jsonapi/sendtx",
            "data": {
                "id": request_id,
                "tx_type": int(tx_type),
                "tx_info": json.loads(tx_info_json),
            },
        }

        try:
            await ws.send(json.dumps(payload))
            msg = await asyncio.wait_for(fut, timeout=max(0.0, float(timeout)))
        except Exception:
            with contextlib.suppress(Exception):
                self._trading_ws_response_futures.pop(request_id, None)
            raise

        if not isinstance(msg, dict):
            raise RuntimeError(f"unexpected ws response type: {type(msg)}")
        return msg

    async def _ws_submit_signed_tx(
        self,
        *,
        purpose: str,
        signer_fn: Callable[[int, int], tuple[int | None, str | None, str | None, str | None]],
        timeout: float,
        retry_on_invalid_nonce: bool = True,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """
        Sign a tx with a fresh nonce and submit it over WS.

        Returns (tx_hash, ws_response).
        """
        if not self._signer:
            raise RuntimeError("signer unavailable")

        api_key_index = int(self._api_key_index)

        def _next_nonce() -> int:
            nonlocal api_key_index
            try:
                _, n = self._signer.nonce_manager.next_nonce(api_key_index)  # type: ignore[attr-defined]
                return int(n)
            except Exception:
                ak, n = self._signer.nonce_manager.next_nonce()  # type: ignore[attr-defined]
                api_key_index = int(ak)
                return int(n)

        async def _attempt(*, nonce: int) -> tuple[str | None, dict[str, Any] | None]:
            tx_type, tx_info, tx_hash, err = signer_fn(api_key_index, nonce)
            if err or tx_type is None or not tx_info:
                with contextlib.suppress(Exception):
                    self._signer.nonce_manager.acknowledge_failure(api_key_index)  # type: ignore[attr-defined]
                raise RuntimeError(str(err or "sign failed"))

            req_id = f"{purpose}_{int(time.time() * 1000)}_{random.randint(0, 1_000_000)}"
            t0 = time.perf_counter()
            try:
                ws_resp = await self._trading_ws_send_tx(
                    tx_type=int(tx_type),
                    tx_info_json=str(tx_info),
                    request_id=req_id,
                    timeout=timeout,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    self._signer.nonce_manager.acknowledge_failure(api_key_index)  # type: ignore[attr-defined]
                raise
            dt_ms = (time.perf_counter() - t0) * 1000.0

            data = ws_resp.get("data") if isinstance(ws_resp.get("data"), dict) else {}
            code = data.get("code", ws_resp.get("code"))
            message = data.get("message", ws_resp.get("message"))
            predicted = data.get("predicted_execution_time_ms", ws_resp.get("predicted_execution_time_ms"))
            if code is not None and int(code) != 200:
                with contextlib.suppress(Exception):
                    self._signer.nonce_manager.acknowledge_failure(api_key_index)  # type: ignore[attr-defined]
                raise RuntimeError(f"ws sendtx rejected: code={code} message={message}")

            logger.info(
                f"Lighter WS sendtx ok: purpose={purpose} api_key_index={api_key_index} nonce={nonce} "
                f"ws_rtt_ms={dt_ms:.1f} predicted_execution_time_ms={predicted}"
            )
            return (str(tx_hash) if tx_hash else None), ws_resp

        nonce = _next_nonce()
        try:
            return await _attempt(nonce=nonce)
        except Exception as e:
            if retry_on_invalid_nonce and "invalid nonce" in str(e).lower():
                with contextlib.suppress(Exception):
                    self._signer.nonce_manager.hard_refresh_nonce(api_key_index)  # type: ignore[attr-defined]
                nonce = _next_nonce()
                return await _attempt(nonce=nonce)
            raise

    async def ensure_orders_ws_market(self, symbol: str, *, timeout: float) -> bool:
        """
        Ensure we are subscribed to `account_orders/{market}/{account}` for this symbol.

        Returns True if the WS is connected and we have best-effort enqueued the subscription.

        Note: Per Lighter WS reference, `account_orders` updates are emitted as `update/account_orders`.
        In practice, you may not receive any message until an order state changes (i.e., after you
        place an order). Therefore, "no message yet" within a short timeout must not be treated as
        "not subscribed" for execution gating.
        """
        if self._account_index is None:
            return False

        if not self._markets:
            with contextlib.suppress(Exception):
                await self.load_markets()

        market_index = self._get_market_index(symbol)
        # Lighter market indices can be 0 (e.g. ETH); treat only negative as invalid.
        if market_index < 0:
            return False

        await self._start_orders_websocket()

        self._orders_ws_markets.add(int(market_index))
        market_ev = self._get_orders_ws_market_event(int(market_index))
        # If the WS is not connected, a previously-set event may be stale (from an earlier run).
        if market_ev.is_set() and not self.ws_ready():
            market_ev.clear()
        # enqueue subscribe (best-effort)
        with contextlib.suppress(Exception):
            self._orders_ws_subscribe_queue.put_nowait(int(market_index))

        try:
            # Wait for the WS connection to be up first.
            # We purposely do NOT require an `update/account_orders` message here: it may not arrive
            # until after an order is placed.
            t0 = time.monotonic()
            await asyncio.wait_for(self._orders_ws_ready.wait(), timeout=max(0.0, float(timeout)))

            # Best-effort: if an update arrives quickly, mark per-market readiness; otherwise still proceed.
            remaining = float(timeout) - max(0.0, time.monotonic() - t0)
            if remaining > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(market_ev.wait(), timeout=remaining)
        except TimeoutError:
            return False

        return self.ws_ready()

    async def _orders_ws_loop(self) -> None:
        """Dedicated websocket loop to receive update/account_orders events."""
        try:
            import websockets
        except Exception:
            logger.warning("websockets package not available; account_orders WS disabled")
            return

        ws_cfg = getattr(self.settings, "websocket", None)
        base_delay = float(getattr(ws_cfg, "reconnect_delay_initial", 2.0) or 2.0)
        max_delay = float(getattr(ws_cfg, "reconnect_delay_max", 120.0) or 120.0)
        delay = max(0.1, base_delay)

        url = self._get_ws_stream_url()

        while True:
            try:
                auth = self._create_ws_auth_token(deadline_seconds=600)
                if not auth:
                    logger.warning("Lighter account_orders WS: missing auth token; will retry")
                    raise RuntimeError("missing auth token")

                async with websockets.connect(url) as ws:
                    self._orders_ws_ready.set()
                    self._clear_orders_ws_market_ready()
                    delay = max(0.1, base_delay)

                    async def _send_subscribe(mid: int, auth: str | None = auth) -> None:
                        msg = {
                            "type": "subscribe",
                            "channel": f"account_orders/{mid}/{self._account_index}",
                            "auth": auth,
                        }
                        await ws.send(json.dumps(msg))

                    # Subscribe to any markets requested so far.
                    for mid in sorted(self._orders_ws_markets):
                        with contextlib.suppress(Exception):
                            await _send_subscribe(int(mid))

                    while True:
                        # flush queued subscription requests
                        while True:
                            try:
                                mid = self._orders_ws_subscribe_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            with contextlib.suppress(Exception):
                                await _send_subscribe(int(mid))

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except TimeoutError:
                            continue

                        try:
                            data = json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            continue

                        msg_type = str(data.get("type", "") if isinstance(data, dict) else "")
                        if msg_type == "ping":
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"type": "pong"}))
                            continue

                        if msg_type in ("subscribed/account_orders", "update/account_orders"):
                            # Mark per-market readiness from channel/orders keys.
                            ch = str(data.get("channel", "") if isinstance(data, dict) else "")
                            if ch.startswith("account_orders:"):
                                with contextlib.suppress(Exception):
                                    mid = int(ch.split(":", 1)[1])
                                    self._get_orders_ws_market_event(mid).set()
                            orders_obj = data.get("orders") if isinstance(data, dict) else None
                            if isinstance(orders_obj, dict):
                                for k in list(orders_obj.keys()):
                                    with contextlib.suppress(Exception):
                                        mid = int(str(k))
                                        self._get_orders_ws_market_event(mid).set()

                            # Reuse the account update parser to map orders and dispatch callbacks.
                            with contextlib.suppress(Exception):
                                self._on_ws_account_update(self._account_index, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._orders_ws_ready.clear()
                self._clear_orders_ws_market_ready()
                msg = str(e)
                if "no close frame received or sent" in msg:
                    logger.warning(f"Lighter account_orders WS disconnected: {msg}")
                else:
                    logger.warning(f"Lighter account_orders WS error: {msg}")

            jitter = random.random() * 0.25 * delay
            await asyncio.sleep(min(max_delay, delay + jitter))
            delay = min(max_delay, max(base_delay, delay * 2))

    async def _ws_loop(self) -> None:
        """WebSocket connection loop."""
        ws = getattr(self.settings, "websocket", None)
        base_delay = float(getattr(ws, "reconnect_delay_initial", 2.0) or 2.0)
        max_delay = float(getattr(ws, "reconnect_delay_max", 120.0) or 120.0)
        delay = max(0.1, base_delay)

        while True:
            try:
                if not self._ws_client_cls:
                    logger.warning("WS Client not available")
                    return

                # Reset per-connection orderbook continuity state so we don't false-positive on reconnection.
                self._ws_orderbook_last_nonce.clear()
                self._ws_orderbook_last_offset.clear()

                account_ids: list[int] = [self._account_index] if self._account_index is not None else []
                order_book_ids: list[int] = list(self._ws_order_book_ids or [])
                if not account_ids and not order_book_ids:
                    logger.warning("No WS subscriptions configured (no account_ids, no order_book_ids)")
                    return

                # Initialize WsClient
                ws_host = str(self._base_url or "").replace("https://", "").replace("http://", "").strip().strip("/")
                client = self._ws_client_cls(
                    host=ws_host or None,
                    account_ids=account_ids,
                    order_book_ids=order_book_ids,
                    on_order_book_update=self._on_ws_order_book_update,
                    on_account_update=self._on_ws_account_update,
                )

                logger.info(
                    f"Connecting to Lighter WS... account_ids={account_ids} order_book_ids={len(order_book_ids)}"
                )
                await client.run_async()

            except asyncio.CancelledError:
                break
            except Exception as e:
                msg = str(e)
                # Common for transient network disconnects: server drops TCP without WS close handshake.
                if "no close frame received or sent" in msg:
                    logger.warning(f"Lighter WS disconnected: {msg}")
                else:
                    logger.error(f"Lighter WS error: {msg}")
            else:
                logger.warning("Lighter WS stream ended; reconnecting with backoff")

            jitter = random.random() * 0.25 * delay
            await asyncio.sleep(min(max_delay, delay + jitter))
            delay = min(max_delay, max(base_delay, delay * 2))

    def _on_ws_account_update(self, account_id: int, data: dict[str, Any]) -> None:
        """Handle WS account updates."""
        # Check matching account (should be correct via subscription)
        if self._account_index is not None and account_id != self._account_index:
            return

        try:
            if not self._ws_account_ready.is_set():
                # Mark ready as soon as we see any account stream message; we don't depend on order presence.
                self._ws_account_ready.set()
                if not self._ws_account_ready_logged:
                    msg_type = str(data.get("type", "") if isinstance(data, dict) else "")
                    logger.info(f"Lighter WS account stream ready (first message type={msg_type})")
                    self._ws_account_ready_logged = True

            # WsClient passes the *full message* for update/account_all:
            # see src/funding_bot/adapters/exchanges/lighter/ws_client.py -> handle_update_account()
            # Be robust: "orders"/"positions" might be nested and/or dict keyed by id.
            payload: Any = data
            if isinstance(payload, dict):
                for key in ("account", "account_all", "data", "state"):
                    inner = payload.get(key)
                    if isinstance(inner, dict) and ("orders" in inner or "positions" in inner):
                        payload = inner
                        break

            # 1. Orders
            orders_obj = payload.get("orders", []) if isinstance(payload, dict) else []
            orders_iter: list[dict[str, Any]] = []

            def _is_order_dict(obj: Any) -> bool:
                if not isinstance(obj, dict):
                    return False
                return any(
                    k in obj
                    for k in (
                        "order_id",
                        "order_index",
                        "client_order_id",
                        "client_order_index",
                        "market_index",
                        "market_id",
                    )
                )

            def _flatten_orders(obj: Any) -> list[dict[str, Any]]:
                # account_all uses: orders={market_index: [Order, ...]} (dict-of-lists).
                # We flatten this so we can emit per-order updates (mirrors `.tmp` order-callback model).
                if obj is None:
                    return []
                if _is_order_dict(obj):
                    return [obj]
                if isinstance(obj, dict):
                    out: list[dict[str, Any]] = []
                    for v in obj.values():
                        out.extend(_flatten_orders(v))
                    return out
                if isinstance(obj, list):
                    out = []
                    for v in obj:
                        out.extend(_flatten_orders(v))
                    return out
                return []

            orders_iter = _flatten_orders(orders_obj)

            for o_data in orders_iter:
                if not isinstance(o_data, dict):
                    continue
                # Map to Order
                status_map = {
                    "open": OrderStatus.OPEN,
                    "filled": OrderStatus.FILLED,
                    "cancelled": OrderStatus.CANCELLED,
                    "canceled": OrderStatus.CANCELLED,
                    "partially_filled": OrderStatus.PARTIALLY_FILLED,
                }
                status_str = str(o_data.get("status", "")).lower()
                status = status_map.get(status_str, OrderStatus.OPEN)  # Default to OPEN if unknown but present

                # If status is unknown, maybe map from remaining amount?
                if (
                    ("remaining_base_amount" in o_data and _safe_decimal(o_data["remaining_base_amount"]) == 0)
                    or ("remaining_size" in o_data and _safe_decimal(o_data["remaining_size"]) == 0)
                    and status == OrderStatus.OPEN
                ):
                    status = OrderStatus.FILLED

                # Market ID to Symbol
                market_id = 0
                with contextlib.suppress(Exception):
                    market_id = int(o_data.get("market_id") or o_data.get("market_index") or 0)
                symbol = str(o_data.get("symbol") or self._market_id_to_symbol.get(market_id, "UNKNOWN"))

                side_val = o_data.get("side", "")  # 'buy' or 'sell'
                if side_val:
                    side = Side.BUY if str(side_val).lower() == "buy" else Side.SELL
                else:
                    # Lighter Order JSON includes `is_ask` boolean (ask = sell).
                    is_ask = bool(o_data.get("is_ask", False))
                    side = Side.SELL if is_ask else Side.BUY

                initial_qty = _safe_decimal(
                    o_data.get("initial_base_amount") or o_data.get("initial_size") or o_data.get("size")
                )
                has_explicit_filled = any(k in o_data for k in ("filled_base_amount", "filled_size", "filled_amount"))
                filled_qty = _safe_decimal(
                    o_data.get("filled_base_amount", None)
                    if "filled_base_amount" in o_data
                    else (
                        o_data.get("filled_size", None)
                        if "filled_size" in o_data
                        else o_data.get("filled_amount", None)
                    )
                )
                remaining_qty = _safe_decimal(o_data.get("remaining_base_amount") or o_data.get("remaining_size"))

                # Derive filled_qty from initial-remaining ONLY if there is no explicit filled field.
                # We've observed some WS payloads where `remaining_*` is actually a filled-like value;
                # deriving in that case causes massive overestimation and over-hedging.
                if (not has_explicit_filled) and initial_qty > 0 and remaining_qty >= 0:
                    computed = initial_qty - remaining_qty
                    if computed > 0:
                        filled_qty = computed

                avg_fill_price = _safe_decimal(o_data.get("avg_filled_price") or o_data.get("avg_fill_price"))
                if avg_fill_price <= 0:
                    # If we have filled quote amount, we can compute avg price = quote/base.
                    filled_quote = _safe_decimal(o_data.get("filled_quote_amount"))
                    if filled_quote > 0 and filled_qty > 0:
                        avg_fill_price = filled_quote / filled_qty
                price = _safe_decimal(o_data.get("price"))
                avg_fill_price = _clamp_avg_fill_price(avg_fill_price, price)

                order = Order(
                    order_id=str(o_data.get("order_id") or o_data.get("order_index")),
                    symbol=symbol,
                    exchange=Exchange.LIGHTER,
                    side=side,
                    order_type=OrderType.LIMIT,  # WS might not have type, assume Limit
                    qty=initial_qty,
                    price=price,
                    status=status,
                    filled_qty=filled_qty,
                    avg_fill_price=avg_fill_price if avg_fill_price > 0 else price,  # Fallback
                    fee=Decimal("0"),  # WS might not have fee
                    client_order_id=str(o_data.get("client_order_id") or o_data.get("client_order_index") or ""),
                )

                # Cache client->system mapping so pending_{client}_{market} placeholders can be resolved
                # without extra REST calls, and WS-driven fill waits can correlate immediately.
                if order.client_order_id and order.order_id:
                    self._client_order_id_map[str(order.client_order_id)] = str(order.order_id)

                # 🚀 PERFORMANCE: Populate fill cache for instant get_order() lookups
                # Store terminal states (FILLED/CANCELLED) for fast detection.
                # Avoids 2-5s REST API timeout in critical close/entry flows.
                if order.client_order_id and order.status in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.PARTIALLY_FILLED,
                ):
                    asyncio.create_task(self._cache_filled_order(order))

                # Dispatch
                for cb in self._order_callbacks:
                    asyncio.create_task(cb(order))

            # 2. Positions
            positions_obj = payload.get("positions", []) if isinstance(payload, dict) else []
            if isinstance(positions_obj, dict):
                positions_iter = list(positions_obj.values())
            elif isinstance(positions_obj, list):
                positions_iter = positions_obj
            else:
                positions_iter = []

            for p_data in positions_iter:
                if not isinstance(p_data, dict):
                    continue
                symbol = str(p_data.get("symbol", ""))
                # If symbol not in data, try market_id?
                # Usually 'symbol' is present in enriched updates
                if not symbol:
                    with contextlib.suppress(Exception):
                        symbol = self._market_id_to_symbol.get(int(p_data.get("market_id", 0)), "")

                qty = _safe_decimal(p_data.get("size") or p_data.get("position"))
                if qty == 0:
                    # Zero position might imply closed, but Position model represents open positions
                    # We might want to dispatch it as closed (qty=0)
                    pass

                # Determine side from sign or size
                # 'position' is signed float in some contexts, or 'size' + 'side'
                raw_size = _safe_decimal(p_data.get("position", 0))
                side = Side.BUY
                if raw_size < 0:
                    side = Side.SELL
                    qty = abs(raw_size)
                else:
                    qty = raw_size

                if qty == 0:
                    # Check if 'size' is present
                    qty = _safe_decimal(p_data.get("size", 0))
                    side_str = str(p_data.get("side", "buy")).lower()
                    side = Side.BUY if side_str == "buy" else Side.SELL

                pos = Position(
                    symbol=symbol,
                    exchange=Exchange.LIGHTER,
                    side=side,
                    qty=qty,
                    entry_price=_safe_decimal(p_data.get("entry_price") or p_data.get("avg_entry_price")),
                    mark_price=Decimal("0"),  # Not in account update
                    unrealized_pnl=_safe_decimal(p_data.get("unrealized_pnl")),
                    leverage=Decimal("1"),
                )

                for cb in self._position_callbacks:
                    asyncio.create_task(cb(pos))

        except Exception as e:
            logger.error(f"Error processing WS update: {e}")

    def ws_ready(self) -> bool:
        """
        Best-effort WS readiness indicator for fast per-order updates.

        True when the dedicated `account_orders` WS is connected.
        """
        return bool(self._orders_ws_ready.is_set()) and bool(self._orders_ws_task and not self._orders_ws_task.done())

    async def wait_ws_ready(self, *, timeout: float) -> bool:
        """
        Wait until WS streams needed for fast execution are ready.

        Returns False on timeout or when WS is unavailable.
        """
        if self.ws_ready():
            return True

        if not self._ws_task:
            await self._start_websocket()
        if not self._orders_ws_task:
            await self._start_orders_websocket()

        try:
            await asyncio.wait_for(self._orders_ws_ready.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return False

        return self.ws_ready()

    def _mark_ws_orderbook_stale(self, symbol: str) -> None:
        """
        Invalidate WS-fed orderbook/price caches for a symbol.

        We keep an explicit old timestamp to prevent MarketDataService's hydration fallback
        from treating missing timestamps as "fresh now".
        """
        stale_ts = datetime.fromtimestamp(0, tz=UTC)
        with contextlib.suppress(Exception):
            self._orderbook_cache.pop(symbol, None)
        self._orderbook_updated_at[symbol] = stale_ts
        # Mid price is derived from the orderbook; treat it as stale as well.
        with contextlib.suppress(Exception):
            self._price_cache.pop(symbol, None)
        self._price_updated_at[symbol] = stale_ts

    def _on_ws_order_book_update(self, market_id: str, order_book: dict[str, Any]) -> None:
        """Handle WS orderbook updates and keep local cache hot via LocalOrderbook."""
        try:
            mid = int(str(market_id))
        except Exception:
            return

        symbol = self._market_id_to_symbol.get(mid)
        if not symbol:
            return

        # Ensure LocalOrderbook exists
        if mid not in self._ob_ws_books:
            self._ob_ws_books[mid] = LocalOrderbook(symbol, mid)
            # If we don't have a snapshot, we might be receiving updates before snapshot?
            # WS client usually sends snapshot as first update (if type is `subscribed/order_book`).
            # But the SDK wrapper might abstract that.
            # If we start empty, incremental updates might fail.
            # However, WsClient handles initial snapshot too (sending it as partial update?).
            # Let's assume WsClient sends correct sequence.
            # To be safe, mark as loaded if we assumed it.
            # But wait, `LocalOrderbook.apply_update` requires `snapshot_loaded=True`.
            # We need to manually set it if WsClient streams "update/order_book" as the first message?
            # Actually, `WsClient` implementation details matter.
            # Assuming standard Lighter behavior: subscribe -> snapshot -> updates.
            pass

        book = self._ob_ws_books[mid]

        # WsClient might pass raw "update/order_book" payload as `order_book`.
        # Check if it looks like a snapshot (huge size, type field?)
        # WsClient abstraction: `on_order_book_update` receives `order_book` dict.

        # We'll just call `apply_update` because `LocalOrderbook` handles nonces.
        # But if it's the FIRST message, `snapshot_loaded` is False.
        if not book.snapshot_loaded:
            # Assume first message is a snapshot-like update or just force it?
            # For now, let's treat it as snapshot if it has many levels?
            # Or just allow update to pass if we trust WsClient?
            # We'll use apply_snapshot for the first one to be safe/lazy.
            book.apply_snapshot(order_book)
        else:
            try:
                book.apply_update(order_book)
                if not book.snapshot_loaded:
                    # Nonce gap detected by LocalOrderbook (it marks snapshot as invalid).
                    self._mark_ws_orderbook_stale(symbol)
                    return
            except ExchangeError as e:
                # Gap detected
                logger.warning(f"Lighter Shared WS Gap: {e}")
                # We can't easily resync the Shared WS without reconnecting EVERYTHING.
                # So we just mark it stale and hope for the best (or implementation details handle it).
                self._mark_ws_orderbook_stale(symbol)
                # Invalidate the book
                book.snapshot_loaded = False
                msg = str(e).lower()
                if "gap (nonce)" in msg:
                    raise RuntimeError(f"nonce gap: {e}") from e
                if "gap (offset)" in msg:
                    raise RuntimeError(f"offset gap: {e}") from e
                raise RuntimeError(str(e)) from e

        # Update cache (Top of Book)
        l1 = book.get_l1()
        now = datetime.now(UTC)

        if l1["best_bid"] > 0 or l1["best_ask"] > 0:
            self._orderbook_cache[symbol] = l1
            self._orderbook_updated_at[symbol] = now

        if l1["best_bid"] > 0 and l1["best_ask"] > 0 and l1["best_bid"] < l1["best_ask"]:
            self._price_cache[symbol] = (l1["best_bid"] + l1["best_ask"]) / 2
            self._price_updated_at[symbol] = now

    def _ob_ws_update_cache_from_book(self, market_id: int) -> None:
        """Update L1 cache from the full LocalOrderbook."""
        book = self._ob_ws_books.get(market_id)
        if not book:
            return

        l1 = book.get_effective_l1(min_notional=Decimal("10"))
        symbol = self._market_id_to_symbol.get(market_id, "")
        if not symbol:
            return

        now = datetime.now(UTC)
        if l1["best_bid"] > 0 or l1["best_ask"] > 0:
            self._orderbook_cache[symbol] = l1
            self._orderbook_updated_at[symbol] = now

        if l1["best_bid"] > 0 and l1["best_ask"] > 0 and l1["best_bid"] < l1["best_ask"]:
            self._price_cache[symbol] = (l1["best_bid"] + l1["best_ask"]) / 2
            self._price_updated_at[symbol] = now
